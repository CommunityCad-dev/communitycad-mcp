"""Add the server to the AI apps on this computer, or take it out again.

Each app keeps its MCP servers in a config file: JSON (comments allowed) for
most, TOML for Codex. Claude Code is told through its own `claude mcp` command
when that's on the PATH. Only the "communitycad" entry is ever changed: other
servers, settings, comments and key order are kept byte for byte, a timestamped
backup is written next to any file before it changes, and files are written as
UTF-8 without a BOM.

An app counts as installed when its config folder exists; no app is started to
find out. Config locations follow each client's documentation; SolidCraft's
open-source `mcp setup` (crates/engine/src/agent_setup.rs in
github.com/tangency-labs/solidcraft) was a useful cross-check for the paths and
entry shapes of the same set of apps.
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

from . import configfiles
from .configfiles import ConfigError

SERVER_NAME = "communitycad"
PACKAGE = "communitycad-mcp"


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
        return self.xdg_config()

    def xdg_config(self) -> Path:
        """$XDG_CONFIG_HOME or ~/.config, on every OS (OpenCode and Zed use it on macOS too)."""
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


# -- config files ---------------------------------------------------------------


def read_text(path: Path) -> str | None:
    """The file as text (a BOM some editors add is dropped), None when it doesn't exist."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} isn't UTF-8 text") from exc


def read_config(path: Path) -> dict[str, Any] | None:
    """The file's JSON object (comments allowed), {} for an empty file, None when it doesn't exist."""
    text = read_text(path)
    if text is None:
        return None
    try:
        return configfiles.loads(text)
    except ConfigError as exc:
        raise ConfigError(f"{path} isn't readable JSON: {exc}") from exc


def backup_path(path: Path, host: Host) -> Path:
    stamp = host.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        n += 1
    return candidate


def write_text(path: Path, text: str, host: Host) -> Path | None:
    """Write the text (UTF-8, no BOM) atomically after backing up the old file. Returns the backup."""
    backup = None
    if path.exists():
        backup = backup_path(path, host)
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)
    return backup


def write_config(path: Path, data: dict[str, Any], host: Host) -> Path | None:
    return write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", host)


# -- apps -----------------------------------------------------------------------

# How each app spells a server entry:
#   plain    {"command": exe, "args": [...]}
#   stdio    the same with "type": "stdio" (Claude Code, VS Code)
#   copilot  the same with "type": "local" and "tools": ["*"] (GitHub Copilot CLI)
#   opencode {"type": "local", "command": [exe, ...], "enabled": true}
#   codex    TOML [mcp_servers.communitycad] with command and args
SHAPES = ("plain", "stdio", "copilot", "opencode", "codex")


@dataclass(frozen=True)
class FileApp:
    """An app configured by one file with a map of servers."""

    key: str
    label: str
    servers_key: str
    restart: str
    # Where the file is (first one is where --app creates it), and which of those count as installed.
    locate: Callable[[Host], list[Path]]
    detect: Callable[[Host], list[Path]]
    shape: str = "plain"
    absolute_command: bool = False  # GUI apps that may not see the shell's PATH

    def entry(self, launch: Launch) -> dict[str, Any]:
        cmd, args = launch.command, list(launch.args)
        if self.shape == "stdio":
            return {"type": "stdio", "command": cmd, "args": args}
        if self.shape == "copilot":
            return {"type": "local", "command": cmd, "args": args, "tools": ["*"]}
        if self.shape == "opencode":
            return {"type": "local", "command": [cmd, *args], "enabled": True}
        return {"command": cmd, "args": args}

    def managed(self) -> tuple[str, ...]:
        """The fields install keeps up to date. The rest (enabled, tools, env...) are the user's once written."""
        if self.shape == "opencode":
            return ("type", "command")
        if self.shape == "copilot":
            return ("type", "command", "args")
        return tuple(self.entry(Launch("x", ())))

    @property
    def toml(self) -> bool:
        return self.shape == "codex"

    def snippet(self, launch: Launch) -> str:
        if self.toml:
            return f"[{self.servers_key}.{SERVER_NAME}]\n" + "".join(
                f"{k} = {json.dumps(v)}\n" for k, v in self.entry(launch).items()
            )
        return json.dumps({self.servers_key: {SERVER_NAME: self.entry(launch)}}, indent=2)


def entry_command(entry: Any) -> str | None:
    """The program an entry starts (OpenCode keeps it as the first item of a list)."""
    if not isinstance(entry, dict):
        return None
    cmd = entry.get("command")
    if isinstance(cmd, list):
        cmd = cmd[0] if cmd else None
    return cmd if isinstance(cmd, str) else None


def _claude_desktop_files(host: Host) -> list[Path]:
    files = [host.user_config() / "Claude" / "claude_desktop_config.json"]
    if host.system == "Windows":
        # The Microsoft Store build keeps its settings in a per-package folder.
        packages = host.localappdata() / "Packages"
        if packages.is_dir():
            for pkg in sorted(packages.glob("Claude_*")):
                files.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    return files


