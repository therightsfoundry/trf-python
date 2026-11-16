"""Replace a temp track: find cleared catalogue tracks that sound like a reference recording.

Editors often cut to a "temp track" they cannot license. Acoustic Similarity Search
(``client.tracks.search_similar``) takes a publicly reachable ``.mp3`` or ``.wav`` URL. The search
provider fingerprints the audio and returns catalogue tracks that are acoustically similar. It matches
on the audio signal, not on metadata.

Money rules:
  * Page 1 is charged at ``costs.file_search_first_page`` every time it is sent; later pages at
    ``costs.file_search_next_page``.
  * If the provider cannot download or decode your file, the API answers 400 AND KEEPS THE CHARGE.
    The SDK raises ``UnreachableFileError`` with ``charge_state == "charged"``. To avoid paying for a bad
    URL, the SDK first rejects anything that is not an http(s) URL ending in .mp3/.wav on a public
    hostname (``ValidationError``, nothing sent).
  * Host the reference somewhere the provider can reach (a public CDN or a pre-signed object-storage
    URL), under 120 MB.

Run it:
    export TRF_API_KEY="…"
    python examples/04_acoustic_similarity_temp_track.py https://cdn.example.com/temp-track.mp3
"""

from __future__ import annotations

import sys

import rightsfoundry

PLACEHOLDER_URL = "https://cdn.example.com/temp-track.mp3"


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] == PLACEHOLDER_URL:
        # Deliberately refuse the placeholder: an unreachable file would be charged.
        print(f"usage: {argv[0]} <public .mp3/.wav URL>   (e.g. {PLACEHOLDER_URL}, but use a real file)")
        return 2
    file_url = argv[1]

    with rightsfoundry.Client() as client:
        try:
            page = client.tracks.search_similar(file_url, limit=20, instrumental=False, max_credits=50)
        except rightsfoundry.ValidationError as exc:
            # Caught locally: nothing was sent and nothing was charged.
            print(f"Invalid reference URL: {exc}")
            return 2
        except rightsfoundry.UnreachableFileError as exc:
            # The provider tried and failed. This call WAS charged and is not refunded.
            print(f"The provider could not fetch/decode the file (charge_state={exc.charge_state}).")
            print(f"Check the URL is public and under 120 MB. Support reference: {exc.request_id}")
            return 1
        except rightsfoundry.FeatureNotEnabledError:
            print("Acoustic Similarity Search needs the Deep Search ability on this API key; contact support.")
            return 1
        except rightsfoundry.SearchUnavailableError as exc:
            print(f"Search provider unavailable; the charge is refunded best-effort ({exc.request_id}).")
            return 1

        print(f"{page.total} similar tracks (showing {len(page.data)}):")
        for track in page:
            bpm = track.bpm or track.bpm_detected
            moods = ", ".join(track.moods[:3])
            print(
                f"  {track.id:>10}  {track.title} by {track.artist}  ({track.duration or 0:.0f}s, {bpm} bpm)  {moods}"
            )
            # Signed preview URLs are bearer links: log only the redacted form.
            print(f"              preview: {track.redacted_asset_url()}")
        if page.has_next_page():
            print("More results are available; later pages cost costs.file_search_next_page each.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
