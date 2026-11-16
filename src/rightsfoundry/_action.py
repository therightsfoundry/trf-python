"""GitHub Action entrypoint (``action.yml`` runs :func:`main` under ``python3 -I``).

Hardened for untrusted data (SEC-20):

* every input arrives through ``INPUT_*`` / ``TRF_*`` environment variables set by
  the step's ``env:`` map — never interpolated into a shell script;
* outputs go to ``$GITHUB_OUTPUT`` with the multiline ``name<<DELIM`` form and a
  fresh random delimiter, so catalogue strings cannot inject output keys;
* anything printed to the log is wrapped in ``::stop-commands::`` so a track
  title cannot issue workflow commands;
* the API key and every ``sig`` are masked (``::add-mask::``) and redacted from
  ``result-json`` and the step summary;
* ``action.yml`` starts Python in isolated mode (``-I``) with only the action's
  own ``src/`` on ``sys.path``, so the workspace checkout cannot shadow a module.

``search`` filter inputs are read generically from the endpoint registry: any
``GET /v1/tracks`` filter declared as an input in ``action.yml`` (``bpm-min`` for
``bpm_min``, …) is passed through with the type the registry gives it.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from collections.abc import Mapping
from typing import Any, Optional, TextIO
from urllib.parse import parse_qsl, urlsplit

from ._client import Client
from ._endpoints import LIST_QUERY_PARAMS
from ._errors import APIStatusError, RightsFoundryError
from ._models import Track, TrackPage
from ._redact import redact_text, redact_value

__all__ = ["main"]

ACTIONS = ("health", "credits", "search", "deep-search", "search-similar", "track", "sync")


class ActionInputError(Exception):
    pass


def _input(name: str, env: Mapping[str, str]) -> Optional[str]:
    key = "INPUT_" + name.upper().replace("-", "_")
    value = env.get(key)
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _int_input(name: str, env: Mapping[str, str]) -> Optional[int]:
    raw = _input(name, env)
    if raw is None:
        return None
    if not raw.isascii() or not raw.lstrip("-").isdigit():
        raise ActionInputError(f"input {name} must be an integer")
    return int(raw)


def _float_input(name: str, env: Mapping[str, str]) -> Optional[float]:
    raw = _input(name, env)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ActionInputError(f"input {name} must be a number") from None


def _bool_input(name: str, env: Mapping[str, str]) -> bool:
    raw = (_input(name, env) or "false").lower()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ActionInputError(f"input {name} must be true or false")


def _workflow_command_safe(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


class _Log:
    """Prints to the job log inside a stop-commands block."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self.token = secrets.token_hex(16)
        self.stream.write(f"::stop-commands::{self.token}\n")

    def line(self, text: str) -> None:
        self.stream.write(redact_text(text) + "\n")

    def mask(self, value: str) -> None:
        """Workflow commands are paused inside the block: resume, mask, pause again."""
        if not value:
            return
        self.stream.write(f"::{self.token}::\n")
        self.stream.write(f"::add-mask::{_workflow_command_safe(value)}\n")
        self.token = secrets.token_hex(16)
        self.stream.write(f"::stop-commands::{self.token}\n")

    def close(self) -> None:
        self.stream.write(f"::{self.token}::\n")
        self.stream.flush()


def write_outputs(path: Optional[str], outputs: Mapping[str, str]) -> None:
    """Append outputs using random heredoc delimiters (no key injection possible)."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for name, value in outputs.items():
            delimiter = f"ghadelimiter_{secrets.token_hex(16)}"
            while delimiter in value:
                delimiter = f"ghadelimiter_{secrets.token_hex(16)}"
            fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


_MD_LIMIT = 200


def _md(text: Any) -> str:
    """Markdown-safe text for the step summary: redacted, printable, truncated, then escaped.

    Truncating *after* escaping could cut an escape in half and leave a dangling
    backslash that escapes the table's next ``|``.
    """
    value = redact_text(str(text if text is not None else ""))
    value = "".join(ch if ch.isprintable() else " " for ch in value)[:_MD_LIMIT]
    for ch in ("\\", "|", "`", "*", "_", "[", "]", "<", ">", "#"):
        value = value.replace(ch, "\\" + ch)
    return value


def _signatures(url: str) -> list[str]:
    """Every ``sig`` value in a URL's query, wherever it appears."""
    try:
        return [v for k, v in parse_qsl(urlsplit(url).query, keep_blank_values=False) if k.lower() == "sig"]
    except ValueError:
        return []


