"""Tools whose /v1 routes CommunityCAD doesn't offer yet.

They're written against the documented response shapes and tested with
hand-written fixtures. They're only registered with
COMMUNITYCAD_PREVIEW_TOOLS=1 until the routes exist; calling one against an
API without the route returns `not_available_yet`.
"""

from __future__ import annotations

from typing import Annotated, Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .api import ApiClient
from .config import Settings
from .errors import CommunityCADError
from .formatting import USER_CONTENT_NOTE, card, with_user_content

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
SCORE_DIMENSIONS = ("dimensional", "printability", "manufacturability", "documentation", "compatibility")


def register(
    mcp: MCPServer,
    api: ApiClient,
    settings: Settings,
    tool_errors: Callable,
    model_ref: Callable[[str], str],
    pages_text: Callable[[list[dict[str, Any]]], str],
) -> None:
    tool = mcp.tool

    def links(model_id: str, sheet: dict[str, Any] | None = None) -> dict[str, Any]:
        out = {"model_url": settings.model_url(model_id)}
        if sheet is not None:
            out["datasheet_url"] = sheet.get("datasheet_url") or sheet.get("pdf_url")
        return out

    # -- datasheets ---------------------------------------------------------------

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def list_datasheets(model_id: str) -> dict:
        """A model's datasheets: title, pages, size, whether text is available, and links.
        Reading datasheets never uses a download."""
        ref = model_ref(model_id)
        data = await api.datasheets(ref)
        sheets = []
        for s in data.get("items", []):
            sheets.append(
                {
                    "datasheet_id": s.get("id"),
                    "title": s.get("title"),
                    "page_count": s.get("page_count"),
                    "size_bytes": s.get("size_bytes"),
                    "has_text": s.get("has_text"),
                    **links(ref, s),
                }
            )
        return {**links(ref), "datasheets": sheets}

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def read_datasheet(
        model_id: str,
        datasheet_id: int,
        first_page: Annotated[int, Field(ge=1)] = 1,
        last_page: Annotated[int | None, Field(ge=1, description="Defaults to first_page + 4.")] = None,
    ) -> dict:
        """OCR text of a datasheet's pages, with page markers so you can cite the page.
        Always give the user the datasheet_url and model_url you used."""
        ref = model_ref(model_id)
        last = last_page or first_page + 4
        if last < first_page or last - first_page > 19:
            raise CommunityCADError("invalid_request", "Read at most 20 pages at a time, first_page <= last_page.")
        data = await api.datasheet_pages(ref, datasheet_id, first_page, last)
        return {
            **links(ref, data),
            "datasheet_id": datasheet_id,
            "page_count": data.get("page_count"),
            "pages": [p.get("page") for p in data.get("pages", [])],
            "user_content": {"note": USER_CONTENT_NOTE, "text": pages_text(data.get("pages", []))},
        }

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def search_datasheet(
        model_id: str,
        query: Annotated[str, Field(min_length=1, max_length=200)],
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict:
        """Find pages in a model's datasheets that mention something (e.g. 'mounting holes').
        Returns pages with short snippets and links; follow up with read_datasheet."""
        ref = model_ref(model_id)
        data = await api.datasheet_search(ref, query, limit)
        matches = []
        for m in data.get("items", []):
            matches.append(
                {
                    "datasheet_id": m.get("datasheet_id"),
                    "page": m.get("page"),
                    **links(ref, m),
                    "user_content": {"snippet": m.get("untrusted_text") or m.get("snippet")},
                }
            )
        return {**links(ref), "note": USER_CONTENT_NOTE, "matches": matches}

    # -- community ------------------------------------------------------------------

    def comment(c: dict[str, Any]) -> dict[str, Any]:
        if c.get("removed"):
            out = {"id": c.get("id"), "removed": True, "created_at": c.get("created_at")}
        else:
            out = with_user_content(
                {k: v for k, v in c.items() if k not in ("replies", "author")}, fields=("body",)
            )
            out["author"] = (c.get("author") or {}).get("username")
        if c.get("replies"):
            out["replies"] = [comment(r) for r in c["replies"]]
        return out

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_comments(
        model_id: str,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """Comments on a model, threaded one level deep, newest first. Read-only: to comment,
        the user goes to the model page."""
        ref = model_ref(model_id)
        data = await api.comments(ref, limit, offset)
        items = [comment(c) for c in data.get("items", [])]
        return {
            "model_url": settings.model_url(ref),
            "total": data.get("total", len(items)),
            "note": USER_CONTENT_NOTE,
            "comments": items,
        }

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_scores(model_id: str) -> dict:
        """Community accuracy scores in five dimensions (dimensional, printability,
        manufacturability, documentation, compatibility): average and number of ratings."""
        ref = model_ref(model_id)
        data = await api.scores(ref)
        dims = data.get("dimensions") or {}
        return {
            "model_url": settings.model_url(ref),
            "scores": {
                d: {"average": (dims.get(d) or {}).get("average"), "count": (dims.get(d) or {}).get("count", 0)}
                for d in SCORE_DIMENSIONS
            },
        }

    # -- profiles and neighbours --------------------------------------------------------

    def profile(data: dict[str, Any], page: str, text_fields: tuple[str, ...]) -> dict[str, Any]:
        models = [card(m, settings) for m in data.get("models", [])]
        rest = {k: v for k, v in data.items() if k not in ("models", "donation", "stripe_account_id")}
        out = with_user_content(rest, fields=text_fields)
        out["url"] = page
        if data.get("donation") or data.get("accepts_donations"):
            out["donate"] = f"{page} (donations are made on the website)"
        out["models"] = models
        return out

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_company(slug: str) -> dict:
        """A company's public profile and its models."""
        data = await api.company(model_ref(slug))
        return profile(data, f"{settings.site_url}/companies/{data.get('slug', slug)}", ("description",))

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def search_companies(
        query: Annotated[str, Field(max_length=200)] = "",
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """Search companies (vendors and makers) on CommunityCAD."""
        data = await api.companies(query, limit, offset)
        return {
            "total": data.get("total"),
            "companies": [
                {
                    "slug": c.get("slug"),
                    "name": c.get("name"),
                    "url": f"{settings.site_url}/companies/{c.get('slug')}",
                    "model_count": c.get("model_count"),
                }
                for c in data.get("items", [])
            ],
        }

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_creator(username: str) -> dict:
        """A creator's public profile and their models."""
        data = await api.creator(model_ref(username))
        return profile(data, f"{settings.site_url}/users/{data.get('username', username)}", ("bio",))

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def find_similar(model_id: str, limit: Annotated[int, Field(ge=1, le=25)] = 10) -> dict:
        """Models most similar to this one (semantic neighbours)."""
        ref = model_ref(model_id)
        data = await api.similar(ref, limit)
        return {"model_url": settings.model_url(ref), "results": [card(m, settings) for m in data.get("items", [])]}
