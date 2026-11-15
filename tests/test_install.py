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
from communitycad_mcp.installer import (
    FILE_APPS,
    Host,
    Launch,
    app_key,
    configured_apps,
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


def xdg(host: Host) -> Path:
    return host.home / ".config"


def all_app_dirs(host: Host) -> dict[str, Path]:
    dirs = {
        "claude-desktop": desktop_dir(host),
        "cursor": host.home / ".cursor",
        "vscode": vscode_dir(host),
        "windsurf": host.home / ".codeium" / "windsurf",
        "opencode": xdg(host) / "opencode",
        "codex": host.home / ".codex",
        "gemini": host.home / ".gemini",
        "copilot": host.home / ".copilot",
        "zed": host.home / "AppData" / "Roaming" / "Zed" if host.system == "Windows" else xdg(host) / "zed",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


FILES = {
    "claude-desktop": "claude_desktop_config.json", "cursor": "mcp.json", "vscode": "mcp.json",
    "windsurf": "mcp_config.json", "opencode": "opencode.json", "codex": "config.toml",
    "gemini": "settings.json", "copilot": "mcp-config.json", "zed": "settings.json",
}
LABELS = {
    "claude-desktop": "Claude Desktop", "cursor": "Cursor", "vscode": "VS Code", "windsurf": "Windsurf",
    "opencode": "OpenCode", "codex": "Codex", "gemini": "Gemini CLI", "copilot": "GitHub Copilot CLI", "zed": "Zed",
}


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_install_every_app_per_os(tmp_path, system):
    host = make_host(tmp_path, system)
    dirs = all_app_dirs(host)
    # Cursor already has another server and a setting; both must survive.
    cursor = dirs["cursor"] / "mcp.json"
    cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": 1}), encoding="utf-8")

    results = install(host, Launch.for_host(host))
    status = {r.app: r.status for r in results}
    assert status == {"Claude Code": "not_found", **{label: "added" for label in LABELS.values()}}
    f = {k: dirs[k] / name for k, name in FILES.items()}
    # GUI apps get the full uvx path (they may not see the shell PATH).
    assert read_config(f["claude-desktop"])["mcpServers"]["communitycad"] == {"command": "/bin/uvx", "args": ["communitycad-mcp"]}
    assert read_config(f["zed"])["context_servers"]["communitycad"] == {"command": "/bin/uvx", "args": ["communitycad-mcp"]}
    assert read_config(cursor) == {"mcpServers": {"other": {"command": "x"}, "communitycad": ENTRY}, "keep": 1}
    assert read_config(f["vscode"]) == {"servers": {"communitycad": {"type": "stdio", **ENTRY}}}
    assert read_config(f["windsurf"])["mcpServers"]["communitycad"] == ENTRY
    assert read_config(f["gemini"]) == {"mcpServers": {"communitycad": ENTRY}}
    assert read_config(f["copilot"]) == {"mcpServers": {"communitycad": {"type": "local", **ENTRY, "tools": ["*"]}}}
    assert read_config(f["opencode"]) == {
        "mcp": {"communitycad": {"type": "local", "command": ["uvx", "communitycad-mcp"], "enabled": True}}
    }
    assert f["codex"].read_text(encoding="utf-8") == '[mcp_servers.communitycad]\ncommand = "uvx"\nargs = ["communitycad-mcp"]\n'
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
    assert not list(dirs["codex"].glob("*.bak-*"))

    # doctor's view: every app detected and set up.
    states = {s.label: s for s in configured_apps(host)}
    assert all(states[label].command in ("uvx", "/bin/uvx") for label in LABELS.values())

    # Uninstall takes out only our entry.
    gone = uninstall(host)
    assert {r.app: r.status for r in gone} == {"Claude Code": "absent", **{label: "removed" for label in LABELS.values()}}
    assert read_config(cursor) == {"mcpServers": {"other": {"command": "x"}}, "keep": 1}
    assert read_config(f["vscode"]) == {"servers": {}}
    assert read_config(f["opencode"]) == {"mcp": {}}
    assert "communitycad" not in f["codex"].read_text(encoding="utf-8")
    assert {r.status for r in uninstall(host)} == {"absent"}


ZED_SETTINGS = """// Zed settings
//
// For information on how to configure Zed, see the Zed documentation.
{
  "theme": "One Dark", // my theme
  "context_servers": {
    /* a server I wrote */
    "mine": {"command": "node", "args": ["srv.js"]},
  },
  "vim_mode": true,
}
"""


def test_json_with_comments_keeps_every_other_byte(tmp_path):
    host = make_host(tmp_path, "Darwin")
    f = all_app_dirs(host)["zed"] / "settings.json"
    f.write_text(ZED_SETTINGS, encoding="utf-8")
    [r] = install(host, Launch.for_host(host), ["zed"])
    assert r.status == "added"
    text = f.read_text(encoding="utf-8")
    assert text.startswith(ZED_SETTINGS[: ZED_SETTINGS.index('"mine"')])
    assert '"vim_mode": true,\n}\n' in text and "// my theme" in text and "/* a server I wrote */" in text
    assert read_config(f)["context_servers"] == {
        "mine": {"command": "node", "args": ["srv.js"]},
        "communitycad": {"command": "/bin/uvx", "args": ["communitycad-mcp"]},
    }
    assert [x.status for x in install(host, Launch.for_host(host), ["zed"])] == ["unchanged"]
    [r] = uninstall(host, ["zed"])
    assert r.status == "removed"
    assert f.read_text(encoding="utf-8") == ZED_SETTINGS


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        "",
        '{\n  // nothing yet\n}\n',
        '{\n  "a": 1 // note\n}\n',
        '{"a": [1, 2,], "servers": {},}',
        '{"servers": {"x": {"command": "y"} /* end */}}',
    ],
)
def test_json_edits_round_trip(tmp_path, text):
    host = make_host(tmp_path, "Linux")
    f = all_app_dirs(host)["vscode"] / "mcp.json"
    f.write_text(text, encoding="utf-8")
    before = read_config(f)
    [r] = install(host, Launch.for_host(host), ["vscode"])
    assert r.status == "added"
    after = read_config(f)
    assert after["servers"]["communitycad"] == {"type": "stdio", **ENTRY}
    assert {k: v for k, v in after.items() if k != "servers"} == {k: v for k, v in before.items() if k != "servers"}
    assert {k: v for k, v in after["servers"].items() if k != "communitycad"} == before.get("servers", {})
    uninstall(host, ["vscode"])
    assert "communitycad" not in read_config(f).get("servers", {})


