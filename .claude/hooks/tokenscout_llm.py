#!/usr/bin/env python3
"""
TokenScout LLM Integration — Claude Haiku for intelligent code navigation.

Fills the LLM-dependent gaps identified in the FastCode paper:
  1. Query Augmentation (§3.2.1): Haiku expands queries with synonyms, related concepts
  2. Confidence Assessment (§3.3.1): Haiku evaluates "have we gathered enough context?"
  3. Semantic Ranking (§3.2.2): Haiku ranks candidate files by semantic relevance

Authentication priority:
  1. OAuth via `claude -p` CLI (uses existing Claude Code subscription — free)
  2. ANTHROPIC_API_KEY env var (pay-per-token fallback)
  3. Graceful degradation to heuristics if neither is available

Cost with API key: Haiku ~$0.25/1M input, ~$1.25/1M output.
Cost with OAuth: $0 (included in Claude Code subscription).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# ─── Configuration ──────────────────────────────────────────────────────────

HAIKU_MODEL = "claude-haiku-4-5-20251001"
CLAUDE_CLI_MODEL = "haiku"  # alias for claude -p --model
MAX_TOKENS_AUGMENT = 300
MAX_TOKENS_CONFIDENCE = 200
MAX_TOKENS_RANKING = 400
API_TIMEOUT = 8  # seconds — hooks have strict time limits
CLI_TIMEOUT = 15  # slightly longer for CLI subprocess startup

# ─── Auth detection ─────────────────────────────────────────────────────────

_llm_method: Optional[str] = None  # cached: "oauth", "api_key", or None


def _detect_llm_method() -> Optional[str]:
    """
    Detect the best available LLM method.
    Priority: OAuth (claude CLI) > API key > None.
    """
    global _llm_method
    if _llm_method is not None:
        return _llm_method if _llm_method != "" else None

    # 1. Check for claude CLI (OAuth)
    if shutil.which("claude"):
        _llm_method = "oauth"
        return "oauth"

    # 2. Check for API key
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        _llm_method = "api_key"
        return "api_key"

    _llm_method = ""  # cache negative result
    return None


def is_llm_available() -> bool:
    """Check if any LLM method is available (OAuth or API key)."""
    return _detect_llm_method() is not None


def _call_haiku(system: str, user_message: str, max_tokens: int = 200) -> Optional[str]:
    """
    Call Claude Haiku using the best available method.
    Priority: OAuth (claude -p) > API key (direct HTTP).
    Returns the text response or None on failure.
    """
    method = _detect_llm_method()
    if method is None:
        return None

    if method == "oauth":
        return _call_haiku_oauth(system, user_message, max_tokens)
    else:
        return _call_haiku_api_key(system, user_message, max_tokens)


def _call_haiku_oauth(system: str, user_message: str, max_tokens: int = 200) -> Optional[str]:
    """
    Call Haiku via `claude -p --model haiku` using OAuth session.
    Zero cost — uses the existing Claude Code subscription.
    """
    try:
        # Combine system + user message into a single prompt for -p mode
        combined_prompt = f"<system>{system}</system>\n\n{user_message}"

        result = subprocess.run(
            [
                "claude", "-p",
                "--model", CLAUDE_CLI_MODEL,
                "--output-format", "text",
                "--no-session-persistence",
            ],
            input=combined_prompt,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT,
        )

        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

        # Log stderr if there's an error
        if result.stderr.strip():
            _log_llm_error("oauth_stderr", result.stderr[:200])

    except subprocess.TimeoutExpired:
        _log_llm_error("oauth_timeout", f"CLI timed out after {CLI_TIMEOUT}s")
    except FileNotFoundError:
        _log_llm_error("oauth_not_found", "claude CLI not found")
        # Invalidate cache — CLI disappeared
        global _llm_method
        _llm_method = None
    except Exception as e:
        _log_llm_error("oauth_failed", str(e)[:200])

    return None


def _call_haiku_api_key(system: str, user_message: str, max_tokens: int = 200) -> Optional[str]:
    """
    Call Claude Haiku via the Anthropic Messages API (pay-per-token).
    Uses urllib (stdlib) to avoid requiring the anthropic SDK.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    import urllib.request
    import urllib.error

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    payload = json.dumps({
        "model": HAIKU_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_message}],
        "system": system,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for block in data.get("content", []):
                if block.get("type") == "text":
                    return block["text"]
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, Exception) as e:
        _log_llm_error("api_key_call_failed", str(e))
    return None


