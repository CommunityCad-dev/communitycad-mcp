"""Settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

DEFAULT_API_URL = "https://communitycad.dev/api"
SITE_URL = "https://communitycad.dev"
# Largest file the local server will save (bytes). Override with
# COMMUNITYCAD_MAX_DOWNLOAD_MB.
DEFAULT_MAX_DOWNLOAD_MB = 500


@dataclass(frozen=True)
class Settings:
    api_url: str = DEFAULT_API_URL
    site_url: str = SITE_URL
    download_dir: Path = Path("~/CommunityCAD").expanduser()
    config_dir: Path = Path(user_config_dir("communitycad-mcp"))
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_MB * 1024 * 1024
    timeout_s: float = 20.0

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
