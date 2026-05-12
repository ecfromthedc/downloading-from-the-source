#!/usr/bin/env python3
"""Minimal intake script for voice-memo-pipeline.

Drop-in substitute for the RT/Alexandria intake script. Takes one or
more audio files on the command line, runs Whisper, and writes a
markdown transcript next to each input. Trashes the source audio
afterward unless --keep is passed.

Usage:
    minimal-intake.py [--model small] [--keep] FILE [FILE ...]

The pipeline calls this as:
    minimal-intake.py --model <whisper-model> <file1> <file2> ...
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path.home() / "voice-memo-transcripts"


def transcribe(path: Path, model: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "whisper", str(path),
            "--model", model,
            "--output_format", "txt",
            "--output_dir", tmp,
            "--language", "en",
            "--fp16", "False",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            print(f"  whisper failed: {result.stderr[:500]}", file=sys.stderr)
            return ""
        txt = Path(tmp) / f"{path.stem}.txt"
        return txt.read_text().strip() if txt.exists() else ""


def write_markdown(audio: Path, transcript: str) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUTPUT_DIR / f"{audio.stem}.md"
    out.write_text(
        f"---\n"
        f"date: {datetime.now().date().isoformat()}\n"
        f"source: {audio.name}\n"
        f"---\n\n"
        f"# {audio.stem}\n\n"
        f"{transcript}\n"
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+")
    p.add_argument("--model", default="small")
    p.add_argument("--keep", action="store_true", help="Don't trash source audio after transcribing")
    args = p.parse_args()

    failures = 0
    for f in args.files:
        path = Path(f)
        if not path.exists():
            print(f"missing: {path}", file=sys.stderr)
            failures += 1
            continue
        print(f"transcribing: {path.name}")
        transcript = transcribe(path, args.model)
        if not transcript:
            failures += 1
            continue
        out = write_markdown(path, transcript)
        print(f"  → {out}")
        if not args.keep:
            subprocess.run(
                ["osascript", "-e", f'tell application "Finder" to delete POSIX file "{path}"'],
                capture_output=True,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
