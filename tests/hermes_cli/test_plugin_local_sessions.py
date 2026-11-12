"""Local skill admission follows the owner's real durable local-session FIFO."""
import asyncio
import hashlib
import shutil
from types import SimpleNamespace

import pytest

from hermes_cli.plugin_local_sessions import LocalSkillSubmissionError, PluginLocalSessions


@pytest.fixture
def skill(tmp_path):
    path = tmp_path / "skills" / "triage" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Triage\nReturn a concise classification.\n", encoding="utf-8")
    return path


async def _service(home, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority

    config = {"model": {"default": "fixture-model"}, "platform_toolsets": {"cli": []},
              "security": {"output_policy": {"slack": "deny"}},
              "plugins": {"entries": {"example-plugin": {"allow_gateway_injection": True}}}}
    monkeypatch.setenv("HERMES_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "model:\n  default: fixture-model\n"
        "security:\n  output_policy:\n    slack: deny\n"
        "plugins:\n  entries:\n    example-plugin:\n      allow_gateway_injection: true\n",
        encoding="utf-8")
    monkeypatch.setattr(run, "_load_gateway_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(run, "_resolve_gateway_model", lambda _config: "fixture-model")
    store = SessionStore(home, GatewayConfig())
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    store._db = db
    runner = SimpleNamespace(session_store=store, _session_db=db, adapters={}, _draining=False,
                             _cached_agent_for=lambda _route: None, _adapter_for_source=lambda _source: None)
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id="local-skill-test", db=db)
    authority._schedule = lambda _ref: None
    monkeypatch.setattr(run, "_gateway_runner_ref", lambda: runner)
    from gateway.session_authorities import authority_for_home
    assert authority_for_home(runner, home) is authority, (authority.db.db_path, home)
    return PluginLocalSessions("example-plugin", home), authority


def _version(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_real_local_admission_is_deduplicated_and_replay_survives_skill_change(tmp_path, monkeypatch, skill):
    service, authority = await _service(tmp_path, monkeypatch)
    request = dict(skill_id="triage", skill_version=_version(skill), input={"topic": "alpha"},
                   idempotency_key="job-1")

    first = await service.submit_skill(**request)
    restarted = PluginLocalSessions("example-plugin", tmp_path)
    replay = await restarted.submit_skill(**request)
    queried = await restarted.get_submission(idempotency_key="job-1")
    skill.write_text("# Changed\nDifferent body.\n", encoding="utf-8")
    after_change = await service.submit_skill(**request)

    assert first["status"] == "accepted", first
    assert first["session_id"] == replay["session_id"] == after_change["session_id"]
    assert first["submission_id"] == replay["submission_id"] == after_change["submission_id"]
    assert queried["status"] == "FOUND"
    assert queried["receipt"]["request_digest"] == first["request_digest"]
    from hermes_state_runtime import list_session_admissions
    rows = list_session_admissions(authority.db, session_id=first["session_id"], pending_only=False)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_terminal_receipt_reads_the_canonical_result(tmp_path, monkeypatch, skill):
    service, authority = await _service(tmp_path, monkeypatch)
    accepted = await service.submit_skill(skill_id="triage", skill_version=_version(skill), input={"topic": "alpha"},
                                          idempotency_key="job-terminal")
    from hermes_state_runtime import claim_session_input, settle_session_input
    claimed = claim_session_input(authority.db, epoch=authority.epoch, session_id=accepted["session_id"])
    assert claimed["admission_id"] == accepted["submission_id"]
    settle_session_input(authority.db, epoch=authority.epoch, admission_id=claimed["admission_id"],
                         generation=claimed["generation"], outcome="completed",
                         result={"result": {"final_response": "classified", "messages": []}, "usage": {}})

    receipt = await service.get_submission(idempotency_key="job-terminal")

    assert receipt["status"] == "FOUND"
    assert receipt["receipt"]["status"] == "completed"
    assert receipt["receipt"]["result"] == {"text": "classified"}
    stale = {**receipt["receipt"], "status": "accepted", "result": None}
    saved = await asyncio.to_thread(service._save_receipt, "job-terminal",
                                     accepted["request_digest"], stale)
    assert saved == receipt["receipt"]
    authority.db.delete_session(accepted["session_id"])

    retained = await service.get_submission(idempotency_key="job-terminal")

    assert retained == receipt


@pytest.mark.asyncio
async def test_idempotency_conflict_and_profile_scoped_not_found(tmp_path, monkeypatch, skill):
    service, _authority = await _service(tmp_path, monkeypatch)
    request = dict(skill_id="triage", skill_version=_version(skill), input={"topic": "alpha"},
                   idempotency_key="job-1")
    await service.submit_skill(**request)

    with pytest.raises(LocalSkillSubmissionError, match="IDEMPOTENCY_CONFLICT"):
        await service.submit_skill(**(request | {"input": {"topic": "different"}}))
    assert await service.get_submission(idempotency_key="job-1", profile_id="another-profile") == {
        "status": "NOT_FOUND"}
    assert await service.get_submission(idempotency_key="never-seen") == {"status": "NOT_FOUND",
                                                                           "idempotency_key": "never-seen"}


@pytest.mark.asyncio
async def test_lost_submit_ack_reconciles_same_canonical_admission(tmp_path, monkeypatch, skill):
    service, authority = await _service(tmp_path, monkeypatch)
    submit = authority.submit
    lost = False

    async def submit_then_lose_ack(*args, **kwargs):
        nonlocal lost
        receipt = await submit(*args, **kwargs)
        if not lost:
            lost = True
            raise RuntimeError("simulated lost acknowledgement")
        return receipt

    monkeypatch.setattr(authority, "submit", submit_then_lose_ack)
    request = dict(skill_id="triage", skill_version=_version(skill), input={"topic": "alpha"},
                   idempotency_key="lost-ack")

    first = await service.submit_skill(**request)
    replay = await service.submit_skill(**request)
    queried = await service.get_submission(idempotency_key="lost-ack")

    assert first["status"] == "UNKNOWN"
    assert replay["status"] == "accepted"
    assert queried["receipt"]["submission_id"] == replay["submission_id"]
    from hermes_state_runtime import list_session_admissions
    rows = list_session_admissions(authority.db, session_id=replay["session_id"], pending_only=False)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_gateway_injection_opt_out_keeps_receipt_readable_and_blocks_new_admission(
        tmp_path, monkeypatch, skill):
    service, authority = await _service(tmp_path, monkeypatch)
    request = dict(skill_id="triage", skill_version=_version(skill), input={"topic": "alpha"},
                   idempotency_key="accepted-before-opt-out")
    accepted = await service.submit_skill(**request)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "model:\n  default: fixture-model\n"
        "security:\n  output_policy:\n    slack: deny\n"
        "plugins:\n  entries:\n    example-plugin:\n      allow_gateway_injection: false\n",
        encoding="utf-8")

    replay = await service.submit_skill(**request)
    queried = await service.get_submission(idempotency_key=request["idempotency_key"])
    from hermes_state_runtime import list_session_admissions
    before = list_session_admissions(authority.db, session_id=accepted["session_id"], pending_only=False)
    with pytest.raises(LocalSkillSubmissionError, match="GATEWAY_INJECTION_NOT_ALLOWED"):
        await service.submit_skill(**(request | {"idempotency_key": "after-opt-out"}))
    after = list_session_admissions(authority.db, session_id=accepted["session_id"], pending_only=False)

    assert replay["submission_id"] == accepted["submission_id"]
    assert queried["status"] == "FOUND"
    assert len(before) == len(after) == 1


@pytest.mark.asyncio
async def test_missing_initialized_ledger_returns_unknown_without_relaunch(tmp_path, monkeypatch, skill):
    service, _authority = await _service(tmp_path, monkeypatch)
    accepted = await service.submit_skill(skill_id="triage", skill_version=_version(skill), input={},
                                          idempotency_key="job-1")
    ledger = tmp_path / "local" / "plugin-local-sessions" / "receipts.sqlite3"
    ledger.unlink()

    queried = await service.get_submission(idempotency_key="job-1")

    assert accepted["status"] == "accepted"
    assert queried["status"] == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.platforms("posix")
async def test_callback_after_profile_removal_does_not_recreate_home(tmp_path, monkeypatch):
    home = tmp_path / "removed-profile"
    home.mkdir()
    skill = home / "skills" / "triage" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Triage\n", encoding="utf-8")
    service, authority = await _service(home, monkeypatch)
    accepted = await service.submit_skill(skill_id="triage", skill_version=_version(skill), input={},
                                          idempotency_key="before-removal")
    authority.db.close()
    shutil.rmtree(home)

    receipt = await service.get_submission(idempotency_key="before-removal")

    assert accepted["status"] == "accepted"
    assert receipt["status"] == "UNKNOWN"
    assert not home.exists()


@pytest.mark.platforms("posix")
def test_skill_root_symlink_cannot_escape_profile(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    skill = outside / "triage" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Outside\n", encoding="utf-8")
    (tmp_path / "skills").symlink_to(outside, target_is_directory=True)
    service = PluginLocalSessions("example-plugin", tmp_path)

    with pytest.raises(LocalSkillSubmissionError, match="SKILL_UNAVAILABLE"):
        service._skill_path("triage")
