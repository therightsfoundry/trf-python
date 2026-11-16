"""Parameter and body-field specifications (a leaf module: no SDK imports).

:mod:`._endpoints` attaches these to each :class:`~._endpoints.EndpointSpec`;
:mod:`._validation` checks values against them. Keeping them here lets both
import the specs without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

__all__ = ["LIST_QUERY_PARAMS", "BodyField", "BodyKind", "QueryKind", "QueryParam"]

QueryKind = Literal["int", "number", "str", "bool", "enum"]
BodyKind = Literal["text", "file_url", "int", "bool"]


@dataclass(frozen=True)
class QueryParam:
    """One allowlisted query parameter, mirroring the API's documented query validation."""

    name: str  # Python keyword
    wire: str  # name on the wire
    kind: QueryKind
    max_length: Optional[int] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: tuple[str, ...] = ()
    description: str = ""


#: ``GET /v1/tracks`` query parameters, in the order they are documented.
LIST_QUERY_PARAMS: tuple[QueryParam, ...] = (
    QueryParam("search", "search", "str", max_length=200, description="free-text search"),
    QueryParam("genre", "genre", "str", max_length=100),
    QueryParam("mood", "mood", "str", max_length=100),
    QueryParam("instrument", "instrument", "str", max_length=100),
    QueryParam("theme", "theme", "str", max_length=100),
    QueryParam("artist", "artist", "str", max_length=200),
    QueryParam("album", "album", "str", max_length=200),
    QueryParam("vocals", "vocals", "enum", choices=("instrumental", "male", "female", "any")),
    QueryParam("key", "key", "str", max_length=12, description="musical key, e.g. 'A minor'"),
    QueryParam("bpm_min", "bpmMin", "number", minimum=0),
    QueryParam("bpm_max", "bpmMax", "number", minimum=0, maximum=999),
    QueryParam("duration_min", "durationMin", "number", minimum=0),
    QueryParam("duration_max", "durationMax", "number", minimum=0, maximum=99999),
    QueryParam("featured", "featured", "bool"),
    QueryParam("explicit", "explicit", "bool"),
    QueryParam("decade", "decade", "str", max_length=10),
    QueryParam("id", "id", "int", minimum=1, maximum=999_999_999_999_999),
    QueryParam("include_children", "includeChildren", "bool"),
    QueryParam(
        "sort_by",
        "sortBy",
        "enum",
        choices=(
            "relevance",
            "first_published",
            "created_at",
            "title",
            "artist",
            "bpm",
            "duration",
        ),
    ),
    QueryParam("sort_order", "sortOrder", "enum", choices=("asc", "desc")),
    QueryParam("page", "page", "int", minimum=1),
    QueryParam("limit", "limit", "int", minimum=1, maximum=100),
)


@dataclass(frozen=True)
class BodyField:
    """One JSON request-body property, parallel to :class:`QueryParam`.

    ``kind`` selects the validator: ``text`` (trimmed, non-empty, at most
    ``max_length`` UTF-16 units), ``file_url`` (the strict reference-audio URL
    rules), ``int`` (bounded integer) or ``bool`` (a real ``bool``).
    """

    name: str
    kind: BodyKind
    default: Any = None
    required: bool = False
    max_length: Optional[int] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    description: str = ""
