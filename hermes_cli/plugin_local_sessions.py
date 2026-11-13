"""Profile-scoped durable local skill submissions for trusted plugins.

This adapter admits a Markdown skill into a durable local session. The restricted
mode uses a no-tools worker; normal local sessions use the profile's usual tools
and plugins. The gateway owns the session and its canonical admission FIFO.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Mapping

from gateway.session_contract import SessionActor, SessionRef, Submission
from persistence.core.errors import AuthorityRefusal as RuntimeStoreError

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SKILL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
_MAX_INPUT_CHARS = 65_536
_MAX_SKILL_BYTES = 262_144


class LocalSkillSubmissionError(ValueError):
    """A definite local submission refusal with a stable machine-readable code."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        super().__init__(detail or code)


class PluginLocalSessions:
    """A plugin's private view of durable, profile-local skill sessions.

    ``profile_home`` and ``profile_id`` are host-derived by the caller. Methods
    are async and execute on the gateway owner loop.
    """

    def __init__(self, plugin_id: str, profile_home: str | Path, profile_id: str | None = None):
        self.plugin_id = str(plugin_id)
        self.profile_home = Path(profile_home).resolve()
        if profile_id is None:
            from gateway.session_authorities import served_profile_name
            profile_id = served_profile_name(self.profile_home)
        self.profile_id = str(profile_id)

    async def submit_skill(self, *, skill_id: str, skill_version: str, input: Mapping,
                           idempotency_key: str, output_policy: str = "local_only",
                           profile_id: str | None = None) -> dict:
        """Submit a frozen Markdown skill once; retry/query by the same key after uncertainty."""
        self._check_profile(profile_id)
        identity = self._request_identity(skill_id, skill_version, input, idempotency_key, output_policy)
        try:
            existing = await asyncio.to_thread(self._load_row, idempotency_key)
        except (sqlite3.Error, OSError):
            return {"status": "UNKNOWN", "idempotency_key": idempotency_key}
        if existing is not None:
            if existing["request_digest"] != identity["digest"]:
                raise LocalSkillSubmissionError("IDEMPOTENCY_CONFLICT")
            observed = await self._observe(existing)
            return observed["receipt"] if observed.get("status") == "FOUND" else observed
        request = await asyncio.to_thread(self._request_with_markdown, identity)
        result = await self._submit(request)
        return result["receipt"] if result.get("status") == "FOUND" else result

    async def get_submission(self, *, idempotency_key: str, profile_id: str | None = None) -> dict:
        """Return FOUND, NOT_FOUND, or UNKNOWN for this plugin and profile's stable key."""
        if profile_id is not None and str(profile_id) != self.profile_id:
            return {"status": "NOT_FOUND"}
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise LocalSkillSubmissionError("INVALID_IDEMPOTENCY_KEY")
        return await self._get_submission(idempotency_key)

    def _check_profile(self, profile_id):
        if profile_id is not None and str(profile_id) != self.profile_id:
            raise LocalSkillSubmissionError("PROFILE_MISMATCH")

    def _request_identity(self, skill_id, skill_version, values, key, output_policy):
        if output_policy not in ("local_only", "local_session"):
            raise LocalSkillSubmissionError("POLICY_UNSUPPORTED")
        if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
            raise LocalSkillSubmissionError("INVALID_IDEMPOTENCY_KEY")
        if not isinstance(skill_id, str) or not _SKILL_ID.fullmatch(skill_id):
            raise LocalSkillSubmissionError("INVALID_SKILL_ID")
        parts = skill_id.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise LocalSkillSubmissionError("INVALID_SKILL_ID")
        if not isinstance(values, Mapping):
            raise LocalSkillSubmissionError("INVALID_INPUT")
        try:
            input_json = json.dumps(dict(values), ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise LocalSkillSubmissionError("INVALID_INPUT") from exc
        if len(input_json) > _MAX_INPUT_CHARS:
            raise LocalSkillSubmissionError("INVALID_INPUT")
        if not isinstance(skill_version, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", skill_version):
            raise LocalSkillSubmissionError("INVALID_SKILL_VERSION")
        digest = skill_version.lower()
        canonical = json.dumps({"skill_id": skill_id, "skill_digest": digest,
                                "input": json.loads(input_json), "output_policy": output_policy},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {"key": key, "skill_id": skill_id, "skill_digest": digest,
                "input_json": input_json, "digest": hashlib.sha256(canonical.encode()).hexdigest(),
                "output_policy": output_policy}

    def _request_with_markdown(self, identity):
        skill_path = self._skill_path(identity["skill_id"])
        try:
            markdown_bytes = skill_path.read_bytes()
            markdown = markdown_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise LocalSkillSubmissionError("SKILL_UNAVAILABLE") from exc
        if len(markdown_bytes) > _MAX_SKILL_BYTES:
            raise LocalSkillSubmissionError("SKILL_TOO_LARGE")
        digest = hashlib.sha256(markdown_bytes).hexdigest()
        if not hmac.compare_digest(digest, identity["skill_digest"]):
            raise LocalSkillSubmissionError("SKILL_VERSION_MISMATCH")
        return {**identity, "markdown": markdown}

    def _skill_path(self, skill_id):
        skills_root = self.profile_home / "skills"
        if skills_root.is_symlink():
            raise LocalSkillSubmissionError("SKILL_UNAVAILABLE")
        root = skills_root.resolve()
        try:
            root.relative_to(self.profile_home)
        except ValueError as exc:
            raise LocalSkillSubmissionError("SKILL_UNAVAILABLE") from exc
        path = skills_root / skill_id / "SKILL.md"
        if any(component.is_symlink() for component in (path, *path.parents[:len(skill_id.split("/"))])):
            raise LocalSkillSubmissionError("SKILL_UNAVAILABLE")
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise LocalSkillSubmissionError("SKILL_UNAVAILABLE") from exc
        if not resolved.is_file():
            raise LocalSkillSubmissionError("SKILL_UNAVAILABLE")
        return resolved

    def _db_path(self):
        if not self.profile_home.is_dir():
            raise FileNotFoundError("profile home is missing")
        local_dir = self.profile_home / "local"
        local_dir.mkdir(mode=0o700, exist_ok=True)
        if local_dir.is_symlink():
            raise OSError("local submission store path escaped profile")
        try:
            local_dir.resolve().relative_to(self.profile_home)
        except ValueError as exc:
            raise OSError("local submission store path escaped profile") from exc
        state_dir = local_dir / "plugin-local-sessions"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        try:
            state_dir.resolve().relative_to(self.profile_home)
        except ValueError as exc:
            raise OSError("local submission store path escaped profile") from exc
        if state_dir.is_symlink():
            raise OSError("local submission store path escaped profile")
        if stat.S_IMODE(state_dir.stat().st_mode) & 0o077:
            os.chmod(state_dir, 0o700)
        return state_dir / "receipts.sqlite3"

    def _connect(self):
        path = self._db_path()
        marker = self.profile_home / ".plugin-local-sessions.namespace"
        if not path.exists() and marker.exists():
            raise sqlite3.DatabaseError("submission ledger is missing from an initialized namespace")
        if path.exists() and not marker.exists():
            raise sqlite3.DatabaseError("submission ledger has no namespace marker")
        if not marker.exists():
            descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write("local-skill-submissions-v1\n")
        if marker.is_symlink() or marker.read_text(encoding="ascii") != "local-skill-submissions-v1\n":
            raise sqlite3.DatabaseError("invalid submission namespace marker")
        if path.is_symlink():
            raise sqlite3.DatabaseError("submission ledger path is a symlink")
        if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise sqlite3.DatabaseError("submission ledger permissions are too broad")
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        db = sqlite3.connect(path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS plugin_local_submissions (
            plugin_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
            status TEXT NOT NULL, session_id TEXT, admission_id TEXT, result_json TEXT,
            updated_at REAL NOT NULL, PRIMARY KEY(idempotency_key))""")
        os.chmod(path, 0o600)
        return db

    def _reserve(self, request):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plugin_local_submissions WHERE idempotency_key=?",
                             (request["key"],)).fetchone()
            if row:
                if row["plugin_id"] != self.plugin_id:
                    db.rollback()
                    raise LocalSkillSubmissionError("IDEMPOTENCY_CONFLICT")
                if row["request_digest"] != request["digest"]:
                    db.rollback()
                    raise LocalSkillSubmissionError("IDEMPOTENCY_CONFLICT")
                db.commit()
                return dict(row), False
            db.execute("INSERT INTO plugin_local_submissions(plugin_id,idempotency_key,request_digest,status,updated_at) "
                       "VALUES(?,?,?,'admitting',?)",
                       (self.plugin_id, request["key"], request["digest"], time.time()))
            db.commit()
            return {"plugin_id": self.plugin_id, "idempotency_key": request["key"],
                    "request_digest": request["digest"], "status": "admitting"}, True
        except (sqlite3.Error, LocalSkillSubmissionError):
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    async def _submit(self, request):
        authority = self._active_authority()
        actor = self._actor(authority)
        from gateway.session_authorities import async_owner_scope
        async with async_owner_scope(authority):
            await asyncio.to_thread(self._require_injection_permission)
            if request["output_policy"] == "local_only":
                await asyncio.to_thread(self._require_policy)
            params = {"request_id": request["key"], "source": "tool", "cwd": str(self.profile_home)}
            if request["output_policy"] == "local_only":
                params.update(toolsets=[], safe_mode=True, ignore_user_config=True, local_only=True)
            params, trusted_policy, trusted_secrets = await asyncio.to_thread(self._launch_policy, params)
            try:
                row, fresh = await asyncio.to_thread(self._reserve, request)
            except LocalSkillSubmissionError:
                raise
            except (sqlite3.Error, OSError):
                return {"status": "UNKNOWN", "idempotency_key": request["key"]}
            if not fresh:
                return await self._observe(row)
            try:
                from gateway.session_local import create_local_session
                ref = await asyncio.to_thread(create_local_session, authority, actor, params,
                                              trusted_policy=trusted_policy,
                                              trusted_secrets=trusted_secrets,
                                              skills_prompt=request["markdown"])
                session_id = ref.session_id
                await asyncio.to_thread(self._save_progress, request["key"], request["digest"], session_id)
                admission = await authority.submit(actor, Submission(
                    request_id=request["key"], ref=SessionRef(authority.profile_id, session_id),
                    payload={"text": self._format_input(request)},
                    intent="queue"))
                result = await self._receipt(request["key"], request["digest"], session_id,
                                             admission.admission_id, admission.status, admission.outcome)
                if result["status"] == "UNKNOWN":
                    return {"status": "UNKNOWN", "idempotency_key": request["key"],
                            "session_id": session_id}
                result = await asyncio.to_thread(self._save_receipt, request["key"], request["digest"], result)
                return result
            except (RuntimeStoreError, sqlite3.Error, OSError, RuntimeError) as exc:
                # A write may have committed before an error. Keep the reservation quarantined.
                return {"status": "UNKNOWN", "idempotency_key": request["key"],
                        "error": getattr(exc, "reason", "admission_uncertain")}

    async def _get_submission(self, key):
        try:
            row = await asyncio.to_thread(self._load_row, key)
        except (sqlite3.Error, OSError):
            return {"status": "UNKNOWN", "idempotency_key": key}
        if row is None:
            try:
                authority = self._active_authority()
                actor = self._actor(authority)
                from gateway.session_authorities import async_owner_scope
                from persistence.authority.bindings import local_session_id
                async with async_owner_scope(authority):
                    session_id = local_session_id(authority.profile_id, actor.subject, key)
                    exists = await asyncio.to_thread(authority.persistence.sessions.get, session_id)
                if exists is not None:
                    return {"status": "UNKNOWN", "idempotency_key": key, "session_id": session_id}
            except (LocalSkillSubmissionError, RuntimeStoreError, sqlite3.Error, OSError):
                return {"status": "UNKNOWN", "idempotency_key": key}
            return {"status": "NOT_FOUND", "idempotency_key": key}
        observed = await self._observe(row)
        return ({"status": "FOUND", "receipt": observed["receipt"]}
                if observed.get("status") == "FOUND" else observed)

    async def _observe(self, row):
        if row.get("result_json"):
            try:
                return {"status": "FOUND", "receipt": json.loads(row["result_json"])}
            except (TypeError, ValueError):
                return {"status": "UNKNOWN", "idempotency_key": row["idempotency_key"]}
        key = row["idempotency_key"]
        session_id = row.get("session_id")
        try:
            authority = self._active_authority()
            actor = self._actor(authority)
            from gateway.session_authorities import async_owner_scope
            if not row.get("session_id"):
                from gateway.session_local_recovery import local_identity
                session_id = local_identity(authority.profile_id, actor.subject, key)
            else:
                session_id = row["session_id"]
            async with async_owner_scope(authority):
                if not await asyncio.to_thread(authority.persistence.sessions.get, session_id):
                    return {"status": "UNKNOWN", "idempotency_key": key}
                found = await asyncio.to_thread(authority.persistence.admissions.find,
                                                principal_id=actor.subject, session_id=session_id,
                                                request_id=key)
            if found is None:
                return {"status": "UNKNOWN", "idempotency_key": key, "session_id": session_id}
            if found.status == "unknown":
                return {"status": "UNKNOWN", "idempotency_key": key, "session_id": session_id}
            result = await self._receipt(key, row["request_digest"], session_id, found.admission_id,
                                         found.status, found.outcome)
            if result["status"] == "UNKNOWN":
                return {"status": "UNKNOWN", "idempotency_key": key, "session_id": session_id}
            result = await asyncio.to_thread(self._save_receipt, key, row["request_digest"], result)
            return {"status": "FOUND", "receipt": result}
        except (LocalSkillSubmissionError, RuntimeStoreError, sqlite3.Error, OSError, RuntimeError):
            return {"status": "UNKNOWN", "idempotency_key": key, "session_id": session_id}

    def _active_authority(self):
        from gateway.run import _gateway_runner_ref
        from gateway.session_authorities import authority_for_home, served_profile_name
        runner = _gateway_runner_ref()
        authority = authority_for_home(runner, self.profile_home) if runner is not None else None
        if (authority is None or Path(str(authority.profile_id)).resolve() != self.profile_home
                or served_profile_name(authority.profile_id) != self.profile_id):
            raise LocalSkillSubmissionError("PROFILE_UNAVAILABLE")
        return authority

    def _actor(self, authority):
        return SessionActor(subject=f"plugin:{self.plugin_id}", profile_id=authority.profile_id,
                            capabilities=frozenset({"session:create", "session:submit", "session:read"}),
                            transport_id=f"plugin:{self.plugin_id}")

    def _require_policy(self):
        from gateway.session_local import load_profile_config
        config = load_profile_config(self.profile_home)
        security = config.get("security") if isinstance(config, dict) else None
        output_policy = security.get("output_policy") if isinstance(security, dict) else None
        if not isinstance(output_policy, dict) or output_policy.get("slack") != "deny":
            raise LocalSkillSubmissionError("LOCAL_ONLY_POLICY_REQUIRED")

    def _require_injection_permission(self):
        from hermes_cli import plugins
        with plugins._plugin_home_scope(self.profile_home):
            config = plugins.load_config_readonly() or {}
        entry = plugins._plugin_settings_entry(config, self.plugin_id) or {}
        if entry.get("allow_gateway_injection") is not True:
            raise LocalSkillSubmissionError("GATEWAY_INJECTION_NOT_ALLOWED")

    def _launch_policy(self, params):
        from gateway.session_local import load_profile_config
        from gateway.run import _resolve_gateway_model
        from gateway.session_local_route import resolve_launch_route
        from gateway.session_policy import build_policy
        config = load_profile_config(self.profile_home)
        params = dict(params, model=_resolve_gateway_model(config))
        if not params["model"]:
            raise LocalSkillSubmissionError("MODEL_UNAVAILABLE")
        resolved = resolve_launch_route(params, config)
        trusted_secrets = {}
        policy = build_policy(resolved, config, private_secrets=trusted_secrets,
                              profile_terminal=False, workspace_posture=False)
        return resolved, policy, trusted_secrets

    @staticmethod
    def _format_input(request):
        instructions = "Apply the supplied skill instructions to this JSON input. Return the result as text. "
        if request["output_policy"] == "local_only":
            instructions += "Do not request tools or perform external actions."
        return instructions + "\n\n" + request["input_json"]

    def _load_row(self, key):
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM plugin_local_submissions WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None and row["plugin_id"] != self.plugin_id:
                return None
            return dict(row) if row else None
        finally:
            db.close()

    def _save_receipt(self, key, digest, receipt):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM plugin_local_submissions "
                             "WHERE idempotency_key=? AND request_digest=? AND plugin_id=?",
                             (key, digest, self.plugin_id)).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("submission receipt reservation is missing")
            if row["result_json"]:
                saved = json.loads(row["result_json"])
                db.commit()
                return saved
            if row["status"] in {"completed", "no_action", "failed", "cancelled"}:
                db.commit()
                return {"profile_id": self.profile_id, "idempotency_key": key,
                        "request_digest": digest, "submission_id": row["admission_id"],
                        "session_id": row["session_id"], "status": "UNKNOWN", "result": None}
            ranks = {"admitting": 0, "accepted": 1, "running": 2,
                     "completed": 3, "no_action": 3, "failed": 3, "cancelled": 3}
            existing_rank = ranks.get(row["status"], -1)
            incoming_rank = ranks.get(receipt["status"], -1)
            if incoming_rank < existing_rank:
                db.commit()
                return self._receipt_from_row(row)
            if (row["admission_id"] and receipt.get("submission_id")
                    and row["admission_id"] != receipt["submission_id"]):
                db.commit()
                return {"profile_id": self.profile_id, "idempotency_key": key,
                        "request_digest": digest, "submission_id": row["admission_id"],
                        "session_id": row["session_id"], "status": "UNKNOWN", "result": None}
            cursor = db.execute("UPDATE plugin_local_submissions SET status=?,session_id=?,admission_id=?,result_json=?,updated_at=? "
                                "WHERE idempotency_key=? AND request_digest=? AND plugin_id=?",
                                (receipt["status"], receipt.get("session_id"), receipt.get("submission_id"),
                                 json.dumps(receipt, sort_keys=True) if receipt["status"] in
                                 {"completed", "no_action", "failed", "cancelled"} else None,
                                 time.time(), key, digest, self.plugin_id))
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("submission receipt reservation is missing")
            db.commit()
            return receipt
        except sqlite3.Error:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _receipt_from_row(self, row):
        return {"profile_id": self.profile_id, "idempotency_key": row["idempotency_key"],
                "request_digest": row["request_digest"], "submission_id": row["admission_id"],
                "session_id": row["session_id"], "status": row["status"],
                "result": None, "error": None}

    def _save_progress(self, key, digest, session_id):
        db = self._connect()
        try:
            db.execute("UPDATE plugin_local_submissions SET session_id=?,updated_at=? "
                       "WHERE plugin_id=? AND idempotency_key=? AND request_digest=?",
                       (session_id, time.time(), self.plugin_id, key, digest))
        finally:
            db.close()

    async def _receipt(self, key, request_digest, session_id, admission_id, status, outcome):
        mapped = {"queued": "accepted", "started": "running"}.get(status, status)
        if mapped == "terminal":
            mapped = {"completed": "completed", "cancelled": "cancelled"}.get(outcome, "failed")
        result = (await asyncio.to_thread(self._admission_result, admission_id) if mapped in
                  {"completed", "no_action", "failed", "cancelled"} else None)
        if mapped == "completed" and result is None:
            mapped = "UNKNOWN"
        return {"profile_id": self.profile_id, "idempotency_key": key, "submission_id": admission_id,
                "request_digest": request_digest, "session_id": session_id, "status": mapped,
                "result": result,
                "error": outcome if mapped in {"failed", "cancelled"} else None}

    def _admission_result(self, admission_id):
        authority = self._active_authority()
        settled = authority.persistence.admissions.settled_result(admission_id=admission_id)
        if settled is None or not isinstance(settled.result, Mapping):
            return None
        stored = settled.result
        result = stored.get("result")
        final = result.get("final_response") if isinstance(result, dict) else None
        return {"text": final} if isinstance(final, str) else dict(stored)
