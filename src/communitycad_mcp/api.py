"""Async client for CommunityCAD's public /v1 API.

One method per route. No retries anywhere: a failed call becomes a typed
CommunityCADError and the tool reports it.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from . import __version__
from .config import Settings
from .errors import CommunityCADError, from_response

CLIENT_NAME = "communitycad-mcp"
SCOPES = "catalog:read download"
USER_AGENT = f"{CLIENT_NAME}/{__version__}"


class ApiClient:
    def __init__(
        self,
        settings: Settings,
        token_getter: Callable[[], str | None],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._token_getter = token_getter
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        download: bool = False,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        headers = {}
        token = self._token_getter() if auth else None
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
        if resp.is_success:
            return resp
        raise from_response(resp, signed_in=bool(token), download=download)

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self._request(method, path, **kw)
        data = resp.json()
        return data if isinstance(data, dict) else {"items": data}

    # -- auth (device grant, RFC 8628) ------------------------------------

    async def device_code(self, device_name: str | None = None) -> dict[str, Any]:
        body = {"client_name": CLIENT_NAME, "client_version": __version__, "scope": SCOPES}
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

    async def revoke_current_token(self) -> None:
        await self._request("DELETE", "/auth/tokens/current")

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
