"""The device token, kept in the user's config dir with mode 0600.

The token itself is only ever read here and by the API client's auth header.
Nothing returns or logs it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredToken:
    access_token: str
    username: str | None = None
    scope: str | None = None
    token_id: int | None = None
    api_url: str | None = None

    def __repr__(self) -> str:  # never show the token
        return f"StoredToken(username={self.username!r}, scope={self.scope!r}, token_id={self.token_id!r})"

    __str__ = __repr__


class TokenStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __repr__(self) -> str:
        return f"TokenStore({str(self.path)!r})"

    def load(self) -> StoredToken | None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            return None
        return StoredToken(
            access_token=data["access_token"],
            username=data.get("username"),
            scope=data.get("scope"),
            token_id=data.get("token_id"),
            api_url=data.get("api_url"),
        )

    def save(self, token: StoredToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(
                {
                    "access_token": token.access_token,
                    "username": token.username,
                    "scope": token.scope,
                    "token_id": token.token_id,
                    "api_url": token.api_url,
                },
                fh,
            )
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
