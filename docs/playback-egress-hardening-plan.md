# Playback egress hardening

Status: implemented and deployed on 2026-10-17. The three destination rules
below are verified on the Pi; full reboot/lifecycle acceptance remains separate.

This document describes policy/design and verification, not installation order.
For first install, updates, prerequisites and commands use the
[current deployment guide](./playback-agent-deployment.md).

Scope approved 2026-10-17: allow public internet streams, block loopback,
private LAN and link-local stream destinations, and enforce the policy after
DNS resolution and on every redirect. No inbound firewall or general host
network policy changes. The user explicitly approved an MPD-only outbound
filter, including a root-managed service.

## Implementation

1. Share an explicit public-unicast address policy between Python fetching and
   the generated nftables rules. Include IPv4 and IPv6; reject special-use,
   multicast and translation/tunneling ranges that could bypass private-address
   restrictions. Allow normal public stream ports, not only 80/443.
2. Resolve TuneIn API, playlist and selected stream hostnames. Reject empty or
   mixed public/non-public DNS answers. For Python HTTP fetching, connect
   directly to the checked socket address without another DNS lookup; preserve
   the original HTTP Host and TLS hostname/certificate validation. Ignore
   environment HTTP proxies. Repeat validation for every redirect and bound
   redirects and response sizes. Return safe explicit errors.
3. Put only the MPD user services in a persistent `mpdstreams.slice`. A
   root-managed oneshot service starts this slice and atomically installs a
   dedicated nftables table matching that cgroup. No existing firewall tables
   are flushed or rewritten.
4. Permit replies to locally initiated MPD control connections and public
   outbound destinations. Permit UDP DNS queries only to explicitly configured
   resolver addresses, port 53; this infrastructure exception is not permission
   for HTTP streams to LAN addresses. TCP DNS fallback to private resolvers is
   not allowed. Unix-socket PulseAudio is unaffected.
5. MPD startup waits for the root service to be active and fails closed if
   enforcement is unavailable. A root-owned runtime record of the loaded
   cgroup inode also gates startup; recreating the slice cannot silently bypass
   old rules. If the inode changes, reload the root service before starting MPD.
   Persist the slice/drop-ins and root unit.
   Reload the policy when the user manager is restarted, since cgroup IDs are
   recreated. MPD redirects and DNS rebinding are blocked at actual packet
   destinations, independently of the Python resolver.

## Validation

- Unit tests: IPv4/IPv6, mixed/empty DNS answers, pinned connections, TLS Host,
  redirect revalidation, proxy bypass prevention, bounded responses and safe
  failures. Keep TuneIn search/playlist regressions green.
- Rule-generation tests: public address classification matches nftables sets,
  cgroup-only matching, reply traffic and exact DNS exceptions.
- Pi: back up affected code/units and current state; syntax-check rules and
  units before loading. Verify localhost control still works and the three
  existing public stations play through correct routes/gains.
- In a transient probe service in the protected slice, test public HTTP,
  loopback, LAN and link-local connections, a hostname resolving to a blocked
  address, and public HTTP redirects to a blocked address. Use controlled test
  fixtures; do not contact third-party metadata services.
- Verify counters and the active/enabled units; restart the filter and MPD
  services to check persistence. A full Pi reboot still requires its own
  approved acceptance window.

## Deployment and rollback

Deploy the checked Python helpers to both the agent install and Pi checkout;
the web image uses the same safe fetcher for TuneIn search. Install the scoped
filter using the dedicated Ansible playbook, then restart MPD/supervisor/agent
and rebuild the web container. Expect a brief audio interruption.

On failure, stop MPD and its supervisor first. Restore backed-up code/drop-ins
and reload systemd. Remove only the dedicated `inet mpdrussoundpi_egress`
table if explicitly rolling back enforcement; never flush the host ruleset.
Restore compatible runtime state only if required; ordinary rollback must not
overwrite station/favorite/gain edits.

This does not add authentication, logging services, provider allowlists,
rate limits or automatic playback policy. The three destination rules do not
prove analog audio quality or full outage/reboot acceptance.

## Rollout evidence (2026-10-17)

- Private backup:
  `~/.local/state/mpdrussoundpi/egress-backup-20261017-215138/`
  (agent/checkout helpers, user units, config/runtime state, prior nftables
  ruleset and `SHA256SUMS`).
- The preinstalled nftables 1.1.3 supports cgroup-v2 socket matching.
  No packages or existing host firewall tables were changed.
- `mpdrussoundpi-egress.service` is active/enabled. The three MPD units use
  `mpdstreams.slice`; only that slice jumps to the destination filter.
- UDP DNS is allowed only to the configured resolver `192.168.1.1:53`.
  The root rule loader validates IP literals, so resolver hostnames and
  arbitrary nftables fragments are not accepted.
- Protected-slice curl: public HTTPS returned 204; loopback, LAN, link-local,
  `127.0.0.1.nip.io` resolving to loopback and a public HTTP 302 redirect to a
  local fixture were blocked. Block counters increased.
- An isolated real MPD with a null audio output played Radio 88.6, then failed
  to connect to the local fixture by literal address, DNS name and public
  redirect. The fixture received **zero requests**. The temporary MPD service
  and fixture were stopped; normal output queues were not changed.
- Public redirect fixture: `https://httpbin.org/redirect-to`. The first probe
  used httpbingo, which rejected private redirect targets itself (403); that
  response was not counted as filter acceptance. Validation was rerun against
  httpbin's actual 302 response.
- The deployed Python fetcher performed FM4 lookup, resolved the playlist-backed
  station `s260655`, followed a public redirect and rejected a private redirect.
- Restarting the root filter atomically reloaded its own table. All three normal
  outputs remained playing at -7.2/-1.6/-0.6 dB, with correct routes and unchanged
  admin-config SHA-256 (`353969e8...`).
- Local regression: 551 Python tests (one skipped), 27 JavaScript tests;
  new production modules have no Pylance diagnostics. The Ansible play passed
  syntax validation and completed with no failed tasks.

IPv6 address policy and rule generation have unit coverage and the Pi loaded
the IPv6 rules. Live public/private IPv6 transport was not separately exercised.
User-manager restart/whole-Pi reboot ordering is configured, not yet live-tested.
