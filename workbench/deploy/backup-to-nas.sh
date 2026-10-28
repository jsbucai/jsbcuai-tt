#!/usr/bin/env bash
set -euo pipefail
umask 077
cd /opt/ul-workbench
: "${UL_NAS_MOUNT:?Set the verified NAS mount point}"
: "${UL_BACKUP_DIR:?Set the backup folder inside that mount}"
mountpoint -q "$UL_NAS_MOUNT" || { echo 'NAS is not mounted; no backup copied.' >&2; exit 1; }
nas_root="$(realpath "$UL_NAS_MOUNT")"
backup_root="$(realpath -m "$UL_BACKUP_DIR")"
case "$backup_root/" in "$nas_root/"*) ;; *) echo 'Backup folder must be on NAS.' >&2; exit 1 ;; esac
mkdir -p "$backup_root"
archive="ul-$(date -u +%Y%m%dT%H%M%SZ)-$$.ulbackup"
local_archive="${UL_DATA_DIR:-/opt/ul-workbench/service/runtime/data}/backups/$archive"
temporary="$backup_root/.$archive.tmp"
trap 'rm -f -- "$temporary"' EXIT
docker compose exec -T workbench mkdir -p /app/service/runtime/data/backups
docker compose exec -T workbench python backup.py backup \
  --runtime /app/service/runtime/data \
  --file "/app/service/runtime/data/backups/$archive" \
  --key-file /app/service/runtime/data/secrets/backup.key
cp -- "$local_archive" "$temporary"
cmp -- "$local_archive" "$temporary"
mv -- "$temporary" "$backup_root/$archive"
rm -- "$local_archive"
echo 'Encrypted consistent backup copied and verified.'
