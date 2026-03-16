#!/usr/bin/env python3
"""
TokenScout Hook — Phase 2: PreToolUse
Structural scouting + cost-aware gate for file reads.

Implements:
  - §3.2.2 Structural Repository Scouting: expose metadata BEFORE full read
  - §3.3   Cost-Aware Context Management: budget enforcement
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    should_terminate, priority_score, compute_budget,
    find_related_via_graphs,
)


def main():
    hook_input = read_hook_input()
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    state = load_state()
    ctx = state["context_state"]
    repo_map = state["repo_map"]

    # ── Budget check (§3.3.2) ──
    # Recompute budget if not set
    if ctx["budget"] <= 0 and ctx["D_q"] > 0:
        ctx["budget"] = compute_budget(ctx["D_q"], ctx["H_r"])
        state["context_state"] = ctx
        save_state(state)

    terminate, reason = should_terminate(state)

    # ── Handle Read tool ──
    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        rel_path = _to_relative(file_path)

        # Structural scouting: inject metadata as context
        file_info = repo_map.get("files", {}).get(rel_path, {})
        deps = repo_map.get("dependencies", {}).get(rel_path, [])

        if file_info:
            sigs = file_info.get("signatures", [])
            size = file_info.get("lines", 0)

            # Track as scouted
            if rel_path not in state["scouted_files"]:
                state["scouted_files"].append(rel_path)

            # If budget exhausted, advise but don't block
            context_parts = []
            if terminate:
                context_parts.append(
                    f"[TokenScout Budget Warning] {reason}. "
                    f"Consider whether this read is essential. "
                    f"Current context: {ctx['L_t']} lines / budget {ctx['budget']}."
                )

            # Provide structural metadata (scouting data)
            if sigs:
                sig_summary = "; ".join(
                    f"{s['type']} {s['name']} (L{s.get('line', '?')})"
                    for s in sigs[:10]
                )
                context_parts.append(
                    f"[TokenScout Scout] {rel_path} ({size} lines, {file_info.get('lang', '?')}): "
                    f"Contains: {sig_summary}"
                )

            if deps:
                context_parts.append(
                    f"[TokenScout Deps] {rel_path} imports: {', '.join(deps[:8])}"
                )

            # Find related files via 3-layer graph (§3.1.3: G_dep + G_inh + G_call)
            related = find_related_via_graphs(rel_path, repo_map)
            if related:
                rel_summary = "; ".join(
                    f"{p} ({info['relation']} via {info['via']})"
                    for p, info in list(related.items())[:6]
                )
                context_parts.append(
                    f"[TokenScout Graph] Related to {rel_path}: {rel_summary}"
                )

            if context_parts:
                # Use JSON output to add context without blocking
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "\n".join(context_parts),
                    }
                }
                print(json.dumps(output))
                save_state(state)

                audit_log("pre_read_scout", {
                    "file": rel_path,
                    "sigs_count": len(sigs),
                    "deps_count": len(deps),
                    "budget_remaining": max(0, ctx["budget"] - ctx["L_t"]),
                    "terminate_advised": terminate,
                })
                return

        # File not in map — allow but log
        audit_log("pre_read_unknown", {"file": rel_path})

    # ── Handle Grep/Glob — lightweight, always allow ──
    elif tool_name in ("Grep", "Glob"):
        # These are scouting tools themselves — always allow
        # But inject budget awareness if exhausted
        if terminate:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"[TokenScout Budget] {reason}. "
                        f"Context: {ctx['L_t']}/{ctx['budget']} lines. "
                        f"Focus on confirming existing candidates."
                    ),
                }
            }
            print(json.dumps(output))
            return

    # Default: allow everything
    sys.exit(0)


def _to_relative(file_path: str) -> str:
    """Convert absolute path to project-relative."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    if file_path.startswith(project_dir):
        return os.path.relpath(file_path, project_dir)
    return file_path




if __name__ == "__main__":
    main()
