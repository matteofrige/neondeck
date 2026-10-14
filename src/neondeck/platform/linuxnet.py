"""Linux network detection: `ip -j` with /proc/net fallbacks. Parsers are pure; I/O is injected."""
from __future__ import annotations

import ipaddress
import json
import math
import socket
import struct
import time
from typing import Callable

from ..netguard import NO_ARP, NO_ROUTE, _run, normalize_mac, poke_gateway, subnet_of

Run = Callable[[list[str]], str]
Read = Callable[[str], str]


def read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _json_list(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _hex_ip(h: str) -> str | None:
    try:
        return socket.inet_ntoa(struct.pack("<L", int(h, 16)))
    except (ValueError, struct.error):
        return None


def parse_ip_route_json(text: str) -> tuple[str | None, str | None]:
    """`ip -j route show default` -> (gateway, device) of the lowest-metric route that has a gateway."""
    best: tuple[int, str, str | None] | None = None
    for r in _json_list(text):
        gw = r.get("gateway")
        if r.get("dst") != "default" or not gw:
            continue
        try:
            ipaddress.ip_address(str(gw).split("%")[0])
        except ValueError:
            continue
        metric = r.get("metric") if isinstance(r.get("metric"), int) else 0
        if best is None or metric < best[0]:
            best = (metric, gw, r.get("dev"))
    return (best[1], best[2]) if best else (None, None)


def parse_proc_net_route(text: str) -> tuple[str | None, str | None]:
    """/proc/net/route -> (gateway, interface) of the lowest-metric default route with RTF_GATEWAY."""
    best: tuple[int, str, str] | None = None
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or f[1] != "00000000" or f[7] != "00000000":
            continue
        try:
            flags, metric = int(f[3], 16), int(f[6])
        except ValueError:
            continue
        gw = _hex_ip(f[2])
        if not flags & 0x2 or not gw or gw == "0.0.0.0":
            continue
        if best is None or metric < best[0]:
            best = (metric, gw, f[0])
    return (best[1], best[2]) if best else (None, None)


def parse_ip_neigh_json(text: str) -> str | None:
    """`ip -j neigh show <gw> dev <dev>` -> normalized MAC of a resolved entry."""
    for n in _json_list(text):
        states = n.get("state") or []
        if isinstance(states, str):
            states = [states]
        if any(s in ("FAILED", "INCOMPLETE") for s in states):
            continue
        mac = normalize_mac(str(n.get("lladdr", "")))
        if mac:
            return mac
    return None


def parse_proc_net_arp(text: str, gateway: str, dev: str | None = None) -> str | None:
    """/proc/net/arp -> MAC of `gateway` (complete entries only, flags & 0x2)."""
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 6 or f[0] != gateway or (dev and f[5] != dev):
            continue
        try:
            if not int(f[2], 16) & 0x2:
                continue
        except ValueError:
            continue
        mac = normalize_mac(f[3])
        if mac:
            return mac
    return None


def parse_ip_addr_json(text: str) -> dict[str, list]:
    """`ip -j addr` -> {interface: addresses with prefix} (loopback and link-local skipped)."""
    result: dict[str, list] = {}
    for dev in _json_list(text):
        name = str(dev.get("ifname") or "")
        for a in dev.get("addr_info") or []:
            try:
                iface = ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}")
            except (KeyError, ValueError, TypeError):
                continue
            if iface.ip.is_loopback or iface.ip.is_link_local:
                continue
            result.setdefault(name, []).append(iface)
    return result


def interfaces_from_proc(route_text: str, local_ips: list[str]) -> dict[str, list]:
    """Without `ip`: pair local addresses with the on-link routes of /proc/net/route to get prefixes."""
    nets: list[tuple[ipaddress.IPv4Network, str]] = []
    for line in route_text.splitlines()[1:]:
        f = line.split()
        if len(f) < 8 or f[1] == "00000000":
            continue
        dest, mask = _hex_ip(f[1]), _hex_ip(f[7])
        if not dest or not mask:
            continue
        try:
            nets.append((ipaddress.ip_network(f"{dest}/{mask}"), f[0]))
        except ValueError:
            continue
    result: dict[str, list] = {}
    for ip in local_ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        match = next(((n, dev) for n, dev in sorted(nets, key=lambda x: -x[0].prefixlen) if addr in n), None)
        if match and not addr.is_loopback and not addr.is_link_local:
            result.setdefault(match[1], []).append(ipaddress.ip_interface(f"{addr}/{match[0].prefixlen}"))
    return result


def default_route(run: Run, read: Read) -> tuple[str | None, str | None]:
    gw, dev = parse_ip_route_json(run(["ip", "-j", "route", "show", "default"]))
    return (gw, dev) if gw else parse_proc_net_route(read("/proc/net/route"))


def gateway_mac(gw: str, dev: str | None, run: Run, read: Read) -> str | None:
    cmd = ["ip", "-j", "neigh", "show", gw] + (["dev", dev] if dev else [])
    return parse_ip_neigh_json(run(cmd)) or parse_proc_net_arp(read("/proc/net/arp"), gw, dev)


def local_addresses(run: Run, read: Read) -> dict[str, list]:
    by_iface = parse_ip_addr_json(run(["ip", "-j", "addr"]))
    if by_iface:
        return by_iface
    from ..config import lan_ips
    return interfaces_from_proc(read("/proc/net/route"), lan_ips())


def diagnose_linux(run: Run = _run, poke: Callable[[str], None] = poke_gateway,
                   read: Read = read_file) -> tuple[dict | None, str | None]:
    """(network, reason) with the same dict shape as the macOS detection."""
    gw, dev = default_route(run, read)
    if not gw:
        return None, NO_ROUTE
    mac = gateway_mac(gw, dev, run, read)
    if not mac:
        poke(gw)
        mac = gateway_mac(gw, dev, run, read)
    if not mac:
        return None, NO_ARP
    by_iface = local_addresses(run, read)
    interfaces = [i for lst in by_iface.values() for i in lst]
    ip = next((str(i.ip) for i in by_iface.get(dev or "", []) if i.version == 4), None)
    return {"mac": mac, "gateway": gw, "interface": dev, "ip": ip,
            "subnet": subnet_of(gw, interfaces), "interfaces": interfaces}, None


class LinuxProbe:
    """diagnose_linux with a cheap steady state: full detection only when the default route changed,
    REFRESH elapsed, or the last result was a failure."""

    REFRESH = 300.0

    def __init__(self, run: Run = _run, clock: Callable[[], float] = time.monotonic,
                 poke: Callable[[str], None] = poke_gateway, read: Read = read_file) -> None:
        self._run, self._clock, self._poke, self._read = run, clock, poke, read
        self.reason: str | None = None
        self._key: tuple | None = None
        self._net: dict | None = None
        self._at = -math.inf

    def __call__(self) -> dict | None:
        key = default_route(self._run, self._read)
        now = self._clock()
        if self._net is None or key != self._key or now - self._at >= self.REFRESH:
            self._net, self.reason = diagnose_linux(self._run, self._poke, self._read)
            self._key, self._at = key, now
        else:
            self.reason = None
        return self._net
