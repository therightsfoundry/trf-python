"""Quickstart: from a creative brief to a licensed WAV master in three calls.

What it does:
  1. Reads your live rate card (free) and checks you can afford the run.
  2. Runs a Deep Search on a natural-language brief (charged at ``costs.deep_search_first_page``).
  3. Licenses the top result with a standard sync (charged at ``costs.sync``; free if you already hold a
     live link for this track) and downloads the WAV master to ``./masters/`` (free).

Run it:
    export TRF_API_KEY="…"      # from https://therightsfoundry.com/dashboard/token
    python examples/01_quickstart_brief_to_wav.py "warm hopeful acoustic guitar for a nature documentary"

The signed download URL is a bearer capability, so this script prints only its redacted form.
"""

from __future__ import annotations

import sys

import rightsfoundry

DEFAULT_BRIEF = "warm hopeful acoustic guitar for a nature documentary"


def main(argv: list[str]) -> int:
    brief = " ".join(argv[1:]).strip() or DEFAULT_BRIEF

    with rightsfoundry.Client() as client:  # reads TRF_API_KEY once
        # 1. Budget off the live rate card. Never hard-code prices.
        credits = client.credits.retrieve()
        need = credits.costs.get("deep_search_first_page", 0) + credits.costs.get("sync", 0)
        if credits.api_credits.spendable < need:
            print(f"Need about {need} credits, have {credits.api_credits.spendable}. Top up at /dashboard/billing.")
            return 1

        # 2. Deep Search. Page 1 is charged every time it is sent, so ask for one useful page.
        results = client.tracks.deep_search(brief, limit=10, instrumental=True)
        if not results.data:
            print("No matches; try a broader brief.")
            return 1
        for i, candidate in enumerate(results.data[:5], start=1):
            print(f"{i}. {candidate.id}  {candidate.title} by {candidate.artist}  ({candidate.duration or 0:.0f}s)")
        track = results.data[0]

        # 3. License the master. A repeat inside the 1-hour window is free and keeps the original expiry.
        try:
            link = client.tracks.sync(track.id)
        except rightsfoundry.FeatureNotEnabledError:
            print("Sync licensing is not enabled for this API key; contact support.")
            return 1
        except rightsfoundry.InsufficientCreditsError as exc:
            print(f"Out of credits (balance {exc.api_credits}); top up and retry.")
            return 1

        note = " (free reissue)" if link.reissued else ""
        print(f"Licensed {link.title!r}: {link.credits_charged} credits{note}, link expires {link.expires_at}")
        print("Link (redacted):", link.redacted_download_url())

        # 4. Download. No Authorization header is sent to the asset host; the write is atomic.
        path = client.sync_links.download(link, "./masters/", overwrite=True)
        print("Saved WAV master to", path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
