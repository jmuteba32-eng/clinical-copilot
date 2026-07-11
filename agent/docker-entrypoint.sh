#!/bin/sh
# Fix /data ownership after Railway mounts a persistent volume at runtime.
# Build-time chown in the Dockerfile does not survive the mount — the volume
# replaces the directory and is typically root-owned until we chown here.
set -e
if [ -d /data ]; then
  chown -R appuser:appuser /data 2>/dev/null || true
fi
if [ "$(id -u)" = "0" ]; then
  exec runuser -u appuser -- "$@"
fi
exec "$@"
