#!/usr/bin/env bash
set -euo pipefail
umask 077
display_dir=/var/lib/ul-purchase-collector/display
mkdir -p "$display_dir"
chmod 700 "$display_dir"
auth_file="$display_dir/.Xauthority"
touch "$auth_file"
chmod 600 "$auth_file"
cookie="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
xauth -f "$auth_file" add :97 . "$cookie"
unset cookie
exec /usr/bin/Xvfb :97 -screen 0 1920x1080x24 -nolisten tcp -auth "$auth_file"
