"""Typed errors: a stable code plus one plain sentence.

Every error the tools raise is a CommunityCADError. Its message is written
here or taken from the API's own error text; request headers (and so the
token) never go into it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Codes from the design, plus `invalid_request` (bad arguments) and
# `forbidden` (the account can't do this, for a reason the API states).
CODES = {
    "not_signed_in",
    "sign_in_required_for_download",
    "download_limit_reached",
    "scope_missing",
    "model_not_found",
    "model_unavailable",
    "rate_limited",
    "file_too_large",
    "upstream_unavailable",
    "invalid_request",
    "forbidden",
    "not_available_yet",
}

# Extra fields from an API error body that are safe and useful to pass on.
_PASSTHROUGH = (
    "limit_type",
    "daily_remaining",
    "weekly_remaining",
    "reset_at",
    "upgrade_url",
    "sign_up_url",
    "account_hint",
    "request_url",
)


class CommunityCADError(Exception):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        assert code in CODES, code
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {k: v for k, v in details.items() if v is not None}

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.details}}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def __repr__(self) -> str:
        return f"CommunityCADError({self.code!r}, {self.message!r})"


def _body(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _detail_text(body: dict[str, Any]) -> str | None:
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        text = detail.get("message") or detail.get("detail")
        return text if isinstance(text, str) else None
    return body.get("error_description") if isinstance(body.get("error_description"), str) else None


def from_response(resp: httpx.Response, *, signed_in: bool, download: bool = False) -> CommunityCADError:
    """Turn a non-2xx API response into a typed error."""
    status = resp.status_code
    body = _body(resp)
    detail = body.get("detail")
    api_code = body.get("error") or (detail.get("error") if isinstance(detail, dict) else None)
    extras = {k: body[k] for k in _PASSTHROUGH if k in body}
    text = _detail_text(body)

    if status == 401:
        if signed_in:
            return CommunityCADError(
                "not_signed_in",
                "The saved sign-in was revoked or has expired. Run link_account to sign in again.",
            )
        if download:
            return CommunityCADError(
                "sign_in_required_for_download",
                "Downloading needs a linked CommunityCAD account. Run link_account to sign in.",
                **extras,
            )
        return CommunityCADError("not_signed_in", "This needs a linked CommunityCAD account. Run link_account.")
    if status == 403:
        if api_code == "insufficient_scope":
            missing = detail.get("missing") if isinstance(detail, dict) else None
            return CommunityCADError(
                "scope_missing",
                "The linked account didn't grant this permission. Run unlink_account, then link_account again.",
                missing=missing,
            )
        return CommunityCADError("forbidden", text or "CommunityCAD refused this request.", **extras)
    if status == 404:
        if text == "Not Found":  # the route itself doesn't exist on this API yet
            return CommunityCADError(
                "not_available_yet", "The CommunityCAD API doesn't offer this yet. Use the model page on the website."
            )
        if text and "file" in text.lower():
            return CommunityCADError("model_not_found", "That file isn't part of this model's latest version.")
        return CommunityCADError("model_not_found", "No model with that ID, or it isn't visible to this account.")
    if status == 410:
        return CommunityCADError("model_unavailable", text or "This model is no longer available.")
    if status == 413:
        return CommunityCADError("file_too_large", text or "The file is too large.")
    if status == 429:
        if api_code == "download_limit_reached":
            return CommunityCADError(
                "download_limit_reached",
                "Download limit reached, the same limit as on the website. Don't retry; it resets at reset_at.",
                **extras,
            )
        retry_after = resp.headers.get("Retry-After")
        return CommunityCADError(
            "rate_limited",
            text or "Too many requests. Wait before trying again.",
            retry_after_s=int(retry_after) if retry_after and retry_after.isdigit() else None,
            **extras,
        )
    if status in (400, 422):
        return CommunityCADError("invalid_request", text or "CommunityCAD rejected the request's arguments.")
    return CommunityCADError("upstream_unavailable", f"CommunityCAD answered with an error (HTTP {status}). Try again later.")
