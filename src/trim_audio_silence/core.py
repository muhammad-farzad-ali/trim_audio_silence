from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MIN_SEGMENT_DURATION = 0.05
EDGE_TOLERANCE = 0.08
_EPSILON = 1e-6

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")

_REQUIRED_TOOLS = ("ffmpeg", "ffprobe")


class MediaError(RuntimeError):
    """Raised when an audio file cannot be inspected or processed."""


@dataclass(frozen=True)
class Silence:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    sample_rate: int | None = None
    channels: int | None = None


@dataclass(frozen=True)
class TrimPlan:
    info: AudioInfo
    silences: tuple[Silence, ...]
    segments: tuple[Segment, ...]

    @property
    def source_duration(self) -> float:
        return self.info.duration

    @property
    def output_duration(self) -> float:
        return sum(segment.duration for segment in self.segments)

    @property
    def all_silence(self) -> bool:
        return not self.segments

    @property
    def trimmed(self) -> bool:
        return (
            bool(self.segments)
            and abs(self.source_duration - self.output_duration) > _EPSILON
        )


@dataclass(frozen=True)
class TrimResult:
    source: Path
    output: Path | None
    plan: TrimPlan

    @property
    def source_duration(self) -> float:
        return self.plan.source_duration

    @property
    def output_duration(self) -> float:
        return self.plan.output_duration

    @property
    def removed_duration(self) -> float:
        return max(0.0, self.source_duration - self.output_duration)

    @property
    def segments(self) -> tuple[Segment, ...]:
        return self.plan.segments


