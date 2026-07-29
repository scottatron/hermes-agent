#!/usr/bin/env bash
# Rebase the home Hermes patch stack onto current upstream/main.
# This script deliberately stops before pushing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

EXPECTED_BRANCH="${CARRY_BRANCH:-carry/home}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
UPSTREAM_REF="$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"
CURRENT_BRANCH="$(git branch --show-current)"

if [[ "$CURRENT_BRANCH" != "$EXPECTED_BRANCH" ]]; then
  printf 'error: expected branch %s, found %s\n' "$EXPECTED_BRANCH" "$CURRENT_BRANCH" >&2
  exit 2
fi

if [[ -n "$(git status --porcelain)" ]]; then
  printf 'error: worktree is not clean; commit, stash, or discard changes first\n' >&2
  git status --short >&2
  exit 2
fi

if ! git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1; then
  printf 'error: remote %s is not configured\n' "$UPSTREAM_REMOTE" >&2
  exit 2
fi

git config rerere.enabled true
git config rerere.autoupdate true

git fetch --prune "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"

OLD_HEAD="$(git rev-parse HEAD)"
OLD_BASE="$(git merge-base "$UPSTREAM_REF" HEAD)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_BRANCH="backup/carry-home-$STAMP"
git branch "$BACKUP_BRANCH" "$OLD_HEAD"

printf '\nBackup: %s -> %s\n' "$BACKUP_BRANCH" "$OLD_HEAD"
printf '\nPatch-ID status before rebase (- means equivalent patch is upstream):\n'
git cherry -v "$UPSTREAM_REF" HEAD || true

printf '\nRebasing %s onto %s...\n' "$EXPECTED_BRANCH" "$UPSTREAM_REF"
git rebase "$UPSTREAM_REF"

NEW_HEAD="$(git rev-parse HEAD)"
NEW_BASE="$(git merge-base "$UPSTREAM_REF" HEAD)"

printf '\nRange diff:\n'
git range-diff "$OLD_BASE..$OLD_HEAD" "$NEW_BASE..$NEW_HEAD" || true

printf '\nPatch-ID status after rebase:\n'
git cherry -v "$UPSTREAM_REF" HEAD || true

git diff --check "$UPSTREAM_REF...HEAD"

printf '\nRebase complete. No push was performed.\n'
printf 'Verify the focused tests in CARRYING_PATCHES.md, then run:\n'
printf '  git push --force-with-lease origin %s\n' "$EXPECTED_BRANCH"
