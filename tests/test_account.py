import asyncio
import json
import os

import httpx
import pytest

from communitycad_mcp.token_store import TokenStore

from .conftest import TOKEN, V1, fixture


def oauth_error(error, **extra):
    return httpx.Response(400, json={"error": error, "error_description": "x", **extra})


async def test_link_two_step_then_unlink(run, api_mock, settings):
    code = api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").mock(
        side_effect=[oauth_error("authorization_pending"), httpx.Response(200, json=fixture("device_token.json"))]
    )
    first = await run.ok("link_account")
    assert first["status"] == "pending" and first["user_code"] == "ABCD-EFGH"
    assert "link?code=ABCD-EFGH" in first["verification_uri_complete"]
    body = json.loads(code.calls[0].request.content)
    assert body["client_name"] == "communitycad-mcp" and body["scope"] == "catalog:read download"

    done = await run.ok("link_account", {"wait": True})
    assert done == {
        "status": "linked",
        "username": "example-user",
        "scope": "catalog:read download",
        "message": "Signed in as example-user. You can close the browser tab.",
    }
    assert TOKEN not in json.dumps(first) + json.dumps(done)
    if os.name != "nt":  # Windows has no POSIX modes; the per-user config dir protects it
        assert settings.token_path.stat().st_mode & 0o777 == 0o600
        assert settings.token_path.parent.stat().st_mode & 0o777 == 0o700

    again = await run.ok("link_account")
    assert again["status"] == "already_linked"

    revoke = api_mock.delete(f"{V1}/auth/tokens/current").respond(204)
    out = await run.ok("unlink_account")
    assert out["revoked_on_server"] is True
    assert revoke.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert not settings.token_path.exists()


async def test_slow_down_increases_interval(run, api_mock, settings):
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    token = api_mock.post(f"{V1}/auth/device/token").mock(
        side_effect=[
            oauth_error("slow_down", interval=10),
            oauth_error("authorization_pending"),
            httpx.Response(200, json=fixture("device_token.json")),
        ]
    )
    out = await run.ok("link_account", {"wait": True})
    assert out["status"] == "linked" and token.call_count == 3


async def test_denied(run, api_mock, settings):
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(400, json={"error": "access_denied"})
    err = await run.err("link_account", {"wait": True})
    assert err["code"] == "not_signed_in" and "declined" in err["message"]
    assert not settings.token_path.exists()


async def test_expired(run, api_mock):
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(400, json={"error": "expired_token"})
    err = await run.err("link_account", {"wait": True})
    assert "expired" in err["message"]


async def test_account_not_allowed_passes_reason(run, api_mock):
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(
        400,
        json={"error": "account_not_eligible", "error_description": "Not open yet.", "request_url": "https://example.test/r"},
    )
    err = await run.err("link_account", {"wait": True})
    assert err == {"code": "forbidden", "message": "Not open yet.", "request_url": "https://example.test/r"}


async def test_unlink_when_server_already_forgot(run, api_mock, settings, signed_in):
    api_mock.delete(f"{V1}/auth/tokens/current").respond(401, json={"detail": "Invalid or revoked token"})
    out = await run.ok("unlink_account")
    assert out["status"] == "unlinked"
    assert TokenStore(settings.token_path).load() is None


async def test_unlink_not_linked(run):
    assert (await run.ok("unlink_account"))["status"] == "not_linked"


async def test_account_status_signed_in(run, api_mock, signed_in):
    route = api_mock.get(f"{V1}/account/status").respond(200, json=fixture("account_status_signed_in.json"))
    out = await run.ok("account_status")
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert out["signed_in"] and out["plan"] == "free" and out["daily_remaining"] == 9
    assert out["token"]["scopes"] == ["catalog:read", "download"]
    assert out["message"] == "Signed in as example-user (free plan). 9 of 10 downloads left today, 29 of 30 this week."
    assert TOKEN not in json.dumps(out)


async def test_account_status_signed_out(run, api_mock):
    route = api_mock.get(f"{V1}/account/status").respond(200, json=fixture("account_status_signed_out.json"))
    out = await run.ok("account_status")
    assert "Authorization" not in route.calls[0].request.headers
    assert out["signed_in"] is False and out["daily_limit"] == 5 and out["weekly_limit"] == 10
    assert out["message"] == "Not signed in; the signed-out limit applies. 5 of 5 downloads left today, 9 of 10 this week."
    # 5 of 5 left: no nudge to sign up.
    assert not {"sign_up_url", "plans_url", "account_hint"} & out.keys()
    assert "link_account" not in json.dumps(out)


