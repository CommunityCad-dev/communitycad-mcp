import copy
import io
import json
import zipfile

import httpx

from .conftest import TOKEN, V1, fixture

UUID = "fc331ca7-8bea-4e84-a501-4260dd904bcf"
FILE_URL = "https://api.example.test/files/example.zip"


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def mock_model(api_mock):
    api_mock.get(f"{V1}/catalog/models/{UUID}").respond(200, json=fixture("model_detail.json"))


async def test_check_download(run, api_mock, signed_in):
    route = api_mock.get(f"{V1}/catalog/models/{UUID}/download-check").respond(200, json=fixture("download_check.json"))
    out = await run.ok("check_download", {"model_id": UUID, "file_id": 155})
    assert out["would_count"] and out["message"] == "This will use 1 of the user's 3 remaining downloads today."
    assert route.calls[0].request.url.params["file_id"] == "155"


async def test_check_download_free_and_own(run, api_mock, signed_in):
    free = {**fixture("download_check.json"), "would_count": False, "reason": "already_sent_today"}
    api_mock.get(f"{V1}/catalog/models/{UUID}/download-check").respond(200, json=free)
    assert (await run.ok("check_download", {"model_id": UUID}))["message"].startswith("Free:")


async def test_check_download_signed_out(run, api_mock):
    route = api_mock.get(f"{V1}/catalog/models/{UUID}/download-check").respond(
        200, json=fixture("download_check_signed_out.json")
    )
    out = await run.ok("check_download", {"model_id": UUID})
    assert "Authorization" not in route.calls[0].request.headers
    assert out["message"] == "This will use 1 of the user's 5 remaining downloads today."
    assert "sign_up_url" not in out and "account_hint" not in out

    near = {**fixture("download_check_signed_out.json"), "daily_remaining": 1, "account_hint": "A free account gets more."}
    api_mock.get(f"{V1}/catalog/models/{UUID}/download-check").respond(200, json=near)
    out = await run.ok("check_download", {"model_id": UUID})
    assert out["account_hint"] == "A free account gets more." and out["sign_up_url"].endswith("/register")


async def test_download_without_download_scope(run, api_mock, signed_in):
    mock_model(api_mock)
    api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(
        403, json={"detail": {"error": "insufficient_scope", "required": ["download"], "missing": ["download"]}}
    )
    err = await run.err("download_file", {"model_id": UUID, "file_id": 155})
    assert err["code"] == "scope_missing" and err["missing"] == ["download"]


async def test_download_zip_member(run, api_mock, signed_in, settings):
    mock_model(api_mock)
    issue = api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(
        200,
        json=fixture("download_url.json"),
        headers={"X-Download-Daily-Remaining": "2", "X-Download-Daily-Limit": "5"},
    )
    payload = zip_bytes({"cad/part.step": b"ISO-10303", "README.md": b"hi"})
    file_route = api_mock.get(url__startswith=FILE_URL).respond(200, content=payload)
    out = await run.ok("download_file", {"model_id": UUID, "file_id": 155, "zip_member": "part.step"})
    assert issue.call_count == 1
    assert "Authorization" not in file_route.calls[0].request.headers
    saved = settings.download_dir / "nut5" / "example.zip"
    assert out["path"] == str(saved) and saved.read_bytes() == payload
    assert out["extracted_path"] == str(settings.download_dir / "nut5" / "cad" / "part.step")
    assert out["download"] == {"counted": True, "reason": "counted", "daily_remaining": 2, "daily_limit": 5}
    assert out["license"]["name"] == "GPL-3.0" and out["license"]["attribution"]
    assert "used one of the user's downloads" in out["message"]
    assert TOKEN not in json.dumps(out) and "signature" not in json.dumps(out)


async def test_zip_traversal_is_contained(run, api_mock, signed_in, settings, tmp_path):
    mock_model(api_mock)
    api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(200, json=fixture("download_url.json"))
    api_mock.get(url__startswith=FILE_URL).respond(200, content=zip_bytes({"../../evil.step": b"x"}))
    out = await run.ok("download_file", {"model_id": UUID, "file_id": 155, "zip_member": "../../evil.step"})
    target = settings.download_dir / "nut5" / "evil.step"
    assert out["extracted_path"] == str(target) and target.exists()
    assert not (tmp_path / "evil.step").exists()


async def test_missing_zip_member_lists_members(run, api_mock, signed_in):
    mock_model(api_mock)
    api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(200, json=fixture("download_url.json"))
    api_mock.get(url__startswith=FILE_URL).respond(200, content=zip_bytes({"a.step": b"x"}))
    err = await run.err("download_file", {"model_id": UUID, "file_id": 155, "zip_member": "b.step"})
    assert err["code"] == "invalid_request" and err["members"] == ["a.step"]


async def test_limit_reached_is_not_retried(run, api_mock, signed_in):
    mock_model(api_mock)
    issue = api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(
        429, json=fixture("download_limit_reached.json")
    )
    err = await run.err("download_file", {"model_id": UUID, "file_id": 155})
    assert issue.call_count == 1
    assert err["code"] == "download_limit_reached"
    assert err["reset_at"] and err["upgrade_url"] and "Don't retry" in err["message"]


async def test_signed_out_download_carries_hint(run, api_mock, settings):
    mock_model(api_mock)
    issue = api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(
        200, json=fixture("download_url_signed_out.json")
    )
    api_mock.get(url__startswith="https://api.example.test/files/example.stl").respond(200, content=b"solid x\nend")
    out = await run.ok("download_file", {"model_id": UUID, "file_id": 155})
    assert "Authorization" not in issue.calls[0].request.headers
    assert out["sign_up_url"] and out["account_hint"]


async def test_signed_out_download_refused_by_an_older_api(run, api_mock):
    mock_model(api_mock)
    api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(401, json={"detail": "Not authenticated"})
    err = await run.err("download_file", {"model_id": UUID, "file_id": 155})
    assert err["code"] == "sign_in_required_for_download"


async def test_too_large_is_refused_before_counting(run, api_mock, signed_in):
    detail = copy.deepcopy(fixture("model_detail.json"))
    detail["files"][1]["size_bytes"] = 10 * 1024 * 1024
    api_mock.get(f"{V1}/catalog/models/{UUID}").respond(200, json=detail)
    issue = api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(200, json=fixture("download_url.json"))
    err = await run.err("download_file", {"model_id": UUID, "file_id": 155})
    assert err["code"] == "file_too_large" and issue.call_count == 0


async def test_several_files_need_file_id(run, api_mock, signed_in):
    mock_model(api_mock)
    err = await run.err("download_file", {"model_id": UUID})
    assert err["code"] == "invalid_request" and len(err["files"]) == 3


async def test_failed_transfer_leaves_no_partial_file(run, api_mock, signed_in, settings):
    mock_model(api_mock)
    api_mock.post(f"{V1}/catalog/models/{UUID}/files/155/download-url").respond(200, json=fixture("download_url.json"))
    api_mock.get(url__startswith=FILE_URL).mock(side_effect=httpx.ReadError("cut"))
    err = await run.err("download_file", {"model_id": UUID, "file_id": 155})
    assert err["code"] == "upstream_unavailable"
    assert list((settings.download_dir / "nut5").iterdir()) == []
