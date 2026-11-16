"""``rightsfoundry`` — a zero-dependency command line for The Rights Foundry API.

The API key is read from ``TRF_API_KEY`` (never from a flag, so it does not land
in shell history or ``ps`` output). Human output redacts signed URLs; ``--json``
prints the API's own JSON for scripts, ASCII-escaped so catalogue text can never
inject terminal control or bidirectional-override sequences. Global flags
(``--json``, ``--environment``) work before or after the subcommand. The
``search`` filters are generated from the endpoint registry, so every
``GET /v1/tracks`` filter the SDK knows is available here too.

Exit codes: 0 ok · 1 API/network error · 2 usage or validation error ·
3 health check not healthy.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any, Optional, TextIO

from ._client import Client
from ._endpoints import LIST_QUERY_PARAMS, QueryParam
from ._errors import (
    APIStatusError,
    ConfigurationError,
    CreditGuardError,
    RightsFoundryError,
    ValidationError,
)
from ._models import HealthStatus, LivenessStatus, ReadinessStatus, SyncLink, Track, TrackPage
from ._redact import redact_text
from ._version import __version__

__all__ = ["main"]


def _out(stream: TextIO, text: str) -> None:
    stream.write(text + "\n")


def _dump(stream: TextIO, value: Any) -> None:
    stream.write(json.dumps(value, indent=2, ensure_ascii=True, sort_keys=False) + "\n")


def _clean(value: Optional[str]) -> str:
    """Strip control characters from untrusted catalogue text before printing."""
    if value is None:
        return ""
    return "".join(ch if ch.isprintable() else " " for ch in value)


def _track_line(track: Track) -> str:
    seconds = int(track.duration or 0)
    bpm = f"{track.bpm:g} bpm" if track.bpm else "- bpm"
    genres = ", ".join(track.genres[:3])
    return (
        f"{track.id:>10}  {_clean(track.title)[:40]:<40}  {_clean(track.artist)[:24]:<24}  "
        f"{seconds // 60}:{seconds % 60:02d}  {bpm:>8}  {_clean(genres)}"
    )


def _print_page(stream: TextIO, page: TrackPage) -> None:
    _out(stream, f"{page.total} tracks · page {page.page} · limit {page.limit}")
    for c in page.corrections:
        _out(stream, f"  corrected {_clean(c.from_)!r} → {_clean(c.to)!r}")
    if page.constraints is not None and not page.data and page.constraints.empty_because:
        reasons = ", ".join(r.constraint for r in page.constraints.empty_because)
        _out(stream, f"  nothing matched; try loosening: {reasons}")
    for track in page.data:
        _out(stream, _track_line(track))
    if page.request_id:
        _out(stream, f"request_id: {page.request_id}")


#: ``search`` filters that are not plain ``--flag`` options (positional query, paging).
_SEARCH_SPECIAL = frozenset({"search", "page", "limit"})


def _add_query_param(parser: argparse.ArgumentParser, param: QueryParam) -> None:
    flag = "--" + param.name.replace("_", "-")
    help_text = param.description or None
    if param.kind == "bool":
        parser.add_argument(flag, action=argparse.BooleanOptionalAction, default=None, help=help_text)
    elif param.kind == "enum":
        parser.add_argument(flag, choices=param.choices, help=help_text)
    elif param.kind == "int":
        parser.add_argument(flag, type=int, help=help_text)
    elif param.kind == "number":
        parser.add_argument(flag, type=float, help=help_text)
    else:
        parser.add_argument(flag, help=help_text)


def _global_flags() -> argparse.ArgumentParser:
    """Flags accepted both before and after the subcommand.

    ``SUPPRESS`` defaults stop a subparser from overwriting a value given before
    the subcommand with its own default.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--environment", choices=("production", "staging"), default=argparse.SUPPRESS)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="print JSON (ASCII-escaped) for scripts"
    )
    return common