async def test_account_status_signed_out_hint_near_limit(run, api_mock):
    data = {**fixture("account_status_signed_out.json"), "daily_remaining": 1, "account_hint": "A free account gets more."}
    api_mock.get(f"{V1}/account/status").respond(200, json=data)
    out = await run.ok("account_status")
    assert out["account_hint"] == "A free account gets more." and out["sign_up_url"].endswith("/register")
    assert "link_account" not in out["message"]


async def test_account_status_fallback_until_route_exists(run, api_mock, signed_in):
    api_mock.get(f"{V1}/account/status").respond(404, json={"detail": "Not Found"})
    out = await run.ok("account_status")
    assert out["signed_in"] and out["username"] == "example-user"


async def test_account_status_signed_out_fallback(run, api_mock):
    api_mock.get(f"{V1}/account/status").respond(404, json={"detail": "Not Found"})
    out = await run.ok("account_status")
    assert out == {"signed_in": False, "message": "Not signed in; the signed-out limit applies."}


async def _link_with_client(settings, api_mock, client_name):
    from mcp import Client
    from mcp.types import Implementation

    from communitycad_mcp.server import build_server

    route = api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    info = Implementation(name=client_name, version="1.0") if client_name is not None else None
    async with Client(build_server(settings), **({"client_info": info} if info else {})) as c:
        await c.call_tool("link_account", {})
    return json.loads(route.calls[-1].request.content)["device_name"]


async def test_device_name_from_client_info(settings, api_mock, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "DESKTOP-ABC.local")
    cases = {
        "claude-code": "Claude Code on DESKTOP-ABC",
        "claude-ai": "Claude Desktop on DESKTOP-ABC",
        "cursor-vscode": "Cursor on DESKTOP-ABC",
        "Visual Studio Code": "VS Code on DESKTOP-ABC",
        "my-agent": "my-agent on DESKTOP-ABC",
        "mcp": "AI app on DESKTOP-ABC",
        None: "AI app on DESKTOP-ABC",  # the SDK's default client name
    }
    for client_name, expected in cases.items():
        assert await _link_with_client(settings, api_mock, client_name) == expected, client_name


def test_device_name_limits():
    from communitycad_mcp.account import device_name

    assert device_name("claude-code", "x" * 300) == ("Claude Code on " + "x" * 300)[:100]
    assert device_name("<script>\nalert", "host") == "script alert on host"
    assert device_name("", "") == "AI app"


async def test_wait_runs_out_still_waiting(run, api_mock):
    """A code that's still pending when the wait runs out is not an error and says so."""
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(400, json={"error": "authorization_pending"})
    out = await run.ok("link_account", {"wait": True})
    assert out["status"] == "pending" and out["waiting_for_approval"] is True
    assert out["message"].startswith("[Approve CommunityCAD sign-in](https://example.test/link?code=ABCD-EFGH)")
    assert "Still waiting for approval" in out["message"]
    assert "link?code=ABCD-EFGH" in out["message"]
    again = await run.ok("link_account")
    assert again["status"] == "pending" and "upstream" not in json.dumps(again)


async def test_poll_hiccup_keeps_waiting(run, api_mock):
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").mock(
        side_effect=[
            httpx.ConnectError("x"),
            httpx.Response(503),
            httpx.Response(200, json=fixture("device_token.json")),
        ]
    )
    first = await run.ok("link_account", {"wait": True})
    assert first["status"] == "pending" and "Still waiting for approval" in first["message"]
    second = await run.ok("link_account", {"wait": True})
    assert second["status"] == "pending"
    assert (await run.ok("link_account"))["status"] == "linked"


SIGNED_OUT_NOTE = (
    "Your CommunityCAD sign-in was revoked or expired; you're signed out now. Run link_account to sign in again."
)


def revoked_then(body):
    """401 for a request with a token; `body` for one without."""

    def answer(request):
        if "Authorization" in request.headers:
            return httpx.Response(401, json={"detail": "Invalid or revoked token"})
        return httpx.Response(200, json=body)

    return answer


