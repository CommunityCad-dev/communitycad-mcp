"""install / uninstall / doctor against fake home folders for each OS and app."""

from __future__ import annotations

import io
import json
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import respx

from communitycad_mcp.__main__ import main
from communitycad_mcp.cli import cmd_doctor, parser, run
from communitycad_mcp.config import Settings
from communitycad_mcp.installer import (
    FILE_APPS,
    Host,
    Launch,
    app_key,
    install,
    read_config,
    uninstall,
)
from communitycad_mcp.token_store import StoredToken, TokenStore

from .conftest import TOKEN, V1

ENTRY = {"command": "uvx", "args": ["communitycad-mcp"]}


def make_host(tmp_path: Path, system: str, programs: dict[str, str] | None = None, calls: list | None = None) -> Host:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {}
    if system == "Windows":
        env = {"APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local")}
    programs = {"uvx": "/bin/uvx", "uv": "/bin/uv"} if programs is None else programs

    def fake_run(args: list[str]) -> subprocess.CompletedProcess:
        if calls is not None:
            calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    return Host(
        system=system,
        home=home,
        env=env,
        which=programs.get,
        run=fake_run,
        now=lambda: datetime(2026, 1, 2, 3, 4, 5),
        python="/usr/bin/python3",
    )


def desktop_dir(host: Host) -> Path:
    return {
        "Windows": host.home / "AppData" / "Roaming" / "Claude",
        "Darwin": host.home / "Library" / "Application Support" / "Claude",
        "Linux": host.home / ".config" / "Claude",
    }[host.system]


def vscode_dir(host: Host) -> Path:
    return {
        "Windows": host.home / "AppData" / "Roaming" / "Code" / "User",
        "Darwin": host.home / "Library" / "Application Support" / "Code" / "User",
        "Linux": host.home / ".config" / "Code" / "User",
    }[host.system]


def all_app_dirs(host: Host) -> dict[str, Path]:
    dirs = {
        "claude-desktop": desktop_dir(host),
        "cursor": host.home / ".cursor",
        "vscode": vscode_dir(host),
        "windsurf": host.home / ".codeium" / "windsurf",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


FILES = {"claude-desktop": "claude_desktop_config.json", "cursor": "mcp.json", "vscode": "mcp.json", "windsurf": "mcp_config.json"}


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_install_every_app_per_os(tmp_path, system):
    host = make_host(tmp_path, system)
    dirs = all_app_dirs(host)
    # Cursor already has another server and a setting; both must survive.
    cursor = dirs["cursor"] / "mcp.json"
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}), encoding="utf-8")

    results = install(host, Launch.for_host(host))
    status = {r.app: r.status for r in results}
    assert status == {
        "Claude Code": "not_found", "Claude Desktop": "added", "Cursor": "added", "VS Code": "added", "Windsurf": "added",
    }
    desktop = read_config(dirs["claude-desktop"] / FILES["claude-desktop"])
    # Claude Desktop gets the full uvx path (GUI apps may not see the shell PATH).
    assert desktop["mcpServers"]["communitycad"] == {"command": "/bin/uvx", "args": ["communitycad-mcp"]}
    assert read_config(cursor) == {"mcpServers": {"other": {"command": "x"}, "communitycad": ENTRY}, "keep": 1}
    vs = read_config(dirs["vscode"] / "mcp.json")
    assert vs == {"servers": {"communitycad": {"type": "stdio", **ENTRY}}}
    assert read_config(dirs["windsurf"] / "mcp_config.json")["mcpServers"]["communitycad"] == ENTRY
    # The existing file was backed up; new files need no backup.
    assert (dirs["cursor"] / "mcp.json.bak-20260102-030405").read_text(encoding="utf-8").startswith('{"mcpServers"')
    assert not list(dirs["windsurf"].glob("*.bak-*"))
    # UTF-8 without a BOM.
    for key, d in dirs.items():
        assert not (d / FILES[key]).read_bytes().startswith(b"\xef\xbb\xbf")

    # Idempotent: a second run changes nothing and makes no new backups.
    again = install(host, Launch.for_host(host))
    assert {r.status for r in again if r.app != "Claude Code"} == {"unchanged"}
    assert len(list(dirs["cursor"].glob("*.bak-*"))) == 1

    # Uninstall takes out only our entry.
    gone = uninstall(host)
    assert {r.app: r.status for r in gone}["Cursor"] == "removed"
    assert read_config(cursor) == {"mcpServers": {"other": {"command": "x"}}, "keep": 1}
    assert read_config(dirs["vscode"] / "mcp.json") == {"servers": {}}
    assert {r.status for r in uninstall(host)} == {"absent"}


