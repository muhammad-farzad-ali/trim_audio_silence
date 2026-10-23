from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trim_audio_silence import cli, core

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg and ffprobe are required",
)


def make_tone_with_gaps(path: Path, duration: float = 5.0) -> None:
    expression = "if(between(t,1,2)+between(t,3,4),1,0)"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=44100:duration={duration}",
            "-af",
            f"volume=eval=frame:volume='{expression}'",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def make_silence(path: Path, duration: float = 2.0) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=44100:duration={duration}",
            "-af",
            "volume=0",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def make_continuous_tone(path: Path, duration: float = 2.0) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=44100:duration={duration}",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def test_trim_file_removes_gaps(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp3"
    output = tmp_path / "out" / "sample.mp3"
    make_tone_with_gaps(source)

    result = core.trim_file(
        source,
        output,
        threshold_db=-50,
        min_silence=0.3,
        padding=0.05,
        fade_ms=5,
    )

    assert result.plan.trimmed
    assert result.output == output
    assert output.exists()
    assert len(result.segments) == 2
    assert 1.8 <= result.output_duration <= 2.6

    output_info = core.probe_audio(output)
    assert 1.8 <= output_info.duration <= 2.9


def test_all_silence_file_is_skipped(tmp_path: Path) -> None:
    source = tmp_path / "silence.mp3"
    make_silence(source)

    result = core.trim_file(
        source,
        tmp_path / "out.mp3",
        threshold_db=-50,
        min_silence=0.3,
    )

    assert result.plan.all_silence
    assert not result.plan.trimmed
    assert result.output is None
    assert not (tmp_path / "out.mp3").exists()


def test_file_without_silence_is_not_rewritten(tmp_path: Path) -> None:
    source = tmp_path / "tone.mp3"
    make_continuous_tone(source)

    result = core.trim_file(source, tmp_path / "out.mp3", threshold_db=-50)

    assert not result.plan.trimmed
    assert result.output is None
    assert not (tmp_path / "out.mp3").exists()


def test_cli_dry_run_does_not_write(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp3"
    make_tone_with_gaps(source)
    output_dir = tmp_path / "trimmed"

    code = cli.run(
        [
            str(source),
            "-o",
            str(output_dir),
            "--threshold-db",
            "-50",
            "--min-silence",
            "0.3",
            "--dry-run",
        ]
    )

    assert code == 0
    assert not output_dir.exists()


def test_cli_directory_mode_preserves_relative_paths(tmp_path: Path) -> None:
    input_dir = tmp_path / "audio"
    nested = input_dir / "nested"
    nested.mkdir(parents=True)
    make_tone_with_gaps(input_dir / "first.mp3")
    make_tone_with_gaps(nested / "second.mp3")
    output_dir = tmp_path / "trimmed"

    code = cli.run(
        [
            str(input_dir),
            "-o",
            str(output_dir),
            "--threshold-db",
            "-50",
            "--min-silence",
            "0.3",
        ]
    )

    assert code == 0
    assert (output_dir / "first.mp3").exists()
    assert (output_dir / "nested" / "second.mp3").exists()

    code = cli.run(
        [
            str(input_dir),
            "-o",
            str(output_dir),
            "--threshold-db",
            "-50",
            "--min-silence",
            "0.3",
        ]
    )
    assert code == 0


def test_cli_rejects_output_that_would_overwrite_source(tmp_path: Path) -> None:
    source = tmp_path / "sample.mp3"
    source.write_bytes(b"")

    code = cli.run([str(source), "-o", str(tmp_path)])

    assert code == 2
