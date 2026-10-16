#!/usr/bin/env bash
# bash tests/usb-target-runtime.sh [user@host] [fixture-dir] [capture-dir]
# Fixture inputs are fragments named pipewire.conf/wireplumber.conf plus
# mpdrussoundpi-usb-target.lua/usb-target-hook.lua; capture-dir must not exist.
# Pi prerequisites: PipeWire/WirePlumber, Lua, jq, cc, pkg-config and
# libpipewire-0.3 development headers. No Python or dependency installation.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ${1:-} != --local ]]; then
  host=${1:-musicpi@192.168.1.105}
  remote=".usb-test-$(date +%s)-$$"
  fixture=${2:-}
  capture=${3:-}
  [[ -z $capture || ! -e $capture ]]
  ssh_args=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10)
  cleanup_remote() {
    ssh "${ssh_args[@]}" "$host" \
      "rm -rf -- '$remote' && test ! -e '$remote'" || :
  }
  trap cleanup_remote EXIT
  ssh "${ssh_args[@]}" "$host" "umask 077; mkdir -- '$remote'"
  COPYFILE_DISABLE=1 tar --no-xattrs -cf - mpdrussoundpi/russound/policy/usb-target{,-hook}.lua \
    tests/usb-target-policy-test.lua tests/usb-target-hook-test.lua tests/usb-target-runtime* |
    ssh "${ssh_args[@]}" "$host" "tar -xf - -C '$remote'"
  if [[ -n $fixture ]]; then
    COPYFILE_DISABLE=1 tar --no-xattrs -cf - -C "$fixture" \
      pipewire.conf wireplumber.conf mpdrussoundpi-usb-target.lua usb-target-hook.lua |
      ssh "${ssh_args[@]}" "$host" "mkdir '$remote/fixture' && tar -xf - -C '$remote/fixture'"
  fi
  ssh "${ssh_args[@]}" "$host" \
    "cd '$remote' && USB_RUNTIME_CAPTURE=1 bash tests/usb-target-runtime.sh --local"
  if [[ -n $capture ]]; then
    mkdir -p "$capture"
    ssh "${ssh_args[@]}" "$host" "tar -cf - -C '$remote/captures' ." |
      tar -xf - -C "$capture"
    echo "Captured real graphs and lifecycle evidence: $capture"
  fi
  exit
fi

root="$PWD/tests/.ut-$$"
pids=()
stop_owned() {
  local wanted=$1 index
  for index in "${!pids[@]}"; do
    if [[ ${pids[index]} == "$wanted" ]]; then
      kill "$wanted"
      wait "$wanted"
      unset 'pids[index]'
      return
    fi
  done
  return 1
}
cleanup() {
  local pid
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || :; done
  for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || :; done
  rm -rf -- "$root"
}
failed() {
  echo "FAIL at line $1" >&2
  tail -n 18 "$root/wp.log" "$root/pw.log" "$root/stream.log" >&2 || :
}
trap 'failed "$LINENO"' ERR
trap cleanup EXIT
umask 077
production_runtime="/run/user/$(id -u)"
production_lifecycle() {
  XDG_RUNTIME_DIR="$production_runtime" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$production_runtime/bus" \
  systemctl --user show pipewire.service pipewire-pulse.service wireplumber.service \
    -p Id -p MainPID -p ActiveState -p ActiveEnterTimestampMonotonic -p NRestarts
}
mkdir -p "$root"/{run,config/wireplumber/wireplumber.conf.d,data/wireplumber/scripts/lib,data/wireplumber/scripts/mpdrussoundpi,state,cache}
production_lifecycle >"$root/lifecycle-before.txt"
export XDG_RUNTIME_DIR="$root/run" XDG_CONFIG_HOME="$root/config"
export XDG_DATA_HOME="$root/data" XDG_STATE_HOME="$root/state" XDG_CACHE_HOME="$root/cache"
export WIREPLUMBER_CONFIG_DIR="$root/config/wireplumber"
export PIPEWIRE_CONFIG_DIR="$root"
export PIPEWIRE_RUNTIME_DIR="$root/run" PIPEWIRE_REMOTE=usb-policy-test
export DBUS_SESSION_BUS_ADDRESS="unix:path=$root/run/no-session-bus"
export DBUS_SYSTEM_BUS_ADDRESS="unix:path=$root/run/no-system-bus"
export WIREPLUMBER_DEBUG=3
export WIREPLUMBER_DATA_DIR="$root/data/wireplumber:/usr/share/wireplumber"
cp /usr/share/pipewire/client.conf "$root/client.conf"
pipewire --version
wireplumber --version
cp mpdrussoundpi/russound/policy/usb-target.lua "$root/data/wireplumber/scripts/lib/mpdrussoundpi-usb-target.lua"
cp mpdrussoundpi/russound/policy/usb-target-hook.lua "$root/data/wireplumber/scripts/mpdrussoundpi/"
cp tests/usb-target-runtime-observer.lua "$root/data/wireplumber/scripts/"
export LUA_PATH="$root/data/wireplumber/scripts/lib/?.lua;$root/data/wireplumber/scripts/mpdrussoundpi/?.lua;;"
lua tests/usb-target-policy-test.lua
lua tests/usb-target-hook-test.lua
cc -Wall -Wextra -Werror tests/usb-target-runtime-device.c \
  $(pkg-config --cflags --libs libpipewire-0.3) -o "$root/device"
