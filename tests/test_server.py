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

from conftest import FakeBackend

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
    app["core"].notify = lambda title, text: None  # no real desktop notifications from tests
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


async def test_refresh_merges_every_backend_into_tiles(tmp_path):
    """Regression: Context was built positionally and got front_app in the session list."""
    cfg = load_config(tmp_path)
    pane = {"backend": "iterm", "pane_id": "S1", "window": 1, "tab": 2, "pane": 1, "panes": 1,
            "tty": "/dev/ttys004", "title": "-zsh", "job": "zsh", "cwd": "/Users/alice", "active": True,
            "apps": ["iTerm2"], "loc": "F1 · T2"}
    tmux = dict(pane, backend="tmux", pane_id="%1", tty="/dev/pts/1", apps=["alacritty"], loc="tmux main:1.1",
                active=False)
    c = server.Core(tmp_path, cfg, backends=[FakeBackend("iterm", "iTerm2", [pane]),
                                             FakeBackend("tmux", "tmux", [tmux]),
                                             FakeBackend("wezterm", "WezTerm", ok=False)])
    c.front_app = mock.AsyncMock(return_value="iTerm2")
    with mock.patch("neondeck.sessions._json_files", return_value=[]):
        await c.refresh(max_age=0)
    tiles = {t["id"]: t for t in c.tracker.sessions}
    assert set(tiles) == {"iterm:S1", "tmux:%1"} and tiles["iterm:S1"]["focused"]
    assert [t["id"] for t in c.state()["terminals"]] == ["iterm", "tmux"]
    # show_all_terminals off: no terminal tiles
    cfg["show_all_terminals"] = False
    c._sig = None
    with mock.patch("neondeck.sessions._json_files", return_value=[]):
        await c.refresh(max_age=0)
    assert c.tracker.sessions == []


async def test_backends_are_asked_to_stay_connected_while_clients_are_present(tmp_path):
    cfg = load_config(tmp_path)
    fb = FakeBackend("iterm")
    c = server.Core(tmp_path, cfg, backends=[fb])
    c.add_client(mock.Mock(), "127.0.0.1")
    c.remove_client(next(iter(c.clients)))
    assert fb.wanted[0] == float("inf") and fb.wanted[-1] == 60


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
    assert data["show_all_terminals"] is True
    assert any(b["name"] == "vscode" for b in data["backends"])


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


# ---- manifest token / start_url

async def manifest_json(app, query="", ip="127.0.0.1"):
    r = await server.manifest(req(app, "/manifest.webmanifest" + query, ip=ip))
    assert r.headers["Cache-Control"] == "no-store"
    return json.loads(r.text)


async def test_manifest_without_token_is_plain(env):
    app, _, _, _ = env
    data = await manifest_json(app)
    assert data["start_url"] == "/preview" and data["id"] == "/preview"


async def test_manifest_with_valid_token_carries_it(env):
    app, cfg, _, _ = env
    data = await manifest_json(app, "?token=" + cfg["token"])
    assert data["start_url"].startswith("/preview?token=") and cfg["token"] in data["start_url"]
    assert data["id"] == "/preview"


async def test_manifest_wrong_token_local_is_plain(env):
    app, _, _, _ = env
    assert (await manifest_json(app, "?token=nope"))["start_url"] == "/preview"


async def test_manifest_wrong_token_remote_records_failure_and_locks(env):
    app, cfg, _, _ = env
    ip = "192.168.50.50"
    for _ in range(10):
        assert (await manifest_json(app, "?token=nope", ip=ip))["start_url"] == "/preview"
    assert server.core(req(app, "/")).throttle.is_locked(ip)
    # locked: even the right token no longer yields the token-carrying start_url
    assert (await manifest_json(app, "?token=" + cfg["token"], ip=ip))["start_url"] == "/preview"


# ---- QR scan from photo

async def test_vendor_jsqr_served_public(env):
    app, _, _, _ = env
    r = await server.vendor_jsqr(req(app, "/vendor/jsQR.js", ip="192.168.50.50"))
    assert r.headers["Content-Type"].startswith("text/javascript")
    assert "max-age=604800" in r.headers["Cache-Control"]
    assert "jsQR" in (server.STATIC / "vendor" / "jsQR.js").read_text()


def test_preview_has_scan_button():
    html = (server.STATIC / "preview.html").read_text()
    assert 'id="scanBtn"' in html and 'capture="environment"' in html


# ---------------------------------------------------------------- pairing

def preq(app, path, ip="192.168.50.50", method="GET", admin_key=None, **match):
    h = {"Host": "localhost:8787"}
    if admin_key:
        h["X-Admin-Key"] = admin_key
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda k, d=None: (ip, 5555) if k == "peername" else d
    return make_mocked_request(method, path, headers=h, app=app, transport=transport, match_info=match)


