#!/usr/bin/env python3
"""Classify an Alexandria markdown entry by primary context.

Runs a local Ollama model against the entry's content and writes
`context:`, `team_share:`, and (if missing) `applies_to:` into the
frontmatter. Result governs downstream routing:

    context: rising-tides → team-publisher publishes, Slack posts to #library-of-alexandria
    context: mon-rovia    → BOTH downstream tools silently skip
    context: personal     → BOTH downstream tools silently skip

Idempotent: if `context:` is already present, exits without re-classifying
unless --force is passed.

Usage:
    classify-alexandria-entry.py <path/to/entry.md>
    classify-alexandria-entry.py --force <path/to/entry.md>
    classify-alexandria-entry.py --model gemma3 <path/to/entry.md>
    classify-alexandria-entry.py --dry-run <path/to/entry.md>

Requires:
    ollama (https://ollama.com) with at least one model pulled.
    Defaults to llama3.1:8b.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

Context = Literal["rising-tides", "mon-rovia", "personal"]
VALID_CONTEXTS: tuple[Context, ...] = ("rising-tides", "mon-rovia", "personal")

DEFAULT_MODEL = "llama3.1:8b"
OLLAMA_TIMEOUT_SEC = 90
MAX_CONTENT_CHARS = 12000  # plenty of signal in the first ~12KB

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

PROMPT = """You are classifying a note for a music-industry founder named Eric Cromartie.
He has three distinct life contexts and does NOT want them blended together.

1. rising-tides — His marketing/social-media agency, Rising Tides. Topics include:
   client campaigns (Warner, indie labels, other artists who are NOT Mon Rovia),
   creator/influencer outreach, owned social pages, team workflows, sales,
   pricing, hiring, agency revenue, internal tooling/automation, AI/LLM/n8n
   workflows built for agency operations, podcast-client growth vector,
   RT Viral course, Library of Alexandria intake, founder operations.

2. mon-rovia — Eric's artist project Mon Rovia (Afro-Appalachian folk musician).
   Topics include: song lyrics, melodies, song ideas, recording sessions,
   release strategy for Mon Rovia specifically, fan community, tour, brand
   voice for the artist, collaborators on the artist side. Lyrical or
   spiritual-sounding voice memos are almost always this.

3. personal — Eric himself, not work. Topics include: health, fitness, sleep,
   diet, finance, investments, forecasting, family, friends, dating,
   philosophy, journaling, random side ideas unrelated to RT or Mon Rovia.

Rules:
- Pick ONE primary context. If a note mentions multiple, pick what it's most
  fundamentally about — what would Eric file it under in his own head?
- A note about agency tooling that happens to mention Mon Rovia as a use case
  is still "rising-tides".
- A voice memo of song lyrics, even short, is "mon-rovia".
- A voice memo about diet/health/finance is "personal", even if Eric is
  thinking about it for business reasons.
- When genuinely uncertain, prefer "personal" (the safer default — keeps it
  out of team channels).

Respond with EXACTLY one word from this list, no punctuation, no explanation:
rising-tides
mon-rovia
personal

NOTE CONTENT:
<<<
{content}
>>>
"""

# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_yaml, body_without_frontmatter). FM may be None."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None, text
    return m.group(1), text[m.end():]


def merge_into_frontmatter(fm: str | None, updates: dict[str, str]) -> str:
    """Update or insert keys in a YAML frontmatter block. Preserves order."""
    if fm is None:
        fm = ""
    lines = fm.splitlines()
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", ln)
        if m and m.group(1) in updates:
            key = m.group(1)
            out.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            out.append(ln)
    for key, val in updates.items():
        if key not in seen:
            out.append(f"{key}: {val}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def classify_via_ollama(content: str, model: str) -> Context:
    snippet = content[:MAX_CONTENT_CHARS]
    prompt = PROMPT.format(content=snippet)
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            timeout=OLLAMA_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(f"⚠ ollama timed out; defaulting to personal", file=sys.stderr)
        return "personal"
    except FileNotFoundError:
        print(f"⚠ ollama not on PATH; defaulting to personal", file=sys.stderr)
        return "personal"

    raw = (result.stdout or "").strip().lower()
    if result.returncode != 0:
        print(f"⚠ ollama exit {result.returncode}: {result.stderr[:200]}", file=sys.stderr)
        return "personal"

    # Look for any of the three keywords anywhere in the response — models
    # sometimes preface with thinking text even when told not to.
    for candidate in VALID_CONTEXTS:
        if candidate in raw:
            return candidate
    print(f"⚠ unparseable ollama response: {raw[:120]!r}; defaulting to personal", file=sys.stderr)
    return "personal"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify an Alexandria entry by context")
    parser.add_argument("entry", type=Path, help="Path to the .md file to classify")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    parser.add_argument("--force", action="store_true", help="Re-classify even if context already set")
    parser.add_argument("--dry-run", action="store_true", help="Print classification without writing")
    args = parser.parse_args()

    if not args.entry.exists():
        print(f"✗ entry not found: {args.entry}", file=sys.stderr)
        return 1

    text = args.entry.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)

    if fm and re.search(r"^context:\s*\S", fm, re.MULTILINE) and not args.force:
        existing = re.search(r"^context:\s*(\S+)", fm, re.MULTILINE).group(1)
        print(f"  context already set: {existing} (use --force to re-classify)")
        return 0

    context = classify_via_ollama(body, args.model)
    team_share = "true" if context == "rising-tides" else "false"
    applies_to = "rising-tides-agency" if context == "rising-tides" else context

    print(f"  → context: {context} | team_share: {team_share}")

    if args.dry_run:
        return 0

    new_fm = merge_into_frontmatter(
        fm,
        {
            "context": context,
            "team_share": team_share,
            "applies_to": applies_to,
        },
    )
    new_text = f"---\n{new_fm}\n---\n{body}"
    args.entry.write_text(new_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
