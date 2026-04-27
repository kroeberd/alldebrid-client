#!/bin/sh
# AllDebrid-Client container entrypoint.
#
# Supports PUID / PGID environment variables so that downloaded files are owned
# by the same user as the host process (e.g. Sonarr, Radarr, Plex).
#
# Usage:
#   environment:
#     - PUID=1000
#     - PGID=1000
#
# When PUID/PGID are omitted the app runs as the 'appuser' created in the
# Dockerfile (UID 1000 / GID 1000) — still non-root.
# To run as root deliberately set PUID=0.

set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# ── Create / adjust group ─────────────────────────────────────────────────────
if [ "${PGID}" != "0" ]; then
    # Check if a group with this GID already exists
    EXISTING_GROUP=$(getent group "${PGID}" | cut -d: -f1 || true)
    if [ -z "${EXISTING_GROUP}" ]; then
        groupadd -g "${PGID}" appgroup 2>/dev/null || true
    fi
fi

# ── Create / adjust user ──────────────────────────────────────────────────────
if [ "${PUID}" != "0" ]; then
    EXISTING_USER=$(getent passwd "${PUID}" | cut -d: -f1 || true)
    if [ -z "${EXISTING_USER}" ]; then
        # Create the user with the requested UID, belonging to the requested GID
        useradd -u "${PUID}" -g "${PGID}" -M -s /bin/sh appuser 2>/dev/null || true
        RUN_USER="appuser"
    else
        RUN_USER="${EXISTING_USER}"
        # Make sure the existing user is in the right group
        usermod -g "${PGID}" "${RUN_USER}" 2>/dev/null || true
    fi
else
    RUN_USER="root"
fi

echo "[entrypoint] PUID=${PUID} PGID=${PGID} → running as ${RUN_USER}"

# ── Apply umask ──────────────────────────────────────────────────────────────
UMASK="${UMASK:-002}"
umask "${UMASK}" 2>/dev/null || true

# ── Fix ownership of app directories ─────────────────────────────────────────
# /app/data    — SQLite DB, backups, aria2 session/log, watch/processed folders
# /app/config  — config.json
# /download    — the mounted download target (most important for other containers)
for DIR in /app/data /app/config /download; do
    if [ -d "${DIR}" ]; then
        chown -R "${PUID}:${PGID}" "${DIR}" 2>/dev/null || true
    fi
done

# ── Hand off to the app ───────────────────────────────────────────────────────
if [ "${PUID}" = "0" ]; then
    # Explicit root — run directly
    exec "$@"
else
    exec gosu "${RUN_USER}" "$@"
fi
