#!/usr/bin/env bash
# Daily Hermes deployment watchdog.
#
# Safe scope: compare only the checked-in Git revision and verify the running
# dashboard from *inside* its container. Rebuild only when that revision differs
# from the last successful deployment or health fails. Uncommitted/untracked
# work (including trader/) is reported but never silently built or deployed.

set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
state_dir="${HOME}/.hermes/automation"
state_file="${state_dir}/hermes-deployed-commit"
log_dir="${HOME}/.hermes/logs"
lock_dir="${state_dir}/hermes-daily-redeploy.lock"
review_workspace="${HOME}/.hermes/workspace/hermes-agent"
review_context="${review_workspace}/.hermes-review-context.md"
dry_run=false
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=true
fi

mkdir -p "$state_dir" "$log_dir"

timestamp() {
  date '+%Y-%m-%dT%H:%M:%S%z'
}

sync_review_workspace() {
  mkdir -p "$review_workspace"
  rsync -a --delete \
    --exclude='.git/' \
    --exclude='.env' \
    --exclude='*/.env' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='node_modules/' \
    --exclude='__pycache__/' \
    --exclude='.pytest_cache/' \
    --exclude='*.egg-info/' \
    --exclude='.coverage' \
    "$repo_dir/" "$review_workspace/"

  {
    echo "# Hermes review workspace context"
    echo
    echo "Generated: $(timestamp)"
    echo "Committed HEAD: $(git -C "$repo_dir" rev-parse HEAD)"
    echo
    echo "## Latest commit"
    git -C "$repo_dir" log -1 --format='%h %s (%ad)' --date=iso
    echo
    echo "## Working tree status (names only)"
    git -C "$repo_dir" status --short
  } > "$review_context"
}

if ! mkdir "$lock_dir" 2>/dev/null; then
  echo "[$(timestamp)] skipped: another deployment check is running"
  exit 0
fi
trap 'rmdir "$lock_dir"' EXIT

cd "$repo_dir"
current_commit="$(git rev-parse HEAD)"
previous_commit="$(cat "$state_file" 2>/dev/null || true)"
dirty_count="$(git status --porcelain | wc -l | tr -d ' ')"
sync_review_workspace

dashboard_healthy() {
  # The dashboard is served by the single consolidated `gateway` container
  # (HERMES_DASHBOARD=1, network_mode:host). There is no separate `dashboard`
  # service anymore.
  docker compose exec -T gateway \
    curl --fail --silent --show-error --max-time 10 http://127.0.0.1:9119/ >/dev/null
}

if dashboard_healthy; then
  health="healthy"
else
  health="unhealthy"
fi

if [[ "$current_commit" == "$previous_commit" && "$health" == "healthy" ]]; then
  echo "[$(timestamp)] no deploy: commit ${current_commit:0:12} unchanged; dashboard healthy; uncommitted paths=$dirty_count"
  exit 0
fi

reason=()
[[ "$current_commit" != "$previous_commit" ]] && reason+=("commit changed")
[[ "$health" != "healthy" ]] && reason+=("dashboard $health")
reason_text="$(IFS=', '; echo "${reason[*]}")"

if [[ "$dry_run" == true ]]; then
  echo "[$(timestamp)] would deploy: $reason_text; target=${current_commit:0:12}; uncommitted paths=$dirty_count"
  exit 0
fi

echo "[$(timestamp)] deploy: $reason_text; target=${current_commit:0:12}; uncommitted paths=$dirty_count"
docker compose up --build -d gateway

for _ in $(seq 1 24); do
  if dashboard_healthy; then
    printf '%s\n' "$current_commit" > "$state_file"
    echo "[$(timestamp)] deploy complete: dashboard healthy; deployed=${current_commit:0:12}"
    exit 0
  fi
  sleep 5
done

echo "[$(timestamp)] ERROR: deployment started but dashboard health did not recover" >&2
exit 1
