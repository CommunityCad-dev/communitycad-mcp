import json
import zipfile
from io import BytesIO

import httpx
import pytest

from communitycad_mcp.token_store import StoredToken, TokenStore
from communitycad_mcp.uploads import readme_missing

from .conftest import TOKEN, V1, fixture

UUID = "0b6f3a52-6a1e-4c55-9d1a-3c7f0f2a9e11"
STAGING = "https://api.example.test/staging"

DESCRIPTION = (
    "A DIN rail clip for 35 mm rails that holds a 60 x 40 mm circuit board with two M3 screws, "
    "printed flat without supports."
)
README = """# DIN rail board clip

## What it is
A clip that snaps onto a standard 35 mm DIN rail and carries a small circuit board, such as a relay or sensor module.

## Dimensions
Overall 70 x 45 x 12 mm. Board holes 50 x 30 mm apart, for M3 screws.

## How to use
Print flat, PETG recommended, 3 walls and 30 % infill. Screw the board on with two M3 x 6 screws, then press onto the rail.

## Materials
- 2 x M3 x 6 screws
- 2 x M3 heat-set inserts
"""


@pytest.fixture
def uploader(settings):
    tok = StoredToken(access_token=TOKEN, username="example-user", scope="catalog:read download models:write", token_id=42)
    TokenStore(settings.token_path).save(tok)
    return tok


@pytest.fixture(autouse=True)
def account_status(api_mock):
    """Account status is mocked so a stray call would be visible; uploads never need it."""
    return api_mock.get(f"{V1}/account/status").respond(200, json=fixture("account_status_signed_in.json"))


