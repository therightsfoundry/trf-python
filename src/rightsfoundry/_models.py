"""Typed, frozen response models.

* Every model keeps the original JSON in ``.raw`` — a deep, immutable
  :class:`FrozenMap` — so fields the API adds later are available before the SDK
  names them.
* Fields are declarative: a field's annotation picks its JSON decoder
  (``Optional[str]`` → string or ``None``, ``tuple[str, ...]`` → list of strings,
  …), so a new API field is one annotated line on the dataclass.
* Unknown fields never raise; unknown enum values never raise (enums are open:
  typed as ``str``).
* Nullability follows the API server: every AI musical analysis column,
  ``asset_url`` and ``peak_data_url`` can be ``None``.
* Models pickle and ``copy.deepcopy`` (for multiprocessing, task queues and
  caches). Signed URLs stay intact on the model (your application needs them)
  but every ``repr`` / ``str`` — including ``str(model.raw)`` — redacts their
  ``sig`` (SEC-07).
"""

from __future__ import annotations

import datetime as _dt
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields
from typing import TYPE_CHECKING, Any, Callable, NoReturn, Optional, TypeVar

from ._errors import UnexpectedResponseError
from ._redact import redact_text, redact_value

if TYPE_CHECKING:
    from typing_extensions import dataclass_transform
else:  # pragma: no cover - runtime shim; only type checkers need the real thing

    def dataclass_transform(**_: Any) -> Callable[[Any], Any]:
        return lambda f: f


__all__ = [
    "BandwidthBalance",
    "BillingCheck",
    "CoverageLevel",
    "CreditBalance",
    "Credits",
    "DatabaseCheck",
    "DurationConstraint",
    "EmptyConstraintReason",
    "ErrorEvent",
    "ExpiringBatch",
    "HealthStatus",
    "LivenessStatus",
    "LowCreditEvent",
    "PlanDiscount",
    "ReadinessStatus",
    "RequestEvent",
    "ResponseEvent",
    "SearchConstraints",
    "SearchCorrection",
    "SearchCoverage",
    "SearchMatch",
    "SubscriptionInfo",
    "SyncBilling",
    "SyncLink",
    "Taxonomies",
    "TaxonomyItem",
    "TokenUsage",
    "Track",
    "TrackPage",
    "FrozenMap",
    "freeze",
    "thaw",
]

_T = TypeVar("_T")


class FrozenMap(Mapping[str, Any]):
    """An immutable, hashable, picklable read-only mapping (the type of every ``.raw``).

    * ``m["download_url"]`` returns the real value;
    * ``repr(m)`` / ``str(m)`` go through the SDK redactor, so printing a model's
      ``raw`` never shows a live ``sig=`` signature;
    * pickling, ``copy.copy`` and ``copy.deepcopy`` work (unlike ``MappingProxyType``),
      so models cross process boundaries and caches.
    """

    __slots__ = ("_data", "_hash")

    _data: dict[str, Any]
    _hash: Optional[int]

    def __init__(self, data: Optional[Mapping[str, Any]] = None) -> None:
        object.__setattr__(self, "_data", dict(data) if data is not None else {})
        object.__setattr__(self, "_hash", None)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __eq__(self, other: object) -> bool:
        if isinstance(other, FrozenMap):
            return self._data == other._data
        if isinstance(other, Mapping):
            return self._data == dict(other.items())
        return NotImplemented

    def __hash__(self) -> int:
        if self._hash is None:
            object.__setattr__(self, "_hash", hash(frozenset((k, _hashable(v)) for k, v in self._data.items())))
        return self._hash  # type: ignore[return-value]

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise AttributeError("FrozenMap is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("FrozenMap is immutable")

    def __repr__(self) -> str:
        return repr(redact_value(self))

    __str__ = __repr__

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenMap, (self._data,))

    def __copy__(self) -> FrozenMap:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> FrozenMap:
        return self


def _hashable(value: Any) -> Any:
    if isinstance(value, (FrozenMap, tuple, str, int, float, bool)) or value is None:
        return value
    return repr(value)


_EMPTY: Mapping[str, Any] = FrozenMap()


_DATACLASS_OPTIONS: dict[str, bool] = (
    {"frozen": True, "slots": True} if sys.version_info >= (3, 10) else {"frozen": True}
)


