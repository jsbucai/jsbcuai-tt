#!/usr/bin/env bash
set -euo pipefail
umask 077
# Only loopback listeners. The operator reaches this session through an SSH
# tunnel; neither VNC nor the web viewer is exposed to the LAN or internet.
/usr/bin/x11vnc -display :97 -auth /var/lib/ul-purchase-collector/display/.Xauthority \
  -localhost -rfbport 5901 -forever -shared -nopw \
  -o /var/lib/ul-purchase-collector/display/manual-view.log &
viewer_pid=$!
trap 'kill "$viewer_pid" 2>/dev/null || true' EXIT
/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:6081 127.0.0.1:5901
