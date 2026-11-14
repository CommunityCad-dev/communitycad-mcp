"""Datasheet, community, profile and similar-model tools."""

from __future__ import annotations

from typing import Annotated, Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .api import ApiClient
from .config import Settings
from .errors import CommunityCADError
from .formatting import USER_CONTENT_NOTE, card

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
SCORE_DIMENSIONS = ("dimensional", "printability", "manufacturability", "documentation", "compatibility")
MAX_DATASHEET_PAGES = 20


def register(
    mcp: MCPServer,
    api: ApiClient,
    settings: Settings,
    tool_errors: Callable,
    model_ref: Callable[[str], str],
    pages_text: Callable[[list[dict[str, Any]]], str],
) -> None:
    tool = mcp.tool

    def links(ref: str, data: dict[str, Any]) -> dict[str, Any]:
        out = {"model_url": data.get("model_url") or settings.model_url(ref)}
        if "datasheet_url" in data or "pdf_url" in data:
            out["datasheet_url"] = data.get("datasheet_url") or data.get("pdf_url")
            out["pdf_url"] = data.get("pdf_url")
        return out

    # -- datasheets ---------------------------------------------------------------

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def list_datasheets(model_id: str) -> dict:
        """A model's datasheets: file name, pages, size, whether OCR text is available, and
        links. Reading datasheets never uses a download."""
        ref = model_ref(model_id)
        data = await api.datasheets(ref)
        sheets = [
            {
                "datasheet_id": s.get("id"),
                "file_name": s.get("file_name"),
                "page_count": s.get("page_count"),
                "pages_with_text": s.get("pages_with_text"),
                "size_bytes": s.get("size_bytes"),
                "has_text": s.get("has_text"),
                **links(ref, s),
            }
            for s in data.get("items", [])
        ]
        return {"model_url": data.get("model_url") or settings.model_url(ref), "datasheets": sheets}

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def read_datasheet(
        model_id: str,
        datasheet_id: int,
        first_page: Annotated[int, Field(ge=1)] = 1,
        last_page: Annotated[int | None, Field(ge=1, description="Defaults to first_page + 4. At most 20 pages per call.")] = None,
    ) -> dict:
        """OCR text of a datasheet's pages, with page markers so you can cite the page.
        Always give the user the datasheet_url and model_url you used."""
        ref = model_ref(model_id)
        last = last_page or first_page + 4
        if last < first_page or last - first_page >= MAX_DATASHEET_PAGES:
            raise CommunityCADError("invalid_request", "Read at most 20 pages at a time, with first_page <= last_page.")
        data = await api.datasheet_pages(ref, datasheet_id, first_page, last)
        pages = data.get("pages", [])
        return {
            **links(ref, data),
            "datasheet_id": datasheet_id,
            "file_name": data.get("file_name"),
            "page_count": data.get("page_count"),
            "from_page": data.get("from_page"),
            "to_page": data.get("to_page"),
            "user_content": {"note": USER_CONTENT_NOTE, "text": pages_text(pages)},
        }

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def search_datasheet(
        model_id: str,
        query: Annotated[str, Field(min_length=2, max_length=200)],
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> dict:
        """Find pages in a model's datasheets that mention something (e.g. 'mounting holes').
        Returns pages with short snippets and links; follow up with read_datasheet."""
        ref = model_ref(model_id)
        data = await api.datasheet_search(ref, query)
        hits = data.get("hits", [])
        matches = [
            {
                "datasheet_id": h.get("datasheet_id"),
                "file_name": h.get("file_name"),
                "page": h.get("page"),
                "match_count": h.get("match_count"),
                **links(ref, h),
                "user_content": {"snippet": h.get("untrusted_text")},
            }
            for h in hits[:limit]
        ]
        return {
            "model_url": data.get("model_url") or settings.model_url(ref),
            "note": USER_CONTENT_NOTE,
            "matches": matches,
            "more": bool(data.get("truncated")) or len(hits) > limit,
        }

    # -- community ------------------------------------------------------------------

    def comment(c: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"id": c.get("id"), "created_at": c.get("created_at")}
        if c.get("parent_id") is not None:
            out["parent_id"] = c["parent_id"]
        if c.get("removed"):
            out["removed"] = True
        else:
            author = c.get("author") or {}
            out.update(
                {
                    "author": author.get("username"),
                    "is_creator": bool(c.get("is_creator")),
                    "score": c.get("score"),
                    "edited": bool(c.get("edited")),
                    "user_content": {"text": c.get("untrusted_text")},
                }
            )
        if "replies" in c:
            out["replies"] = [comment(r) for r in c.get("replies") or []]
            out["reply_count"] = c.get("reply_count", len(out["replies"]))
        return out

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_comments(
        model_id: str,
        order: Literal["newest", "oldest"] = "newest",
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
        replies: Annotated[int, Field(ge=0, le=20, description="Most replies to include per comment.")] = 5,
    ) -> dict:
        """Comments on a model, threaded one level deep. Removed comments show as removed.
        Read-only: to comment, the user goes to the model page."""
        ref = model_ref(model_id)
        data = await api.comments(ref, limit, offset, order, replies)
        items = [comment(c) for c in data.get("items", [])]
        total = data.get("total", len(items))
        out: dict[str, Any] = {
            "model_url": data.get("model_url") or settings.model_url(ref),
            "total": total,
            "note": USER_CONTENT_NOTE,
            "comments": items,
        }
        if offset + len(items) < total:
            out["next_offset"] = offset + len(items)
        return out

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_scores(model_id: str) -> dict:
        """Community accuracy scores in five dimensions (dimensional, printability,
        manufacturability, documentation, compatibility): average and number of ratings."""
        ref = model_ref(model_id)
        data = await api.scores(ref)
        dims = data.get("dimensions") or {}
        return {
            "model_url": data.get("model_url") or settings.model_url(ref),
            "ratings": data.get("count", 0),
            "overall": data.get("overall"),
            "scores": {
                d: {"average": (dims.get(d) or {}).get("average"), "count": (dims.get(d) or {}).get("count", 0)}
                for d in SCORE_DIMENSIONS
            },
        }

    # -- profiles and neighbours --------------------------------------------------------

    def models_page(data: dict[str, Any]) -> dict[str, Any]:
        page = data.get("models") or {}
        items = page.get("items", []) if isinstance(page, dict) else page
        total = page.get("total") if isinstance(page, dict) else len(items)
        return {"total": total, "items": [card(m, settings) for m in items]}

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_company(
        slug: Annotated[str, Field(description="The company's slug (from search_companies or a model) or id.")],
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """A company's public profile and its models."""
        data = await api.company(model_ref(slug), limit, offset)
        return {
            "slug": data.get("slug"),
            "name": data.get("name"),
            "url": data.get("page_url"),
            "website_url": data.get("website_url"),
            "verified": data.get("verified"),
            "model_count": data.get("model_count"),
            "followers": data.get("follower_count"),
            "user_content": {"note": USER_CONTENT_NOTE, "description": data.get("untrusted_text") or data.get("description")},
            "models": models_page(data),
        }

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
                    "url": c.get("page_url"),
                    "verified": c.get("verified"),
                    "model_count": c.get("model_count"),
                }
                for c in data.get("items", [])
            ],
        }

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def get_creator(
        username: str,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict:
        """A creator's public profile and their models. Donations are made on the website;
        `donate_url` is a link to that page, never a payment action."""
        data = await api.creator(model_ref(username), limit, offset)
        out = {
            "username": data.get("username"),
            "display_name": data.get("display_name"),
            "url": data.get("page_url"),
            "website_url": data.get("website_url"),
            "model_count": data.get("model_count"),
            "total_downloads": data.get("total_downloads"),
            "user_content": {"note": USER_CONTENT_NOTE, "bio": data.get("untrusted_text") or data.get("bio")},
            "models": models_page(data),
        }
        if data.get("donation_url"):
            out["donate_url"] = data["donation_url"]
        return out

    @tool(annotations=READ_ONLY)
    @tool_errors
    async def find_similar(model_id: str, limit: Annotated[int, Field(ge=1, le=25)] = 10) -> dict:
        """Public models most similar to this one, closest first. Empty when the model
        hasn't been analysed yet."""
        ref = model_ref(model_id)
        data = await api.similar(ref, limit)
        return {"model_url": data.get("model_url") or settings.model_url(ref), "results": [card(m, settings) for m in data.get("items", [])]}