cat > "$root/pipewire.conf" <<'EOF'
context.properties = { core.daemon = true core.name = usb-policy-test }
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
 { name = libpipewire-module-protocol-native }
 { name = libpipewire-module-metadata }
 { name = libpipewire-module-spa-node-factory }
 { name = libpipewire-module-client-node }
 { name = libpipewire-module-client-device }
 { name = libpipewire-module-access }
 { name = libpipewire-module-adapter }
 { name = libpipewire-module-link-factory }
]
context.objects = [
 { factory = spa-node-factory args = { factory.name = support.node.driver node.name = fixture.driver node.group = fixture priority.driver = 20000 } }
]
EOF
cp /usr/share/wireplumber/wireplumber.conf "$root/config/wireplumber/"
cat > "$root/config/wireplumber/wireplumber.conf.d/90-usb-test.conf" <<'EOF'
wireplumber.profiles = {
 usb-test = {
  inherits = [ main mixin.systemwide-session mixin.stateless ]
  hardware.audio = disabled
  hardware.bluetooth = disabled
  hardware.video-capture = disabled
  support.dbus = disabled
  test.usb-observer = required
 }
}
wireplumber.components = [
 { name = usb-target-runtime-observer.lua type = script/lua provides = test.usb-observer
   requires = [ support.standard-event-source ] }
]
EOF
mkdir -p "$root/pipewire.conf.d"
if [[ -d fixture ]]; then
  cp fixture/pipewire.conf "$root/pipewire.conf.d/80-generated.conf"
  cp fixture/wireplumber.conf "$root/config/wireplumber/wireplumber.conf.d/80-generated.conf"
  cp fixture/mpdrussoundpi-usb-target.lua "$root/data/wireplumber/scripts/lib/"
  cp fixture/usb-target-hook.lua "$root/data/wireplumber/scripts/mpdrussoundpi/"
