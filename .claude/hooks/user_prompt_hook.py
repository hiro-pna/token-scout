#!/usr/bin/env python3
"""
TokenScout Hook — UserPromptSubmit: Query Augmentation + BM25 Ranking

Implements §3.2.1 Query Augmentation:
  - Categorize query intent
  - Estimate query complexity D_q (heuristic + optional LLM)
  - Compute dynamic budget B ∝ D_q · H_r
  - BM25 sparse retrieval for candidate ranking
  - Optional Haiku LLM for query expansion + semantic ranking
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tokenscout_common import (
    read_hook_input, load_state, save_state, audit_log,
    compute_budget, priority_score, BM25Index,
)
from tokenscout_llm import (
    is_llm_available, augment_query, semantic_rank_candidates,
    build_repo_summary, build_file_summaries,
)


def main():
    hook_input = read_hook_input()
    prompt = hook_input.get("prompt", "")

    if not prompt.strip():
        sys.exit(0)

    state = load_state()
    ctx = state["context_state"]
    repo_map = state["repo_map"]

    # ── Step 1: Heuristic complexity estimate ──
    D_q_heuristic = _estimate_complexity(prompt)

    # ── Step 2: Optional LLM query augmentation ──
    llm_result = {"expanded_terms": [], "D_q": None, "intent": None, "strategy": None}
    expanded_terms = []

    if is_llm_available():
        repo_summary = build_repo_summary(repo_map)
        # Get initial keyword candidates for LLM context
        keyword_candidates = list(_find_candidates_keyword(prompt, repo_map).keys())[:10]
        llm_result = augment_query(prompt, repo_summary, keyword_candidates)
        expanded_terms = llm_result.get("expanded_terms", [])

    # Use LLM D_q if available, else heuristic
    D_q = llm_result.get("D_q") if llm_result.get("D_q") is not None else D_q_heuristic
    ctx["D_q"] = D_q

    # ── Step 3: Reset per-query tracking ──
    ctx["L_t"] = 0
    ctx["t"] = 0
    ctx["kappa_t"] = 0.0
    ctx["igr_history"] = []
    state["explored_files"] = []
    state["scouted_files"] = []

    # ── Step 4: Compute dynamic budget ──
    H_r = ctx.get("H_r", 1.0)
    budget = compute_budget(D_q, H_r)
    ctx["budget"] = budget

    # ── Step 5: BM25 candidate ranking ──
    bm25 = BM25Index()
    bm25.index_repo(repo_map)

    # Combine original prompt with LLM expanded terms
    search_query = prompt
    if expanded_terms:
        search_query += " " + " ".join(expanded_terms)

    bm25_results = bm25.query(search_query, top_k=20)

    # ── Step 6: Merge BM25 with keyword candidates ──
    keyword_candidates = _find_candidates_keyword(prompt, repo_map)

    candidates = {}
    # BM25 scores (normalize to 0-1)
    if bm25_results:
        max_bm25 = max(s for _, s in bm25_results) if bm25_results else 1.0
        for path, score in bm25_results:
            norm_score = score / max(0.01, max_bm25)
            file_info = repo_map.get("files", {}).get(path, {})
            n_sigs = len(file_info.get("signatures", []))
            density = min(1.0, n_sigs / max(1, file_info.get("lines", 1)) * 20)

            final_score = priority_score(
                relevance=min(1.0, norm_score),
                tool_confirmed=False,
                density=density,
            )
            candidates[path] = {
                "score": round(final_score, 3),
                "bm25_raw": round(score, 3),
                "provenance": "bm25",
                "cost": file_info.get("lines", 0),
            }

    # Merge keyword candidates (upgrade score if BM25 also found them)
    for path, info in keyword_candidates.items():
        if path in candidates:
            # Boost: both BM25 and keyword match
            candidates[path]["score"] = round(
                min(1.0, candidates[path]["score"] * 1.2 + info["score"] * 0.3), 3
            )
            candidates[path]["provenance"] = "bm25+keyword"
        else:
            candidates[path] = info

    # ── Step 7: Optional LLM semantic re-ranking ──
    if is_llm_available() and candidates:
        file_summaries = build_file_summaries(repo_map)
        ranked = semantic_rank_candidates(prompt, candidates, file_summaries)
        # Merge LLM ranking into candidates
        for i, (path, llm_score) in enumerate(ranked):
            if path in candidates:
                # Blend: 60% LLM + 40% BM25/keyword
                old_score = candidates[path]["score"]
                candidates[path]["score"] = round(0.6 * llm_score + 0.4 * old_score, 3)
                candidates[path]["provenance"] += "+llm"

    # Keep top 20
    if len(candidates) > 20:
        sorted_candidates = sorted(candidates.items(), key=lambda x: -x[1]["score"])[:20]
        candidates = dict(sorted_candidates)

    state["candidates"] = candidates
    state["_last_query"] = prompt  # saved for LLM confidence assessment
    state["context_state"] = ctx
    save_state(state)

    # ── Inject augmented context ──
    context_parts = [
        f"[TokenScout Query Analysis] D_q={D_q}, Budget={budget} lines, H_r={H_r:.2f}",
    ]

    if llm_result.get("intent"):
        context_parts[0] += f", Intent={llm_result['intent']}"

    if llm_result.get("strategy"):
        context_parts.append(f"[TokenScout LLM Strategy] {llm_result['strategy']}")

    if expanded_terms:
        context_parts.append(
            f"[TokenScout Expanded Terms] {', '.join(expanded_terms[:10])}"
        )

    if candidates:
        top_candidates = sorted(candidates.items(), key=lambda x: -x[1]["score"])[:8]
        candidate_list = ", ".join(
            f"{path} ({info['provenance']}, score={info['score']:.2f})"
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
        "D_q_heuristic": D_q_heuristic,
        "D_q_llm": llm_result.get("D_q"),
        "budget": budget,
        "H_r": H_r,
        "llm_available": is_llm_available(),
        "llm_intent": llm_result.get("intent"),
        "expanded_terms": expanded_terms[:10],
        "num_candidates": len(candidates),
        "num_bm25": len(bm25_results),
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


def _find_candidates_keyword(prompt: str, repo_map: dict) -> dict:
    """
    Identify initial candidate files using keyword matching.
    Original heuristic — now supplemented by BM25.
    """
    candidates = {}
    prompt_lower = prompt.lower()

    terms = _extract_search_terms(prompt_lower)
    if not terms:
        return candidates

    files = repo_map.get("files", {})

    for path, info in files.items():
        score = 0.0
        path_lower = path.lower()

        for term in terms:
            if term in path_lower:
                score += 0.3
            for part in path_lower.split(os.sep):
                if term in part:
                    score += 0.2

        for sig in info.get("signatures", []):
            sig_name = sig.get("name", "").lower()
            sig_str = sig.get("signature", "").lower()
            for term in terms:
                if term in sig_name:
                    score += 0.4
                elif term in sig_str:
                    score += 0.2

        if score > 0.1:
            n_sigs = len(info.get("signatures", []))
            density = min(1.0, n_sigs / max(1, info.get("lines", 1)) * 20)

            final_score = priority_score(
                relevance=min(1.0, score),
                tool_confirmed=False,
                density=density,
            )
            candidates[path] = {
                "score": round(final_score, 3),
                "provenance": "keyword",
                "cost": info.get("lines", 0),
            }

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
