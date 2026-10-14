import ipaddress

from neondeck import netguard as ng


class Sys:
    """Fake route/arp/ifconfig/scutil outputs, switchable between checks."""

    def __init__(self):
        self.gw, self.iface, self.mac, self.ip = "192.168.50.1", "en0", "02:00:5e:1:2:3", "192.168.50.23"
        self.arp_calls = 0
        self.scutil_calls = 0
        self.service_calls = 0
        self.scutil_ok = True  # False: configd gives nothing, detection falls back to route/arp

    def scutil(self, cmd):
        self.scutil_calls += 1
        if not self.scutil_ok:
            return ""
        if "Global/IPv4" in cmd:
            if not self.gw:
                return "  No such key\n"
            return (f"<dictionary> {{\n  PrimaryInterface : {self.iface}\n  PrimaryService : SVC-{self.gw}\n"
                    f"  Router : {self.gw}\n}}\n")
        self.service_calls += 1
        mac = f"  ARPResolvedHardwareAddress : {self.mac}\n" if self.mac else ""
        return (f"<dictionary> {{\n{mac}  Addresses : <array> {{\n    0 : {self.ip}\n  }}\n"
                f"  InterfaceName : {self.iface}\n  Router : {self.gw}\n"
                "  SubnetMasks : <array> {\n    0 : 255.255.255.0\n  }\n}\n")

    def __call__(self, cmd):
        if cmd[0] == "route":
            return f"gateway: {self.gw}\n interface: {self.iface}\n" if self.gw else ""
        if cmd[0] == "arp":
            self.arp_calls += 1
            return f"? ({self.gw}) at {self.mac} on en0 ifscope [ethernet]\n" if self.mac else \
                f"? ({self.gw}) at (incomplete) on en0\n"
        return f"{self.iface}: flags=8863<UP>\n\tinet {self.ip} netmask 0xffffff00 broadcast x\n"


class Harness:
    def __init__(self, cfg):
        self.sys = Sys()
        self.t = 0.0
        self.events = []
        self.cfg = cfg
        probe = ng.NetworkProbe(self.sys, lambda: self.t, scutil=self.sys.scutil)
        self.ctl = ng.LanController(ng.NetworkMonitor(probe, lambda: self.t), lambda: self.cfg,
                                    self.start, self.stop, self.down)

    async def start(self, ip):
        self.events.append(("start", ip))

    async def stop(self):
        self.events.append(("stop",))

    async def down(self):
        self.events.append(("down",))


def harness(**kw):
    cfg = {"port": 8787, "trusted_networks": [{"mac": "02:00:5e:01:02:03", "name": "casa"}], **kw}
    return Harness(cfg)


async def test_trusted_starts_on_interface_ip_only():
    h = harness()
    await h.ctl.check()
    assert h.events == [("start", "192.168.50.23")] and h.ctl.network_name == "casa"
    await h.ctl.check()
    assert h.events == [("start", "192.168.50.23")]  # steady state: nothing happens


async def test_trusted_to_untrusted_stops_and_closes_clients():
    h = harness()
    await h.ctl.check()
    h.sys.gw, h.sys.mac, h.sys.ip = "10.0.0.1", "aa:bb:cc:dd:ee:ff", "10.0.0.5"
    await h.ctl.check()
    assert h.events[1:] == [("stop",), ("down",)] and h.ctl.listening_ip is None
    assert not h.ctl.network_trusted


async def test_untrusted_never_starts():
    h = harness()
    h.sys.mac = "aa:bb:cc:dd:ee:ff"
    await h.ctl.check()
    assert h.events == []


async def test_ip_change_restarts_on_new_ip_without_closing_clients():
    h = harness()
    await h.ctl.check()
    h.sys.ip = "192.168.50.99"
    h.t += ng.NetworkProbe.REFRESH + 1  # same router: the address is re-read on the periodic refresh
    await h.ctl.check()
    assert h.events == [("start", "192.168.50.23"), ("stop",), ("start", "192.168.50.99")]


async def test_gateway_change_to_other_trusted_network():
    h = harness()
    h.cfg["trusted_networks"].append({"mac": "aa:bb:cc:dd:ee:ff", "name": "ufficio"})
    await h.ctl.check()
    h.sys.gw, h.sys.mac, h.sys.ip = "10.0.0.1", "aa:bb:cc:dd:ee:ff", "10.0.0.5"
    await h.ctl.check()
    assert h.events == [("start", "192.168.50.23"), ("stop",), ("start", "10.0.0.5")]
    assert h.ctl.network_name == "ufficio"


