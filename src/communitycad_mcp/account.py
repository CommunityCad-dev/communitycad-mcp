"""Sign-in with the OAuth device authorization grant (RFC 8628).

MCP tool calls are request/response, so linking is two steps: the first
call starts the flow and returns the code and link; the next call (or the
same call with wait=True) polls until the person approves, denies, or the
code expires.
"""

from __future__ import annotations

import asyncio
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .api import SCOPES, UPLOAD_SCOPE, ApiClient
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
    # True for a scope upgrade of the saved token (no new token comes back).
    upgrade: bool = False
    with_uploads: bool = False


_FAILURES = {
    "access_denied": "The sign-in was declined on CommunityCAD. Nothing was linked.",
    "expired_token": "The code expired before it was approved. Run link_account again for a new one.",
    "invalid_grant": "That code is no longer valid. Run link_account again for a new one.",
}


# MCP clientInfo names of common AI apps, lower-cased, and how to show them.
KNOWN_APPS = {
    "claude-code": "Claude Code",
    "claude-ai": "Claude Desktop",
    "claude desktop": "Claude Desktop",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
    "cursor-vscode": "Cursor",
    "visual studio code": "VS Code",
    "vscode": "VS Code",
    "windsurf": "Windsurf",
    "windsurf-client": "Windsurf",
    "zed": "Zed",
    "cline": "Cline",
    "continue": "Continue",
    "goose": "Goose",
    "mcp-inspector": "MCP Inspector",
    "inspector-client": "MCP Inspector",
}
FALLBACK_APP = "AI app"
MAX_DEVICE_NAME = 100
_UNSAFE = re.compile(r"[^\w .()+-]+")


def app_name(client_name: str | None) -> str:
    """A readable name for the AI app from its MCP clientInfo name."""
    raw = (client_name or "").strip()
    if raw.lower() in KNOWN_APPS:
        return KNOWN_APPS[raw.lower()]
    cleaned = " ".join(_UNSAFE.sub(" ", raw).split())[:40]
    # The SDKs' default names say nothing about the app.
    if not cleaned or cleaned.lower() in ("mcp", "client", "mcp-client", "python-sdk"):
        return FALLBACK_APP
    return cleaned


def device_name(client_name: str | None, hostname: str | None = None) -> str:
    """'<App> on <computer>', at most 100 characters."""
    host = hostname if hostname is not None else socket.gethostname()
    host = " ".join(_UNSAFE.sub(" ", host.split(".")[0]).split())
    name = app_name(client_name)
    full = f"{name} on {host}" if host else name
    return full[:MAX_DEVICE_NAME].rstrip()