async def pcall(app, r, handler):
    return await server.security(r, handler)


async def create_pair(app):
    r = await pcall(app, preq(app, "/api/pair", method="POST"), server.api_pair)
    assert r.status == 200
    return json.loads(r.body)


async def test_pair_create_from_trusted_lan_without_token(env):
    app, cfg, _, _ = env
    data = await create_pair(app)
    assert data["ok"] and len(data["code"]) == 6 and data["expires_in"] == 120 and data["id"]


async def test_pair_from_untrusted_network_403(env):
    app, cfg, state, _ = env
    state["net"] = net("aa:bb:cc:dd:ee:ff")
    assert (await pcall(app, preq(app, "/api/pair", method="POST"), server.api_pair)).status == 403
    assert (await pcall(app, preq(app, "/api/pair/xyz", rid="xyz"), server.api_pair_status)).status == 403


async def test_pair_rate_limit(env):
    app, cfg, _, _ = env
    for _ in range(5):
        await create_pair(app)
    r = await pcall(app, preq(app, "/api/pair", method="POST"), server.api_pair)
    assert r.status == 429 and "Troppe richieste" in json.loads(r.body)["error"]


async def test_pair_flow_pending_approve_token_once(env):
    app, cfg, _, _ = env
    d = await create_pair(app)
    poll = lambda: pcall(app, preq(app, f"/api/pair/{d['id']}", rid=d["id"]), server.api_pair_status)
    r = await poll()
    assert r.status == 200 and json.loads(r.body) == {"ok": True, "state": "pending"}

    key = cfg["admin_key"]
    lst = await pcall(app, preq(app, "/api/admin/pair", ip="127.0.0.1", admin_key=key), server.admin_pair_list)
    pending = json.loads(lst.body)["pending"]
    assert [q["code"] for q in pending] == [d["code"]] and d["id"] not in lst.text
    info = await pcall(app, preq(app, "/api/admin/info", ip="127.0.0.1", admin_key=key), server.admin_info)
    # admin_info can be slow on CI (network probing): age/expires_in may drift, compare the stable fields
    stable = lambda qs: [(q["code"], q["ip"], q["ua"]) for q in qs]
    assert stable(json.loads(info.body)["pairing"]) == stable(pending)

    ok = await pcall(app, preq(app, f"/api/admin/pair/{d['code']}/approve", ip="127.0.0.1", method="POST",
                               admin_key=key, code=d["code"]), server.admin_pair_approve)
    assert ok.status == 200
    got = json.loads((await poll()).body)
    assert got == {"ok": True, "state": "approved", "token": cfg["token"]}
    again = await poll()
    assert again.status == 404 and json.loads(again.body)["state"] == "expired"


async def test_pair_deny_and_unknown_code(env):
    app, cfg, _, _ = env
    d = await create_pair(app)
    key = cfg["admin_key"]

    def adm(action, code):
        return pcall(app, preq(app, f"/api/admin/pair/{code}/{action}", ip="127.0.0.1", method="POST",
                               admin_key=key, code=code),
                     server.admin_pair_deny if action == "deny" else server.admin_pair_approve)

    assert (await adm("approve", "000000")).status == 404
    assert (await adm("deny", d["code"])).status == 200
    r = await pcall(app, preq(app, f"/api/pair/{d['id']}", rid=d["id"]), server.api_pair_status)
    assert json.loads(r.body) == {"ok": True, "state": "denied"}


async def test_pair_admin_endpoints_need_key_and_localhost(env):
    app, cfg, _, _ = env
    d = await create_pair(app)
    path = f"/api/admin/pair/{d['code']}/approve"
    nokey = preq(app, path, ip="127.0.0.1", method="POST", code=d["code"])
    assert (await pcall(app, nokey, server.admin_pair_approve)).status == 401
    remote = preq(app, path, method="POST", admin_key=cfg["admin_key"], code=d["code"])
    assert (await pcall(app, remote, server.admin_pair_approve)).status == 403
    assert (await pcall(app, preq(app, "/api/admin/pair", ip="127.0.0.1"), server.admin_pair_list)).status == 401


async def test_pair_unknown_id_404_and_counts_as_failure(env):
    app, cfg, _, _ = env
    r = await pcall(app, preq(app, "/api/pair/nope", rid="nope"), server.api_pair_status)
    assert r.status == 404 and json.loads(r.body) == {"ok": False, "state": "expired"}
    for _ in range(20):
        await pcall(app, preq(app, "/api/pair/nope", rid="nope"), server.api_pair_status)
    assert app["core"].throttle.is_locked("192.168.50.50")
