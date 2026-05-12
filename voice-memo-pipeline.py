#!/usr/bin/env python3
"""Voice Memos → Alexandria pipeline.

Watches the macOS Voice Memos CloudKit-synced container for new
recordings, then transcribes each one via Whisper and ingests the
result into the Library of Alexandria.

The Voice Memos.app must have been launched at least once on this Mac
so the CloudKit sync engine populates
``~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/``.
After that the cloudd daemon keeps it in sync in the background.

Originals are NEVER touched — we copy each recording into the
Alexandria Audio Drop, run ``alexandria-audio-intake.py`` on the COPY
(which transcribes and trashes the copy), and record the UUID stem in
``~/.voice-memo-pipeline.state.json`` so we never re-process the same
memo.

Usage:
    voice-memo-pipeline.py                 # process new memos
    voice-memo-pipeline.py --dry-run       # preview
    voice-memo-pipeline.py --backfill      # process every memo
    voice-memo-pipeline.py --since 30      # only memos from last N days
    voice-memo-pipeline.py --model medium  # whisper model override
    voice-memo-pipeline.py --no-neo4j      # skip Neo4j sync

Requires: openai-whisper, ffmpeg, and Full Disk Access for
``/usr/bin/python3`` (or whatever python launchd invokes) so that the
Voice Memos container is readable from a launchd-spawned process.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HOME = Path.home()

VOICE_MEMOS_CONTAINER = (
    HOME / "Library" / "Group Containers" / "group.com.apple.VoiceMemos.shared"
)
RECORDINGS_DIR = VOICE_MEMOS_CONTAINER / "Recordings"
CLOUD_DB = RECORDINGS_DIR / "CloudRecordings.db"

ALEXANDRIA_DROP = (
    HOME
    / "Documents"
    / "Obsidian Vault"
    / "Alexandria"
    / "Inspiration Queue"
    / "Audio Drop"
)

INTAKE_SCRIPT = Path(
    os.environ.get(
        "VOICE_MEMO_INTAKE_SCRIPT",
        str(HOME / "Projects" / "active" / "rt-agents" / "alexandria-audio-intake.py"),
    )
)
NEO4J_SCRIPT = HOME / "Projects" / "active" / "rt-agents" / "neo4j-alexandria.py"
NEO4J_VENV_PYTHON = HOME / "Projects" / "active" / "rt-agents" / ".venv" / "bin" / "python3"

STATE_FILE = HOME / ".voice-memo-pipeline.state.json"
LOG_FILE = HOME / "Library" / "Logs" / "voice-memo-pipeline.log"
LOCK_FILE = Path("/tmp/voice-memo-pipeline.lock")

# A file must be untouched for this many seconds before we'll process it.
QUIESCENCE_SECONDS = 8

# Stale lock older than this is reclaimed (assumes the prior run died).
STALE_LOCK_SECONDS = 30 * 60

# Cocoa Core Data epoch — what Voice Memos uses in ZCLOUDRECORDING.ZDATE.
COCOA_EPOCH_OFFSET = 978307200  # 2001-01-01 00:00:00 UTC vs Unix epoch

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("voice-memo-pipeline")


# ---------------------------------------------------------------------------
# State + lock
# ---------------------------------------------------------------------------


def load_state() -> dict[str, dict]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("State file corrupt; starting fresh")
    return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def acquire_lock() -> bool:
    if LOCK_FILE.exists():
        try:
            age = time.time() - LOCK_FILE.stat().st_mtime
        except OSError:
            age = 0
        if age < STALE_LOCK_SECONDS:
            log.info("Another run is in progress (lock age %.0fs); exiting.", age)
            return False
        log.warning("Stale lock found (%.0fs old); reclaiming.", age)
    try:
        LOCK_FILE.write_text(str(os.getpid()))
    except OSError as exc:
        log.error("Could not write lock: %s", exc)
        return False
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Voice Memos discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceMemo:
    uuid: str            # filename stem (e.g. "20180624 003400-46C6EC12")
    path: Path           # absolute .m4a path
    title: str           # custom label or filename fallback
    recorded_at: float   # epoch seconds
    duration: float      # seconds


def lookup_titles_from_db() -> dict[str, tuple[str, float, float]]:
    """Return {uuid_stem: (title, duration_sec, recorded_unix_epoch)}.

    Voice Memos uses WAL — copy the DB to a temp file and open read-only
    so we don't fight the live writer. ZDATE is Cocoa epoch seconds.
    """
    if not CLOUD_DB.exists():
        log.debug("CloudRecordings.db not found at %s", CLOUD_DB)
        return {}

    import tempfile

    titles: dict[str, tuple[str, float, float]] = {}
    tmp_path = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

    try:
        shutil.copy2(CLOUD_DB, tmp_path)
        for sidecar_suffix in ("-wal", "-shm"):
            src = CLOUD_DB.with_name(CLOUD_DB.name + sidecar_suffix)
            if src.exists():
                shutil.copy2(src, tmp_path.with_name(tmp_path.name + sidecar_suffix))

        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            cur = conn.execute(
                """
                SELECT ZPATH, ZCUSTOMLABEL, ZENCRYPTEDTITLE, ZDURATION, ZDATE
                FROM ZCLOUDRECORDING
                """
            )
            for zpath, label, enc_title, duration, zdate in cur:
                if not zpath:
                    continue
                stem = Path(zpath).stem
                title = (label or enc_title or stem).strip()
                duration = float(duration or 0)
                recorded = float(zdate or 0) + COCOA_EPOCH_OFFSET if zdate else 0.0
                titles[stem] = (title, duration, recorded)
        except sqlite3.Error as exc:
            log.warning("Could not read ZCLOUDRECORDING: %s", exc)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        log.warning("Could not copy/open CloudRecordings.db: %s", exc)
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(tmp_path) + suffix).unlink(missing_ok=True)
            except OSError:
                pass

    return titles


def slugify_for_filename(title: str) -> str:
    title = title.strip() or "Untitled Memo"
    safe = re.sub(r"[^\w\s\-]", "", title, flags=re.UNICODE)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:120] or "Untitled Memo"


def discover_memos(
    state: dict[str, dict],
    backfill: bool,
    since_days: int | None,
) -> list[VoiceMemo]:
    if not RECORDINGS_DIR.exists():
        log.error(
            "Recordings dir not found: %s\n"
            "Likely cause: launchd-spawned python lacks Full Disk Access, "
            "OR Voice Memos.app has not been launched on this Mac yet so "
            "CloudKit hasn't populated the container.",
            RECORDINGS_DIR,
        )
        return []

    titles = lookup_titles_from_db()
    now = time.time()
    cutoff = now - since_days * 86400 if since_days else None
    found: list[VoiceMemo] = []

    for path in RECORDINGS_DIR.glob("*.m4a"):
        uuid = path.stem
        if not backfill and uuid in state:
            continue

        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            log.warning("Cannot stat %s: %s", path, exc)
            continue

        if now - mtime < QUIESCENCE_SECONDS:
            log.info("Skipping %s — modified %.1fs ago, still settling", uuid, now - mtime)
            continue

        title, duration, recorded = titles.get(uuid, (uuid, 0.0, mtime))
        recorded = recorded or mtime  # fall back to mtime if DB is silent
        if cutoff is not None and recorded < cutoff:
            continue

        found.append(
            VoiceMemo(
                uuid=uuid,
                path=path,
                title=title,
                recorded_at=recorded,
                duration=duration,
            )
        )

    found.sort(key=lambda m: m.recorded_at)
    return found


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def stage_to_drop(memo: VoiceMemo) -> Path:
    ALEXANDRIA_DROP.mkdir(parents=True, exist_ok=True)
    safe_title = slugify_for_filename(memo.title)
    date_prefix = time.strftime("%Y-%m-%d", time.localtime(memo.recorded_at))
    short_uuid = memo.uuid.split("-")[-1][:8] if "-" in memo.uuid else memo.uuid[:8]
    target = ALEXANDRIA_DROP / f"{date_prefix} - {safe_title} [{short_uuid}].m4a"

    counter = 2
    while target.exists():
        target = ALEXANDRIA_DROP / f"{date_prefix} - {safe_title} [{short_uuid}] ({counter}).m4a"
        counter += 1

    shutil.copy2(memo.path, target)
    return target


def run_intake(staged_files: list[Path], model: str) -> int:
    if not staged_files:
        return 0
    cmd = [sys.executable, str(INTAKE_SCRIPT), "--model", model, *(str(f) for f in staged_files)]
    log.info("Running intake on %d file(s) with model=%s", len(staged_files), model)
    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in result.stdout.splitlines():
        log.info("  intake | %s", line)
    for line in result.stderr.splitlines():
        log.warning("  intake! | %s", line)
    return result.returncode


def neo4j_sync() -> None:
    if not NEO4J_SCRIPT.exists():
        return
    py = str(NEO4J_VENV_PYTHON) if NEO4J_VENV_PYTHON.exists() else sys.executable
    for action in ("ingest", "embed"):
        log.info("Neo4j %s …", action)
        try:
            result = subprocess.run(
                [py, str(NEO4J_SCRIPT), action],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            log.warning("Neo4j %s timed out", action)
            continue
        if result.returncode != 0:
            log.warning("Neo4j %s failed: %s", action, result.stderr[:400])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice Memos → Alexandria pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--backfill", action="store_true", help="Reprocess every memo, ignoring state")
    parser.add_argument(
        "--since",
        type=int,
        metavar="DAYS",
        help="Only consider memos recorded within the last N days",
    )
    parser.add_argument(
        "--model",
        default="small",
        help="Whisper model: tiny | base | small | medium | large (default: small)",
    )
    parser.add_argument("--no-neo4j", action="store_true", help="Skip Neo4j sync")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info(
        "voice-memo-pipeline starting (dry-run=%s, backfill=%s, since=%s, model=%s)",
        args.dry_run, args.backfill, args.since, args.model,
    )

    if not INTAKE_SCRIPT.exists():
        log.error("Intake script missing: %s", INTAKE_SCRIPT)
        return 2

    if not args.dry_run and not acquire_lock():
        return 0

    try:
        state = load_state()
        memos = discover_memos(state, backfill=args.backfill, since_days=args.since)

        if not memos:
            log.info("No new voice memos to process.")
            return 0

        log.info("Discovered %d memo(s):", len(memos))
        total_minutes = sum(m.duration for m in memos) / 60.0
        for m in memos:
            log.info(
                "  • %s  [%s, %.0fs]",
                m.title,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(m.recorded_at)),
                m.duration,
            )
        log.info("Total audio: %.1f min", total_minutes)

        if args.dry_run:
            return 0

        staged: list[tuple[VoiceMemo, Path]] = []
        for memo in memos:
            try:
                target = stage_to_drop(memo)
                staged.append((memo, target))
                log.info("Staged %s → %s", memo.uuid, target.name)
            except OSError as exc:
                log.error("Failed to stage %s: %s", memo.uuid, exc)

        if not staged:
            log.warning("Nothing successfully staged.")
            return 1

        rc = run_intake([t for _, t in staged], model=args.model)
        if rc != 0:
            log.error("Intake exited %s — leaving state untouched so files retry next run", rc)
            return rc

        for memo, target in staged:
            state[memo.uuid] = {
                "title": memo.title,
                "recorded_at": memo.recorded_at,
                "processed_at": time.time(),
                "staged_as": target.name,
            }
        save_state(state)
        log.info("State updated with %d new entr(ies)", len(staged))

        if not args.no_neo4j:
            try:
                neo4j_sync()
            except Exception as exc:  # noqa: BLE001
                log.warning("Neo4j sync skipped: %s", exc)

        log.info("voice-memo-pipeline done.")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
