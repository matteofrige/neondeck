import asyncio
import json
import ipaddress
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from neondeck import server
from neondeck.config import load_config, save_config
from neondeck.netguard import NetworkMonitor

MAC = "02:00:5e:01:02:03"
IFS = [ipaddress.ip_interface("192.168.50.23/24")]


def net(mac=MAC):
    return {"mac": mac, "gateway": "192.168.50.1", "interface": "en0",
            "subnet": "192.168.50.0/24", "interfaces": IFS}


@pytest.fixture
def env(tmp_path):
    cfg = load_config(tmp_path)
    cfg["trusted_networks"] = [{"mac": MAC, "name": "casa"}]
    save_config(tmp_path, cfg)
    state = {"net": net()}
    app = server.make_app(tmp_path, cfg, NetworkMonitor(lambda: state["net"]))
    return app, cfg, state, tmp_path


def req(app, path, ip="192.168.50.50", headers=None, method="GET"):
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda k, d=None: (ip, 5555) if k == "peername" else d
    return make_mocked_request(method, path, headers=headers or {}, app=app, transport=transport)


async def call(app, r):
    async def handler(request):
        return web.json_response({"ok": True})
    return await server.security(r, handler)


def bearer(cfg):
    return {"Authorization": f"Bearer {cfg['token']}"}


async def test_device_ok_on_trusted_network(env):
    app, cfg, _, _ = env
    assert (await call(app, req(app, "/api/buttons", headers=bearer(cfg)))).status == 200


async def test_device_untrusted_network_403_even_with_token(env):
    app, cfg, state, _ = env
    state["net"] = net("aa:bb:cc:dd:ee:ff")
    r = await call(app, req(app, "/api/buttons", headers=bearer(cfg)))
    assert r.status == 403 and json.loads(r.body)["error"] == "Rete non fidata"


async def test_device_fails_closed_without_gateway(env):
    app, cfg, state, _ = env
    state["net"] = None
    assert (await call(app, req(app, "/api/buttons", headers=bearer(cfg)))).status == 403


async def test_localhost_bypasses_network_but_needs_token(env):
    app, cfg, state, _ = env
    state["net"] = None
    assert (await call(app, req(app, "/api/buttons", ip="127.0.0.1", headers=bearer(cfg)))).status == 200
    assert (await call(app, req(app, "/api/buttons", ip="::1"))).status == 401


async def test_client_outside_local_subnet_refused(env):
    app, cfg, _, _ = env
    r = await call(app, req(app, "/api/buttons", ip="203.0.113.9", headers=bearer(cfg)))
    assert r.status == 403


async def test_bad_token_401_then_429_lockout(env):
    app, cfg, _, _ = env
    for _ in range(5):
        r = await call(app, req(app, "/api/buttons", headers={"Authorization": "Bearer nope"}))
        assert r.status == 401
    # locked: even the right token is refused
    assert (await call(app, req(app, "/api/buttons", headers=bearer(cfg)))).status == 429
    # other IPs are unaffected; localhost is never throttled
    assert (await call(app, req(app, "/api/buttons", ip="192.168.50.51", headers=bearer(cfg)))).status == 200


async def test_admin_requires_key_and_localhost(env):
    app, cfg, _, _ = env
    h = {"Host": "localhost:8787"}
    assert (await call(app, req(app, "/api/admin/info", ip="127.0.0.1", headers=h))).status == 401
    bad = {**h, "X-Admin-Key": "wrong"}
    assert (await call(app, req(app, "/api/admin/info", ip="127.0.0.1", headers=bad))).status == 401
    good = {**h, "X-Admin-Key": cfg["admin_key"]}
    assert (await call(app, req(app, "/api/admin/info", ip="127.0.0.1", headers=good))).status == 200
    # key alone is not enough from the LAN
    assert (await call(app, req(app, "/api/admin/info", headers=good))).status == 403
    # the HTML page needs no key
    assert (await call(app, req(app, "/admin", ip="127.0.0.1", headers=h))).status == 200


async def test_config_reload_picks_up_cli_changes(env):
    app, cfg, state, home = env
    state["net"] = net("aa:bb:cc:dd:ee:ff")
    assert (await call(app, req(app, "/api/buttons", headers=bearer(cfg)))).status == 403
    # simulate `neondeck trust` from another process
    disk = load_config(home)
    disk["trusted_networks"].append({"mac": "aa:bb:cc:dd:ee:ff", "name": "ufficio"})
    save_config(home, disk)
    c = app["core"]
    c._cfg_mtime = 0  # mtime granularity guard
    app["core"].network._at = None  # force a network re-check
    assert (await call(app, req(app, "/api/buttons", headers=bearer(cfg)))).status == 200


async def test_watch_idles_without_clients(tmp_path):
    cfg = load_config(tmp_path)
    c = server.Core(tmp_path, cfg, NetworkMonitor(lambda: None))
    c.refresh = mock.AsyncMock()
    task = asyncio.create_task(c.watch())
    await asyncio.sleep(0.3)
    c.wake.set()
    await asyncio.sleep(0.1)
    assert c.refresh.await_count == 0  # nobody connected: no refresh, no timers
    c.add_client(mock.Mock(), "127.0.0.1")
    await asyncio.sleep(0.2)
    assert c.refresh.await_count == 1
    ws = next(iter(c.clients))
    c.remove_client(ws)
    n = c.refresh.await_count
    await asyncio.sleep(1.4)
    assert c.refresh.await_count == n  # idle again after the last client leaves
    task.cancel()


