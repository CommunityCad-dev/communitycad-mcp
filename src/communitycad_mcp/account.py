"""Sign-in with the OAuth device authorization grant (RFC 8628).

MCP tool calls are request/response, so linking is two steps: the first
call starts the flow and returns the code and link; the next call (or the
same call with wait=True) polls until the person approves, denies, or the
code expires.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .api import ApiClient
from .errors import CommunityCADError
from .token_store import StoredToken, TokenStore


@dataclass
class _Pending:
    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_at: float
    interval: int


_FAILURES = {
    "access_denied": "The sign-in was declined on CommunityCAD. Nothing was linked.",
    "expired_token": "The code expired before it was approved. Run link_account again for a new one.",
    "invalid_grant": "That code is no longer valid. Run link_account again for a new one.",
}


class AccountLinker:
    def __init__(
        self,
        api: ApiClient,
        store: TokenStore,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.store = store
        self._sleep = sleep
        self._clock = clock
        self._pending: _Pending | None = None

    def _code_info(self, p: _Pending) -> dict[str, Any]:
        return {
            "user_code": p.user_code,
            "verification_uri": p.verification_uri,
            "verification_uri_complete": p.verification_uri_complete,
            "expires_in_s": max(0, int(p.expires_at - self._clock())),
        }

    async def link(self, *, wait: bool = False, max_wait_s: float = 120.0, device_name: str | None = None) -> dict[str, Any]:
        existing = self.store.load()
        if existing is not None:
            return {
                "status": "already_linked",
                "username": existing.username,
                "message": f"Already signed in as {existing.username}. Run unlink_account first to switch accounts.",
            }
        if self._pending is not None and self._clock() >= self._pending.expires_at:
            self._pending = None
        if self._pending is None:
            data = await self.api.device_code(device_name=device_name)
            self._pending = _Pending(
                device_code=data["device_code"],
                user_code=data["user_code"],
                verification_uri=data["verification_uri"],
                verification_uri_complete=data.get("verification_uri_complete") or data["verification_uri"],
                expires_at=self._clock() + int(data.get("expires_in", 600)),
                interval=max(1, int(data.get("interval", 5))),
            )
            if not wait:
                return {
                    "status": "pending",
                    **self._code_info(self._pending),
                    "message": (
                        f"Ask the user to open {self._pending.verification_uri_complete} (signed in to CommunityCAD) "
                        f"and approve code {self._pending.user_code}. Then call link_account again to finish."
                    ),
                }
        return await self._poll(wait=wait, max_wait_s=max_wait_s)

    async def _poll(self, *, wait: bool, max_wait_s: float) -> dict[str, Any]:
        p = self._pending
        assert p is not None
        deadline = self._clock() + max_wait_s
        while True:
            error, body = await self.api.device_token(p.device_code)
            if error is None:
                token = body.get("access_token")
                if not token:
                    self._pending = None
                    raise CommunityCADError("upstream_unavailable", "Sign-in finished without a token. Try link_account again.")
                self.store.save(
                    StoredToken(
                        access_token=token,
                        username=body.get("username"),
                        scope=body.get("scope"),
                        token_id=body.get("token_id"),
                        api_url=self.api.settings.api_url,
                    )
                )
                self._pending = None
                return {
                    "status": "linked",
                    "username": body.get("username"),
                    "scope": body.get("scope"),
                    "message": f"Signed in to CommunityCAD as {body.get('username')}.",
                }
            if error == "slow_down":
                p.interval = int(body.get("interval") or p.interval + 5)
            elif error != "authorization_pending":
                self._pending = None
                reason = _FAILURES.get(error) or body.get("error_description") or "Sign-in failed."
                extra = {"request_url": body["request_url"]} if isinstance(body.get("request_url"), str) else {}
                raise CommunityCADError("forbidden" if error not in _FAILURES else "not_signed_in", reason, **extra)
            now = self._clock()
            if now >= p.expires_at:
                self._pending = None
                raise CommunityCADError("not_signed_in", _FAILURES["expired_token"])
            if not wait or now + p.interval > deadline:
                return {
                    "status": "pending",
                    **self._code_info(p),
                    "message": f"Still waiting for the user to approve code {p.user_code}. Call link_account again once they have.",
                }
            await self._sleep(p.interval)

    async def unlink(self) -> dict[str, Any]:
        existing = self.store.load()
        if existing is None:
            return {"status": "not_linked", "message": "No CommunityCAD account is linked."}
        revoked = True
        try:
            await self.api.revoke_current_token()
        except CommunityCADError as exc:
            if exc.code != "not_signed_in":  # 401: already revoked on the server
                if exc.code == "upstream_unavailable":
                    revoked = False
                else:
                    raise
        self.store.delete()
        msg = "Unlinked: the sign-in was revoked on CommunityCAD and deleted from this computer."
        if not revoked:
            msg = (
                "Deleted the sign-in from this computer, but couldn't reach CommunityCAD to revoke it. "
                "Remove it from the linked devices list in the account settings on the website."
            )
        return {"status": "unlinked", "revoked_on_server": revoked, "message": msg}