def _has_upload_scope(scope: str | None) -> bool:
    return UPLOAD_SCOPE in (scope or "").split()


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

    def _waiting(self, p: _Pending, note: str = "") -> dict[str, Any]:
        """The answer while the code is still valid and not yet approved. Never an error."""
        return {
            "status": "pending",
            "waiting_for_approval": True,
            **self._code_info(p),
            "message": (
                f"Still waiting for approval: code {p.user_code} hasn't been approved yet.{note} "
                f"Ask the user to open {p.verification_uri_complete} (signed in to CommunityCAD) and approve it, "
                "then call link_account again to finish."
            ),
        }

    async def link(
        self,
        *,
        wait: bool = False,
        max_wait_s: float = 50.0,
        device_name: str | None = None,
        allow_uploads: bool = False,
    ) -> dict[str, Any]:
        existing = self.store.load()
        if self._pending is not None and self._clock() >= self._pending.expires_at:
            self._pending = None
        if existing is not None:
            if not allow_uploads:
                if self._pending is not None and self._pending.upgrade:
                    return await self._poll(wait=wait, max_wait_s=max_wait_s)
                return {
                    "status": "already_linked",
                    "username": existing.username,
                    "uploads_allowed": _has_upload_scope(existing.scope),
                    "message": f"Already signed in as {existing.username}. Run unlink_account first to switch accounts.",
                }
            return await self._upgrade(existing, wait=wait, max_wait_s=max_wait_s)
        if self._pending is not None and (self._pending.upgrade or (allow_uploads and not self._pending.with_uploads)):
            self._pending = None  # a different request than the one waiting
        if self._pending is None:
            scope = f"{SCOPES} {UPLOAD_SCOPE}" if allow_uploads else SCOPES
            data = await self.api.device_code(device_name=device_name, scope=scope)
            self._pending = self._pending_from(data, with_uploads=allow_uploads)
            if not wait:
                return self._started(self._pending)
        return await self._poll(wait=wait, max_wait_s=max_wait_s)

    def _pending_from(self, data: dict[str, Any], *, upgrade: bool = False, with_uploads: bool = False) -> _Pending:
        return _Pending(
            device_code=data["device_code"],
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            verification_uri_complete=data.get("verification_uri_complete") or data["verification_uri"],
            expires_at=self._clock() + int(data.get("expires_in") or 600),
            interval=max(1, int(data.get("interval") or 5)),
            upgrade=upgrade,
            with_uploads=with_uploads or upgrade,
        )

    def _started(self, p: _Pending) -> dict[str, Any]:
        what = "allow uploads from this app" if p.upgrade else "approve"
        return {
            "status": "pending",
            **self._code_info(p),
            "message": (
                f"Ask the user to open {p.verification_uri_complete} (signed in to CommunityCAD) "
                f"and {what} with code {p.user_code}. Then call link_account again to finish."
            ),
        }

    async def _upgrade(self, existing: StoredToken, *, wait: bool, max_wait_s: float) -> dict[str, Any]:
        """Add the upload permission to the saved token (the user approves it on the site)."""
        if self._pending is None or not self._pending.upgrade:
            self._pending = None
            data = await self.api.request_scopes(UPLOAD_SCOPE)
            if data.get("status") == "granted":
                scope = data.get("scope") or existing.scope
                self._save_scope(existing, scope)
                return {
                    "status": "uploads_allowed",
                    "username": existing.username,
                    "scope": scope,
                    "message": f"Uploads are already allowed for {existing.username}.",
                }
            self._pending = self._pending_from(data, upgrade=True)
            if not wait:
                return self._started(self._pending)
        return await self._poll(wait=wait, max_wait_s=max_wait_s)

    def _save_scope(self, existing: StoredToken, scope: str | None) -> None:
        self.store.save(
            StoredToken(
                access_token=existing.access_token,
                username=existing.username,
                scope=scope,
                token_id=existing.token_id,
                api_url=existing.api_url,
            )
        )

    async def _poll(self, *, wait: bool, max_wait_s: float) -> dict[str, Any]:
        p = self._pending
        assert p is not None
        waited = 0.0  # counted in poll intervals, so a test sleep that returns at once still ends
        while True:
            try:
                error, body = await self.api.device_token(p.device_code)
            except CommunityCADError as exc:
                if exc.code not in ("upstream_unavailable", "rate_limited") or self._clock() >= p.expires_at:
                    raise
                # A check that didn't get through doesn't end the sign-in: the code is still valid.
                return self._waiting(p, " The last check didn't get through, so call again in a moment.")
            if error is None and p.upgrade:
                self._pending = None
                existing = self.store.load()
                if existing is None:
                    raise CommunityCADError("not_signed_in", "Signed out while waiting. Run link_account again.")
                self._save_scope(existing, body.get("scope"))
                allowed = _has_upload_scope(body.get("scope"))
                return {
                    "status": "uploads_allowed" if allowed else "linked",
                    "username": existing.username,
                    "scope": body.get("scope"),
                    "message": (
                        f"Uploads are now allowed for {existing.username}."
                        if allowed
                        else "The approval finished, but upload permission wasn't granted."
                    ),
                }
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
                uploads = " Uploads are allowed." if _has_upload_scope(body.get("scope")) else ""
                return {
                    "status": "linked",
                    "username": body.get("username"),
                    "scope": body.get("scope"),
                    "message": f"Signed in to CommunityCAD as {body.get('username')}.{uploads}",
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
            if not wait or waited + p.interval > max_wait_s:
                return self._waiting(p)
            await self._sleep(p.interval)
            waited += p.interval

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
