import ipaddress

from neondeck import netguard as ng

ROUTE_OK = """   route to: default
destination: default
       mask: default
    gateway: 192.168.50.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
ROUTE_NONE = "route: writing to routing socket: not in table\n"
ROUTE_V6 = "destination: default\n gateway: fe80::1%en0\n interface: en0\n"
ARP_OK = "? (192.168.50.1) at 02:00:5e:1:2:3 on en0 ifscope [ethernet]\n"
ARP_PADDED = "? (192.168.50.1) at 02:00:5E:AA:BB:0C on en0 ifscope [ethernet]\n"
ARP_INCOMPLETE = "? (192.168.50.1) at (incomplete) on en0 ifscope [ethernet]\n"
ARP_NONE = "192.168.50.1 (192.168.50.1) -- no entry\n"
IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1c2b:3d4e:5f60:7182%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.168.50.23 netmask 0xffffff00 broadcast 192.168.50.255
\tinet6 2001:db8::5 prefixlen 64 autoconf
en5: flags=8863<UP> mtu 1500
\tinet 10.8.0.2 netmask 0xfffffc00 broadcast 10.8.3.255
"""


def test_parse_default_route():
    assert ng.parse_default_route(ROUTE_OK) == ("192.168.50.1", "en0")
    assert ng.parse_default_route(ROUTE_NONE) == (None, None)
    assert ng.parse_default_route("") == (None, None)
    assert ng.parse_default_route(ROUTE_V6) == ("fe80::1%en0", "en0")


def test_parse_arp_and_normalize():
    assert ng.parse_arp(ARP_OK) == "02:00:5e:01:02:03"
    assert ng.parse_arp(ARP_PADDED) == "02:00:5e:aa:bb:0c"
    assert ng.parse_arp(ARP_INCOMPLETE) is None
    assert ng.parse_arp(ARP_NONE) is None
    assert ng.parse_arp("") is None


def test_normalize_mac():
    assert ng.normalize_mac("02:00:5e:1:2:3") == "02:00:5e:01:02:03"
    assert ng.normalize_mac("02-00-5e-01-02-03") == "02:00:5e:01:02:03"
    assert ng.normalize_mac("nope") is None
    assert ng.normalize_mac("00:00:00:00:00:00") is None
    assert ng.normalize_mac("1:2:3") is None


def test_parse_ifconfig_and_subnets():
    ifs = ng.parse_ifconfig(IFCONFIG)
    assert ipaddress.ip_interface("192.168.50.23/24") in ifs
    assert ipaddress.ip_interface("10.8.0.2/22") in ifs
    assert all(not i.ip.is_loopback and not i.ip.is_link_local for i in ifs)
    assert ng.in_local_subnet("192.168.50.77", ifs)
    assert ng.in_local_subnet("10.8.3.9", ifs)
    assert not ng.in_local_subnet("10.8.4.1", ifs)
    assert not ng.in_local_subnet("8.8.8.8", ifs)
    assert ng.in_local_subnet("::ffff:192.168.50.5", ifs)
    assert ng.in_local_subnet("2001:db8::99", ifs)
    assert not ng.in_local_subnet("garbage", ifs)
    assert not ng.in_local_subnet("192.168.50.5", [])
    assert ng.subnet_of("192.168.50.1", ifs) == "192.168.50.0/24"
    assert ng.subnet_of("172.16.0.1", ifs) is None


def test_is_trusted_fails_closed():
    nets = [{"mac": "02:00:5e:1:2:3", "name": "casa"}]
    assert ng.is_trusted("02:00:5e:01:02:03", nets)
    assert not ng.is_trusted("aa:bb:cc:dd:ee:ff", nets)
    assert not ng.is_trusted(None, nets)
    assert not ng.is_trusted("02:00:5e:01:02:03", [])
    assert not ng.is_trusted("02:00:5e:01:02:03", [{"name": "senza mac"}, "x"])