else
cat >"$root/config/wireplumber/wireplumber.conf.d/80-generated.conf" <<'EOF'
wireplumber.components = [
 { name = mpdrussoundpi/usb-target-hook.lua type = script/lua
   provides = custom.mpdrussoundpi-usb-target requires = [ support.standard-event-source ] }
]
wireplumber.profiles = { main = { custom.mpdrussoundpi-usb-target = required } }
mpdrussoundpi.usb-targets = [
 { node = playback.stereo_front vendor = "0d8c" product = "0102" }
 { node = playback.stereo_centerlfe vendor = "0d8c" product = "0102" }
 { node = playback.stereo_rear vendor = "0d8c" product = "0102" }
]
EOF
  {
    echo 'context.modules = ['
    for specification in 'stereo_front FL FR' 'stereo_centerlfe FC LFE' 'stereo_rear RL RR'; do
      read -r name left right <<<"$specification"
      cat <<EOF
 { name = libpipewire-module-loopback args = {
   capture.props = { node.name = sink.$name audio.position = [ FL FR ] media.class = Audio/Sink }
   playback.props = { node.name = playback.$name audio.position = [ $left $right ]
     target.object = mpdrussoundpi.usb-required mpdrussoundpi.managed = true
     node.dont-fallback = true node.linger = true node.dont-reconnect = false
     stream.dont-remix = true node.passive = true }
 } }
EOF
    done
    echo ']'
  } >"$root/pipewire.conf.d/80-generated.conf"
fi
pipewire -c "$root/pipewire.conf" >"$root/pw.log" 2>&1 &
pids+=("$!")
for ((i=0; i<100; i++)); do [[ -S "$root/run/$PIPEWIRE_REMOTE" ]] && break; sleep .1; done
[[ -S "$root/run/$PIPEWIRE_REMOTE" ]]
pw-cli info 0 >/dev/null
wireplumber -p usb-test >"$root/wp.log" 2>&1 &
wp_pid=$!; pids+=("$wp_pid")
wait_log() {
  local pattern=$1 logfile=${2:-$root/wp.log}
  for ((i=0; i<100; i++)); do
    grep -q "$pattern" "$logfile" && return
    sleep .1
  done
  return 1
}
wait_log 'managed USB target hooks loaded'
wait_log 'FIXTURE observer loaded'
echo "PASS private daemon and startup component load"

create_sink() {
  local name=$1 card=${2:-0} channels=${3:-2} profile=${4:-analog-stereo}
  pw-cli create-node adapter "{ factory.name=support.null-audio-sink node.name=$name media.class=Audio/Sink object.linger=true audio.channels=$channels audio.position=[ FL FR FC LFE RL RR ] device.id=$card api.alsa.pcm.stream=playback device.profile.name=$profile node.pause-on-idle=false }" >/dev/null
}
snapshot() { pw-dump >"$root/graph.json"; }
node_id() {
  snapshot
  jq -er --arg name "$1" '.[] | select(.type == "PipeWire:Interface:Node" and .info.props["node.name"] == $name) | .id' "$root/graph.json"
}
links_to() {
  snapshot
  jq --argjson id "$1" '[.[] | select(.type == "PipeWire:Interface:Link" and .info["input-node-id"] == $id)] | length' "$root/graph.json"
}
wait_links() {
  local id=$1 wanted=$2 count
  for ((i=0; i<100; i++)); do
    count=$(links_to "$id")
    if [[ $wanted == yes && $count -gt 0 || $wanted == no && $count -eq 0 ]]; then return; fi
    sleep .1
  done
  return 1
}
create_sink unrelated
unrelated=$(node_id unrelated)
sleep 1
wait_log hardware_missing
[[ $(links_to "$unrelated") == 0 ]]
echo "PASS missing hardware and no fallback"
"$root/device" fixture.usb.a >"$root/card-a.id" 2>"$root/card-a.log" &
card_a_pid=$!; pids+=("$card_a_pid")
for ((i=0; i<100; i++)); do [[ -s "$root/card-a.id" ]] && break; sleep .1; done
card_a=$(cat "$root/card-a.id")
[[ $card_a =~ ^[0-9]+$ ]]
create_sink matching "$card_a" 6 analog-surround-51
matching=$(node_id matching)
wait_links "$matching" yes
for specification in 'stereo_front FL FR' 'stereo_centerlfe FC LFE' 'stereo_rear RL RR'; do
  read -r name left right <<<"$specification"
  id=$(node_id "playback.$name")
  for ((i=0; i<100; i++)); do
    snapshot
    if jq -e --argjson id "$id" --argjson target "$matching" \
      --arg left "$left" --arg right "$right" '
      [.[] | select(.type == "PipeWire:Interface:Port")] as $ports |
      [.[] | select(.type == "PipeWire:Interface:Link" and .info["output-node-id"] == $id)] as $links |
      ($links | length) == 2 and all($links[];
        .info as $link |
        ($link["input-node-id"] == $target) and
        ([$ports[] | select(.id == $link["output-port-id"]) | .info.props["audio.channel"]][0]) ==
        ([$ports[] | select(.id == $link["input-port-id"]) | .info.props["audio.channel"]][0]) and
        ([$ports[] | select(.id == $link["input-port-id"]) | .info.props["audio.channel"]][0] | . == $left or . == $right))
      ' "$root/graph.json" >/dev/null; then break; fi
    sleep .1
  done
  [[ $i -lt 100 ]]
