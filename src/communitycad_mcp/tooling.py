"""Tool registration with a human title on the tool and in its annotations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.types import ToolAnnotations


def titled_tool(mcp: Any) -> Callable[..., Callable]:
    """`mcp.tool`, but the title is required and also copied into the annotations,
    where some clients and directories look for it."""

    def tool(*, title: str, annotations: ToolAnnotations, **kwargs: Any) -> Callable:
        return mcp.tool(title=title, annotations=annotations.model_copy(update={"title": title}), **kwargs)

    return tool
