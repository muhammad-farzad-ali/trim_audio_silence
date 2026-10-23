# trim-audio-silence

Remove silent gaps from MP3 files with `ffmpeg`. Give it a file or a directory;
it writes trimmed copies to a separate output folder and never modifies the
originals.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` available on `PATH`

## Install

```bash
git clone git@github.com:muhammad-farzad-ali/trim_audio_silence.git
cd trim_audio_silence
uv sync
```

Run it from the project with `uv run`, or install the `trim-silence` command:

```bash
uv tool install .
```

## Usage

```bash
trim-silence PATH [options]
```

`PATH` can be a single MP3 or a directory. Directories are searched
recursively for `.mp3`/`.MP3` files, and relative paths are preserved in the
output directory.

By default, output goes to `PATH/trimmed` for a directory and
`<file-parent>/trimmed` for a single file.

```bash
# Trim every MP3 under ./recordings into ./recordings/trimmed
trim-silence ./recordings

# Trim one file into an explicit output directory
trim-silence interview.mp3 -o ./clean

# Preview the cuts without writing anything
trim-silence ./recordings --dry-run

# Aggressive gap removal for noisy recordings
trim-silence ./recordings --threshold-db -45 --min-silence 0.2 --padding 0.03
```

### Options

| Option | Default | Description |
| --- | --- | --- |
| `-o, --output-dir DIR` | `<path>/trimmed` | Where trimmed files are written. |
| `--threshold-db DB` | `-35` | Audio quieter than this is treated as silence. Range `-120` to `0`. |
| `--min-silence SEC` | `0.35` | Only gaps at least this long are cut. |
| `--padding SEC` | `0.05` | Silence kept around each sound so cuts do not clip speech. |
| `--fade-ms MS` | `10` | Fade applied at each segment join to avoid clicks. |
| `--quality N` | `2` | `libmp3lame` VBR quality, `0` (best) to `9` (smallest). |
| `--overwrite` | off | Replace existing files in the output directory. |
| `--dry-run` | off | Show segments and duration changes without writing files. |
| `-q, --quiet` | off | Only print errors. |

### Behavior

- Silent gaps are detected with `ffmpeg`'s `silencedetect` filter.
- The kept audio segments are re-encoded with `libmp3lame` and concatenated,
  with short fades at the joins.
- Leading and trailing silence are removed as well as internal gaps.
- Files with no detected silence are skipped rather than re-encoded.
- Files that are entirely silence are skipped.
- Existing output files are skipped unless `--overwrite` is passed.
- The output directory is excluded from recursive scans, so reruns do not
  process previously trimmed files.

Exit codes: `0` success, `1` no files found or one or more files failed, `2`
invalid arguments or an output path that would overwrite a source file.

## Development

```bash
uv sync
uv run pytest
uvx ruff check .
uvx ruff format --check .
```

Project layout:

```
src/trim_audio_silence/
  cli.py    argument parsing, path traversal, reporting
  core.py   ffmpeg probing, silence detection, segment math, encoding
tests/
  test_segments.py     pure segment/parser tests
  test_integration.py  end-to-end tests with generated MP3s
```