def ensure_tools() -> None:
    missing = [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise MediaError(
            "required executable(s) not found on PATH: " + ", ".join(missing)
        )


def probe_audio(path: Path) -> AudioInfo:
    ensure_tools()
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    process = _run(command)
    try:
        data = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(f"could not parse ffprobe output for {path}") from exc

    streams = data.get("streams") or []
    if not streams:
        raise MediaError(f"no audio stream found in {path}")

    stream = streams[0]
    duration = _parse_number(data.get("format", {}).get("duration"))
    if duration is None:
        duration = _parse_number(stream.get("duration"))
    if duration is None or duration <= 0:
        raise MediaError(f"could not determine duration of {path}")

    return AudioInfo(
        duration=duration,
        sample_rate=_parse_int(stream.get("sample_rate")),
        channels=_parse_int(stream.get("channels")),
    )


def detect_silence(
    path: Path,
    duration: float,
    *,
    threshold_db: float = -35.0,
    min_silence: float = 0.35,
) -> tuple[Silence, ...]:
    ensure_tools()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-map",
        "0:a:0",
        "-af",
        f"silencedetect=noise={threshold_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    process = _run(command)
    return parse_silence_intervals(process.stderr, duration)


def parse_silence_intervals(stderr: str, duration: float) -> tuple[Silence, ...]:
    intervals: list[Silence] = []
    current_start: float | None = None

    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            current_start = float(start_match.group(1))
            continue

        end_match = _SILENCE_END_RE.search(line)
        if end_match:
            start = current_start if current_start is not None else 0.0
            intervals.append(Silence(start=start, end=float(end_match.group(1))))
            current_start = None

    if current_start is not None:
        intervals.append(Silence(start=current_start, end=duration))

    merged = list(merge_silences(intervals, duration))

    # MP3 duration estimates include encoder delay/padding, so silencedetect
    # can report EOF slightly before the container duration.
    if merged and duration - merged[-1].end <= EDGE_TOLERANCE:
        merged[-1] = Silence(start=merged[-1].start, end=duration)

    return tuple(merged)


def merge_silences(
    intervals: Sequence[Silence], duration: float
) -> tuple[Silence, ...]:
    normalized: list[Silence] = []
    for interval in intervals:
        start = min(max(interval.start, 0.0), duration)
        end = min(max(interval.end, 0.0), duration)
        if end - start > _EPSILON:
            normalized.append(Silence(start=start, end=end))

    normalized.sort(key=lambda interval: interval.start)

    merged: list[Silence] = []
    for interval in normalized:
        if merged and interval.start <= merged[-1].end + _EPSILON:
            previous = merged[-1]
            merged[-1] = Silence(
                start=previous.start, end=max(previous.end, interval.end)
            )
        else:
            merged.append(interval)

    return tuple(merged)


def compute_keep_segments(
    silences: Sequence[Silence],
    duration: float,
    *,
    padding: float = 0.05,
    min_segment: float = MIN_SEGMENT_DURATION,
) -> tuple[Segment, ...]:
    if duration <= 0:
        return ()
    if padding < 0:
        raise ValueError("padding must not be negative")
    if min_segment < 0:
        raise ValueError("min_segment must not be negative")

    cuts: list[Segment] = []
    for silence in merge_silences(silences, duration):
        cut_start = 0.0 if silence.start <= _EPSILON else silence.start + padding
        cut_end = (
            duration if silence.end >= duration - _EPSILON else silence.end - padding
        )
        if cut_end - cut_start > _EPSILON:
            cuts.append(Segment(start=cut_start, end=cut_end))

    segments: list[Segment] = []
    cursor = 0.0
    threshold = max(min_segment, _EPSILON)
    for cut in cuts:
        if cut.start - cursor >= threshold:
            segments.append(Segment(start=cursor, end=cut.start))
        cursor = max(cursor, cut.end)

    if duration - cursor >= threshold:
        segments.append(Segment(start=cursor, end=duration))

    return tuple(segments)


def plan_trim(
    source: Path,
    *,
    threshold_db: float = -35.0,
    min_silence: float = 0.35,
    padding: float = 0.05,
    min_segment: float = MIN_SEGMENT_DURATION,
) -> TrimPlan:
    info = probe_audio(source)
    silences = detect_silence(
        source,
        info.duration,
        threshold_db=threshold_db,
        min_silence=min_silence,
    )
    segments = compute_keep_segments(
        silences,
        info.duration,
        padding=padding,
        min_segment=min_segment,
    )
    return TrimPlan(info=info, silences=silences, segments=segments)


def trim_file(
    source: Path,
    output: Path,
    *,
    threshold_db: float = -35.0,
    min_silence: float = 0.35,
    padding: float = 0.05,
    min_segment: float = MIN_SEGMENT_DURATION,
    fade_ms: int = 10,
    quality: int = 2,
) -> TrimResult:
    plan = plan_trim(
        source,
        threshold_db=threshold_db,
        min_silence=min_silence,
        padding=padding,
        min_segment=min_segment,
    )
    if not plan.trimmed:
        return TrimResult(source=source, output=None, plan=plan)

    render_segments(
        source,
        output,
        plan.segments,
        fade_ms=fade_ms,
        quality=quality,
    )
    return TrimResult(source=source, output=output, plan=plan)


def render_segments(
    source: Path,
    output: Path,
    segments: Sequence[Segment],
    *,
    fade_ms: int = 10,
    quality: int = 2,
) -> None:
    ensure_tools()
    if not segments:
        raise MediaError("cannot render output without any audio segments")
    if not 0 <= quality <= 9:
        raise ValueError("quality must be between 0 and 9")
    if fade_ms < 0:
        raise ValueError("fade_ms must not be negative")

    output.parent.mkdir(parents=True, exist_ok=True)
    filter_script = build_filter_script(segments, fade_ms=fade_ms)

    temporary_output = output.parent / (
        f".{output.stem}.{uuid.uuid4().hex}.part{output.suffix}"
    )
    script_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".ffmpeg", prefix="trim-silence-", delete=False
        ) as handle:
            script_path = Path(handle.name)
            handle.write(filter_script)

        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-filter_complex_script",
            str(script_path),
            "-map",
            "[out]",
            "-map_metadata",
            "0",
            "-c:a",
            "libmp3lame",
            "-q:a",
            str(quality),
            str(temporary_output),
        ]
        _run(command)
        os.replace(temporary_output, output)
    finally:
        if script_path is not None:
            script_path.unlink(missing_ok=True)
        temporary_output.unlink(missing_ok=True)


def build_filter_script(segments: Sequence[Segment], *, fade_ms: int = 10) -> str:
    fade_seconds = max(0.0, fade_ms / 1000.0)
    inputs: list[str] = []

    for index, segment in enumerate(segments):
        chain = (
            f"atrim=start={segment.start:.6f}:end={segment.end:.6f},"
            "asetpts=PTS-STARTPTS"
        )
        if fade_seconds > 0 and segment.duration > 0:
            fade = min(fade_seconds, segment.duration / 2)
            chain += (
                f",afade=t=in:st=0:d={fade:.6f}"
                f",afade=t=out:st={segment.duration - fade:.6f}:d={fade:.6f}"
            )
        inputs.append(f"[0:a:0]{chain}[s{index}]")

    if len(segments) == 1:
        inputs.append("[s0]anull[out]")
    else:
        labels = "".join(f"[s{index}]" for index in range(len(segments)))
        inputs.append(f"{labels}concat=n={len(segments)}:v=0:a=1[out]")

    return ";\n".join(inputs)


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaError(f"executable not found: {command[0]}") from exc

    if process.returncode != 0:
        details = _tail(process.stderr or process.stdout)
        raise MediaError(
            f"{command[0]} exited with status {process.returncode}: {details}"
        )
    return process


def _tail(text: str, limit: int = 2000) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _parse_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _parse_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
