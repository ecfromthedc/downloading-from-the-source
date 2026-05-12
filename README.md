# Downloading From The Source

> Auto-transcribe iCloud Voice Memos and pipe them into your knowledge system — directly from Apple's CloudKit container, no AirDrop, no manual export.

A macOS launchd-driven pipeline that watches Apple's Voice Memos CloudKit cache, transcribes new recordings with [Whisper](https://github.com/openai/whisper), and routes the resulting markdown into a downstream "intake" script of your choice (originally built to feed the [Library of Alexandria](https://en.wikipedia.org/wiki/Library_of_Alexandria) note system in an Obsidian vault).

The interesting part isn't the transcription — Whisper's well-trodden ground. The interesting part is **getting at the recordings in the first place**, because Apple stores them in a TCC-protected CloudKit container with a lazy-sync model. Most of this README is the architectural archaeology required to make a launchd watcher actually work against that container.

---

## What it does

```
iPhone records voice memo
        │
        ▼  (CloudKit)
Mac's Voice Memos.app maintains local cache:
   ~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/
        │
        ▼  (launchd WatchPaths fires on directory change)
voice-memo-pipeline.py
   ├─ reads CloudRecordings.db for the user-friendly title
   ├─ filters on --since DAYS so backfill doesn't drown your inbox
   ├─ copies new .m4a → "audio drop" folder under "<date> - <title> [<uuid>].m4a"
   ├─ invokes your intake script (whisper transcribe → markdown → trash copy)
   └─ records UUID in ~/.voice-memo-pipeline.state.json so it never re-runs
        │
        ▼
Markdown transcript lands in your knowledge system.
The original recording in iCloud is never touched.
```

A second launchd agent (`voice-memos-keepalive`) silently re-launches `Voice Memos.app` every 20 minutes in the background, hidden — without it, CloudKit eventually drops the subscription and new iPhone recordings stop syncing down to the Mac.

---

## The four hard problems, in order

If you're forking this for a different downstream, these are the gotchas that ate hours.

### 1. Voice Memos lives in CloudKit, not the filesystem.

Out of the box, the Voice Memos app on Mac doesn't materialize anything on disk. The container at `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/` will sit at 4KB (just a metadata plist) — recordings exist only in CloudKit's private database. You can see them in the Voice Memos UI but there's nothing for a watcher to watch.

**Fix:** opening Voice Memos.app *triggers* the CloudKit hydration — within ~15 seconds the container fills with `<YYYYMMDD HHMMSS>-<8charhash>.m4a` files plus `CloudRecordings.db`. From then on `cloudd` keeps the local cache in sync... but only while a CloudKit subscriber is alive in your user session.

That subscriber is Voice Memos.app. **If it's closed for too long, sync stops** and new iPhone recordings never reach the local container. Hence the keep-awake agent — every 20 min it does `open -gja /System/Applications/VoiceMemos.app`, which silently relaunches the app with no UI.

### 2. The Voice Memos container is TCC-protected.

`~/Library/Group Containers/...` falls under Full Disk Access. Without FDA, `ls` on the directory returns `Operation not permitted`. Even though your shell has FDA (because Terminal does), launchd-spawned processes inherit nothing — they need their own grant.

**Fix:** grant FDA to the binary launchd actually exec's. See the next gotcha for what binary that actually is.

### 3. `/usr/bin/python3` is a stub. TCC checks the real binary.

You'd think granting FDA to `/usr/bin/python3` would suffice. It does not. On modern macOS, `/usr/bin/python3` is a stub that re-execs through xcode-select to the underlying Command Line Tools Python. macOS TCC checks the *real* exec path, not the stub. Your `/usr/bin/python3` grant gets ignored at runtime.

```
$ /usr/bin/python3 -c "import sys; print(sys.executable)"
/Library/Developer/CommandLineTools/usr/bin/python3
```

