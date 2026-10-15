from neondeck import cli


def test_wait_network_check_returns_when_checked():
    seq = iter([None, {"network_checked": False}, {"network_checked": True, "network_reason": "localnet_permission"}])
    sleeps = []
    info = cli.wait_network_check({}, 8, lambda cfg, t: next(seq), sleeps.append)
    assert info["network_reason"] == "localnet_permission" and len(sleeps) == 2


def test_wait_network_check_times_out():
    info = cli.wait_network_check({}, 0, lambda cfg, t: None, lambda s: None)
    assert info is None


def test_macos_service_constants():
    from neondeck.platform import macos

    assert macos.LABEL == "io.neondeck.agent" and macos.LOG.name == "neondeck.log"
    assert macos.LEGACY_PLIST.name == f"{macos.LEGACY_LABEL}.plist"
    assert macos.build_plist()["ProgramArguments"][1:] == ["run"]


def test_install_accepts_legacy_launchd_flag():
    args = cli.make_parser().parse_args(["install", "--no-launchd"])
    assert args.no_service
    assert cli.make_parser().parse_args(["install", "--no-service", "--no-vscode"]).no_vscode


def test_unsupported_platform_exits_with_italian_message(monkeypatch, capsys):
    import pytest

    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(SystemExit) as exc:
        cli.main(["status"])
    assert exc.value.code == 1
    assert "Piattaforma non supportata" in capsys.readouterr().err


def pair_cfg(monkeypatch):
    monkeypatch.setattr(cli, "load", lambda: (None, {"port": 8787, "admin_key": "k"}))


def test_pair_none_pending(monkeypatch, capsys):
    pair_cfg(monkeypatch)
    args = cli.make_parser().parse_args(["pair"])
    assert cli.cmd_pair(args, call=lambda cfg, path, method="GET": (200, {"pending": []})) == 0
    assert "Nessuna richiesta di abbinamento in attesa" in capsys.readouterr().out


def test_pair_prompts_approve_deny_skip(monkeypatch, capsys):
    pair_cfg(monkeypatch)
    pend = [{"code": c, "ip": "192.168.7.122", "ua": "iPhone", "expires_in": 87} for c in ("111111", "222222", "333333")]
    calls = []

    def call(cfg, path, method="GET"):
        calls.append((method, path))
        return (200, {"pending": pend}) if method == "GET" else (200, {"ok": True})

    answers = iter(["s", "n", ""])
    args = cli.make_parser().parse_args(["pair"])
    assert cli.cmd_pair(args, call=call, ask=lambda prompt: next(answers)) == 0
    assert calls[1:] == [("POST", "/api/admin/pair/111111/approve"), ("POST", "/api/admin/pair/222222/deny")]
    assert "Codice 111111 da 192.168.7.122 (iPhone), scade tra 87 s" in capsys.readouterr().out


def test_pair_direct_code_and_errors(monkeypatch, capsys):
    pair_cfg(monkeypatch)
    ok = cli.make_parser().parse_args(["pair", "482913"])
    assert cli.cmd_pair(ok, call=lambda cfg, path, method="GET": (200, {"ok": True})) == 0
    assert cli.cmd_pair(ok, call=lambda cfg, path, method="GET": (404, None)) == 1
    assert cli.cmd_pair(ok, call=lambda cfg, path, method="GET": (0, None)) == 1
    plain = cli.make_parser().parse_args(["pair"])
    assert cli.cmd_pair(plain, call=lambda cfg, path, method="GET": (0, None)) == 1
