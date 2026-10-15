from neondeck.pairing import MAX_PENDING, TTL, Pairing


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make():
    clk = Clock()
    return Pairing(clk), clk


def test_create_and_pending():
    p, _ = make()
    rid, code = p.create("10.0.0.2", "iPhone")
    assert len(code) == 6 and code.isdigit()
    assert p.status(rid) == {"state": "pending"}
    [q] = p.pending()
    assert q["code"] == code and q["ip"] == "10.0.0.2" and q["expires_in"] == int(TTL)


def test_pending_never_exposes_rid():
    p, _ = make()
    rid, _ = p.create("10.0.0.2", "ua")
    assert rid not in repr(p.pending()) and all("id" not in q and "rid" not in q for q in p.pending())


def test_approve_delivered_once():
    p, _ = make()
    rid, code = p.create("10.0.0.2", "ua")
    assert p.approve(code) is True
    assert p.pending() == []
    assert p.status(rid) == {"state": "approved"}
    assert p.status(rid) is None  # removed after delivery
    assert p.approve(code) is False


def test_deny():
    p, _ = make()
    rid, code = p.create("10.0.0.2", "ua")
    assert p.deny(code) and p.status(rid) == {"state": "denied"} and p.status(rid) is None


def test_unknown_code():
    p, _ = make()
    assert not p.approve("000000") and not p.deny("000000") and p.status("nope") is None


def test_expiry():
    p, clk = make()
    rid, code = p.create("10.0.0.2", "ua")
    clk.t += TTL - 1
    assert p.status(rid) == {"state": "pending"}
    clk.t += 1
    assert p.status(rid) is None and p.pending() == [] and not p.approve(code)


def test_expired_approved_is_gone():
    p, clk = make()
    rid, code = p.create("10.0.0.2", "ua")
    p.approve(code)
    clk.t += TTL
    assert p.status(rid) is None


def test_one_per_ip_replaces_old():
    p, _ = make()
    old, _ = p.create("10.0.0.2", "ua")
    new, code = p.create("10.0.0.2", "ua")
    assert p.status(old) is None and p.status(new) == {"state": "pending"}
    assert [q["code"] for q in p.pending()] == [code]


def test_max_pending_evicts_oldest():
    p, clk = make()
    rids = []
    for n in range(MAX_PENDING + 1):
        clk.t += 1
        rids.append(p.create(f"10.0.0.{n}", "ua")[0])
    assert len(p.pending()) == MAX_PENDING
    assert p.status(rids[0]) is None and p.status(rids[-1]) == {"state": "pending"}


def test_codes_unique_among_pending(monkeypatch):
    p, _ = make()
    seq = iter([7, 7, 7, 8])
    monkeypatch.setattr("neondeck.pairing.secrets.randbelow", lambda n: next(seq))
    _, a = p.create("10.0.0.1", "ua")
    _, b = p.create("10.0.0.2", "ua")
    assert a == "000007" and b == "000008"


def test_ua_truncated():
    p, _ = make()
    p.create("10.0.0.1", "x" * 500)
    assert len(p.pending()[0]["ua"]) <= 60
