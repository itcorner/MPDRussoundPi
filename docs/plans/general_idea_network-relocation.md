# Idea: moving the installation to another network

Status: **idea, not scheduled or authorized for implementation; no design
chosen** (2026-11-21). This page inventories everything in the current
installation that depends on the network it was deployed on, describes what
breaks when the network changes and collects directions to fix it. Facts marked
*verified* were read from the reference Pi on 2026-11-21 (read-only, no
changes); items marked *to verify* are inferences that still need a test.

## Goal

Let the system keep working, with a small and reviewed set of actions or none
at all, when the network around it changes. Getting the Pi onto the new Wi-Fi
is a separate problem: [Wi-Fi bootstrap](./general_idea_wifi-bootstrap.md).
This idea starts after the Pi is online and asks what else has to follow.

## What counts as a network change

| ID | Scenario | What changes |
|---|---|---|
| S1 | Same network, new lease | The router hands out a different IP (24 h DHCP leases; no reservation known). |
| S2 | Router replaced or reconfigured | New subnet, gateway and DNS server; same site. |
| S3 | Relocation | Another building: new subnet, DNS, possibly IPv6, different Internet quality. |
| S4 | Unusual networks | Phone hotspot, guest or hotel Wi-Fi with a login page, client isolation, networks that block NTP. |
| S5 | Interface change | Ethernet plugged in (the Pi has `eth0` and autoconnects it), or the Wi-Fi hotspot mode of the Wi-Fi bootstrap becomes active for a while. |

## What is tied to the network (verified 2026-11-21)

Reference Pi: `wlan0` at `192.168.1.105/24` from DHCP (lease 86400 s), gateway
and DNS server `192.168.1.1`, search domain `home.itcorner.net`.

| # | Item | Where it is set | Current value | Breaks when |
|---|---|---|---|---|
| 1 | Pi address | NetworkManager profile `heimnetz`, method `auto` (DHCP) | `192.168.1.105`; router reservation unknown | S1 to S5: every item below depends on it |
| 2 | Playback proxy site and certificate | Caddy site address in `/etc/caddy/mpdrussoundpi-playback-agent.Caddyfile`; self-signed certificate | Site `https://192.168.1.105:8443` (Caddy listens on all addresses, `*:8443`); certificate subject and only SAN `IP:192.168.1.105`, valid 2026-10-16 to 2027-10-16 | Address change: the certificate no longer matches the name the web app uses |
| 3 | Proxy source allowlist | `remote_ip` in the same Caddyfile | `192.168.1.0/24 172.18.0.0/16 172.19.0.0/16 10.231.84.0/28` | New LAN subnet: the LAN entry is wrong; the web container presumably reaches Caddy from the Docker bridge entry (to verify) |
| 4 | Web app to agent URL and trust | `RUSSOUND_PLAYBACK_AGENTS` in the web container; part of the reviewed deployment spec | URL `https://192.168.1.105:8443`, `tls_ca_file` pinned to the proxy certificate | Address change: connection fails (name mismatch) or goes to the wrong host |
| 5 | Egress filter DNS exception | `--resolver` argument in `mpdrussoundpi-egress.service`, rendered into an nftables table | `192.168.1.1` only; the filter blocks all private, loopback and special ranges for MPD, so a resolver must be listed explicitly | New or changed DNS server: MPD cannot resolve stream host names |
| 6 | Docker networks | Docker bridges | Active project network `10.231.84.0/28`; also `172.17.0.0/16` (`docker0`), `172.18.0.0/16`, `172.19.0.0/16` and `172.20.0.0/16` (three bridges left over from earlier deployments, link down) | New LAN overlaps one of these ranges: routing conflicts |
| 7 | Clock | `systemd-timesyncd` with Debian pool servers; no RTC (`RTC time: n/a`) | Synchronized, NTP active | Network that blocks NTP or a slow start: wrong time at boot |
| 8 | Operator tooling | `ansible/inventory.yaml` (`ansible_host: 192.168.1.105`), `known_hosts` entries, documentation examples, reviewed target specs | Address `192.168.1.105`, user `musicpi` | Address change: every controller command needs the new address |
| 9 | Access boundary | Web port published on `0.0.0.0:8000`; `RUSSOUND_ALLOWED_HOSTS` unset | Any Host header accepted; no login | Moving to a guest or public network widens who can reach an unauthenticated UI |
| 10 | Russound gateway | `RUSSOUND_BACKEND_HOST=russound-ser2net` on the Compose network | ser2net on the same Pi, serial adapter local | Not affected in this layout; an external or split-host gateway has its own address |

Adjacent finding (not caused by relocation): the live proxy certificate was
created 2026-10-16 and expires **2027-10-16**, although the proxy playbook
generates certificates for 3650 days. How this certificate was produced is
unclear; its renewal needs its own item before October 2027.

## What actually happens when the address changes

Observed design facts, effects to verify on a rehearsal system:

