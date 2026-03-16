#!/usr/bin/env python3
"""
TokenScout Hook Commons — Shared state, utilities, and constants.

Implements the shared infrastructure for the TokenScout-inspired hook system
for Claude Code. Based on the TokenScout paper (Li et al., 2026):
  - Semantic-Structural Code Representation
  - Codebase Context Navigation
  - Cost-Aware Context Management

All hooks communicate through a shared JSON state file at:
  $CLAUDE_PROJECT_DIR/.claude/hooks/tokenscout_state.json
"""

import json
import os
import sys
import time
import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ─── Paths ───────────────────────────────────────────────────────────────────

def get_project_dir() -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def state_path() -> str:
    return os.path.join(get_project_dir(), ".claude", "hooks", "tokenscout_state.json")


def log_path() -> str:
    return os.path.join(get_project_dir(), ".claude", "hooks", "tokenscout_audit.jsonl")


# ─── State management ────────────────────────────────────────────────────────

def load_state() -> Dict[str, Any]:
    """Load or initialize the shared TokenScout state."""
    p = state_path()
    if os.path.exists(p):
        try:
            with open(p, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return _default_state()


def save_state(state: Dict[str, Any]) -> None:
    p = state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


def _default_state() -> Dict[str, Any]:
    """
    State vector S_t = {D_q, H_r, L_t, t, κ_t} from paper §3.3.1
    Plus additional bookkeeping for the hook system.
    """
    return {
        # ── Phase 1: Repo representation ──
        "repo_map": {
            "files": {},          # path -> {type, size, lang, signatures:[]}
            "dependencies": {},   # path -> [imported_paths]
            "symbols": {},        # "module.Class.method" -> {path, line, type}
            "stats": {
                "total_files": 0,
                "total_lines": 0,
                "languages": {},
                "depth": 0,
            },
        },

        # ── Phase 3: Cost-aware context management ──
        "context_state": {
            "D_q": 0,            # Query complexity (0-100)
            "H_r": 1.0,          # Repository entropy (0.5-2.0)
            "L_t": 0,            # Cumulative context lines consumed
            "t": 0,              # Iteration depth (tool call count)
            "kappa_t": 0.0,      # Epistemic confidence (0-100)
            "budget": 0,         # Dynamic line budget B ∝ D_q · H_r
            "igr_history": [],   # Information Gain Rate per step
        },

        # ── Bookkeeping ──
        "explored_files": [],    # Files already read in full
        "scouted_files": [],     # Files scouted via metadata only
        "candidates": {},        # path -> {score, provenance, cost}
        "session_start": time.time(),
        "version": "1.0.0",
    }


# ─── Audit logging ───────────────────────────────────────────────────────────

def audit_log(event: str, data: Dict[str, Any]) -> None:
    """Append a structured log entry (one JSON line) for monitoring."""
    entry = {
        "ts": time.time(),
        "event": event,
        **data,
    }
    p = log_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ─── Repo scanning utilities ─────────────────────────────────────────────────

LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript",
    ".java": "java", ".go": "go",
    ".rs": "rust", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".kt": "kotlin",
}

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".tox", ".mypy_cache", ".pytest_cache", "env", ".env",
    "site-packages", "coverage", ".coverage", ".idea", ".vscode",
}

IGNORE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock", "Pipfile.lock",
}


def scan_repo_lightweight(root: str, max_files: int = 5000) -> Dict[str, Any]:
    """
    Phase 1 — Build a lightweight semantic-structural map.

    Corresponds to TokenScout §3.1 (Hierarchical Code Units + Symbol-Aware Relation Modeling).
    Extracts file metadata and function/class signatures WITHOUT reading full file bodies.
    """
    files = {}
    dependencies = {}
    symbols = {}
    lang_counts = {}
    total_lines = 0
    max_depth = 0
    count = 0

    root = os.path.abspath(root)

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune ignored directories
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        max_depth = max(max_depth, depth)

        for fname in filenames:
            if fname in IGNORE_FILES or fname.startswith("."):
                continue

            ext = os.path.splitext(fname)[1].lower()
            lang = LANG_MAP.get(ext)
            if lang is None:
                continue

            count += 1
            if count > max_files:
                break

            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, root)

            try:
                size = os.path.getsize(fpath)
            except OSError:
                size = 0

            # Extract lightweight metadata (signatures only)
            sigs, imports, line_count = _extract_signatures(fpath, lang)
            total_lines += line_count
            lang_counts[lang] = lang_counts.get(lang, 0) + 1

            files[rel] = {
                "lang": lang,
                "size": size,
                "lines": line_count,
                "signatures": sigs[:50],  # cap per file
            }

            if imports:
                dependencies[rel] = imports

            for sig in sigs:
                sym_key = f"{rel}::{sig['name']}"
                symbols[sym_key] = {
                    "path": rel,
                    "line": sig.get("line", 0),
                    "type": sig.get("type", "unknown"),
                    "signature": sig.get("signature", ""),
                }

        if count > max_files:
            break

    # Compute repository entropy H_r ∈ [0.5, 2.0]
    # Based on file count, code depth, and language diversity
    n_files = len(files)
    n_langs = len(lang_counts)
    entropy = min(2.0, max(0.5,
        0.5 + 0.3 * min(n_files / 500, 1.0)
        + 0.2 * min(max_depth / 10, 1.0)
        + 0.5 * min(n_langs / 4, 1.0)
    ))

    return {
        "files": files,
        "dependencies": dependencies,
        "symbols": symbols,
        "stats": {
            "total_files": n_files,
            "total_lines": total_lines,
            "languages": lang_counts,
            "depth": max_depth,
            "entropy": round(entropy, 3),
        },
    }


