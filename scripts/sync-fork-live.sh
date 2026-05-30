#!/usr/bin/env bash
# Sync Szab's Hermes fork with NousResearch upstream, replay the live patch stack,
# push the rewritten live branch, then run Hermes' own update path against live.
#
# Branch model:
#   upstream/main = NousResearch/hermes-agent vendor truth
#   origin/main   = clean mirror of upstream/main
#   origin/live   = deployable/running custom Hermes branch

set -Eeuo pipefail

REPO_DIR="${REPO_DIR:-$HOME/.hermes/hermes-agent}"
UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
FORK_REMOTE="${FORK_REMOTE:-origin}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
FORK_MAIN_BRANCH="${FORK_MAIN_BRANCH:-main}"
LIVE_BRANCH="${LIVE_BRANCH:-live}"
RUN_TESTS="${RUN_TESTS:-0}"
DRY_RUN="${DRY_RUN:-0}"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }
run() {
  printf '+ %q' "$1"
  shift || true
  printf ' %q' "$@"
  printf '\n'
  if [[ "$DRY_RUN" != "1" ]]; then
    "$@"
  fi
}

usage() {
  cat <<'USAGE'
Usage: scripts/sync-fork-live.sh [--dry-run] [--run-tests]

Does, in order:
  1. require a clean checkout;
  2. fetch upstream and fork remotes;
  3. reset local main to upstream/main;
  4. force-with-lease push fork main so origin/main mirrors upstream/main;
  5. checkout live and fast-forward it from origin/live;
  6. rebase live on origin/main;
  7. force-with-lease push origin/live;
  8. run Hermes update against --branch live.

Environment overrides:
  REPO_DIR, UPSTREAM_REMOTE, FORK_REMOTE, UPSTREAM_BRANCH, FORK_MAIN_BRANCH,
  LIVE_BRANCH, RUN_TESTS=1, DRY_RUN=1
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --run-tests) RUN_TESTS=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
  shift
done

on_error() {
  local exit_code=$?
  warn "sync failed at line ${BASH_LINENO[0]} with exit code ${exit_code}."
  if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    if [[ -d "$(git -C "$REPO_DIR" rev-parse --git-path rebase-merge 2>/dev/null)" || -d "$(git -C "$REPO_DIR" rev-parse --git-path rebase-apply 2>/dev/null)" ]]; then
      warn "A rebase appears to be in progress. Resolve conflicts then run 'git rebase --continue', or abort with 'git rebase --abort'."
    fi
  fi
  exit "$exit_code"
}
trap on_error ERR

cd "$REPO_DIR"

git rev-parse --is-inside-work-tree >/dev/null || die "Not a git repository: $REPO_DIR"

log "Preflight"
[[ -z "$(git status --porcelain=v1)" ]] || die "Working tree is dirty. Commit/stash changes before syncing."
git remote get-url "$UPSTREAM_REMOTE" >/dev/null || die "Missing remote: $UPSTREAM_REMOTE"
git remote get-url "$FORK_REMOTE" >/dev/null || die "Missing remote: $FORK_REMOTE"

current_branch="$(git branch --show-current)"
log "Current branch: ${current_branch:-detached}"
log "Fetching remotes"
run git-fetch-upstream git fetch "$UPSTREAM_REMOTE" --prune
run git-fetch-fork git fetch "$FORK_REMOTE" --prune

upstream_ref="refs/remotes/${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}"
fork_main_ref="refs/remotes/${FORK_REMOTE}/${FORK_MAIN_BRANCH}"
fork_live_ref="refs/remotes/${FORK_REMOTE}/${LIVE_BRANCH}"

git rev-parse --verify "$upstream_ref" >/dev/null || die "Missing upstream ref: $upstream_ref"
git rev-parse --verify "$fork_live_ref" >/dev/null || die "Missing live ref: $fork_live_ref"

old_fork_main="$(git rev-parse --verify "$fork_main_ref" 2>/dev/null || true)"
old_fork_live="$(git rev-parse --verify "$fork_live_ref")"

log "Mirror ${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH} -> ${FORK_REMOTE}/${FORK_MAIN_BRANCH}"
run checkout-main git checkout "$FORK_MAIN_BRANCH"
run reset-main git reset --hard "$upstream_ref"
if [[ -n "$old_fork_main" ]]; then
  run push-main git push --force-with-lease="refs/heads/${FORK_MAIN_BRANCH}:${old_fork_main}" "$FORK_REMOTE" "HEAD:refs/heads/${FORK_MAIN_BRANCH}"
else
  run push-main git push "$FORK_REMOTE" "HEAD:refs/heads/${FORK_MAIN_BRANCH}"
fi

log "Refresh fork refs after main push"
run fetch-fork-after-main git fetch "$FORK_REMOTE" --prune
new_fork_main="$(git rev-parse --verify "$fork_main_ref")"

log "Rebase ${LIVE_BRANCH} on ${FORK_REMOTE}/${FORK_MAIN_BRANCH}"
run checkout-live git checkout "$LIVE_BRANCH"
run ff-live git pull --ff-only "$FORK_REMOTE" "$LIVE_BRANCH"
run rebase-live git rebase "$new_fork_main"

if [[ "$DRY_RUN" != "1" ]]; then
  git merge-base --is-ancestor "$new_fork_main" HEAD || die "Postcondition failed: live does not contain origin/main"
fi

if [[ "$RUN_TESTS" == "1" ]]; then
  log "Running test suite before pushing live"
  if [[ -x ./venv/bin/python ]]; then
    run pytest ./venv/bin/python -m pytest tests/ -o addopts= -q
  else
    run pytest python -m pytest tests/ -o addopts= -q
  fi
else
  warn "Skipping tests. Re-run with --run-tests or RUN_TESTS=1 for full verification."
fi

log "Push rebased live -> ${FORK_REMOTE}/${LIVE_BRANCH}"
run push-live git push --force-with-lease="refs/heads/${LIVE_BRANCH}:${old_fork_live}" "$FORK_REMOTE" "HEAD:refs/heads/${LIVE_BRANCH}"

log "Run Hermes update against ${LIVE_BRANCH}"
if [[ -x ./venv/bin/python ]]; then
  run hermes-update ./venv/bin/python -m hermes_cli.main update --branch "$LIVE_BRANCH"
else
  run hermes-update hermes update --branch "$LIVE_BRANCH"
fi

log "Final status"
git status --short --branch
git log --oneline --decorate -5

log "Done. ${FORK_REMOTE}/${FORK_MAIN_BRANCH} mirrors ${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}; ${FORK_REMOTE}/${LIVE_BRANCH} is rebased and deployed through hermes update."