def test_bom_and_non_ascii_survive(tmp_path):
    host = make_host(tmp_path, "Windows")
    d = all_app_dirs(host)["claude-desktop"]
    f = d / "claude_desktop_config.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"m\u00fcller": {"command": "C:\\Programme\\\u00e4.exe"}}}).encode())
    install(host, Launch.for_host(host), ["claude-desktop"])
    raw = f.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(raw.decode("utf-8"))
    assert data["mcpServers"]["m\u00fcller"]["command"] == "C:\\Programme\\\u00e4.exe"
    assert "communitycad" in data["mcpServers"]


def test_windows_store_claude_desktop(tmp_path):
    host = make_host(tmp_path, "Windows")
    store = host.home / "AppData" / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    results = install(host, Launch.for_host(host), ["claude-desktop"])
    assert [r.app for r in results] == ["Claude Desktop (Microsoft Store)"]
    assert "communitycad" in read_config(store / "claude_desktop_config.json")["mcpServers"]


def test_unreadable_config_is_left_alone(tmp_path):
    host = make_host(tmp_path, "Linux")
    d = all_app_dirs(host)["vscode"]
    text = '{\n  // my servers\n  "servers": {},\n}\n'
    (d / "mcp.json").write_text(text, encoding="utf-8")
    [r] = install(host, Launch.for_host(host), ["vscode"])
    assert r.status == "error" and "by hand" in r.detail
    assert (d / "mcp.json").read_text(encoding="utf-8") == text
    assert not list(d.glob("*.bak-*"))


def test_existing_entry_is_updated_keeping_env(tmp_path):
    host = make_host(tmp_path, "Darwin")
    f = all_app_dirs(host)["cursor"] / "mcp.json"
    f.write_text(json.dumps({"mcpServers": {"communitycad": {"command": "old", "args": [], "env": {"A": "1"}}}}))
    [r] = install(host, Launch.for_host(host), ["cursor"])
    assert r.status == "updated"
    assert read_config(f)["mcpServers"]["communitycad"] == {**ENTRY, "env": {"A": "1"}}


def test_dry_run_writes_nothing(tmp_path):
    host = make_host(tmp_path, "Linux")
    dirs = all_app_dirs(host)
    results = install(host, Launch.for_host(host), dry_run=True)
    assert {r.status for r in results if r.app != "Claude Code"} == {"added"}
    assert not any(any(d.iterdir()) for d in dirs.values())


def test_app_flag_creates_config_when_not_detected(tmp_path):
    host = make_host(tmp_path, "Linux")
    [r] = install(host, Launch.for_host(host), ["windsurf"])
    assert r.status == "added"
    assert read_config(host.home / ".codeium" / "windsurf" / "mcp_config.json")["mcpServers"]["communitycad"] == ENTRY


def test_from_path_and_no_uv(tmp_path):
    host = make_host(tmp_path, "Linux")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    launch = Launch.for_host(host, checkout)
    assert launch.argv() == ["uv", "run", "--directory", str(checkout.resolve()), "communitycad-mcp"]
    no_uv = make_host(tmp_path, "Linux", programs={})
    launch = Launch.for_host(no_uv)
    assert launch.argv() == ["/usr/bin/python3", "-m", "communitycad_mcp"] and launch.note


def test_claude_code_uses_cli(tmp_path):
    calls: list = []
    host = make_host(tmp_path, "Windows", {"uvx": "uvx.exe", "claude": "C:\\bin\\claude.exe"}, calls)
    [r] = install(host, Launch.for_host(host), ["claude-code"])
    assert r.status == "added"
    assert calls == [["C:\\bin\\claude.exe", "mcp", "add", "--scope", "user", "communitycad", "--", "uvx", "communitycad-mcp"]]
    # Once claude has written it, a second install is a no-op.
    (host.home / ".claude.json").write_text(json.dumps({"mcpServers": {"communitycad": {"type": "stdio", **ENTRY, "env": {}}}}))
    calls.clear()
    [r] = install(host, Launch.for_host(host), ["claude-code"])
    assert r.status == "unchanged" and calls == []
    [r] = uninstall(host, ["claude-code"])
    assert r.status == "removed"
    assert calls == [["C:\\bin\\claude.exe", "mcp", "remove", "--scope", "user", "communitycad"]]


