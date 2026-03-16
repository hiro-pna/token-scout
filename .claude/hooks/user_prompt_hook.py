#!/usr/bin/env python3
"""
TokenScout Hook — UserPromptSubmit: Query Augmentation

Implements §3.2.1 Query Augmentation:
  - Categorize query intent
  - Estimate query complexity D_q
  - Compute dynamic budget B ∝ D_q · H_r
  - Identify initial candidate files from the structural map
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    compute_budget, priority_score,
)


def main():
    hook_input = read_hook_input()
    prompt = hook_input.get("prompt", "")

    if not prompt.strip():
        sys.exit(0)

    state = load_state()
    ctx = state["context_state"]
    repo_map = state["repo_map"]

    # ── Estimate Query Complexity D_q (0-100) ──
    D_q = _estimate_complexity(prompt)
    ctx["D_q"] = D_q

    # ── Reset per-query tracking ──
    ctx["L_t"] = 0
    ctx["t"] = 0
    ctx["kappa_t"] = 0.0
    ctx["igr_history"] = []
    state["explored_files"] = []
    state["scouted_files"] = []

    # ── Compute dynamic budget ──
    H_r = ctx.get("H_r", 1.0)
    budget = compute_budget(D_q, H_r)
    ctx["budget"] = budget

    # ── Identify initial candidates from the structural map ──
    candidates = _find_candidates(prompt, repo_map)
    state["candidates"] = candidates

    state["context_state"] = ctx
    save_state(state)

    # ── Inject augmented context ──
    context_parts = [
        f"[TokenScout Query Analysis] Complexity D_q={D_q}, Budget={budget} lines, H_r={H_r:.2f}",
    ]

    if candidates:
        top_candidates = sorted(candidates.items(), key=lambda x: -x[1]["score"])[:8]
        candidate_list = ", ".join(
            f"{path} (score={info['score']:.2f})"
            for path, info in top_candidates
        )
        context_parts.append(
            f"[TokenScout Candidates] Priority targets: {candidate_list}"
        )
        context_parts.append(
            "[TokenScout Strategy] Start with these candidates. "
            "Use Grep/Glob to confirm relevance before Read. "
            "Follow dependency edges for cross-file context."
        )

    output = {
        "additionalContext": "\n".join(context_parts),
    }
    print(json.dumps(output))

    audit_log("query_augmentation", {
        "D_q": D_q,
        "budget": budget,
        "H_r": H_r,
        "num_candidates": len(candidates),
        "top_candidates": [p for p, _ in sorted(candidates.items(), key=lambda x: -x[1]["score"])[:5]],
    })


def _estimate_complexity(prompt: str) -> int:
    """
    Estimate query complexity D_q ∈ [0, 100].

    Heuristic based on:
      - Query length (longer = more complex)
      - Multi-hop indicators (cross-file, dependency, flow)
      - Scope indicators (repository-wide, architecture)
      - Technical depth indicators
    """
    score = 10  # baseline

    words = prompt.lower().split()
    n_words = len(words)

    # Length factor
    if n_words > 50:
        score += 20
    elif n_words > 20:
        score += 10
    elif n_words > 10:
        score += 5

    # Multi-hop indicators
    multi_hop_kw = [
        "dependency", "dependencies", "import", "imports",
        "calls", "flow", "trace", "chain", "across files",
        "cross-file", "inheritance", "extends", "implements",
        "interaction", "relationship", "connected",
    ]
    score += min(30, sum(5 for kw in multi_hop_kw if kw in prompt.lower()))

    # Scope indicators
    scope_kw = [
        "architecture", "design", "system", "entire", "all files",
        "repository", "codebase", "project-wide", "overall",
    ]
    score += min(20, sum(5 for kw in scope_kw if kw in prompt.lower()))

    # Technical depth
    depth_kw = [
        "algorithm", "performance", "security", "race condition",
        "deadlock", "memory leak", "optimization", "complexity",
        "concurrency", "thread", "async",
    ]
    score += min(20, sum(5 for kw in depth_kw if kw in prompt.lower()))

    return min(100, score)


def _find_candidates(prompt: str, repo_map: dict) -> dict:
    """
    Identify initial candidate files from the structural map.
    Uses keyword matching against file paths, symbols, and signatures.
    """
    candidates = {}
    prompt_lower = prompt.lower()

    # Extract meaningful terms from the prompt
    terms = _extract_search_terms(prompt_lower)
    if not terms:
        return candidates

    files = repo_map.get("files", {})
    symbols = repo_map.get("symbols", {})
    deps = repo_map.get("dependencies", {})

    for path, info in files.items():
        score = 0.0
        path_lower = path.lower()

        # Path matching
        for term in terms:
            if term in path_lower:
                score += 0.3
            # Match directory names
            for part in path_lower.split(os.sep):
                if term in part:
                    score += 0.2

        # Signature matching
        for sig in info.get("signatures", []):
            sig_name = sig.get("name", "").lower()
            sig_str = sig.get("signature", "").lower()
            for term in terms:
                if term in sig_name:
                    score += 0.4
                elif term in sig_str:
                    score += 0.2

        if score > 0.1:
            # Density: fine-grained units in large files are more valuable
            n_sigs = len(info.get("signatures", []))
            density = min(1.0, n_sigs / max(1, info.get("lines", 1)) * 20)

            final_score = priority_score(
                relevance=min(1.0, score),
                tool_confirmed=False,
                density=density,
            )
            candidates[path] = {
                "score": round(final_score, 3),
                "provenance": "map_search",
                "cost": info.get("lines", 0),
            }

    # Keep top 20 candidates
    if len(candidates) > 20:
        sorted_candidates = sorted(candidates.items(), key=lambda x: -x[1]["score"])[:20]
        candidates = dict(sorted_candidates)

    return candidates


def _extract_search_terms(prompt: str) -> list:
    """Extract meaningful search terms from the prompt."""
    stopwords = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
        "been", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "this", "that", "these",
        "those", "i", "you", "he", "she", "it", "we", "they", "how", "what",
        "where", "when", "why", "which", "who", "me", "my", "your",
        "code", "file", "files", "function", "class", "method",
        "explain", "show", "find", "tell", "about", "please",
    }
    words = re.findall(r'\b[a-z_][a-z0-9_]+\b', prompt)
    return [w for w in words if w not in stopwords and len(w) > 2]


if __name__ == "__main__":
    main()