@pytest.mark.parametrize(
    "text",
    [
        '{\n  "servers": {}\n}\n',
        '{\n  "servers": {\n    "x": {"command": "y"}\n  }\n}\n',
        '{\n  "servers": {\n    "x": {"command": "y"} // note\n  }\n}\n',
        '{\n  "servers": {\n    "x": {"command": "y"}, // note\n  },\n}\n',
        '{"servers": {"x": 1, "y": 2}, "z": 3}',
    ],
)
def test_install_then_uninstall_gives_back_the_same_bytes(tmp_path, text):
    host = make_host(tmp_path, "Linux")
    f = all_app_dirs(host)["vscode"] / "mcp.json"
    f.write_text(text, encoding="utf-8")
    install(host, Launch.for_host(host), ["vscode"])
    assert "// note" not in text or "// note" in f.read_text(encoding="utf-8")
    uninstall(host, ["vscode"])
    assert f.read_text(encoding="utf-8") == text


def test_opencode_jsonc_and_user_choices(tmp_path, settings):
    host = make_host(tmp_path, "Windows")  # OpenCode uses ~/.config on every OS
    d = all_app_dirs(host)["opencode"]
    jsonc = d / "opencode.jsonc"
    jsonc.write_text('{\n  "$schema": "https://opencode.ai/config.json",\n  // models\n  "model": "x"\n}\n', encoding="utf-8")
    [r] = install(host, Launch.for_host(host), ["opencode"])
    assert r.path == jsonc and not (d / "opencode.json").exists()
    assert "// models" in jsonc.read_text(encoding="utf-8")
    # The user turned it off and added env: an update keeps both.
    data = read_config(jsonc)
    entry = {"type": "local", "command": ["old-uvx", "communitycad-mcp"], "enabled": False, "environment": {"A": "1"}}
    jsonc.write_text(json.dumps({**data, "mcp": {"communitycad": entry}}), encoding="utf-8")
    [r] = install(host, Launch.for_host(host), ["opencode"])
    assert r.status == "updated"
    assert read_config(jsonc)["mcp"]["communitycad"] == {**entry, "command": ["uvx", "communitycad-mcp"]}
    [r] = install(host, Launch.for_host(host), ["opencode"])
    assert r.status == "unchanged"
    # doctor says it's turned off.
    out = io.StringIO()
    cmd_doctor(parser().parse_args(["doctor"]), host, out, settings, httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    assert "OpenCode: set up, but turned off" in out.getvalue()


def test_xdg_config_home_and_codex_home(tmp_path):
    host = make_host(tmp_path, "Linux")
    host.env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "CODEX_HOME": str(tmp_path / "codex")}
    for d in ("xdg/opencode", "xdg/zed", "codex"):
        (tmp_path / d).mkdir(parents=True)
    results = {r.app: r for r in install(host, Launch.for_host(host)) if r.status == "added"}
    assert results["OpenCode"].path == tmp_path / "xdg" / "opencode" / "opencode.json"
    assert results["Zed"].path == tmp_path / "xdg" / "zed" / "settings.json"
    assert results["Codex"].path == tmp_path / "codex" / "config.toml"


