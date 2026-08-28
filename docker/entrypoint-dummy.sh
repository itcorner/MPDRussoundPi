#!/bin/sh
# UNTESTED: dummy backend startup inside the container has not been verified yet.
set -e

state_file="${RUSSOUND_DUMMY_STATE:-/data/dummy_state.json}"
session="${RUSSOUND_DUMMY_TMUX_SESSION:-dummy}"

if [ ! -f "${state_file}" ]; then
    mkdir -p "$(dirname "${state_file}")"
    cp /app/tool/dummy_backend/example_state.json "${state_file}"
fi

set -- "$@" \
    --host "${RUSSOUND_DUMMY_HOST:-0.0.0.0}" \
    --port "${RUSSOUND_DUMMY_PORT:-6666}" \
    --state "${state_file}"

if [ "${RUSSOUND_DUMMY_DEBUG:-false}" = "true" ]; then
    set -- "$@" --debug
fi

if [ "${RUSSOUND_DUMMY_TUI:-true}" != "true" ]; then
    exec "$@"
fi

set -- "$@" --tui

# The curses TUI needs a pty, so it runs inside tmux and is reachable with
# `docker exec -it <container> tmux attach -t <session>`.
# Arguments contain no whitespace, so word splitting through "$*" is safe here.
tmux new-session -d -s "${session}" -x 200 -y 50 "$*"

echo "dummy backend: TUI running in tmux session '${session}'."
echo "dummy backend: attach with: docker exec -it \$(hostname) tmux attach -t ${session}"

while tmux has-session -t "${session}" 2>/dev/null; do
    sleep 5
done

echo "dummy backend: tmux session '${session}' ended, exiting." >&2
