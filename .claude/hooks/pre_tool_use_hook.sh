#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TokenScout Hook — Phase 2: PreToolUse (Read|Bash|Glob|Grep)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Purpose:
#   Structural Repository Scouting (§3.2.2) — before Claude reads a file,
#   check the scouting map and inject structural metadata so Claude can
#   make better-informed decisions about what to read.
#
#   Also enforces budget constraints (§3.3) — if context budget is exhausted,
#   advise Claude to stop exploring.
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "${SCRIPT_DIR}/pre_tool_use_hook.py"
