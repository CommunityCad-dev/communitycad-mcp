import json

from .conftest import V1, fixture

INA = "485bfaea-e88d-41d9-bfa4-fa2693df2042"
TEST = "908a55bb-a963-42cd-ac9c-6bbc575613f0"
M = f"{V1}/catalog/models/{INA}"


async def test_list_datasheets_has_links(run, api_mock):
    api_mock.get(f"{M}/datasheets").respond(200, json=fixture("datasheets.json"))
    out = await run.ok("list_datasheets", {"model_id": INA})
    assert out["model_url"].endswith(INA)
    sheet = out["datasheets"][0]
    assert sheet["datasheet_id"] == 1 and sheet["page_count"] == 38 and sheet["has_text"]
    assert sheet["model_url"] and sheet["datasheet_url"] and sheet["pdf_url"]


async def test_read_datasheet_marks_pages_and_quarantines_text(run, api_mock):
    route = api_mock.get(f"{M}/datasheets/1/pages").respond(200, json=fixture("datasheet_pages.json"))
    out = await run.ok("read_datasheet", {"model_id": INA, "datasheet_id": 1, "first_page": 1, "last_page": 2})
    q = route.calls[0].request.url.params
    assert q["from"] == "1" and q["to"] == "2"
    assert out["model_url"] and out["datasheet_url"] and (out["from_page"], out["to_page"]) == (1, 2)
    text = out.pop("user_content")["text"]
    assert "--- page 1 ---\nExample text" in text and "--- page 2 ---" in text
    assert "Example text" not in json.dumps(out)


async def test_read_datasheet_page_cap(run, api_mock):
    route = api_mock.get(f"{M}/datasheets/1/pages")
    err = await run.err("read_datasheet", {"model_id": INA, "datasheet_id": 1, "first_page": 1, "last_page": 21})
    assert err["code"] == "invalid_request" and route.call_count == 0


async def test_search_datasheet(run, api_mock):
    data = fixture("datasheet_search.json")
    route = api_mock.get(f"{M}/datasheets/search").respond(200, json=data)
    out = await run.ok("search_datasheet", {"model_id": INA, "query": "gain", "limit": 2})
    assert route.calls[0].request.url.params["q"] == "gain"
    assert len(out["matches"]) == 2 and out["more"] is (len(data["hits"]) > 2 or data["truncated"])
    m = out["matches"][0]
    assert m["page"] == 1 and m["model_url"] and m["datasheet_url"]
    assert "untrusted_text" not in json.dumps(out) and m["user_content"]["snippet"]


async def test_comments_threaded_and_removed(run, api_mock):
    route = api_mock.get(f"{V1}/catalog/models/{TEST}/comments").respond(200, json=fixture("comments.json"))
    out = await run.ok("get_comments", {"model_id": TEST, "order": "oldest", "limit": 3, "replies": 2})
    q = route.calls[0].request.url.params
    assert q["order"] == "oldest" and q["replies"] == "2"
    first = out["comments"][0]
    assert first["author"] == "example-user-3" and first["reply_count"] == 9
    assert first["user_content"]["text"].startswith("Example text")
    assert first["replies"][0]["is_creator"] is True and first["replies"][0]["parent_id"] == first["id"]
    assert "untrusted_text" not in json.dumps(out)
    removed = [r for c in out["comments"] for r in [c, *c.get("replies", [])] if r.get("removed")]
    assert removed and all("author" not in r and "user_content" not in r for r in removed)
    assert out["next_offset"] == 3


async def test_scores(run, api_mock):
    api_mock.get(f"{M}/scores").respond(200, json=fixture("scores.json"))
    out = await run.ok("get_scores", {"model_id": INA})
    assert set(out["scores"]) == {"dimensional", "printability", "manufacturability", "documentation", "compatibility"}
    assert out["scores"]["dimensional"] == {"average": 3.0, "count": 2}
    assert out["ratings"] == 2 and out["overall"] == 4.0


async def test_scores_empty(run, api_mock):
    empty = {"count": 0, "overall": None, "dimensions": {d: {"average": None, "count": 0} for d in (
        "dimensional", "printability", "manufacturability", "documentation", "compatibility")}}
    api_mock.get(f"{M}/scores").respond(200, json=empty)
    out = await run.ok("get_scores", {"model_id": INA})
    assert out["ratings"] == 0 and out["scores"]["compatibility"] == {"average": None, "count": 0}


async def test_company_and_search(run, api_mock):
    route = api_mock.get(f"{V1}/companies/acme-corp").respond(200, json=fixture("company.json"))
    api_mock.get(f"{V1}/companies").respond(200, json=fixture("companies.json"))
    co = await run.ok("get_company", {"slug": "acme-corp", "limit": 2})
    assert route.calls[0].request.url.params["limit"] == "2"
    assert co["url"].endswith("/companies/acme-corp") and co["model_count"] == 7
    assert co["user_content"]["description"].startswith("Example text")
    assert co["models"]["total"] == 7 and co["models"]["items"][0]["url"].startswith("https://communitycad.dev/m/")
    found = await run.ok("search_companies", {"query": "acme"})
    assert found["companies"][0]["slug"] == "acme-corp"


async def test_creator(run, api_mock):
    data = fixture("creator.json")
    api_mock.get(f"{V1}/creators/example-user-2").respond(200, json={**data, "donation_url": "https://example.test/users/example-user-2#donate"})
    cr = await run.ok("get_creator", {"username": "example-user-2"})
    assert cr["url"].endswith("/users/example-user-2") and cr["user_content"]["bio"].startswith("Example")
    assert cr["donate_url"].endswith("#donate") and cr["models"]["items"]


async def test_similar(run, api_mock):
    api_mock.get(f"{M}/similar").respond(200, json=fixture("similar.json"))
    sim = await run.ok("find_similar", {"model_id": INA, "limit": 2})
    assert len(sim["results"]) == 2 and all(r["url"] for r in sim["results"])


async def test_route_missing_says_not_available(run, api_mock):
    api_mock.get(f"{M}/scores").respond(404, json={"detail": "Not Found"})
    assert (await run.err("get_scores", {"model_id": INA}))["code"] == "not_available_yet"


async def test_datasheet_resource_reads_in_chunks(settings, api_mock):
    from mcp import Client

    from communitycad_mcp.server import build_server

    api_mock.get(f"{M}/datasheets").respond(200, json=fixture("datasheets.json"))
    pages = api_mock.get(f"{M}/datasheets/1/pages").respond(200, json=fixture("datasheet_pages.json"))
    async with Client(build_server(settings)) as c:
        res = await c.read_resource(f"communitycad://model/{INA}/datasheet/1")
    text = res.contents[0].text
    assert text.startswith("Datasheet: https://example.test/") and "--- page 1 ---" in text
    assert [(r.request.url.params["from"], r.request.url.params["to"]) for r in pages.calls] == [("1", "20"), ("21", "38")]
