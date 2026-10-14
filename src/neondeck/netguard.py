"""Network trust and abuse protection: gateway identification, subnet checks, auth throttling."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import math
import os
import re
import socket
import subprocess
import sys
import time
from collections import OrderedDict, deque
from datetime import date
from typing import Callable

CHECK_TTL = 30.0  # seconds a network check stays valid
MAX_FAILURES = 5
FAIL_WINDOW = 600.0
LOCKOUT = 600.0
MAX_TRACKED = 1024
LOCALNET = "localnet_permission"  # reason: macOS Local Network privacy hides the neighbor table (macOS only)
NO_ROUTE, NO_ARP = "no_route", "no_arp"
LOCALNET_WARN_EVERY = 600.0
log = logging.getLogger("neondeck.net")


# ---------------------------------------------------------------- pure parsers

def normalize_mac(raw: str) -> str | None:
    """'3c:a6:2f:1:2:3' -> '3c:a6:2f:01:02:03'; None when it is not a MAC."""
    parts = raw.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6 or not all(1 <= len(p) <= 2 and re.fullmatch(r"[0-9a-f]+", p) for p in parts):
        return None
    mac = ":".join(p.zfill(2) for p in parts)
    return None if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff") else mac


def parse_default_route(text: str) -> tuple[str | None, str | None]:
    """`route -n get default` output -> (gateway ip, interface)."""
    gateway = interface = None
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        value = value.strip()
        if key == "gateway" and value:
            try:
                ipaddress.ip_address(value.split("%")[0])
                gateway = value
            except ValueError:
                pass
        elif key == "interface" and value:
            interface = value
    return gateway, interface


def _scutil_top(text: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """scutil dictionary dump -> (top-level scalars, top-level arrays of scalars); nested dictionaries are skipped."""
    scalars: dict[str, str] = {}
    arrays: dict[str, list[str]] = {}
    depth = 0
    top = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "}":
            depth -= 1
            continue
        key, sep, value = line.partition(" : ")
        opens = line.endswith("{")
        if depth == 1 and sep:
            top = key.strip()
            if opens:
                if "<array>" in value:
                    arrays[top] = []
            else:
                scalars[top] = value.strip()
        elif depth == 2 and sep and not opens and top in arrays:
            arrays[top].append(value.strip())
        if opens:
            depth += 1
    return scalars, arrays


def parse_scutil_global(text: str) -> tuple[str | None, str | None, str | None]:
    """`show State:/Network/Global/IPv4` -> (primary service id, primary interface, router)."""
    scalars, _ = _scutil_top(text)
    return scalars.get("PrimaryService"), scalars.get("PrimaryInterface"), scalars.get("Router")


def parse_scutil_service_ipv4(text: str) -> dict:
    """`show State:/Network/Service/<id>/IPv4` -> {"mac", "router", "addresses", "masks", "interface"}."""
    scalars, arrays = _scutil_top(text)
    mac = normalize_mac(scalars.get("ARPResolvedHardwareAddress", ""))
    if not mac:  # the network signature carries the router MAC too
        m = re.search(r"RouterHardwareAddress=([0-9a-fA-F:]+)", scalars.get("NetworkSignature", ""))
        mac = normalize_mac(m.group(1)) if m else None
    return {"mac": mac, "router": scalars.get("Router"), "addresses": arrays.get("Addresses", []),
            "masks": arrays.get("SubnetMasks", []),
            "interface": scalars.get("InterfaceName") or scalars.get("ConfirmedInterfaceName")}


def parse_arp(text: str) -> str | None:
    """`arp -n <ip>` output -> normalized MAC; None for '(incomplete)' or no entry."""
    m = re.search(r"\bat\s+([0-9a-fA-F:]{5,17})\s", text + " ")
    return normalize_mac(m.group(1)) if m else None


def arp_entry_count(text: str) -> int:
    """Number of neighbor entries in `arp -an` output (complete or incomplete)."""
    return len(re.findall(r"^\S*\s*\([^)\s]+\)\s+at\s", text, re.MULTILINE))


def localnet_guidance(executable: str | None = None) -> str:
    """User-facing hint for the macOS Local Network permission of the Python interpreter."""
    real = os.path.realpath(executable or sys.executable)
    return (f"macOS blocca l'accesso alla rete locale per {real}: Impostazioni di Sistema → "
            f"Privacy e sicurezza → Rete locale → abilita '{os.path.basename(real)}', "
            "poi neondeck restart")


def parse_ifconfig_by_iface(text: str) -> dict[str, list]:
    """`ifconfig` output -> {interface name: configured addresses with prefix} (loopback/link-local skipped)."""
    result: dict[str, list] = {}
    name = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            name = line.split(":", 1)[0]
            continue
        parts = line.split()
        try:
            if parts[:1] == ["inet"] and "netmask" in parts:
                mask = parts[parts.index("netmask") + 1]
                prefix = bin(int(mask, 16)).count("1") if mask.startswith("0x") else None
                iface = ipaddress.ip_interface(
                    f"{parts[1]}/{prefix}" if prefix is not None else f"{parts[1]}/{mask}")
            elif parts[:1] == ["inet6"] and "prefixlen" in parts:
                addr = parts[1].split("%")[0]
                iface = ipaddress.ip_interface(f"{addr}/{parts[parts.index('prefixlen') + 1]}")
            else:
                continue
        except (ValueError, IndexError):
            continue
        if not (iface.ip.is_loopback or iface.ip.is_link_local):
            result.setdefault(name, []).append(iface)
    return result


def parse_ifconfig(text: str) -> list:
    """Flat list of all configured addresses (see parse_ifconfig_by_iface)."""
    return [i for lst in parse_ifconfig_by_iface(text).values() for i in lst]


def in_local_subnet(client_ip: str, interfaces) -> bool:
    """True when client_ip is inside the subnet of one of the host's interfaces."""
    try:
        ip = ipaddress.ip_address(client_ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip.version == i.version and ip in i.network for i in interfaces)


