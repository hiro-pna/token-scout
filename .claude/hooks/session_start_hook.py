#!/usr/bin/env python3
"""
TokenScout Hook — Phase 1: SessionStart
Builds the semantic-structural map and injects a scouting summary into context.
"""
import json
import os
import sys
import time

# Allow imports from the hooks directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    scan_repo_lightweight, compute_budget, get_project_dir,
)


def main():
    hook_input = read_hook_input()
    source = hook_input.get("source", "startup")
    project_dir = get_project_dir()

    # Build or refresh the repo map
    state = load_state()

    # Only do a full scan on startup or if state is stale (> 10 min)
    needs_scan = (
        source in ("startup", "clear")
        or not state["repo_map"]["files"]
        or (time.time() - state.get("session_start", 0)) > 600
    )

    if needs_scan:
        repo_map = scan_repo_lightweight(project_dir)
        state["repo_map"] = repo_map
        state["session_start"] = time.time()

        # Initialize cost-aware context state
        H_r = repo_map["stats"].get("entropy", 1.0)
        state["context_state"]["H_r"] = H_r
        state["context_state"]["L_t"] = 0
        state["context_state"]["t"] = 0
        state["context_state"]["kappa_t"] = 0.0
        state["context_state"]["igr_history"] = []
        state["explored_files"] = []
        state["scouted_files"] = []
        state["candidates"] = {}

        save_state(state)

        audit_log("session_start_scan", {
            "source": source,
            "total_files": repo_map["stats"]["total_files"],
            "total_lines": repo_map["stats"]["total_lines"],
            "languages": repo_map["stats"]["languages"],
            "entropy": H_r,
        })

    # ── Output context reminder (injected into Claude's context) ──
    stats = state["repo_map"]["stats"]
    n_files = stats.get("total_files", 0)
    langs = stats.get("languages", {})
    entropy = stats.get("entropy", 1.0)

    # Build a compact structural summary
    top_dirs = _top_directories(state["repo_map"]["files"], limit=10)
    top_symbols = _top_symbols(state["repo_map"]["symbols"], limit=15)

    summary_parts = [
        f"[TokenScout Scouting Map] Repository: {os.path.basename(project_dir)}",
        f"  Files: {n_files} | Languages: {', '.join(f'{k}({v})' for k,v in sorted(langs.items(), key=lambda x:-x[1]))}",
        f"  Entropy H_r={entropy:.2f} | Lines: {stats.get('total_lines', 0)}",
    ]

    if top_dirs:
        summary_parts.append(f"  Key dirs: {', '.join(top_dirs)}")

    if top_symbols:
        summary_parts.append(f"  Key symbols: {', '.join(top_symbols)}")

    summary_parts.extend([
        "",
        "TOKENSCOUT STRATEGY: Use structural scouting before reading files.",
        "  1. Check the repo map for relevant files/symbols first",
        "  2. Read only high-priority targets (not entire directories)",
        "  3. Follow dependency edges for cross-file reasoning",
        "  4. Stop when confidence is sufficient — avoid over-reading",
    ])

    print("\n".join(summary_parts))


def _top_directories(files: dict, limit: int = 10) -> list:
    """Extract the most populated directories."""
    dir_counts = {}
    for path in files:
        d = os.path.dirname(path)
        if d:
            top = d.split(os.sep)[0] if os.sep in d else d
            dir_counts[top] = dir_counts.get(top, 0) + 1
    return [d for d, _ in sorted(dir_counts.items(), key=lambda x: -x[1])[:limit]]


def _top_symbols(symbols: dict, limit: int = 15) -> list:
    """Extract the most important symbol names."""
    names = []
    for key, info in list(symbols.items())[:200]:
        name = key.split("::")[-1] if "::" in key else key
        if info.get("type") in ("class", "struct", "enum"):
            names.append(name)
    # Deduplicate
    seen = set()
    unique = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    return unique[:limit]


if __name__ == "__main__":
    main()
