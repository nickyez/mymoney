#!/usr/bin/env bash
set -euo pipefail

# Create a consistent archive of the named MyMoney Docker volume.
# Usage: ./scripts/backup-docker-volume.sh [output-directory]

volume="${MYMONEY_VOLUME:-mymoney-data}"
out_dir="${1:-./backups}"
mkdir -p "$out_dir"
chmod 700 "$out_dir"
archive="$out_dir/mymoney-${volume}-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
archive_path="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"
umask 077

docker volume inspect "$volume" >/dev/null
docker run --rm \
  -v "$volume:/data:ro" \
  -v "$(cd "$out_dir" && pwd):/backup" \
  alpine:3.20 \
  sh -eu -c 'test -f /data/transactions.db; tar -czf "/backup/'"$(basename "$archive_path")"'" -C /data .'
chmod 600 "$archive_path"
printf 'Backup created: %s\n' "$archive_path"
