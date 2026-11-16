"""Soundtrack an AI-generated video: pick a track that fits, license it, and mux it in with ffmpeg.

This is the shape of a typical AI-video or programmatic-video pipeline (for example a Remotion render,
or a clip from a text-to-video model). The video already exists; it needs music that fits its length
and pace, licensed for the place it will be published.

Steps:
  1. Deep Search on a brief (charged at ``costs.deep_search_first_page``), instrumental only.
  2. Pick the best candidate locally: long enough to cover the video, and within a BPM window when one
     is given (uses the editorial ``bpm`` and falls back to the audio analysis ``bpm_detected``).
  3. License it with ``tracks.sync`` (charged at ``costs.sync``). Pass ``--external-export`` for an Export
     Sync (``costs.export_sync``) when the result will be delivered, broadcast or distributed outside the
     platform. A standard sync covers use on online third-party platforms without monetization. See
     https://therightsfoundry.com/licensing for the authoritative terms.
  4. Download the WAV master (free), then run ffmpeg to replace the video's audio with the master,
     fading out over the last two seconds.

Run it:
    export TRF_API_KEY="…"
    python examples/02_ai_video_soundtrack_pipeline.py render.mp4 --duration 42 \
        --brief "driving synthwave, rain-soaked city at night" --bpm-min 100 --bpm-max 125

ffmpeg is optional. Without it, the script prints the command to run later. It is always run with an
argument list (never ``shell=True``), so file names cannot inject shell syntax.
"""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import rightsfoundry


def effective_bpm(track: rightsfoundry.Track) -> Optional[float]:
    return track.bpm if track.bpm else track.bpm_detected


def pick_track(
    tracks: tuple[rightsfoundry.Track, ...],
    *,
    video_seconds: float,
    bpm_min: Optional[float],
    bpm_max: Optional[float],
) -> Optional[rightsfoundry.Track]:
    """The shortest track that still covers the video and fits the BPM window."""
    fitting: list[rightsfoundry.Track] = []
    for track in tracks:
        if track.duration is None or track.duration < video_seconds:
            continue
        bpm = effective_bpm(track)
        if bpm_min is not None and (bpm is None or bpm < bpm_min):
            continue
        if bpm_max is not None and (bpm is None or bpm > bpm_max):
            continue
        fitting.append(track)
    if not fitting:
        return None
    return min(fitting, key=lambda t: (t.duration or 0.0) - video_seconds)


def ffmpeg_command(ffmpeg: str, video: Path, audio: Path, output: Path, video_seconds: float) -> list[str]:
    fade_start = max(0.0, video_seconds - 2.0)
    return [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-af",
        f"afade=t=out:st={fade_start:.2f}:d=2",
        "-c:a",
        "aac",
        "-b:a",
        "320k",
        "-shortest",
        str(output),
    ]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video", type=Path, help="the rendered video file (e.g. render.mp4)")
    parser.add_argument("--duration", type=float, required=True, help="video length in seconds")
    parser.add_argument("--brief", default="uplifting cinematic electronic, builds to a confident finish")
    parser.add_argument("--bpm-min", type=float)
    parser.add_argument("--bpm-max", type=float)
    parser.add_argument("--external-export", action="store_true", help="Export Sync (offline / outside the platform)")
    parser.add_argument("--max-credits", type=int, default=100, help="refuse any single call projected above this")
    parser.add_argument("--out", type=Path, help="output video (default: <video>.scored.mp4)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    # Absolute paths never start with "-", so a file name cannot be mistaken for an ffmpeg option.
    video: Path = args.video.resolve()
    if not video.is_file():
        print(f"Video not found: {video}")
        return 2
    output: Path = (args.out or video.with_name(f"{video.stem}.scored.mp4")).resolve()

    with rightsfoundry.Client(max_credits_per_request=args.max_credits) as client:
        # Ask once for a large page: Deep Search page 1 is charged every time it is sent.
        results = client.tracks.deep_search(args.brief, limit=50, instrumental=True)
        track = pick_track(results.data, video_seconds=args.duration, bpm_min=args.bpm_min, bpm_max=args.bpm_max)
        if track is None:
            print(f"None of {len(results.data)} candidates covers {args.duration:.0f}s in the BPM window.")
            print("Loosen --bpm-min/--bpm-max or broaden --brief.")
            return 1
        print(f"Picked {track.id}: {track.title} by {track.artist} ({track.duration:.0f}s, {effective_bpm(track)} bpm)")

        try:
            link = client.tracks.sync(track.id, external_export=args.external_export)
        except rightsfoundry.CreditCapExceededError as exc:
            print(f"Sync would cost {exc.projected} credits, above --max-credits {exc.cap}. Nothing was charged.")
            return 1
        mode = "Export Sync" if link.external_export else "Standard sync"
        print(f"{mode}: {link.credits_charged} credits charged, reissued={link.reissued}")

        masters = Path("masters")
        masters.mkdir(mode=0o700, exist_ok=True)
        audio = client.sync_links.download(link, masters, overwrite=True).resolve()
        print("WAV master:", audio)

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg not found on PATH. Install it, then run:")
        print("  " + shlex.join(ffmpeg_command("ffmpeg", video, audio, output, args.duration)))
        return 0
    # The argument list comes from fixed flags plus absolute local paths; no shell is involved.
    try:
        subprocess.run(ffmpeg_command(ffmpeg, video, audio, output, args.duration), check=True)  # noqa: S603
    except subprocess.CalledProcessError as exc:
        print(f"ffmpeg failed with exit code {exc.returncode}; the licensed master is still at {audio}")
        return 1
    print("Scored video:", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
