"""Runs against a private tmux server (own socket, no user config): skipped when tmux is not installed."""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

from neondeck.errors import ControlError
from neondeck.terminals.tmux import TmuxBackend

pytestmark = [pytest.mark.tmux, pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")]


def tmux_cmd(sock, *args):
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["LC_ALL"] = "C.UTF-8" if sys.platform.startswith("linux") else "en_US.UTF-8"  # tmux decides UTF-8 from the locale
    return subprocess.run(["tmux", "-S", sock, "-f", "/dev/null", *args], capture_output=True, text=True,
                          env=env, timeout=10)


@pytest.fixture
def server():
    base = tempfile.mkdtemp(prefix="ndt")
    if len(base) > 60:  # unix socket paths are short (104 bytes on macOS)
        shutil.rmtree(base, ignore_errors=True)
        base = tempfile.mkdtemp(prefix="ndt", dir="/tmp")
    sock = os.path.join(base, f"s{os.getpid()}")
    res = tmux_cmd(sock, "new-session", "-d", "-s", "one", "-x", "120", "-y", "30", "cat")
    if res.returncode != 0 or tmux_cmd(sock, "list-sessions").returncode != 0:  # e.g. a sandbox denies the socket
        shutil.rmtree(base, ignore_errors=True)
        pytest.skip(f"cannot start a private tmux server here: {(res.stdout + res.stderr).strip()}")
    try:
        assert tmux_cmd(sock, "new-session", "-d", "-s", "two", "cat").returncode == 0
        assert tmux_cmd(sock, "split-window", "-t", "two", "cat").returncode == 0
        yield sock
    finally:
        tmux_cmd(sock, "kill-server")
        shutil.rmtree(base, ignore_errors=True)


async def wait_for(sock, pane, needle, timeout=4.0):
    end = time.monotonic() + timeout
    text = ""
    while time.monotonic() < end:
        text = tmux_cmd(sock, "capture-pane", "-p", "-t", pane).stdout
        if needle in text:
            return text
        await asyncio.sleep(0.1)
    return text


async def test_snapshot_focus_and_send_text_on_a_private_server(server):
    b = TmuxBackend(socket=server)
    assert b.available()
    panes = await b.snapshot()
    assert len(panes) == 3 and {p["session"] for p in panes} == {"one", "two"}
    assert all(p["job"] and p["tty"] and p["backend"] == "tmux" for p in panes)  # job may still be the shell at first
    assert not any(p["active"] for p in panes)  # no client attached
    assert sorted(p["loc"] for p in panes) == ["tmux one:0.0", "tmux two:0.0", "tmux two:0.1"]

    # focus: with no client the error is reported, but the pane is selected anyway
    target = next(p for p in panes if p["loc"] == "tmux two:0.0")
    assert tmux_cmd(server, "select-pane", "-t", next(p for p in panes if p["loc"] == "tmux two:0.1")["pane_id"]).returncode == 0
    with pytest.raises(ControlError, match="Nessun client tmux collegato"):
        await b.focus(target["pane_id"])
    active = tmux_cmd(server, "display-message", "-p", "-t", "two", "#{pane_id}").stdout.strip()
    assert active == target["pane_id"]

    # send_text: raw bytes through send-keys -H, including multi-byte UTF-8
    await b.send_text(target["pane_id"], "ciao\n")
    await b.send_text(target["pane_id"], "è\n")
    text = await wait_for(server, target["pane_id"], "è")
    assert "ciao" in text and "è" in text

    rep = await b.report()
    assert rep["available"] and rep["text"] == "tmux: attivo (2 sessioni, 3 pannelli)"


async def test_unavailable_when_there_is_no_server(tmp_path):
    b = TmuxBackend(socket=str(tmp_path / "nothing"))
    assert not b.available() and await b.snapshot() == []
