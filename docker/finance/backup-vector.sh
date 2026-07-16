#!/bin/sh
#
# backup-vector.sh — snapshot the hermes-finance-vector Qdrant data volume.
#
# Takes a stop-less snapshot: the named Docker volume is mounted READ-ONLY
# into a throwaway helper container and tarred to
# ./backups/finance-vector/<UTC timestamp>.tar.gz (relative to the caller's
# working directory; run from the repo root).
#
# The service does NOT need to be stopped, but for a guaranteed-consistent
# snapshot stop it first:  docker compose stop hermes-finance-vector
#
# Loop.md §5.10: back the volume up BEFORE every Qdrant image upgrade.
# Restore counterpart: docker/finance/restore-vector.sh
#
set -eu

VOLUME=""
CONTAINER="hermes-finance-vector"
HELPER_IMAGE="alpine:3.22"
BACKUP_DIR="./backups/finance-vector"

log() {
    printf '%s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

# --- Prerequisites ----------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker CLI not found in PATH"
docker info >/dev/null 2>&1 || die "docker daemon is not reachable"
VOLUME=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' "${CONTAINER}" 2>/dev/null || true)
[ -n "${VOLUME}" ] \
    || die "cannot resolve the Qdrant volume from container '${CONTAINER}' (start the service once)"
docker volume inspect "${VOLUME}" >/dev/null 2>&1 \
    || die "Docker volume '${VOLUME}' does not exist"

# --- Snapshot ----------------------------------------------------------------
log "Creating backup directory ${BACKUP_DIR}"
mkdir -p "${BACKUP_DIR}"
backup_dir_abs=$(CDPATH='' cd -- "${BACKUP_DIR}" && pwd)

stamp=$(date -u '+%Y%m%dT%H%M%SZ')
archive="${stamp}.tar.gz"

log "Backing up volume '${VOLUME}' (mounted read-only) to ${backup_dir_abs}/${archive}"
docker run --rm \
    -v "${VOLUME}:/qdrant-storage:ro" \
    -v "${backup_dir_abs}:/backup" \
    "${HELPER_IMAGE}" \
    tar czf "/backup/${archive}" -C /qdrant-storage .

log "Backup complete: ${backup_dir_abs}/${archive}"