def test_claude_code_without_cli_edits_file(tmp_path):
    host = make_host(tmp_path, "Linux")
    f = host.home / ".claude.json"
    f.write_text(json.dumps({"numStartups": 3, "mcpServers": {"x": {"command": "y"}}, "projects": {"/p": {}}}))
    [r] = install(host, Launch.for_host(host))[:1]
    assert r.app == "Claude Code" and r.status == "added"
    data = read_config(f)
    assert data["numStartups"] == 3 and data["projects"] == {"/p": {}}
    assert data["mcpServers"] == {"x": {"command": "y"}, "communitycad": {"type": "stdio", **ENTRY}}
    [r] = uninstall(host, ["claude-code"])
    assert r.status == "removed" and read_config(f)["mcpServers"] == {"x": {"command": "y"}}


def test_app_names():
    assert app_key("Claude Desktop") == "claude-desktop"
    assert app_key("VS_Code") == "vscode"
    assert app_key("claude") == "claude-code"
    with pytest.raises(ValueError):
        app_key("notepad")
    assert set(FILE_APPS) == {"claude-desktop", "cursor", "vscode", "windsurf"}


# -- the commands -----------------------------------------------------------------


def test_install_command_output(tmp_path):
    host = make_host(tmp_path, "Darwin")
    all_app_dirs(host)
    out = io.StringIO()
    assert run(["install", "--app", "desktop,cursor"], host, out) == 0
    text = out.getvalue()
    assert "[added]    Claude Desktop" in text and "[added]    Cursor" in text and "Windsurf" not in text
    assert "Quit Claude Desktop fully" in text
    assert "find me an M8 screw with a STEP file" in text
    assert "link your CommunityCAD account" in text


def test_install_nothing_found_prints_snippet(tmp_path):
    host = make_host(tmp_path, "Linux")
    out = io.StringIO()
    assert run(["install"], host, out) == 1
    assert '"communitycad"' in out.getvalue() and "--app" in out.getvalue()


def test_unknown_app(tmp_path):
    out = io.StringIO()
    assert run(["install", "--app", "notepad"], make_host(tmp_path, "Linux"), out) == 2
    assert "Unknown app" in out.getvalue()


def test_no_arguments_starts_server(monkeypatch):
    assert run(["serve"]) is None
    assert parser().parse_args([]).command is None
    started = []
    monkeypatch.setattr("communitycad_mcp.server.build_server", lambda: type("S", (), {"run": lambda self, t: started.append(t)})())
    main([])
    assert started == ["stdio"]


@respx.mock
def test_doctor_signed_in_never_prints_token(tmp_path, settings):
    TokenStore(settings.token_path).save(StoredToken(access_token=TOKEN, username="maker"))
    respx.get(f"{V1}/account/status").respond(200, json={"signed_in": True, "username": "maker"})
    host = make_host(tmp_path, "Windows")
    install(host, Launch.for_host(host), ["cursor"])
    out = io.StringIO()
    code = cmd_doctor(parser().parse_args(["doctor"]), host, out, settings=settings)
    text = out.getvalue()
    assert code == 0, text
    assert TOKEN not in text
    assert "Signed in as maker" in text and "API reachable" in text and "Cursor: set up" in text
    assert respx.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock
def test_doctor_reports_problems_with_fixes(tmp_path, settings):
    respx.get(f"{V1}/account/status").mock(side_effect=httpx.ConnectError("down"))
    host = make_host(tmp_path, "Linux", programs={})
    out = io.StringIO()
    code = cmd_doctor(parser().parse_args(["doctor"]), host, out, settings=settings)
    text = out.getvalue()
    assert code == 1
    assert "uvx isn't on your PATH" in text and "docs.astral.sh/uv" in text
    assert "Can't reach the CommunityCAD API" in text
    assert "Not signed in" in text
    assert "No AI app has CommunityCAD set up yet" in text and "Fix: communitycad-mcp install" in text


@respx.mock
def test_doctor_dead_sign_in(tmp_path, settings):
    TokenStore(settings.token_path).save(StoredToken(access_token=TOKEN, username="maker"))
    respx.get(f"{V1}/account/status").respond(200, json={"signed_in": False})
    host = make_host(tmp_path, "Linux")
    install(host, Launch.for_host(host), ["cursor"])
    out = io.StringIO()
    assert cmd_doctor(parser().parse_args(["doctor"]), host, out, settings=settings) == 1
    assert "isn't accepted any more" in out.getvalue() and TOKEN not in out.getvalue()
