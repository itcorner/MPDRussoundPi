#!/bin/sh
# UNTESTED: ser2net startup inside the container has not been verified on real hardware.
set -e

device="${RUSSOUND_SERIAL_DEVICE:-/dev/ttyUSB0}"

if [ ! -e "${device}" ]; then
    echo "ser2net: serial device ${device} not found. Pass it into the container (devices:)." >&2
elif [ ! -r "${device}" ] || [ ! -w "${device}" ]; then
    echo "ser2net: no read/write access to ${device}. Add the device group to the container (group_add)." >&2
fi

# Rendered at runtime because the device and line settings are environment driven.
cat > /tmp/ser2net.yaml <<EOF
%YAML 1.1
---
connection: &russound
    accepter: tcp,${RUSSOUND_SER2NET_BIND:-0.0.0.0},${RUSSOUND_SER2NET_PORT:-6666}
    enable: on
    options:
      telnet-brk-on-sync: true
      mdns: false
    connector: serialdev,${device},${RUSSOUND_SERIAL_OPTIONS:-19200n81},local
EOF

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec ser2net -n -c /tmp/ser2net.yaml
