#!/usr/bin/env bash
# TokenScout Installer — Adds TokenScout hooks to any .claude project
# Usage: ./install.sh [target_dir]
# If target_dir is omitted, installs in current directory.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/src"
TARGET_DIR="${1:-.}"
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

CLAUDE_DIR="$TARGET_DIR/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
TOKENSCOUT_DIR="$CLAUDE_DIR/.tokenscout"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

# ── Validation ──
if [ ! -d "$SRC_DIR" ]; then
  echo "ERROR: src/ directory not found at $SRC_DIR"
  exit 1
fi

echo "TokenScout Installer"
echo "  Source:  $SRC_DIR"
echo "  Target:  $TARGET_DIR"
echo ""

# ── Create directories ──
mkdir -p "$HOOKS_DIR"
mkdir -p "$HOOKS_DIR/lib"
mkdir -p "$TOKENSCOUT_DIR"

# ── Copy hook files ──
echo "Copying hook files..."
for f in "$SRC_DIR"/tokenscout-hook-*.cjs; do
  [ -f "$f" ] || continue
  cp "$f" "$HOOKS_DIR/$(basename "$f")"
  echo "  + hooks/$(basename "$f")"
done

# ── Copy lib files ──
echo "Copying lib files..."
for f in "$SRC_DIR"/lib/tokenscout-*.cjs; do
  [ -f "$f" ] || continue
  cp "$f" "$HOOKS_DIR/lib/$(basename "$f")"
  echo "  + hooks/lib/$(basename "$f")"
done

# ── Merge settings.json ──
echo "Merging settings.json..."

TOKENSCOUT_HOOKS=$(cat <<'HOOKJSON'
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout-hook-session-start-repo-scan-and-context-inject.cjs\"",
            "timeout": 30,
            "statusMessage": "TokenScout: Building repo structural map..."
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout-hook-user-prompt-query-augment-bm25-candidate-rank.cjs\"",
            "timeout": 30,
            "statusMessage": "TokenScout: Analyzing query & ranking candidates..."
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout-hook-pre-tool-use-scout-metadata-and-budget-gate.cjs\"",
            "timeout": 5,
            "statusMessage": "TokenScout: Scouting file metadata..."
          }
        ]
      },
      {
        "matcher": "Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout-hook-pre-tool-use-scout-metadata-and-budget-gate.cjs\"",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Read|Grep|Glob|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout-hook-post-tool-use-context-consumption-tracker-and-confidence-update.cjs\"",
            "timeout": 5,
            "statusMessage": "TokenScout: Updating context state..."
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout-hook-stop-epistemic-confidence-check-and-block-if-low.cjs\"",
            "timeout": 5,
            "statusMessage": "TokenScout: Checking confidence..."
          }
        ]
      }
    ]
  }
}
HOOKJSON
)

TOKENSCOUT_JSON_TMP="$(mktemp)"
echo "$TOKENSCOUT_HOOKS" > "$TOKENSCOUT_JSON_TMP"

if [ -f "$SETTINGS_FILE" ]; then
  # Merge: append TokenScout hook entries to existing event arrays
  MERGE_SCRIPT=$(cat <<'MERGEJS'
const fs = require('fs');
const settingsPath = process.argv[2];
const tokenscoutPath = process.argv[3];
const existing = JSON.parse(fs.readFileSync(settingsPath, 'utf-8'));
const tokenscout = JSON.parse(fs.readFileSync(tokenscoutPath, 'utf-8'));

if (!existing.hooks) existing.hooks = {};

for (const [event, entries] of Object.entries(tokenscout.hooks)) {
  if (!existing.hooks[event]) {
    existing.hooks[event] = entries;
  } else {
    const hasTokenScout = existing.hooks[event].some(e =>
      e.hooks && e.hooks.some(h => h.command && h.command.includes('tokenscout-'))
    );
    if (!hasTokenScout) {
      existing.hooks[event].push(...entries);
    }
  }
}

fs.writeFileSync(settingsPath, JSON.stringify(existing, null, 2) + '\n');
MERGEJS
)
  echo "$MERGE_SCRIPT" | node - "$SETTINGS_FILE" "$TOKENSCOUT_JSON_TMP"
  echo "  Merged into existing settings.json"
else
  cp "$TOKENSCOUT_JSON_TMP" "$SETTINGS_FILE"
  echo "  Created new settings.json"
fi

rm -f "$TOKENSCOUT_JSON_TMP"

echo ""
echo "TokenScout installed successfully!"
echo "  Hooks:    $HOOKS_DIR/tokenscout-*.cjs"
echo "  State:    $TOKENSCOUT_DIR/"
echo "  Settings: $SETTINGS_FILE"
echo ""
echo "To uninstall: ./uninstall.sh $TARGET_DIR"
