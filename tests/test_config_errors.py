from pathlib import Path

import httpx

from communitycad_mcp.config import DEFAULT_API_URL, Settings
from communitycad_mcp.errors import CommunityCADError, from_response
from communitycad_mcp.token_store import StoredToken, TokenStore

from .conftest import TOKEN, fixture


def test_defaults():
    s = Settings.from_env({})
    assert s.api_url == DEFAULT_API_URL == "https://communitycad.dev/api"
    assert s.download_dir == Path("~/CommunityCAD").expanduser()
    assert s.model_url("abc") == "https://communitycad.dev/m/abc"


def test_overrides(tmp_path):
    s = Settings.from_env(
        {
            "COMMUNITYCAD_API_URL": "https://api.example.test/",
            "COMMUNITYCAD_DOWNLOAD_DIR": str(tmp_path / "d"),
            "COMMUNITYCAD_CONFIG_DIR": str(tmp_path / "c"),
            "COMMUNITYCAD_MAX_DOWNLOAD_MB": "2",
        }
    )
    assert s.api_url == "https://api.example.test"
    assert s.download_dir == tmp_path / "d"
    assert s.token_path == tmp_path / "c" / "token.json"
    assert s.max_download_bytes == 2 * 1024 * 1024


def resp(status, body=None, headers=None):
    return httpx.Response(status, json=body, headers=headers)


def test_error_mapping():
    cases = [
        (resp(401, {"detail": "Invalid or revoked token"}), True, False, "not_signed_in"),
        (resp(401, {"detail": "Not authenticated"}), False, True, "sign_in_required_for_download"),
        (resp(401, {"detail": "Not authenticated"}), False, False, "not_signed_in"),
        (resp(403, {"detail": {"error": "insufficient_scope", "missing": ["download"]}}), True, True, "scope_missing"),
        (resp(403, {"detail": "Account suspended"}), True, False, "forbidden"),
        (resp(404, {"detail": "Model not found"}), False, False, "model_not_found"),
        (resp(404, {"detail": "Not Found"}), False, False, "not_available_yet"),
        (resp(410, {"detail": "Removed"}), False, False, "model_unavailable"),
        (resp(413, {}), False, False, "file_too_large"),
        (resp(429, fixture("download_limit_reached.json")), True, True, "download_limit_reached"),
        (resp(429, {"error": "issue_cap_reached", "detail": "x"}, {"Retry-After": "60"}), True, True, "rate_limited"),
        (resp(422, {"detail": [{"msg": "bad"}]}), False, False, "invalid_request"),
        (resp(502, None), False, False, "upstream_unavailable"),
    ]
    for r, signed_in, download, code in cases:
        assert from_response(r, signed_in=signed_in, download=download).code == code


def test_limit_error_carries_reset_and_links():
    e = from_response(resp(429, fixture("download_limit_reached.json")), signed_in=False, download=True)
    d = e.to_dict()["error"]
    assert d["reset_at"] and d["upgrade_url"] and d["sign_up_url"] and d["account_hint"]
    assert d["daily_remaining"] == 0


def test_token_never_in_reprs(tmp_path):
    tok = StoredToken(access_token=TOKEN, username="example-user")
    store = TokenStore(tmp_path / "t.json")
    store.save(tok)
    for text in (repr(tok), str(tok), repr(store), repr(CommunityCADError("not_signed_in", "x"))):
        assert TOKEN not in text
    assert (tmp_path / "t.json").stat().st_mode & 0o777 == 0o600
    assert store.load().access_token == TOKEN