async def test_account_status_revoked_signs_out(run, api_mock, signed_in, settings):
    route = api_mock.get(f"{V1}/account/status").mock(side_effect=revoked_then(fixture("account_status_signed_out.json")))
    out = await run.ok("account_status")
    assert route.call_count == 2 and "Authorization" not in route.calls[1].request.headers
    assert TokenStore(settings.token_path).load() is None
    assert out["signed_in"] is False and out["note"] == SIGNED_OUT_NOTE
    assert out["message"].startswith(SIGNED_OUT_NOTE + " Not signed in; the signed-out limit applies.")
    # One time only: the next call is a plain signed-out answer.
    again = await run.ok("account_status")
    assert "note" not in again and route.call_count == 3


async def test_unlink_after_revoke_does_not_retry(run, api_mock, signed_in, settings):
    route = api_mock.delete(f"{V1}/auth/tokens/current").respond(401, json={"detail": "Invalid or revoked token"})
    out = await run.ok("unlink_account")
    assert route.call_count == 1 and out["status"] == "unlinked" and "note" not in out


async def test_account_status_signed_out_answer_to_a_token_signs_out(run, api_mock, signed_in, settings):
    # The status route may answer a dead token as signed out rather than with a 401.
    route = api_mock.get(f"{V1}/account/status").respond(200, json=fixture("account_status_signed_out.json"))
    out = await run.ok("account_status")
    assert route.call_count == 1 and route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TokenStore(settings.token_path).load() is None
    assert out["signed_in"] is False and out["note"] == SIGNED_OUT_NOTE


# -- upload permission ---------------------------------------------------------


UPGRADE = {
    "status": "approval_required",
    "scope": "catalog:read download",
    "added_scopes": ["models:write"],
    "device_code": "example-upgrade-code",
    "user_code": "WXYZ-2345",
    "verification_uri": "https://example.test/link",
    "verification_uri_complete": "https://example.test/link?code=WXYZ-2345&app=mcp",
    "expires_in": 600,
    "interval": 5,
}


async def test_link_with_uploads_asks_for_models_write(run, api_mock, settings):
    code = api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(
        200, json={**fixture("device_token.json"), "scope": "catalog:read download models:write"}
    )
    first = await run.ok("link_account", {"allow_uploads": True})
    assert first["status"] == "pending"
    assert json.loads(code.calls[0].request.content)["scope"] == "catalog:read download models:write"
    done = await run.ok("link_account", {"allow_uploads": True})
    assert done["status"] == "linked" and "Uploads are allowed" in done["message"]
    assert TokenStore(settings.token_path).load().scope == "catalog:read download models:write"


async def test_upgrade_when_already_linked(run, api_mock, settings, signed_in):
    up = api_mock.post(f"{V1}/auth/tokens/current/scopes").respond(200, json=UPGRADE)
    poll = api_mock.post(f"{V1}/auth/device/token").mock(
        side_effect=[
            oauth_error("authorization_pending"),
            httpx.Response(
                200,
                json={
                    "access_token": None,
                    "scope": "catalog:read download models:write",
                    "token_id": 42,
                    "username": "example-user",
                    "upgraded": True,
                },
            ),
        ]
    )
    first = await run.ok("link_account", {"allow_uploads": True})
    assert first["status"] == "pending" and first["user_code"] == "WXYZ-2345"
    assert "allow uploads" in first["message"]
    assert json.loads(up.calls[0].request.content) == {"scope": "models:write"}
    assert up.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"

    waiting = await run.ok("link_account", {"allow_uploads": True})
    assert waiting["status"] == "pending" and up.call_count == 1  # same code, not a new request
    done = await run.ok("link_account", {"allow_uploads": True})
    assert done["status"] == "uploads_allowed" and poll.call_count == 2
    saved = TokenStore(settings.token_path).load()
    assert saved.access_token == TOKEN and saved.scope == "catalog:read download models:write"
    assert (await run.ok("link_account"))["uploads_allowed"] is True


async def test_upgrade_already_granted(run, api_mock, settings, signed_in):
    api_mock.post(f"{V1}/auth/tokens/current/scopes").respond(
        200, json={"status": "granted", "scope": "catalog:read download models:write", "added_scopes": []}
    )
    poll = api_mock.post(f"{V1}/auth/device/token").respond(500)
    out = await run.ok("link_account", {"allow_uploads": True})
    assert out["status"] == "uploads_allowed" and not poll.called
    assert TokenStore(settings.token_path).load().scope.endswith("models:write")


