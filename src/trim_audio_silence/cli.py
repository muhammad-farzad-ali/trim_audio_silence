from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import core

DEFAULT_OUTPUT_NAME = "trimmed"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trim-silence",
        description=(
            "Remove silent gaps from MP3 files. Accepts a single MP3 or a "
            "directory that is searched recursively. Originals are never "
            "modified."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="MP3 file or directory containing MP3 files",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help=(
            "directory for trimmed files (default: <path>/trimmed for a "
            "directory, <file-parent>/trimmed for a file)"
        ),
    )
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=-35.0,
        help="silence threshold in dB, e.g. -35 (default: -35)",
    )
    parser.add_argument(
        "--min-silence",
        type=float,
        default=0.35,
        help="minimum silence duration to cut in seconds (default: 0.35)",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=0.05,
        help="silence kept around each sound in seconds (default: 0.05)",
    )
    parser.add_argument(
        "--fade-ms",
        type=int,
        default=10,
        help="fade duration at segment joins in milliseconds (default: 10)",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=2,
        help="libmp3lame VBR quality, 0 (best) to 9 (smallest) (default: 2)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing files in the output directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be cut without writing any files",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only print errors",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    errors = _validate_args(args)
    if errors:
        parser.error("; ".join(errors))

    try:
        return _run(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


def _run(args: argparse.Namespace) -> int:
    source = args.path.expanduser()
    if not source.exists():
        print(f"error: path does not exist: {source}", file=sys.stderr)
        return 1
    if not source.is_file() and not source.is_dir():
        print(f"error: unsupported path type: {source}", file=sys.stderr)
        return 1
    if source.is_file() and source.suffix.lower() != ".mp3":
        print(f"error: not an MP3 file: {source}", file=sys.stderr)
        return 1

    output_dir = (
        args.output_dir.expanduser()
        if args.output_dir
        else (
            source / DEFAULT_OUTPUT_NAME
            if source.is_dir()
            else source.parent / DEFAULT_OUTPUT_NAME
        )
    )

    base_dir = source if source.is_dir() else source.parent
    targets = _collect_mp3s(source, output_dir)
    if not targets:
        print(f"no MP3 files found in {source}", file=sys.stderr)
        return 1

    targets_with_output = [
        (target, output_dir / target.relative_to(base_dir)) for target in targets
    ]

    collisions = [
        target
        for target, output in targets_with_output
        if target.resolve() == output.resolve()
    ]
    if collisions:
        print(
            "error: output directory would overwrite source file(s): "
            + ", ".join(str(path) for path in collisions[:3]),
            file=sys.stderr,
        )
        return 2

    _say(
        args,
        f"{'Planning' if args.dry_run else 'Processing'} "
        f"{len(targets)} MP3 file(s) from {source}",
    )

    trimmed = 0
    skipped = 0
    failed = 0

    for index, (target, output) in enumerate(targets_with_output, start=1):
        _say(args, f"[{index}/{len(targets)}] {target}")

        if not args.dry_run and output.exists() and not args.overwrite:
            skipped += 1
            _say(args, "  output exists, skipping (use --overwrite)")
            continue

        try:
            if args.dry_run:
                plan = core.plan_trim(
                    target,
                    threshold_db=args.threshold_db,
                    min_silence=args.min_silence,
                    padding=args.padding,
                )
                result = core.TrimResult(source=target, output=None, plan=plan)
            else:
                result = core.trim_file(
                    target,
                    output,
                    threshold_db=args.threshold_db,
                    min_silence=args.min_silence,
                    padding=args.padding,
                    fade_ms=args.fade_ms,
                    quality=args.quality,
                )
        except (core.MediaError, OSError, ValueError) as exc:
            failed += 1
            print(f"  error: {exc}", file=sys.stderr)
            continue

        if result.plan.all_silence:
            skipped += 1
            print("  entire file is silence, skipping", file=sys.stderr)
            continue

        if not result.plan.trimmed:
            skipped += 1
            _say(
                args,
                f"  no silence found, skipping ({_format_duration(result.source_duration)})",
            )
            continue

        trimmed += 1
        _say(args, "  " + _format_change(result))
        if args.dry_run:
            _say(args, "  keep: " + _format_segments(result.segments))
            _say(args, f"  would write {output}")
        else:
            _say(args, f"  wrote {output}")

    if not args.quiet:
        if args.dry_run:
            print(f"\nPlanned {trimmed} trim(s), {skipped} skipped, {failed} failed.")
        else:
            print(f"\nTrimmed {trimmed} file(s), {skipped} skipped, {failed} failed.")

    return 1 if failed else 0


def _collect_mp3s(source: Path, output_dir: Path) -> list[Path]:
    if source.is_file():
        return [source]

    output_resolved = output_dir.resolve()
    targets: list[Path] = []
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".mp3":
            continue
        if _is_within(path, output_resolved):
            continue
        targets.append(path)
    return sorted(targets, key=lambda path: str(path).lower())


def _is_within(path: Path, directory: Path) -> bool:
    try:
        return path.resolve().is_relative_to(directory)
    except (OSError, ValueError):
        return False


def _format_change(result: core.TrimResult) -> str:
    source = result.source_duration
    removed = result.removed_duration
    percent = (removed / source * 100.0) if source > 0 else 0.0
    return (
        f"{_format_duration(source)} -> {_format_duration(result.output_duration)} "
        f"(removed {_format_duration(removed)}, {percent:.0f}%, "
        f"{len(result.segments)} segment(s))"
    )


def _format_segments(segments: Sequence[core.Segment]) -> str:
    return ", ".join(f"{segment.start:.2f}-{segment.end:.2f}s" for segment in segments)


def _format_duration(seconds: float) -> str:
    return f"{seconds:.2f}s"


def _say(args: argparse.Namespace, message: str) -> None:
    if not args.quiet:
        print(message)


def _validate_args(args: argparse.Namespace) -> list[str]:
    errors: list[str] = []
    if not -120.0 <= args.threshold_db <= 0.0:
        errors.append("--threshold-db must be between -120 and 0")
    if args.min_silence <= 0:
        errors.append("--min-silence must be greater than 0")
    if args.padding < 0:
        errors.append("--padding must not be negative")
    if args.fade_ms < 0:
        errors.append("--fade-ms must not be negative")
    if not 0 <= args.quality <= 9:
        errors.append("--quality must be between 0 and 9")
    return errors


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))