def _existing_parent(paths: Callable[[Host], list[Path]]) -> Callable[[Host], list[Path]]:
    return lambda host: [p for p in paths(host) if p.parent.is_dir()]


def _opencode(host: Host) -> list[Path]:
    d = host.xdg_config() / "opencode"
    # OpenCode also reads opencode.jsonc; edit that one when it's the only one there.
    if not (d / "opencode.json").exists() and (d / "opencode.jsonc").exists():
        return [d / "opencode.jsonc"]
    return [d / "opencode.json"]


def _codex(host: Host) -> list[Path]:
    home = Path(host.env["CODEX_HOME"]) if host.env.get("CODEX_HOME") else host.home / ".codex"
    return [home / "config.toml"]


def _zed(host: Host) -> list[Path]:
    base = host.appdata() / "Zed" if host.system == "Windows" else host.xdg_config() / "zed"
    return [base / "settings.json"]


_cursor = lambda host: [host.home / ".cursor" / "mcp.json"]  # noqa: E731
_vscode = lambda host: [host.user_config() / "Code" / "User" / "mcp.json"]  # noqa: E731
_windsurf = lambda host: [host.home / ".codeium" / "windsurf" / "mcp_config.json"]  # noqa: E731
_gemini = lambda host: [host.home / ".gemini" / "settings.json"]  # noqa: E731
_copilot = lambda host: [host.home / ".copilot" / "mcp-config.json"]  # noqa: E731


def _app(key: str, label: str, servers_key: str, restart: str, files: Callable[[Host], list[Path]], **kw: Any) -> FileApp:
    return FileApp(key, label, servers_key, restart, files, _existing_parent(files), **kw)


FILE_APPS: dict[str, FileApp] = {
    "claude-desktop": _app(
        "claude-desktop", "Claude Desktop", "mcpServers",
        "Quit Claude Desktop fully (also from the tray or menu bar) and open it again",
        _claude_desktop_files, absolute_command=True,
    ),
    "cursor": _app("cursor", "Cursor", "mcpServers", "Restart Cursor", _cursor),
    "vscode": _app(
        "vscode", "VS Code", "servers", "Reload VS Code, then turn on the CommunityCAD server in Copilot Chat's tools list",
        _vscode, shape="stdio",
    ),
    "windsurf": _app("windsurf", "Windsurf", "mcpServers", "Restart Windsurf", _windsurf),
    "opencode": _app("opencode", "OpenCode", "mcp", "Start a new OpenCode session", _opencode, shape="opencode"),
    "codex": _app("codex", "Codex", "mcp_servers", "Start a new Codex session", _codex, shape="codex"),
    "gemini": _app("gemini", "Gemini CLI", "mcpServers", "Start a new Gemini CLI session", _gemini),
    "copilot": _app("copilot", "GitHub Copilot CLI", "mcpServers", "Start a new Copilot CLI session", _copilot, shape="copilot"),
    "zed": _app(
        "zed", "Zed", "context_servers", "Zed reloads its settings on its own; check the server is on in the Agent panel's settings",
        _zed, absolute_command=True,
    ),
}

CLAUDE_CODE = "claude-code"
APP_KEYS = [CLAUDE_CODE, *FILE_APPS]
APP_LABELS = {CLAUDE_CODE: "Claude Code", **{k: a.label for k, a in FILE_APPS.items()}}
ALIASES = {
    "claude": CLAUDE_CODE, "claudecode": CLAUDE_CODE, "claude-code": CLAUDE_CODE, "code": "vscode",
    "desktop": "claude-desktop", "claude-desktop": "claude-desktop", "claudedesktop": "claude-desktop",
    "vs-code": "vscode", "vscode": "vscode", "cursor": "cursor", "windsurf": "windsurf",
    "opencode": "opencode", "open-code": "opencode", "codex": "codex", "codex-cli": "codex",
    "gemini": "gemini", "gemini-cli": "gemini", "copilot": "copilot", "copilot-cli": "copilot",
    "github-copilot": "copilot", "github-copilot-cli": "copilot", "zed": "zed",
}


def app_key(name: str) -> str:
    key = ALIASES.get(name.strip().lower().replace(" ", "-").replace("_", "-"))
    if key is None:
        raise ValueError(f"Unknown app {name!r}. Choose one of: {', '.join(APP_KEYS)}.")
    return key


def _read_entry(app: FileApp, text: str | None) -> Any:
    """Our entry in the file's text (None when absent). Raises ConfigError for files we can't read."""
    if text is None:
        return None
    if app.toml:
        return configfiles.toml_server(text, app.servers_key, SERVER_NAME)
    servers = configfiles.loads(text).get(app.servers_key)
    if servers is None:
        return None
    if not isinstance(servers, dict):
        raise ConfigError(f'"{app.servers_key}" isn\'t an object')
    return servers.get(SERVER_NAME)


