# Reliable `musicpi.local` (mDNS)

Phones and laptops find the web UI as `https://musicpi.local/` through multicast DNS, answered on
the Pi by Avahi. `ansible/playbooks/deploy-mdns.yml` hardens that name:

```sh
ansible-playbook -i ansible/inventory.yaml ansible/playbooks/deploy-mdns.yml --limit musicpi
```

Avahi restarts once when its configuration changes, so the name is gone for a few seconds.
Playback is not affected.

## What it changes

| Change | Why |
|---|---|
| `allow-interfaces=eth0,wlan0` (only the ones that exist) | By default Avahi also runs on every Docker bridge and veth pair. Those appear and disappear with each container restart or update (370 join/leave events in two weeks), and the Pi even answered its own name with a bridge address (`10.231.84.1`). |
| `use-ipv6=no`, `publish-aaaa-on-ipv4=no` | Clients received the `fe80::` link-local address first and tried it before `192.168.1.105`. Link-local IPv6 needs an interface scope, which some phones and browsers handle poorly. |
| `Restart=on-failure` for `avahi-daemon` | Debian's unit leaves a crashed Avahi stopped, so the name would vanish until the next reboot. |
| `mpdrussoundpi-mdns-check.timer` (every 2 minutes) | Runs `tool/mdns_check.py`. It restarts Avahi if Avahi is not running, stops answering on D-Bus, cannot resolve its own name, or published a different name. The last case is the known `musicpi-2.local` rename after a Wi-Fi reconnect makes Avahi see its own announcement as a conflict. It waits 5 minutes after any start before restarting again. |

The original `/etc/avahi/avahi-daemon.conf` stays next to the new one as a timestamped backup.
Inputs (in `ansible/roles/deployment_inputs/defaults/mdns.yml`):

- `mdns_interfaces`
- `mdns_ipv6`
- `mdns_check_interval`

## Checking it

```sh
systemctl status avahi-daemon mpdrussoundpi-mdns-check.timer
journalctl -u mpdrussoundpi-mdns-check --since today   # one JSON line per check
dns-sd -G v4v6 musicpi.local                            # on a Mac: only 192.168.1.105
```

## Noise in the Avahi log

`... not fitting in legacy unicast packet, dropping.` appears about once a minute. It comes from
the router (`192.168.1.1`), which browses all service types from a non-mDNS port. That browsing
does not affect how clients resolve the name.

## What the Pi cannot fix

mDNS is multicast, and Wi-Fi delivers multicast less reliably than unicast. In order of effect:

1. **Use Ethernet.** `eth0` is already allowed and will be used as soon as a cable is connected.
2. **Router and access points:** keep multicast DNS working between Wi-Fi and LAN. Options such as
   "multicast enhancement", "IGMP snooping" without a querier, or "multicast and broadcast
   filtering" on the Wi-Fi network commonly break `.local` names for wireless clients.
3. **Access point firmware:** on 2026-11-25, `musicpi.local` failed on a Mac and a phone with
   `DNS_PROBE_FINISHED_NXDOMAIN`, also after reconnecting to Wi-Fi. Both clients were on the
   6 GHz band, the Pi on 5 GHz. The Pi answered unicast queries correctly and received the
   clients' multicast, but no multicast from any device reached the clients. A pending access
   point firmware update fixed it. To tell this apart from a Pi fault: if the Pi's watchdog
   reports `"ok": true` and `https://192.168.1.105/` works, look at the Wi-Fi first (band,
   multicast settings, firmware).
4. **Fallback address:** reserve `192.168.1.105` for the Pi in the router's DHCP settings. The
   HTTPS certificate already includes that address, so `https://192.168.1.105/` works with the
   same trusted CA whenever `.local` does not resolve.