@dataclass_transform(frozen_default=True, field_specifiers=(field,))
def model(cls: type[_T]) -> type[_T]:
    """``@dataclass(frozen=True, slots=True)`` on 3.10+, ``frozen=True`` on 3.9."""
    return dataclass(**_DATACLASS_OPTIONS)(cls)


def _raw_field() -> Any:
    return field(default=_EMPTY, repr=False, compare=False)


def freeze(value: Any, _depth: int = 0) -> Any:
    """Deep read-only copy: dicts → :class:`FrozenMap`, lists → tuples."""
    if _depth > 64:
        raise UnexpectedResponseError("response nesting exceeds 64 levels")
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap({str(k): freeze(v, _depth + 1) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v, _depth + 1) for v in value)
    return value


def thaw(value: Any) -> Any:
    """Inverse of :func:`freeze`: plain ``dict`` / ``list`` for JSON serialisation."""
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


class _ModelMixin:
    """``to_dict()`` for every model: the original JSON as plain Python objects."""

    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        raw = getattr(self, "raw", _EMPTY)
        result = thaw(raw)
        return result if isinstance(result, dict) else {}


# --- decoding helpers ------------------------------------------------------------


def _bad(what: str) -> UnexpectedResponseError:
    return UnexpectedResponseError(f"unexpected response shape: {what}")