def _file_install(app: FileApp, path: Path, launch: Launch, host: Host, dry_run: bool, label: str) -> Result:
    entry = app.entry(launch)
    managed = {k: entry[k] for k in app.managed()}
    try:
        text = read_text(path)
        old = _read_entry(app, text)
        if isinstance(old, dict) and all(old.get(k) == v for k, v in managed.items()):
            return Result(label, "unchanged", path, restart=app.restart)
        status = "updated" if old is not None else "added"
        if app.toml:
            new = configfiles.set_toml_server(text or "", app.servers_key, SERVER_NAME, managed)
        else:
            new_entry = {**old, **managed} if isinstance(old, dict) else entry  # keep the user's env etc.
            new = configfiles.set_server(text or "", app.servers_key, SERVER_NAME, new_entry)
    except ConfigError as exc:
        return Result(label, "error", path, f"{path}: {exc}. Left it alone; add the entry by hand:\n{app.snippet(launch)}", app.restart)
    if dry_run:
        return Result(label, status, path, "dry run: nothing written", app.restart)
    backup = write_text(path, new, host)
    return Result(label, status, path, f"backup: {backup.name}" if backup else "", app.restart)


def _file_uninstall(app: FileApp, path: Path, host: Host, dry_run: bool, label: str) -> Result:
    try:
        text = read_text(path)
        if _read_entry(app, text) is None:
            return Result(label, "absent", path)
        assert text is not None
        if app.toml:
            new = configfiles.remove_toml_server(text, app.servers_key, SERVER_NAME)
        else:
            new = configfiles.remove_server(text, app.servers_key, SERVER_NAME)
    except ConfigError as exc:
        return Result(label, "error", path, f"{path}: {exc}")
    if dry_run:
        return Result(label, "removed", path, "dry run: nothing written")
    backup = write_text(path, new, host)
    return Result(label, "removed", path, f"backup: {backup.name}" if backup else "")


def _label(app: FileApp, path: Path, host: Host) -> str:
    if app.key == "claude-desktop" and "Packages" in path.parts:
        return "Claude Desktop (Microsoft Store)"
    return app.label


# -- Claude Code ----------------------------------------------------------------

CLAUDE_CODE_RESTART = "Start a new Claude Code session"


def claude_code_file(host: Host) -> Path:
    return host.home / ".claude.json"


_CLAUDE_CODE_APP = FileApp(
    CLAUDE_CODE, "Claude Code", "mcpServers", CLAUDE_CODE_RESTART,
    lambda h: [claude_code_file(h)], lambda h: [claude_code_file(h)], shape="stdio",
)


def claude_code_detected(host: Host) -> bool:
    return claude_code_file(host).exists() or (host.home / ".claude").is_dir()


def _claude_code_servers(host: Host) -> dict[str, Any]:
    try:
        servers = (read_config(claude_code_file(host)) or {}).get("mcpServers") or {}
    except ConfigError:
        servers = {}
    return servers if isinstance(servers, dict) else {}


def _claude_code_install(launch: Launch, host: Host, dry_run: bool) -> Result:
    label, path = "Claude Code", claude_code_file(host)
    entry = _CLAUDE_CODE_APP.entry(launch)
    old = _claude_code_servers(host).get(SERVER_NAME)
    if isinstance(old, dict) and all(old.get(k) == v for k, v in entry.items()):
        return Result(label, "unchanged", path, restart=CLAUDE_CODE_RESTART)
    status = "updated" if old is not None else "added"
    cli = host.which("claude")
    if cli:
        # Claude Code rewrites ~/.claude.json while it runs, so let it make the change itself.
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
    return _file_install(_CLAUDE_CODE_APP, path, launch, host, dry_run, label)


def _claude_code_uninstall(host: Host, dry_run: bool) -> Result:
    label, path = "Claude Code", claude_code_file(host)
    if SERVER_NAME not in _claude_code_servers(host):
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
    return _file_uninstall(_CLAUDE_CODE_APP, path, host, dry_run, label)


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
            out.append(_file_install(app, path, app_launch, host, dry_run, _label(app, path, host)))
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
    key: str = ""

    @property
    def command(self) -> str | None:
        return entry_command(self.entry)


def configured_apps(host: Host) -> list[AppState]:
    """Which apps are installed and whether they have our entry (for `doctor`)."""
    states: list[AppState] = []
    for app in [_CLAUDE_CODE_APP, *FILE_APPS.values()]:
        found = ([claude_code_file(host)] if claude_code_detected(host) else []) if app is _CLAUDE_CODE_APP else app.detect(host)
        if not found:
            states.append(AppState(app.label, False, key=app.key))
        for p in found:
            label = _label(app, p, host)
            try:
                entry = _read_entry(app, read_text(p))
                states.append(AppState(label, True, p, entry if isinstance(entry, dict) else None, key=app.key))
            except ConfigError as exc:
                states.append(AppState(label, True, p, None, f"{p} can't be read: {exc}", key=app.key))
    return states


def manual_snippet(launch: Launch) -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: {"command": launch.command, "args": list(launch.args)}}}, indent=2)
