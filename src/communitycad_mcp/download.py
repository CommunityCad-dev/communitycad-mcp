"""Saving files from the counted download endpoint to disk."""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .api import ApiClient
from .errors import CommunityCADError

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(name: str, fallback: str = "file") -> str:
    base = PurePosixPath(name.replace("\\", "/")).name
    base = _SAFE.sub("_", base).strip("._") or fallback
    return base[:200]


def model_dir(root: Path, slug: str | None, uuid: str) -> Path:
    return root / safe_name(slug or "", fallback=uuid)


async def stream_to(api: ApiClient, url: str, dest: Path, max_bytes: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".part-", dir=dest.parent)
    written = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            try:
                async with api.stream_file(url) as resp:
                    if not resp.is_success:
                        raise CommunityCADError(
                            "upstream_unavailable",
                            f"The file link answered HTTP {resp.status_code}. The download was already counted; "
                            "don't retry right away.",
                        )
                    async for chunk in resp.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise CommunityCADError(
                                "file_too_large", f"The file is larger than this server's {max_bytes // 2**20} MB limit."
                            )
                        fh.write(chunk)
            except CommunityCADError:
                raise
            except Exception:
                raise CommunityCADError("upstream_unavailable", "The file transfer failed part way.") from None
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp_name, 0o666 & ~umask)  # a normal user file, not mkstemp's 0600
        os.replace(tmp_name, dest)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return written


def extract_member(archive: Path, member: str, dest_dir: Path, max_bytes: int) -> Path:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise CommunityCADError("invalid_request", "The downloaded file isn't a ZIP archive.") from None
    with zf:
        names = [i for i in zf.infolist() if not i.is_dir()]
        wanted = member.replace("\\", "/").lstrip("/")
        info = next((i for i in names if i.filename == wanted), None)
        if info is None:
            matches = [i for i in names if PurePosixPath(i.filename).name == PurePosixPath(wanted).name]
            if len(matches) == 1:
                info = matches[0]
        if info is None:
            listing = [i.filename for i in names][:50]
            raise CommunityCADError(
                "invalid_request", f"No member named {member!r} in the archive.", members=listing
            )
        if info.file_size > max_bytes:
            raise CommunityCADError("file_too_large", "That archive member is larger than this server's limit.")
        parts = [p for p in PurePosixPath(info.filename).parts if p not in ("", ".", "..")]
        target = dest_dir.joinpath(*[safe_name(p) for p in parts]) if parts else dest_dir / "member"
        root = dest_dir.resolve()
        if root not in target.resolve().parents:
            raise CommunityCADError("invalid_request", "That archive member points outside the download folder.")
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with zf.open(info) as src, open(target, "wb") as out:
            while chunk := src.read(1 << 16):
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise CommunityCADError("file_too_large", "That archive member is larger than this server's limit.")
                out.write(chunk)
        return target


def remaining_from_headers(headers: dict[str, str]) -> dict[str, Any]:
    h = {k.lower(): v for k, v in headers.items()}

    def num(key: str) -> int | None:
        v = h.get(key)
        return int(v) if v and v.lstrip("-").isdigit() else None

    out = {
        "daily_remaining": num("x-download-daily-remaining"),
        "daily_limit": num("x-download-daily-limit"),
        "weekly_remaining": num("x-download-weekly-remaining"),
        "weekly_limit": num("x-download-weekly-limit"),
        "daily_reset_at": h.get("x-download-reset-daily"),
        "weekly_reset_at": h.get("x-download-reset-weekly"),
    }
    return {k: v for k, v in out.items() if v is not None}
