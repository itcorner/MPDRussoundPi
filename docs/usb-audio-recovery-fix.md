# Coordinated USB audio recovery fix

Status: investigation and implementation design prepared, 2026-10-16.
No corrective configuration, code deployment, package change or audio-service
restart has been applied. Controlled reconnect cycles are paused.

## Observed failure

Before the physical test, the plugged-in adapter was absent from USB, ALSA
and PulseAudio. Kernel logs recorded USB interface/frequency errors and a
disconnect at 07:37:53 CEST. This is a separate hardware/USB failure; fixing
runtime naming does not establish its underlying cause.

A separate read-only recorder started at 19:53:52 CEST with a two-hour
deadline, under `playback_usb_recovery_audit.service`, recording to
`~/.local/state/mpdrussoundpi/usb-recovery-20261016/`. The completed endurance
run was not reused or overwritten.

After the user reconnected the same adapter to the same port, it enumerated
at 19:54:47 CEST as:

- USB vendor/product: `0d8c:0102`.
- Pulse card: `alsa_card.usb-0d8c_USB_Sound_Device-00.3`.
- Profile: `output:analog-stereo+input:analog-stereo`.
- Physical output: `alsa_output.usb-0d8c_USB_Sound_Device-00.3.analog-stereo`.

At 19:57:00 CEST this remained unchanged. The supervisor reported the exact
configured card name missing; the recorder also detected an incorrect
Radio 88.6 route. No healthy 5.1 baseline was established. This is not a
successful recovery merely because MPDs and virtual sinks report running.

The operational config, generated profile rule and all three loopback targets
refer to the unsuffixed device and its `analog-surround-51` hardware sink.
The returned adapter still advertises the required 5.1 output profile.

## What the suffix means

Read-only inspection of installed WirePlumber **0.5.8-2**, specifically
`/usr/share/wireplumber/scripts/monitors/alsa.lua`, establishes:

1. `prepareDevice` constructs the card name from device properties.
2. Its `device_names_table` allocator appends numeric suffixes when a name is
   already reserved.
3. Configured ALSA rules are applied **after** this deduplication.
4. Default ALSA output node names derive from the resulting card name.
5. Device removal includes name-reservation cleanup.

Thus `.3` is a WirePlumber naming collision suffix, not the ALSA card index.
It prevents the exact-name profile rule from matching and propagates into the
physical sink name, breaking the readiness check and loopback target together.

Only one live USB audio Device global and one WirePlumber process were found.
The adapter currently occupies ALSA card 3, but that coincidental number is
not what generates the `.3` suffix. Stale reservations after prior removal/
creation failures are plausible; their specific lifecycle cause is not proven.
Do not claim a confirmed upstream bug or patch distribution scripts on this
evidence alone. Cached apt metadata offers no newer WirePlumber candidate.

## Accepted hardware-selection decision

User selected **adapter model independent of USB port, rejecting multiple
matches**, rather than binding to the current physical port.

Match `device.api=alsa`, `device.bus=usb`, vendor `0x0d8c`, product `0x0102`.
Normalize equivalent hex representations explicitly when reading inventories.
Require exactly one matching device:

- Zero matches: unavailable; do not start/recover an intended-playing output.
- One match: use its actual current card identity.
- Multiple matches: explicit ambiguous-hardware failure; never pick first,
  lowest ALSA index, preferred name or remembered instance.

The serial `0d8c_USB_Sound_Device` is model-derived, not a unique device serial.
Descriptions, ALSA indices and `.N` suffixes are not persistent identities.
Moving the adapter between USB ports must remain supported.

## Coordinated implementation

### 1. Separate physical selection from logical output identity

Add an explicit optional hardware selector to each configured audio-device
entry, retaining existing `name`, `card-profile`, stream names, sink names,
ports, gains and intentions. Legacy entries retain exact-name behavior.
Validate selector fields and reject duplicate configured selectors/aliases.
No automatic production config migration during observation.

One shared read-only resolver must be used by readiness, deployment preflight
and recovery. Resolve the unique current card from actual inventory, rather
than stripping `.N` suffixes or trying sequential guessed names.

### 2. Select the actual physical target through a guarded linking hook

The read-only feasibility review below supersedes the initial proposal to
assign a shared physical `node.name` alias. Keep stable logical identity on the
three managed loopback playback nodes, but select the actual hardware node
through a dedicated WirePlumber `select-target` hook. Resolve its `device.id`
to exactly one matching model-selected card, then require the configured 5.1
profile and expected output node/channel properties.

Do not rename `device.name` or `node.name` after their deduplication. Dynamic
selection avoids dependence on the suffix and avoids alias collisions when two
identical adapters are attached. Restrict policy to explicitly marked managed
loopback nodes; leave unrelated desktop/client policy unchanged.

Register before `linking/find-defined-target`; do not allow later default,
best-target or stored-user-target policies to bypass a rejected selection.
For missing, ambiguous or wrong-profile hardware, explicitly remove existing
managed downstream links, clear the associated peer flags, and stop target
processing. Stopping an event alone does not unlink an existing route.
Observe device addition/removal, including a second matching card with no
playback node yet, and schedule a rescan. Event-driven ambiguity refusal is
not a guarantee of zero audio frames between physical attachment and policy
notification; do not promise instantaneous hardware isolation.

