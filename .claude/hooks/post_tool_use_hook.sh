#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TokenScout Hook — Phase 3: PostToolUse (Read|Bash|Grep|Glob)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Purpose:
#   Cost-Aware Context Management (§3.3):
#     - Track cumulative context consumption L_t
#     - Update epistemic confidence κ_t
#     - Compute Information Gain Rate (IGR)
#     - Update the state vector S_t
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "${SCRIPT_DIR}/post_tool_use_hook.py"
