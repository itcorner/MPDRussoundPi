# Idea: Wi-Fi bootstrap on the Raspberry Pi

Status: **idea, not scheduled or authorized for implementation; no solution
chosen** (2026-11-21). This page collects the problem, the constraints of this
installation and the candidate solutions so that a decision can be made on
evidence. Facts marked *verified* were read from the reference Pi on
2026-11-21 (read-only). Facts about external projects come from their public
pages on the same day and are marked *unverified* until tested on the target.

## Goal

Let an operator get a headless Pi onto a Wi-Fi network, and later onto a
different one, without a keyboard, monitor, Ethernet cable or SSH. Today the
Pi's address, DNS and certificates all assume one fixed home network; moving
or re-provisioning means console access. This idea covers only **getting the
Pi online**. What else must change when the network changes is a separate idea:
[network relocation](./general_idea_network-relocation.md).

Use cases, in rough order of value:

1. **First install** of a new Pi: no network configured yet.
2. **Network replaced**: new router or password, same location.
3. **Relocation**: the system moves to another building or network.
4. **Recovery**: the Pi booted before the router after a power failure, or the
   saved network vanished, and nobody should need a laptop to bring it back.

## Reference installation (verified 2026-11-21)

| Fact | Value | Consequence |
|---|---|---|
| Hardware, OS | Raspberry Pi 4 Model B Rev 1.5, Debian 13 (trixie) | Candidate tools must support Debian 13. |
| Network stack | NetworkManager active and enabled; `wpa_supplicant` active; `dhcpcd` and `systemd-networkd` absent or disabled | Tools that require NetworkManager need no stack change. |
| Uplink | `wlan0` only, profile `heimnetz`, DHCP, autoconnect. `eth0` exists (`Wired connection 1`, autoconnect) but has no cable | Wi-Fi is the only path. Hotspot mode takes `wlan0` off the LAN: no SSH, no web UI from the LAN, and no Internet for streams until the Pi is back online. |
| Hardware AP support | Not confirmed: `iw` and `rfkill` are not installed | Verify with `iw list` before relying on AP mode (the Pi 4 radio normally supports it). |
| Wi-Fi country code | No country setting found in `/etc/wpa_supplicant` or `/etc/NetworkManager` (inconclusive without `iw reg get`) | The regulatory domain affects AP and 5 GHz operation and must be set before a hotspot is reliable. |
| Local name | Hostname `musicpi`, `avahi-daemon` active | A `.local` name may be usable to find the Pi after joining a network; not tested from a client. |
| NetworkManager dispatcher | No scripts installed (`no-wait.d`, `pre-down.d`, `pre-up.d` exist but are empty) | Network-change hooks would be new. |
| Running services | Web and ser2net in Docker, agent, supervisor, MPD instances and egress native | The Pi is the live audio system. Provisioning must not disturb it beyond the unavoidable loss of connectivity. |

## Constraints from this project

- **Host-native, outside the web app and containers.** Configuring Wi-Fi needs
  host privileges (NetworkManager, D-Bus, radio). The web container must not
  get `NET_ADMIN` or the host D-Bus. The
  [guided setup plan](./general_open_guided-first-time-setup.md) already
  excludes host-network changes from the browser, and this feature is a
  *precondition* for that wizard being reachable at all.
- **Deployed like everything else on the Pi:** an Ansible play with ownership
  markers, a backup and a documented rollback, rehearsed before it touches the
  live Pi. Wi-Fi is the only uplink, so a bad install can cut the Pi off.
- **Security.** While the hotspot is up, anyone in range can reach the setup
  page and could replace the saved network. Captive portals run over plain
  HTTP, so credentials cross the hotspot unencrypted unless the hotspot itself
  is WPA2. The hotspot must only exist while the Pi is offline, and the
  portal must not remain reachable once the Pi is connected.
- **Power-failure behavior.** After an outage the Pi usually boots before the
  router's Wi-Fi is back. The solution must keep retrying known networks and
  must not leave the Pi in hotspot mode forever or discard the saved network
  because of a brief outage.
