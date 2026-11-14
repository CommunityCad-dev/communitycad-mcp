"""Settings, read once from the environment."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_dir

DEFAULT_API_URL = "https://communitycad.dev/api"
SITE_URL = "https://communitycad.dev"
# Largest file the local server will save (bytes). Override with
# COMMUNITYCAD_MAX_DOWNLOAD_MB.
DEFAULT_MAX_DOWNLOAD_MB = 500


@dataclass(frozen=True)
class Settings:
    api_url: str = DEFAULT_API_URL
    # Empty: derived from api_url (see site_url_for). Set COMMUNITYCAD_SITE_URL
    # when the API isn't served under the site's /api path.
    site_url: str = ""
    download_dir: Path = Path("~/CommunityCAD").expanduser()
    config_dir: Path = Path(user_config_dir("communitycad-mcp"))
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_MB * 1024 * 1024
    timeout_s: float = 20.0

    def __post_init__(self) -> None:
        if not self.site_url:
            object.__setattr__(self, "site_url", site_url_for(self.api_url))

    @property
    def token_path(self) -> Path:
        return self.config_dir / "token.json"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        kwargs: dict = {}
        if env.get("COMMUNITYCAD_API_URL"):
            kwargs["api_url"] = env["COMMUNITYCAD_API_URL"].rstrip("/")
        if env.get("COMMUNITYCAD_SITE_URL"):
            kwargs["site_url"] = env["COMMUNITYCAD_SITE_URL"].rstrip("/")
        if env.get("COMMUNITYCAD_DOWNLOAD_DIR"):
            kwargs["download_dir"] = Path(env["COMMUNITYCAD_DOWNLOAD_DIR"]).expanduser()
        if env.get("COMMUNITYCAD_CONFIG_DIR"):
            kwargs["config_dir"] = Path(env["COMMUNITYCAD_CONFIG_DIR"]).expanduser()
        if env.get("COMMUNITYCAD_MAX_DOWNLOAD_MB"):
            kwargs["max_download_bytes"] = int(float(env["COMMUNITYCAD_MAX_DOWNLOAD_MB"]) * 1024 * 1024)
        return cls(**kwargs)

    def model_url(self, uuid: str) -> str:
        return f"{self.site_url}/m/{uuid}"


def site_url_for(api_url: str, *, warn: bool = True) -> str:
    """The website that goes with an API: https://example.org/api -> https://example.org.

    An API that isn't under an /api path (e.g. one on its own port) gives no
    safe guess; its own address is used and COMMUNITYCAD_SITE_URL should be set.
    """
    parts = urlsplit(api_url.rstrip("/"))
    path = parts.path.rstrip("/")
    origin = f"{parts.scheme}://{parts.netloc}"
    if path.endswith("/api"):
        return origin + path[: -len("/api")]
    if warn:
        print(
            f"communitycad-mcp: can't tell the website address from COMMUNITYCAD_API_URL={api_url}; "
            "set COMMUNITYCAD_SITE_URL. Page links use the API address until then.",
            file=sys.stderr,
        )
    return origin + path