def _obj(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _bad(f"{what} is not an object")
    return value


def _opt_obj(value: Any) -> Optional[Mapping[str, Any]]:
    return value if isinstance(value, Mapping) else None


def _int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise _bad(f"{what} is not an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise _bad(f"{what} is not an integer")


def _opt_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _opt_num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _opt_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _strs(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def _opt_strs(value: Any) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return _strs(value)


def _list(value: Any) -> Sequence[Any]:
    return value if isinstance(value, (list, tuple)) else ()


def _redact_url_repr(url: Optional[str]) -> Optional[str]:
    return None if url is None else redact_text(url)


# --- declarative decoding ----------------------------------------------------------
#
# A model field's annotation selects its decoder, so adding a field the API
# started returning is a single line on the dataclass. Field metadata refines it:
#   STRICT            the value must be an integer (else UnexpectedResponseError)
#   json_key="from"   read a JSON key that is not a valid Python name
#   json=callable     a custom decoder
#   json=False        never read from JSON (set by the caller instead)
#   absent=<value>    the value to use when the key is missing (strict fields)

#: ``field(metadata=STRICT)``: a required integer that must be present and integral.
STRICT: Mapping[str, Any] = {"strict": True}

_DECODERS: Mapping[str, Callable[[Any], Any]] = {
    "Optional[str]": lambda v: _opt_str(v),
    "Optional[int]": lambda v: _opt_int(v),
    "Optional[float]": lambda v: _opt_num(v),
    "Optional[bool]": lambda v: _opt_bool(v),
    "tuple[str, ...]": lambda v: _strs(v),
    "Optional[tuple[str, ...]]": lambda v: _opt_strs(v),
}

#: Fields the caller supplies (transport metadata), never decoded from the body.
_CALLER_FIELDS = frozenset({"raw", "request_id", "retried_after_refundable_failure", "http_status", "retry_after"})

_Plan = tuple[tuple[str, str, Callable[[Any, str], Any], Any], ...]
_PLANS: dict[type[Any], _Plan] = {}
_ABSENT = object()


def _scalar_decoder(f: Any) -> Optional[Callable[[Any, str], Any]]:
    meta = f.metadata
    custom = meta.get("json")
    if callable(custom):
        return lambda v, _what: custom(v)
    annotation = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", "")
    simple = _DECODERS.get(annotation)
    if simple is not None:
        return lambda v, _what: simple(v)
    default = f.default if f.default is not MISSING else None
    if annotation == "int":
        if meta.get("strict"):
            return lambda v, what: _int(v, what)
        fallback = default if isinstance(default, int) else 0
        return lambda v, _what: (lambda i: fallback if i is None else i)(_opt_int(v))
    if annotation == "str":
        text_fallback = default if isinstance(default, str) else ""
        return lambda v, _what: v if isinstance(v, str) else text_fallback
    if annotation == "bool":
        return lambda v, _what: v is True
    return None  # nested model: the from_json that owns it passes it explicitly


def _plan(cls: type[Any]) -> _Plan:
    plan = _PLANS.get(cls)
    if plan is None:
        entries = []
        for f in fields(cls):
            if f.name in _CALLER_FIELDS or f.metadata.get("json") is False:
                continue
            decoder = _scalar_decoder(f)
            if decoder is not None:
                entries.append((f.name, f.metadata.get("json_key", f.name), decoder, f.metadata.get("absent", _ABSENT)))
        plan = _PLANS[cls] = tuple(entries)
    return plan


def decode_model(cls: type[_T], obj: Mapping[str, Any], what: str, /, **explicit: Any) -> _T:
    """Build ``cls`` from a JSON object: every declared scalar field is decoded from
    its annotation; ``explicit`` supplies nested models and caller metadata."""
    kwargs: dict[str, Any] = {}
    for name, key, decoder, absent in _plan(cls):
        if name in explicit:
            continue
        value = obj.get(key, _ABSENT)
        if value is _ABSENT:
            value = None if absent is _ABSENT else absent
        kwargs[name] = decoder(value, f"{what}.{key}")
    kwargs.update(explicit)
    if "raw" not in kwargs and any(f.name == "raw" for f in fields(cls)):  # type: ignore[arg-type]
        kwargs["raw"] = freeze(obj)
    return cls(**kwargs)


def _decode_list(cls: type[_T], value: Any, what: str, required: str = "") -> tuple[_T, ...]:
    """Decode a JSON list of objects, skipping non-objects (and objects missing ``required``)."""
    out = []
    for item in _list(value):
        obj = _opt_obj(item)
        if obj is None or (required and not isinstance(obj.get(required), str)):
            continue
        out.append(decode_model(cls, obj, what))
    return tuple(out)


# --- tracks ---------------------------------------------------------------------


@model
class Track(_ModelMixin):
    """One catalogue track as returned by every track-bearing endpoint.

    ``asset_url`` (MP3 preview) and ``peak_data_url`` are signed for 1 hour and
    are bearer capabilities: hand them to a player, do not store or log them —
    store the track ``id`` instead. Playing a preview is metered today and is
    intended to be billed from API credits (``costs.stream``) in a future update.
    """

    id: int = field(metadata=STRICT)
    title: Optional[str] = None
    artist: Optional[str] = None
    performed_by: Optional[str] = None
    album: Optional[str] = None
    duration: Optional[float] = None
    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    decades: Optional[str] = None
    vocals: Optional[str] = None
    explicit: Optional[bool] = None
    first_published: Optional[str] = None
    release_date: Optional[str] = None
    asset_url: Optional[str] = field(default=None, repr=False)
    peak_data_url: Optional[str] = field(default=None, repr=False)
    genres: tuple[str, ...] = ()
    sub_genres: tuple[str, ...] = ()
    moods: tuple[str, ...] = ()
    instruments: tuple[str, ...] = ()
    sub_instruments: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    sub_themes: tuple[str, ...] = ()
    main_genre: Optional[tuple[str, ...]] = None
    subgenre: Optional[tuple[str, ...]] = None
    free_genre: Optional[tuple[str, ...]] = None
    mood_simple: Optional[tuple[str, ...]] = None
    mood_advanced: Optional[tuple[str, ...]] = None
    character: Optional[tuple[str, ...]] = None
    movement: Optional[tuple[str, ...]] = None
    instruments_detected: Optional[tuple[str, ...]] = None
    music_for: Optional[tuple[str, ...]] = None
    vocals_detected: Optional[tuple[str, ...]] = None
    vocal_presence: Optional[str] = None
    vocal_gender: Optional[str] = None
    vocal_style: Optional[tuple[str, ...]] = None
    voiceover_degree: Optional[float] = None
    voiceover_dominant: Optional[bool] = None
    bpm_detected: Optional[float] = None
    bpm_confidence: Optional[float] = None
    key: Optional[str] = None
    key_confidence: Optional[float] = None
    tempo: Optional[str] = None
    time_signature: Optional[str] = None
    valence: Optional[float] = None
    arousal: Optional[float] = None
    energy_level: Optional[str] = None
    emotion_profile: Optional[str] = None
    musical_era: Optional[str] = None
    production_year: Optional[int] = None
    auto_description: Optional[str] = None
    is_ai_music: Optional[bool] = None
    ai_music_score: Optional[float] = None
    tag_source: Optional[str] = None
    tagged_at: Optional[str] = None
    has_alternatives: Optional[bool] = None
    term_matches: Optional[int] = None
    child_tracks: tuple[Track, ...] = field(default=(), metadata={"json": False})
    raw: Mapping[str, Any] = _raw_field()

    def __repr__(self) -> str:
        return (
            f"Track(id={self.id!r}, title={self.title!r}, artist={self.artist!r}, "
            f"duration={self.duration!r}, bpm={self.bpm!r}, vocals={self.vocals!r}, "
            f"has_alternatives={self.has_alternatives!r}, "
            f"asset_url={_redact_url_repr(self.asset_url)!r}, "
            f"child_tracks={len(self.child_tracks)})"
        )

    def redacted_asset_url(self) -> Optional[str]:
        """``asset_url`` with its signature removed — safe for logs."""
        return _redact_url_repr(self.asset_url)

    @classmethod
    def from_json(cls, data: Any, _depth: int = 0) -> Track:
        obj = _obj(data, "track")
        children: tuple[Track, ...] = ()
        if _depth == 0 and isinstance(obj.get("child_tracks"), (list, tuple)):
            children = tuple(cls.from_json(c, 1) for c in obj["child_tracks"])
        return decode_model(cls, obj, "track", child_tracks=children)


@model
class SearchMatch(_ModelMixin):
    """A query term recognised as catalogue vocabulary (lexical search only)."""

    term: str
    field: str
    value: str
    partial: bool = False
    raw: Mapping[str, Any] = _raw_field()


@model
class SearchCorrection(_ModelMixin):
    """A misspelled word replaced by the closest vocabulary word (``from`` → ``to``)."""

    from_: str = field(metadata={"json_key": "from"})
    to: str = ""
    raw: Mapping[str, Any] = _raw_field()


@model
class CoverageLevel(_ModelMixin):
    matched: int
    rows: int
    raw: Mapping[str, Any] = _raw_field()


@model
class SearchCoverage(_ModelMixin):
    """How many terms the query had and how many tracks matched each number of them."""

    terms: int
    levels: tuple[CoverageLevel, ...] = ()
    raw: Mapping[str, Any] = _raw_field()


@model
class DurationConstraint(_ModelMixin):
    min: Optional[float] = None
    max: Optional[float] = None
    widened: bool = False
    raw: Mapping[str, Any] = _raw_field()


@model
class EmptyConstraintReason(_ModelMixin):
    """Why a page is empty: dropping ``constraint`` alone would return ``rows_without`` rows."""

    constraint: str
    rows_without: int
    rows_with: Optional[int] = None
    raw: Mapping[str, Any] = _raw_field()


@model
class SearchConstraints(_ModelMixin):
    """Hard constraints the API applied (never relaxed away), echoed back."""

    vocals: Optional[str] = None
    key: Optional[str] = None
    bpm: Optional[tuple[float, float]] = None
    duration: Optional[DurationConstraint] = None
    empty_because: tuple[EmptyConstraintReason, ...] = ()
    raw: Mapping[str, Any] = _raw_field()

    @classmethod
    def from_json(cls, data: Any) -> Optional[SearchConstraints]:
        obj = _opt_obj(data)
        if obj is None:
            return None
        bpm_raw = obj.get("bpm")
        bpm: Optional[tuple[float, float]] = None
        if isinstance(bpm_raw, (list, tuple)) and len(bpm_raw) == 2:
            lo, hi = _opt_num(bpm_raw[0]), _opt_num(bpm_raw[1])
            if lo is not None and hi is not None:
                bpm = (lo, hi)
        dur_obj = _opt_obj(obj.get("duration"))
        duration = None if dur_obj is None else decode_model(DurationConstraint, dur_obj, "constraints.duration")
        reasons = _decode_list(EmptyConstraintReason, obj.get("empty_because"), "constraints", "constraint")
        return decode_model(cls, obj, "constraints", bpm=bpm, duration=duration, empty_because=reasons)


@model
class TrackPage(_ModelMixin):
    """One page of tracks. Iterating a page iterates its ``data``.

    Search metadata (``matched`` … ``hint``) is populated only by ``tracks.list``
    with a non-empty ``search``; ``constraints`` whenever a hard constraint was
    applied.
    """

    data: tuple[Track, ...]
    total: int = field(metadata=STRICT)
    page: int = field(metadata=STRICT)
    limit: int = field(metadata=STRICT)
    matched: tuple[SearchMatch, ...] = ()
    unmatched_terms: tuple[str, ...] = ()
    corrections: tuple[SearchCorrection, ...] = ()
    relaxed: Optional[bool] = None
    coverage: Optional[SearchCoverage] = None
    ignored_terms: tuple[str, ...] = ()
    hint: Optional[str] = None
    constraints: Optional[SearchConstraints] = None
    request_id: Optional[str] = None
    retried_after_refundable_failure: bool = False
    raw: Mapping[str, Any] = _raw_field()

    def has_next_page(self) -> bool:
        """``page * limit < total`` and this page was not empty."""
        return bool(self.data) and self.page * self.limit < self.total

    def __iter__(self) -> Iterator[Track]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)

    def __repr__(self) -> str:
        return (
            f"TrackPage(page={self.page}, limit={self.limit}, total={self.total}, "
            f"tracks={len(self.data)}, request_id={self.request_id!r})"
        )

    @classmethod
    def from_json(cls, data: Any, *, request_id: Optional[str] = None, retried: bool = False) -> TrackPage:
        obj = _obj(data, "page")
        items = obj.get("data")
        if not isinstance(items, (list, tuple)):
            raise _bad("page.data is not a list")
        cov = _opt_obj(obj.get("coverage"))
        coverage = None
        if cov is not None:
            levels = _decode_list(CoverageLevel, cov.get("levels"), "page.coverage")
            coverage = decode_model(SearchCoverage, cov, "page.coverage", levels=levels)
        return decode_model(
            cls,
            obj,
            "page",
            data=tuple(Track.from_json(t) for t in items),
            matched=_decode_list(SearchMatch, obj.get("matched"), "page.matched"),
            corrections=_decode_list(SearchCorrection, obj.get("corrections"), "page.corrections"),
            coverage=coverage,
            constraints=SearchConstraints.from_json(obj.get("constraints")),
            request_id=request_id,
            retried_after_refundable_failure=retried,
        )


# --- sync -------------------------------------------------------------------------


@model
class SyncBilling(_ModelMixin):
    charged_on: Optional[str] = None
    note: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()


@model
class SyncLink(_ModelMixin):
    """A signed, 1-hour link to a track's WAV master (``POST /v1/tracks/{id}/sync``).

    ``download_url`` is a **bearer capability**: anyone holding it can fetch the
    master until ``expires_at``. ``repr`` redacts its signature; the attribute
    itself is the real URL. Use ``client.sync_links.download(link, dest)``.
    """

    id: int = field(metadata=STRICT)
    title: Optional[str]
    artist: Optional[str]
    format: str = field(metadata={"json": lambda v: v if isinstance(v, str) and v else "wav"})
    file_name: str
    download_url: str = field(repr=False, metadata={"json": False})
    expires_at: str
    expires_in: int
    credits_charged: int = field(metadata={**STRICT, "absent": 0})
    reissued: bool
    external_export: bool
    billing: SyncBilling = field(default_factory=SyncBilling)
    request_id: Optional[str] = None
    retried_after_refundable_failure: bool = False
    raw: Mapping[str, Any] = _raw_field()

    @property
    def expires_at_datetime(self) -> Optional[_dt.datetime]:
        """``expires_at`` parsed as an aware UTC datetime (``None`` if unparseable)."""
        try:
            return _dt.datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    def redacted_download_url(self) -> str:
        return redact_text(self.download_url)

    def __repr__(self) -> str:
        return (
            f"SyncLink(id={self.id!r}, title={self.title!r}, artist={self.artist!r}, "
            f"format={self.format!r}, file_name={self.file_name!r}, "
            f"download_url={self.redacted_download_url()!r}, expires_at={self.expires_at!r}, "
            f"expires_in={self.expires_in!r}, credits_charged={self.credits_charged!r}, "
            f"reissued={self.reissued!r}, external_export={self.external_export!r}, "
            f"request_id={self.request_id!r})"
        )

    __str__ = __repr__

    @classmethod
    def from_json(cls, data: Any, *, request_id: Optional[str] = None, retried: bool = False) -> SyncLink:
        obj = _obj(data, "sync link")
        url = obj.get("download_url")
        if not isinstance(url, str):
            raise _bad("sync.download_url is not a string")
        billing = _opt_obj(obj.get("billing")) or {}
        return decode_model(
            cls,
            obj,
            "sync",
            download_url=url,
            billing=decode_model(SyncBilling, billing, "sync.billing"),
            request_id=request_id,
            retried_after_refundable_failure=retried,
        )


# --- taxonomies ---------------------------------------------------------------------

TAXONOMY_FACETS: tuple[str, ...] = (
    "genre",
    "mood",
    "theme",
    "instrument",
    "character",
    "movement",
    "vocals",
    "tempo",
    "energy",
    "era",
    "decade",
    "key",
)


@model
class TaxonomyItem(_ModelMixin):
    """One facet value and the number of distinct tracks carrying it."""

    value: str
    count: int
    raw: Mapping[str, Any] = _raw_field()


@model
class Taxonomies(_ModelMixin):
    """Every facet value the catalogue offers, ordered by ``count`` desc.

    Each value is an exact-match term for ``tracks.list``. ``facets`` holds
    every facet the server returned, including ones added after this release.
    """

    genre: tuple[TaxonomyItem, ...] = ()
    mood: tuple[TaxonomyItem, ...] = ()
    theme: tuple[TaxonomyItem, ...] = ()
    instrument: tuple[TaxonomyItem, ...] = ()
    character: tuple[TaxonomyItem, ...] = ()
    movement: tuple[TaxonomyItem, ...] = ()
    vocals: tuple[TaxonomyItem, ...] = ()
    tempo: tuple[TaxonomyItem, ...] = ()
    energy: tuple[TaxonomyItem, ...] = ()
    era: tuple[TaxonomyItem, ...] = ()
    decade: tuple[TaxonomyItem, ...] = ()
    key: tuple[TaxonomyItem, ...] = ()
    facets: Mapping[str, tuple[TaxonomyItem, ...]] = field(default=_EMPTY, repr=False)
    request_id: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()

    def get(self, facet: str) -> tuple[TaxonomyItem, ...]:
        return self.facets.get(facet, ())

    def values(self, facet: str) -> list[str]:
        return [item.value for item in self.get(facet)]

    def __repr__(self) -> str:
        counts = ", ".join(f"{k}={len(v)}" for k, v in self.facets.items())
        return f"Taxonomies({counts})"

    @classmethod
    def from_json(cls, data: Any, *, request_id: Optional[str] = None) -> Taxonomies:
        obj = _obj(data, "taxonomies")
        facets: dict[str, tuple[TaxonomyItem, ...]] = {}
        for name, items in obj.items():
            if not isinstance(items, (list, tuple)):
                continue
            facets[str(name)] = _decode_list(TaxonomyItem, items, f"taxonomies.{name}", "value")
        return cls(
            genre=facets.get("genre", ()),
            mood=facets.get("mood", ()),
            theme=facets.get("theme", ()),
            instrument=facets.get("instrument", ()),
            character=facets.get("character", ()),
            movement=facets.get("movement", ()),
            vocals=facets.get("vocals", ()),
            tempo=facets.get("tempo", ()),
            energy=facets.get("energy", ()),
            era=facets.get("era", ()),
            decade=facets.get("decade", ()),
            key=facets.get("key", ()),
            facets=FrozenMap(facets),
            request_id=request_id,
            raw=freeze(obj),
        )


# --- credits ------------------------------------------------------------------------


@model
class CreditBalance(_ModelMixin):
    """API credits: ``spendable = balance + overdraft_limit``. ``balance`` can be negative."""

    balance: int = field(metadata=STRICT)
    overdraft_limit: int = field(metadata={**STRICT, "absent": 0})
    spendable: int = field(metadata=STRICT)
    raw: Mapping[str, Any] = _raw_field()


@model
class BandwidthBalance(_ModelMixin):
    """Streaming bandwidth allowance in bytes."""

    balance: int = field(metadata=STRICT)
    overdraft_limit: int = field(metadata={**STRICT, "absent": 0})
    spendable: int = field(metadata=STRICT)
    raw: Mapping[str, Any] = _raw_field()


@model
class ExpiringBatch(_ModelMixin):
    api_credits: int
    bandwidth_bytes: int
    expires_at: str
    raw: Mapping[str, Any] = _raw_field()


@model
class PlanDiscount(_ModelMixin):
    standard: int
    plan_id: Optional[int]
    plan: str
    raw: Mapping[str, Any] = _raw_field()


@model
class SubscriptionInfo(_ModelMixin):
    plan_id: int = field(metadata=STRICT)
    plan: str
    raw: Mapping[str, Any] = _raw_field()


@model
class TokenUsage(_ModelMixin):
    name: Optional[str] = None
    public_code: Optional[str] = None
    api_calls_month: int = 0
    bandwidth_month_bytes: int = 0
    month_reset_at: Optional[str] = None
    expires_at: Optional[str] = None
    daily_call_limit: Optional[int] = None
    raw: Mapping[str, Any] = _raw_field()


@model
class Credits(_ModelMixin):
    """``GET /v1/credits`` — free, unmetered. ``costs`` is your own rate card.

    ``costs`` already includes your plan discounts and subscription tier prices;
    it is the only correct input for cost projection.
    """

    as_of: str
    balance_source: str
    api_credits: CreditBalance
    bandwidth_bytes: BandwidthBalance
    expiring_credits: tuple[ExpiringBatch, ...] = ()
    costs: Mapping[str, int] = field(default=_EMPTY)
    standard_costs: Mapping[str, int] = field(default=_EMPTY)
    discounts: Mapping[str, PlanDiscount] = field(default=_EMPTY)
    subscription: Optional[SubscriptionInfo] = None
    token: TokenUsage = field(default_factory=TokenUsage)
    request_id: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()

    def __repr__(self) -> str:
        return (
            f"Credits(spendable={self.api_credits.spendable}, balance={self.api_credits.balance}, "
            f"overdraft_limit={self.api_credits.overdraft_limit}, "
            f"balance_source={self.balance_source!r}, as_of={self.as_of!r})"
        )

    @classmethod
    def from_json(cls, data: Any, *, request_id: Optional[str] = None) -> Credits:
        obj = _obj(data, "credits")
        api = _obj(obj.get("api_credits"), "credits.api_credits")
        bw = _obj(obj.get("bandwidth_bytes"), "credits.bandwidth_bytes")

        def costs_of(value: Any) -> Mapping[str, int]:
            out: dict[str, int] = {}
            for k, v in (_opt_obj(value) or {}).items():
                iv = _opt_int(v)
                if iv is not None:
                    out[str(k)] = iv
            return FrozenMap(out)

        discounts = {
            str(k): decode_model(PlanDiscount, v, f"credits.discounts.{k}")
            for k, v in (_opt_obj(obj.get("discounts")) or {}).items()
            if _opt_obj(v) is not None
        }
        sub = _opt_obj(obj.get("subscription"))
        subscription = None
        if sub is not None and _opt_int(sub.get("plan_id")) is not None:
            subscription = decode_model(SubscriptionInfo, sub, "subscription")
        return decode_model(
            cls,
            obj,
            "credits",
            api_credits=decode_model(CreditBalance, api, "api_credits"),
            bandwidth_bytes=decode_model(BandwidthBalance, bw, "bandwidth_bytes"),
            expiring_credits=_decode_list(ExpiringBatch, obj.get("expiring_credits"), "credits.expiring_credits"),
            costs=costs_of(obj.get("costs")),
            standard_costs=costs_of(obj.get("standard_costs")),
            discounts=FrozenMap(discounts),
            subscription=subscription,
            token=decode_model(TokenUsage, _opt_obj(obj.get("token")) or {}, "credits.token"),
            request_id=request_id,
        )


# --- health -------------------------------------------------------------------------


@model
class DatabaseCheck(_ModelMixin):
    status: str
    latency: Optional[str] = None
    latency_ms: Optional[float] = None
    raw: Mapping[str, Any] = _raw_field()

    @classmethod
    def from_json(cls, data: Any) -> Optional[DatabaseCheck]:
        obj = _opt_obj(data)
        return None if obj is None else decode_model(cls, obj, "database")


@model
class BillingCheck(_ModelMixin):
    status: str
    required: Optional[bool] = None
    mode: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()


@model
class HealthStatus(_ModelMixin):
    """``GET /v1/health``. A 503 ``degraded`` answer is returned, not raised."""

    success: bool
    status: str
    http_status: int
    version: Optional[str] = None
    environment: Optional[str] = None
    timestamp: Optional[str] = None
    uptime: Optional[float] = None
    database: Optional[DatabaseCheck] = None
    retry_after: Optional[float] = None
    request_id: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()

    @property
    def healthy(self) -> bool:
        return self.http_status == 200 and self.status == "healthy"

    @classmethod
    def from_json(
        cls,
        data: Any,
        *,
        http_status: int,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> HealthStatus:
        obj = _obj(data, "health")
        checks = _opt_obj(obj.get("checks")) or {}
        return decode_model(
            cls,
            obj,
            "health",
            http_status=http_status,
            database=DatabaseCheck.from_json(checks.get("database")),
            retry_after=retry_after,
            request_id=request_id,
        )


@model
class LivenessStatus(_ModelMixin):
    """``GET /v1/health/live`` — zero I/O on the server."""

    success: bool
    status: str
    http_status: int
    request_id: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()

    @property
    def alive(self) -> bool:
        return self.http_status == 200 and self.status == "alive"

    @classmethod
    def from_json(
        cls,
        data: Any,
        *,
        http_status: int,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> LivenessStatus:
        obj = _obj(data, "liveness")
        return decode_model(cls, obj, "liveness", http_status=http_status, request_id=request_id)


@model
class ReadinessStatus(_ModelMixin):
    """``GET /v1/health/ready``. A 503 ``not ready`` answer is returned, not raised."""

    success: bool
    status: str
    http_status: int
    database: Optional[DatabaseCheck] = None
    billing: Optional[BillingCheck] = None
    retry_after: Optional[float] = None
    request_id: Optional[str] = None
    raw: Mapping[str, Any] = _raw_field()

    @property
    def ready(self) -> bool:
        return self.http_status == 200 and self.status == "ready"

    @classmethod
    def from_json(
        cls,
        data: Any,
        *,
        http_status: int,
        retry_after: Optional[float] = None,
        request_id: Optional[str] = None,
    ) -> ReadinessStatus:
        obj = _obj(data, "readiness")
        checks = _opt_obj(obj.get("checks")) or {}
        billing_obj = _opt_obj(checks.get("billing"))
        return decode_model(
            cls,
            obj,
            "readiness",
            http_status=http_status,
            database=DatabaseCheck.from_json(checks.get("database")),
            billing=None if billing_obj is None else decode_model(BillingCheck, billing_obj, "billing"),
            retry_after=retry_after,
            request_id=request_id,
        )


# --- hook events --------------------------------------------------------------------


@model
class RequestEvent:
    """Passed to ``on_request`` before each attempt. Already redacted; a copy."""

    operation: str
    sdk_call_id: str
    attempt: int
    method: str
    url: str
    headers: Mapping[str, str]
    cost_key: Optional[str] = None
    projected_credits: Optional[int] = None
    body: Optional[Any] = None


@model
class ResponseEvent:
    """Passed to ``on_response`` after each attempt that produced an HTTP response."""

    operation: str
    sdk_call_id: str
    attempt: int
    method: str
    url: str
    status: int
    headers: Mapping[str, str]
    request_id: Optional[str]
    duration_ms: float
    will_retry: bool = False
    retry_in_ms: Optional[float] = None
    cost_key: Optional[str] = None
    projected_credits: Optional[int] = None
    body: Optional[Any] = None


@model
class ErrorEvent:
    """Passed to ``on_error`` for every failed attempt (redacted error included)."""

    operation: str
    sdk_call_id: str
    attempt: int
    method: str
    url: str
    error: BaseException
    charge_state: Optional[str]
    status: Optional[int] = None
    request_id: Optional[str] = None
    duration_ms: Optional[float] = None
    will_retry: bool = False
    retry_in_ms: Optional[float] = None
    cost_key: Optional[str] = None
    projected_credits: Optional[int] = None


@model
class LowCreditEvent:
    """Passed to ``on_low_credit`` when observed ``spendable`` drops below the threshold.

    Edge-triggered: fires once per crossing and re-arms after an observation at
    or above the threshold. ``source`` is ``credits`` | ``insufficient_credits``
    | ``estimate`` | ``response_header``.
    """

    spendable: int
    balance: Optional[int]
    overdraft_limit: Optional[int]
    threshold: int
    source: str
    as_of: Optional[str] = None
    token_public_code: Optional[str] = None


def model_field_names(cls: type[Any]) -> tuple[str, ...]:
    """Field names of a model class (used by tests and the conformance suite)."""
    return tuple(f.name for f in fields(cls))
