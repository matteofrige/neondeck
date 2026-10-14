from neondeck import cli


def test_wait_network_check_returns_when_checked():
    seq = iter([None, {"network_checked": False}, {"network_checked": True, "network_reason": "localnet_permission"}])
    sleeps = []
    info = cli.wait_network_check({}, 8, lambda cfg, t: next(seq), sleeps.append)
    assert info["network_reason"] == "localnet_permission" and len(sleeps) == 2


def test_wait_network_check_times_out():
    info = cli.wait_network_check({}, 0, lambda cfg, t: None, lambda s: None)
    assert info is None


def test_legacy_constants_and_new_label():
    assert cli.LABEL == "io.neondeck.agent" and cli.LOG.name == "neondeck.log"
    assert cli.LEGACY_PLIST.name == f"{cli.LEGACY_LABEL}.plist"