@pytest.fixture
def files(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    (d / "clip.step").write_bytes(b"ISO-10303-21; example")
    (d / "photo.png").write_bytes(b"\x89PNG example")
    (d / "sheet.pdf").write_bytes(b"%PDF-1.4 example")
    (d / "notes.txt").write_text("not a model")
    return d


def staging(api_mock):
    """POST /v1/uploads hands out numbered tickets; PUTs to the staging URL are recorded."""
    counter = {"n": 0}

    def ticket(request):
        counter["n"] += 1
        t = f"stg_example_{counter['n']}"
        body = fixture("upload_staged.json")
        body["staged_upload"] = t
        body["upload"]["url"] = f"{STAGING}/{t}"
        return httpx.Response(201, json=body)

    stage = api_mock.post(f"{V1}/uploads").mock(side_effect=ticket)
    put = api_mock.put(url__startswith=STAGING).respond(200)
    return stage, put


def model_args(files, **over):
    args = {
        "path": str(files / "clip.step"),
        "title": "DIN rail board clip",
        "description": DESCRIPTION,
        "readme_markdown": README,
        "visibility": "private",
        "is_mature": False,
        "image_paths": [str(files / "photo.png")],
    }
    args.update(over)
    return args


def no_requests(api_mock):
    assert not [c for c in api_mock.calls if not c.request.url.path.endswith("/account/status")], [str(c.request.url) for c in api_mock.calls]


# -- local checks ------------------------------------------------------------------


def test_readme_rules():
    assert readme_missing(README) == []
    assert readme_missing("") == ["README"]
    short = "# What it is\nA clip.\n## Specs\n70 mm\n## Assembly\nScrew it.\n## Hardware\nM3"
    assert readme_missing(short) == [f"README too short ({len(short)} of 400 characters)"]
    no_dims = README.replace("## Dimensions", "## Notes")
    assert readme_missing(no_dims) == ["README section: Dimensions"]


async def test_missing_docs_fail_before_any_request(run, api_mock, uploader, files):
    err = await run.err(
        "upload_model",
        model_args(files, description="A clip.", readme_markdown="# Clip\nIt clips.", image_paths=[], visibility="public"),
    )
    assert err["code"] == "documentation_missing"
    assert err["missing"] == [
        "description too short (7 of 80 characters)",
        "README section: What it is",
        "README section: Dimensions",
        "README section: How to use",
        "README section: Materials",
        "README too short (16 of 400 characters)",
        "at least one image",
    ]
    assert err["license_required"] is True and "never invent dimensions" in err["message"]
    no_requests(api_mock)


async def test_public_needs_a_licence_chosen_by_the_user(run, api_mock, uploader, files):
    err = await run.err("upload_model", model_args(files, visibility="public"))
    assert err["code"] == "license_required" and "CC-BY-4.0" in err["licenses"]
    assert "don't pick one" in err["message"]
    no_requests(api_mock)


async def test_bad_file_types_and_sizes_fail_locally(run, api_mock, uploader, files, monkeypatch):
    err = await run.err("upload_model", model_args(files, path=str(files / "notes.txt")))
    assert err["code"] == "invalid_request" and "step" in err["message"]
    err = await run.err("upload_model", model_args(files, image_paths=[str(files / "sheet.pdf")]))
    assert err["code"] == "invalid_request" and "image" in err["message"]
    err = await run.err("upload_model", model_args(files, datasheet_paths=[str(files / "photo.png")]))
    assert err["code"] == "invalid_request" and "pdf" in err["message"]
    err = await run.err("upload_model", model_args(files, path=str(files / "missing.step")))
    assert err["code"] == "invalid_request"
    from communitycad_mcp import uploads

    monkeypatch.setattr(uploads, "MAX_FILE_BYTES", 5)
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "file_too_large" and "250 MB" in err["message"]
    no_requests(api_mock)


# -- no daily upload cap ------------------------------------------------------------


async def test_uploads_skip_account_status(run, api_mock, uploader, files, account_status):
    """No local pre-check: the server never asks account status before staging."""
    status = {**fixture("account_status_signed_in.json"), "uploads_left_today": 0}
    account_status.respond(200, json=status)
    stage, _ = staging(api_mock)
    api_mock.post(f"{V1}/models/{UUID}/images").respond(201, json=fixture("images_added.json"))
    out = await run.ok("add_images", {"model_id": UUID, "paths": [str(files / "photo.png")]})
    assert stage.called and not account_status.called
    assert "uploads_left_today" not in out and "left today" not in out["message"]


async def test_api_429_on_upload_is_passed_on(run, api_mock, uploader, files):
    stage, _ = staging(api_mock)
    route = api_mock.post(f"{V1}/models/{UUID}/images").respond(429, json=fixture("upload_limit_reached.json"))
    err = await run.err("add_images", {"model_id": UUID, "paths": [str(files / "photo.png")]})
    assert err["code"] == "upload_limit_reached" and stage.called and route.call_count == 1
    assert err["message"].startswith("Too many uploads in a short time.") and "Don't retry" in err["message"]
    assert err["reset_at"] == "2026-11-15T00:00:00Z"
    assert "10" not in err["message"] and "a day" not in err["message"]


# -- upload_model --------------------------------------------------------------------


async def test_upload_model_stages_then_creates(run, api_mock, uploader, files):
    stage, put = staging(api_mock)
    create = api_mock.post(f"{V1}/models").respond(201, json=fixture("model_created.json"))
    out = await run.ok(
        "upload_model",
        model_args(
            files,
            visibility="public",
            license="CC-BY-4.0",
            tags=["din-rail", " pcb "],
            sourcing="none",
            image_captions=["Clip on a rail"],
            datasheet_paths=[str(files / "sheet.pdf")],
        ),
    )
    assert out["page_url"] == f"https://example.test/m/{UUID}" and "uploads_left_today" not in out
    assert "left today" not in out["message"] and "processing" in out["message"]

    staged = [json.loads(c.request.content) for c in stage.calls]
    assert staged == [
        {"filename": "clip.step", "size": 21, "content_type": None},
        {"filename": "photo.png", "size": 12, "content_type": "image/png"},
        {"filename": "sheet.pdf", "size": 16, "content_type": "application/pdf"},
    ]
    assert stage.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    # The file goes to the staging URL with its headers, never with the API token.
    first = put.calls[0].request
    assert str(first.url) == f"{STAGING}/stg_example_1"
    assert first.headers["x-example-signature"] == "example-signature"
    assert first.headers["Content-Length"] == "21" and "Authorization" not in first.headers
    assert first.read() == b"ISO-10303-21; example"

    body = json.loads(create.calls[0].request.content)
    assert body == {
        "staged_upload": "stg_example_1",
        "title": "DIN rail board clip",
        "description": DESCRIPTION,
        "readme_markdown": README.strip(),
        "tags": ["din-rail", "pcb"],
        "visibility": "public",
        "license": "CC-BY-4.0",
        "is_mature": False,
        "sourcing": "none",
        "images": [{"staged_upload": "stg_example_2", "caption": "Clip on a rail"}],
        "datasheets": [{"staged_upload": "stg_example_3", "title": None}],
    }
    assert TOKEN not in json.dumps(out)


async def test_folder_is_zipped_without_junk(run, api_mock, uploader, files, tmp_path):
    folder = tmp_path / "clip-project"
    (folder / "cad").mkdir(parents=True)
    (folder / "cad" / "clip.step").write_text("step")
    (folder / "README.md").write_text("readme")
    (folder / ".git").mkdir()
    (folder / ".git" / "config").write_text("x")
    (folder / ".env").write_text("SECRET=1")
    for junk in (".venv", "node_modules", "__pycache__", "cad/__pycache__"):
        (folder / junk).mkdir(parents=True, exist_ok=True)
        (folder / junk / "junk.bin").write_text("x")
    stage, put = staging(api_mock)
    api_mock.post(f"{V1}/models").respond(201, json=fixture("model_created.json"))

    out = await run.ok("upload_model", model_args(files, path=str(folder)))
    assert out["uploaded_file"] == "clip-project.zip"
    assert json.loads(stage.calls[0].request.content)["filename"] == "clip-project.zip"
    with zipfile.ZipFile(BytesIO(put.calls[0].request.read())) as zf:
        assert sorted(zf.namelist()) == ["README.md", "cad/clip.step"]


async def test_empty_folder_refused(run, api_mock, uploader, files, tmp_path):
    folder = tmp_path / "empty"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "HEAD").write_text("x")
    err = await run.err("upload_model", model_args(files, path=str(folder)))
    assert err["code"] == "invalid_request" and "no files" in err["message"]
    no_requests(api_mock)


