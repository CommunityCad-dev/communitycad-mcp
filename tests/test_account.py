import json

import httpx

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
        "message": "Signed in to CommunityCAD as example-user.",
    }
    assert TOKEN not in json.dumps(first) + json.dumps(done)
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
    assert out["message"].startswith("Still waiting for approval")
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
    assert first["status"] == "pending" and first["message"].startswith("Still waiting for approval")
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
