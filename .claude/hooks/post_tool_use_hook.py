#!/usr/bin/env python3
"""
TokenScout Hook — Phase 3: PostToolUse
Tracks context consumption and updates the cost-aware state vector.

Implements §3.3 Cost-Aware Context Management:
  - S_t = {D_q, H_r, L_t, t, κ_t}
  - Information Gain Rate (IGR_t)
  - Dynamic budget enforcement
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    information_gain_rate, should_terminate, compute_budget,
)


def main():
    hook_input = read_hook_input()
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response", {})

    state = load_state()
    ctx = state["context_state"]

    # ── Update iteration depth t ──
    ctx["t"] += 1

    # ── Estimate context consumed by this tool call ──
    lines_consumed = _estimate_lines_consumed(tool_name, tool_input, tool_response)

    # ── Update L_t (cumulative context) ──
    L_prev = ctx["L_t"]
    kappa_prev = ctx["kappa_t"]
    ctx["L_t"] += lines_consumed

    # ── Update epistemic confidence κ_t ──
    # Heuristic: confidence grows with each relevant file read,
    # but with diminishing returns
    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        rel_path = _to_relative(file_path)

        # Track explored files
        if rel_path not in state["explored_files"]:
            state["explored_files"].append(rel_path)

            # Confidence boost based on file relevance
            # Files in the scouting candidates list get a bigger boost
            if rel_path in state.get("candidates", {}):
                score = state["candidates"][rel_path].get("score", 0.5)
                boost = min(15, score * 20)
            else:
                boost = 5  # Unknown file, smaller boost

            ctx["kappa_t"] = min(100, ctx["kappa_t"] + boost)
        else:
            # Re-reading a file: tiny boost (might be for specific section)
            ctx["kappa_t"] = min(100, ctx["kappa_t"] + 1)

    elif tool_name == "Grep":
        # Grep results help narrow search — moderate confidence boost
        ctx["kappa_t"] = min(100, ctx["kappa_t"] + 3)

    elif tool_name == "Glob":
        # Glob is pure scouting — small boost
        ctx["kappa_t"] = min(100, ctx["kappa_t"] + 1)

    elif tool_name == "Bash":
        # Bash commands (tests, builds) — varies
        command = tool_input.get("command", "")
        if any(kw in command for kw in ["test", "pytest", "jest", "cargo test", "go test"]):
            ctx["kappa_t"] = min(100, ctx["kappa_t"] + 10)
        elif any(kw in command for kw in ["grep", "find", "rg", "ag", "fd"]):
            ctx["kappa_t"] = min(100, ctx["kappa_t"] + 3)
        else:
            ctx["kappa_t"] = min(100, ctx["kappa_t"] + 2)

    # ── Compute IGR ──
    igr = information_gain_rate(ctx["kappa_t"], kappa_prev, ctx["L_t"], L_prev)
    ctx["igr_history"].append(round(igr, 4))
    # Keep last 20 entries
    ctx["igr_history"] = ctx["igr_history"][-20:]

    # ── Ensure budget is set ──
    if ctx["budget"] <= 0:
        ctx["budget"] = compute_budget(ctx["D_q"], ctx["H_r"])

    # ── Save updated state ──
    state["context_state"] = ctx
    save_state(state)

    # ── Check termination and provide feedback ──
    terminate, reason = should_terminate(state)

    context_parts = []
    if terminate:
        context_parts.append(
            f"[TokenScout Context] {reason}. "
            f"κ={ctx['kappa_t']:.1f}, L={ctx['L_t']}/{ctx['budget']}, "
            f"t={ctx['t']}, files_read={len(state['explored_files'])}. "
            f"Consider synthesizing an answer from gathered context."
        )

    # Periodic status (every 5 tool calls)
    elif ctx["t"] % 5 == 0:
        context_parts.append(
            f"[TokenScout Status] Step {ctx['t']}: "
            f"κ={ctx['kappa_t']:.1f}, L={ctx['L_t']}/{ctx['budget']}, "
            f"IGR={igr:.4f}, files_read={len(state['explored_files'])}"
        )

    if context_parts:
        output = {
            "additionalContext": "\n".join(context_parts),
        }
        print(json.dumps(output))

    # ── Audit log ──
    audit_log("post_tool_use", {
        "tool": tool_name,
        "lines_consumed": lines_consumed,
        "L_t": ctx["L_t"],
        "kappa_t": round(ctx["kappa_t"], 2),
        "igr": round(igr, 4),
        "t": ctx["t"],
        "budget": ctx["budget"],
        "terminate": terminate,
        "reason": reason,
    })


def _estimate_lines_consumed(tool_name: str, tool_input: dict, tool_response: dict) -> int:
    """Estimate lines of context consumed by a tool call."""
    if tool_name == "Read":
        # If limit is specified, use that; otherwise estimate from file
        limit = tool_input.get("limit", 0)
        if limit:
            return limit
        # Estimate from response length
        resp_str = str(tool_response)
        return max(1, resp_str.count("\n"))

    elif tool_name == "Grep":
        resp_str = str(tool_response)
        return max(1, resp_str.count("\n"))

    elif tool_name == "Glob":
        # Glob returns file paths — lightweight
        resp_str = str(tool_response)
        return max(1, resp_str.count("\n") // 5)  # much less context weight

    elif tool_name == "Bash":
        resp_str = str(tool_response)
        return max(1, resp_str.count("\n"))

    return 10  # default estimate


def _to_relative(file_path: str) -> str:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    if file_path.startswith(project_dir):
        return os.path.relpath(file_path, project_dir)
    return file_path


if __name__ == "__main__":
    main()
