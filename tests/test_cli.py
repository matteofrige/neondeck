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