def _build_parser() -> argparse.ArgumentParser:
    common = _global_flags()
    parser = argparse.ArgumentParser(
        prog="rightsfoundry",
        parents=[common],
        description=(
            "The Rights Foundry API from your terminal: search 85,000+ cleared, human-made tracks "
            "(royalty-free music for commercial and creative sync) and license WAV sync masters. "
            "Set TRF_API_KEY first (https://therightsfoundry.com/dashboard/token)."
        ),
    )
    parser.add_argument("--version", action="version", version=f"rightsfoundry {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    p = command("health", "probe the API (no key needed, free)")
    p.add_argument("--mode", choices=("check", "live", "ready"), default="check")

    command("credits", "balance, overdraft and your rate card (free)")

    p = command("taxonomies", "every facet value with track counts (free)")
    p.add_argument("--facet", help="show one facet (genre, mood, instrument, …)")

    p = command("search", "GET /v1/tracks — filter or text-search (charged)")
    p.add_argument("query", nargs="?", help="free-text search")
    for param in LIST_QUERY_PARAMS:
        if param.name not in _SEARCH_SPECIAL:
            _add_query_param(p, param)
    p.add_argument("--page", type=int)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-credits", type=int)

    p = command("deep-search", "POST /v1/tracks/deep-search — natural-language brief (charged)")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--instrumental", action="store_true")
    p.add_argument("--max-credits", type=int)

    p = command("similar", "POST /v1/tracks/search — sounds like a reference .mp3/.wav (charged)")
    p.add_argument("file_url")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--instrumental", action="store_true")
    p.add_argument("--max-credits", type=int)

    p = command("track", "GET /v1/tracks/{id} (charged at track_detail)")
    p.add_argument("id")

    p = command("sync", "POST /v1/tracks/{id}/sync — license the WAV master (charged)")
    p.add_argument("id")
    p.add_argument(
        "--external-export",
        action="store_true",
        help="Export Sync (offline / outside the platform)",
    )
    p.add_argument("--download", metavar="DIR_OR_FILE", help="download the WAV master here")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--max-credits", type=int)
    return parser


def _parse(parser: argparse.ArgumentParser, argv: Optional[Sequence[str]]) -> argparse.Namespace:
    args = parser.parse_args(argv)
    if not hasattr(args, "environment"):
        args.environment = None
    if not hasattr(args, "json"):
        args.json = False
    return args


def _client(args: argparse.Namespace, *, unauthenticated: bool = False) -> Client:
    options: dict[str, Any] = {}
    if args.environment:
        options["environment"] = args.environment
    if unauthenticated:
        return Client.unauthenticated(**options)
    return Client(**options)


def _run(args: argparse.Namespace, out: TextIO) -> int:
    cmd = args.command
    if cmd == "health":
        with _client(args, unauthenticated=True) as client:
            status: HealthStatus | LivenessStatus | ReadinessStatus
            if args.mode == "live":
                status = client.health.live()
                ok = status.alive
            elif args.mode == "ready":
                status = client.health.ready()
                ok = status.ready
            else:
                status = client.health.check()
                ok = status.healthy
            if args.json:
                _dump(out, status.to_dict())
            else:
                _out(out, f"{status.status} (HTTP {status.http_status})")
            return 0 if ok else 3

    with _client(args) as client:
        if cmd == "credits":
            credits = client.credits.retrieve()
            if args.json:
                _dump(out, credits.to_dict())
                return 0
            _out(out, f"Spendable API credits: {credits.api_credits.spendable}")
            _out(
                out,
                f"  balance {credits.api_credits.balance}, overdraft {credits.api_credits.overdraft_limit}",
            )
            _out(out, f"Bandwidth spendable: {credits.bandwidth_bytes.spendable} bytes")
            _out(out, "Rate card:")
            for k, v in sorted(credits.costs.items()):
                _out(out, f"  {k:<26} {v}")
            for batch in credits.expiring_credits:
                _out(out, f"  {batch.api_credits} credits expire at {batch.expires_at}")
            return 0
        if cmd == "taxonomies":
            tax = client.taxonomies.list()
            if args.json:
                data = tax.to_dict()
                if args.facet:
                    data = {args.facet: data.get(args.facet, [])}
                _dump(out, data)
                return 0
            facets = [args.facet] if args.facet else list(tax.facets)
            for facet in facets:
                items = tax.get(facet)
                preview = ", ".join(f"{_clean(i.value)} ({i.count})" for i in items[:15])
                _out(out, f"{facet}: {preview}")
            return 0
        if cmd == "search":
            filters = {
                p.name: getattr(args, p.name)
                for p in LIST_QUERY_PARAMS
                if p.name not in _SEARCH_SPECIAL and getattr(args, p.name) is not None
            }
            page = client.tracks.list(
                search=args.query,
                page=args.page,
                limit=args.limit,
                max_credits=args.max_credits,
                **filters,
            )
            if args.json:
                _dump(out, page.to_dict())
            else:
                _print_page(out, page)
            return 0
        if cmd == "deep-search":
            page = client.tracks.deep_search(
                args.query,
                page=args.page,
                limit=args.limit,
                instrumental=args.instrumental,
                max_credits=args.max_credits,
            )
            if args.json:
                _dump(out, page.to_dict())
            else:
                _print_page(out, page)
            return 0
        if cmd == "similar":
            page = client.tracks.search_similar(
                args.file_url,
                page=args.page,
                limit=args.limit,
                instrumental=args.instrumental,
                max_credits=args.max_credits,
            )
            if args.json:
                _dump(out, page.to_dict())
            else:
                _print_page(out, page)
            return 0
        if cmd == "track":
            track = client.tracks.get(args.id)
            if args.json:
                _dump(out, track.to_dict())
            else:
                _out(out, _track_line(track))
                if track.auto_description:
                    _out(out, f"  {_clean(track.auto_description)}")
                for child in track.child_tracks:
                    _out(out, "  alt " + _track_line(child))
            return 0
        if cmd == "sync":
            link: SyncLink = client.tracks.sync(
                args.id, external_export=args.external_export, max_credits=args.max_credits
            )
            path = None
            if args.download:
                path = client.sync_links.download(link, args.download, overwrite=args.overwrite)
            if args.json:
                data = link.to_dict()
                if path is not None:
                    data["downloaded_file"] = str(path)
                _dump(out, data)
                return 0
            mode = "Export Sync" if link.external_export else "Sync"
            _out(
                out,
                f"{mode} licence for {link.id} {_clean(link.title)!r} by {_clean(link.artist)!r}",
            )
            _out(
                out,
                f"  credits charged: {link.credits_charged}{' (free reissue)' if link.reissued else ''}",
            )
            _out(out, f"  expires: {link.expires_at} ({link.expires_in}s)")
            if path is not None:
                _out(out, f"  saved to: {path}")
            else:
                _out(
                    out,
                    f"  link: {link.redacted_download_url()}  (use --download DIR or --json for the URL)",
                )
            return 0
    raise AssertionError(f"unhandled command {cmd}")  # pragma: no cover


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = _build_parser()
    args = _parse(parser, argv)
    try:
        return _run(args, out)
    except (ConfigurationError, ValidationError, CreditGuardError) as exc:
        _out(err, f"error: {redact_text(str(exc))}")
        return 2
    except APIStatusError as exc:
        _out(err, f"error: {redact_text(str(exc))}")
        if exc.charge_state and exc.charge_state != "not_charged":
            _out(err, f"charge state: {exc.charge_state}")
        return 1
    except RightsFoundryError as exc:
        _out(err, f"error: {redact_text(str(exc))}")
        return 1
    except KeyboardInterrupt:
        return 130
