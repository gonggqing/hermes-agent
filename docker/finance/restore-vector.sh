#!/bin/sh
#
# restore-vector.sh — restore a backup archive into the hermes-finance-vector
# Qdrant data volume.
#
# Usage:
#   sh docker/finance/restore-vector.sh [--force] <archive.tar.gz>
#
# The archive is one produced by docker/finance/backup-vector.sh. The script
# REFUSES to run unless the target volume is empty or --force is given, and
# refuses while the hermes-finance-vector container is running (restoring
# under a live Qdrant corrupts storage).
#
# NOTE: --force untars OVER existing volume contents without wiping them
# first, which can leave mixed state. For a clean restore, remove and
# recreate the volume instead (see the drill in docs/finance-vector.md).
#
set -eu

VOLUME="hermes-finance-vector-data"
HELPER_IMAGE="alpine:3.22"
CONTAINER="hermes-finance-vector"

log() {
    printf '%s\n' "$*"
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    printf 'Usage: %s [--force] <archive.tar.gz>\n' "$0"
}

# --- Arguments ---------------------------------------------------------------
FORCE=0
ARCHIVE=""
for arg in "$@"; do
    case "${arg}" in
        --force)
            FORCE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            usage >&2
            die "unknown option: ${arg}"
            ;;
        *)
            [ -z "${ARCHIVE}" ] || die "exactly one archive path expected"
            ARCHIVE="${arg}"
            ;;
    esac
done
if [ -z "${ARCHIVE}" ]; then
    usage >&2
    die "missing archive path"
fi

# --- Prerequisites ----------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker CLI not found in PATH"
docker info >/dev/null 2>&1 || die "docker daemon is not reachable"
[ -f "${ARCHIVE}" ] || die "archive not found: ${ARCHIVE}"
docker volume inspect "${VOLUME}" >/dev/null 2>&1 \
    || die "Docker volume '${VOLUME}' does not exist (create it first: docker volume create ${VOLUME})"

running=$(docker ps -q --filter "name=^${CONTAINER}\$")
[ -z "${running}" ] \
    || die "container '${CONTAINER}' is running; stop it first: docker compose stop hermes-finance-vector"

# --- Refuse to clobber a non-empty volume without --force ---------------------
contents=$(docker run --rm -v "${VOLUME}:/qdrant-storage:ro" "${HELPER_IMAGE}" \
    sh -c 'ls -A /qdrant-storage')
if [ -n "${contents}" ] && [ "${FORCE}" -ne 1 ]; then
    die "volume '${VOLUME}' is not empty; re-run with --force to untar over it (or remove and recreate the volume for a clean restore)"
fi

# --- Restore ------------------------------------------------------------------
archive_dir=$(CDPATH='' cd -- "$(dirname -- "${ARCHIVE}")" && pwd)
archive_name=$(basename -- "${ARCHIVE}")

log "Restoring ${archive_dir}/${archive_name} into volume '${VOLUME}'"
if [ -n "${contents}" ]; then
    log "WARNING: --force given; untarring over existing volume contents"
fi
docker run --rm \
    -v "${VOLUME}:/qdrant-storage" \
    -v "${archive_dir}:/backup:ro" \
    "${HELPER_IMAGE}" \
    tar xzf "/backup/${archive_name}" -C /qdrant-storage

log "Restore complete. Start the service with: docker compose up -d hermes-finance-vector"
log "Then verify the collection count as described in docs/finance-vector.md."