def test_detection_runs_no_programs(tmp_path):
    calls: list = []
    host = make_host(tmp_path, "Linux", {"uvx": "/bin/uvx", "claude": "/bin/claude", "codex": "/bin/codex"}, calls)
    results = install(host, Launch.for_host(host))
    assert {r.status for r in results} == {"not_found"} and calls == []


CODEX = """# my codex settings
model = "gpt-5"

[mcp_servers.other]
command = "o" # mine
args = ["x"]
"""


@pytest.mark.parametrize("no_tomllib", [False, True])
def test_codex_toml(tmp_path, monkeypatch, no_tomllib):
    from communitycad_mcp import configfiles

    if no_tomllib:  # Python 3.10 has no tomllib: the edits must not depend on it
        monkeypatch.setattr(configfiles, "_toml", None)
    host = make_host(tmp_path, "Darwin")
    f = all_app_dirs(host)["codex"] / "config.toml"
    f.write_text(CODEX, encoding="utf-8")
    [r] = install(host, Launch.for_host(host), ["codex"])
    assert r.status == "added"
    assert f.read_text(encoding="utf-8") == CODEX + '\n[mcp_servers.communitycad]\ncommand = "uvx"\nargs = ["communitycad-mcp"]\n'
    assert [x.status for x in install(host, Launch.for_host(host), ["codex"])] == ["unchanged"]

    # A stale command is updated in place; the user's extra keys and env table stay.
    f.write_text(
        CODEX + '\n[mcp_servers.communitycad]\ncommand = "/old/uvx"  \nargs = ["communitycad-mcp"]\nstartup_timeout_sec = 30\n'
        "\n[mcp_servers.communitycad.env]\nA = \"1\"\n\n[profiles.x]\nmodel = \"y\"\n",
        encoding="utf-8",
    )
    [r] = install(host, Launch.for_host(host), ["codex"])
    assert r.status == "updated"
    text = f.read_text(encoding="utf-8")
    assert 'command = "uvx"\n' in text and "startup_timeout_sec = 30" in text and 'A = "1"' in text
    if not no_tomllib:
        tomllib = pytest.importorskip("tomllib")
        data = tomllib.loads(text)
        assert data["mcp_servers"]["communitycad"]["env"] == {"A": "1"} and data["profiles"] == {"x": {"model": "y"}}

    [r] = uninstall(host, ["codex"])
    assert r.status == "removed"
    assert f.read_text(encoding="utf-8") == CODEX + '\n[profiles.x]\nmodel = "y"\n'


def test_codex_inline_entry_is_left_alone(tmp_path):
    host = make_host(tmp_path, "Linux")
    f = all_app_dirs(host)["codex"] / "config.toml"
    text = 'mcp_servers.communitycad = { command = "x", args = [] }\n'
    f.write_text(text, encoding="utf-8")
    [r] = install(host, Launch.for_host(host), ["codex"])
    assert r.status == "error" and "[mcp_servers.communitycad]" in r.detail
    assert f.read_text(encoding="utf-8") == text


def test_copilot_keeps_narrowed_tools(tmp_path):
    host = make_host(tmp_path, "Windows")
    f = all_app_dirs(host)["copilot"] / "mcp-config.json"
    f.write_text(json.dumps({"mcpServers": {"communitycad": {"type": "local", "command": "old", "args": [], "tools": ["search_models"]}}}))
    [r] = install(host, Launch.for_host(host), ["copilot"])
    assert r.status == "updated"
    assert read_config(f)["mcpServers"]["communitycad"] == {"type": "local", **ENTRY, "tools": ["search_models"]}


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
    text = '{\n  // my servers\n  "servers": {,\n}\n'
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
    assert app_key("OpenCode") == "opencode" and app_key("open-code") == "opencode"
    assert app_key("GitHub Copilot") == "copilot" and app_key("gemini-cli") == "gemini"
    assert app_key("Codex") == "codex" and app_key("zed") == "zed"
    assert set(FILE_APPS) == set(LABELS)


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