def no_scutil(cmd):
    return ""  # configd unavailable: forces the route/arp fallback


def fake_run(outputs):
    def run(cmd):
        return outputs.get(cmd[0], "")
    return run


def test_detect_network():
    net = ng.detect_network(fake_run({"route": ROUTE_OK, "arp": ARP_OK, "ifconfig": IFCONFIG}), scutil=no_scutil)
    assert net["mac"] == "02:00:5e:01:02:03" and net["subnet"] == "192.168.50.0/24"
    assert net["interface"] == "en0"
    assert ng.detect_network(fake_run({"route": ROUTE_NONE}), scutil=no_scutil) is None
    assert ng.detect_network(fake_run({"route": ROUTE_OK, "arp": ARP_INCOMPLETE}), scutil=no_scutil) is None
    assert ng.detect_network(fake_run({}), scutil=no_scutil) is None


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_throttle_lockout_and_expiry():
    clk = Clock()
    th = ng.AuthThrottle(clk)
    for _ in range(4):
        assert th.record_failure("1.2.3.4") is False
    assert not th.is_locked("1.2.3.4")
    assert th.record_failure("1.2.3.4") is True
    assert th.is_locked("1.2.3.4")
    assert not th.is_locked("1.2.3.5")
    clk.t += ng.LOCKOUT - 1
    assert th.is_locked("1.2.3.4")
    clk.t += 2
    assert not th.is_locked("1.2.3.4")


def test_throttle_window_and_bounds():
    clk = Clock()
    th = ng.AuthThrottle(clk, max_tracked=3)
    for _ in range(4):
        th.record_failure("a")
        clk.t += ng.FAIL_WINDOW / 3  # failures spread out: the window slides
    assert not th.is_locked("a")
    for i in range(10):
        th.record_failure(f"ip{i}")
    assert len(th._fails) <= 4
    th.record_failure("b")
    th.record_success("b")
    assert "b" not in th._fails


def test_rate_limited_log():
    clk = Clock()
    rl = ng.RateLimitedLog(60, clk, max_tracked=2)
    assert rl.allow("x") and not rl.allow("x")
    clk.t += 61
    assert rl.allow("x")
    rl.allow("y"); rl.allow("z")
    assert len(rl._last) == 2


async def test_network_monitor_cache():
    clk = Clock()
    calls = []

    def detect():
        calls.append(1)
        return {"mac": "aa:bb:cc:dd:ee:ff", "interfaces": []}

    mon = ng.NetworkMonitor(detect, clk, ttl=30)
    await mon.refresh(); await mon.refresh()
    assert len(calls) == 1
    clk.t += 31
    await mon.refresh()
    assert len(calls) == 2
    await mon.refresh(force=True)
    assert len(calls) == 3


ARP_ALL = ("? (192.168.50.1) at 02:00:5e:1:2:3 on en0 ifscope [ethernet]\n"
           "? (192.168.50.50) at aa:bb:cc:dd:ee:01 on en0 ifscope [ethernet]\n")


def test_arp_entry_count():
    assert ng.arp_entry_count(ARP_ALL) == 2
    assert ng.arp_entry_count(ARP_INCOMPLETE) == 1
    assert ng.arp_entry_count(ARP_NONE) == 0
    assert ng.arp_entry_count("") == 0


class ArpSys:
    """`arp -n <gw>` and `arp -an` with separate outputs; the poke can make the entry appear."""

    def __init__(self, single, full, after_poke=None):
        self.single, self.full, self.after_poke = single, full, after_poke
        self.pokes = []

    def run(self, cmd):
        if cmd[0] == "route":
            return ROUTE_OK
        if cmd[0] == "arp":
            return self.full if cmd[1] == "-an" else self.single
        return IFCONFIG

    def poke(self, gw):
        self.pokes.append(gw)
        if self.after_poke is not None:
            self.single = self.after_poke