async def test_no_network_stops_listener():
    h = harness()
    await h.ctl.check()
    h.sys.gw = None
    await h.ctl.check()
    assert h.events[1:] == [("stop",), ("down",)]


async def test_arp_incomplete_fails_closed():
    h = harness()
    h.sys.mac = None
    await h.ctl.check()
    assert h.events == []


async def test_local_only_mode_never_listens():
    h = harness(bind="127.0.0.1")
    await h.ctl.check()
    assert h.events == [] and h.ctl.local_only


async def test_trust_added_later_is_picked_up():
    h = harness()
    h.cfg["trusted_networks"] = []
    await h.ctl.check()
    assert h.events == []
    h.cfg["trusted_networks"] = [{"mac": "02:00:5e:01:02:03", "name": "casa"}]
    await h.ctl.check()
    assert h.events == [("start", "192.168.50.23")]


async def test_start_failure_retries_next_check():
    h = harness()
    calls = {"n": 0}

    async def flaky(ip):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("address in use")
        h.events.append(("start", ip))

    h.ctl._start = flaky
    await h.ctl.check()
    assert h.ctl.listening_ip is None
    await h.ctl.check()
    assert h.ctl.listening_ip == "192.168.50.23"


def test_probe_reads_service_only_on_change_or_refresh():
    s = Sys()
    t = [0.0]
    probe = ng.NetworkProbe(s, lambda: t[0], scutil=s.scutil)
    probe(); probe(); probe()
    assert s.scutil_calls == 4 and s.service_calls == 1  # one cheap global read per call
    assert s.arp_calls == 0  # configd is enough: arp never runs
    t[0] += ng.NetworkProbe.REFRESH + 1
    probe()
    assert s.service_calls == 2
    s.gw = "10.0.0.1"
    probe()
    assert s.service_calls == 3 and s.arp_calls == 0


def test_probe_falls_back_to_arp_when_scutil_has_no_mac():
    s = Sys()
    s.scutil_ok = False
    t = [0.0]
    probe = ng.NetworkProbe(s, lambda: t[0], lambda gw: None, scutil=s.scutil)
    assert probe()["mac"] == "02:00:5e:01:02:03"
    probe(); probe()
    assert s.arp_calls == 1  # cached between refreshes


def test_ifconfig_by_iface():
    txt = "lo0: flags=1\n\tinet 127.0.0.1 netmask 0xff000000\nen0: flags=2\n\tinet 192.168.50.23 netmask 0xffffff00\nen5: x\n\tinet 10.0.0.2 netmask 0xffffff00\n"
    d = ng.parse_ifconfig_by_iface(txt)
    assert list(d) == ["en0", "en5"] and d["en0"][0] == ipaddress.ip_interface("192.168.50.23/24")


async def test_localnet_permission_keeps_listener_off_and_warns_rate_limited(caplog):
    import logging

    h = harness()
    base = h.sys.__call__

    def filtered(cmd):
        if cmd[0] == "arp":
            return ""
        return base(cmd)

    clock = {"t": 0.0}
    h.sys.scutil_ok = False  # configd unusable: only arp is left, and it is filtered
    probe = ng.NetworkProbe(filtered, lambda: h.t, lambda gw: None, scutil=h.sys.scutil)
    h.ctl = ng.LanController(ng.NetworkMonitor(probe, lambda: h.t), lambda: h.cfg,
                             h.start, h.stop, h.down, lambda: clock["t"])
    with caplog.at_level(logging.WARNING, logger="neondeck.net"):
        await h.ctl.check()
        await h.ctl.check()
        assert len([r for r in caplog.records if "Rete locale" in r.getMessage()]) == 1
        clock["t"] += ng.LOCALNET_WARN_EVERY + 1
        await h.ctl.check()
        assert len([r for r in caplog.records if "Rete locale" in r.getMessage()]) == 2
    assert h.events == [] and h.ctl.listening_ip is None  # fail closed
    assert h.ctl.network_reason == ng.LOCALNET and h.ctl.checked


async def test_reason_cleared_once_network_is_readable():
    h = harness()
    await h.ctl.check()
    assert h.ctl.network_reason is None