async def test_api_errors_are_typed(run, api_mock, uploader, files):
    staging(api_mock)
    create = api_mock.post(f"{V1}/models")

    create.respond(429, json=fixture("upload_limit_reached.json"))
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "upload_limit_reached"
    assert err["reset_at"] == "2026-11-15T00:00:00Z" and "Nothing was published" in err["message"]

    create.respond(422, json=fixture("documentation_missing.json"))
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "documentation_missing" and err["missing"] == ["README section: Dimensions"]

    create.respond(422, json={"error": "license_required", "detail": "Public models need a licence."})
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "license_required" and "MIT" in err["licenses"]

    create.respond(403, json={"error": "scope_missing", "detail": "Needs models:write."})
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "scope_missing" and "link_account(allow_uploads=true)" in err["message"]


async def test_failed_staging_put_publishes_nothing(run, api_mock, uploader, files):
    stage = api_mock.post(f"{V1}/uploads").respond(201, json=fixture("upload_staged.json"))
    api_mock.put(url__startswith=STAGING).respond(403)
    create = api_mock.post(f"{V1}/models").respond(201, json=fixture("model_created.json"))
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "upstream_unavailable" and "Nothing was published" in err["message"]
    assert stage.called and not create.called


async def test_signed_out_upload(run, api_mock, files):
    api_mock.post(f"{V1}/uploads").respond(401, json={"detail": "Not authenticated"})
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "not_signed_in"


async def test_revoked_sign_in_is_forgotten(run, api_mock, uploader, files, settings):
    api_mock.post(f"{V1}/uploads").respond(401, json={"detail": "Invalid or revoked token"})
    err = await run.err("upload_model", model_args(files))
    assert err["code"] == "not_signed_in" and "signed out" in err["note"]
    assert TokenStore(settings.token_path).load() is None


# -- versions, images, datasheets, edits -----------------------------------------------


async def test_upload_version(run, api_mock, uploader, files):
    stage, put = staging(api_mock)
    route = api_mock.post(f"{V1}/models/{UUID}/versions").respond(201, json=fixture("version_created.json"))
    out = await run.ok(
        "upload_version",
        {
            "model_id": f"https://example.test/m/{UUID}",
            "path": str(files / "clip.step"),
            "notes": "Board holes moved 2 mm outward to fit 54 x 30 mm boards.",
            "readme_markdown": README,
        },
    )
    assert out["version_number"] == 2 and "uploads_left_today" not in out
    assert out["message"].startswith("Uploaded version 2:")
    assert json.loads(route.calls[0].request.content) == {
        "staged_upload": "stg_example_1",
        "notes": "Board holes moved 2 mm outward to fit 54 x 30 mm boards.",
        "readme_markdown": README.strip(),
    }
    assert put.call_count == 1


async def test_upload_version_checks_notes_and_readme_first(run, api_mock, uploader, files):
    err = await run.err("upload_version", {"model_id": UUID, "path": str(files / "clip.step"), "notes": "fix"})
    assert err["code"] == "documentation_missing" and err["missing"] == ["version notes too short (3 of 20 characters)"]
    err = await run.err(
        "upload_version",
        {"model_id": UUID, "path": str(files / "clip.step"), "notes": "Thicker walls, 2.4 mm now.", "readme_markdown": "# Clip"},
    )
    assert "README section: Dimensions" in err["missing"]
    no_requests(api_mock)


