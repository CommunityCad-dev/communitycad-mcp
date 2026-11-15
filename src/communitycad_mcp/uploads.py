"""Upload tools: new models, new versions, images, datasheets and detail edits.

Everything is checked here before any file is staged, with the same
documentation rules the API applies, so a refused upload sends nothing and
doesn't use one of the day's uploads. Files go to the API's staging route,
then straight to the storage URL it returns (without the API token), then
the model route gets the staged tickets.
"""

from __future__ import annotations

import os
import re
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import ApiClient
from .config import Settings
from .errors import CommunityCADError

MAX_FILE_BYTES = 250 * 1024 * 1024
MIN_DESCRIPTION = 80
MIN_README = 400
MIN_NOTES = 20
MAX_IMAGES = 10
MAX_DATASHEETS = 5

MODEL_TYPES = {
    "step", "stp", "stl", "obj", "3mf", "sldprt", "sldasm", "f3d", "fcstd",
    "dxf", "svg", "slddrw",
    "c2d", "nc", "gcode", "tap", "cnc",
    "zip",
}  # fmt: skip
IMAGE_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
DATASHEET_TYPES = {"pdf": "application/pdf"}

# The licences the website's upload form offers (SPDX ids). The user picks one.
LICENSES = {
    "MIT": "MIT: permissive, minimal restrictions",
    "Apache-2.0": "Apache 2.0: permissive, with a patent grant",
    "GPL-3.0": "GPL v3: copyleft, derivatives must use the same licence",
    "BSD-3-Clause": "BSD 3-Clause: permissive, no endorsement clause",
    "CC-BY-4.0": "CC BY 4.0: free use with attribution (common for design files)",
    "CC-BY-SA-4.0": "CC BY-SA 4.0: attribution and share-alike",
    "CC0-1.0": "CC0: public domain dedication, no restrictions",
}

# README sections, by the words a heading may use for each.
README_SECTIONS = {
    "What it is": ("what it is", "what is it", "overview", "about", "description", "introduction"),
    "Dimensions": ("dimension", "specification", "specs", "size"),
    "How to use": ("how to use", "usage", "use", "printing", "print", "assembly", "assembling", "mounting", "install"),
    "Materials": ("material", "hardware", "bill of materials", "bom"),
}
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)

# Never put these in a zipped folder.
SKIP_DIRS = {".venv", "venv", "node_modules", "__pycache__"}


# -- local checks ----------------------------------------------------------------


def readme_missing(readme: str | None) -> list[str]:
    """What the README lacks, in the API's wording. Empty when it passes."""
    text = (readme or "").strip()
    if not text:
        return ["README"]
    headings = [h.lower() for h in _HEADING.findall(text)]
    missing = [
        f"README section: {name}"
        for name, words in README_SECTIONS.items()
        if not any(w in h for h in headings for w in words)
    ]
    if len(text) < MIN_README:
        missing.append(f"README too short ({len(text)} of {MIN_README} characters)")
    return missing


def description_missing(description: str | None) -> list[str]:
    n = len((description or "").strip())
    return [] if n >= MIN_DESCRIPTION else [f"description too short ({n} of {MIN_DESCRIPTION} characters)"]


def documentation_error(missing: list[str], **extra: Any) -> CommunityCADError:
    return CommunityCADError(
        "documentation_missing",
        "Nothing was uploaded and no upload was used: the documentation is incomplete. "
        "Fill in what's listed in `missing` from what the user and the files actually say "
        "(ask the user for anything you don't know, never invent dimensions), then try again.",
        missing=missing,
        **extra,
    )


def license_error() -> CommunityCADError:
    return CommunityCADError(
        "license_required",
        "A public model needs a licence, and the user must choose it. Show them the options in "
        "`licenses` and ask; don't pick one for them. Nothing was uploaded.",
        licenses=LICENSES,
    )


def _ext(path: Path) -> str:
    return path.suffix.lower().lstrip(".")


