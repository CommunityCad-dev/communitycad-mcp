from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from mcp import Client

from communitycad_mcp.config import Settings
from communitycad_mcp.server import build_server
from communitycad_mcp.token_store import StoredToken, TokenStore

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://example.test/api"
V1 = API + "/v1"
TOKEN = "cct_example_secret_value"


def fixture(name: str) -> Any:
    for sub in ("recorded", "handwritten"):
        p = FIXTURES / sub / name
        if p.exists():
            return json.loads(p.read_text())
    raise FileNotFoundError(name)


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """Never open a real browser from tests; browser tests turn it back on themselves."""
    monkeypatch.setenv("COMMUNITYCAD_NO_BROWSER", "1")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_url=API,
        download_dir=tmp_path / "downloads",
        config_dir=tmp_path / "config",
        max_download_bytes=1024 * 1024,
    )


@pytest.fixture
def signed_in(settings: Settings) -> StoredToken:
    tok = StoredToken(access_token=TOKEN, username="example-user", scope="catalog:read download", token_id=42)
    TokenStore(settings.token_path).save(tok)
    return tok


@pytest.fixture
def api_mock():
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _no_sleep(_s: float) -> None:
    return None


class ToolRunner:
    def __init__(self, server):
        self.server = server

    async def call(self, name: str, args: dict | None = None) -> tuple[bool, Any]:
        async with Client(self.server) as c:
            r = await c.call_tool(name, args or {})
        text = r.content[0].text if r.content else ""
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        return r.is_error, data

    async def ok(self, name: str, args: dict | None = None) -> Any:
        is_error, data = await self.call(name, args)
        assert not is_error, data
        return data

    async def err(self, name: str, args: dict | None = None) -> dict:
        is_error, data = await self.call(name, args)
        assert is_error, data
        return data["error"]


@pytest.fixture
def run(settings: Settings, api_mock) -> ToolRunner:
    return ToolRunner(build_server(settings, sleep=_no_sleep))
