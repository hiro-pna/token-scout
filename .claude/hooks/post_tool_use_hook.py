#!/usr/bin/env python3
"""
TokenScout Hook — Phase 3: PostToolUse
Tracks context consumption and updates the cost-aware state vector.

Implements §3.3 Cost-Aware Context Management:
  - S_t = {D_q, H_r, L_t, t, κ_t}
  - Information Gain Rate (IGR_t)
  - Dynamic budget enforcement
  - Optional LLM confidence assessment (every N tool calls)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    information_gain_rate, should_terminate, compute_budget,
    estimate_confidence_boost,
)
from tokenscout_llm import is_llm_available, assess_confidence


def main():
    hook_input = read_hook_input()
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    tool_response = hook_input.get("tool_response", {})

    state = load_state()
    ctx = state["context_state"]
    repo_map = state.get("repo_map", {})

    # ── Update iteration depth t ──
    ctx["t"] += 1

    # ── Estimate context consumed by this tool call ──
    lines_consumed = _estimate_lines_consumed(tool_name, tool_input, tool_response)

    # ── Update L_t (cumulative context) ──
    L_prev = ctx["L_t"]
    kappa_prev = ctx["kappa_t"]
    ctx["L_t"] += lines_consumed

    # ── Update epistemic confidence κ_t (§3.3.1) ──
    # Multi-signal confidence estimation: candidate score, graph centrality,
    # diminishing returns, coverage fraction
    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        rel_path = _to_relative(file_path)

        # Track explored files
        if rel_path not in state["explored_files"]:
            state["explored_files"].append(rel_path)

        boost = estimate_confidence_boost(rel_path, state, tool_name, tool_input)
        ctx["kappa_t"] = min(100, ctx["kappa_t"] + boost)

    elif tool_name in ("Grep", "Glob", "Bash"):
        boost = estimate_confidence_boost("", state, tool_name, tool_input)
        ctx["kappa_t"] = min(100, ctx["kappa_t"] + boost)

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

    # ── Optional LLM confidence assessment (every 5 tool calls) ──
    llm_confidence = None
    if is_llm_available() and ctx["t"] % 5 == 0 and ctx["t"] > 0:
        # Gather signatures from explored files
        explored_sigs = []
        for efile in state.get("explored_files", [])[:15]:
            finfo = repo_map.get("files", {}).get(efile, {})
            for sig in finfo.get("signatures", [])[:3]:
                explored_sigs.append(f"{efile}::{sig.get('name', '')}")

        # Unexplored candidates
        explored_set = set(state.get("explored_files", []))
        remaining = [p for p in state.get("candidates", {}) if p not in explored_set]

        budget_used_pct = (ctx["L_t"] / max(1, ctx["budget"])) * 100

        llm_confidence = assess_confidence(
            query=state.get("_last_query", ""),
            explored_files=state.get("explored_files", []),
            explored_signatures=explored_sigs,
            candidates_remaining=remaining,
            current_kappa=ctx["kappa_t"],
            budget_used_pct=budget_used_pct,
        )

        # Blend LLM confidence with heuristic (70% LLM + 30% heuristic)
        if llm_confidence.get("kappa_estimate") is not None:
            blended_kappa = 0.7 * llm_confidence["kappa_estimate"] + 0.3 * ctx["kappa_t"]
            ctx["kappa_t"] = min(100, blended_kappa)
            state["context_state"] = ctx
            save_state(state)

            # Re-check termination with updated confidence
            terminate, reason = should_terminate(state)

            if llm_confidence.get("reasoning"):
                context_parts.append(
                    f"[TokenScout LLM Assessment] κ={ctx['kappa_t']:.1f} "
                    f"({llm_confidence['reasoning']})"
                )
            if llm_confidence.get("next_targets"):
                context_parts.append(
                    f"[TokenScout LLM Suggestion] Explore: {', '.join(llm_confidence['next_targets'])}"
                )

    if terminate:
        context_parts.append(
            f"[TokenScout Context] {reason}. "
            f"κ={ctx['kappa_t']:.1f}, L={ctx['L_t']}/{ctx['budget']}, "
            f"t={ctx['t']}, files_read={len(state['explored_files'])}. "
            f"Consider synthesizing an answer from gathered context."
        )

    # Periodic status (every 5 tool calls, if LLM didn't already provide one)
    elif ctx["t"] % 5 == 0 and not llm_confidence:
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
    audit_entry = {
        "tool": tool_name,
        "lines_consumed": lines_consumed,
        "L_t": ctx["L_t"],
        "kappa_t": round(ctx["kappa_t"], 2),
        "igr": round(igr, 4),
        "t": ctx["t"],
        "budget": ctx["budget"],
        "terminate": terminate,
        "reason": reason,
    }
    if llm_confidence and llm_confidence.get("kappa_estimate") is not None:
        audit_entry["llm_kappa"] = round(llm_confidence["kappa_estimate"], 2)
        audit_entry["llm_reasoning"] = llm_confidence.get("reasoning", "")
    audit_log("post_tool_use", audit_entry)


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
