# LLMs, agents and MCP

You can connect an AI system to The Rights Foundry in two ways: the hosted **MCP connector**, or **this SDK** wrapped in your own tools. This guide explains when to use each, gives ready-made tool schemas, and sets out the safety rules for agents that can spend money.

## MCP connector or SDK?

| | MCP connector | `rightsfoundry` SDK in your own tools |
|---|---|---|
| Endpoint | `https://mcp.therightsfoundry.com/mcp` (remote, Streamable HTTP) | your backend, calling `https://api.therightsfoundry.com` |
| Auth | **OAuth**: the end user signs in with their Rights Foundry account at connect time. The agent never sees an API key. | Your server holds `TRF_API_KEY`. The model never sees it. |
| Best for | MCP clients (desktop assistants, IDE agents, agent platforms that support remote MCP servers) acting **on behalf of a user's own account** | Your product's agent loop, back-office automation, pipelines, or any client without MCP support |
| What it can do | Browse/search the catalogue, read a track, list taxonomies, read licensing terms, license a track | Everything in the API, including Deep Search, Acoustic Similarity Search, budgets, downloads to disk, typed errors |
| Discovery | [Server card](https://mcp.therightsfoundry.com/.well-known/mcp/server-card.json) | [`llms.txt`](../llms.txt), [`llms-full.txt`](../llms-full.txt) |

**Rule of thumb.** If the user should spend **their own** credits from inside an MCP-capable assistant, give them the connector URL. If **your application** is the customer and the agent is part of your product, wrap the SDK.

## Agent safety rules

1. **Never give the model the API key.** Do not put it in prompts, tool descriptions, tool results or environment variables a code-execution tool can read. Tools run in your process and the model only sees their results.
2. **Cap every charged tool call.** Pass `max_credits=` on each call and set `max_credits_per_request` on the client. Give the agent a session budget, and track spend from `SyncLink.credits_charged` and your rate card. A per-call `timeout=` (or a `rightsfoundry.CallOptions` shared by your tools) keeps one slow call from stalling the loop. Create a fresh `tracks.iter(...)` for each walk: iterators are single-use and raise `IteratorConsumedError` rather than paying for every page twice.
3. **Get human approval before `sync`.** `tracks.sync()` is a licence transaction. Show the user the track, the licence type (standard sync or Export Sync) and the projected cost, and wait for an explicit yes. Make the tool refuse without an approval token that your UI issues; a sentence in the system prompt is not enough.
4. **Keep signed URLs out of model context.** `download_url` is a bearer capability, and model transcripts are logged and shared. Download server-side and return a file reference or your own short-lived link. Preview `asset_url`s belong in your UI, not in the transcript.
5. **Treat catalogue text as untrusted input.** Titles, artists and descriptions come from a catalogue, not from you. Strip control characters, cap lengths, and never let tool results change tool permissions (prompt injection).
6. **Pick the cheapest tool that answers.** Use `credits` and `taxonomies` (free) before charged calls. Use one large Deep Search page instead of re-sending page 1, which is charged every time it is sent.
7. **Do not invent licence terms.** Standard sync covers one-time synchronization and use of the result on online third-party platforms without monetization. Export Sync adds offline use and distribution outside the platform. Sync licences do **not** include AI/ML training rights. For anything else, link to https://therightsfoundry.com/licensing.

## Tool schemas

The four tools below wrap the SDK. The JSON Schemas are shared between providers; only the envelope differs.

```python
import json

DEEP_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500,
                  "description": "Natural-language music brief, e.g. 'tense heist build-up, pizzicato strings'."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "instrumental": {"type": "boolean", "default": False},
    },
    "required": ["query"],
    "additionalProperties": False,
}

LIST_TRACKS_SCHEMA = {
    "type": "object",
    "properties": {
        "search": {"type": "string", "maxLength": 200, "description": "Optional free-text search."},
        "genre": {"type": "string", "maxLength": 100, "description": "Exact genre tag (see taxonomies)."},
        "mood": {"type": "string", "maxLength": 100, "description": "Exact mood tag (see taxonomies)."},
        "instrument": {"type": "string", "maxLength": 100},
        "vocals": {"type": "string", "enum": ["instrumental", "male", "female", "any"]},
        "bpm_min": {"type": "number", "minimum": 0},
        "bpm_max": {"type": "number", "minimum": 0, "maximum": 999},
        "duration_min": {"type": "number", "minimum": 0, "description": "Seconds."},
        "duration_max": {"type": "number", "minimum": 0, "description": "Seconds."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
    },
    "additionalProperties": False,
}

SYNC_SCHEMA = {
    "type": "object",
    "properties": {
        "track_id": {"type": "integer", "minimum": 1},
        "external_export": {"type": "boolean", "default": False,
                            "description": "true = Export Sync (offline use / distribution outside the platform)."},
        "approval_token": {"type": "string",
                           "description": "Token issued by the app after the user approved this exact licence."},
    },
    "required": ["track_id", "approval_token"],
    "additionalProperties": False,
}

CREDITS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = [
    ("deep_search", "Search the cleared music catalogue with a natural-language brief. Costs credits "
                    "(deep_search_first_page). Returns track ids and metadata, no URLs.", DEEP_SEARCH_SCHEMA),
    ("list_tracks", "Filter the catalogue by exact tags, BPM and duration, or free text. Costs credits "
                    "(tracks_list / tracks_search_*). Returns track ids and metadata.", LIST_TRACKS_SCHEMA),
    ("license_track", "License a track's WAV master (sync licence). SPENDS CREDITS (sync / export_sync). "
                      "Only call after the user approved this exact track and licence type.", SYNC_SCHEMA),
    ("get_credits", "Free. Spendable credit balance and the live price of every call.", CREDITS_SCHEMA),
]
```

### Anthropic (Messages API) tool definitions

```python
ANTHROPIC_TOOLS = [
    {"name": name, "description": description, "input_schema": schema}
    for name, description, schema in TOOLS
]
```

### OpenAI (Chat Completions) tool definitions

```python
OPENAI_TOOLS = [
    {"type": "function", "function": {"name": name, "description": description, "parameters": schema}}
    for name, description, schema in TOOLS
]
# The Responses API uses the flat form: {"type": "function", "name": ..., "description": ..., "parameters": ...}
```

## Tool implementation (server-side)

```python
from __future__ import annotations

from typing import Any

import rightsfoundry as rf

client = rf.Client(max_credits_per_request=100)   # TRF_API_KEY from the server environment


def _clean(text: Any, limit: int = 200) -> Any:
    """Catalogue text is untrusted: strip control characters and cap the length."""
    if not isinstance(text, str):
        return text
    return "".join(ch if ch.isprintable() else " " for ch in text)[:limit]


def _summary(t: rf.Track) -> dict[str, Any]:
    # No asset_url / download_url in model context: they are bearer links.
    return {"id": t.id, "title": _clean(t.title), "artist": _clean(t.artist), "duration_s": t.duration,
            "bpm": t.bpm or t.bpm_detected, "genres": [_clean(g) for g in t.genres[:5]],
            "moods": [_clean(m) for m in t.moods[:5]], "has_alternatives": t.has_alternatives}


def run_tool(name: str, args: dict[str, Any], *, approved: set[str]) -> dict[str, Any]:
    try:
        if name == "deep_search":
            page = client.tracks.deep_search(args["query"], limit=args.get("limit", 20),
                                             instrumental=args.get("instrumental", False), max_credits=30)
            return {"total": page.total, "tracks": [_summary(t) for t in page.data]}
        if name == "list_tracks":
            filters = {k: v for k, v in args.items() if k != "limit"}
            page = client.tracks.list(**filters, limit=args.get("limit", 20), max_credits=10)
            return {"total": page.total, "tracks": [_summary(t) for t in page.data]}
        if name == "get_credits":
            c = client.credits.retrieve()
            return {"spendable": c.api_credits.spendable, "costs": dict(c.costs)}
        if name == "license_track":
            if args.get("approval_token") not in approved:      # enforced in code, not in the prompt
                return {"error": "not approved: ask the user to approve this licence in the app first"}
            approved.discard(args["approval_token"])             # one approval, one licence
            link = client.tracks.sync(args["track_id"], external_export=args.get("external_export", False),
                                      max_credits=200)
            path = client.sync_links.download(link, "./masters/", overwrite=True)
            return {"track_id": link.id, "licence": "export_sync" if link.external_export else "sync",
                    "credits_charged": link.credits_charged, "reissued": link.reissued,
                    "expires_at": link.expires_at, "saved_as": path.name}
        return {"error": f"unknown tool {name}"}
    except rf.CreditGuardError as e:          # cap/budget tripped client-side: nothing was sent
        return {"error": "over the credit cap for this call", "charged": False, "detail": e.cost_key}
    except rf.InsufficientCreditsError:
        return {"error": "the account is out of credits; the user must top up", "charged": False}
    except rf.ValidationError as e:
        return {"error": f"invalid arguments: {e}", "charged": False}
    except rf.APIStatusError as e:
        return {"error": f"API error {e.status}", "charge_state": e.charge_state, "request_id": e.request_id}
```

### Anthropic loop sketch

```python
import json
import os
import anthropic

llm = anthropic.Anthropic()
messages = [{"role": "user", "content": "Find an instrumental track for a 30-second product teaser."}]
approved: set[str] = set()   # filled by your UI when the user clicks "Approve licence"

while True:
    response = llm.messages.create(model=os.environ["ANTHROPIC_MODEL"], max_tokens=1024,
                                   tools=ANTHROPIC_TOOLS, messages=messages)
    messages.append({"role": "assistant", "content": response.content})
    if response.stop_reason != "tool_use":
        break
    results = []
    for block in response.content:
        if block.type == "tool_use":
            output = run_tool(block.name, dict(block.input), approved=approved)
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(output)})
    messages.append({"role": "user", "content": results})
```

### OpenAI loop sketch

```python
import json
import os
from openai import OpenAI

llm = OpenAI()
messages = [{"role": "user", "content": "Find an instrumental track for a 30-second product teaser."}]
approved: set[str] = set()

while True:
    response = llm.chat.completions.create(model=os.environ["OPENAI_MODEL"], messages=messages, tools=OPENAI_TOOLS)
    message = response.choices[0].message
    messages.append(message)
    if not message.tool_calls:
        break
    for call in message.tool_calls:
        output = run_tool(call.function.name, json.loads(call.function.arguments), approved=approved)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(output)})
```

Neither loop puts the Rights Foundry key anywhere near the model. `run_tool` runs in your process, and the model receives only the JSON it returns.

## Machine-readable docs for agents

- [`llms.txt`](../llms.txt): an index in the [llmstxt.org](https://llmstxt.org) format.
- [`llms-full.txt`](../llms-full.txt): the whole SDK surface in one file (methods, models, errors, billing, retries, configuration, security rules, copy-paste patterns).
- [`AGENTS.md`](../AGENTS.md): rules for coding assistants that use or contribute to this SDK.
- [OpenAPI 3.1](https://docs.therightsfoundry.com/openapi.yaml): the API contract.