- **No lock-in of the audio system:** playback must resume by itself when the
  network returns. Streams stop while offline (they need the Internet); the
  supervisor already retries.

## Evaluation criteria

Each candidate should be judged on the same points:

1. Trigger model: first boot only, or whenever no known network is available.
2. Behavior on a flaky or late network (grace period, retry, back-off).
3. Hotspot security: default password, WPA2 support, lifetime of the hotspot.
4. Interplay with the existing `heimnetz` profile and NetworkManager state;
   what it may overwrite.
5. How the operator finds the setup page (SSID, captive-portal detection on
   iOS, Android, desktop, fixed fallback address) and finds the Pi afterwards.
6. Customization: can the page match this project and link to the Russound UI.
7. Installation and updates on Debian 13 (package, binary, container), and
   whether it fits an Ansible play with rollback.
8. Maintenance signals, license and the size of what we would own.
9. Coverage of unusual networks: hidden SSIDs, WPA3, WPA-Enterprise, networks
   behind their own captive portal.
10. Test effort: what a rehearsal needs (spare card or Pi, cable fallback).

## Candidates

The candidates are not mutually exclusive; combinations are listed last.

### A. Comitup

Source: [repo](https://github.com/davesteele/comitup),
[home page](https://davesteele.github.io/comitup/).

- Always-on service. If the device cannot join a known access point it creates
  a hotspot `comitup-<nnn>` (persistent number) and serves a web page where the
  operator picks a visible network and enters credentials.
- Remembers several upstream networks and tries them at start.
- Setup page at `comitup-<nnn>.local` (mDNS), fallback address `10.41.0.1`;
  captive portal for iOS, Android, macOS, Linux and Firefox.
- With two Wi-Fi interfaces, one can stay the hotspot while the other connects.
- On Raspberry Pi the page has a "Locate" function that blinks the green LED.
- Requires NetworkManager and systemd. License GPL-2.0.
- Repository: 821 commits, 7 open issues; the captured page shows no releases.
- *Unverified:* whether it is available as a Debian 13 package, its default
  hotspot password, how it adopts an existing `heimnetz` profile, whether it
  provides hooks (for example a callback on state change), and its behavior
  when the router is late after a power cut.

### B. balena wifi-connect

Source: [repo](https://github.com/balena-os/wifi-connect),
[releases](https://github.com/balena-os/wifi-connect/releases).

- Captive-portal utility: scans networks, opens an access point with a portal,
  the operator selects a network and enters the passphrase; on success the
  access point is closed and NetworkManager stores the profile; on failure the
  access point returns.
- Default SSID "WiFi Connect", **open** by default; a WPA2 passphrase can be set
  (`--portal-ssid`, `--portal-passphrase`).
- Requires NetworkManager. Apache-2.0.
- Latest release seen: v4.11.91 (22 Oct); it requires a Debian 13
  `ca-certificates` version, and an earlier release moved its Docker base image
  to Debian trixie, which suggests current Debian 13 attention.
- Repository: 185 open issues and 28 open pull requests. A balena forum thread
  discusses community maintainers, and a downstream project (Anthias) reported
  that its integration had stopped working for a while because of missing
  armv6 builds, so maintenance risk deserves a closer look.
- Not designed to get through networks that have their own login page.
- It is a one-shot portal, not a supervisor: the logic that starts it when the
  Pi is offline (and again after a later loss) would be ours.
- The portal UI can be replaced, so it could carry this project's look.
- *Unverified:* released binaries for aarch64 on Debian 13, behavior with the
  existing `heimnetz` profile, retry behavior after a power cut, and WPA3 or
  Enterprise handling.

### C. Own minimal component on NetworkManager

- A small host service that uses NetworkManager's hotspot support and a
  minimal portal page, started when no known network is available.
- Full control of behavior, wording and integration with the guided setup, and
  no third-party license or release schedule to follow.
- Cost: we own the captive-portal detection quirks across phone operating
  systems, hotspot and DHCP/DNS handling, security review and tests, which the
  existing projects already carry. This is the largest effort and risk of the
  candidates.

### D. RaspAP

Source: [docs](https://docs.raspap.com/).

- A feature-rich Wi-Fi router and access-point management interface rather than
  a first-boot onboarding tool. It does mention joining a known network at
  boot.
- Probably too large for this purpose (it manages the Pi as a router), listed
  for completeness. *Unverified* for Debian 13 and NetworkManager.

### E. Out-of-band provisioning only (no on-device portal)

- Set Wi-Fi when the SD card is written (Raspberry Pi Imager customization or
  a configuration file on the boot partition), and use a cable or the console
  afterwards.
- Cheapest and nothing new runs on the Pi, but it does not help relocation or
  recovery by someone without a computer and an SD-card reader.
- *Unverified for Debian 13:* the exact mechanism the Imager and the image use
  for NetworkManager profiles.

### Other tools seen in searches (not evaluated)

[python-wifi-connect](https://github.com/Pistacchione/python-wifi-connect),
`pi-wifi-setup` and `headless_wifi_connect`. Secondhand, one of them is said to
recommend Comitup as the simplest option on Bookworm; not verified.

### Combinations to consider

- **E for the first flash, plus A or B** for later changes and recovery.
- **B with our own supervisor** if the setup page must match the project.
- **A or B first, C only if both fall short.**

## Comparison to fill in

Entries are what is currently known; "to verify" means not tested.

| | A. Comitup | B. wifi-connect | C. Own | D. RaspAP | E. Out-of-band |
|---|---|---|---|---|---|
| Trigger model | Continuous, whenever no known network | One-shot; trigger logic ours | Ours | Router-style | None on device |
| Remembers several networks | Yes (per page) | Through NetworkManager profiles | Through NetworkManager | To verify | n/a |
| Hotspot security | To verify (password default) | Open by default, WPA2 optional | Ours | To verify | n/a |
| Customizable page | Limited (to verify) | Replaceable UI | Fully | Large UI | n/a |
| License | GPL-2.0 | Apache-2.0 | Ours | To verify | n/a |
| Debian 13 evidence | To verify | Recent releases mention Debian 13 | n/a | To verify | To verify |
| Install form | To verify (package?) | Binary or container | Playbook | Installer script | None |
| Biggest unknown | Power-cut behavior, existing profile | Maintenance, aarch64 build | Effort | Fit | Not usable for relocation |

## Test plan before choosing

Run on a **spare SD card or second Pi**, not the live audio Pi; if the live Pi
is ever used, plug in the Ethernet cable first and keep a verified backup.

1. Check hardware AP support (`iw list`) and set the Wi-Fi country.
2. For each shortlisted tool: install on a clean Debian 13 image; first boot
   with no profile; with a wrong password; with the router powered off at boot
   and powered on 2 minutes later; with a hidden SSID; with WPA3 and, if
   relevant, WPA-Enterprise; switching to a second network.
3. Phones: captive-portal detection on iOS and Android, desktop browsers,
   what the user sees after success and how they find the Pi on the new LAN.
4. Interaction with the existing profile (`heimnetz`): is it kept, adopted or
   replaced; does a manual `nmcli` change survive.
5. Behavior of the rest of the stack while the Pi is offline and after it
   returns (agent, supervisor, MPD, web) and the time it takes to recover.
6. Security review: hotspot password, how long the hotspot stays up, whether
   the portal is reachable once connected, where credentials are stored.
7. Install, upgrade and removal as an Ansible play with rollback.

## Decisions needed

- Which use cases are required (the four above); relocation and recovery need
  an on-device solution, first install alone does not.
- Acceptable ownership: third-party daemon versus code we maintain.
- Hotspot policy: password, naming, maximum lifetime, trigger conditions.
- Whether the setup page must look like this project or may be generic.
- Whether the same component should also announce the Pi's address after
  joining (name or display), or leave that to mDNS and the router.

## Relationship to other plans

- [Network relocation](./general_idea_network-relocation.md): getting online is
  only the first half of moving to another network.
- [Guided first-time setup](./general_open_guided-first-time-setup.md): a Pi
  without a network cannot show the setup wizard; this feature would come
  before it.
- [Guided diagnostics](./general_idea_guided-diagnostics.md): offline and
  reconnect states could reuse its observations.
