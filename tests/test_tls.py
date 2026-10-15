import asyncio
import datetime as dt
import ipaddress
import json
import os
import plistlib
import socket
import ssl
import stat
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from neondeck import cli, server, tls
from neondeck.config import load_config, save_config
from neondeck.netguard import NetworkMonitor

NAMES = ["neondeck.local"]
IPS = ["192.168.1.5"]


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_ca_constraints_and_permissions(tmp_path):
    cert, _ = tls.ensure_ca(tmp_path)
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca and bc.value.path_length == 0
    nc = cert.extensions.get_extension_for_class(x509.NameConstraints)
    assert nc.critical and nc.value.excluded_subtrees is None
    assert nc.value.permitted_subtrees == tls.PERMITTED
    assert x509.DNSName("local") in nc.value.permitted_subtrees
    assert x509.IPAddress(ipaddress.ip_network("192.168.0.0/16")) in nc.value.permitted_subtrees
    ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    assert ku.key_cert_sign and ku.crl_sign
    assert mode(tmp_path / "tls") == 0o700
    assert mode(tmp_path / "tls" / "ca.key") == 0o600
    # a second call loads the same CA
    assert tls.ensure_ca(tmp_path)[0] == cert


def test_leaf_san_signature_eku_validity(tmp_path):
    ca, _ = tls.ensure_ca(tmp_path)
    assert tls.ensure_leaf(tmp_path, NAMES + ["mac.local"], IPS) is True
    pem = (tmp_path / "tls" / "server.pem").read_bytes()
    leaf, chain_ca = x509.load_pem_x509_certificates(pem)
    assert chain_ca == ca
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["neondeck.local", "mac.local"]
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address("192.168.1.5")]
    leaf.verify_directly_issued_by(ca)
    eku = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
    assert leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc <= dt.timedelta(days=398)
    assert mode(tmp_path / "tls" / "server.key") == 0o600


def test_ensure_leaf_idempotent_and_regenerates(tmp_path):
    assert tls.ensure_leaf(tmp_path, NAMES, IPS) is True
    assert tls.ensure_leaf(tmp_path, NAMES, IPS) is False
    assert tls.ensure_leaf(tmp_path, NAMES, ["192.168.1.9"]) is True
    assert tls.ensure_leaf(tmp_path, NAMES, ["192.168.1.9"]) is False
    tls.rotate_ca(tmp_path)
    assert not (tmp_path / "tls" / "server.pem").exists()
    assert tls.ensure_leaf(tmp_path, NAMES, ["192.168.1.9"]) is True


def test_leaf_regenerated_when_signed_by_old_ca(tmp_path):
    tls.ensure_leaf(tmp_path, NAMES, IPS)
    old = (tmp_path / "tls" / "server.pem").read_bytes()
    for n in ("ca.key", "ca.pem"):  # new CA, stale leaf left behind
        (tmp_path / "tls" / n).unlink()
    tls.ensure_ca(tmp_path)
    assert tls.ensure_leaf(tmp_path, NAMES, IPS) is True
    assert (tmp_path / "tls" / "server.pem").read_bytes() != old


def test_server_context_and_status(tmp_path):
    assert tls.status(tmp_path)["ca"] is False and tls.ca_fingerprint(tmp_path) is None
    ctx = tls.server_context(tmp_path, NAMES, IPS)
    assert isinstance(ctx, ssl.SSLContext) and ctx.minimum_version == ssl.TLSVersion.TLSv1_2
    st = tls.status(tmp_path)
    assert st["ca"] and st["leaf_names"] == ["192.168.1.5", "neondeck.local"]
    assert len(st["ca_fingerprint"].split(":")) == 32
    assert tls.default_names({"lan_hostname": "neondeck"})[0] == "neondeck.local"


async def test_real_handshake(tmp_path):
    ctx = tls.server_context(tmp_path, NAMES, ["127.0.0.1"])
    done = asyncio.Event()

    async def handle(reader, writer):
        writer.write(b"ok")
        await writer.drain()
        writer.close()
        done.set()

    try:
        srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    except OSError as exc:
        pytest.skip(f"bind non consentito: {exc}")
    port = srv.sockets[0].getsockname()[1]
    try:
        client = ssl.create_default_context(cafile=str(tmp_path / "tls" / "ca.pem"))
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client)
        assert await reader.read(2) == b"ok"
        writer.close()
    finally:
        srv.close()
        await srv.wait_closed()


def test_mobileconfig(tmp_path):
    data = tls.mobileconfig(tmp_path, "mac")
    prof = plistlib.loads(data)
    der = x509.load_pem_x509_certificate(tls.ca_pem(tmp_path)).public_bytes(serialization.Encoding.DER)
    inner = prof["PayloadContent"][0]
    assert prof["PayloadType"] == "Configuration" and prof["PayloadDisplayName"] == "Neondeck (mac)"
    assert inner["PayloadType"] == "com.apple.security.root" and inner["PayloadContent"] == der
    assert prof["PayloadIdentifier"].startswith("io.neondeck.ca.")
    assert tls.mobileconfig(tmp_path, "mac") == data  # stable UUIDs


