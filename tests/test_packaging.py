"""Release bookkeeping: every place that names the version agrees."""

from __future__ import annotations

import json
import re
from pathlib import Path

import communitycad_mcp


ROOT = Path(__file__).resolve().parent.parent


def test_package_version_matches_pyproject():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
    assert communitycad_mcp.__version__ == version


def test_changelog_has_an_entry_for_this_version():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{communitycad_mcp.__version__}]" in changelog


def test_desktop_extension_and_plugin_carry_the_same_version():
    version = communitycad_mcp.__version__
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == version
    bundle = (ROOT / "mcpb" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "([^"]+)"', bundle, re.M).group(1) == version
    assert f'"communitycad-mcp=={version}"' in bundle
    plugin = json.loads((ROOT / "plugins" / "communitycad" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert plugin["version"] == version


def test_desktop_extension_settings_match_the_server_environment():
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    env = manifest["server"]["mcp_config"]["env"]
    assert set(env) <= {"COMMUNITYCAD_DOWNLOAD_DIR", "COMMUNITYCAD_MAX_DOWNLOAD_MB"}
    for value in env.values():
        key = re.fullmatch(r"\$\{user_config\.(\w+)\}", value).group(1)
        assert key in manifest["user_config"]
    assert manifest["privacy_policies"] == ["https://communitycad.dev/privacy"]