def test_arp_filtered_is_localnet_permission_and_pokes_once():
    sysm = ArpSys(ARP_NONE, "")
    net, reason = ng.diagnose_network(sysm.run, sysm.poke, no_scutil)
    assert net is None and reason == ng.LOCALNET
    assert sysm.pokes == ["192.168.50.1"]
    assert ng.detect_network(sysm.run, sysm.poke, no_scutil) is None


def test_poke_populates_arp_and_succeeds():
    sysm = ArpSys(ARP_NONE, "", after_poke=ARP_OK)
    net, reason = ng.diagnose_network(sysm.run, sysm.poke, no_scutil)
    assert reason is None and net["mac"] == "02:00:5e:01:02:03"
    assert len(sysm.pokes) == 1


def test_no_poke_when_arp_already_works():
    sysm = ArpSys(ARP_OK, ARP_ALL)
    assert ng.diagnose_network(sysm.run, sysm.poke, no_scutil)[1] is None
    assert sysm.pokes == []


def test_gateway_missing_but_neighbors_present_is_no_arp():
    sysm = ArpSys(ARP_NONE, ARP_ALL)
    net, reason = ng.diagnose_network(sysm.run, sysm.poke, no_scutil)
    assert net is None and reason == ng.NO_ARP


def test_no_route_reason():
    assert ng.diagnose_network(fake_run({"route": ROUTE_NONE}), lambda gw: None, no_scutil) == (None, ng.NO_ROUTE)


def test_probe_exposes_reason():
    sysm = ArpSys(ARP_NONE, "")
    probe = ng.NetworkProbe(sysm.run, Clock(), sysm.poke, no_scutil)
    assert probe() is None and probe.reason == ng.LOCALNET
    sysm.single = ARP_OK
    assert probe() is not None and probe.reason is None


async def test_monitor_propagates_reason():
    sysm = ArpSys(ARP_NONE, "")
    mon = ng.NetworkMonitor(ng.NetworkProbe(sysm.run, Clock(), sysm.poke, no_scutil), Clock())
    await mon.refresh(force=True)
    assert mon.current is None and mon.reason == ng.LOCALNET


def test_localnet_guidance_mentions_binary():
    msg = ng.localnet_guidance("/usr/bin/python3")
    assert "Rete locale" in msg and "neondeck restart" in msg and "'python3" in msg


# ---------------------------------------------------------------- scutil (configd)

SC_GLOBAL = """<dictionary> {
  PrimaryInterface : en0
  PrimaryService : 5E1F0C2A-7B3D-4E6A-9C41-2D8A6B0F7E13
  Router : 192.168.50.1
}
"""
SC_SERVICE = """<dictionary> {
  ARPResolvedHardwareAddress : 02:00:5e:10:00:01
  ARPResolvedIPAddress : 192.168.50.1
  AdditionalRoutes : <array> {
    0 : <dictionary> {
      DestinationAddress : 192.168.50.42
      SubnetMask : 255.255.255.255
    }
    1 : <dictionary> {
      DestinationAddress : 169.254.0.0
      SubnetMask : 255.255.0.0
    }
  }
  Addresses : <array> {
    0 : 192.168.50.42
  }
  ConfirmedInterfaceName : en0
  InterfaceName : en0
  NetworkSignature : IPv4.Router=192.168.50.1;IPv4.RouterHardwareAddress=02:00:5e:10:00:01
  NetworkSignatureHash : <data> 0x4f1d2a9b7c3e5086d1a2b3c4e5f60718293a4b5c
  Router : 192.168.50.1
  SubnetMasks : <array> {
    0 : 255.255.255.0
  }
}
"""
SC_NO_KEY = "  No such key\n"
IFCONFIG_117 = "en0: flags=8863<UP>\n\tinet 192.168.50.42 netmask 0xffffff00 broadcast 192.168.50.255\n"


