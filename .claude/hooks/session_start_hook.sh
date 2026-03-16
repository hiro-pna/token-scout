#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TokenScout Hook — Phase 1: SessionStart
# ═══════════════════════════════════════════════════════════════════════════════
#
# Fires on: SessionStart (startup, resume, compact)
#
# Purpose:
#   Build the lightweight semantic-structural map of the repository.
#   Corresponds to TokenScout Phase 1 (§3.1):
#     - Hierarchical Code Units (file/class/function metadata)
#     - Symbol-Aware Relation Modeling (import dependencies)
#     - Multi-Grained Hybrid Indexing (signature-level index)
#
# Output:
#   Prints context reminder to stdout (added to Claude's context).
#   Saves repo map to .claude/hooks/tokenscout_state.json
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "${SCRIPT_DIR}/session_start_hook.py"
