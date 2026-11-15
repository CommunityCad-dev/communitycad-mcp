"""Sign-in with the OAuth device authorization grant (RFC 8628).

The first link_account call gets a code, opens the approval page in the
browser when it can, and returns a clickable link at once. A background task
then polls at the server's interval until the person approves, declines, or
the code expires, and saves the token, so the next tool call is signed in
without anyone saying "done". link_account(wait=True) still polls in the
foreground; both share one lock, so only one check runs at a time.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import browser
from .api import SCOPES, UPLOAD_SCOPE, ApiClient
from .errors import CommunityCADError
from .token_store import StoredToken, TokenStore

log = logging.getLogger(__name__)


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
    browser_opened: bool = False
    # How it ended, for a poller that was waiting on the lock while another finished it.
    result: dict[str, Any] | None = field(default=None, repr=False)
    error: CommunityCADError | None = field(default=None, repr=False)


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
        poll_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.store = store
        self._sleep = sleep  # between foreground checks (wait=True)
        self._poll_sleep = poll_sleep  # between background checks
        self._clock = clock
        self._pending: _Pending | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._task_for: _Pending | None = None
        # A sign-in the background task finished that no link_account result has shown yet.
        self._unreported: dict[str, Any] | None = None
        # Why the last background sign-in ended without one (declined, expired).
        self._last_failure: str | None = None

    # -- answers -----------------------------------------------------------------

    def _code_info(self, p: _Pending) -> dict[str, Any]:
        return {
            "user_code": p.user_code,
            "verification_uri": p.verification_uri,
            "verification_uri_complete": p.verification_uri_complete,
            "expires_in_s": max(0, int(p.expires_at - self._clock())),
            "browser_opened": p.browser_opened,
        }

    def _link_message(self, p: _Pending, lead: str) -> str:
        label = "Approve CommunityCAD upload permission" if p.upgrade else "Approve CommunityCAD sign-in"
        opened = "It's already open in the user's browser. " if p.browser_opened else ""
        return (
            f"[{label}]({p.verification_uri_complete})\n\n"
            f"{lead}Code: {p.user_code} (if the link doesn't open, go to {p.verification_uri} and enter it).\n\n"
            f"Show the user the link above as a clickable link. {opened}"
            "Sign-in finishes on its own once they approve: they don't need to tell you, and the next "
            "CommunityCAD tool call sees it. link_account or account_status shows where it stands."
        )

    def _started(self, p: _Pending) -> dict[str, Any]:
        lead = (
            "The user approves this to allow uploads from this app. "
            if p.upgrade
            else "The user approves this while signed in to CommunityCAD. "
        )
        out = {"status": "pending", **self._code_info(p), "message": self._link_message(p, lead)}
        if self._last_failure:
            out["previous_attempt"] = self._last_failure
            self._last_failure = None
        return out

    def _waiting(self, p: _Pending, note: str = "") -> dict[str, Any]:
        """The answer while the code is still valid and not yet approved. Never an error."""
        return {
            "status": "pending",
            "waiting_for_approval": True,
            **self._code_info(p),
            "message": self._link_message(p, f"Still waiting for approval of code {p.user_code}.{note} "),
        }

    def pending_info(self) -> dict[str, Any] | None:
        """The sign-in waiting for approval, if any (for account_status)."""
        p = self._pending
        if p is None or self._clock() >= p.expires_at:
            return None
        label = "Approve CommunityCAD upload permission" if p.upgrade else "Approve CommunityCAD sign-in"
        return {**self._code_info(p), "link": f"[{label}]({p.verification_uri_complete})"}

    # -- linking -----------------------------------------------------------------

    async def link(
        self,
        *,
        wait: bool = False,
        max_wait_s: float = 50.0,
        device_name: str | None = None,
        allow_uploads: bool = False,
    ) -> dict[str, Any]:
        if self._pending is not None and self._clock() >= self._pending.expires_at:
            self._drop_pending()
            self._last_failure = _FAILURES["expired_token"]
        existing = self.store.load()
        if self._unreported is not None:
            done, self._unreported = self._unreported, None
            if existing is not None and (not allow_uploads or _has_upload_scope(existing.scope)):
                return done
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
            self._drop_pending()  # a different request than the one waiting
        if self._pending is None:
            scope = f"{SCOPES} {UPLOAD_SCOPE}" if allow_uploads else SCOPES
            data = await self.api.device_code(device_name=device_name, scope=scope)
            p = self._pending = self._pending_from(data, with_uploads=allow_uploads)
            if not wait:
                return await self._begin(p)
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

    async def _begin(self, p: _Pending) -> dict[str, Any]:
        """A new code: start checking in the background, open the browser, answer at once."""
        self._start_background(p)
        p.browser_opened = await browser.open_url(p.verification_uri_complete)
        return self._started(p)

    async def _upgrade(self, existing: StoredToken, *, wait: bool, max_wait_s: float) -> dict[str, Any]:
        """Add the upload permission to the saved token (the user approves it on the site)."""
        if self._pending is None or not self._pending.upgrade:
            self._drop_pending()
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
            p = self._pending = self._pending_from(data, upgrade=True)
            if not wait:
                return await self._begin(p)
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
            result, note = await self._step(p)
            if result is not None:
                self._unreported = None  # shown now
                return result
            if note or not wait or waited + p.interval > max_wait_s:
                self._start_background(p)
                return self._waiting(p, note)
            await self._sleep(p.interval)
            waited += p.interval

    async def _step(self, p: _Pending, *, background: bool = False) -> tuple[dict[str, Any] | None, str]:
        """One check of the token endpoint. Returns (final result or None, note).
        Raises CommunityCADError when the sign-in ended without a token."""
        async with self._lock:
            if self._pending is not p:  # finished (or replaced) while this waited for the lock
                if background:
                    return None, ""
                if p.result is not None:
                    return p.result, ""
                if p.error is not None:
                    raise p.error
                raise CommunityCADError("not_signed_in", "That sign-in was cancelled. Run link_account again.")
            try:
                error, body = await self.api.device_token(p.device_code)
            except CommunityCADError as exc:
                if exc.code not in ("upstream_unavailable", "rate_limited") or self._clock() >= p.expires_at:
                    self._fail(p, exc)
                    raise
                # A check that didn't get through doesn't end the sign-in: the code is still valid.
                return None, " The last check didn't get through; it will be tried again."
            if error is None:
                return self._finish(p, body), ""
            if error == "slow_down":
                p.interval = int(body.get("interval") or p.interval + 5)
            elif error != "authorization_pending":
                reason = _FAILURES.get(error) or body.get("error_description") or "Sign-in failed."
                extra = {"request_url": body["request_url"]} if isinstance(body.get("request_url"), str) else {}
                exc = CommunityCADError("forbidden" if error not in _FAILURES else "not_signed_in", reason, **extra)
                self._fail(p, exc)
                raise exc
            if self._clock() >= p.expires_at:
                exc = CommunityCADError("not_signed_in", _FAILURES["expired_token"])
                self._fail(p, exc)
                raise exc
            return None, ""

    def _fail(self, p: _Pending, exc: CommunityCADError) -> None:
        p.error = exc
        if self._pending is p:
            self._pending = None

    def _finish(self, p: _Pending, body: dict[str, Any]) -> dict[str, Any]:
        """Save what an approval returned and build the answer."""
        if p.upgrade:
            existing = self.store.load()
            if existing is None:
                exc = CommunityCADError("not_signed_in", "Signed out while waiting. Run link_account again.")
                self._fail(p, exc)
                raise exc
            self._save_scope(existing, body.get("scope"))
            allowed = _has_upload_scope(body.get("scope"))
            result = {
                "status": "uploads_allowed" if allowed else "linked",
                "username": existing.username,
                "scope": body.get("scope"),
                "message": (
                    f"Uploads are now allowed for {existing.username}. You can close the browser tab."
                    if allowed
                    else "The approval finished, but upload permission wasn't granted. You can close the browser tab."
                ),
            }
        else:
            token = body.get("access_token")
            if not token:
                exc = CommunityCADError("upstream_unavailable", "Sign-in finished without a token. Try link_account again.")
                self._fail(p, exc)
                raise exc
            self.store.save(
                StoredToken(
                    access_token=token,
                    username=body.get("username"),
                    scope=body.get("scope"),
                    token_id=body.get("token_id"),
                    api_url=self.api.settings.api_url,
                )
            )
            uploads = " Uploads are allowed." if _has_upload_scope(body.get("scope")) else ""
            result = {
                "status": "linked",
                "username": body.get("username"),
                "scope": body.get("scope"),
                "message": f"Signed in as {body.get('username')}.{uploads} You can close the browser tab.",
            }
        p.result = result
        self._pending = None
        self._unreported = result
        return result

    # -- background --------------------------------------------------------------

    def _start_background(self, p: _Pending) -> None:
        """Keep checking `p` until it ends, unless a task already does."""
        if self._task is not None and not self._task.done():
            if self._task_for is p:
                return
            self._task.cancel()
        self._task_for = p
        self._task = asyncio.get_running_loop().create_task(self._run_background(p))

    async def _run_background(self, p: _Pending) -> None:
        while self._pending is p:
            await self._poll_sleep(p.interval)
            if self._pending is not p:
                return
            try:
                await self._step(p, background=True)
            except CommunityCADError as exc:
                self._last_failure = exc.message
                return
            except Exception:  # noqa: BLE001 - keep trying until the code expires
                log.debug("Background sign-in check failed", exc_info=True)

    def _drop_pending(self) -> None:
        self._pending = None
        if self._task is not None:
            self._task.cancel()

    async def stop_background(self) -> None:
        """Cancel the background check and wait for it to finish (unlink, shutdown).
        A code that is still valid stays pending; the next link_account resumes checking."""
        task, self._task, self._task_for = self._task, None, None
        if task is not None and not task.done():
            task.cancel()
            # Collects the task's CancelledError; a cancel of this caller still propagates.
            await asyncio.gather(task, return_exceptions=True)

    async def unlink(self) -> dict[str, Any]:
        had_pending = self._pending is not None
        await self.stop_background()
        self._pending = None
        self._unreported = None
        self._last_failure = None
        existing = self.store.load()
        if existing is None:
            msg = "No CommunityCAD account is linked."
            if had_pending:
                msg += " Cancelled the sign-in that was waiting for approval."
            return {"status": "not_linked", "message": msg}
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