For an eligible unique target, delegate ordinary link creation to the existing
prepare/link hooks, retaining established channel mapping. A lifecycle-safe
rescan must reconnect the managed loopbacks after removal/re-addition.
The installed API supports this approach, but an isolated policy prototype and
real hotplug validation are still required before declaring it functional.

Profile selection remains in existing recovery using the uniquely resolved
card and requiring re-verification afterward. Do not install a broad model
profile rule that mutates both adapters before ambiguity is checked.
Read-only observers must never select profiles.

### 3. Verify actual downstream routing, not just virtual sinks

Extend readiness to associate the required hardware output with the uniquely
resolved card, profile and six-channel layout. Reject ambiguous output nodes
and wrong-device associations even when their names look correct.

Inspect all three loopback playback nodes' actual downstream links and channel
mapping. Merely verifying the MPD client reaches its virtual sink misses the
failure demonstrated here. Require FL/FR, FC/LFE and RL/RR to reach the intended
hardware channels, with no fallback links to a default stereo output.
Configure managed loopbacks with `node.dont-fallback = true` and
`node.linger = true`, retaining reconnect behavior (do not set
`node.dont-reconnect = true`). The first prevents fallback; linger matters
because the installed defined-target hook otherwise requests node destruction
when a required target disappears. The custom policy must independently enforce
fallback refusal and link cleanup; these properties alone do not implement
unique model selection.

### 4. Integrate existing recovery and observations

Reuse the existing supervisor/backend; no second recovery daemon.
Intended-playing recovery waits for unique hardware, 5.1 and valid downstream
mapping, then preserves existing mute/route/gain verification sequencing.
Stopped/paused outputs must remain silent throughout device return.

Expose unavailable, ambiguous, wrong-profile and wrong-downstream-target
failures distinctly in readiness/audit results, with sanitized agent errors.
The observer agent remains read-only. Its separate observation-test config
must be deliberately updated alongside the approved production migration;
it does not automatically inherit operational config changes.

Relevant implementation surfaces:

- [Audio-device parsing and rule generation](../mpdrussoundpi/russound/lib/audiodevice.py)
- [Loopback generation](../mpdrussoundpi/russound/lib/sink.py)
- [Audio setup](../mpdrussoundpi/russound/audiosetup.py)
- [Shared readiness](../mpdrussoundpi/russound/lib/audioready.py)
- [Backend recovery](../mpdrussoundpi/russound/lib/mpdbackend.py)
- [Route inspection](../mpdrussoundpi/russound/lib/streamroute.py)
- [Read-only collector](../mpdrussoundpi/russound/lib/playbackstatus.py)
- [Test recorder](../mpdrussoundpi/russound/playbackaudit.py)
- [Pi provisioning](../ansible/playbooks/setup-musicpi.yml)

## Required validation before deployment

Local tests must cover:

- Legacy exact-name configuration remains unchanged.
- Unique model matching works with unsuffixed, `.2`, `.3` and larger suffixes,
  different ALSA indices and different USB ports.
- Zero matches and two identical adapters fail explicitly without mutation.
- Other USB models and model-derived shared serials cannot select a target.
- Generated policy markers and all loopbacks use the same hardware selector.
- Runtime target ambiguity, wrong card association, wrong channel count,
  stereo fallback and disconnected downstream links are rejected.
- Hotplug of a second matching device disables eligible ambiguous routing;
  no arbitrary output is selected during create/remove event races.
- Observers issue no mutations; recovery changes only the uniquely matched
  intended device and still requires a fresh post-change readiness check.
- Saved play/pause/stop and configured gain remain unchanged.

Target validation must begin with an approved maintenance deployment:

1. Preserve the failed-attempt records, baseline config/generated files,
   intentions, daemon lifecycle and relevant USB/WirePlumber journal evidence.
2. Back up and deploy the coordinated config/rules/helpers as one change.
   Record any necessary audio-service restart explicitly as maintenance,
   never as successful automatic recovery.
3. Establish a healthy baseline with verified physical 5.1 output, correct
   channel mapping and calibrated gains.
