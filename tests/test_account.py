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
