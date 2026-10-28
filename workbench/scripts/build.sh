#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
if [ -f runtime/toolchain/local-env.sh ]; then
  source runtime/toolchain/local-env.sh
fi
: "${JAVA_HOME:?Set JAVA_HOME to JDK 17}"
: "${ANDROID_HOME:?Set ANDROID_HOME to Android SDK with platform 35}"
test -x "$JAVA_HOME/bin/java"
test -f "$ANDROID_HOME/platforms/android-35/android.jar"
if [ ! -f runtime/signing/debug.keystore ]; then
  echo 'Missing original NAS signing key: place it in runtime/signing/debug.keystore (mode 600).' >&2
  exit 1
fi
python3 scripts/bootstrap_gradle.py
chmod u+x runtime/toolchain/gradle-8.9/bin/gradle
if [ "${UL_MAVEN_BRIDGE:-0}" = 1 ]; then
  bridge_ready() {
    python3 -c 'import urllib.request; assert urllib.request.urlopen("http://127.0.0.1:5192/health", timeout=2).read() == b"ul-build-maven-cache"' 2>/dev/null
  }
  if ! bridge_ready; then
    python3 scripts/maven_bridge.py > runtime/toolchain/maven-bridge.log 2>&1 &
    bridge_pid=$!
    trap 'kill "$bridge_pid" 2>/dev/null || true' EXIT
    for attempt in {1..20}; do
      if ! kill -0 "$bridge_pid" 2>/dev/null; then
        cat runtime/toolchain/maven-bridge.log >&2
        exit 1
      fi
      if bridge_ready; then break; fi
      sleep 0.25
    done
    bridge_ready || { echo 'Build dependency cache failed to start' >&2; exit 1; }
  fi
fi
if [ "$#" -eq 0 ]; then
  set -- testDebugUnitTest lintDebug assembleDebug
fi
runtime/toolchain/gradle-8.9/bin/gradle --no-daemon "$@"