def _list_filters(env: Mapping[str, str]) -> dict[str, Any]:
    """``search`` filters from ``INPUT_*`` variables, typed by the endpoint registry."""
    out: dict[str, Any] = {}
    for param in LIST_QUERY_PARAMS:
        if param.name in ("search", "page", "limit"):
            continue
        name = param.name.replace("_", "-")
        if param.kind == "number":
            value: Any = _float_input(name, env)
        elif param.kind == "int":
            value = _int_input(name, env)
        elif param.kind == "bool":
            value = _bool_input(name, env) if _input(name, env) is not None else None
        else:
            value = _input(name, env)
        if value is not None:
            out[param.name] = value
    return out


def _page_summary(title: str, page: TrackPage) -> list[str]:
    lines = [
        f"### {title}",
        "",
        f"{page.total} matching tracks (page {page.page}, limit {page.limit})",
        "",
    ]
    if page.data:
        lines += ["| ID | Title | Artist | Duration | BPM |", "|---:|---|---|---:|---:|"]
        for t in page.data[:25]:
            seconds = int(t.duration or 0)
            lines.append(
                f"| {t.id} | {_md(t.title)} | {_md(t.artist)} | {seconds // 60}:{seconds % 60:02d} | "
                f"{_md(t.bpm if t.bpm is not None else '')} |"
            )
    return lines


