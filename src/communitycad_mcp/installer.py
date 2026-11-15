"""Add the server to the AI apps on this computer, or take it out again.

Each app keeps its MCP servers in a JSON file (Claude Code can also be told
through its own `claude mcp` command). Only the "communitycad" entry is ever
changed: other servers and settings are kept as they are, a timestamped backup
is written next to any file before it changes, and files are written as UTF-8
without a BOM.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SERVER_NAME = "communitycad"
PACKAGE = "communitycad-mcp"


class ConfigError(Exception):
    """A config file we can't safely edit (not JSON, or not the shape we expect)."""


@dataclass
class Host:
    """The computer we're running on: its OS, folders and programs. Swapped out in tests."""

    system: str  # "Windows", "Darwin" or "Linux"
    home: Path
    env: Mapping[str, str] = field(default_factory=dict)
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str]], subprocess.CompletedProcess] | None = None
    now: Callable[[], datetime] = datetime.now
    python: str = sys.executable

    @classmethod
    def current(cls) -> Host:
        import platform

        return cls(system=platform.system(), home=Path.home(), env=dict(os.environ))

    def appdata(self) -> Path:
        return Path(self.env["APPDATA"]) if self.env.get("APPDATA") else self.home / "AppData" / "Roaming"

    def localappdata(self) -> Path:
        return Path(self.env["LOCALAPPDATA"]) if self.env.get("LOCALAPPDATA") else self.home / "AppData" / "Local"

    def user_config(self) -> Path:
        """Where desktop apps keep their settings: %APPDATA%, ~/Library/Application Support, ~/.config."""
        if self.system == "Windows":
            return self.appdata()
        if self.system == "Darwin":
            return self.home / "Library" / "Application Support"
        return Path(self.env["XDG_CONFIG_HOME"]) if self.env.get("XDG_CONFIG_HOME") else self.home / ".config"

    def run_command(self, args: list[str]) -> subprocess.CompletedProcess:
        if self.run is not None:
            return self.run(args)
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)


@dataclass(frozen=True)
class Launch:
    """How an app should start the server."""

    command: str
    args: tuple[str, ...]
    note: str | None = None

    @classmethod
    def for_host(cls, host: Host, from_path: Path | None = None) -> Launch:
        if from_path is not None:
            return cls("uv", ("run", "--directory", str(from_path.expanduser().resolve()), PACKAGE))
        if host.which("uvx"):
            return cls("uvx", (PACKAGE,))
        # Installed with pip and no uv: start it with this Python instead.
        return cls(
            host.python,
            ("-m", "communitycad_mcp"),
            note="uv isn't installed, so the apps start the server with this Python. "
            "Installing uv (https://docs.astral.sh/uv/) keeps it up to date automatically.",
        )

    def absolute(self, host: Host) -> Launch:
        """The same launch with uv/uvx as a full path, for apps that don't see the shell's PATH."""
        if self.command in ("uv", "uvx"):
            found = host.which(self.command)
            if found:
                return Launch(found, self.args, self.note)
        return self

    def argv(self) -> list[str]:
        return [self.command, *self.args]


@dataclass
class Result:
    app: str
    status: str  # added, updated, unchanged, removed, absent, not_found, error
    path: Path | None = None
    detail: str = ""
    restart: str = ""


# -- JSON config files ----------------------------------------------------------