async def test_upload_version_synced_model(run, api_mock, uploader, files):
    staging(api_mock)
    api_mock.post(f"{V1}/models/{UUID}/versions").respond(
        409, json={"error": "synced_from_github", "detail": "This model follows its GitHub repository; push the change there."}
    )
    err = await run.err(
        "upload_version", {"model_id": UUID, "path": str(files / "clip.step"), "notes": "Thicker walls, 2.4 mm now."}
    )
    assert err["code"] == "conflict" and "GitHub" in err["message"]


async def test_add_images_and_datasheets(run, api_mock, uploader, files):
    stage, put = staging(api_mock)
    (files / "side.jpg").write_bytes(b"\xff\xd8 example")
    images = api_mock.post(f"{V1}/models/{UUID}/images").respond(201, json=fixture("images_added.json"))
    out = await run.ok(
        "add_images",
        {"model_id": UUID, "paths": [str(files / "photo.png"), str(files / "side.jpg")], "captions": ["Front", "Side"]},
    )
    assert out["added"] == 2 and "left today" not in out["message"]
    assert json.loads(images.calls[0].request.content) == {
        "images": [{"staged_upload": "stg_example_1", "caption": "Front"}, {"staged_upload": "stg_example_2", "caption": "Side"}]
    }
    sheets = api_mock.post(f"{V1}/models/{UUID}/datasheets").respond(201, json=fixture("datasheets_added.json"))
    out = await run.ok("add_datasheets", {"model_id": UUID, "paths": [str(files / "sheet.pdf")], "titles": ["Rail spec"]})
    assert out["added"] == 1 and "uploads_left_today" not in out and "1 datasheet to" in out["message"]
    assert json.loads(sheets.calls[0].request.content) == {"datasheets": [{"staged_upload": "stg_example_3", "title": "Rail spec"}]}
    assert put.call_count == 3


async def test_add_files_wrong_type_or_count_refused_locally(run, api_mock, uploader, files):
    err = await run.err("add_images", {"model_id": UUID, "paths": [str(files / "clip.step")]})
    assert err["code"] == "invalid_request"
    err = await run.err("add_datasheets", {"model_id": UUID, "paths": [str(files / "photo.png")]})
    assert err["code"] == "invalid_request"
    err = await run.err("add_images", {"model_id": UUID, "paths": [str(files / "photo.png")], "captions": ["a", "b"]})
    assert err["code"] == "invalid_request" and "one caption per file" in err["message"]
    no_requests(api_mock)


async def test_edit_model(run, api_mock, uploader):
    route = api_mock.patch(f"{V1}/models/{UUID}").respond(200, json=fixture("model_edited.json"))
    out = await run.ok("edit_model", {"model_id": UUID, "visibility": "public", "license": "CC-BY-4.0", "tags": ["din"]})
    assert json.loads(route.calls[0].request.content) == {"visibility": "public", "license": "CC-BY-4.0", "tags": ["din"]}
    assert out["changed"] == ["license", "tags", "visibility"] and out["license"] == "CC-BY-4.0"
    assert "uploads_left_today" not in out


async def test_edit_model_local_checks(run, api_mock, uploader):
    err = await run.err("edit_model", {"model_id": UUID})
    assert err["code"] == "invalid_request"
    err = await run.err("edit_model", {"model_id": UUID, "description": "Short."})
    assert err["code"] == "documentation_missing"
    no_requests(api_mock)
    # Making public without a licence: refused when the model has none.
    api_mock.get(f"{V1}/catalog/models/{UUID}").respond(200, json={"uuid": UUID, "license_name": None})
    patch = api_mock.patch(f"{V1}/models/{UUID}").respond(200, json=fixture("model_edited.json"))
    err = await run.err("edit_model", {"model_id": UUID, "visibility": "public"})
    assert err["code"] == "license_required" and not patch.called


async def test_edit_model_licence_locked(run, api_mock, uploader):
    api_mock.patch(f"{V1}/models/{UUID}").respond(
        409, json={"error": "license_locked", "detail": "The licence comes from the LICENSE file in the model's ZIP; change it there."}
    )
    err = await run.err("edit_model", {"model_id": UUID, "license": "MIT"})
    assert err["code"] == "conflict" and "LICENSE file" in err["message"]
