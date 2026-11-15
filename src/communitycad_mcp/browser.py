"""Open the sign-in approval page in the user's default browser.

stdout is the MCP channel, and some browsers print to stdout when started
("Opening in existing browser session."), so Python's webbrowser runs in a
child process whose stdin, stdout and stderr all point at the null device.
Opening a browser is a convenience: any failure just means it didn't open.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Mapping

NO_BROWSER_ENV = "COMMUNITYCAD_NO_BROWSER"
# How long to wait for the launcher to report back. A launcher that is still
# running after this (a browser started in the foreground) counts as opened.
LAUNCH_WAIT_S = 3.0

_CHILD = "import sys, webbrowser; sys.exit(0 if webbrowser.open(sys.argv[1]) else 1)"


def can_open(environ: Mapping[str, str] | None = None, platform: str | None = None) -> bool:
    """False when opening a browser would be pointless or unwanted: turned off,
    over SSH, or on Linux/BSD without a graphical session."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if env.get(NO_BROWSER_ENV, "").strip().lower() not in ("", "0", "false", "no"):
        return False
    if env.get("SSH_CONNECTION") or env.get("SSH_CLIENT"):
        return False
    if plat.startswith("win") or plat == "darwin":
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _launch(url: str) -> bool:
    """Run webbrowser.open(url) in a child process with no access to our stdout."""
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", _CHILD, url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return proc.wait(timeout=LAUNCH_WAIT_S) == 0
    except subprocess.TimeoutExpired:
        return True


async def open_url(url: str) -> bool:
    """Try to open `url` in the default browser without blocking the event loop.
    Returns whether it (probably) opened. Never raises."""
    if not can_open():
        return False
    try:
        return await asyncio.to_thread(_launch, url)
    except Exception:  # noqa: BLE001 - a browser that won't open is not an error
        return False