def read_config(path: Path) -> dict[str, Any] | None:
    """The file's JSON object, {} for an empty file, None when it doesn't exist."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        text = raw.decode("utf-8-sig")  # tolerate a BOM some editors add
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} isn't UTF-8 text") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{path} isn't plain JSON (it may have comments or a typo): {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} doesn't hold a JSON object")
    return data


def backup_path(path: Path, host: Host) -> Path:
    stamp = host.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return candidate


def write_config(path: Path, data: dict[str, Any], host: Host) -> Path | None:
    """Write the JSON (UTF-8, no BOM) after backing up the old file. Returns the backup."""
    backup = None
    if path.exists():
        backup = backup_path(path, host)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    os.replace(tmp, path)
    return backup


# -- apps -----------------------------------------------------------------------


@dataclass(frozen=True)
class FileApp:
    """An app configured by one JSON file with a map of servers."""

    key: str
    label: str
    servers_key: str
    restart: str
    # The folders whose presence means the app is installed, and the file in each.
    locate: Callable[[Host], list[Path]]
    detect: Callable[[Host], list[Path]]
    stdio_type: bool = False  # VS Code wants "type": "stdio"
    absolute_command: bool = False  # GUI apps that may not see the shell's PATH

    def entry(self, launch: Launch) -> dict[str, Any]:
        e: dict[str, Any] = {"type": "stdio"} if self.stdio_type else {}
        e.update(command=launch.command, args=list(launch.args))
        return e


def _claude_desktop_files(host: Host) -> list[Path]:
    files = [host.user_config() / "Claude" / "claude_desktop_config.json"]
    if host.system == "Windows":
        # The Microsoft Store build keeps its settings in a per-package folder.
        packages = host.localappdata() / "Packages"
        if packages.is_dir():
            for pkg in sorted(packages.glob("Claude_*")):
                files.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    return files


def _claude_desktop_detect(host: Host) -> list[Path]:
    return [p for p in _claude_desktop_files(host) if p.parent.is_dir()]


def _existing_parent(paths: Callable[[Host], list[Path]]) -> Callable[[Host], list[Path]]:
    return lambda host: [p for p in paths(host) if p.parent.is_dir()]


_cursor = lambda host: [host.home / ".cursor" / "mcp.json"]  # noqa: E731
_vscode = lambda host: [host.user_config() / "Code" / "User" / "mcp.json"]  # noqa: E731
_windsurf = lambda host: [host.home / ".codeium" / "windsurf" / "mcp_config.json"]  # noqa: E731

FILE_APPS: dict[str, FileApp] = {
    "claude-desktop": FileApp(
        "claude-desktop", "Claude Desktop", "mcpServers", "Quit Claude Desktop fully (also from the tray or menu bar) and open it again",
        _claude_desktop_files, _claude_desktop_detect, absolute_command=True,
    ),
    "cursor": FileApp("cursor", "Cursor", "mcpServers", "Restart Cursor", _cursor, _existing_parent(_cursor)),
    "vscode": FileApp(
        "vscode", "VS Code", "servers", "Reload VS Code, then turn on the CommunityCAD server in Copilot Chat's tools list",
        _vscode, _existing_parent(_vscode), stdio_type=True,
    ),
    "windsurf": FileApp("windsurf", "Windsurf", "mcpServers", "Restart Windsurf", _windsurf, _existing_parent(_windsurf)),
}

CLAUDE_CODE = "claude-code"
APP_KEYS = [CLAUDE_CODE, *FILE_APPS]
APP_LABELS = {CLAUDE_CODE: "Claude Code", **{k: a.label for k, a in FILE_APPS.items()}}
ALIASES = {
    "claude": CLAUDE_CODE, "claudecode": CLAUDE_CODE, "claude-code": CLAUDE_CODE, "code": "vscode",
    "desktop": "claude-desktop", "claude-desktop": "claude-desktop", "claudedesktop": "claude-desktop",
    "vs-code": "vscode", "vscode": "vscode", "cursor": "cursor", "windsurf": "windsurf",
}


def app_key(name: str) -> str:
    key = ALIASES.get(name.strip().lower().replace(" ", "-").replace("_", "-"))
    if key is None:
        raise ValueError(f"Unknown app {name!r}. Choose one of: {', '.join(APP_KEYS)}.")
    return key


def _same(a: Any, b: Any) -> bool:
    return isinstance(a, dict) and {k: a.get(k) for k in b} == b


def _file_install(app: FileApp, path: Path, entry: dict[str, Any], host: Host, dry_run: bool, label: str) -> Result:
    try:
        data = read_config(path)
    except ConfigError as exc:
        return Result(label, "error", path, f"{exc}. Add the entry by hand (see below).", app.restart)
    data = {} if data is None else data
    servers = data.get(app.servers_key)
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        return Result(label, "error", path, f'"{app.servers_key}" in {path} isn\'t an object; left it alone.', app.restart)
    old = servers.get(SERVER_NAME)
    if _same(old, entry):
        return Result(label, "unchanged", path, restart=app.restart)
    status = "updated" if old is not None else "added"
    if dry_run:
        return Result(label, status, path, "dry run: nothing written", app.restart)
    new_entry = {**old, **entry} if isinstance(old, dict) else entry  # keep the user's env etc.
    servers = {**servers, SERVER_NAME: new_entry}
    backup = write_config(path, {**data, app.servers_key: servers}, host)
    return Result(label, status, path, f"backup: {backup.name}" if backup else "", app.restart)


def _file_uninstall(app: FileApp, path: Path, host: Host, dry_run: bool, label: str) -> Result:
    try:
        data = read_config(path)
    except ConfigError as exc:
        return Result(label, "error", path, str(exc))
    servers = (data or {}).get(app.servers_key)
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return Result(label, "absent", path)
    if dry_run:
        return Result(label, "removed", path, "dry run: nothing written")
    servers = {k: v for k, v in servers.items() if k != SERVER_NAME}
    backup = write_config(path, {**data, app.servers_key: servers}, host)
    return Result(label, "removed", path, f"backup: {backup.name}" if backup else "")


def _label(app: FileApp, path: Path, host: Host) -> str:
    if app.key == "claude-desktop" and "Packages" in path.parts:
        return "Claude Desktop (Microsoft Store)"
    return app.label


# -- Claude Code ----------------------------------------------------------------

CLAUDE_CODE_RESTART = "Start a new Claude Code session"


def claude_code_file(host: Host) -> Path:
    return host.home / ".claude.json"


def claude_code_detected(host: Host) -> bool:
    return bool(host.which("claude")) or claude_code_file(host).exists() or (host.home / ".claude").is_dir()


def _claude_code_install(launch: Launch, host: Host, dry_run: bool) -> Result:
    label, path = "Claude Code", claude_code_file(host)
    entry = {"type": "stdio", "command": launch.command, "args": list(launch.args)}
    try:
        servers = ((read_config(path) or {}).get("mcpServers")) or {}
    except ConfigError:
        servers = {}
    old = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    if _same(old, entry):
        return Result(label, "unchanged", path, restart=CLAUDE_CODE_RESTART)
    status = "updated" if old is not None else "added"
    cli = host.which("claude")
    if cli:
        add = [cli, "mcp", "add", "--scope", "user", SERVER_NAME, "--", *launch.argv()]
        if dry_run:
            return Result(label, status, path, "dry run: would run " + " ".join(["claude", *add[1:]]), CLAUDE_CODE_RESTART)
        if old is not None:
            host.run_command([cli, "mcp", "remove", "--scope", "user", SERVER_NAME])
        done = host.run_command(add)
        if done.returncode != 0:
            msg = (done.stderr or done.stdout or "").strip().splitlines()
            return Result(label, "error", path, "claude mcp add failed: " + (msg[-1] if msg else f"exit {done.returncode}"))
        return Result(label, status, path, "via claude mcp add --scope user", CLAUDE_CODE_RESTART)
    app = FileApp(CLAUDE_CODE, label, "mcpServers", CLAUDE_CODE_RESTART, lambda h: [path], lambda h: [path])
    return _file_install(app, path, entry, host, dry_run, label)


def _claude_code_uninstall(host: Host, dry_run: bool) -> Result:
    label, path = "Claude Code", claude_code_file(host)
    try:
        servers = ((read_config(path) or {}).get("mcpServers")) or {}
    except ConfigError:
        servers = {}
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return Result(label, "absent", path)
    cli = host.which("claude")
    if cli:
        if dry_run:
            return Result(label, "removed", path, "dry run: would run claude mcp remove --scope user communitycad")
        done = host.run_command([cli, "mcp", "remove", "--scope", "user", SERVER_NAME])
        if done.returncode != 0:
            msg = (done.stderr or done.stdout or "").strip().splitlines()
            return Result(label, "error", path, "claude mcp remove failed: " + (msg[-1] if msg else f"exit {done.returncode}"))
        return Result(label, "removed", path)
    app = FileApp(CLAUDE_CODE, label, "mcpServers", "", lambda h: [path], lambda h: [path])
    return _file_uninstall(app, path, host, dry_run, label)


# -- the commands ---------------------------------------------------------------


def install(host: Host, launch: Launch, apps: list[str] | None = None, dry_run: bool = False) -> list[Result]:
    """Configure every detected app, or just `apps` (configured even when not detected)."""
    out: list[Result] = []
    for key in apps or APP_KEYS:
        forced = apps is not None
        if key == CLAUDE_CODE:
            if forced or claude_code_detected(host):
                out.append(_claude_code_install(launch, host, dry_run))
            else:
                out.append(Result("Claude Code", "not_found"))
            continue
        app = FILE_APPS[key]
        paths = app.detect(host) or (app.locate(host)[:1] if forced else [])
        if not paths:
            out.append(Result(app.label, "not_found"))
            continue
        app_launch = launch.absolute(host) if app.absolute_command else launch
        for path in paths:
            out.append(_file_install(app, path, app.entry(app_launch), host, dry_run, _label(app, path, host)))
    return out


def uninstall(host: Host, apps: list[str] | None = None, dry_run: bool = False) -> list[Result]:
    out: list[Result] = []
    for key in apps or APP_KEYS:
        if key == CLAUDE_CODE:
            out.append(_claude_code_uninstall(host, dry_run))
            continue
        app = FILE_APPS[key]
        paths = [p for p in app.locate(host) if p.exists()]
        if not paths:
            out.append(Result(app.label, "absent"))
        for path in paths:
            out.append(_file_uninstall(app, path, host, dry_run, _label(app, path, host)))
    return out


@dataclass
class AppState:
    label: str
    detected: bool
    path: Path | None = None
    entry: dict[str, Any] | None = None
    problem: str | None = None


def configured_apps(host: Host) -> list[AppState]:
    """Which apps are installed and whether they have our entry (for `doctor`)."""
    states: list[AppState] = []
    path = claude_code_file(host)
    try:
        servers = (read_config(path) or {}).get("mcpServers") or {}
        entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
        states.append(AppState("Claude Code", claude_code_detected(host), path, entry))
    except ConfigError as exc:
        states.append(AppState("Claude Code", True, path, None, str(exc)))
    for app in FILE_APPS.values():
        found = app.detect(host)
        if not found:
            states.append(AppState(app.label, False))
        for p in found:
            try:
                servers = (read_config(p) or {}).get(app.servers_key) or {}
                entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
                states.append(AppState(_label(app, p, host), True, p, entry))
            except ConfigError as exc:
                states.append(AppState(_label(app, p, host), True, p, None, str(exc)))
    return states


def manual_snippet(launch: Launch) -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: {"command": launch.command, "args": list(launch.args)}}}, indent=2)
