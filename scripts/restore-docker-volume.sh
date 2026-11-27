#!/usr/bin/env bash
set -euo pipefail

# Restore a verified MyMoney archive into the named Docker volume.
# Usage: ./scripts/restore-docker-volume.sh BACKUP.tar.gz [--force]
# Stop all MyMoney services first. Without --force, existing volume data is kept.

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 BACKUP.tar.gz [--force]" >&2
  exit 2
fi
archive="$1"
force="${2:-}"
volume="${MYMONEY_VOLUME:-mymoney-data}"
[[ -f "$archive" ]] || { echo "Backup archive not found: $archive" >&2; exit 2; }
if [[ "$force" != "" && "$force" != "--force" ]]; then
  echo "Unknown option: $force" >&2
  exit 2
fi

docker volume inspect "$volume" >/dev/null
if [[ "$force" != "--force" ]]; then
  existing="$(docker run --rm -v "$volume:/data:ro" alpine:3.20 sh -c 'find /data -mindepth 1 -maxdepth 1 -print -quit')"
  if [[ -n "$existing" ]]; then
    echo "Volume '$volume' is not empty. Stop MyMoney and rerun with --force to replace its contents." >&2
    exit 1
  fi
fi

# Validate the complete archive in an isolated container before modifying the volume.
docker run --rm \
  -v "$(cd "$(dirname "$archive")" && pwd):/backup:ro" \
  python:3.12-slim python -c '
import json, pathlib, sqlite3, tarfile, sys
archive = pathlib.Path("/backup") / sys.argv[1]
allowed = {"transactions.db", "config.json"}
with tarfile.open(archive, "r:gz") as tar:
    members = {}
    for member in tar.getmembers():
        name = member.name.lstrip("./")
        path = pathlib.PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"Unsafe archive member path: {member.name}")
        if member.isdir() and name in (".", "./"):
            continue
        if not member.isfile() or name not in allowed:
            raise SystemExit(f"Unexpected archive member: {member.name}")
        members[name] = member
    if "transactions.db" not in members:
        raise SystemExit("Backup is missing transactions.db")
    for name, member in members.items():
        source = tar.extractfile(member)
        if source is None:
            raise SystemExit(f"Could not read archive member: {name}")
        with source:
            destination = pathlib.Path("/tmp/restore") / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read())
config = pathlib.Path("/tmp/restore/config.json")
if config.exists():
    value = json.loads(config.read_text())
    if not isinstance(value, dict):
        raise SystemExit("config.json must contain a JSON object")
db = sqlite3.connect("/tmp/restore/transactions.db")
if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
    raise SystemExit("SQLite integrity check failed")
' \
  "$(basename "$archive")"

# Only after validation, replace volume contents. Keep ownership compatible with the non-root container user.
docker run --rm \
  -v "$volume:/data" \
  -v "$(cd "$(dirname "$archive")" && pwd):/backup:ro" \
  alpine:3.20 sh -eu -c '
    find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    tar -xzf "/backup/'"$(basename "$archive")"'" -C /data
    chown -R 1000:1000 /data
    chmod 600 /data/transactions.db
    [ ! -f /data/config.json ] || chmod 600 /data/config.json
  '
printf "Restored %s into volume '%s'. Start MyMoney and verify /api/health and transactions.\n" "$archive" "$volume"
