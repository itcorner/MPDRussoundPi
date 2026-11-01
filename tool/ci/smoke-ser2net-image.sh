#!/usr/bin/env bash
set -euo pipefail
if [[ $# -ne 2 || ( "$2" != amd64 && "$2" != arm64 ) ]]; then
    echo "Usage: bash tool/ci/smoke-ser2net-image.sh IMAGE amd64|arm64" >&2
    exit 2
fi
container=
network=
cleanup() {
    result=$?
    trap - EXIT
    if [[ -n "$container" ]]; then
        if [[ "$result" -ne 0 ]]; then
            docker logs --tail 100 "$container" >&2 || echo "Unable to read logs" >&2
        fi
        if [[ -n "$network" ]] && ! docker network disconnect --force "$network" "$container"; then
            echo "Unable to detach smoke network" >&2
            result=1
        fi
        if ! docker rm --force --volumes "$container" >/dev/null; then
            echo "Unable to clean up ser2net smoke container" >&2
            result=1
        fi
        if [[ -n "$network" ]]; then
            removed=false
            for ((attempt = 0; attempt < 5; attempt++)); do
                if docker network rm "$network" >/dev/null; then removed=true; break; fi
                sleep 1
            done
            if [[ "$removed" != true ]]; then
                echo "Unable to clean up isolated smoke network" >&2
                result=1
            fi
        fi
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Override only the connector: test TCP bridging without a physical serial device.
network=$(docker network create --internal "ser2net-smoke-$$-$RANDOM")
container=$(docker create --platform "linux/$2" --network "$network" \
    -e RUSSOUND_SER2NET_CONNECTOR=stdio "$1")
docker start "$container" >/dev/null
for ((attempt = 0; attempt < 60; attempt++)); do
    health=$(docker inspect --format '{{.State.Health.Status}}' "$container")
    case "$health" in
        healthy) break ;;
        starting) sleep 2 ;;
        *) echo "ser2net health failure: $health" >&2; exit 1 ;;
    esac
done
[[ "$health" = healthy ]] || { echo "ser2net startup timed out" >&2; exit 1; }
machine=x86_64
[[ "$2" != arm64 ]] || machine=aarch64
[[ "$(docker exec "$container" uname -m)" = "$machine" ]]
[[ "$(docker exec "$container" id -u)" = 10001 ]]
docker exec "$container" sh -c 'test -w /tmp && grep -q "connector: \"stdio\"" /tmp/ser2net.yaml && nc -z 127.0.0.1 6666'
echo "PASS: ser2net $2 non-root runtime, generated config and TCP listener (no hardware acceptance)"