def subnet_of(gateway: str | None, interfaces) -> str | None:
    """Subnet (CIDR text) of the interface that contains the gateway."""
    try:
        gw = ipaddress.ip_address(gateway or "")
    except ValueError:
        return None
    return next((str(i.network) for i in interfaces if i.version == gw.version and gw in i.network), None)


def is_trusted(mac: str | None, trusted: list) -> bool:
    if not mac:
        return False  # fail closed
    return any(isinstance(n, dict) and normalize_mac(str(n.get("mac", ""))) == mac for n in trusted or [])


def new_entry(net: dict, name: str | None = None) -> dict:
    return {"mac": net["mac"], "name": name or f"Rete {net.get('subnet') or net['mac']}",
            "added": date.today().isoformat(), "subnet": net.get("subnet")}


# ---------------------------------------------------------------- detection

def _run(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def _scutil(cmd: str) -> str:
    """Feed one command to scutil: configd data is not subject to Local Network privacy."""
    try:
        res = subprocess.run(["scutil"], input=cmd + "\n", capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


SCUTIL_GLOBAL = "show State:/Network/Global/IPv4"


def _build(gateway, interface, mac, text, ip=None, subnet=None) -> dict:
    by_iface = parse_ifconfig_by_iface(text)
    interfaces = [i for lst in by_iface.values() for i in lst]
    if ip is None:
        ip = next((str(i.ip) for i in by_iface.get(interface or "", []) if i.version == 4), None)
    if subnet is None:
        subnet = subnet_of(gateway, interfaces)
    return {"mac": mac, "gateway": gateway, "interface": interface, "ip": ip,
            "subnet": subnet, "interfaces": interfaces}


def poke_gateway(gateway: str) -> None:
    """Send one empty UDP datagram to the gateway: fills the ARP cache (legacy fallback path)."""
    try:
        family = socket.AF_INET6 if ":" in gateway else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.setblocking(False)
            s.sendto(b"", (gateway, 9))
        time.sleep(0.15)  # let the ARP exchange complete
    except OSError:
        pass


def resolve_gateway_mac(gateway: str, run: Callable[[list[str]], str] = _run,
                        poke: Callable[[str], None] = poke_gateway) -> tuple[str | None, str | None]:
    """(MAC, reason). Reason is None on success, else NO_ARP or LOCALNET.

    LOCALNET means the neighbor table looks empty although a default route exists: a real LAN always
    has at least the gateway entry once we have talked to it.
    """
    mac = parse_arp(run(["arp", "-n", gateway]))
    if mac:
        return mac, None
    poke(gateway)
    mac = parse_arp(run(["arp", "-n", gateway]))
    if mac:
        return mac, None
    return None, (LOCALNET if arp_entry_count(run(["arp", "-an"])) == 0 else NO_ARP)


def _from_scutil(glob: tuple, run, scutil) -> tuple[dict | None, str | None]:
    """(network, reason) from configd data; (None, None) when it lacks the router MAC (caller falls back)."""
    service, iface, router = glob
    svc = parse_scutil_service_ipv4(scutil(f"show State:/Network/Service/{service}/IPv4"))
    router = svc["router"] or router
    iface = svc["interface"] or iface
    if not (router and svc["mac"] and svc["addresses"]):
        return None, None
    try:
        local = ipaddress.ip_interface(f"{svc['addresses'][0]}/{svc['masks'][0] if svc['masks'] else 32}")
    except ValueError:
        return None, None
    net = _build(router, iface, svc["mac"], run(["ifconfig"]), str(local.ip), str(local.network))
    if local not in net["interfaces"]:
        net["interfaces"].append(local)
    return net, None


def _diagnose(gtext: str, run, poke, scutil) -> tuple[dict | None, str | None]:
    glob = parse_scutil_global(gtext)
    if gtext.strip() and not glob[0]:
        return None, NO_ROUTE  # configd answered: there is no primary network
    router = glob[2]
    if glob[0]:
        net, _ = _from_scutil(glob, run, scutil)
        if net:
            return net, None
    gateway, interface = parse_default_route(run(["route", "-n", "get", "default"]))
    gateway = gateway or router
    if not gateway:
        return None, NO_ROUTE
    mac, reason = resolve_gateway_mac(gateway, run, poke)
    if not mac:
        return None, reason
    return _build(gateway, interface or glob[1], mac, run(["ifconfig"])), None


def diagnose_network(run: Callable[[list[str]], str] = _run, poke: Callable[[str], None] = poke_gateway,
                     scutil: Callable[[str], str] = _scutil) -> tuple[dict | None, str | None]:
    """(network, reason): reason explains a None network (NO_ROUTE, NO_ARP or LOCALNET).

    Primary source is configd (scutil); route/arp is only the fallback when configd lacks the router MAC.
    """
    return _diagnose(scutil(SCUTIL_GLOBAL), run, poke, scutil)


def detect_network(run: Callable[[list[str]], str] = _run, poke: Callable[[str], None] = poke_gateway,
                   scutil: Callable[[str], str] = _scutil) -> dict | None:
    """Identify the current network by the gateway's MAC. None when it cannot be determined.

    Returns {"mac", "gateway", "interface", "ip", "subnet", "interfaces"}.
    """
    return diagnose_network(run, poke, scutil)[0]


class NetworkProbe:
    """detect_network with a cheaper steady state: one `scutil` read per call, full detection only when the
    primary service/router changed, REFRESH elapsed, or the last result was a failure."""

    REFRESH = 300.0
    ARP_REFRESH = REFRESH  # backwards-compatible name

    def __init__(self, run: Callable[[list[str]], str] = _run, clock: Callable[[], float] = time.monotonic,
                 poke: Callable[[str], None] = poke_gateway, scutil: Callable[[str], str] = _scutil) -> None:
        self._run, self._clock, self._poke, self._scutil = run, clock, poke, scutil
        self.reason: str | None = None  # why the last call returned None
        self._key: tuple | None = None
        self._net: dict | None = None
        self._at = -math.inf

    def __call__(self) -> dict | None:
        gtext = self._scutil(SCUTIL_GLOBAL)
        key = parse_scutil_global(gtext)
        now = self._clock()
        if self._net is None or key != self._key or now - self._at >= self.REFRESH:
            self._net, self.reason = _diagnose(gtext, self._run, self._poke, self._scutil)
            self._key, self._at = key, now
        else:
            self.reason = None
        return self._net


class NetworkMonitor:
    """Lazily cached view of the current network: recomputed only when asked and older than CHECK_TTL."""

    def __init__(self, detect: Callable[[], dict | None] | None = None,
                 clock: Callable[[], float] = time.monotonic, ttl: float = CHECK_TTL) -> None:
        if detect is None:
            from . import platform
            detect = platform.current().network_probe()
        self._detect, self._clock, self._ttl = detect, clock, ttl
        self._at: float | None = None
        self._lock = asyncio.Lock()
        self.current: dict | None = None
        self.reason: str | None = None  # why `current` is None, when the detector can tell

    async def refresh(self, force: bool = False) -> dict | None:
        async with self._lock:
            now = self._clock()
            if force or self._at is None or now - self._at >= self._ttl:
                self.current = await asyncio.to_thread(self._detect)
                self.reason = None if self.current else getattr(self._detect, "reason", None)
                self._at = now
            return self.current

    @property
    def interfaces(self) -> list:
        return (self.current or {}).get("interfaces", [])


# ---------------------------------------------------------------- throttling

class AuthThrottle:
    """Per-IP bad-token counter: MAX_FAILURES within FAIL_WINDOW locks the IP for LOCKOUT."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_tracked: int = MAX_TRACKED) -> None:
        self._clock = clock
        self._max = max_tracked
        self._fails: OrderedDict[str, deque] = OrderedDict()
        self._locked: OrderedDict[str, float] = OrderedDict()

    def _prune(self) -> None:
        now = self._clock()
        for ip in [i for i, until in self._locked.items() if until <= now]:
            del self._locked[ip]
        for ip in [i for i, d in self._fails.items() if not d or now - d[-1] > FAIL_WINDOW]:
            del self._fails[ip]
        for table in (self._fails, self._locked):
            while len(table) > self._max:
                table.popitem(last=False)

    def is_locked(self, ip: str) -> bool:
        until = self._locked.get(ip)
        if until is None:
            return False
        if until <= self._clock():
            del self._locked[ip]
            return False
        return True

    def record_failure(self, ip: str) -> bool:
        """Register a bad token; True when this failure triggers the lockout."""
        now = self._clock()
        self._prune()
        d = self._fails.setdefault(ip, deque())
        d.append(now)
        while d and now - d[0] > FAIL_WINDOW:
            d.popleft()
        if len(d) >= MAX_FAILURES:
            self._locked[ip] = now + LOCKOUT
            del self._fails[ip]
            return True
        return False

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)


class RateLimitedLog:
    """Allows an event once per `interval` seconds per key (bounded table)."""

    def __init__(self, interval: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 max_tracked: int = MAX_TRACKED) -> None:
        self._interval, self._clock, self._max = interval, clock, max_tracked
        self._last: OrderedDict[str, float] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._interval:
            return False
        self._last[key] = now
        self._last.move_to_end(key)
        while len(self._last) > self._max:
            self._last.popitem(last=False)
        return True


def trusted_entry(mac: str | None, trusted: list) -> dict | None:
    if not mac:
        return None
    return next((n for n in trusted or [] if isinstance(n, dict) and normalize_mac(str(n.get("mac", ""))) == mac), None)


class LanController:
    """Decides whether the LAN listener may exist. It is the security boundary: no trusted network, no socket.

    `start(ip)` / `stop()` / `on_down()` are async callbacks supplied by the server.
    """

    INTERVAL = 30.0

    def __init__(self, monitor: NetworkMonitor, get_config: Callable[[], dict],
                 start: Callable, stop: Callable, on_down: Callable,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.monitor, self._cfg = monitor, get_config
        self._warn = RateLimitedLog(LOCALNET_WARN_EVERY, clock)
        self.network_reason: str | None = None
        self.checked = False  # True after the first completed check
        self._start, self._stop, self._on_down = start, stop, on_down
        self.listening_ip: str | None = None
        self.network_name: str | None = None
        self.network_trusted = False
        self._seen: tuple | None = None
        self.poke = asyncio.Event()
        self._lock = asyncio.Lock()  # the periodic loop and admin-triggered checks must not interleave

    @property
    def local_only(self) -> bool:
        return str(self._cfg().get("bind", "")) in ("127.0.0.1", "localhost", "::1")

    async def check(self) -> None:
        async with self._lock:
            await self._check()

    async def _check(self) -> None:
        net = await self.monitor.refresh(force=True)
        cfg = self._cfg()
        self.network_reason = None if net else self.monitor.reason
        self.checked = True
        if self.network_reason == LOCALNET and self._warn.allow(LOCALNET):
            log.warning("%s", localnet_guidance())
        mac = (net or {}).get("mac")
        entry = trusted_entry(mac, cfg.get("trusted_networks", []))
        ip = (net or {}).get("ip")
        self.network_trusted = entry is not None
        self.network_name = entry.get("name") if entry else None
        want = entry is not None and ip is not None and not self.local_only
        if want and self.listening_ip == ip:
            return
        if self.listening_ip is not None:  # network gone/untrusted, or the interface address changed
            await self._stop()
            self.listening_ip = None
            if not want:
                await self._on_down()
        if want:
            try:
                await self._start(ip)
            except OSError as exc:
                log.error("Impossibile ascoltare su %s:%s: %s", ip, cfg.get("port"), exc)
                return
            self.listening_ip = ip
            log.info("Rete fidata '%s': in ascolto su %s:%s", self.network_name, ip, cfg.get("port"))
            return
        state = ("local-only" if self.local_only else mac or "sconosciuta", entry is not None)
        if state != self._seen:
            if self.local_only:
                log.info("Modalità solo locale: nessun ascolto sulla rete")
            else:
                log.info("Rete non fidata (%s): accesso dalla rete disattivato", mac or "sconosciuta")
        self._seen = state

    async def run(self) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Errore nel controllo della rete")
            try:
                await asyncio.wait_for(self.poke.wait(), self.INTERVAL)
            except asyncio.TimeoutError:
                pass
            self.poke.clear()
