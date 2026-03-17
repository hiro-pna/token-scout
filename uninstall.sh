#!/usr/bin/env bash
# TokenScout Uninstaller — Removes TokenScout hooks from a .claude project
# Usage: ./uninstall.sh [target_dir]
# If target_dir is omitted, uninstalls from current directory.

set -euo pipefail

TARGET_DIR="${1:-.}"
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

CLAUDE_DIR="$TARGET_DIR/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
TOKENSCOUT_DIR="$CLAUDE_DIR/.tokenscout"
SETTINGS_FILE="$CLAUDE_DIR/settings.json"

echo "TokenScout Uninstaller"
echo "  Target: $TARGET_DIR"
echo ""

# ── Remove hook files ──
echo "Removing hook files..."
for f in "$HOOKS_DIR"/tokenscout-*.cjs 2>/dev/null; do
  [ -f "$f" ] || continue
  rm "$f"
  echo "  - hooks/$(basename "$f")"
done

# ── Remove lib files ──
echo "Removing lib files..."
for f in "$HOOKS_DIR"/lib/tokenscout-*.cjs 2>/dev/null; do
  [ -f "$f" ] || continue
  rm "$f"
  echo "  - hooks/lib/$(basename "$f")"
done

# ── Remove state directory ──
if [ -d "$TOKENSCOUT_DIR" ]; then
  rm -rf "$TOKENSCOUT_DIR"
  echo "  - .tokenscout/ state directory"
fi

# ── Clean settings.json ──
if [ -f "$SETTINGS_FILE" ]; then
  echo "Cleaning settings.json..."
  node -e "
    const fs = require('fs');
    const settings = JSON.parse(fs.readFileSync('$SETTINGS_FILE', 'utf-8'));

    if (settings.hooks) {
      for (const [event, entries] of Object.entries(settings.hooks)) {
        settings.hooks[event] = entries.filter(e =>
          !(e.hooks && e.hooks.some(h => h.command && h.command.includes('tokenscout-')))
        );
        // Remove empty event arrays
        if (settings.hooks[event].length === 0) {
          delete settings.hooks[event];
        }
      }
      // Remove empty hooks object
      if (Object.keys(settings.hooks).length === 0) {
        delete settings.hooks;
      }
    }

    fs.writeFileSync('$SETTINGS_FILE', JSON.stringify(settings, null, 2) + '\n');
  "
  echo "  Removed TokenScout entries from settings.json"
fi

echo ""
echo "TokenScout uninstalled successfully."
echo "All other hooks and settings remain untouched."
