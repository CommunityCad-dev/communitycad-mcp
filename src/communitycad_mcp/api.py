"""Async client for CommunityCAD's public /v1 API.

One method per route. No retries anywhere: a failed call becomes a typed
CommunityCADError and the tool reports it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Callable

import httpx

from . import __version__
from .config import Settings
from .errors import CommunityCADError, from_response

CLIENT_NAME = "communitycad-mcp"
SCOPES = "catalog:read download"
# Asked for only when the user allows uploads (link_account(allow_uploads=true)).
UPLOAD_SCOPE = "models:write"
USER_AGENT = f"{CLIENT_NAME}/{__version__}"


class ApiClient:
    def __init__(
        self,
        settings: Settings,
        token_getter: Callable[[], str | None],
        transport: httpx.AsyncBaseTransport | None = None,
        on_revoked: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self._token_getter = token_getter
        # Called with the token the API refused (401), so the caller can forget it.
        self._on_revoked = on_revoked
        self._signed_out_by_revoke = False
        self._http = httpx.AsyncClient(
            base_url=settings.api_url.rstrip("/") + "/v1",
            timeout=settings.timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
        )
        # Presigned file URLs go to storage, never with the API token.
        self._files = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout_s, read=120.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"ApiClient({self.settings.api_url!r})"

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._files.aclose()

    @property
    def signed_in(self) -> bool:
        return self._token_getter() is not None

    def forget_token(self, token: str) -> None:
        """The API refused this saved token: forget it and note it for the tool result."""
        if self._on_revoked:
            self._on_revoked(token)
        self._signed_out_by_revoke = True

    def take_revoked(self) -> bool:
        """True once after a request found the saved sign-in revoked or expired."""
        flag, self._signed_out_by_revoke = self._signed_out_by_revoke, False
        return flag

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        download: bool = False,
        params: dict[str, Any] | None = None,
        json: Any = None,
        signed_out_retry: bool = True,
        forget_on_401: bool = True,
    ) -> httpx.Response:
        token = self._token_getter() if auth else None
        resp = await self._send(method, path, token, params=params, json=json)
        if resp.status_code == 401 and token and signed_out_retry:
            # The saved sign-in was revoked or expired: forget it and ask once more
            # signed out. That request carries no token, so this can't loop.
            self.forget_token(token)
            token = None
            resp = await self._send(method, path, None, params=params, json=json)
        elif resp.status_code == 401 and token and forget_on_401:
            # A write that needs the account: forget the dead sign-in, don't resend.
            self.forget_token(token)
        if resp.is_success:
            return resp
        raise from_response(resp, signed_in=bool(token), download=download)

    async def _send(
        self,
        method: str,
        path: str,
        token: str | None,
        *,
        params: dict[str, Any] | None,
        json: Any,
    ) -> httpx.Response:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        try:
            resp = await self._http.request(method, path, params=params, json=json, headers=headers)
        except httpx.HTTPError:
            # The exception text can include the request; don't pass it on.
            raise CommunityCADError(
                "upstream_unavailable", "Couldn't reach CommunityCAD. Check the connection and try again later."
            ) from None
        return resp

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self._request(method, path, **kw)
        data = resp.json()
        return data if isinstance(data, dict) else {"items": data}

    # -- auth (device grant, RFC 8628) ------------------------------------

    async def device_code(self, device_name: str | None = None, scope: str = SCOPES) -> dict[str, Any]:
        body = {"client_name": CLIENT_NAME, "client_version": __version__, "scope": scope}
        if device_name:
            body["device_name"] = device_name
        return await self._json("POST", "/auth/device/code", auth=False, json=body)

    async def device_token(self, device_code: str) -> tuple[str | None, dict[str, Any]]:
        """One poll. Returns (rfc8628_error, body); error is None on success."""
        try:
            resp = await self._http.post(
                "/auth/device/token",
                json={"device_code": device_code, "grant_type": "urn:ietf:params:oauth:grant-type:device_code"},
            )
        except httpx.HTTPError:
            raise CommunityCADError("upstream_unavailable", "Couldn't reach CommunityCAD to finish sign-in.") from None
        if resp.status_code == 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            return str(body.get("error") or "invalid_grant"), body
        if not resp.is_success:
            raise from_response(resp, signed_in=False)
        return None, resp.json()

    async def request_scopes(self, scope: str) -> dict[str, Any]:
        """Ask the user to add permissions to the saved token (scope upgrade)."""
        return await self._json("POST", "/auth/tokens/current/scopes", json={"scope": scope}, signed_out_retry=False)

    async def revoke_current_token(self) -> None:
        await self._request("DELETE", "/auth/tokens/current", signed_out_retry=False, forget_on_401=False)

    # -- account -----------------------------------------------------------

    async def account_status(self) -> dict[str, Any]:
        return await self._json("GET", "/account/status")

    # -- catalog -----------------------------------------------------------

    async def search(self, **params: Any) -> dict[str, Any]:
        return await self._json("GET", "/catalog/search", params=params)

    async def model(self, model_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/catalog/models/{model_id}")

    async def similar(self, model_id: str, limit: int = 10) -> dict[str, Any]:
        return await self._json("GET", f"/catalog/models/{model_id}/similar", params={"limit": limit})

    async def download_check(self, model_id: str, file_id: int | None = None) -> dict[str, Any]:
        return await self._json(
            "GET", f"/catalog/models/{model_id}/download-check", params={"file_id": file_id}, download=True
        )

    async def download_url(self, model_id: str, file_id: int) -> tuple[dict[str, Any], dict[str, str]]:
        resp = await self._request("POST", f"/catalog/models/{model_id}/files/{file_id}/download-url", download=True)
        limits = {k: v for k, v in resp.headers.items() if k.lower().startswith("x-download-")}
        return resp.json(), limits

    async def fetch_image(self, url: str, max_bytes: int) -> bytes | None:
        """Fetch a preview image (no API token is sent). None when it fails or is too large."""
        try:
            async with self._files.stream("GET", url) as resp:
                if not resp.is_success:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        return None
                return bytes(buf)
        except httpx.HTTPError:
            return None

    def stream_file(self, url: str):
        """Stream a presigned file URL (no API token is sent)."""
        return self._files.stream("GET", url)

    # -- uploads (need models:write) -------------------------------------------

    async def stage_upload(self, filename: str, size: int, content_type: str | None) -> dict[str, Any]:
        return await self._json(
            "POST", "/uploads", json={"filename": filename, "size": size, "content_type": content_type}, signed_out_retry=False
        )

    async def send_staged(self, method: str, url: str, headers: dict[str, str], path: Path, size: int) -> None:
        """Send the whole file to the staging URL the API returned (no API token is sent)."""

        async def chunks() -> AsyncIterator[bytes]:
            with path.open("rb") as fh:
                while block := fh.read(1024 * 1024):
                    yield block

        headers = {**{str(k): str(v) for k, v in headers.items()}, "Content-Length": str(size)}
        try:
            resp = await self._files.request(
                method, url, content=chunks(), headers=headers, timeout=httpx.Timeout(self.settings.timeout_s, write=600.0)
            )
        except httpx.HTTPError:
            raise CommunityCADError(
                "upstream_unavailable", f"Sending {path.name} failed. Nothing was published; try again later."
            ) from None
        if not resp.is_success:
            raise CommunityCADError(
                "upstream_unavailable",
                f"Storage refused {path.name} (HTTP {resp.status_code}). Nothing was published; try again later.",
            )

    async def create_model(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/models", json=body, signed_out_retry=False)

    async def create_version(self, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", f"/models/{model_id}/versions", json=body, signed_out_retry=False)

    async def add_images(self, model_id: str, images: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._json("POST", f"/models/{model_id}/images", json={"images": images}, signed_out_retry=False)

    async def add_datasheets(self, model_id: str, datasheets: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._json(
            "POST", f"/models/{model_id}/datasheets", json={"datasheets": datasheets}, signed_out_retry=False
        )

    async def edit_model(self, model_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("PATCH", f"/models/{model_id}", json=body, signed_out_retry=False)

    # -- datasheets ----------------------------------------------------------

    async def datasheets(self, model_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/catalog/models/{model_id}/datasheets")

    async def datasheet_pages(
        self, model_id: str, datasheet_id: int, first_page: int, last_page: int
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"/catalog/models/{model_id}/datasheets/{datasheet_id}/pages",
            params={"from": first_page, "to": last_page},
        )

    async def datasheet_search(self, model_id: str, query: str) -> dict[str, Any]:
        return await self._json("GET", f"/catalog/models/{model_id}/datasheets/search", params={"q": query})

    # -- community -----------------------------------------------------------

    async def comments(
        self, model_id: str, limit: int, offset: int, order: str = "newest", replies: int = 5
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"/catalog/models/{model_id}/comments",
            params={"limit": limit, "offset": offset, "order": order, "replies": replies},
        )

    async def scores(self, model_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/catalog/models/{model_id}/scores")

    # -- profiles --------------------------------------------------------------

    async def company(self, slug_or_id: str, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        return await self._json("GET", f"/companies/{slug_or_id}", params={"limit": limit, "offset": offset})

    async def companies(self, query: str, limit: int, offset: int) -> dict[str, Any]:
        return await self._json("GET", "/companies", params={"q": query, "limit": limit, "offset": offset})

    async def creator(self, username: str, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        return await self._json("GET", f"/creators/{username}", params={"limit": limit, "offset": offset})
