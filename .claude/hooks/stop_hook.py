#!/usr/bin/env python3
"""
TokenScout Hook — Stop: Epistemic Confidence Verification

Checks if Claude has gathered enough context before stopping.
Implements the termination policy from §3.3.2.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    should_terminate,
)


# Minimum confidence threshold to allow stopping without warning
MIN_CONFIDENCE_TO_STOP = 30.0
# Minimum tool calls before we consider enforcing confidence
MIN_TOOL_CALLS = 3


def main():
    hook_input = read_hook_input()

    # Prevent infinite loops: if stop_hook_active, let Claude stop
    if hook_input.get("stop_hook_active", False):
        sys.exit(0)

    state = load_state()
    ctx = state["context_state"]

    kappa = ctx["kappa_t"]
    t = ctx["t"]
    budget = ctx["budget"]
    L_t = ctx["L_t"]
    explored = state.get("explored_files", [])

    # ── Decision logic ──

    # If very few tool calls, Claude might just be answering from knowledge
    # Don't interfere with trivial queries
    if t < MIN_TOOL_CALLS:
        audit_log("stop_allowed", {
            "reason": "few_tool_calls",
            "t": t, "kappa": kappa,
        })
        sys.exit(0)

    # Check the standard termination conditions
    terminate_ok, reason = should_terminate(state)

    if terminate_ok:
        # Confidence is sufficient, or we've hit diminishing returns / budget
        audit_log("stop_allowed", {
            "reason": reason,
            "kappa": round(kappa, 2),
            "L_t": L_t,
            "budget": budget,
            "t": t,
            "files_explored": len(explored),
        })
        sys.exit(0)

    # Low confidence but budget remains — suggest continuing
    if kappa < MIN_CONFIDENCE_TO_STOP and budget > 0 and L_t < budget * 0.8:
        remaining = budget - L_t
        unexplored = _find_unexplored_candidates(state)

        suggestion_parts = [
            f"[TokenScout] Confidence is low (κ={kappa:.1f}). ",
            f"Budget remaining: {remaining} lines. ",
            f"Files explored: {len(explored)}. ",
        ]

        if unexplored:
            suggestion_parts.append(
                f"Unexplored high-priority candidates: {', '.join(unexplored[:5])}. "
            )
            suggestion_parts.append(
                "Consider reading these files before finalizing your answer."
            )

        output = {
            "decision": "block",
            "reason": "".join(suggestion_parts),
        }
        print(json.dumps(output))

        audit_log("stop_blocked", {
            "kappa": round(kappa, 2),
            "L_t": L_t,
            "budget": budget,
            "unexplored_candidates": unexplored[:5],
        })
        return

    # Otherwise, allow stopping
    audit_log("stop_allowed", {
        "reason": "default",
        "kappa": round(kappa, 2),
        "L_t": L_t,
        "budget": budget,
        "t": t,
    })
    sys.exit(0)


def _find_unexplored_candidates(state: dict) -> list:
    """Find scouted but not yet fully read files."""
    explored = set(state.get("explored_files", []))
    scouted = state.get("scouted_files", [])
    candidates = state.get("candidates", {})

    # Prioritize candidates with scores, then scouted files
    unexplored = []
    for path, info in sorted(candidates.items(), key=lambda x: -x[1].get("score", 0)):
        if path not in explored:
            unexplored.append(path)

    for path in scouted:
        if path not in explored and path not in unexplored:
            unexplored.append(path)

    return unexplored


if __name__ == "__main__":
    main()
