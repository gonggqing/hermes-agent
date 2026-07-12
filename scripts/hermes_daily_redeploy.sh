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
dry_run=false
if [[ "${1:-}" == "--dry-run" ]]; then
  dry_run=true
fi

mkdir -p "$state_dir" "$log_dir"

timestamp() {
  date '+%Y-%m-%dT%H:%M:%S%z'
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

dashboard_healthy() {
  docker compose exec -T dashboard \
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
docker compose up --build -d gateway dashboard

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