- **Playback integration stops, everything else keeps working.** Rooms and
  Russound control go through the local ser2net container and do not use the
  LAN. The web app's link to the agent (items 2 to 4) fails, so playback pages
  show the agent as unavailable. The native agent, supervisor and MPD keep
  running.
- **Streams fall silent after a DNS change** (item 5). The filter rejects
  traffic from the MPD control group to private ranges except the listed
  resolver, so a new DNS server blocks MPD's own name lookups. The agent
  resolves TuneIn stations outside that control group, so station search may
  keep working while playback fails, which would be a confusing partial failure
  (to verify).
- **Hard to see from the UI.** Nothing currently tells the operator that the
  cause is the address or DNS rather than the stream.

## Aspects in detail

### 1. Addressing and name stability

- A router reservation or a static address would keep S1 from happening, but it
  does not help S2 to S5, and it is configuration outside this repository.
- A name (the Pi announces `musicpi` through Avahi) helps people and the
  controller find it, but the proxy certificate and the web container cannot
  rely on it without more work: Docker's resolver does not do mDNS, so the
  container would need the address or an explicit mapping (to verify).
- The Pi's address on the **Docker side** is already chosen by us: the project
  network `10.231.84.0/28` has a fixed gateway address (`10.231.84.1`), and the
  handover that created it checked for route conflicts at that time.

### 2. Proxy site, certificate and allowlist (items 2 to 4)

The web app and the agent are on the same Pi in this layout, yet they talk
through the LAN address. Directions, not exclusive:

- **Do nothing automatic; make re-pointing an explicit reviewed operation.**
  Regenerate the certificate for the new address, update the Caddy site and
  allowlist, update the web deployment spec and redeploy through the reviewed
  operator, then refresh the pinned trust file. Correct, but a long procedure
  that needs a controller, and the Pi is unreachable for web changes in the
  meantime.
- **Decouple from the LAN:** use an address that never changes for the web to
  agent path, for example the Docker bridge gateway (`10.231.84.1`) with its
  own certificate, and drop the LAN site and allowlist entry in this layout.
  Changes the existing split-host design, where another machine reaches the
  agent over the LAN, so it needs its own layout rules.
- **Name instead of address:** a stable local name in the certificate
  (a DNS name SAN) plus a mapping inside the web container. Needs the mapping
  to survive and the name to resolve on the Pi.
- Either way the allowlist must follow the real subnet of any non-Docker
  client; for the single-Pi layout the Docker entries are what matter.
- **Certificate lifetime** belongs here too (see the adjacent finding).

### 3. Egress filter and DNS (item 5)

The unit contains a fixed resolver, `192.168.1.1`. Resolver addresses on other
networks differ (home routers use different gateways, mobile hotspots use their
own, some networks hand out IPv6 or several resolvers), and a router may change
its DHCP-announced resolver without any move. Directions:

- **Follow the network automatically:** read the active resolvers from
  NetworkManager and re-render the nftables table when the connection or its DNS
  changes, using a NetworkManager dispatcher hook (none exists today). Cost: a
  privileged hook that edits the firewall, and a resolver learned from the
  network deserves care, because a hostile network could announce a resolver to
  gain a DNS channel that bypasses the filter.
- **Constrain the choice:** only accept the default gateway or addresses that
  NetworkManager reports for the active connection, never arbitrary ones;
  support IPv6 resolvers and more than one resolver; keep the table consistent
  when the connection drops.
- **Local resolver on the Pi:** MPD talks to a resolver we control, which
  forwards upstream. The filter currently blocks loopback, so it would need
  an explicit exception, and the forwarder becomes another service to keep
  secure and in step with the network.
- **Explicit operation:** treat a resolver change like the address change in
  aspect 2, with a documented command and the same preflight.
- Everything else in the filter is network independent: the blocked ranges are
  fixed and cover private space generally, so no subnet needs to be listed.
- Interaction to check: networks where DNS is only reachable through a
  captive portal or an upstream that rewrites answers.

### 4. Docker ranges versus the new LAN (item 6)

A new network can use a range that a Docker bridge already holds. Phone
hotspots and some routers use private ranges in the `172.16.0.0/12` and
`10.0.0.0/8` space (for example iOS Personal Hotspot is commonly
`172.20.10.0/28`, which falls inside the `172.20.0.0/16` bridge that is still
configured; to verify). Directions:

- Remove the Docker networks confirmed to be unused (candidates: `172.18`,
  `172.19`, `172.20`; the deployment recovery path may still reference the
  `russound-dev` network) and any other leftover, which shrinks the collision
  surface without a design change.
- Choose Docker's default address pool and the project's `/28` from a range that
  home and mobile networks rarely use, and document it.
- Check collisions when the network changes (at least report them), not only
  once at deploy time.

### 5. Time (item 7)

