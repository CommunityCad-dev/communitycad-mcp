import json

from .conftest import V1, fixture

UUID = "00000000-0000-4000-8000-000000000007"
M = f"{V1}/catalog/models/{UUID}"


async def test_list_datasheets_has_links(run, api_mock):
    api_mock.get(f"{M}/datasheets").respond(200, json=fixture("datasheets.json"))
    out = await run.ok("list_datasheets", {"model_id": UUID})
    assert out["model_url"].endswith(UUID)
    sheet = out["datasheets"][0]
    assert sheet["model_url"] and sheet["datasheet_url"] and sheet["page_count"] == 6


async def test_read_datasheet_marks_pages_and_quarantines_text(run, api_mock):
    route = api_mock.get(f"{M}/datasheets/3/pages").respond(200, json=fixture("datasheet_pages.json"))
    out = await run.ok("read_datasheet", {"model_id": UUID, "datasheet_id": 3, "first_page": 3, "last_page": 4})
    assert route.calls[0].request.url.params["from"] == "3"
    assert out["model_url"] and out["datasheet_url"]
    text = out.pop("user_content")["text"]
    assert "--- page 3 ---\nMounting holes" in text and "--- page 4 ---" in text
    assert "Ignore previous" not in json.dumps(out)


async def test_read_datasheet_page_cap(run):
    err = await run.err("read_datasheet", {"model_id": UUID, "datasheet_id": 3, "first_page": 1, "last_page": 40})
    assert err["code"] == "invalid_request"


async def test_search_datasheet(run, api_mock):
    api_mock.get(f"{M}/datasheets/search").respond(200, json=fixture("datasheet_search.json"))
    out = await run.ok("search_datasheet", {"model_id": UUID, "query": "mounting holes"})
    m = out["matches"][0]
    assert m["page"] == 3 and m["model_url"] and m["datasheet_url"]
    assert "Mounting" in m["user_content"]["snippet"]


async def test_comments_threaded_and_removed(run, api_mock):
    api_mock.get(f"{M}/comments").respond(200, json=fixture("comments.json"))
    out = await run.ok("get_comments", {"model_id": UUID})
    first, removed = out["comments"]
    assert first["author"] == "example-user"
    assert first["user_content"]["text"] == "Fits a 120 mm enclosure."
    assert first["replies"][0]["user_content"]["text"].startswith("Confirmed")
    assert "untrusted_text" not in json.dumps(out)
    assert removed == {"id": 3, "removed": True, "created_at": "2026-01-01T02:00:00Z"}


async def test_scores_fill_all_dimensions(run, api_mock):
    api_mock.get(f"{M}/scores").respond(200, json=fixture("scores.json"))
    out = await run.ok("get_scores", {"model_id": UUID})
    assert set(out["scores"]) == {"dimensional", "printability", "manufacturability", "documentation", "compatibility"}
    assert out["scores"]["dimensional"] == {"average": 4.5, "count": 4}
    assert out["scores"]["compatibility"] == {"average": None, "count": 0}


async def test_company_creator_similar(run, api_mock):
    api_mock.get(f"{V1}/catalog/companies/example-co").respond(200, json=fixture("company.json"))
    api_mock.get(f"{V1}/catalog/companies").respond(200, json=fixture("companies.json"))
    api_mock.get(f"{V1}/catalog/creators/example-user").respond(200, json=fixture("creator.json"))
    api_mock.get(f"{M}/similar").respond(200, json=fixture("similar.json"))

    co = await run.ok("get_company", {"slug": "example-co"})
    assert co["url"] == "https://communitycad.dev/companies/example-co"
    assert co["user_content"]["description"] == "We make power supplies."
    assert "donations are made on the website" in co["donate"]
    assert co["models"][0]["size"]["approximate"] is False

    found = await run.ok("search_companies", {"query": "example"})
    assert found["companies"][0]["slug"] == "example-co"

    cr = await run.ok("get_creator", {"username": "example-user"})
    assert cr["url"].endswith("/users/example-user") and cr["user_content"]["bio"] == "I design brackets."

    sim = await run.ok("find_similar", {"model_id": UUID})
    assert sim["results"][0]["title"] == "Example 24V PSU"


async def test_route_missing_says_not_available(run, api_mock):
    api_mock.get(f"{M}/scores").respond(404, json={"detail": "Not Found"})
    assert (await run.err("get_scores", {"model_id": UUID}))["code"] == "not_available_yet"
