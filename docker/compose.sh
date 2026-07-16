#!/usr/bin/env bash
# Stable local operator entrypoint for the repository's Docker Compose stack.
# It intentionally never removes volumes: ~/.hermes and the Finance vector
# volume contain durable sessions, portfolios, briefs, research, and memory.

set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
COMPOSE=(docker compose --project-directory "${ROOT_DIR}" -f "${COMPOSE_FILE}")
DEFAULT_APP_SERVICES=(gateway finance)

export HERMES_UID="${HERMES_UID:-$(id -u)}"
export HERMES_GID="${HERMES_GID:-$(id -g)}"

usage() {
  cat <<'EOF'
Usage: ./docker/compose.sh <command> [service ...]

Lifecycle:
  up [service ...]       Start in background (all services by default)
  build [service ...]    Build gateway + finance by default
  rebuild [service ...]  Build and recreate app services (keeps data/volumes)
  dev [service ...]      Build and run app services in the foreground
  restart [service ...]  Restart gateway + finance by default, without rebuild
  stop [service ...]     Stop selected services, or the whole stack
  down                   Remove containers/network; never removes volumes

Debugging:
  status                 Show Compose service/container state
  health                 Probe dashboard, Finance API, and Qdrant
  logs [service ...]     Follow logs (gateway + finance by default)
  shell [service]        Open bash in a running service (gateway by default)
  exec <service> <cmd>   Run an arbitrary command in a running service
  config                 Validate Compose config without printing secrets

Examples:
  ./docker/compose.sh up
  ./docker/compose.sh rebuild gateway finance
  ./docker/compose.sh logs finance
  ./docker/compose.sh exec finance /opt/hermes/trader/.venv/bin/python -m swing_trader --help
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

compose() {
  "${COMPOSE[@]}" "$@"
}

require_cli() {
  command -v docker >/dev/null 2>&1 || die "Docker is not installed or not on PATH"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable"
  [[ -f "${HOME}/.hermes/.env" ]] || die "missing ${HOME}/.hermes/.env (runtime secrets)"
  [[ -f "${HOME}/.hermes/config.yaml" ]] || die "missing ${HOME}/.hermes/config.yaml (Hermes config)"
}

require_daemon() {
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
}

probe_http() {
  local service="$1"
  local url="$2"
  local label="$3"
  if compose exec -T "$service" curl --fail --silent --show-error --max-time 10 "$url" >/dev/null; then
    printf 'ok    %s (%s)\n' "$label" "$url"
  else
    printf 'fail  %s (%s)\n' "$label" "$url" >&2
    return 1
  fi
}

main() {
  local action="${1:-help}"
  if [[ "$action" == "help" || "$action" == "-h" || "$action" == "--help" ]]; then
    usage
    return 0
  fi
  shift
  require_cli

  # `docker compose config` is useful while Docker Desktop/Engine is down.
  # Every other action talks to images, containers, or the daemon directly.
  if [[ "$action" != "config" ]]; then
    require_daemon
  fi

  case "$action" in
    up|start)
      compose up -d --remove-orphans "$@"
      ;;
    build)
      if (( $# == 0 )); then
        set -- "${DEFAULT_APP_SERVICES[@]}"
      fi
      compose build "$@"
      ;;
    rebuild)
      if (( $# == 0 )); then
        set -- "${DEFAULT_APP_SERVICES[@]}"
      fi
      compose build "$@"
      compose up -d --force-recreate "$@"
      ;;
    dev)
      if (( $# == 0 )); then
        set -- "${DEFAULT_APP_SERVICES[@]}"
      fi
      compose up --build "$@"
      ;;
    restart)
      if (( $# == 0 )); then
        set -- "${DEFAULT_APP_SERVICES[@]}"
      fi
      compose restart "$@"
      ;;
    stop)
      compose stop "$@"
      ;;
    down)
      (( $# == 0 )) || die "down does not accept services; use stop <service>"
      compose down --remove-orphans
      ;;
    status|ps)
      compose ps
      ;;
    logs)
      if (( $# == 0 )); then
        set -- "${DEFAULT_APP_SERVICES[@]}"
      fi
      compose logs --tail=200 --follow "$@"
      ;;
    shell)
      local service="${1:-gateway}"
      (( $# <= 1 )) || die "shell accepts at most one service"
      compose exec "$service" /bin/bash
      ;;
    exec)
      (( $# >= 2 )) || die "usage: ./docker/compose.sh exec <service> <command...>"
      local service="$1"
      shift
      compose exec "$service" "$@"
      ;;
    health)
      local failed=0
      probe_http gateway http://127.0.0.1:9119/ "Hermes dashboard" || failed=1
      probe_http finance http://127.0.0.1:9319/v1/health "Finance API" || failed=1
      local vector_health
      vector_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' hermes-finance-vector 2>/dev/null || true)"
      if [[ "$vector_health" == "healthy" ]]; then
        printf 'ok    Finance vector (%s)\n' "$vector_health"
      else
        printf 'fail  Finance vector (%s)\n' "${vector_health:-not running}" >&2
        failed=1
      fi
      return "$failed"
      ;;
    config)
      # `env_file` contains credentials. Plain `docker compose config` expands
      # and prints them, so validation must be quiet by design.
      compose config --quiet
      printf 'ok    Compose configuration is valid (secret values not printed)\n'
      ;;
    *)
      usage >&2
      die "unknown command: $action"
      ;;
  esac
}

main "$@"