done
cp "$root/graph.json" "$root/healthy.json"
echo "PASS all three loopbacks: FL/FR, FC/LFE, RL/RR actual port links"
wait_log 'FIXTURE SiLink session-item-added'
echo "PASS unique real Device + sink linked via stock SiLink"
"$root/device" fixture.usb.b >"$root/card-b.id" 2>"$root/card-b.log" &
card_b_pid=$!; pids+=("$card_b_pid")
for ((i=0; i<100; i++)); do [[ -s "$root/card-b.id" ]] && break; sleep .1; done
[[ -s "$root/card-b.id" ]]
wait_links "$matching" no
wait_log 'FIXTURE SiLink session-item-removed'
wait_log hardware_ambiguous
snapshot
cp "$root/graph.json" "$root/duplicate.json"
[[ $(links_to "$unrelated") == 0 ]]
echo "PASS duplicate Device without Node removes existing SiLink; no fallback"
stop_owned "$card_b_pid"
wait_links "$matching" yes
echo "PASS duplicate removal recovers"
pw-cli create-node adapter '{ factory.name=support.null-audio-sink node.name=fixture.filter media.class=Audio/Sink object.linger=true audio.channels=2 audio.position=[ FL FR ] node.link-group=fixture.filter filter.smart=true filter.smart.target="{\"node.name\":\"matching\"}" }' >/dev/null
filter=$(node_id fixture.filter)
wait_log 'got filter for given target'
wait_log 'rejected stock target override'
wait_links "$matching" no
[[ $(links_to "$filter") == 0 && $(links_to "$unrelated") == 0 ]]
echo "PASS final guard blocks installed stock smart-filter target override"
pw-cli destroy "$filter"
wait_links "$matching" yes
echo "PASS stock-filter removal recovers"
stop_owned "$card_a_pid"
wait_links "$matching" no
[[ $(links_to "$unrelated") == 0 ]]
echo "PASS card removal rejects stale sink; no fallback"
stop_owned "$wp_pid"
echo 'wireplumber.profiles = { usb-test = { custom.mpdrussoundpi-usb-target = disabled } }' \
  >"$root/config/wireplumber/wireplumber.conf.d/99-disable-usb.conf"
wireplumber -p usb-test >"$root/wp-stock.log" 2>&1 &
pids+=("$!")
wait_log 'waiting for defined target as dont-fallback is set' "$root/wp-stock.log"
sleep .5
node_id playback.stereo_front >/dev/null
[[ $(links_to "$matching") == 0 && $(links_to "$unrelated") == 0 ]]
! grep -q 'managed USB target hooks loaded' "$root/wp-stock.log"
echo "PASS absent component: sentinel keeps stock policy fail-closed and stream alive"
production_lifecycle >"$root/lifecycle-after.txt"
cmp "$root/lifecycle-before.txt" "$root/lifecycle-after.txt"
echo "PASS production service PIDs, activation timestamps and restart counts unchanged"
if [[ ${USB_RUNTIME_CAPTURE:-0} == 1 ]]; then
  mkdir captures
  cp "$root"/{healthy,duplicate}.json "$root"/lifecycle-{before,after}.txt captures/
fi
echo "PASS isolated USB runtime integration; only owned PIDs and paths cleaned"