4. Resume five 10-second disconnects and one two-minute disconnect, then repeat
   with one stopped and one paused stream, using the
   [USB test procedure](./mpd-playback-endurance-test.md#physical-reconnect-procedure).
5. Verify model selection across a port move and ambiguity refusal with an
   identical adapter if one is available. Mark unperformed cases unverified.
6. Accept recovery only when hardware, profile, downstream routing, intention,
   gain and physical signal checks pass. Preserve a rollback of the coordinated
   files; do not rewrite config to whatever runtime suffix happens to exist.

## Current boundary

The naming mechanism and cross-surface failure are established. The opt-in
implementation connects model selection, generated policy and full downstream
readiness. Approved maintenance deployment completed on 2026-10-16 at 20:46 CEST
with healthy software/digital signal checks. Physical output confirmation and
post-fix USB reconnect acceptance remain pending. Configurations without a
selector retain exact-name behavior.

## Steps 1 and 2 executed (2026-10-16)

### Failure evidence sealed; temporary recorder stopped

At 20:14:57 CEST, only `playback_usb_recovery_audit.service` was stopped.
Its final record reports `recorder_stopped` with status `interrupted`, correctly
reflecting an operator-ended test rather than a successful recovery run.
The completed 72-hour recorder remains inactive and its data was not changed.

Private target evidence is archived at:

```text
~/.local/state/mpdrussoundpi/usb-recovery-20261016-evidence-20261016-2014.tar.gz
SHA-256: 02fb39324a918cec58f495e0cd8611fbe4b76024e48b326082acd4813066182b
```

The archive contains the fresh test records plus operational/generated config,
intentions, service lifecycle snapshots, kernel/audio-user journal, USB topology
and PipeWire/card/sink inventories. It is mode 0600 and contains private URLs/
configuration; do not publish it unredacted.

Checksums of operational config, intentions and the completed endurance summary
matched before/after. Service lifecycle snapshots differ only for the stopped
temporary recorder; MPDs, supervisor, PipeWire, PipeWire-Pulse and WirePlumber
were not restarted or stopped. No corrective profile or routing action occurred.

### Installed-policy feasibility findings

Read-only source inspection on WirePlumber 0.5.8-2 verified:

| Installed source | Relevant mechanism |
| --- | --- |
| `scripts/linking/find-user-target.lua.example` | Supported custom `SimpleEventHook` before `linking/find-defined-target`, selecting a target through event data |
| `scripts/lib/linking-utils.lua` | Session-item object-manager access, target-event unpacking and lookup of existing `SiLink` objects |
| `scripts/linking/prepare-link.lua` | Existing links can be removed with `link:remove()` and peer state cleared; reconnect remains enabled unless explicitly disabled |
| `scripts/linking/rescan.lua` | Rescans iterate autoconnect streams, including already linked streams; linkable removal removes associated links and clears peer flags |
| Existing `ObjectManager` users | Device/node add/remove callbacks are available for lifecycle observation |
| `scripts/linking/find-defined-target.lua` | Missing required target plus `node.dont-fallback` stops policy; without `node.linger`, it also requests node destruction |

This supports proceeding with a custom linking-policy prototype, rather than
patching distribution ALSA monitor code or generating model-wide shared aliases.
It does not prove lifecycle ordering under all device races. In particular,
the prototype must prove enumeration includes newly added duplicate cards
before selecting a target, existing links are revoked on ambiguity, and
loopbacks survive/reconnect after a target disappears.

**Go/no-go:** proceed to local/isolated implementation and tests only.
No production policy loading, audio restart or further physical disconnects
until that validation passes and the maintenance deployment is approved.
The sound card remains connected in the failed baseline configuration.

## Step 3: isolated prototype

Implemented without installing policy, changing the audio configuration or
restarting services:

- [Python inventory validator](../mpdrussoundpi/russound/lib/audioidentity.py):
  normalized USB vendor/product IDs; unique ALSA/USB model selection independent
  of card suffix, ALSA index and USB port; explicit missing/ambiguous failures.
  The pure `pw-dump` validator checks the card-to-six-channel-output association
  and every configured playback loopback's downstream links, channel identity,
  port direction/node ownership and active/paused state. Empty/overlapping
  mappings, missing/duplicate links and malformed graph identities fail closed.
- [Lua decision core](../mpdrussoundpi/russound/policy/usb-target.lua):
  matching model/card/output selection and explicit refusal states.
- [WirePlumber hook prototype](../mpdrussoundpi/russound/policy/usb-target-hook.lua):
  maps the selected hardware node to its `SiLinkable`, selects before stock
  defined-target policy, and validates the result after stock `prepare-link`.
  It never replaces a target rejected by stock availability checks.
  Rejection removes existing managed `SiLink` objects and clears peer/failure
  state. Device add/remove schedules a linking rescan and revokes invalid
  managed links, including when a second matching card has no output node yet.
  Unconfigured playback streams are untouched.

The hook requires an explicit `mpdrussoundpi.usb-targets` array of
`{ node, vendor, product }` entries and an explicitly enabled component.
No such production configuration/component has been generated or installed.
The Lua sources are not automatically loaded by their presence in this checkout.
Do not manually copy only the hook into the live session.

### Validation and limits

- Latest focused run: **49 Python tests passed** across inventory validation,
  existing readiness, stream routing and audio setup; **20 Lua core assertions
  and 14 mocked-hook assertions passed**. Pylance reports no diagnostics in the
  new Python module/tests. Existing service/profile mutation calls in regression
  tests are mocked; their expected failure-path logs are not target failures.
- [Python tests](../tests/test_audioidentity.py) cover complete graphs, the three
  channel pairs, suffix/index/port independence, ambiguity, stereo/wrong-card
  output, inactive/wrong/missing links, invalid configuration and no mutation.
- [Core Lua harness](../tests/usb-target-policy-test.lua) runs in standalone Lua.
- [Hook harness](../tests/usb-target-hook-test.lua) supplies mock event sources,
  object managers, session items, flags and links. It checks selection, removal
  of both peers' stale state, rescan scheduling, ambiguity, recovery, final
  fallback rejection, already-linked handling and unmanaged streams.
- Both Lua harnesses were run through the Pi's standalone `lua -` interpreter
  using stdin; no policy files were written on the Pi, no WirePlumber component
  was loaded, and no real links were created or removed.
- A read-only **real `pw-dump`** resolved the connected
  `alsa_card.usb-0d8c_USB_Sound_Device-00.3` uniquely. Downstream validation
  rejected the current failed baseline with
  `Unique 5.1 hardware output is unavailable`, as intended.
- The installed `linking-utils`, `prepare-link`, `link-target` and event-source
  usages were checked against the adapter's APIs. Mock tests do **not** establish
  that hook ordering, object-manager visibility during hotplug, or startup
  component/module loading work in a real WirePlumber session.

### Remaining gate recorded at the end of step 3

At that point step 3 was prototype-only. The following was its outstanding
work list; implementation progress is recorded in the next section.

1. Exercise this adapter in a separate WirePlumber/PipeWire session, proving
   startup loading, card add/remove inventory visibility, link revocation and
   subsequent reconnection. Do not substitute the mock harness for this test.
2. Add the opt-in selector schema and consistent generator output: model-based
   selection, managed loopback identities, `node.dont-fallback=true`,
   `node.linger=true`, reconnect enabled, and coordinated script/component
   install/ownership/cleanup. Keep legacy exact-name behavior unchanged.
3. Connect the same selector and full downstream validator to readiness,
   supervisor/backend recovery and observation/audit paths. Mutating recovery
   must select only the unique card and re-read readiness after profile changes;
   observation must stay read-only. Test saved intention and gain preservation.
4. Run the associated regression tests and review generated files before asking
   for maintenance approval. Further physical USB tests remain paused.

## Step 4: coordinated opt-in implementation

The user clarified that this step means finishing implementation and isolated
validation, **not production maintenance deployment**.

### Configuration and compatibility

Add the selector explicitly to an audio-device entry, retaining its existing
logical name, profile and sinks:

```json
"hardware-selector": {
  "vendor": "0d8c",
  "product": "0102"
}
```

The selector accepts only vendor/product, as 16-bit integer IDs or 1-4 hex
digits with optional `0x`. Explicit null/malformed selectors are rejected.
The opt-in path requires `analog-surround-51`, FL/FR stereo inputs and
non-overlapping FL/FR, FC/LFE or RL/RR hardware pairs. Duplicate model selectors,
audio-device names and sink aliases are refused. Sink names used in generated
policy must contain only ASCII word characters/hyphens.

Configurations without this field preserve legacy target/profile generation
and exact-name readiness. No operational or observation-test config has been
automatically migrated; stream IDs/labels, URLs, ports, gains and intentions
are not changed by this work.

### Generated configuration and ownership

[Audio setup](../mpdrussoundpi/russound/audiosetup.py) now preflights owned
fragments and current unique hardware before an opt-in installation writes
files. The coordinated artifacts are:

- Existing PipeWire loopback and WirePlumber fragments.
- `~/.local/share/wireplumber/scripts/lib/mpdrussoundpi-usb-target.lua`.
- `~/.local/share/wireplumber/scripts/mpdrussoundpi/usb-target-hook.lua`.
- An explicit `mpdrussoundpi.usb-targets` list and required
  `custom.mpdrussoundpi-usb-target` component in the WirePlumber fragment.

Only selector-managed playback nodes get the managed marker, no-fallback,
linger and reconnect properties. Their `target.object` is the intentionally
absent sentinel `mpdrussoundpi.usb-required`, not a renamed physical output.
The custom hook chooses the real target. The sentinel also prevents ordinary
stock fallback if the component is absent.

**No broad model-based profile mutation rule is generated.** Such a rule could
change both identical adapters before ambiguity is checked, violating the
accepted selection policy. Profile selection is instead performed by the
uniqueness-gated recovery path. This supersedes the shorthand “model-based
profile rules” in the earlier step-3 work list.

Scripts have Lua-compatible ownership headers. Install/cleanup refuses unowned
files or symlinked policy paths before changing the coordinated files; cleanup
removes only owned artifacts. Reinstalling a legacy config removes the old
owned USB policy scripts/component. Unrelated WirePlumber files remain intact.
There is no atomic multi-file filesystem transaction: use the maintenance
backup/rollback and do not start services until all files are installed.

### Shared readiness, recovery and observations

[Shared readiness](../mpdrussoundpi/russound/lib/audioready.py) uses the same
selector/config validator as setup and backend configuration parsing. It
correlates PulseAudio cards to PipeWire Device IDs, rejects inventory disagreement,
checks the actual six-channel hardware node and validates every configured
loopback-to-hardware channel link. Virtual sink existence alone cannot pass.
`pw-dump` is read-only and has a two-second subprocess timeout.

Mutating recovery targets the unique current PulseAudio card index, rechecking
PipeWire model uniqueness and device generation immediately before requesting
the profile. A profile request always reports not-ready until a subsequent
fresh check succeeds. Snapshot checks are not a hardware-atomic guarantee;
the event-driven policy independently revokes managed links on ambiguity.

The existing backend startup/reconcile and supervisor paths already use shared
readiness. Stopped/paused reconciliation still does not call audio recovery,
start playback, change gain or overwrite saved intentions. Collector and audit
already use its read-only mode, so they now check downstream links without
repairing them. The agent retains sanitized error messages and adds distinct
readiness codes for missing/ambiguous hardware, wrong profile/output and invalid
downstream routes; see the [read API](./mpd-playback-read-api.md#audio-values).

### Validation status

Targeted Python regressions cover schema rejection, suffix-independent readiness,
duplicate cards without outputs, ambiguity/device-generation changes immediately
before mutation, profile re-verification, inventory disagreement, missing/wrong
downstream links, ownership/cleanup/legacy reinstall, paused/stopped intentions,
gain preservation and read-only collector error reporting.

Concluding results:

- **194 Python tests passed** across selector/readiness, setup, backend, CLI,
  supervisor, collector, HTTP agent, audit and agent deployment regression tests.
- Standalone Lua: **20 core + 17 adapter assertions passed**.
- The [isolated runtime harness](../tests/usb-target-runtime.sh) passed with
  actual PipeWire 1.4.2/WirePlumber 0.5.8, synthetic exported Device/Node objects
  and real session-item links (**13 runtime checks**). It tested component loading, all three channel
  pairs, duplicate-card link removal/recovery, missing hardware, rejection of
  stock smart-filter overrides and absent-component sentinel protection.
- A second runtime run consumed the **actual Python-generated fragments and
  scripts unchanged**, rather than hand-written equivalents. The Python
  downstream validator accepted its real healthy graph (hardware node ID 71)
  and rejected the real duplicate-card graph with `hardware_ambiguous`.
- Production PipeWire/PipeWire-Pulse/WirePlumber lifecycle snapshots before and
  after the isolated run were byte-identical. The harness uses private
  runtime/config/data directories and socket, disables hardware monitors and
  isolates daemon bus access; its own processes and remote temporary files
  were cleaned.
- Read-only shared readiness against production inventories correctly reports
  `hardware_profile_unavailable` for the connected suffixed card's stereo
  profile. The in-memory selector used for this check was not saved to the Pi.

The runtime fixture uses explicit `0x`-prefixed USB IDs, as real ALSA properties
do: bare `"0102"` may be serialized numerically by `pw-dump` as decimal 102.
The Python helper intentionally treats integer values as numbers and hex
strings as hex; no matching rule was weakened to make the fixture pass.

The runtime harness accepts a target SSH address plus optional local fixture/
capture directories:

```sh
bash tests/usb-target-runtime.sh musicpi@192.168.1.105 FIXTURE_DIR CAPTURE_DIR
```

The optional fixture contains `pipewire.conf` and `wireplumber.conf` **fragments**
and the two generated Lua scripts. The harness loads the fragments under its
private `*.conf.d` directories. Captured graphs are test evidence, not production
config. See the harness header for target build/runtime prerequisites.

### Deployment gate recorded before maintenance

The coordinated code and isolated generated-config acceptance are complete.
**No production installation, profile change or audio restart has occurred.**
Physical USB/ALSA ACP enumeration, real profile transitions, audible channel
isolation and reconnect timing remain hardware acceptance work.

Next requires explicit maintenance approval:

1. Back up operational and separate observer configs, generated fragments,
   intentions and service lifecycle. Keep their private URLs/tokens private.
2. Deploy all changed helpers and both policy sources together; add the explicit
   selector to the operational config and deliberately update the separate
   observer config. Do not change logical output/stream IDs or gains.
3. Run the owned audio installation as maintenance, then verify fresh readiness,
   actual downstream routes, intentions, gains and physical 5.1 signal.
4. Only after the healthy physical baseline, resume the controlled USB recovery
   procedure. Service restarts during deployment are maintenance, not automatic
   recovery successes.

Rollback must restore the matching previous source/config/generated fragments
and remove only newly owned policy scripts/component before the approved audio
restart. Restoring only a hardcoded `.3` target is not a rollback strategy.

## Approved maintenance deployment (2026-10-16)

The user explicitly approved production configuration updates and the audio
restart. This is a maintenance recovery, **not a successful automatic USB
recovery cycle**.

### Backup and deployment

Private target rollback/evidence directory:

```text
~/.local/state/mpdrussoundpi/usb-maintenance-20261016-2044/
rollback-files.tar.gz
SHA-256: 7c9bc9eb9ee8e5644e952a50ddcaa3e455045dab2387400a936122eac2dde2eb
```

The directory is mode 0700 and archive mode 0600. It includes original source/
observer helpers, operational and observation-test configs, intentions, owned
generated fragments, absent-file records and lifecycle baseline. Configs
contain private URLs: do not publish the archive unredacted.

- Backed up and installed the tested coordinated helpers and both policy
  sources. Final deployed SHA-256 values match the local tested files for ten
  operational source files and seven corresponding observer helpers.
- Added only `hardware-selector` to each operational/observation-test
  audio-device entry. Semantic comparisons confirm every other configuration
  field is unchanged, including observer IDs/labels, streams and gain values.
- Paused only the supervisor and read-only agent during replacement. Existing
  intended-playing MPDs were software-muted before audio restart to prevent a
  full-gain window while virtual sinks were recreated.
- Ran the owned PipeWire audio installation. PipeWire and WirePlumber restarted
  at **20:46:01 CEST**, PipeWire-Pulse at **20:46:02**. Supervisor and observer
  resumed at **20:46:03** and restored the existing calibrated playback.
- The first verification exposed the operational checkout's older mixer helper
  lacking `read_sink_gain`. Its original was added to the rollback manifest,
  then the tested helper already present in the observer installation was
  synchronized into the operational checkout. No additional audio restart was
  required; final source hashes and complete verification passed.

### Verified target state

- Exactly one matching card, now with unsuffixed runtime name
  `alsa_card.usb-0d8c_USB_Sound_Device-00` after restart; actual active profile
  **`output:analog-surround-51`**. Selection uses model properties, not this name.
- Fresh shared read-only readiness passes all three loopback-to-hardware channel
  pairs and actual node/port/link associations.

| Stream | Saved/observed state | Gain dB | Measured stereo units | MPD route |
| --- | --- | --- | --- | --- |
| Radio 88.6 | play / play | -7.1 | 49905 / 49905 | correct |
| FM4 Radio | play / play | 0 | 65536 / 65536 | correct |
| OE3 Radio | play / play | -0.6 | 64044 / 64044 | correct |

- Saved intentions are **byte-identical** to the backup. The three MPDs retained
  PIDs **65286 / 65283 / 65284**, their Oct 12 start times and zero restarts.
- Audio services, supervisor and observer are active with zero automatic
  restarts after their explicit maintenance stop/start.
- Authenticated observer inventory is fresh, with playing state, ready audio,
  correct routes and matching measured gains for `front`, `centerlfe`, `rear`.
  Listener remains **127.0.0.1:8081**; unauthenticated access returns 401 and
  `write_operations` remains empty.
- Read-only stereo monitor samples showed signal in both channels for every
  stream. A separate native **six-channel hardware-master monitor** sample
  measured FL/FR **-20.93/-20.79**, FC/LFE **-19.21/-19.26**, RL/RR
  **-17.95/-17.77 dBFS RMS**. Programme-dependent samples establish digital
  signal presence, not analog jack placement or audible output.

The user confirmed that all three streams sound correct at their intended
physical destinations, without missing channels or unexpected mixing.

A fresh independent read-only recorder,
`playback_usb_recovery_fixed_audit.service`, started at **20:49:43 CEST** for
two hours, writing to
`~/.local/state/mpdrussoundpi/usb-recovery-fixed-20261016-2049/`.
Its first complete baseline includes playback, shared readiness, routes, gain,
signal, lifecycle and journal probes with **zero problem samples/open incidents**.
It does not overwrite either the sealed failed attempt or completed endurance run.

### Post-fix physical cycle 1: passed

The user performed one card-only short disconnect/reconnect on the same port:

- Kernel disconnect: **20:50:31 CEST**.
- New USB device/enumeration: **20:50:43 CEST** (12 seconds after disconnect,
  including human timing and enumeration).
- First recorder-confirmed full downstream readiness: **20:50:47.249 CEST**,
  approximately four seconds after enumeration, with observation latency.
  Supervisor logs report Radio 88.6 recovered at 20:50:47.546 and FM4 at
  20:50:48.025. These are not exact audible interruption measurements.
- Card returned with unsuffixed name and actual 5.1 profile. All three playback
  states, MPD routes, full downstream channel links and calibrated gain units
  passed again. Intentions remain byte-identical.
- PIDs/start times/restart counters for audio services, MPDs, supervisor and
  agent are unchanged from the post-maintenance baseline. **No manual repair
  or service restart occurred during the physical cycle.**
- Authenticated observer inventory is fresh/healthy. Fresh stereo RMS samples
  show both-channel signal on every stream.
- The user confirmed all three streams recovered correctly at their intended
  physical destinations.
- Recorder currently has no problem samples or open incidents. Its 30-second
  readiness cadence fell before/after the brief disconnect, so this does
  **not** mean no outage occurred; kernel and supervisor records establish
  the intentional interruption and automatic recovery.

Acceptance evidence is retained privately in the fresh recorder directory as
`cycle-01-acceptance.json`, `cycle-01-graph.json` and `cycle-01-lifecycle.txt`,
alongside the ongoing observations.

This accepts **one short same-port physical cycle**, not the full recovery
matrix. Four more short cycles, a two-minute disconnect, stopped/paused
intentions, port relocation and duplicate-adapter refusal remain unperformed.
This cycle did not reproduce a suffixed runtime name; suffix selection remains
covered by isolated tests and pre-maintenance read-only inventory checks.
The recorder remains running until approximately **22:49:43 CEST** unless
explicitly stopped earlier. Keep the card connected until the next guided cycle.

### Test paused before cycle 2: management reachability

On resuming at approximately **20:53 CEST**, repeated SSH connections to
`192.168.1.105` timed out before the cycle-2 baseline could be checked. Local
ping and TCP port 22 probes also failed, while the Mac's gateway responded.
The router still lists musicpi at that address. The user reports the Pi is
powered and the network radio streams continue playing.

No second disconnect, service restart or reboot was performed. Cycle 1 remains
accepted; further physical testing is paused until management access returns.
The recorder was last verified active earlier; its current health cannot be
confirmed remotely. The cached ARP entry and router listing do not establish
current bidirectional connectivity or identify the cause of the loss.

### Management restored; USB bandwidth failure (21:24 CEST)

SSH access returned without a Pi reboot (uptime remained over four days).
The user reported bandwidth errors on sound-card reconnection and confirmed
adding keyboard/mouse or receivers for local access during the network outage.

Read-only inspection confirmed:

- At **21:24:04**, kernel enumeration identifies `0d8c:0102` at `1-1.1`,
  then reports `Not enough bandwidth for new device state` and
  `can't set config #1, error -28` (`ENOSPC` in this USB scheduling context,
  not disk exhaustion).
- The device is visible to `lsusb` at full speed (12 Mbit/s), but its sysfs
  `bConfigurationValue` is empty. It has no ALSA card or PulseAudio card.
  This failure precedes WirePlumber target selection/profile recovery.
- Earlier failures also affected Logitech HID devices behind the same USB2
  branch and an external VIA hub. The first inspected audio altsetting
  bandwidth failures appear at **21:03:55**, following repeated USB
  disconnect/enumeration events and addition of peripherals.
- Current USB2 topology includes the C-Media card and Logitech mouse/receiver
  under VIA hub `2109:3431`. Shared bus periodic scheduling or controller/hub
  reservation state is a hypothesis, not a proven specific hardware/driver bug.
  Logs do not establish that ordinary network stream bandwidth is exhausted.
- Audio services/supervisor/recorder retain their post-maintenance PIDs and zero
  automatic restarts. The recorder is still sampling, with an open readiness
  measurement incident and one kernel journal-overflow flag. Do not count
  the unobserved/repeated manual reconnections as accepted controlled cycles.

Evidence was preserved privately under the fresh run's `bandwidth-2126/`
directory (kernel log, graph, USB topology and summary). No service restart,
profile change, controller reset or reboot was performed during investigation.
Next isolation step: remove newly added nonessential USB input peripherals
while preserving network connectivity, then recheck before another sound-card
reconnect. The driver-level failure is not addressed by changing loopback
channel mapping or selecting a different runtime suffix.

#### Isolation result: audio restored without restarting services

The user removed the added input peripherals at **21:26:17–18**. The sound
card alone remained visible but unconfigured; removal did not automatically
retry its failed USB configuration. One guided card reconnect followed:

- Disconnected **21:26:45**; enumerated **21:26:59** on `1-1.1`.
- `snd-usb-audio` bound successfully, ALSA card 3 returned and the active
  PulseAudio profile became `output:analog-surround-51`.
- Shared downstream readiness, all three MPD routes, gains and intentions
  passed. Supervisor recovered Radio 88.6 at **21:27:06.962**, FM4 at
  **21:27:14.903** and OE3 at **21:27:30.407**; the long absence had advanced
  its existing per-stream retry backoff. Do not equate enumeration with
  immediate supervisor recovery.
- The user confirmed all three streams sound correct again.
- No audio/MPD/supervisor/agent service restarted; saved intentions remain
  byte-identical. By **21:27:58** the recorder had no open incidents, with
  36 historical readiness measurement-error samples and one historical kernel
  journal-overflow flag retained.

This supports a USB scheduling/topology interaction associated with the added
peripherals or accumulated controller state; it does not distinguish actual
periodic-bandwidth pressure from a reservation-accounting/driver issue.
The Pi recovered without reboot, controller reset, sample-rate change or code
modification. The earlier network reachability loss remains unexplained.
Evidence after isolation is retained in `bandwidth-2126/` alongside the failure
capture. Treat this as a diagnostic recovery, not a clean cycle-2 repeat:
peripherals and topology changed during network troubleshooting. Leave
additional input peripherals disconnected for the remaining controlled audio
tests.

### Resumed physical tests: short cycles 2–5 and long disconnect

After isolation, the recorder remained active and a healthy baseline was
confirmed with no open incidents. Additional input peripherals stayed
disconnected. Four more short reconnects used the same current port (`1-1.1`).
These repeatability results apply to the reduced peripheral topology; they do
not establish recovery with the added receivers attached.

| Cycle | Disconnect CEST | Enumeration CEST | Observed absence | Selected supervisor recovery log CEST |
| --- | --- | --- | --- | --- |
| 2 | 21:29:28 | 21:29:38 | 10 seconds | Radio 88.6 21:29:43.991 |
| 3 | 21:30:45 | 21:30:55 | 10 seconds | Radio 88.6 21:31:01.057 |
| 4 | 21:32:02 | 21:32:11 | approximately 9 seconds | FM4 21:32:16.347 |
| 5 | 21:33:17 | 21:33:31 | 14 seconds | FM4 21:33:34.345; OE3 21:33:34.834 |

Cycle 4 was a nominal ten-second human-timed test, not evidence of an exact
ten-second minimum. The selected log timestamps are not all-stream audible
recovery measurements. After **each** cycle, shared downstream readiness,
all three playing states, routes, saved intentions and calibrated gains passed.
The user confirmed all three streams sound correct after each cycle.
Service PIDs/start times and restart counters remained unchanged. No new USB
bandwidth-allocation failure was found in these cycle kernel captures.

Five short physical cycles have now recovered successfully in total, counting
the earlier cycle 1 separately from the diagnostic isolation recovery. Private
per-cycle graph, lifecycle, verification and kernel captures are retained as
`cycle-02-*` through `cycle-05-*` in the fresh audit directory.

#### At-least-two-minute absence

- Card disconnected **21:34:43**, enumerated again **21:37:12**: **149 seconds**
  of observed absence, including operator/confirmation timing.
- During absence, the USB card inventory was empty and the supervisor reported
  missing selected hardware, reaching its existing **30-second retry backoff**.
  No service restart or manual profile/routing repair was performed.
- Recovery logs: OE3 **21:37:15.790**, FM4 **21:37:20.776**, Radio 88.6
  **21:37:41.741**. The last recovery was about 30 seconds after enumeration;
  this validates eventual backoff recovery, not a four-second recovery bound.
- Full shared readiness, routes, gain units and saved intentions passed again;
  all service lifecycles remained unchanged. The user confirmed audible
  correctness for all three streams.
- At **21:37:57**, the recorder had no open incidents. Historical readiness
  problem samples had increased to **43**, including intentional absences;
  its earlier kernel journal-overflow flag remained. These are measurement
  samples, not counts of independent outages.

Evidence is retained as `long-*` in the fresh audit directory, including the
missing-device graph and supervisor retry logs. Stopped/paused intentions,
physical port relocation and real duplicate-adapter refusal remain outstanding.

### Physical port relocation and mixed intentions

The card was moved from `1-1.1` to `1-1.2`: disconnect **21:51:28**,
enumeration **21:51:35**. Full readiness, all three routes/gains/intentions and
unchanged service lifecycles passed; the user confirmed all streams at their
original audio destinations. This demonstrates port-independent model matching,
not independence from the shared USB controller's bandwidth limits.
Private evidence is retained as `port-move-*`.

With explicit user approval, the normal backend paused FM4 and stopped OE3,
leaving Radio 88.6 playing. Initial setup encountered the normal control lock
before changing intentions; a bounded retry succeeded without bypassing it.
The original intention file was preserved privately.

The first mixed-intention reconnect was followed by additional user-confirmed
physical adjustments. Kernel evidence includes re-enumeration at **21:54:29**
and **21:56:53–57**, ending with `can't read configurations, error -32`.
Although earlier post-reconnect state/readiness checks had passed, the user
subsequently reported all outputs silent and fresh inspection found no USB/ALSA
card. This attempt is **not** an accepted controlled cycle; the earlier
software checks cannot establish continued audible recovery.

The user then reconnected to the earlier stable port, `1-1.1`, and left it
untouched. Enumeration succeeded at **21:59:01**. A fresh series of 36
post-reconnect samples over at least 35 seconds preserved:

| Stream | Saved intention | Observed state | User-confirmed audio |
| --- | --- | --- | --- |
| Radio 88.6 | play | play | audible |
| FM4 Radio | pause | pause | silent |
| OE3 Radio | stop | stop | silent |

Shared downstream readiness and the playing stream's route/calibration passed.
This validates mixed-intention preservation on recovery; the contaminated first
attempt must remain separate. Evidence is retained as `mixed-intentions-*`.
The backend subsequently restored FM4 and OE3. All three streams again passed
full verification and user audible confirmation; original intention bytes and
all service lifecycles matched the pre-test baseline.

### Real duplicate-adapter refusal and removal recovery

The user connected a second matching adapter without audio cables, leaving the
original untouched. At **22:02:09**, the second card appeared at `1-1.3`:

- Both `0d8c:0102` devices bound `snd-usb-audio` and appeared as PipeWire/Pulse
  cards. Original runtime name remained unsuffixed; second name ended **`.2`**.
- Original retained `output:analog-surround-51`; second had
  `input:analog-stereo`. It still counts for uniqueness even without a playback
  sink. No test-side profile change was applied.
- Shared readiness explicitly rejected the inventory as
  **`hardware_ambiguous`**.
- A fresh graph asserted **zero outgoing links from all three managed playback
  nodes**, proving existing physical routes were revoked rather than selecting
  the first card or a default target. Saved intentions were byte-identical.
- The user confirmed all original physical outputs were silent.
- The second card also emitted repeated USB bandwidth/altsetting errors.
  These remain a separate hardware limitation; rejection was verified with
  both matching devices present, not inferred from an enumeration failure.

The user removed only the second adapter at **22:03:25**. Original device 62
remained connected. Supervisor recovery logs: OE3 **22:03:26.337**,
Radio 88.6 **22:03:28.869**, FM4 **22:03:32.271**.
Full graph readiness, all routes/calibrated gains, original intention bytes and
unchanged service lifecycles passed again. The user confirmed all three streams
sound correct. Evidence is retained as `duplicate-*`.

### Final checkpoint after guided tests

At **22:04:45**, the independent recorder remained active, with no open
incidents. Its historical counters were 51 readiness problem samples, one
kernel journal-overflow flag and one OE3 route measurement error. Intentional
disconnects/state transitions are included; these counters are not outage
counts or a clean-run assertion.

Fresh stereo monitor samples had signal on both channels of every stream
(all above -65 dBFS RMS). Authenticated API inventory was fresh with every
playback/audio probe healthy; capabilities remained read-only and
unauthenticated health returned 401. Sanitized results are retained in
`final-signal-api.json`.

The planned physical scenarios have now been exercised, with the mixed-state
attempt/retry qualifications above. Remaining limitations: shared-controller USB
bandwidth failures are not fixed; the unrelated network outage remains
unexplained; sustained recovery under added input peripherals is not certified.
The observed real `.2` name occurred on the rejected second adapter, not a
uniquely selected suffixed production card. Isolated tests remain the evidence
for that latter selection case. The two-hour recorder is still running toward
its approximately **22:49:43** deadline; do not describe it as completed.

### Observation period completed (2026-10-16)

Final recorder event: **22:49:44.918 CEST** (`20:49:44.918 UTC`),
`recorder_stopped`, status **`completed`**. At **22:50:57**, systemd reported
the recorder inactive/dead with `Result=success`, exit status 0 and zero
restarts. The summary reports no open incidents.

Final historical problem counters remain **51 readiness measurement samples**,
**one kernel journal-overflow flag** and **one OE3 route measurement error**,
unchanged since the **22:04:45** checkpoint. Thus the final approximately
45-minute observation tail added no recorded problem samples. This is not a
claim that the entire two-hour fault-injection run was interruption-free, nor
that evidence retention was complete despite the earlier overflow flag.

Fresh post-completion verification passed full downstream readiness, all three
playing states/routes and calibrated gain units. Saved intention bytes and
service PIDs/start times/restart counters still match the maintenance baseline.
Post-completion verification, lifecycle and graph captures are retained as
`completed-*` in the same private audit directory. No services were restarted
or production files migrated to close the observation period.

The observation run is now complete. The documented USB bandwidth/controller
limitations, contaminated first mixed-state attempt and suffix-selection scope
qualifications remain unchanged.
