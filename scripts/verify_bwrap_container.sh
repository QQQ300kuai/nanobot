#!/usr/bin/env bash
# Run the real-bwrap isolation tests inside a Docker container with the privileges
# docker-compose.multiuser.yml grants (SYS_ADMIN, apparmor/seccomp/systempaths unconfined),
# plus no-new-privileges. The gateway really runs as uid 1000 (entrypoint.sh drops
# privileges via setpriv), so the main run is uid 1000 and needs no NET_ADMIN; a root run
# (network test skipped, as root has no user namespace) is kept as a secondary check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${BWRAP_VERIFY_IMAGE:-debian:bookworm-slim}"

docker run --rm \
  --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  --security-opt no-new-privileges \
  -v "$ROOT":/src:ro \
  "$IMAGE" bash -euc '
    apt-get update -qq >/dev/null
    apt-get install -y -qq --no-install-recommends bubblewrap python3 python3-pytest python3-pydantic python3-certifi util-linux >/dev/null
    mkdir /work && tar -C /src --exclude=.venv --exclude=.git -cf - . | tar -C /work -xf -
    cd /work
    T="python3 -m pytest --noconftest tests/security/test_bwrap_container.py -v -p no:cacheprovider"
    echo "=== uid 1000 (deployed mode)"
    PYTHONPATH=/work setpriv --reuid=1000 --regid=1000 --clear-groups $T
    echo "=== root (network test skipped)"
    PYTHONPATH=/work $T -k "not net_none"
  '
