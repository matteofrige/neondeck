"""Short-lived pairing requests: a phone shows a code, the user approves it on the computer."""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable

TTL = 120.0  # seconds a request stays valid
MAX_PENDING = 5


class Pairing:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reqs: dict[str, dict] = {}  # rid -> request, insertion order = age

    def _prune(self) -> None:
        now = self._clock()
        for rid in [r for r, q in self._reqs.items() if now >= q["expires"]]:
            del self._reqs[rid]

    def _pending(self) -> list[tuple[str, dict]]:
        return [(r, q) for r, q in self._reqs.items() if q["state"] == "pending"]

    def create(self, ip: str, ua: str) -> tuple[str, str]:
        self._prune()
        for rid, q in self._pending():
            if q["ip"] == ip:  # one per IP: the new request replaces the old one
                del self._reqs[rid]
        pend = self._pending()
        while len(pend) >= MAX_PENDING:
            del self._reqs[pend.pop(0)[0]]
        used = {q["code"] for _, q in pend}
        while True:
            code = f"{secrets.randbelow(1_000_000):06d}"
            if code not in used:
                break
        rid = secrets.token_urlsafe(32)
        now = self._clock()
        self._reqs[rid] = {"code": code, "ip": ip, "ua": ua[:60], "created": now,
                           "expires": now + TTL, "state": "pending"}
        return rid, code

    def status(self, rid: str) -> dict | None:
        """State of a request; None when unknown or expired. A final state is delivered once."""
        self._prune()
        q = self._reqs.get(rid)
        if q is None:
            return None
        if q["state"] != "pending":
            del self._reqs[rid]
        return {"state": q["state"]}

    def pending(self) -> list[dict]:
        self._prune()
        now = self._clock()
        return [{"code": q["code"], "ip": q["ip"], "ua": q["ua"], "age": int(now - q["created"]),
                 "expires_in": max(0, int(q["expires"] - now))} for _, q in self._pending()]

    def _resolve(self, code: str, state: str) -> bool:
        self._prune()
        for _, q in self._pending():
            if q["code"] == code:
                q["state"] = state
                return True
        return False

    def approve(self, code: str) -> bool:
        return self._resolve(code, "approved")

    def deny(self, code: str) -> bool:
        return self._resolve(code, "denied")
