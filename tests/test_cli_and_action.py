"""The ``rightsfoundry`` CLI and the GitHub Action entrypoint (SEC-20 output hardening)."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import rightsfoundry as rf
from helpers import (
    CANARY_KEY,
    CANARY_SIG,
    SIGNED_WAV,
    MockTransport,
    Reply,
    credits_json,
    error_json,
    page_json,
    sync_json,
    track_json,
)
from rightsfoundry import _action, _cli


def patch_client(monkeypatch: pytest.MonkeyPatch, module: Any, transport: MockTransport) -> None:
    class Patched(rf.Client):
        def __init__(self, **kw: Any) -> None:
            kw.setdefault("api_key", CANARY_KEY)
            super().__init__(transport=transport, **kw)
            self._pipeline._sleep = lambda _s: None

        @classmethod
        def unauthenticated(cls, **kw: Any) -> rf.Client:  # type: ignore[override]
            return rf.Client.unauthenticated(transport=transport, **kw)

    monkeypatch.setattr(module, "Client", Patched)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = _cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# --- CLI -------------------------------------------------------------------------------------------------


def test_cli_health(monkeypatch: pytest.MonkeyPatch) -> None:
    t = MockTransport(
        [
            Reply(200, {"success": True, "status": "alive"}),
            Reply(503, {"success": False, "status": "not ready", "checks": {}}),
        ]
        + [Reply(503, {"success": False, "status": "not ready", "checks": {}})] * 2
    )
    patch_client(monkeypatch, _cli, t)
    code, out, _ = run_cli(["health", "--mode", "live"])
    assert code == 0 and "alive (HTTP 200)" in out
    code, out, _ = run_cli(["health", "--mode", "ready", "--json"])
    assert code == 3 and json.loads(out)["status"] == "not ready"
    assert all("authorization" not in r.headers for r in t.calls)


def test_cli_credits_and_taxonomies(monkeypatch: pytest.MonkeyPatch) -> None:
    t = MockTransport(
        [
            Reply(200, credits_json(spendable=3700, balance=1200, overdraft=2500)),
            Reply(200, {"success": True, "data": {"genre": [{"value": "jazz", "count": 812}], "mood": []}}),
            Reply(200, {"success": True, "data": {"genre": [{"value": "jazz", "count": 812}], "mood": []}}),
        ]
    )
    patch_client(monkeypatch, _cli, t)
    code, out, _ = run_cli(["credits"])
    assert code == 0 and "Spendable API credits: 3700" in out and "sync" in out
    code, out, _ = run_cli(["taxonomies", "--facet", "genre"])
    assert code == 0 and "jazz (812)" in out
    code, out, _ = run_cli(["taxonomies", "--facet", "genre", "--json"])
    assert json.loads(out) == {"genre": [{"value": "jazz", "count": 812}]}


def test_cli_search_flags_map_to_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    t = MockTransport([Reply(200, page_json([1], total=1, corrections=[{"from": "orchestal", "to": "orchestral"}]))])
    patch_client(monkeypatch, _cli, t)
    code, out, _ = run_cli(
        [
            "search",
            "dark orchestal",
            "--genre",
            "cinematic",
            "--vocals",
            "instrumental",
            "--bpm-min",
            "90",
            "--bpm-max",
            "120",
            "--key",
            "A minor",
            "--limit",
            "5",
        ]
    )
    assert code == 0 and "Golden Hour" in out and "orchestral" in out
    q = dict(t.calls[0].query)
    assert q == {
        "search": "dark orchestal",
        "genre": "cinematic",
        "vocals": "instrumental",
        "bpmMin": "90",
        "bpmMax": "120",
        "key": "A minor",
        "limit": "5",
    }


def test_cli_deep_search_similar_track(monkeypatch: pytest.MonkeyPatch) -> None:
    t = MockTransport(
        [Reply(200, page_json()), Reply(200, page_json()), Reply(200, {"success": True, "data": track_json(5)})]
    )
    patch_client(monkeypatch, _cli, t)
    assert run_cli(["deep-search", "warm hopeful guitar", "--instrumental", "--limit", "10"])[0] == 0
    assert t.calls[0].json() == {"query": "warm hopeful guitar", "page": 1, "limit": 10, "instrumental": True}
    assert run_cli(["similar", "https://cdn.example.com/ref.mp3", "--json"])[0] == 0
    code, out, _ = run_cli(["track", "5"])
    assert code == 0 and "A warm acoustic guitar piece." in out


def test_cli_sync_redacts_human_output_and_downloads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = MockTransport(
        [Reply(200, sync_json()), Reply(200, sync_json(charged=0, reissued=True)), Reply(200, b"RIFFwav")]
    )
    patch_client(monkeypatch, _cli, t)
    code, out, _ = run_cli(["sync", "29446060"])
    assert code == 0 and CANARY_SIG not in out and "sig=[REDACTED]" in out and "credits charged: 10" in out
    code, out, _ = run_cli(["sync", "29446060", "--download", str(tmp_path), "--json"])
    data = json.loads(out)
    assert data["download_url"] == SIGNED_WAV and data["reissued"] is True
    assert Path(data["downloaded_file"]).read_bytes() == b"RIFFwav"
    assert "authorization" not in t.calls[2].headers


def test_cli_errors_and_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = error_json(f"Insufficient credits. Please top up to continue. {CANARY_KEY}", apiCredits=1, bandwidthBytes=0)
    t = MockTransport(
        [
            Reply(402, body),
            Reply(
                400,
                error_json(
                    "Could not download or decode the audio at file_url. "
                    "It must be a publicly reachable .mp3 or .wav under 120MB."
                ),
            ),
        ]
    )
    patch_client(monkeypatch, _cli, t)
    code, _, err = run_cli(["track", "not-an-id"])
    assert code == 2 and "error:" in err
    code, _, err = run_cli(["search"])
    assert code == 1 and CANARY_KEY not in err and "Insufficient" in err
    code, _, err = run_cli(["similar", "https://cdn.example.com/x.mp3"])
    assert code == 1 and "charge state: charged" in err
    code, _, err = run_cli(["similar", "http://localhost/x.mp3"])
    assert code == 2


def test_cli_without_key_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRF_API_KEY", raising=False)
    code, _, err = run_cli(["credits"])
    assert code == 2 and "TRF_API_KEY" in err


def test_python_dash_m_version() -> None:
    src = str(Path(rf.__file__).resolve().parents[1])
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, {src!r}); sys.argv=['rightsfoundry','--version']; "
            "import runpy; runpy.run_module('rightsfoundry', run_name='__main__')",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0 and rf.__version__ in out.stdout


# --- GitHub Action ----------------------------------------------------------------------------------------


def parse_outputs(text: str) -> dict[str, str]:
    outputs: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue
        name, delim = line.split("<<", 1)
        i += 1
        value_lines = []
        while lines[i] != delim:
            value_lines.append(lines[i])
            i += 1
        outputs[name] = "\n".join(value_lines)
        i += 1
    return outputs


def action_env(tmp_path: Path, **inputs: str) -> dict[str, str]:
    env = {"GITHUB_OUTPUT": str(tmp_path / "out.txt"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    for k, v in inputs.items():
        env["INPUT_" + k.upper()] = v
    return env


def test_action_sync_with_download_and_injection_resistance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    evil_title = "Evil\nfake-output<<EOF\ninjected\nEOF\n::set-output name=x::y | `rm -rf` <script>"
    body = sync_json()
    body["data"]["title"] = evil_title
    t = MockTransport([Reply(200, body), Reply(200, b"RIFFwav")])
    patch_client(monkeypatch, _action, t)
    out = io.StringIO()
    env = action_env(
        tmp_path, ACTION="sync", API_KEY=CANARY_KEY, TRACK_ID="29446060", DOWNLOAD_PATH=str(tmp_path / "dl") + "/"
    )
    code = _action.main(env, stdout=out)
    assert code == 0
    outputs = parse_outputs((tmp_path / "out.txt").read_text())
    assert set(outputs) == {
        "result-json",
        "track-id",
        "track-title",
        "track-artist",
        "credits-charged",
        "downloaded-file",
        "request-id",
    }
    assert outputs["track-title"] == evil_title
    assert outputs["credits-charged"] == "10" and Path(outputs["downloaded-file"]).read_bytes() == b"RIFFwav"
    result = json.loads(outputs["result-json"])
    assert (
        result["data"]["download_url"].endswith("sig=[REDACTED]")
        if "data" in result
        else result["download_url"].endswith("sig=[REDACTED]")
    )
    assert CANARY_SIG not in outputs["result-json"]
    summary = (tmp_path / "summary.md").read_text()
    assert CANARY_SIG not in summary and CANARY_KEY not in summary and "<script>" not in summary
    assert "\\|" in summary
    log = out.getvalue()
    # secrets only ever appear inside ::add-mask:: commands, issued while commands are enabled
    for line in log.splitlines():
        if CANARY_KEY in line or CANARY_SIG in line:
            assert line.startswith("::add-mask::")
    assert re.match(r"::stop-commands::[0-9a-f]{32}\n", log)
    assert "::set-output" not in log


def test_action_mask_commands_are_outside_stop_commands_block(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = MockTransport([Reply(200, credits_json())])
    patch_client(monkeypatch, _action, t)
    out = io.StringIO()
    assert _action.main(action_env(tmp_path, ACTION="credits", API_KEY=CANARY_KEY), stdout=out) == 0
    stopped: str | None = None
    for line in out.getvalue().splitlines():
        if line.startswith("::stop-commands::"):
            stopped = line.split("::")[2]
        elif stopped is not None and line == f"::{stopped}::":
            stopped = None
        elif line.startswith("::add-mask::"):
            assert stopped is None
    outputs = parse_outputs((tmp_path / "out.txt").read_text())
    assert outputs["credits-spendable"] == "1000"


def test_action_search_and_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = MockTransport(
        [Reply(200, page_json([7, 8], total=2)), Reply(200, {"success": True, "status": "healthy", "checks": {}})]
    )
    patch_client(monkeypatch, _action, t)
    env = action_env(
        tmp_path,
        ACTION="search",
        API_KEY=CANARY_KEY,
        QUERY="jazz",
        GENRE="jazz",
        BPM_MIN="90",
        LIMIT="2",
        MAX_CREDITS="5",
    )
    env["INPUT_MAX_CREDITS"] = ""
    assert _action.main(env, stdout=io.StringIO()) == 0
    outputs = parse_outputs((tmp_path / "out.txt").read_text())
    assert outputs["track-id"] == "7" and json.loads(outputs["result-json"])["total"] == 2
    assert dict(t.calls[0].query)["bpmMin"] == "90"
    health_env = action_env(tmp_path, ACTION="health")
    assert _action.main(health_env, stdout=io.StringIO()) == 0
    assert "authorization" not in t.calls[1].headers


@pytest.mark.parametrize(
    ("inputs", "code"),
    [
        ({"ACTION": "launch-missiles"}, 2),
        ({"ACTION": "sync"}, 2),
        ({"ACTION": "sync", "API_KEY": CANARY_KEY}, 2),
        ({"ACTION": "deep-search", "API_KEY": CANARY_KEY}, 2),
        ({"ACTION": "search", "API_KEY": CANARY_KEY, "LIMIT": "abc"}, 2),
        ({"ACTION": "search", "API_KEY": CANARY_KEY, "ENVIRONMENT": "prod"}, 2),
        ({"ACTION": "credits", "API_KEY": "AB12"}, 1),
    ],
)
def test_action_input_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, inputs: dict[str, str], code: int
) -> None:
    patch_client(monkeypatch, _action, MockTransport())
    out = io.StringIO()
    assert _action.main(action_env(tmp_path, **inputs), stdout=out) == code
    assert "::error::" in out.getvalue()
    for line in out.getvalue().splitlines():
        if CANARY_KEY in line:
            assert line.startswith("::add-mask::")


def test_action_api_error_reports_charge_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t = MockTransport(
        [
            Reply(
                400,
                error_json(
                    "Could not download or decode the audio at file_url. "
                    "It must be a publicly reachable .mp3 or .wav under 120MB."
                ),
            )
        ]
    )
    patch_client(monkeypatch, _action, t)
    out = io.StringIO()
    env = action_env(tmp_path, ACTION="search-similar", API_KEY=CANARY_KEY, FILE_URL="https://cdn.example.com/x.wav")
    assert _action.main(env, stdout=out) == 1
    assert "charge_state=charged" in out.getvalue()


def test_write_outputs_delimiter_never_collides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = iter(["a" * 32, "a" * 32, "b" * 32])
    import secrets

    monkeypatch.setattr(secrets, "token_hex", lambda _n: next(tokens))
    path = tmp_path / "o.txt"
    _action.write_outputs(str(path), {"x": "line\nghadelimiter_" + "a" * 32 + "\nmore"})
    parsed = parse_outputs(path.read_text())
    assert parsed == {"x": "line\nghadelimiter_" + "a" * 32 + "\nmore"}


def _action_run_block() -> str:
    lines = (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("run:"))
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return "\n".join(block)


def test_action_yml_never_interpolates_inputs_in_run() -> None:
    block = _action_run_block()
    assert "${{" not in block
    assert "python3 -I -c" in block and "TRF_ACTION_SRC" in block
    text = (Path(__file__).resolve().parents[1] / "action.yml").read_text(encoding="utf-8")
    env_keys = re.findall(r"^\s+(PYTHON[A-Z]*):", text, flags=re.MULTILINE)
    assert "PYTHONSAFEPATH" not in env_keys and "PYTHONPATH" not in env_keys


def _action_python_source() -> str:
    block = _action_run_block()
    start = block.index("-c '") + len("-c '")
    end = block.rindex("'")
    lines = block[start:end].split("\n")
    indent = min(len(line) - len(line.lstrip()) for line in lines[1:] if line.strip())
    return "\n".join([lines[0], *(line[indent:] for line in lines[1:])])


def test_h5_action_ignores_planted_modules_in_the_workspace(tmp_path: Path) -> None:
    """H5: run the exact action.yml command from a hostile checkout; nothing planted is imported."""
    marker = tmp_path / "pwned"
    payload = f"open({str(marker)!r}, 'w').write('x')\n"
    (tmp_path / "rightsfoundry").mkdir()
    (tmp_path / "rightsfoundry" / "__init__.py").write_text(payload)
    (tmp_path / "rightsfoundry" / "_action.py").write_text(payload + "def main():\n    return 0\n")
    (tmp_path / "json.py").write_text(payload)
    src = str(Path(rf.__file__).resolve().parents[1])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "TRF_ACTION_SRC": src,
        "PYTHONPATH": str(tmp_path),  # ignored under -I
        "INPUT_ACTION": "not-an-action",
    }
    if "SYSTEMROOT" in os.environ:  # Windows needs it to start Python at all
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    out = subprocess.run(
        [sys.executable, "-I", "-c", _action_python_source()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 2, out.stderr
    assert "input action must be one of" in out.stdout
    assert not marker.exists()
