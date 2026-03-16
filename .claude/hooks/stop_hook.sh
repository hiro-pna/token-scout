#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# TokenScout Hook — Stop: Epistemic Confidence Verification
# ═══════════════════════════════════════════════════════════════════════════════
#
# Purpose:
#   Before Claude stops, verify whether the gathered context is sufficient
#   for a high-quality answer. Uses the termination conditions from §3.3.2:
#     1. Sufficiency:  κ_t ≥ τ
#     2. Inefficiency:  low IGR plateau
#     3. Exhaustion:    budget exceeded
#
#   If confidence is too low AND budget remains, suggest continuing.
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "${SCRIPT_DIR}/stop_hook.py"
