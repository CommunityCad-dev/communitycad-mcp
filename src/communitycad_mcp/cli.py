"""`communitycad-mcp` commands: no arguments runs the server; install, uninstall, doctor set it up."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

import httpx

from . import __version__
from .config import Settings
from .installer import (
    APP_KEYS,
    APP_LABELS,
    Host,
    Launch,
    Result,
    app_key,
    configured_apps,
    install,
    manual_snippet,
    uninstall,
)
from .token_store import TokenStore

UV_URL = "https://docs.astral.sh/uv/getting-started/installation/"
TRY_IT = 'find me an M8 screw with a STEP file'

_MARK = {
    "added": "[added]    ",
    "updated": "[updated]  ",
    "unchanged": "[ok]       ",
    "removed": "[removed]  ",
    "absent": "[none]     ",
    "not_found": "[skipped]  ",
    "error": "[problem]  ",
}


def _line(r: Result, out: TextIO) -> None:
    where = f"  {r.path}" if r.path and r.status not in ("not_found",) else ""
    note = {
        "unchanged": "already set up",
        "not_found": "not found on this computer",
        "absent": "wasn't set up",
    }.get(r.status, "")
    text = ", ".join(x for x in (note, r.detail) if x)
    print(f"  {_MARK[r.status]}{r.app}{where}", file=out)
    if text:
        print(f"              {text}", file=out)


def _apps(arg: list[str] | None) -> list[str] | None:
    if not arg:
        return None
    keys: list[str] = []
    for a in arg:
        for name in a.split(","):
            if name.strip():
                k = app_key(name)
                if k not in keys:
                    keys.append(k)
    return keys


def cmd_install(args: argparse.Namespace, host: Host, out: TextIO) -> int:
    from_path = Path(args.from_path) if args.from_path else None
    if from_path is not None and not (from_path.expanduser() / "pyproject.toml").exists():
        print(f"--from-path {from_path} isn't a checkout of communitycad-mcp (no pyproject.toml).", file=out)
        return 2
    launch = Launch.for_host(host, from_path)
    results = install(host, launch, _apps(args.app), dry_run=args.dry_run)
    title = "Setting up CommunityCAD in your AI apps" + (" (dry run, nothing is changed)" if args.dry_run else "")
    print(title, file=out)
    print(f"  Server command: {' '.join(launch.argv())}", file=out)
    if launch.note:
        print(f"  Note: {launch.note}", file=out)
    print(file=out)
    for r in results:
        _line(r, out)
    ready = [r for r in results if r.status in ("added", "updated", "unchanged")]
    errors = [r for r in results if r.status == "error"]
    print(file=out)
    if not ready and not errors:
        print("No supported AI apps were found. Pick one with --app "
              f"({', '.join(APP_KEYS)}), or add this to your app's MCP settings:", file=out)
        print(manual_snippet(launch), file=out)
        return 1
    if errors:
        print("For the apps marked [problem], add this entry to their MCP settings by hand:", file=out)
        print(manual_snippet(launch), file=out)
        print(file=out)
    if ready and not args.dry_run:
        print("Next:", file=out)
        restarts = []
        for r in ready:
            if r.status != "unchanged" and r.restart and r.restart not in restarts:
                restarts.append(r.restart)
        n = 1
        for r in restarts:
            print(f"  {n}. {r}.", file=out)
            n += 1
        print(f'  {n}. Ask: "{TRY_IT}".', file=out)
        print(f"  {n + 1}. To sign in, ask your AI app to link your CommunityCAD account.", file=out)
        print("  Something not working? Run: communitycad-mcp doctor", file=out)
    return 1 if errors else 0


def cmd_uninstall(args: argparse.Namespace, host: Host, out: TextIO) -> int:
    results = uninstall(host, _apps(args.app), dry_run=args.dry_run)
    print("Removing CommunityCAD from your AI apps" + (" (dry run, nothing is changed)" if args.dry_run else ""), file=out)
    print(file=out)
    for r in results:
        _line(r, out)
    print(file=out)
    if any(r.status == "removed" for r in results) and not args.dry_run:
        print("Restart those apps to finish. Your sign-in is kept; to remove it too, ask your AI app to unlink "
              "your CommunityCAD account before uninstalling.", file=out)
    return 1 if any(r.status == "error" for r in results) else 0


def cmd_doctor(
    args: argparse.Namespace,
    host: Host,
    out: TextIO,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    settings = settings or Settings.from_env(dict(host.env))
    problems = 0

    def ok(what: str) -> None:
        print(f"  [ok]       {what}", file=out)

    def bad(what: str, fix: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  [problem]  {what}", file=out)
        print(f"              Fix: {fix}", file=out)

    def info(what: str) -> None:
        print(f"  [info]     {what}", file=out)

    print(f"CommunityCAD MCP server {__version__}: checking this computer", file=out)
    print(file=out)
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"Python {v.major}.{v.minor} is too old", "use Python 3.10 or newer (uv installs one for you)")
    for prog in ("uv", "uvx"):
        found = host.which(prog)
        if found:
            ok(f"{prog} found: {found}")
        else:
            bad(f"{prog} isn't on your PATH", f"install uv ({UV_URL}), then open a new terminal")

    store = TokenStore(settings.token_path)
    stored = store.load()
    headers = {"User-Agent": f"communitycad-mcp/{__version__}", "Accept": "application/json"}
    if stored is not None:
        headers["Authorization"] = f"Bearer {stored.access_token}"
    url = settings.api_url.rstrip("/") + "/v1/account/status"
    try:
        with httpx.Client(timeout=10.0, transport=transport) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code == 200:
            ok(f"CommunityCAD API reachable ({settings.api_url})")
            try:
                data = resp.json() or {}
            except ValueError:
                data = {}
        else:
            data = None
            bad(f"CommunityCAD API answered {resp.status_code} ({settings.api_url})",
                "try again in a few minutes; if you set COMMUNITYCAD_API_URL, check it")
    except httpx.HTTPError as exc:
        data = None
        bad(f"Can't reach the CommunityCAD API ({settings.api_url}): {type(exc).__name__}",
            "check your internet connection, proxy or firewall; if you set COMMUNITYCAD_API_URL, check it")

    if stored is None:
        info("Not signed in. Searching and downloads within the signed-out limit still work. "
             "To sign in, ask your AI app to link your CommunityCAD account.")
    elif data is not None and not data.get("signed_in"):
        bad("The saved sign-in isn't accepted any more",
            "ask your AI app to link your CommunityCAD account again")
    else:
        who = (data or {}).get("username") or stored.username or "your account"
        ok(f"Signed in as {who}")

    print(file=out)
    print("AI apps:", file=out)
    any_ready = False
    for s in configured_apps(host):
        if not s.detected:
            print(f"  [skipped]  {s.label}: not found on this computer", file=out)
        elif s.problem:
            bad(f"{s.label}: {s.problem}", "fix the file's JSON, then run communitycad-mcp install")
        elif s.entry is None:
            key = next((k for k, lbl in APP_LABELS.items() if s.label.startswith(lbl)), "")
            print(f"  [not set]  {s.label}: CommunityCAD isn't set up here", file=out)
            print(f"              To add it: communitycad-mcp install --app {key}", file=out)
        else:
            command = s.entry.get("command") if isinstance(s.entry, dict) else None
            if command and not Path(command).is_absolute() and not host.which(command):
                bad(f"{s.label}: set up, but its command '{command}' isn't on your PATH",
                    f"install uv ({UV_URL}), or run communitycad-mcp install again")
            elif command and Path(command).is_absolute() and not Path(command).exists():
                bad(f"{s.label}: set up, but {command} doesn't exist any more", "run communitycad-mcp install again")
            else:
                any_ready = True
                ok(f"{s.label}: set up ({s.path})")
    if not any_ready:
        bad("No AI app has CommunityCAD set up yet", "communitycad-mcp install")
    print(file=out)
    if problems:
        print(f"{problems} problem(s) found. After fixing, restart your AI app.", file=out)
    elif any_ready:
        print(f'All good. In your AI app, ask: "{TRY_IT}".', file=out)
    return 1 if problems else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="communitycad-mcp",
        description="CommunityCAD MCP server. With no command it runs the server over stdio, "
        "which is how AI apps start it.",
    )
    p.add_argument("--version", action="version", version=f"communitycad-mcp {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.add_parser("serve", help="run the server over stdio (the default)")
    apps_help = f"only this app (repeat or comma-separate): {', '.join(APP_KEYS)}"
    i = sub.add_parser("install", help="add the server to the AI apps on this computer")
    i.add_argument("--app", action="append", metavar="NAME", help=apps_help + "; set up even if not detected")
    i.add_argument("--dry-run", action="store_true", help="show what would change without writing anything")
    i.add_argument("--from-path", metavar="DIR", help="run a local checkout with `uv run --directory DIR` (for development)")
    u = sub.add_parser("uninstall", help="remove the server from the AI apps (other servers are left alone)")
    u.add_argument("--app", action="append", metavar="NAME", help=apps_help)
    u.add_argument("--dry-run", action="store_true", help="show what would change without writing anything")
    sub.add_parser("doctor", help="check uv, the API connection, your sign-in and which apps are set up")
    return p


def _utf8_output() -> None:
    # Windows consoles may not be UTF-8; never crash on a path with unusual characters.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def run(argv: list[str], host: Host | None = None, out: TextIO | None = None) -> int | None:
    """Run a command. Returns None when the server should start instead."""
    args = parser().parse_args(argv)
    if args.command in (None, "serve"):
        return None
    _utf8_output()
    host = host or Host.current()
    out = out or sys.stdout
    try:
        if args.command == "install":
            return cmd_install(args, host, out)
        if args.command == "uninstall":
            return cmd_uninstall(args, host, out)
        return cmd_doctor(args, host, out)
    except ValueError as exc:  # unknown --app
        print(str(exc), file=out)
        return 2
