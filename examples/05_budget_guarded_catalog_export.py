"""Export a filtered slice of the catalogue to CSV with a hard credit budget.

Every page of ``GET /v1/tracks`` is charged (``costs.tracks_list``), so an unbounded loop over the catalogue
can spend real money. This example shows the guardrails:

  * ``tracks.iter(..., max_credits=N)`` projects each next page's cost from YOUR live rate card and raises
    ``CreditBudgetExceededError`` before sending a page that would exceed the budget. The rows already
    written are kept.
  * ``max_items`` and ``max_pages`` bound the walk independently of money.
  * ``low_credit_warning_threshold`` + ``on_low_credit`` fire once when the account's spendable balance
    is seen dropping below the threshold (edge-triggered; keep the callback fast).
  * ``limit`` defaults to 100, the fewest charged calls per track.

Only IDs and metadata are exported, never signed URLs, because they expire in an hour. Re-fetch by ID
when you need audio.

Run it:
    export TRF_API_KEY="…"
    python examples/05_budget_guarded_catalog_export.py --genre jazz --max-credits 20 --out jazz.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

import rightsfoundry

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def cell(value: object) -> str:
    """CSV-safe text: catalogue strings are untrusted, so neutralise spreadsheet formula prefixes."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def on_low_credit(event: rightsfoundry.LowCreditEvent) -> None:
    # In production, enqueue an alert here (do not send an email inline).
    print(f"[alert] spendable credits {event.spendable} < {event.threshold} (source={event.source})", file=sys.stderr)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--genre", default="jazz", help="exact genre tag (see `rightsfoundry taxonomies`)")
    parser.add_argument("--mood", help="exact mood tag")
    parser.add_argument("--vocals", choices=("instrumental", "male", "female", "any"))
    parser.add_argument("--max-credits", type=int, default=20, help="budget for the whole export")
    parser.add_argument("--max-items", type=int, default=2_000)
    parser.add_argument("--warn-below", type=int, default=1_000, help="low-credit alert threshold")
    parser.add_argument("--out", type=Path, default=Path("catalog_export.csv"))
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    client = rightsfoundry.Client(
        low_credit_warning_threshold=args.warn_below,
        on_low_credit=on_low_credit,
    )
    written = 0
    with client, args.out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "title", "artist", "duration_s", "bpm", "genres", "moods", "has_alternatives"])
        tracks = client.tracks.iter(
            genre=args.genre,
            mood=args.mood,
            vocals=args.vocals,
            max_items=args.max_items,
            max_credits=args.max_credits,
        )
        try:
            for track in tracks:
                writer.writerow(
                    [
                        track.id,
                        cell(track.title),
                        cell(track.artist),
                        "" if track.duration is None else f"{track.duration:.0f}",
                        "" if track.bpm is None else f"{track.bpm:g}",
                        cell("; ".join(track.genres)),
                        cell("; ".join(track.moods)),
                        track.has_alternatives,
                    ]
                )
                written += 1
        except rightsfoundry.CreditBudgetExceededError as exc:
            print(
                f"Stopped at the budget: next page would cost {exc.next_cost} credits; "
                f"{exc.spent_projected} of {exc.cap} already projected. Nothing more was sent.",
                file=sys.stderr,
            )
        except rightsfoundry.CreditGuardUnavailableError as exc:
            print(f"Could not read the rate card, so nothing was sent: {exc}", file=sys.stderr)
            return 1
        except rightsfoundry.InsufficientCreditsError as exc:
            print(f"Out of credits (balance {exc.api_credits}). Partial export kept.", file=sys.stderr)

    print(f"Wrote {written} tracks to {args.out} (about {tracks.spent_projected} credits projected).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