# ---------------------------------------------------------------- setup app

@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    cfg = load_config(tmp_path)
    app = server.make_app(tmp_path, cfg, NetworkMonitor(lambda: None))
    page = tmp_path / "static"
    page.mkdir()
    (page / "setup.html").write_text("<a href='__HTTPS_URL__'>__CA_FP__</a>")
    monkeypatch.setattr(server, "STATIC", page)
    return app, server.make_setup_app(app["core"]), cfg


async def fetch(setup, path, host="neondeck.local:8786", method="GET"):
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda k, d=None: ("127.0.0.1", 5555) if k == "peername" else d
    r = make_mocked_request(method, path, headers={"Host": host}, app=setup, transport=transport)
    try:
        return await setup._handle(r)
    except web.HTTPException as exc:
        return exc


async def test_setup_page_replaces_placeholders(setup_env):
    _, setup, _ = setup_env
    resp = await fetch(setup, "/setup")
    fp = tls.ca_fingerprint(setup["core"].home)  # created lazily by the page? not necessarily
    assert resp.status == 200 and resp.headers["Cache-Control"] == "no-store"
    assert "https://neondeck.local:8787/preview" in resp.text
    assert "__HTTPS_URL__" not in resp.text and "__CA_FP__" not in resp.text
    assert fp is None or fp in resp.text


async def test_setup_page_missing_file_is_500(setup_env, monkeypatch, tmp_path):
    _, setup, _ = setup_env
    monkeypatch.setattr(server, "STATIC", tmp_path / "nowhere")
    assert (await fetch(setup, "/setup")).status == 500


async def test_setup_downloads_and_redirects(setup_env):
    _, setup, _ = setup_env
    resp = await fetch(setup, "/neondeck-ca.mobileconfig")
    assert resp.content_type == "application/x-apple-aspen-config"
    assert 'filename="neondeck-ca.mobileconfig"' in resp.headers["Content-Disposition"]
    assert plistlib.loads(resp.body)["PayloadType"] == "Configuration"
    resp = await fetch(setup, "/neondeck-ca.pem")
    assert resp.content_type == "application/x-pem-file" and resp.body.startswith(b"-----BEGIN CERTIFICATE")
    assert "neondeck-ca.crt" in resp.headers["Content-Disposition"]
    resp = await fetch(setup, "/preview?x=1")
    assert resp.status == 308 and str(resp.location) == "https://neondeck.local:8787/preview?x=1"
    resp = await fetch(setup, "/")
    assert resp.status == 302 and str(resp.location) == "/setup"


async def test_admin_info_tls_default_off(setup_env):
    app, _, cfg = setup_env
    h = {"Host": "localhost:8787", "X-Admin-Key": cfg["admin_key"]}
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda k, d=None: ("127.0.0.1", 5555) if k == "peername" else d
    r = make_mocked_request("GET", "/api/admin/info", headers=h, app=app, transport=transport)
    data = json.loads((await server.admin_info(r)).body)
    assert data["tls"]["enabled"] is False
    assert data["tls"]["https_url"] == "https://neondeck.local:8787/preview"
    assert data["tls"]["setup_url"] == "http://neondeck.local:8786/setup"


# ---------------------------------------------------------------- cli

def tls_cli(monkeypatch, tmp_path):
    cfg = load_config(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: (tmp_path, cfg))
    return cfg


def test_cli_tls_enable_disable(monkeypatch, tmp_path, capsys):
    tls_cli(monkeypatch, tmp_path)
    restarts = []
    parse = cli.make_parser().parse_args
    assert cli.cmd_tls(parse(["tls", "enable"]), restart=lambda: restarts.append(1) or 0) == 0
    out = capsys.readouterr().out
    assert load_config(tmp_path)["tls"] is True and "Safari" in out and tls.ca_fingerprint(tmp_path) in out
    assert cli.cmd_tls(parse(["tls", "disable"]), restart=lambda: restarts.append(1) or 0) == 0
    assert load_config(tmp_path)["tls"] is False and len(restarts) == 2
    assert (tmp_path / "tls" / "ca.pem").exists()
    assert cli.cmd_tls(parse(["tls"])) == 0
    assert "disattivato" in capsys.readouterr().out


def test_cli_tls_rotate_respects_no(monkeypatch, tmp_path):
    tls_cli(monkeypatch, tmp_path)
    tls.ensure_ca(tmp_path)
    before = tls.ca_fingerprint(tmp_path)
    args = cli.make_parser().parse_args(["tls", "rotate"])
    restarts = []
    assert cli.cmd_tls(args, ask=lambda p: "", restart=lambda: restarts.append(1) or 0) == 1
    assert tls.ca_fingerprint(tmp_path) == before and not restarts
    assert cli.cmd_tls(args, ask=lambda p: "s", restart=lambda: restarts.append(1) or 0) == 0
    assert tls.ca_fingerprint(tmp_path) != before