def _existing_file(raw: str, what: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_file():
        raise CommunityCADError("invalid_request", f"The {what} {raw!r} isn't a file on this computer.")
    return path


def _check_size(path: Path, label: str | None = None) -> int:
    size = path.stat().st_size
    if size == 0:
        raise CommunityCADError("invalid_request", f"{label or path.name} is empty.")
    if size > MAX_FILE_BYTES:
        raise CommunityCADError(
            "file_too_large",
            f"{label or path.name} is {size / 2**20:.0f} MB; uploads can be at most 250 MB. Nothing was uploaded.",
        )
    return size


def _typed_files(raws: list[str], types: dict[str, str] | set[str], what: str, plural: str) -> list[Path]:
    paths = []
    for raw in raws:
        path = _existing_file(raw, what)
        if _ext(path) not in types:
            allowed = ", ".join(sorted(types))
            raise CommunityCADError(
                "invalid_request", f"{path.name} isn't a supported {what} type. {plural} can be: {allowed}."
            )
        _check_size(path)
        paths.append(path)
    return paths


def _optional_list(values: list[str] | None, n: int, name: str) -> list[str | None]:
    if values is None:
        return [None] * n
    if len(values) != n:
        raise CommunityCADError("invalid_request", f"Give one {name} per file ({n}), or none.")
    return [v.strip() or None for v in values]


def check_model_source(raw: str) -> Path:
    """The model file or folder to upload; a folder is zipped later."""
    path = Path(raw).expanduser()
    if path.is_dir():
        if not any(_folder_files(path)):
            raise CommunityCADError("invalid_request", f"The folder {raw!r} has no files to upload.")
        return path
    if not path.is_file():
        raise CommunityCADError("invalid_request", f"{raw!r} isn't a file or folder on this computer.")
    if _ext(path) not in MODEL_TYPES:
        raise CommunityCADError(
            "invalid_request",
            f"{path.name} isn't a supported model file. Use one of: {', '.join(sorted(MODEL_TYPES))}, "
            "or pass a folder to upload it as a ZIP.",
        )
    _check_size(path)
    return path


# -- folders --------------------------------------------------------------------------


def _folder_files(root: Path):
    """Files under root, skipping hidden ones, virtualenvs, node_modules, caches and links."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if name.startswith(".") or p.is_symlink() or not p.is_file():
                continue
            yield p


def zip_folder(root: Path, dest_dir: Path) -> Path:
    """Zip a folder into dest_dir/<folder>.zip (paths inside are relative to the folder)."""
    dest = dest_dir / f"{root.name or 'model'}.zip"
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in _folder_files(root):
            zf.write(p, p.relative_to(root).as_posix())
    _check_size(dest, f"The zipped folder {root.name}")
    return dest


# -- staging ---------------------------------------------------------------------------


@dataclass
class Staged:
    ticket: str
    name: str


async def stage(api: ApiClient, path: Path) -> Staged:
    """POST /v1/uploads, then send the file to the URL it returns."""
    size = path.stat().st_size
    # CAD types have no reliable MIME type; the API goes by the file name.
    content_type = {**IMAGE_TYPES, **DATASHEET_TYPES, "zip": "application/zip"}.get(_ext(path))
    ticket = await api.stage_upload(path.name, size, content_type)
    max_bytes = ticket.get("max_bytes")
    if isinstance(max_bytes, int) and size > max_bytes:
        raise CommunityCADError(
            "file_too_large", f"{path.name} is larger than the {max_bytes // 2**20} MB CommunityCAD accepts."
        )
    upload = ticket.get("upload") or {}
    await api.send_staged(upload.get("method") or "PUT", upload["url"], upload.get("headers") or {}, path, size)
    return Staged(ticket=ticket["staged_upload"], name=path.name)


def _left(data: dict[str, Any]) -> str:
    left = data.get("uploads_left_today")
    return f" {left} upload{'s' if left != 1 else ''} left today." if isinstance(left, int) else ""


# -- tools ------------------------------------------------------------------------------

WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

Visibility = Literal["public", "private"]


def register(mcp, api: ApiClient, settings: Settings, tool_errors, model_ref) -> None:
    tool = mcp.tool

    def page(data: dict[str, Any]) -> str | None:
        return data.get("page_url") or (settings.model_url(data["uuid"]) if data.get("uuid") else None)

    @tool(annotations=WRITE)
    @tool_errors
    async def upload_model(
        path: Annotated[str, Field(description="The CAD file, or a folder to upload as a ZIP (hidden files, .venv, node_modules and __pycache__ are left out).")],
        title: Annotated[str, Field(min_length=3, max_length=200)],
        description: Annotated[str, Field(description=f"What the part is, at least {MIN_DESCRIPTION} characters, from what the user and the CAD say.", max_length=5000)],
        readme_markdown: Annotated[
            str,
            Field(description=f"Markdown with the sections What it is, Dimensions, How to use and Materials (at least ~{MIN_README} characters). Never invent dimensions.", max_length=100_000),
        ],
        visibility: Annotated[Visibility, Field(description="The user's choice. Always ask.")],
        is_mature: Annotated[bool, Field(description="18+ (adult content, firearms or firearm parts, graphic content). Always ask.")],
        image_paths: Annotated[list[str], Field(description="At least one picture of the part (JPEG, PNG, GIF or WebP).", max_length=MAX_IMAGES)],
        license: Annotated[str | None, Field(description="SPDX id the user chose, required for public: " + ", ".join(LICENSES) + ".")] = None,
        tags: Annotated[list[str] | None, Field(max_length=20)] = None,
        sourcing: Annotated[Literal["auto", "own", "none"] | None, Field(description="Where to buy it: auto (let the site find it), own (the user's links), none.")] = None,
        image_captions: Annotated[list[str] | None, Field(description="One caption per image, or none.")] = None,
        datasheet_paths: Annotated[list[str] | None, Field(description="PDF datasheets. Attach the part's datasheet when it has one.", max_length=MAX_DATASHEETS)] = None,
        datasheet_titles: Annotated[list[str] | None, Field(description="One title per datasheet, or none.")] = None,
    ) -> dict:
        """Upload a new model to CommunityCAD (public or private) from a file or folder, with
        pictures, README and optional datasheets. Only after the user explicitly said yes and
        chose the visibility, the licence (for public) and whether it's 18+. Uses one of the
        user's 10 uploads a day; nothing is uploaded or counted when documentation is missing.
        Same moderation as the website. Returns the page link and uploads left today."""
        missing = description_missing(description) + readme_missing(readme_markdown)
        if not image_paths:
            missing.append("at least one image")
        needs_license = visibility == "public" and not (license or "").strip()
        if missing:
            raise documentation_error(missing, license_required=True if needs_license else None)
        if needs_license:
            raise license_error()
        source = check_model_source(path)
        images = _typed_files(image_paths, IMAGE_TYPES, "image", "Images")
        captions = _optional_list(image_captions, len(images), "caption")
        sheets = _typed_files(datasheet_paths or [], DATASHEET_TYPES, "datasheet", "Datasheets")
        titles = _optional_list(datasheet_titles, len(sheets), "datasheet title")

        with ExitStack() as stack:
            if source.is_dir():
                tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="communitycad-")))
                source = zip_folder(source, tmp)
            model_file = await stage(api, source)
            staged_images = [await stage(api, p) for p in images]
            staged_sheets = [await stage(api, p) for p in sheets]

        body = {
            "staged_upload": model_file.ticket,
            "title": title.strip(),
            "description": description.strip(),
            "readme_markdown": readme_markdown.strip(),
            "tags": [t.strip() for t in tags or [] if t.strip()],
            "visibility": visibility,
            "license": (license or "").strip() or None,
            "is_mature": is_mature,
            "sourcing": sourcing,
            "images": [{"staged_upload": s.ticket, "caption": c} for s, c in zip(staged_images, captions)],
            "datasheets": [{"staged_upload": s.ticket, "title": t} for s, t in zip(staged_sheets, titles)],
        }
        data = await api.create_model(body)
        out = {k: data.get(k) for k in ("id", "uuid", "visibility", "status", "created_via", "uploads_left_today")}
        out["page_url"] = page(data)
        out["uploaded_file"] = model_file.name
        processing = " It's still processing; previews appear in a few minutes." if data.get("status") == "processing" else ""
        out["message"] = f"Uploaded as a {data.get('visibility', visibility)} model: {out['page_url']}.{processing}{_left(data)}"
        return {k: v for k, v in out.items() if v is not None}

    @tool(annotations=WRITE)
    @tool_errors
    async def upload_version(
        model_id: Annotated[str, Field(description="The user's own model (id or page URL).")],
        path: Annotated[str, Field(description="The new CAD file, or a folder to upload as a ZIP.")],
        notes: Annotated[str, Field(description=f"What changed, at least {MIN_NOTES} characters.", max_length=5000)],
        readme_markdown: Annotated[
            str | None,
            Field(description="The refreshed README (same sections as upload_model). Send it when dimensions or usage changed.", max_length=100_000),
        ] = None,
    ) -> dict:
        """Upload a new version of the user's own model. Only after the user's explicit yes.
        Write real notes on what changed, and refresh the README when dimensions changed.
        Uses one of the day's uploads; nothing is sent when the notes or README fall short."""
        ref = model_ref(model_id)
        missing = []
        if len(notes.strip()) < MIN_NOTES:
            missing.append(f"version notes too short ({len(notes.strip())} of {MIN_NOTES} characters)")
        if readme_markdown is not None:
            missing += readme_missing(readme_markdown)
        if missing:
            raise documentation_error(missing)
        source = check_model_source(path)
        with ExitStack() as stack:
            if source.is_dir():
                tmp = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="communitycad-")))
                source = zip_folder(source, tmp)
            staged = await stage(api, source)
        body = {"staged_upload": staged.ticket, "notes": notes.strip(), "readme_markdown": readme_markdown.strip() if readme_markdown else None}
        data = await api.create_version(ref, body)
        out = {
            "model_id": data.get("model_id", ref),
            "version_number": data.get("version_number"),
            "page_url": data.get("page_url") or settings.model_url(ref),
            "uploads_left_today": data.get("uploads_left_today"),
            "uploaded_file": staged.name,
        }
        out["message"] = f"Uploaded version {out['version_number']}: {out['page_url']}.{_left(data)}"
        return {k: v for k, v in out.items() if v is not None}