def _log_llm_error(event: str, error: str) -> None:
    """Log LLM errors to audit file without importing full commons."""
    try:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        log_file = os.path.join(project_dir, ".claude", "hooks", "tokenscout_audit.jsonl")
        entry = json.dumps({"ts": time.time(), "event": event, "error": error})
        with open(log_file, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass


# ─── 1. Query Augmentation (§3.2.1) ─────────────────────────────────────────

def augment_query(
    prompt: str,
    repo_summary: str,
    current_candidates: List[str],
) -> Dict[str, Any]:
    """
    Use Haiku to intelligently augment a user query.

    Returns:
        {
            "expanded_terms": [...],  # additional search terms
            "D_q": int,               # estimated complexity (0-100)
            "intent": str,            # query intent classification
            "strategy": str,          # suggested exploration strategy
        }
    """
    if not is_llm_available():
        return {"expanded_terms": [], "D_q": None, "intent": None, "strategy": None}

    system = (
        "You are a code navigation assistant. Analyze the user's query about a codebase "
        "and respond with ONLY a JSON object (no markdown, no explanation)."
    )

    candidates_str = ", ".join(current_candidates[:10]) if current_candidates else "none yet"

    user_msg = f"""Analyze this code query and help me find relevant files.

Query: "{prompt}"

Repository summary:
{repo_summary[:1500]}

Current candidate files: {candidates_str}

Respond with ONLY this JSON:
{{
  "expanded_terms": ["list", "of", "additional", "search", "keywords", "synonyms"],
  "D_q": <complexity 0-100>,
  "intent": "<one of: bug_fix, feature_understanding, refactor, architecture, debugging, testing, documentation>",
  "strategy": "<brief 1-line strategy suggestion>"
}}"""

    response = _call_haiku(system, user_msg, MAX_TOKENS_AUGMENT)
    if not response:
        return {"expanded_terms": [], "D_q": None, "intent": None, "strategy": None}

    try:
        # Parse JSON from response (handle potential markdown wrapping)
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(text)
        return {
            "expanded_terms": result.get("expanded_terms", [])[:15],
            "D_q": min(100, max(0, int(result.get("D_q", 50)))),
            "intent": result.get("intent", "unknown"),
            "strategy": result.get("strategy", ""),
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"expanded_terms": [], "D_q": None, "intent": None, "strategy": None}


# ─── 2. Confidence Assessment (§3.3.1) ──────────────────────────────────────

def assess_confidence(
    query: str,
    explored_files: List[str],
    explored_signatures: List[str],
    candidates_remaining: List[str],
    current_kappa: float,
    budget_used_pct: float,
) -> Dict[str, Any]:
    """
    Use Haiku to assess epistemic confidence — "have we gathered enough?"

    Returns:
        {
            "kappa_estimate": float,  # LLM's confidence estimate (0-100)
            "reasoning": str,         # why this confidence level
            "should_continue": bool,  # whether to keep exploring
            "next_targets": [...],    # suggested files to read next
        }
    """
    if not is_llm_available():
        return {
            "kappa_estimate": None,
            "reasoning": None,
            "should_continue": None,
            "next_targets": [],
        }

    system = (
        "You are a code navigation confidence assessor. Evaluate whether enough "
        "codebase context has been gathered to answer the query. "
        "Respond with ONLY a JSON object."
    )

    explored_str = "\n".join(f"  - {f}" for f in explored_files[:15])
    sigs_str = "\n".join(f"  - {s}" for s in explored_signatures[:20])
    remaining_str = ", ".join(candidates_remaining[:10])

    user_msg = f"""Query: "{query}"

Files explored ({len(explored_files)}):
{explored_str}

Key signatures found:
{sigs_str}

Unexplored candidates: {remaining_str}
Current heuristic confidence: {current_kappa:.1f}/100
Budget used: {budget_used_pct:.0f}%

Respond with ONLY this JSON:
{{
  "kappa_estimate": <your confidence estimate 0-100>,
  "reasoning": "<brief explanation>",
  "should_continue": <true/false>,
  "next_targets": ["file1", "file2"]
}}"""

    response = _call_haiku(system, user_msg, MAX_TOKENS_CONFIDENCE)
    if not response:
        return {
            "kappa_estimate": None,
            "reasoning": None,
            "should_continue": None,
            "next_targets": [],
        }

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = json.loads(text)
        return {
            "kappa_estimate": min(100, max(0, float(result.get("kappa_estimate", 50)))),
            "reasoning": result.get("reasoning", ""),
            "should_continue": bool(result.get("should_continue", True)),
            "next_targets": result.get("next_targets", [])[:5],
        }
    except (json.JSONDecodeError, ValueError, TypeError):
        return {
            "kappa_estimate": None,
            "reasoning": None,
            "should_continue": None,
            "next_targets": [],
        }


# ─── 3. Semantic Ranking (§3.2.2) ──────────────────────────────────────────

def semantic_rank_candidates(
    query: str,
    candidates: Dict[str, Dict],
    file_summaries: Dict[str, str],
) -> List[Tuple[str, float]]:
    """
    Use Haiku to semantically rank candidate files beyond BM25 scoring.

    Args:
        query: the user's question
        candidates: {path: {score, ...}} from BM25/keyword matching
        file_summaries: {path: "brief description from signatures"}

    Returns:
        List of (path, score) sorted by semantic relevance.
    """
    if not is_llm_available() or not candidates:
        return [(p, info.get("score", 0)) for p, info in candidates.items()]

    system = (
        "You are a code relevance ranker. Given a query and candidate files, "
        "rank them by relevance. Respond with ONLY a JSON array."
    )

    candidates_desc = []
    for path, info in list(candidates.items())[:15]:
        summary = file_summaries.get(path, "no summary")
        bm25 = info.get("score", 0)
        candidates_desc.append(f"  {path} (bm25={bm25:.2f}): {summary[:100]}")

    user_msg = f"""Query: "{query}"

Candidate files:
{chr(10).join(candidates_desc)}

Rank these files by relevance to the query. Respond with ONLY a JSON array of objects:
[{{"path": "file/path", "score": 0.0-1.0, "reason": "brief"}}]

Order from most to least relevant. Score 1.0 = highly relevant, 0.0 = irrelevant."""

    response = _call_haiku(system, user_msg, MAX_TOKENS_RANKING)
    if not response:
        return [(p, info.get("score", 0)) for p, info in candidates.items()]

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        rankings = json.loads(text)
        result = []
        for item in rankings:
            path = item.get("path", "")
            score = min(1.0, max(0.0, float(item.get("score", 0))))
            if path in candidates:
                result.append((path, score))
        # Add any candidates not ranked by LLM (with original score)
        ranked_paths = {p for p, _ in result}
        for path, info in candidates.items():
            if path not in ranked_paths:
                result.append((path, info.get("score", 0) * 0.5))  # penalize unranked
        return result
    except (json.JSONDecodeError, ValueError, TypeError):
        return [(p, info.get("score", 0)) for p, info in candidates.items()]


# ─── Utility: build repo summary for LLM context ────────────────────────────

def build_repo_summary(repo_map: Dict[str, Any]) -> str:
    """Build a compact text summary of the repo for LLM context."""
    stats = repo_map.get("stats", {})
    files = repo_map.get("files", {})
    symbols = repo_map.get("symbols", {})

    parts = [
        f"Files: {stats.get('total_files', 0)}",
        f"Lines: {stats.get('total_lines', 0)}",
        f"Languages: {stats.get('languages', {})}",
    ]

    # Top directories
    dir_counts: Dict[str, int] = {}
    for path in files:
        d = os.path.dirname(path)
        top = d.split(os.sep)[0] if os.sep in d else (d or "root")
        dir_counts[top] = dir_counts.get(top, 0) + 1
    top_dirs = sorted(dir_counts.items(), key=lambda x: -x[1])[:8]
    parts.append(f"Key dirs: {', '.join(f'{d}({n})' for d, n in top_dirs)}")

    # Top classes/structs
    classes = []
    for key, info in list(symbols.items())[:300]:
        if info.get("type") in ("class", "struct", "interface"):
            name = key.split("::")[-1] if "::" in key else key
            classes.append(name)
    if classes:
        parts.append(f"Key types: {', '.join(classes[:15])}")

    return "\n".join(parts)


def build_file_summaries(repo_map: Dict[str, Any]) -> Dict[str, str]:
    """Build brief text summaries of each file from their signatures."""
    files = repo_map.get("files", {})
    summaries = {}
    for path, info in files.items():
        sigs = info.get("signatures", [])
        if sigs:
            sig_names = [s.get("name", "") for s in sigs[:5]]
            summaries[path] = f"{info.get('lang', '?')} ({info.get('lines', 0)} lines): {', '.join(sig_names)}"
        else:
            summaries[path] = f"{info.get('lang', '?')} ({info.get('lines', 0)} lines)"
    return summaries
