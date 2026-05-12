#!/usr/bin/env bash
# Voice Memos → Whisper → your-intake-script installer.
#
# Generates launchd plists with your $HOME, walks you through the
# (mandatory) Full Disk Access grant, loads both launchd agents,
# and optionally backfills.
#
# Re-runnable. Safe to re-execute after editing the script or plists.

set -euo pipefail

# ------------------------------------------------------------------------- #
# Paths                                                                     #
# ------------------------------------------------------------------------- #

REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PIPELINE="$REPO_DIR/voice-memo-pipeline.py"

PIPELINE_LABEL="com.risingtides.voice-memo-pipeline"
KEEPALIVE_LABEL="com.risingtides.voice-memos-keepalive"
PIPELINE_PLIST="$HOME/Library/LaunchAgents/${PIPELINE_LABEL}.plist"
KEEPALIVE_PLIST="$HOME/Library/LaunchAgents/${KEEPALIVE_LABEL}.plist"

VOICE_MEMOS_APP="/System/Applications/VoiceMemos.app"
VOICE_MEMOS_DIR="$HOME/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"

# ------------------------------------------------------------------------- #
# Pretty-printing                                                           #
# ------------------------------------------------------------------------- #

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n'  "$*"; }

# ------------------------------------------------------------------------- #
# 1. Resolve the REAL Python binary                                         #
# ------------------------------------------------------------------------- #
# /usr/bin/python3 on modern macOS is a stub that re-execs through
# xcode-select. macOS TCC matches against the real exec path, so we must
# grant FDA to the real binary AND have launchd invoke it directly.
green "▶ Voice Memos pipeline installer"
green "  • Resolving the real python3 binary…"

REAL_PY="$(/usr/bin/python3 -c 'import sys; print(sys.executable)')"
if [[ ! -x "$REAL_PY" ]]; then
    red "    ✗ Could not resolve a working python3"
    exit 1
fi
green "    ✓ Real binary: $REAL_PY"

# ------------------------------------------------------------------------- #
# 2. Dependency check                                                        #
# ------------------------------------------------------------------------- #
green "  • Checking dependencies…"
command -v whisper >/dev/null || {
    red "    ✗ whisper not on PATH. Install with: pip install -U openai-whisper"
    exit 1
}
command -v ffmpeg  >/dev/null || {
    red "    ✗ ffmpeg not on PATH. Install with: brew install ffmpeg"
    exit 1
}
[[ -f "$PIPELINE" ]] || { red "    ✗ pipeline missing: $PIPELINE"; exit 1; }
green "    ✓ whisper, ffmpeg, pipeline present"

# ------------------------------------------------------------------------- #
# 3. Wake Voice Memos.app to populate the CloudKit container                #
# ------------------------------------------------------------------------- #
green "  • Waking Voice Memos.app to hydrate the CloudKit cache…"
open -gja "$VOICE_MEMOS_APP"
sleep 5
if [[ -d "$VOICE_MEMOS_DIR" ]]; then
    n="$(ls "$VOICE_MEMOS_DIR"/*.m4a 2>/dev/null | wc -l | tr -d ' ')"
    green "    ✓ Container present (${n} cached .m4a files)"
else
    yellow "    ! Container not yet hydrated. The keepalive agent will keep retrying."
fi

# ------------------------------------------------------------------------- #
# 4. Generate launchd plists with paths from this user's $HOME              #
# ------------------------------------------------------------------------- #
green "  • Generating launchd plists for $HOME…"

cat > "$PIPELINE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${PIPELINE_LABEL}</string>
    <!--
      Default scope for launchd-triggered fires: only act on memos
      recorded in the last 7 days. Keeps the watcher cheap. Backfills
      of older history must be invoked manually with --backfill --since N.
    -->
    <key>ProgramArguments</key>
    <array>
        <string>${REAL_PY}</string>
        <string>${PIPELINE}</string>
        <string>--since</string>
        <string>7</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>${VOICE_MEMOS_DIR}</string>
    </array>
    <key>ThrottleInterval</key><integer>30</integer>
    <key>KeepAlive</key><false/>
    <key>RunAtLoad</key><false/>
    <key>StandardOutPath</key><string>${HOME}/Library/Logs/voice-memo-pipeline.launchd.log</string>
    <key>StandardErrorPath</key><string>${HOME}/Library/Logs/voice-memo-pipeline.launchd.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>LANG</key><string>en_US.UTF-8</string>
    </dict>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