def _extract_signatures(fpath: str, lang: str) -> Tuple[List[Dict], List[str], int]:
    """
    Extract function/class signatures and import statements from a file.
    Reads the file but only keeps lightweight metadata — NOT full bodies.

    This implements TokenScout §3.1.1 (Hierarchical Code Units) — extracting
    type signatures, docstrings, and line ranges as navigable landmarks.
    """
    sigs: List[Dict] = []
    imports: List[str] = []
    line_count = 0

    try:
        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        line_count = len(lines)
    except (IOError, OSError):
        return sigs, imports, 0

    if lang == "python":
        sigs, imports = _parse_python_sigs(lines)
    elif lang in ("javascript", "typescript"):
        sigs, imports = _parse_js_ts_sigs(lines)
    elif lang == "java":
        sigs, imports = _parse_java_sigs(lines)
    elif lang == "go":
        sigs, imports = _parse_go_sigs(lines)
    elif lang == "rust":
        sigs, imports = _parse_rust_sigs(lines)
    else:
        # Fallback: regex-based generic extraction
        sigs, imports = _parse_generic_sigs(lines)

    return sigs, imports, line_count


# ─── Language-specific parsers (lightweight, regex-based) ─────────────────────

def _parse_python_sigs(lines: List[str]) -> Tuple[List[Dict], List[str]]:
    sigs, imports = [], []
    current_class = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Imports
        if stripped.startswith("import ") or stripped.startswith("from "):
            m = re.match(r'(?:from\s+(\S+)\s+)?import\s+(.+)', stripped)
            if m:
                mod = m.group(1) or m.group(2).split(",")[0].strip().split(" as ")[0]
                imports.append(mod)
        # Class
        elif stripped.startswith("class "):
            m = re.match(r'class\s+(\w+)(\([^)]*\))?:', stripped)
            if m:
                current_class = m.group(1)
                bases = m.group(2) or ""
                sigs.append({
                    "name": current_class,
                    "type": "class",
                    "line": i + 1,
                    "signature": f"class {current_class}{bases}",
                })
        # Function / method
        elif re.match(r'\s*(?:async\s+)?def\s+', stripped):
            m = re.match(r'\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)', stripped)
            if m:
                fname = m.group(1)
                params = m.group(2).strip()
                indent = len(line) - len(line.lstrip())
                sig_type = "method" if indent > 0 and current_class else "function"
                name = f"{current_class}.{fname}" if sig_type == "method" and current_class else fname
                sigs.append({
                    "name": name,
                    "type": sig_type,
                    "line": i + 1,
                    "signature": f"def {fname}({params})",
                })
        # Reset class scope on dedent
        elif current_class and not stripped.startswith("#") and stripped and not line[0].isspace():
            if not stripped.startswith("class ") and not stripped.startswith("def "):
                current_class = None
    return sigs, imports


def _parse_js_ts_sigs(lines: List[str]) -> Tuple[List[Dict], List[str]]:
    sigs, imports = [], []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Imports
        if stripped.startswith("import "):
            m = re.search(r'from\s+["\']([^"\']+)["\']', stripped)
            if m:
                imports.append(m.group(1))
        # Function
        m = re.match(r'(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)', stripped)
        if m:
            sigs.append({
                "name": m.group(1), "type": "function",
                "line": i + 1, "signature": f"function {m.group(1)}({m.group(2)})",
            })
            continue
        # Arrow / const function
        m = re.match(r'(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', stripped)
        if m:
            sigs.append({
                "name": m.group(1), "type": "function",
                "line": i + 1, "signature": f"const {m.group(1)} = (...)",
            })
            continue
        # Class
        m = re.match(r'(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?', stripped)
        if m:
            extends = f" extends {m.group(2)}" if m.group(2) else ""
            sigs.append({
                "name": m.group(1), "type": "class",
                "line": i + 1, "signature": f"class {m.group(1)}{extends}",
            })
    return sigs, imports


def _parse_java_sigs(lines: List[str]) -> Tuple[List[Dict], List[str]]:
    sigs, imports = [], []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            m = re.match(r'import\s+(?:static\s+)?([^;]+);', stripped)
            if m:
                imports.append(m.group(1))
        m = re.match(r'(?:public|private|protected)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "class", "line": i + 1, "signature": stripped.rstrip("{").strip()})
            continue
        m = re.match(r'(?:public|private|protected)\s+(?:static\s+)?(?:\w+(?:<[^>]+>)?)\s+(\w+)\s*\(([^)]*)\)', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "method", "line": i + 1, "signature": stripped.rstrip("{").strip()})
    return sigs, imports