def test_parse_scutil_global():
    assert ng.parse_scutil_global(SC_GLOBAL) == ("5E1F0C2A-7B3D-4E6A-9C41-2D8A6B0F7E13", "en0", "192.168.50.1")
    assert ng.parse_scutil_global(SC_NO_KEY) == (None, None, None)
    assert ng.parse_scutil_global("") == (None, None, None)


def test_parse_scutil_service_ignores_nested_dictionaries():
    svc = ng.parse_scutil_service_ipv4(SC_SERVICE)
    assert svc == {"mac": "02:00:5e:10:00:01", "router": "192.168.50.1", "addresses": ["192.168.50.42"],
                   "masks": ["255.255.255.0"], "interface": "en0"}
    assert ng.parse_scutil_service_ipv4(SC_NO_KEY)["addresses"] == []


def test_parse_scutil_service_mac_from_network_signature():
    text = SC_SERVICE.replace("  ARPResolvedHardwareAddress : 02:00:5e:10:00:01\n", "")
    assert ng.parse_scutil_service_ipv4(text)["mac"] == "02:00:5e:10:00:01"
    no_mac = text.replace("IPv4.RouterHardwareAddress=02:00:5e:10:00:01", "")
    assert ng.parse_scutil_service_ipv4(no_mac)["mac"] is None


def make_scutil(glob=SC_GLOBAL, service=SC_SERVICE):
    def scutil(cmd):
        return glob if "Global/IPv4" in cmd else service
    return scutil


def arp_forbidden(cmd):
    assert cmd[0] not in ("arp", "route"), cmd
    return IFCONFIG_117 if cmd[0] == "ifconfig" else ""


def test_diagnose_prefers_scutil_and_never_calls_arp():
    net, reason = ng.diagnose_network(arp_forbidden, lambda gw: 1 / 0, make_scutil())
    assert reason is None
    assert net["mac"] == "02:00:5e:10:00:01" and net["gateway"] == "192.168.50.1"
    assert net["ip"] == "192.168.50.42" and net["subnet"] == "192.168.50.0/24" and net["interface"] == "en0"
    assert ipaddress.ip_interface("192.168.50.42/24") in net["interfaces"]


def test_diagnose_scutil_works_even_with_empty_ifconfig():
    net, _ = ng.diagnose_network(lambda cmd: "", lambda gw: None, make_scutil())
    assert net["ip"] == "192.168.50.42" and ng.in_local_subnet("192.168.50.5", net["interfaces"])


def test_diagnose_scutil_no_network_is_no_route():
    assert ng.diagnose_network(arp_forbidden, lambda gw: None, make_scutil(glob=SC_NO_KEY)) == (None, ng.NO_ROUTE)


def test_diagnose_scutil_without_mac_falls_back_to_arp():
    no_mac = SC_SERVICE.replace("ARPResolvedHardwareAddress : 02:00:5e:10:00:01", "").replace(
        "IPv4.RouterHardwareAddress=02:00:5e:10:00:01", "")
    sysm = ArpSys(ARP_OK, ARP_ALL)
    net, reason = ng.diagnose_network(sysm.run, sysm.poke, make_scutil(service=no_mac))
    assert reason is None and net["mac"] == "02:00:5e:01:02:03"
    filtered = ArpSys(ARP_NONE, "")
    assert ng.diagnose_network(filtered.run, filtered.poke, make_scutil(service=no_mac)) == (None, ng.LOCALNET)
    assert ng.diagnose_network(ArpSys(ARP_NONE, ARP_ALL).run, lambda gw: None,
                               make_scutil(service=no_mac)) == (None, ng.NO_ARP)


def test_trusted_mac_format_matches_existing_entries():
    net, _ = ng.diagnose_network(arp_forbidden, lambda gw: None, make_scutil())
    assert ng.is_trusted(net["mac"], [{"mac": "02:00:5E:10:0:1", "name": "casa"}])