async def test_refresh_passes_iterm_sessions_to_tracker(tmp_path):
    """Regression: Context was built positionally and got front_app in iterm_sessions."""
    cfg = load_config(tmp_path)
    c = server.Core(tmp_path, cfg)
    sess = [{"session_id": "S1", "window": 1, "tab": 2, "pane": 1, "panes": 1, "tty": "/dev/ttys004",
             "title": "-zsh", "job": "zsh", "path": "/Users/alice", "current": False}]
    c.iterm.snapshot = mock.AsyncMock(return_value=("/dev/ttys004", frozenset({"/dev/ttys004"}), sess))
    c.front_app = mock.AsyncMock(return_value="iTerm2")
    with mock.patch.object(type(c.iterm), "connected", new_callable=mock.PropertyMock, return_value=True), \
            mock.patch("neondeck.sessions._json_files", return_value=[]):
        await c.refresh(max_age=0)
    ids = [t["id"] for t in c.tracker.sessions]
    assert "iterm:S1" in ids


async def test_front_app_only_when_needed(tmp_path):
    cfg = load_config(tmp_path)
    c = server.Core(tmp_path, cfg)
    with mock.patch.object(server, "frontmost_app", mock.AsyncMock(return_value="iTerm2")) as fa:
        assert await c.front_app() == "iTerm2"
        assert await c.front_app() == "iTerm2"
        assert fa.await_count == 1  # cached 2 s
        c._need_front = False
        assert await c.front_app() is None
        assert fa.await_count == 1


async def test_admin_info_reports_network_reason_and_settings(env):
    app, cfg, _, _ = env
    c = app["core"]
    c.lan.network_reason, c.lan.checked = "localnet_permission", True
    h = {"Host": "localhost:8787", "X-Admin-Key": cfg["admin_key"]}
    r = req(app, "/api/admin/info", ip="127.0.0.1", headers=h)
    resp = await server.admin_info(r)
    data = json.loads(resp.body)
    assert data["network_reason"] == "localnet_permission" and data["network_checked"] is True
    assert data["show_all_iterm"] is True


# ---------------------------------------------------------------- admin: network management

def admin_req(app, cfg, method="GET", ip="127.0.0.1"):
    h = {"Host": "localhost:8787", "X-Admin-Key": cfg["admin_key"]}
    return req(app, "/api/admin/networks", ip=ip, headers=h, method=method)


def with_json(data):
    return mock.patch.object(web.BaseRequest, "json", mock.AsyncMock(return_value=data))


async def with_match(app, cfg, method, mac):
    h = {"Host": "localhost:8787", "X-Admin-Key": cfg["admin_key"]}
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda k, d=None: ("127.0.0.1", 5555) if k == "peername" else d
    return make_mocked_request(method, f"/api/admin/networks/{mac}", headers=h, app=app, transport=transport,
                               match_info={"mac": mac})


async def test_admin_networks_requires_key_and_localhost(env):
    app, cfg, _, _ = env
    bad = req(app, "/api/admin/networks", ip="127.0.0.1", headers={"Host": "localhost:8787"})
    assert (await call(app, bad)).status == 401
    remote = req(app, "/api/admin/networks", ip="192.168.50.50",
                 headers={"Host": "localhost:8787", "X-Admin-Key": cfg["admin_key"]})
    assert (await call(app, remote)).status == 403
    assert (await call(app, admin_req(app, cfg))).status == 200


async def test_admin_networks_state(env):
    app, cfg, _, _ = env
    data = json.loads((await server.admin_networks(admin_req(app, cfg))).body)
    assert data["current"]["mac"] == MAC and data["current"]["trusted"] and data["current"]["name"] == "casa"
    assert data["trusted"][0]["index"] == 1 and data["lan"]["port"] == cfg["port"]


async def test_admin_trust_uses_server_detected_network_and_ignores_body_mac(env):
    app, cfg, state, home = env
    state["net"] = net("aa:bb:cc:dd:ee:ff")
    with with_json({"name": "Ufficio", "mac": "11:22:33:44:55:66"}):
        resp = await server.admin_networks_trust(admin_req(app, cfg, "POST"))
    assert resp.status == 201
    macs = [n["mac"] for n in load_config(home)["trusted_networks"]]
    assert macs == [MAC, "aa:bb:cc:dd:ee:ff"]
    assert json.loads(resp.body)["current"]["name"] == "Ufficio"
    with with_json({}):  # idempotent
        await server.admin_networks_trust(admin_req(app, cfg, "POST"))
    assert len(load_config(home)["trusted_networks"]) == 2


async def test_admin_trust_409_when_network_undetectable(env):
    app, cfg, state, home = env
    state["net"] = None
    with with_json({"name": "x"}):
        resp = await server.admin_networks_trust(admin_req(app, cfg, "POST"))
    assert resp.status == 409 and json.loads(resp.body)["error"]
    assert len(load_config(home)["trusted_networks"]) == 1


async def test_admin_rename_and_delete(env):
    app, cfg, _, home = env
    with with_json({"name": "  Casa mia "}):
        resp = await server.admin_networks_rename(await with_match(app, cfg, "PATCH", MAC.upper()))
    assert resp.status == 200 and load_config(home)["trusted_networks"][0]["name"] == "Casa mia"
    with with_json({"name": ""}):
        assert (await server.admin_networks_rename(await with_match(app, cfg, "PATCH", MAC))).status == 400
    assert (await server.admin_networks_delete(await with_match(app, cfg, "DELETE", "aa:bb:cc:dd:ee:ff"))).status == 404
    resp = await server.admin_networks_delete(await with_match(app, cfg, "DELETE", MAC))
    assert resp.status == 200 and load_config(home)["trusted_networks"] == []
    assert json.loads(resp.body)["current"]["trusted"] is False
