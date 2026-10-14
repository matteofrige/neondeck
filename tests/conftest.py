import pytest

from neondeck.terminals.base import Backend


class FakeBackend(Backend):
    """In-memory terminal backend for server tests."""

    def __init__(self, name="iterm", label="iTerm2", panes=None, ok=True):
        self.name, self.label = name, label
        self.panes, self.ok = panes or [], ok
        self.focused: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.wanted: list[float] = []

    def available(self):
        return self.ok

    def want(self, ttl):
        self.wanted.append(ttl)

    async def snapshot(self, details=True):
        return [dict(p) for p in self.panes]

    async def focus(self, pane_id):
        self.focused.append(pane_id)

    async def send_text(self, pane_id, text):
        self.sent.append((pane_id, text))

    async def report(self):
        return {"name": self.name, "label": self.label, "available": self.ok, "text": f"{self.label}: test"}


@pytest.fixture
def fake_backend():
    return FakeBackend
