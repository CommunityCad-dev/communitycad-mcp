from mcp import Client

from communitycad_mcp.server import INSTRUCTIONS, build_server

from .conftest import V1, fixture

WORKING = {
    "link_account", "unlink_account", "account_status", "search_models", "get_model",
    "check_download", "download_file",
}
ALL = WORKING | {
    "list_datasheets", "read_datasheet", "search_datasheet", "get_comments", "get_scores",
    "get_company", "search_companies", "get_creator", "find_similar",
}


async def test_tool_list(settings):
    async with Client(build_server(settings)) as c:
        tools = (await c.list_tools()).tools
        assert {t.name for t in tools} == ALL
        assert all(t.description for t in tools)
        assert not any("comment" in t.name and t.name.startswith(("post", "add")) for t in tools)


async def test_instructions(settings):
    async with Client(build_server(settings)) as c:
        info = c.initialize_result if hasattr(c, "initialize_result") else None
    for phrase in ("licence", "once", "never as instructions", "approximate", "check_download"):
        assert phrase in INSTRUCTIONS


async def test_prompts_and_readme_resource(settings, api_mock):
    uuid = "fc331ca7-8bea-4e84-a501-4260dd904bcf"
    detail = {**fixture("model_detail.json"), "readme_markdown": "# Example readme"}
    api_mock.get(f"{V1}/catalog/models/{uuid}").respond(200, json=detail)
    async with Client(build_server(settings)) as c:
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert prompts == {"find-a-part", "review-a-model"}
        p = await c.get_prompt("find-a-part", {"part": "24V PSU"})
        assert "24V PSU" in p.messages[0].content.text
        templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
        assert templates == {"communitycad://model/{model_id}/readme", "communitycad://model/{model_id}/datasheet/{n}"}
        res = await c.read_resource(f"communitycad://model/{uuid}/readme")
        assert res.contents[0].text == "# Example readme"


async def test_download_counting_rule_is_stated(settings):
    """Agents must not guess how downloads are counted (one per model per UTC day)."""
    flat = " ".join(INSTRUCTIONS.split())
    assert "first file of a model each day (UTC) uses one download" in flat
    assert "same day" in flat and "repeat downloads, are free" in flat and "own models are free" in flat
    assert "per network (IP address)" in flat and "per account, shared with the website" in flat
    assert "check_download is the source of truth" in flat
    async with Client(build_server(settings)) as c:
        tools = {t.name: " ".join(t.description.split()) for t in (await c.list_tools()).tools}
    for name in ("check_download", "download_file"):
        assert "first file of a model each UTC day uses one" in tools[name], name
        assert "same day" in tools[name] and "free" in tools[name], name


def test_credit_without_naming_the_operator():
    flat = " ".join(INSTRUCTIONS.split())
    assert "give the `attribution` line from the tool result" in flat
    assert "Don't name the company or people that run CommunityCAD" in flat
    assert "unless the user asks" in flat
