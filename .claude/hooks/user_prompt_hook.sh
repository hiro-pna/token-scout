#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TokenScout Hook — UserPromptSubmit: Query Augmentation (§3.2.1)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Purpose:
#   Analyze the user's query to set D_q (query complexity),
#   compute the dynamic budget, and identify initial candidate files
#   from the structural map.
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "${SCRIPT_DIR}/user_prompt_hook.py"
