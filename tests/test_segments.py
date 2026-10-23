from __future__ import annotations

import pytest

from trim_audio_silence.core import (
    Segment,
    Silence,
    build_filter_script,
    compute_keep_segments,
    merge_silences,
    parse_silence_intervals,
)

SAMPLE_STDERR = """
Input #0, mp3, from 'sample.mp3':
  Duration: 00:00:06.00, start: 0.025057, bitrate: 36 kb/s
Stream mapping:
  Stream #0:0 -> #0:0 (mp3 (mp3float) -> pcm_s16le (native))
[silencedetect @ 0x55] silence_start: 0
[silencedetect @ 0x55] silence_end: 1.25 | silence_duration: 1.25
[silencedetect @ 0x55] silence_start: 4.5
[silencedetect @ 0x55] silence_end: 5 | silence_duration: 0.5
"""


def test_parse_silence_intervals() -> None:
    assert parse_silence_intervals(SAMPLE_STDERR, 6.0) == (
        Silence(0.0, 1.25),
        Silence(4.5, 5.0),
    )


def test_parse_open_ended_trailing_silence() -> None:
    stderr = "[silencedetect @ 0x55] silence_start: 4.5\n"
    assert parse_silence_intervals(stderr, 6.0) == (Silence(4.5, 6.0),)


def test_parse_snaps_near_eof_to_duration() -> None:
    stderr = (
        "[silencedetect @ 0x55] silence_start: 4.5\n"
        "[silencedetect @ 0x55] silence_end: 5.95 | silence_duration: 1.45\n"
    )
    assert parse_silence_intervals(stderr, 6.0) == (Silence(4.5, 6.0),)


def test_parse_keeps_silence_that_ends_well_before_eof() -> None:
    stderr = (
        "[silencedetect @ 0x55] silence_start: 4.5\n"
        "[silencedetect @ 0x55] silence_end: 5.5 | silence_duration: 1.0\n"
    )
    assert parse_silence_intervals(stderr, 6.0) == (Silence(4.5, 5.5),)


def test_parse_does_not_snap_silence_start_near_bof() -> None:
    stderr = (
        "[silencedetect @ 0x55] silence_start: 0.05\n"
        "[silencedetect @ 0x55] silence_end: 1.0 | silence_duration: 0.95\n"
    )
    assert parse_silence_intervals(stderr, 6.0) == (Silence(0.05, 1.0),)


def test_parse_clamps_and_merges_overlaps() -> None:
    stderr = (
        "[silencedetect @ 0x55] silence_start: -1\n"
        "[silencedetect @ 0x55] silence_end: 2\n"
        "[silencedetect @ 0x55] silence_start: 1.5\n"
        "[silencedetect @ 0x55] silence_end: 8\n"
    )
    assert parse_silence_intervals(stderr, 5.0) == (Silence(0.0, 5.0),)


def test_merge_silences_keeps_touching_intervals_together() -> None:
    merged = merge_silences(
        [Silence(2.0, 3.0), Silence(0.0, 1.0), Silence(1.0, 2.0)], 10.0
    )
    assert merged == (Silence(0.0, 3.0),)


def test_compute_keep_segments_with_padding() -> None:
    silences = [Silence(0.0, 1.0), Silence(2.0, 3.0), Silence(9.0, 10.0)]
    segments = compute_keep_segments(silences, 10.0, padding=0.1)
    assert segments == (Segment(0.9, 2.1), Segment(2.9, 9.1))


def test_compute_keep_segments_without_silence() -> None:
    assert compute_keep_segments([], 10.0) == (Segment(0.0, 10.0),)


def test_compute_keep_segments_all_silence() -> None:
    assert compute_keep_segments([Silence(0.0, 10.0)], 10.0) == ()


def test_short_gap_is_kept_when_padding_covers_it() -> None:
    segments = compute_keep_segments([Silence(4.0, 4.08)], 10.0, padding=0.1)
    assert segments == (Segment(0.0, 10.0),)


def test_too_short_tail_segment_is_dropped() -> None:
    segments = compute_keep_segments(
        [Silence(0.03, 10.0)], 10.0, padding=0.0, min_segment=0.05
    )
    assert segments == ()


def test_negative_padding_is_rejected() -> None:
    with pytest.raises(ValueError):
        compute_keep_segments([], 10.0, padding=-0.1)


def test_filter_script_concatenates_segments() -> None:
    script = build_filter_script([Segment(1.0, 2.0), Segment(3.0, 4.5)], fade_ms=0)
    assert "[0:a:0]atrim=start=1.000000:end=2.000000" in script
    assert "[0:a:0]atrim=start=3.000000:end=4.500000" in script
    assert "[s0][s1]concat=n=2:v=0:a=1[out]" in script


def test_filter_script_single_segment() -> None:
    script = build_filter_script([Segment(1.0, 2.0)], fade_ms=0)
    assert "[s0]anull[out]" in script
    assert "concat" not in script


def test_filter_script_adds_fades() -> None:
    script = build_filter_script([Segment(1.0, 2.0)], fade_ms=10)
    assert "afade=t=in:st=0:d=0.010000" in script
    assert "afade=t=out:st=0.990000:d=0.010000" in script