def _run(env: Mapping[str, str], log: _Log) -> tuple[dict[str, str], list[str], int]:
    action = (_input("action", env) or "").lower()
    if action not in ACTIONS:
        raise ActionInputError(f"input action must be one of: {', '.join(ACTIONS)}")
    environment = _input("environment", env) or "production"
    if environment not in ("production", "staging"):
        raise ActionInputError("input environment must be production or staging")
    api_key = _input("api-key", env) or (env.get("TRF_API_KEY") or "").strip() or None
    if api_key:
        log.mask(api_key)
    max_credits = _int_input("max-credits", env)
    limit = _int_input("limit", env) or 20
    outputs: dict[str, str] = {}
    summary: list[str] = ["## The Rights Foundry", ""]

    if action == "health":
        with Client.unauthenticated(environment=environment) as client:
            status = client.health.check()
        outputs["result-json"] = json.dumps(redact_value(status.to_dict()))
        outputs["request-id"] = status.request_id or ""
        summary.append(f"API health: **{_md(status.status)}** (HTTP {status.http_status})")
        log.line(f"health: {status.status} (HTTP {status.http_status})")
        return outputs, summary, 0 if status.healthy else 1

    if not api_key:
        raise ActionInputError("input api-key is required for this action (store it as a GitHub secret)")
    with Client(api_key=api_key, environment=environment) as client:
        if action == "credits":
            credits = client.credits.retrieve()
            outputs["result-json"] = json.dumps(redact_value(credits.to_dict()))
            outputs["credits-spendable"] = str(credits.api_credits.spendable)
            outputs["request-id"] = credits.request_id or ""
            summary.append(f"Spendable API credits: **{credits.api_credits.spendable}**")
            log.line(f"credits: spendable={credits.api_credits.spendable}")
            return outputs, summary, 0

        page: Optional[TrackPage] = None
        track: Optional[Track] = None
        if action == "search":
            page = client.tracks.list(
                search=_input("query", env),
                limit=limit,
                max_credits=max_credits,
                **_list_filters(env),
            )
            title = "Catalogue search"
        elif action == "deep-search":
            query = _input("query", env)
            if not query:
                raise ActionInputError("input query is required for deep-search")
            page = client.tracks.deep_search(
                query,
                limit=limit,
                instrumental=_bool_input("instrumental", env),
                max_credits=max_credits,
            )
            title = "Deep Search"
        elif action == "search-similar":
            file_url = _input("file-url", env)
            if not file_url:
                raise ActionInputError("input file-url is required for search-similar")
            page = client.tracks.search_similar(
                file_url,
                limit=limit,
                instrumental=_bool_input("instrumental", env),
                max_credits=max_credits,
            )
            title = "Acoustic Similarity Search"
        elif action == "track":
            track_id = _input("track-id", env)
            if not track_id:
                raise ActionInputError("input track-id is required for track")
            track = client.tracks.get(track_id, max_credits=max_credits)
            title = "Track"
        else:  # sync
            track_id = _input("track-id", env)
            if not track_id:
                raise ActionInputError("input track-id is required for sync")
            link = client.tracks.sync(
                track_id,
                external_export=_bool_input("external-export", env),
                max_credits=max_credits,
            )
            for sig in _signatures(link.download_url):
                log.mask(sig)
            downloaded = ""
            download_path = _input("download-path", env)
            if download_path:
                downloaded = str(client.sync_links.download(link, download_path, overwrite=True))
            outputs.update(
                {
                    "result-json": json.dumps(redact_value(link.to_dict())),
                    "track-id": str(link.id),
                    "track-title": link.title or "",
                    "track-artist": link.artist or "",
                    "credits-charged": str(link.credits_charged),
                    "downloaded-file": downloaded,
                    "request-id": link.request_id or "",
                }
            )
            mode = "Export Sync" if link.external_export else "Sync"
            summary += [
                f"### {mode} licence",
                "",
                f"- Track: **{_md(link.title)}** by {_md(link.artist)} (`{link.id}`)",
                f"- Credits charged: {link.credits_charged}{' (free reissue)' if link.reissued else ''}",
                f"- Link expires: {_md(link.expires_at)}",
            ]
            if downloaded:
                summary.append(f"- Saved WAV master: `{_md(downloaded)}`")
            log.line(f"sync: track {link.id} credits_charged={link.credits_charged} reissued={link.reissued}")
            return outputs, summary, 0

        if page is not None:
            first = page.data[0] if page.data else None
            outputs["result-json"] = json.dumps(redact_value(page.to_dict()))
            outputs["request-id"] = page.request_id or ""
            if first is not None:
                outputs["track-id"] = str(first.id)
                outputs["track-title"] = first.title or ""
                outputs["track-artist"] = first.artist or ""
            summary += _page_summary(title, page)
            log.line(f"{action}: {page.total} matches, {len(page.data)} returned")
            return outputs, summary, 0
        if track is None:  # pragma: no cover - every branch above sets page or track
            raise ActionInputError("internal error: no result")
        outputs.update(
            {
                "result-json": json.dumps(redact_value(track.to_dict())),
                "track-id": str(track.id),
                "track-title": track.title or "",
                "track-artist": track.artist or "",
            }
        )
        summary += [
            f"### {title}",
            "",
            f"**{_md(track.title)}** by {_md(track.artist)} (`{track.id}`)",
        ]
        log.line(f"track: {track.id}")
        return outputs, summary, 0


def main(env: Optional[Mapping[str, str]] = None, stdout: Optional[TextIO] = None) -> int:
    environ = dict(os.environ if env is None else env)
    out = stdout or sys.stdout
    log = _Log(out)
    code = 1
    outputs: dict[str, str] = {}
    summary: list[str] = []
    try:
        outputs, summary, code = _run(environ, log)
    except ActionInputError as exc:
        log.close()
        out.write(f"::error::{_workflow_command_safe(str(exc))}\n")
        return 2
    except RightsFoundryError as exc:
        log.close()
        detail = redact_text(str(exc))
        if isinstance(exc, APIStatusError) and exc.charge_state:
            detail += f" [charge_state={exc.charge_state}]"
        out.write(f"::error::{_workflow_command_safe(detail)}\n")
        return 1
    log.close()
    write_outputs(environ.get("GITHUB_OUTPUT"), outputs)
    summary_path = environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and summary:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(summary) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