**Fix:** grant FDA to `/Library/Developer/CommandLineTools/usr/bin/python3` (the actual binary), and have the launchd plist invoke it directly — don't go through the stub.

The `install.sh` in this repo does both automatically: it generates the plist with the real binary path, and walks you through the FDA grant for that exact path.

### 4. CloudKit hydrates lazily.

The local cache shows recordings up to whenever Voice Memos.app last actively synced. New ones from iPhone arrive in batches. The DB will list more recordings than are on disk — those `.m4a` files materialize as Voice Memos.app pulls them down. Don't be alarmed if the file count lags the DB count.

The keep-awake agent (above) keeps the gap small. If you record on iPhone and don't see a transcript within ~30 minutes, force-trigger the watcher: `launchctl kickstart -k gui/$(id -u)/com.risingtides.voice-memo-pipeline`.

---

## Install

```bash
git clone https://github.com/ecfromthedc/downloading-from-the-source.git
cd downloading-from-the-source
./install.sh
```

The installer:
1. Verifies dependencies (`whisper`, `ffmpeg`, `python3`).
2. Resolves the **real** Python binary path (handles the xcode-select stub).
3. Wakes Voice Memos.app once so the CloudKit container hydrates.
4. Generates the launchd plists with paths from your environment.
5. Walks you through granting FDA to the right binary (puts the path on your clipboard, opens the right System Settings pane).
6. Loads both launchd agents.
7. Optionally backfills the last N days of recordings.

**One-time manual step the installer cannot automate:** clicking through System Settings → Privacy & Security → Full Disk Access to add the Python binary. Apple intentionally blocks programmatic FDA grants — there is no CLI to bypass this. The installer makes the click-through as fast as possible (path pre-loaded in clipboard).

---

## Context classification (optional but recommended)

If you want each transcript auto-tagged with which life-context it belongs to
— so downstream tools (team-shared repos, Slack channels) can silently skip
the entries that should stay private — drop `classify-alexandria-entry.py`
next to the pipeline and the pipeline will run it automatically on every new
transcript.

