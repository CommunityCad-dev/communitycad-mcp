"""The installed console script speaks MCP over stdio."""

import shutil
import sys
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from .test_server import WORKING

SCRIPT = Path(sys.executable).parent / "communitycad-mcp"


@pytest.mark.skipif(not SCRIPT.exists() and not shutil.which("communitycad-mcp"), reason="not installed")
async def test_console_script_over_stdio(tmp_path):
    params = StdioServerParameters(
        command=str(SCRIPT if SCRIPT.exists() else shutil.which("communitycad-mcp")),
        env={"COMMUNITYCAD_CONFIG_DIR": str(tmp_path), "COMMUNITYCAD_API_URL": "https://api.example.test"},
    )
    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
    assert tools == WORKING