def _parse_go_sigs(lines: List[str]) -> Tuple[List[Dict], List[str]]:
    sigs, imports = [], []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import "):
            m = re.search(r'"([^"]+)"', stripped)
            if m:
                imports.append(m.group(1))
        # Go type declarations: type Foo struct/interface
        m = re.match(r'type\s+(\w+)\s+(struct|interface)\b', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": m.group(2), "line": i + 1, "signature": stripped.split("{")[0].strip()})
            continue
        # Go functions and methods
        m = re.match(r'func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)', stripped)
        if m:
            receiver = m.group(1)
            fname = m.group(2)
            name = f"{receiver}.{fname}" if receiver else fname
            sigs.append({"name": name, "type": "method" if receiver else "function", "line": i + 1, "signature": stripped.split("{")[0].strip()})
    return sigs, imports


def _parse_rust_sigs(lines: List[str]) -> Tuple[List[Dict], List[str]]:
    sigs, imports = [], []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("use "):
            m = re.match(r'use\s+([^;]+);', stripped)
            if m:
                imports.append(m.group(1))
        m = re.match(r'(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[(<]', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "function", "line": i + 1, "signature": stripped.split("{")[0].strip()})
            continue
        m = re.match(r'(?:pub\s+)?struct\s+(\w+)', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "struct", "line": i + 1, "signature": stripped.split("{")[0].strip()})
        m = re.match(r'(?:pub\s+)?enum\s+(\w+)', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "enum", "line": i + 1, "signature": stripped.split("{")[0].strip()})
    return sigs, imports


def _parse_generic_sigs(lines: List[str]) -> Tuple[List[Dict], List[str]]:
    sigs, imports = [], []
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = re.match(r'(?:function|def|fn|func|sub|proc)\s+(\w+)', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "function", "line": i + 1, "signature": stripped[:120]})
        m = re.match(r'(?:class|struct|enum|interface|trait|type)\s+(\w+)', stripped)
        if m:
            sigs.append({"name": m.group(1), "type": "class", "line": i + 1, "signature": stripped[:120]})
    return sigs, imports


# ─── Cost-aware helpers (§3.3) ────────────────────────────────────────────────

def compute_budget(D_q: float, H_r: float, base: int = 2000) -> int:
    """
    Dynamic line budget: B ∝ D_q · H_r  (paper §3.3.2)
    base scales it to practical line counts.
    """
    return int(base * max(1, D_q / 50) * H_r)


def information_gain_rate(kappa_t: float, kappa_prev: float, L_t: int, L_prev: int) -> float:
    """
    IGR_t = (κ_t − κ_{t−1}) / (L_t − L_{t−1})   (paper §3.3.2)
    Confidence improvement per unit of context expansion.
    """
    delta_L = L_t - L_prev
    if delta_L <= 0:
        return 0.0
    return (kappa_t - kappa_prev) / delta_L


def should_terminate(state: Dict[str, Any], tau: float = 70.0, epsilon: float = 0.01) -> Tuple[bool, str]:
    """
    Termination conditions from paper §3.3.2:
    1. Sufficiency:  κ_t ≥ τ
    2. Inefficiency:  consecutive low IGR (< ε)
    3. Exhaustion:    projected cost > remaining budget (B − L_t)
    """
    ctx = state["context_state"]
    kappa = ctx["kappa_t"]
    budget = ctx["budget"]
    L_t = ctx["L_t"]
    igr_hist = ctx["igr_history"]

    # 1. Sufficiency
    if kappa >= tau:
        return True, f"sufficient (κ={kappa:.1f} ≥ τ={tau})"

    # 2. Inefficiency: 3 consecutive low IGR
    if len(igr_hist) >= 3:
        recent = igr_hist[-3:]
        if all(abs(r) < epsilon for r in recent):
            return True, f"inefficient (last 3 IGR < ε={epsilon})"

    # 3. Exhaustion
    if budget > 0 and L_t >= budget:
        return True, f"exhausted (L_t={L_t} ≥ B={budget})"

    return False, "continue"


def priority_score(
    relevance: float,
    tool_confirmed: bool,
    density: float,
    w1: float = 0.5,
    w2: float = 0.3,
    w3: float = 0.2,
) -> float:
    """
    Priority selection P(u) = w1·Rel(u) + w2·𝟙_tool(u) + w3·Density(u)
    (paper §3.3.2, Equation 2)
    """
    return w1 * relevance + w2 * (1.0 if tool_confirmed else 0.0) + w3 * density


# ─── Stdin helper ─────────────────────────────────────────────────────────────

def read_hook_input() -> Dict[str, Any]:
    """Read JSON from stdin (provided by Claude Code to hooks)."""
    try:
        return json.load(sys.stdin)
    except (json.JSONDecodeError, IOError):
        return {}