The Pi has no battery-backed clock. After a power loss the clock must come back
before certificates are checked: the proxy certificate in this layout is
self-signed and valid from 2026-10-16, so a clock before that date would make
the web app reject it. `systemd-timesyncd` stores a recent time across
reboots (to verify), which usually avoids this, but a network that blocks NTP
leaves the clock unsynchronized for good. Directions: order services after
time synchronization, show the clock state in diagnostics, optionally
configure a local time source for restricted networks.

### 6. Operator and user access (items 8 and 9)

- The controller needs the new address (or a name) for SSH and for every
  reviewed operation. The inventory, `known_hosts` and documentation examples
  assume one address. Direction: one place per site for the address, and a
  documented way to find the Pi after a move.
- Browsers need to find the UI after a move: by address, by the mDNS name, or by
  something the bootstrap tool shows. Client isolation on guest networks blocks
  phone-to-Pi traffic altogether (to verify per network).
- The UI has **no login** and relies on a trusted network (stated in the guided
  setup plan). Moving to a guest or public network breaks that assumption.
  Options: document it as unsupported, require a trusted network for the web
  port, or add the access-control design that the guided setup plan defers.

### 7. Special networks

- **Captive portals** (hotel or dormitory Wi-Fi with a login page): streams
  need the Internet and will fail until the portal is accepted from a browser on
  that network. The Wi-Fi bootstrap tools reviewed so far do not get through
  such portals.
- **Client isolation / separate guest VLAN:** the web UI is reachable from
  nothing but the Pi itself.
- **IPv6 and dual stack:** the egress filter handles IPv6, but resolver
  discovery and the proxy site only know the IPv4 address today (to verify).
- **Ethernet plugged in:** NetworkManager brings up `Wired connection 1` and
  prefers it (lower route metric), so the address changes at once (S5).

### 8. Interaction with the Wi-Fi hotspot mode

While a bootstrap hotspot is active, `wlan0` carries the hotspot address, the
LAN address disappears and nothing on the Pi can reach the Internet. Services
keep running locally. When the Pi joins a network again, the address is new
(or the same by luck), so items 1 to 5 apply from then on. The design question
is whether any of it may fail quietly in the meantime. A hotspot also adds its
own subnet (for example the one that Comitup documents, `10.41.0.0/24`), which
must stay clear of the Docker ranges.

## Scenario impact (what each change breaks, per the inventory)

| Item | S1 new IP | S2 router replaced | S3 relocation | S4 hotspot / guest | S5 interface change |
|---|---|---|---|---|---|
| 2 proxy certificate and site | breaks | breaks | breaks | breaks | breaks |
| 3 allowlist LAN entry | stale | stale | stale | stale | stale |
| 4 web to agent URL | breaks | breaks | breaks | breaks | breaks |
| 5 egress resolver | ok unless router DNS changed | breaks | breaks | breaks | may break |
| 6 Docker overlap | ok | possible | possible | likely (e.g. phone hotspot) | ok |
| 7 clock | ok | ok | NTP may be blocked | NTP may be blocked | ok |
| 8 operator tooling | breaks | breaks | breaks | breaks | breaks |
| 9 access boundary | ok | ok | review | review | ok |
| 10 Russound gateway (local) | ok | ok | ok | ok | ok |

"Breaks" means a configured value no longer matches reality. How far each case
has been proven in practice is still to verify.

## Open questions and verification

On a rehearsal system, not the live Pi:

1. Change the Pi's address and record exactly what each component does:
   playback pages, web logs, agent, supervisor, MPD, Caddy.
2. Change the DNS server and confirm the MPD and agent behavior (aspect 3).
3. Move to a network that uses an overlapping Docker range and see how routing
   fails.
4. Boot with the clock wrong and with NTP blocked.
5. Check whether the Pi keeps its address (router reservation) and what the lease
   behavior is on this network.
6. Find out how the live proxy certificate was created (lifetime 365 days
   versus the playbook's 3650).
7. Confirm which Docker networks are unused and safe to remove.
8. Decide, per scenario, whether the aim is **no action**, **automatic
   follow**, or **one reviewed command**.

## Decisions needed

- Which scenarios (S1 to S5) must be supported, and at which level.
- Whether the web to agent path should stop depending on the LAN address.
- How the egress resolver follows the network, and who may add one.
- Whether foreign (guest, hotel, phone) networks are in scope given the
  missing login.
- Whether relocation becomes a reviewed operator action with preflight
  checks (subnet collisions, resolver, clock) or stays a documented manual
  procedure.

## Relationship to other plans

- [Wi-Fi bootstrap](./general_idea_wifi-bootstrap.md): getting onto the new
  network.
- [Playback egress hardening](./mpd_done_playback-egress-hardening.md): the
  filter whose resolver exception is network specific.
- [Guided first-time setup](./general_open_guided-first-time-setup.md): the access
  boundary and the setup page must stay reachable after a move.
- [Simplify deployment and configuration](./general_done_simplify-deployment-configuration.md):
  reviewed operations, target specs and the Pi handover this layout comes from.
- [Guided diagnostics](./general_idea_guided-diagnostics.md): where address,
  DNS and clock problems could be explained to the operator.