async def test_scope_missing_says_how_to_allow_uploads():
    from communitycad_mcp.errors import from_response

    resp = httpx.Response(403, json={"error": "scope_missing", "detail": "Needs models:write."})
    err = from_response(resp, signed_in=True)
    assert err.code == "scope_missing" and "link_account(allow_uploads=true)" in err.message
    resp = httpx.Response(
        403, json={"detail": {"error": "insufficient_scope", "required": ["models:write"], "missing": ["models:write"]}}
    )
    assert "allow_uploads=true" in from_response(resp, signed_in=True).message


# -- one-click sign-in: browser, background completion --------------------------

LINK = "[Approve CommunityCAD sign-in](https://example.test/link?code=ABCD-EFGH)"


async def _yield(_s: float) -> None:
    await asyncio.sleep(0)


def _server(settings, **kw):
    from communitycad_mcp.server import build_server

    return build_server(settings, sleep=_yield, **kw)


async def _until(cond, tries: int = 500) -> None:
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


@pytest.fixture
def desktop(monkeypatch):
    """A Linux desktop session where opening a browser is allowed."""
    for name in ("COMMUNITYCAD_NO_BROWSER", "SSH_CONNECTION", "SSH_CLIENT", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("sys.platform", "linux")


@pytest.mark.parametrize(
    "env, platform, expected",
    [
        ({"DISPLAY": ":0"}, "linux", True),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", True),
        ({}, "linux", False),  # headless
        ({}, "win32", True),
        ({}, "darwin", True),
        ({"DISPLAY": ":0", "SSH_CONNECTION": "a b c d"}, "linux", False),
        ({"SSH_CONNECTION": "a b c d"}, "darwin", False),
        ({"COMMUNITYCAD_NO_BROWSER": "1"}, "win32", False),
        ({"COMMUNITYCAD_NO_BROWSER": "0", "DISPLAY": ":0"}, "linux", True),
    ],
)
def test_can_open_browser(env, platform, expected):
    from communitycad_mcp.browser import can_open

    assert can_open(env, platform) is expected


async def test_link_opens_browser_and_leads_with_link(run, api_mock, desktop, monkeypatch):
    opened = []
    monkeypatch.setattr("communitycad_mcp.browser._launch", lambda url: opened.append(url) or True)
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    out = await run.ok("link_account")
    assert opened == ["https://example.test/link?code=ABCD-EFGH"]
    assert out["status"] == "pending" and out["browser_opened"] is True
    assert out["message"].startswith(LINK + "\n\n")
    assert out["message"].index(LINK) < out["message"].index("Code: ABCD-EFGH")
    assert "clickable link" in out["message"] and "already open" in out["message"]


@pytest.mark.parametrize("env", [{"COMMUNITYCAD_NO_BROWSER": "1"}, {"SSH_CONNECTION": "a b c d"}, {"DISPLAY": ""}])
async def test_link_skips_browser_when_headless(run, api_mock, desktop, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("communitycad_mcp.browser._launch", lambda url: pytest.fail("browser opened"))
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    out = await run.ok("link_account")
    assert out["browser_opened"] is False and out["message"].startswith(LINK)
    assert "already open" not in out["message"]


async def test_browser_failure_is_not_an_error(run, api_mock, desktop, monkeypatch):
    def boom(url):
        raise OSError("no browser")

    monkeypatch.setattr("communitycad_mcp.browser._launch", boom)
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    out = await run.ok("link_account")
    assert out["status"] == "pending" and out["browser_opened"] is False


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shell script as the browser")
async def test_browser_launch_writes_nothing_to_stdout(run, api_mock, desktop, monkeypatch, tmp_path, capfd):
    """stdout is the MCP channel: a browser that prints when started must not reach it."""
    seen = tmp_path / "opened.txt"
    fake = tmp_path / "fake-browser"
    fake.write_text(f'#!/bin/sh\necho "stdout noise $1"\necho "stderr noise" >&2\nprintf %s "$1" > "{seen}"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("BROWSER", str(fake))
    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    out = await run.ok("link_account")
    assert out["browser_opened"] is True
    assert seen.read_text() == "https://example.test/link?code=ABCD-EFGH"
    captured = capfd.readouterr()
    assert captured.out == "" and "noise" not in captured.err


async def test_background_completion_signs_in_without_done(settings, api_mock):
    from mcp import Client

    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    poll = api_mock.post(f"{V1}/auth/device/token").mock(
        side_effect=[oauth_error("authorization_pending"), httpx.Response(200, json=fixture("device_token.json"))]
    )
    api_mock.get(f"{V1}/account/status").respond(200, json=fixture("account_status_signed_in.json"))
    server = _server(settings, poll_sleep=_yield)
    store = TokenStore(settings.token_path)
    async with Client(server) as c:
        first = json.loads((await c.call_tool("link_account", {})).content[0].text)
        assert first["status"] == "pending"
        await _until(lambda: store.load() is not None)
        assert store.load().access_token == TOKEN and poll.call_count == 2
        status = json.loads((await c.call_tool("account_status", {})).content[0].text)
        assert status["signed_in"] is True
        done = json.loads((await c.call_tool("link_account", {})).content[0].text)
        assert done["status"] == "linked"
        assert done["message"] == "Signed in as example-user. You can close the browser tab."
        again = json.loads((await c.call_tool("link_account", {})).content[0].text)
        assert again["status"] == "already_linked"
    assert poll.call_count == 2


async def test_pending_recall_returns_same_code(run, api_mock):
    code = api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(400, json={"error": "authorization_pending"})
    first = await run.ok("link_account")
    second = await run.ok("link_account")
    assert code.call_count == 1
    assert second["user_code"] == first["user_code"] == "ABCD-EFGH"
    assert second["verification_uri_complete"] == first["verification_uri_complete"]
    assert second["status"] == "pending" and second["message"].startswith(LINK)


async def test_background_expiry_then_new_code(settings, api_mock):
    from mcp import Client

    now = [0.0]

    async def tick(s: float) -> None:
        now[0] += s
        await asyncio.sleep(0)

    code = api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(400, json={"error": "authorization_pending"})
    server = _server(settings, poll_sleep=tick, clock=lambda: now[0])
    async with Client(server) as c:
        await c.call_tool("link_account", {})
        await _until(lambda: server.linker._task is not None and server.linker._task.done(), tries=5000)
        assert server.linker.pending_info() is None and now[0] >= 600
        fresh = json.loads((await c.call_tool("link_account", {})).content[0].text)
    assert code.call_count == 2 and fresh["status"] == "pending"
    assert "expired" in fresh["previous_attempt"]
    assert not settings.token_path.exists()


async def test_background_denied_reported_on_next_link(settings, api_mock):
    from mcp import Client

    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.post(f"{V1}/auth/device/token").respond(400, json={"error": "access_denied"})
    server = _server(settings, poll_sleep=_yield)
    async with Client(server) as c:
        await c.call_tool("link_account", {})
        await _until(lambda: server.linker.pending_info() is None)
        fresh = json.loads((await c.call_tool("link_account", {})).content[0].text)
    assert "declined" in fresh["previous_attempt"] and not settings.token_path.exists()


async def test_account_status_shows_pending_link(settings, api_mock):
    from mcp import Client

    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    api_mock.get(f"{V1}/account/status").respond(200, json=fixture("account_status_signed_out.json"))
    async with Client(_server(settings)) as c:  # background sleeps for real: nothing polls here
        await c.call_tool("link_account", {})
        out = json.loads((await c.call_tool("account_status", {})).content[0].text)
    assert out["sign_in_pending"]["user_code"] == "ABCD-EFGH"
    assert out["message"].endswith(LINK)


async def test_unlink_and_shutdown_cancel_background(settings, api_mock):
    from mcp import Client

    api_mock.post(f"{V1}/auth/device/code").respond(200, json=fixture("device_code.json"))
    server = _server(settings)
    async with Client(server) as c:
        await c.call_tool("link_account", {})
        task = server.linker._task
        assert task is not None and not task.done()
        out = json.loads((await c.call_tool("unlink_account", {})).content[0].text)
        assert out["status"] == "not_linked" and "Cancelled" in out["message"]
        assert task.cancelled() and server.linker.pending_info() is None
        await c.call_tool("link_account", {})
        task = server.linker._task
        assert task is not None and not task.done()
    # Leaving the session (shutdown) stops the background check.
    assert task.cancelled() and server.linker._task is None
