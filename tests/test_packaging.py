"""Release bookkeeping: every place that names the version agrees."""

from __future__ import annotations

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
