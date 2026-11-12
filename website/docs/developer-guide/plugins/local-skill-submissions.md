# Durable local skill submissions

`ctx.local_sessions` admits a profile-owned Markdown skill to the gateway's local
session FIFO and exposes a durable receipt. It is asynchronous and available to
plugins in-process and through the plugin-host facade. It does not install or
activate a messaging route.

## Restricted execution

This first implementation supports Markdown-only skills. The host freezes the
skill text, disables every model tool, bypasses user configuration and plugin
callbacks for execution, and retains the restriction in managed-worker bootstrap
data. Shell commands, MCP tools, skill scripts, and Python handlers are not
supported by this execution mode. It is not an OS sandbox for arbitrary code.

The owning profile must configure:

```yaml
security:
  output_policy:
    slack: deny
plugins:
  entries:
    your-plugin-id:
      allow_gateway_injection: true
```

The Slack adapter enforces that host output restriction independently of plugin
hooks. It covers SDK writes and the adapter's raw HTTP/response URL paths. It does
not allow ordinary Slack fallback chat: a consuming intake hook gets its chance,
then protected input is dropped before commands, agent execution, or busy-session
queueing when it was not consumed. This remains true if the intake hook is absent,
raises, or returns a malformed result. The restriction does
not constrain independent networking by trusted in-process plugin code. The host
persists a deny marker so an omitted configuration key cannot reopen output on
restart. Unknown shared-transport attribution is denied when a protected profile
is served; other profiles must have an explicit owning context for output.

## Submit and query

Resolve and pin a skill version before saving a job. `skill_version` is the SHA256
hex digest of the UTF-8 `SKILL.md` content in the profile's `skills/<skill_id>/`
directory. It is not a semantic version. A changed skill requires a new job's
selected version; replay of accepted work retains the original request.

```python
receipt = await ctx.local_sessions.submit_skill(
    skill_id="support-triage",
    skill_version=ctx.get_config("triage_sha256"),
    input={"text": "Saved support event"},
    idempotency_key="switchboard:r2:saved-job-id",
    output_policy="local_only",
)
lookup = await ctx.local_sessions.get_submission(
    idempotency_key="switchboard:r2:saved-job-id",
)
```

Ownership comes from the plugin context's captured profile home. An optional
`profile_id` must match it. Input is a JSON object; the host does not accept a
platform destination or caller-supplied Python executable.

New admission requires the existing per-plugin `allow_gateway_injection` opt-in.
Receipt queries remain available after that opt-in is removed, so disabling new
work does not prevent recovery of already admitted work.

Submission returns a receipt with a stable submission/session identity, or
`status: UNKNOWN` when admission cannot be established. Receipt acceptance is not
completion. Query returns `FOUND` with a receipt, authoritative `NOT_FOUND`, or
`UNKNOWN`. Query an uncertain request using its original key; never invent a key
or blindly resubmit. Reusing a key with changed input or skill version is a
conflict. Terminal receipts are not rerunnable attempts.

The profile's receipt ledger and canonical session database are durable state.
Keep both in backups. Loss or corruption of previously initialized receipt
history is uncertainty, not proof that execution never happened. Arbitrary
rollback of both databases is not an exactly-once guarantee; restore procedures
must fence old workers and establish history completeness before new admission.