The classifier uses local [Ollama](https://ollama.com) (default `llama3.1:8b`)
to assign `context: rising-tides | mon-rovia | personal` to each entry, plus
`team_share: true|false`. Only `rising-tides` is team-shared by default —
everything else is treated as private to you.

Downstream tools (e.g., `alexandria-team-publisher.py`,
`alexandria-slack-notifier.py`) check the `context:` and `team_share:` flags
and silently no-op for non-RT entries. No "skipped" notification leaks the
existence of personal content into shared channels.

You can edit the categories and rules in `classify-alexandria-entry.py` to
match your own contexts. The shipped version is tuned for the original RT
use case (agency / artist project / personal).

## Configure

By default the pipeline calls the [Library of Alexandria audio intake script](https://github.com/ecfromthedc) at `~/Projects/active/rt-agents/alexandria-audio-intake.py`. That script does the actual Whisper transcribe, generates a templated markdown entry, and trashes the staged `.m4a` copy.

If you don't have that script (likely — it's RT-internal), point at your own intake script via env var:

```bash
export VOICE_MEMO_INTAKE_SCRIPT=/path/to/your-intake-script.py
```

Or edit `INTAKE_SCRIPT` near the top of `voice-memo-pipeline.py`. The pipeline calls it as:

```
$INTAKE_SCRIPT --model <whisper-model> <audio-file-1> <audio-file-2> ...
```

A minimal substitute is ~50 lines of Python — see `examples/minimal-intake.py` for a barebones version that just runs Whisper and writes a `.md` file beside each `.m4a`.

---

## Operate

```bash
# Manual run (process new memos)
/Library/Developer/CommandLineTools/usr/bin/python3 voice-memo-pipeline.py

# Backfill last 90 days
voice-memo-pipeline.py --backfill --since 90

# Re-process EVERYTHING (no state filter, no date filter)
voice-memo-pipeline.py --backfill

# Just list what would run
voice-memo-pipeline.py --backfill --since 30 --dry-run

# Force the launchd watcher to fire now
launchctl kickstart -k gui/$(id -u)/com.risingtides.voice-memo-pipeline

# Tail live logs
tail -f ~/Library/Logs/voice-memo-pipeline.log

# Uninstall both agents (keeps script + state)
launchctl bootout gui/$(id -u)/com.risingtides.voice-memo-pipeline
launchctl bootout gui/$(id -u)/com.risingtides.voice-memos-keepalive
```

---

## Files in this repo

| Path | Role |
|------|------|
| `voice-memo-pipeline.py` | The pipeline. Discovery, staging, intake handoff, state tracking. |
| `install.sh` | Portable installer — handles FDA walkthrough, plist generation, agent loading. |
| `launchd/com.risingtides.voice-memo-pipeline.plist` | Reference plist for the watcher. `install.sh` regenerates this for your `$HOME`. |
| `launchd/com.risingtides.voice-memos-keepalive.plist` | Reference plist for the 20-min Voice Memos waker. |
| `examples/minimal-intake.py` | Barebones intake script if you don't want to use the RT/Alexandria one. |

---

## State + logs

- `~/.voice-memo-pipeline.state.json` — UUID ledger. Delete this to force re-processing of every memo.
- `~/Library/Logs/voice-memo-pipeline.log` — pipeline run log.
- `~/Library/Logs/voice-memo-pipeline.launchd.log` — launchd stdout/stderr.
- `~/Library/Logs/voice-memos-keepalive.log` — keepalive log.

---

## Tuning

- **Default scope of the launchd watcher** — `--since 7` (last week only). The watcher is intentionally narrow so a CloudKit re-shuffle of the Recordings directory doesn't trigger transcription of every historical memo. Backfills of older history are explicit: `voice-memo-pipeline.py --backfill --since 90`. Edit the `--since` value in the plist (or in `install.sh`) if you want a wider default.
- **Whisper model** — defaults to `small` (good balance, ~2-5x realtime on Apple Silicon). Use `--model medium` or `--model large` for tougher audio, `--model tiny` for speed.
- **`QUIESCENCE_SECONDS`** in the script (default 8) — how long a file must be untouched before the pipeline considers it "done writing". Bump if you see partial transcripts.
- **`ThrottleInterval`** in the watcher plist (default 30s) — how long launchd waits before re-firing on rapid filesystem events.
- **Keepalive cadence** — `StartInterval` in the keepalive plist (default 1200s = 20 min). Bump higher if Voice Memos.app waking is too chatty; lower if iPhone recordings still take too long to sync down.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Recordings dir not found` in log | Voice Memos.app never opened on this Mac | Open it manually once, or wait 20 min for the keepalive agent. |
| `Operation not permitted` on container files | FDA missing on the real Python binary | Grant FDA to `/Library/Developer/CommandLineTools/usr/bin/python3` — see install.sh. |
| Watcher loaded but never fires | Path in `WatchPaths` doesn't match container path | Check the plist matches your `$HOME`. |
| New iPhone memos don't appear within 30 min | CloudKit subscription dropped | Confirm `voice-memos-keepalive` is loaded: `launchctl list \| grep keepalive`. |
| Same memo processed twice | State file deleted | Safe — intake script de-duplicates output filenames with a counter. |
| Empty / garbled transcript | Whisper struggling with audio quality | Try `--model medium`. Confirm the source `.m4a` plays in QuickTime. |

---

## Why "Downloading From The Source"

Most "transcribe my voice memos" tutorials punt to AirDrop, share-sheet exports, or third-party recorder apps. This one talks to Apple's actual storage — the CloudKit container that backs the Voice Memos UI itself. No middlemen, no manual exports, no losing the high-fidelity originals. The recordings stay in iCloud where they always were. The transcripts flow into your knowledge system automatically.

That's the source.

---

## License

MIT — see [LICENSE](./LICENSE).