EOF

cat > "$KEEPALIVE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${KEEPALIVE_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-gja</string>
        <string>${VOICE_MEMOS_APP}</string>
    </array>
    <key>StartInterval</key><integer>1200</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>${HOME}/Library/Logs/voice-memos-keepalive.log</string>
    <key>StandardErrorPath</key><string>${HOME}/Library/Logs/voice-memos-keepalive.log</string>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
EOF

plutil -lint "$PIPELINE_PLIST" >/dev/null
plutil -lint "$KEEPALIVE_PLIST" >/dev/null
green "    ✓ Plists written and validated"

# ------------------------------------------------------------------------- #
# 5. Full Disk Access grant                                                  #
# ------------------------------------------------------------------------- #
green "  • Verifying Full Disk Access for ${REAL_PY}…"
if "$REAL_PY" -c "import os; os.listdir('${VOICE_MEMOS_DIR}')" >/dev/null 2>&1; then
    green "    ✓ FDA already granted"
else
    yellow ""
    bold   "    ┌──────────────────────────────────────────────────────────────"
    bold   "    │ MANUAL STEP REQUIRED — Full Disk Access"
    bold   "    └──────────────────────────────────────────────────────────────"
    yellow "    Apple does not allow programmatic FDA grants. You have to click."
    yellow ""
    yellow "    The path is being copied to your clipboard now:"
    bold   "      ${REAL_PY}"
    printf "%s" "${REAL_PY}" | pbcopy
    yellow ""
    yellow "    System Settings is opening to the right pane. In it:"
    yellow "      1. Click the +"
    yellow "      2. Press ⌘⇧G (Go to Folder)"
    yellow "      3. Press ⌘V then Return then Open"
    yellow "      4. Toggle the new entry ON"
    yellow ""
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" || true
    read -r -p "$(yellow '   Press Enter when toggled on… ')" _

    if "$REAL_PY" -c "import os; os.listdir('${VOICE_MEMOS_DIR}')" >/dev/null 2>&1; then
        green "    ✓ FDA grant confirmed"
    else
        red "    ✗ Still cannot read the container. Check the entry path is exactly:"
        red "        ${REAL_PY}"
        red "      and the toggle is ON. Then re-run this installer."
        exit 2
    fi
fi

# ------------------------------------------------------------------------- #
# 6. Load launchd agents                                                     #
# ------------------------------------------------------------------------- #
green "  • Loading launchd agents…"
for label in "$PIPELINE_LABEL" "$KEEPALIVE_LABEL"; do
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
done
launchctl bootstrap "gui/$(id -u)" "$PIPELINE_PLIST"
launchctl enable    "gui/$(id -u)/${PIPELINE_LABEL}"
launchctl bootstrap "gui/$(id -u)" "$KEEPALIVE_PLIST"
launchctl enable    "gui/$(id -u)/${KEEPALIVE_LABEL}"
green "    ✓ Both agents loaded"

# ------------------------------------------------------------------------- #
# 7. Optional backfill                                                       #
# ------------------------------------------------------------------------- #
echo ""
read -r -p "$(yellow 'Backfill the last 90 days of recordings now? (y/N) ')" reply
case "$reply" in
    [yY]|[yY][eE][sS])
        green "  • Backfilling (last 90 days)…"
        "$REAL_PY" "$PIPELINE" --backfill --since 90
        ;;
    *)
        yellow "  • Skipped. Backfill manually anytime:"
        yellow "      $REAL_PY $PIPELINE --backfill --since 90"
        ;;
esac

# ------------------------------------------------------------------------- #
# Done                                                                       #
# ------------------------------------------------------------------------- #
echo ""
green "✓ Installed."
green "  Logs:"
green "    $HOME/Library/Logs/voice-memo-pipeline.log"
green "    $HOME/Library/Logs/voice-memo-pipeline.launchd.log"
green "    $HOME/Library/Logs/voice-memos-keepalive.log"
echo ""
green "  Force-trigger watcher: launchctl kickstart -k gui/$(id -u)/${PIPELINE_LABEL}"
green "  Tail live logs:        tail -f $HOME/Library/Logs/voice-memo-pipeline.log"
green "  Uninstall:             launchctl bootout gui/$(id -u)/${PIPELINE_LABEL}"
green "                         launchctl bootout gui/$(id -u)/${KEEPALIVE_LABEL}"
