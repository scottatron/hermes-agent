# Carrying Hermes patches

This fork keeps upstream history and home-specific changes separate:

- `main` mirrors `NousResearch/hermes-agent:main` without private patches.
- `carry/home` is the deployable branch: current upstream plus a short linear patch stack.
- `backup/carry-home-*` branches are created before rebases so a failed update is easy to undo.

Do not merge upstream into `carry/home`. Rebase the patch stack so each carried change remains independently reviewable and droppable.

## Current patch ledger

| Order | Commit subject | Source | Why carried | Drop condition |
|---:|---|---|---|---|
| 1 | `feat: add profile-aware outbound routing` | `ddf3efdcbd5154ca5dfc7ba47ee768baec0f7039` | Routes model/tool subprocess traffic through the Agent Vault profile selected for the active Hermes profile. | Upstream provides equivalent profile-aware outbound routing and the focused routing tests pass without this commit. |
| 2 | `feat: add profile-aware Slack proxy routing` | `03def476bcc7ccd3a27a65b7d79aae55fee5d107` | Extends the same routing boundary to gateway/Slack HTTP clients. | Upstream provides equivalent gateway proxy routing and the proxy/Slack tests pass without this commit. |
| 3–4 | Hindsight 0.9.x support and exact 0.9.0 convergence | `c57c37d4eded9225481d7cf1bfa2a5cab96553f5`, `5c647f8d9ebade7f3001e318833a08b62d5f0fee` | The home profiles use Hindsight 0.9.0; upstream still pins 0.6.1. These two commits are one logical patch and must be dropped together. The July 30 rebase adapted runtime re-pinning to upstream's environment-aware `lazy_deps.install_specs()` path so sealed hosted environments keep using their durable dependency target. | Upstream pins/supports the required Hindsight client and provider compatibility tests pass without both commits. |
| 5 | `fix(dashboard): secure loopback public URL proxy mode` | [NousResearch/hermes-agent#72127](https://github.com/NousResearch/hermes-agent/pull/72127), commit `92daadfb31f49d7bd1dce0540a0eb48508149a5f` | Allows the exact configured `dashboard.public_url` Host/Origin behind a loopback reverse proxy while engaging dashboard authentication. | The PR or an equivalent implementation lands upstream and the dashboard auth plus Host/Origin tests pass after this commit is removed. |
| 8 | `fix(frontend): build with aube instead of npm` | squashed from `8caad1fc67`, `472977b732`, `e014f71d7e`, `6839f92b7a`, `42ebf19b46`, `6bca291ec7` | Builds the frontend with aube rather than npm, and makes the desktop/TUI install and build paths package-manager-agnostic. Carried as six commits until 2026-08-16; none was independently droppable and two only corrected earlier ones, so every rebase resolved the same conflicts repeatedly. | Upstream builds the frontend with aube, or its npm assumptions become package-manager-agnostic on their own, and the TUI install plus desktop build tests pass without this commit. |

Update this table whenever a patch is added, materially changed during conflict resolution, superseded, or removed.

## Updating from upstream

From a clean `carry/home` worktree:

```bash
scripts/rebase-carry.sh
```

The helper:

1. fetches `upstream/main`;
2. creates a timestamped backup branch;
3. shows patch-equivalent commits with `git cherry`;
4. rebases the stack onto current upstream;
5. shows `git range-diff` so adaptations are reviewable;
6. runs whitespace validation;
7. does **not** push.

After tests pass:

```bash
git push --force-with-lease origin carry/home
```

Rewriting `carry/home` is intentional. Never use an unqualified `--force`.

## Deciding whether to drop a patch

Use evidence in this order:

1. `git cherry upstream/main carry/home` marks exact patch-ID equivalents with `-`.
2. During rebase, Git may report a skipped previously-applied commit or an empty commit.
3. Check the linked upstream PR/issue and inspect the current upstream implementation.
4. Remove the candidate commit in a temporary worktree and run its focused tests.
5. Update this ledger with the decision.

Patch-ID equivalence is strong evidence, not magic. Upstream may implement the same outcome differently, in which case the carried commit remains `+`; use behavior tests to decide.

## Conflict policy

- Preserve unrelated upstream changes; resolve only the carried behavior.
- Enable and inspect `git rerere` resolutions, but never accept them blindly.
- Keep each functional patch as a coherent commit.
- Use `git range-diff OLD_BASE..OLD_HEAD NEW_BASE..NEW_HEAD` after every rebase.
- Abort rather than guess when a conflict changes a security boundary, authentication behavior, provider protocol, or credential flow.

## Verification

Run focused tests for every patch touched by the rebase, then the canonical suite when practical:

```bash
scripts/run_tests.sh tests/agent/test_outbound_routing.py \
  tests/agent/test_anthropic_adapter.py \
  tests/gateway/test_proxy_mode.py \
  tests/gateway/test_slack.py

scripts/run_tests.sh tests/plugins/memory/test_hindsight_provider.py \
  tests/test_packaging_metadata.py

scripts/run_tests.sh tests/hermes_cli/test_dashboard_auth_gate.py \
  tests/hermes_cli/test_web_server_host_header.py
```

For a release candidate, run `scripts/run_tests.sh` and verify the real home deployment path separately. Unit tests do not prove that the reverse proxy, authentication provider, Agent Vault, or Hindsight service is reachable.

## Rebase log

### 2026-08-16 — squash the aube stack, rebase onto `56526bc0d3`

Six aube commits squashed into one (22 patches -> 17). Rebased 1434 commits of
upstream drift. Adaptations worth knowing about:

- **`apps/desktop/vite.config.ts` — carried change dropped.** Upstream now
  resolves react/react-dom with `createRequire(...).resolve()` instead of
  hardcoded `node_modules` paths. That is package-manager-agnostic by
  construction, which is exactly what the carried change hand-rolled, and it
  handles layouts our two-location fallback never would. Superseded; upstream's
  version taken whole.
- **`apps/desktop/scripts/assert-root-install.mjs` — merged, not replaced.**
  The carried `require.resolve("vite/package.json")` check was kept, and so was
  upstream's newer react/react-dom version-mismatch guard, which the carried
  patch predates and would otherwise have deleted.
- **`hermes_cli/main.py` — carried behavior kept, upstream's env adopted.**
  Package-manager-agnostic commands now run under upstream's
  `_npm_lifecycle_env()`, which also strips `ESBUILD_BINARY_PATH`.
- **`hermes_cli/update_cmd.py` — carried change followed a refactor.** Upstream
  extracted the desktop rebuild into `_rebuild_desktop_after_update()`; the
  npm assumption moved with it, so the carried
  `_resolve_node_runtime_package_manager()` change moved into that helper.
  Re-inlining the old block would have silently dropped the patch here.
- **`pyproject.toml` / `uv.lock`** — `exclude-newer-package` is now a union:
  upstream's new exemptions plus the carried `hindsight-client`.

`git range-diff` shows the other adapted patches (`profile-aware outbound
routing`, both hindsight commits) changed only in context — upstream renamed
`LoadedPlugin` to `PluginState` and bumped neighbouring pins. No carried
behavior moved, which matters most for the routing patch.

Not regenerated: `uv.lock` was merged by hand rather than re-resolved with
`uv lock`. The change is a list union, but a real resolve would be worth doing
before this is promoted.
