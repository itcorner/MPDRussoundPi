# Playback deployment evidence

This is historical verification evidence, not a setup procedure. Use the
[current deployment guide](./playback-agent-deployment.md) for installation
and updates. Earlier checks do not certify later code or full lifecycle behavior.

## Approved isolated target test (2026-10-15)

An isolated agent with a private config copy verified real Pi cached
observations, metadata, routes/gain, HTTP authentication, bounded errors and
responsive liveness. Production config/intentions hashes and playback/audio
processes were unchanged. This was not a production write or LAN latency test.

## Production read-only rollout (2026-10-17)

Pi-local authenticated agent, Caddy TLS and Docker playback reads were verified.
Authenticated reads returned 200, unauthenticated reads 401, disallowed sources
403, and unsupported routes 404. The web cache and three output cards were
healthy. Read-only activation did not restart MPD or change playback.

The current endpoint is `https://192.168.1.105:8443`; the approved Caddy source
CIDR is `192.168.1.0/24`. This is an HTTP source filter, not an inbound firewall.

## S06 LAN latency acceptance (2026-10-17)

A 156-second Pi-to-web LAN sample completed 240 fetches, collecting 720 healthy
output observations with no stale/errors. All outputs had 64 metadata changes.
Metadata change intervals: p50 2.458 s, p95 2.971 s, maximum 3.394 s.

The conservative web-visible age estimate was p50 2.978 s, p95 4.819 s and
maximum 5.806 s. It combines agent playback age, time since the web cache's
second-precision success timestamp and HTTP duration; timestamp precision can
overstate age by nearly one second. The proposed 5-second p95 target was met.
This was not an independent browser measurement or an audio-dropout guarantee.

## Manual controls and TuneIn (2026-10-17)

S07 transport/gain controls were accepted by the user after production testing.
Individual user test cases were not enumerated; this was not restart/endurance
acceptance.

TuneIn search/selection was activated on the Pi. Later fixes removed repeated
resolution contention and added bounded M3U/PLS-to-direct-stream extraction.
Playlist-backed `s260655` played successfully. The initial test exception for
missing egress enforcement was superseded by the public-only filter rollout.

## Channel-based output identities (2026-10-17)

Station-named instances were renamed to stable channels:
`stereo_front`, `stereo_centerlfe`, `stereo_rear`. Stations, routes and gains were
preserved. The one-off migration tool was subsequently removed; there is no
supported old-schema migration or compatibility path.

## Configuration split rollout (2026-10-17)

Private backup:
`~/.local/state/mpdrussoundpi/config-split-backup-20261017-205452/`,
with source/config/runtime-state/unit copies and checksums.

The Pi moved to `outputs` plus station seeds in `config.json` and runtime
selections/gains/intentions/favorites in `playback.json`. Regenerated MPD configs
and live listeners used only `127.0.0.1:6601-6603`; a connection to the Pi LAN
address on 6601 failed. All outputs resumed their prior stations:

| Output | TuneIn station | Gain |
| --- | --- | --- |
| `stereo_front` | `s25744` Radio 88.6 | -7.2 dB |
| `stereo_centerlfe` | `s8007` ORF Hitradio OE3 | -1.6 dB |
| `stereo_rear` | `s260655` EROTICA LOUNGE | -0.6 dB |

FM4 (`s8235`) was also seeded as a favorite. Dashboard API checks switched rear
to FM4 and back and exercised favorites add/move/remove. Admin config remained
byte-identical (SHA-256 prefix `353969e8`). The supervisor and MPD readiness
commands run from the updated Pi checkout; the temporary supervisor override
was removed. The agent retains its independent installation.

## Public-only stream egress (2026-10-17)

The [egress plan and evidence](./playback-egress-hardening-plan.md) record the
approved MPD-only outbound filter, DNS-pinned Python fetching and live tests
with both curl and a real isolated MPD. Public playback succeeded; literal
private, DNS-to-private and public-to-private redirects were blocked. The
private fixture received zero requests. Normal gains, stations, routes and
admin-config bytes were preserved.

## Remaining acceptance

- Live URL-type favorite and no-default-station behavior.
- Persistence of edited favorites, gains and selections across restarts.
- Current-version whole-Pi reboot, user-manager restart and outage/fault tests.
- Live IPv6 transport; its policy/rule generation has unit coverage.
- Physical/analog audio and S10 hardware-driven automation acceptance.

Historical headless recovery checks for earlier code are recorded separately
in the hardware audit; they do not close these current-version gates.
