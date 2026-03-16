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
            "dependencies": {},   # G_dep: path -> [imported_modules]
            "inheritance": {},    # G_inh: class -> {bases:[], subclasses:[], path}
            "call_graph": {},     # G_call: func -> [called_funcs]
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

    # ── Build 3-layer graph (§3.1.3 Symbol-Aware Relation Modeling) ──
    # G = {G_dep, G_inh, G_call}
    inheritance = _build_inheritance_graph(files, symbols)
    call_graph = _build_call_graph(files, symbols)

    return {
        "files": files,
        "dependencies": dependencies,    # G_dep: import relationships
        "inheritance": inheritance,       # G_inh: class hierarchy
        "call_graph": call_graph,         # G_call: function invocations
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


# ─── Graph builders (§3.1.3 Symbol-Aware Relation Modeling) ──────────────

def _build_inheritance_graph(files: Dict, symbols: Dict) -> Dict[str, Any]:
    """
    G_inh: Maps class hierarchy — base classes and subclasses.
    Extracts from signature patterns like:
      Python:  class Foo(Bar, Baz):
      JS/TS:   class Foo extends Bar
      Java:    class Foo extends Bar implements Baz
      Go:      (embedded structs detected from signatures)
      Rust:    impl Trait for Struct
    """
    graph: Dict[str, Dict] = {}
    # class_name -> {path, bases:[], subclasses:[]}

    # Pass 1: collect all classes and their declared bases
    for path, info in files.items():
        for sig in info.get("signatures", []):
            if sig.get("type") not in ("class", "struct", "interface"):
                continue
            name = sig["name"]
            sig_str = sig.get("signature", "")
            bases = _extract_bases(sig_str, info.get("lang", ""))

            if name not in graph:
                graph[name] = {"path": path, "bases": [], "subclasses": [], "line": sig.get("line", 0)}
            graph[name]["bases"] = bases
            graph[name]["path"] = path

    # Pass 2: fill in reverse edges (subclasses)
    for cls, info in graph.items():
        for base in info["bases"]:
            if base in graph:
                if cls not in graph[base]["subclasses"]:
                    graph[base]["subclasses"].append(cls)

    return graph


def _extract_bases(signature: str, lang: str) -> List[str]:
    """Extract base classes/interfaces from a class signature."""
    bases = []

    if lang == "python":
        # class Foo(Bar, Baz):
        m = re.search(r'class\s+\w+\(([^)]+)\)', signature)
        if m:
            raw = m.group(1)
            for b in raw.split(","):
                b = b.strip().split("[")[0].split("(")[0]  # strip generics
                if b and b not in ("object", "ABC", "metaclass="):
                    if not b.startswith("metaclass"):
                        bases.append(b)

    elif lang in ("javascript", "typescript"):
        # class Foo extends Bar implements Baz
        m = re.search(r'extends\s+(\w+)', signature)
        if m:
            bases.append(m.group(1))
        m = re.search(r'implements\s+([\w,\s]+)', signature)
        if m:
            for b in m.group(1).split(","):
                b = b.strip()
                if b:
                    bases.append(b)

    elif lang == "java":
        m = re.search(r'extends\s+(\w+)', signature)
        if m:
            bases.append(m.group(1))
        m = re.search(r'implements\s+([\w,\s]+)', signature)
        if m:
            for b in m.group(1).split(","):
                b = b.strip()
                if b:
                    bases.append(b)

    elif lang == "rust":
        # impl Trait for Struct / : Trait
        m = re.search(r':\s*([\w\s+<>]+)', signature)
        if m:
            for b in m.group(1).split("+"):
                b = b.strip().split("<")[0]
                if b:
                    bases.append(b)

    return bases


def _build_call_graph(files: Dict, symbols: Dict) -> Dict[str, Any]:
    """
    G_call: Lightweight call graph — maps functions to called symbols.
    Extracts function/method calls via regex from signatures and file structure.
    Since we only have signatures (not full bodies), we extract calls from:
      1. Default arguments and decorators in signatures
      2. Known patterns (super().__init__, self.method, etc.)
      3. Cross-reference symbols: if a function name appears in another file's signatures
    """
    graph: Dict[str, List[str]] = {}

    # Build symbol lookup: name -> [paths]
    symbol_lookup: Dict[str, List[str]] = {}
    for sym_key, info in symbols.items():
        name = sym_key.split("::")[-1] if "::" in sym_key else sym_key
        base_name = name.split(".")[-1] if "." in name else name
        if base_name not in symbol_lookup:
            symbol_lookup[base_name] = []
        symbol_lookup[base_name].append(info["path"])

    # For each function/method, find likely callees
    for path, info in files.items():
        for sig in info.get("signatures", []):
            if sig.get("type") not in ("function", "method"):
                continue

            caller_key = f"{path}::{sig['name']}"
            callees = set()

            sig_str = sig.get("signature", "")

            # Extract function calls from signatures (defaults, decorators, type hints)
            calls_in_sig = re.findall(r'(\w+)\s*\(', sig_str)
            for call in calls_in_sig:
                if call in ("def", "async", "return", "if", "for", "while", "print",
                            "len", "str", "int", "float", "bool", "list", "dict",
                            "set", "tuple", "range", "type", "super", "self"):
                    continue
                if call in symbol_lookup:
                    for callee_path in symbol_lookup[call]:
                        if callee_path != path:  # cross-file calls are most interesting
                            callees.add(f"{callee_path}::{call}")

            # For methods: check if class has known base → likely calls super methods
            if sig.get("type") == "method" and "." in sig["name"]:
                cls_name = sig["name"].split(".")[0]
                method_name = sig["name"].split(".")[-1]
                # Check if same method exists in other classes (potential override/call)
                if method_name in symbol_lookup:
                    for callee_path in symbol_lookup[method_name]:
                        if callee_path != path:
                            callees.add(f"{callee_path}::{method_name}")

            if callees:
                graph[caller_key] = sorted(callees)[:10]  # cap per function

    return graph


def find_related_via_graphs(target_path: str, repo_map: Dict, max_hops: int = 2) -> Dict[str, Dict]:
    """
    Graph expansion (§3.2.2): find files connected via ALL 3 graph layers.
    Returns {path: {relation_type, distance, via}} for related files.

    Traces:
      G_dep: import dependencies (forward + reverse)
      G_inh: inheritance hierarchy (superclasses + subclasses)
      G_call: function call targets and callers
    """
    related: Dict[str, Dict] = {}
    deps = repo_map.get("dependencies", {})
    inheritance = repo_map.get("inheritance", {})
    call_graph = repo_map.get("call_graph", {})
    files = repo_map.get("files", {})
    symbols = repo_map.get("symbols", {})

    # ── G_dep: dependency layer ──
    # Forward: files that target imports
    for dep in deps.get(target_path, []):
        for path in files:
            if path == target_path:
                continue
            if dep in path or any(dep in s.get("name", "") for s in files[path].get("signatures", [])):
                if path not in related:
                    related[path] = {"relation": "imports", "distance": 1, "via": dep}

    # Reverse: files that import target
    target_stem = os.path.splitext(target_path)[0].replace(os.sep, ".")
    target_name = os.path.splitext(os.path.basename(target_path))[0]
    for path, path_deps in deps.items():
        if path == target_path:
            continue
        for d in path_deps:
            if target_name == d or target_stem.endswith(d) or d.endswith(target_name):
                if path not in related:
                    related[path] = {"relation": "imported_by", "distance": 1, "via": d}
                break

    # ── G_inh: inheritance layer ──
    # Find classes defined in target_path
    target_classes = set()
    for sig in files.get(target_path, {}).get("signatures", []):
        if sig.get("type") in ("class", "struct", "interface"):
            target_classes.add(sig["name"])

    for cls_name in target_classes:
        cls_info = inheritance.get(cls_name, {})
        # Superclasses
        for base in cls_info.get("bases", []):
            base_info = inheritance.get(base, {})
            base_path = base_info.get("path", "")
            if base_path and base_path != target_path and base_path not in related:
                related[base_path] = {"relation": "superclass", "distance": 1, "via": f"{cls_name} extends {base}"}
        # Subclasses
        for sub in cls_info.get("subclasses", []):
            sub_info = inheritance.get(sub, {})
            sub_path = sub_info.get("path", "")
            if sub_path and sub_path != target_path and sub_path not in related:
                related[sub_path] = {"relation": "subclass", "distance": 1, "via": f"{sub} extends {cls_name}"}

    # ── G_call: call graph layer ──
    # Functions in target that call things in other files
    for sym_key, callees in call_graph.items():
        caller_path = sym_key.split("::")[0] if "::" in sym_key else ""
        if caller_path == target_path:
            for callee_key in callees:
                callee_path = callee_key.split("::")[0] if "::" in callee_key else ""
                if callee_path and callee_path != target_path and callee_path not in related:
                    related[callee_path] = {"relation": "calls", "distance": 1, "via": callee_key.split("::")[-1]}

    # Reverse: functions in other files that call things in target
    target_funcs = set()
    for sig in files.get(target_path, {}).get("signatures", []):
        if sig.get("type") in ("function", "method"):
            target_funcs.add(sig["name"].split(".")[-1])

    for sym_key, callees in call_graph.items():
        caller_path = sym_key.split("::")[0] if "::" in sym_key else ""
        if caller_path == target_path:
            continue
        for callee_key in callees:
            callee_name = callee_key.split("::")[-1] if "::" in callee_key else callee_key
            callee_base = callee_name.split(".")[-1] if "." in callee_name else callee_name
            callee_path = callee_key.split("::")[0] if "::" in callee_key else ""
            if callee_path == target_path or callee_base in target_funcs or callee_name in target_funcs:
                # Upgrade relation if already present from weaker layer (imports → called_by)
                if caller_path not in related or related[caller_path]["relation"] in ("imports", "imported_by"):
                    related[caller_path] = {"relation": "called_by", "distance": 1, "via": callee_name}

    # Sort by relevance: inheritance > calls > imports
    priority = {"superclass": 0, "subclass": 0, "calls": 1, "called_by": 1,
                "imports": 2, "imported_by": 2}
    return dict(sorted(related.items(), key=lambda x: priority.get(x[1]["relation"], 3))[:15])


# ─── Confidence estimation (§3.3.1) ─────────────────────────────────────────

def estimate_confidence_boost(
    file_path: str,
    state: Dict[str, Any],
    tool_name: str = "Read",
    tool_input: Optional[Dict] = None,
) -> float:
    """
    Improved epistemic confidence update based on multiple signals.
    More nuanced than flat heuristic — considers:
      1. Whether file was a scouting candidate (and its score)
      2. Coverage: what fraction of candidates have been explored
      3. Graph centrality: files with more connections are more informative
      4. Diminishing returns: each successive file adds less confidence
    """
    candidates = state.get("candidates", {})
    explored = state.get("explored_files", [])
    repo_map = state.get("repo_map", {})
    ctx = state.get("context_state", {})

    if tool_name == "Read":
        rel_path = file_path
        base_boost = 5.0  # default for unknown files

        # Signal 1: candidate score (0-1) → bigger boost for higher-scored files
        if rel_path in candidates:
            score = candidates[rel_path].get("score", 0.5)
            base_boost = 8.0 + score * 12.0  # range: 8-20

        # Signal 2: graph centrality — files connected to many others are more informative
        inh = repo_map.get("inheritance", {})
        call = repo_map.get("call_graph", {})
        deps = repo_map.get("dependencies", {})
        connections = 0
        connections += len(deps.get(rel_path, []))
        # Count how many call graph entries reference this file
        for key in call:
            if key.startswith(rel_path + "::"):
                connections += len(call[key])
        # Inheritance connections
        for cls, info in inh.items():
            if info.get("path") == rel_path:
                connections += len(info.get("bases", [])) + len(info.get("subclasses", []))
        centrality_bonus = min(5.0, connections * 0.5)
        base_boost += centrality_bonus

        # Signal 3: diminishing returns — each file adds less
        n_explored = len(explored)
        diminish_factor = 1.0 / (1.0 + n_explored * 0.15)
        base_boost *= diminish_factor

        # Signal 4: coverage boost — exploring a larger fraction of candidates
        if candidates:
            explored_set = set(explored)
            coverage = len(explored_set & set(candidates.keys())) / len(candidates)
            if coverage > 0.8:
                base_boost += 5.0  # explored most candidates → high confidence
            elif coverage > 0.5:
                base_boost += 2.0

        # Signal 5: re-read penalty
        if rel_path in explored:
            base_boost = max(0.5, base_boost * 0.1)  # re-read is almost no new info

        return round(min(25.0, base_boost), 2)

    elif tool_name == "Grep":
        # Grep narrows search — moderate boost, scales down with iteration
        n = ctx.get("t", 0)
        return round(max(1.0, 5.0 / (1 + n * 0.1)), 2)

    elif tool_name == "Glob":
        return 1.0

    elif tool_name == "Bash":
        command = (tool_input or {}).get("command", "")
        if any(kw in command for kw in ["test", "pytest", "jest", "cargo test", "go test"]):
            return 10.0  # running tests = high confidence signal
        elif any(kw in command for kw in ["grep", "find", "rg", "ag", "fd"]):
            return 3.0
        return 2.0

    return 1.0


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


# ─── BM25 Sparse Retrieval (§3.2.1) ────────────────────────────────────────

import math

class BM25Index:
    """
    BM25 (Best Matching 25) sparse retrieval over code files.

    Implements the Okapi BM25 ranking function from the FastCode paper §3.2.1.
    Indexes file signatures, paths, and symbol names to score relevance
    against a search query.

    Formula: score(D, Q) = Σ IDF(q_i) · (tf(q_i, D) · (k1+1)) / (tf(q_i, D) + k1 · (1 - b + b · |D|/avgdl))

    Parameters:
      k1 = 1.5 (term frequency saturation)
      b  = 0.75 (length normalization)
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_count = 0
        self.avg_dl = 0.0
        self.doc_lens: Dict[str, int] = {}           # path -> doc length (in terms)
        self.doc_tf: Dict[str, Dict[str, int]] = {}   # path -> {term: count}
        self.df: Dict[str, int] = {}                  # term -> num docs containing it
        self.corpus_indexed = False

    def index_repo(self, repo_map: Dict[str, Any]) -> None:
        """
        Build BM25 index from repo_map.
        Each 'document' is a file represented by its path segments + signature names + import names.
        """
        files = repo_map.get("files", {})
        deps = repo_map.get("dependencies", {})

        self.doc_count = 0
        self.avg_dl = 0.0
        self.doc_lens = {}
        self.doc_tf = {}
        self.df = {}
        total_len = 0

        for path, info in files.items():
            terms = self._tokenize_file(path, info, deps.get(path, []))
            if not terms:
                continue

            self.doc_count += 1
            tf: Dict[str, int] = {}
            for t in terms:
                tf[t] = tf.get(t, 0) + 1

            self.doc_tf[path] = tf
            self.doc_lens[path] = len(terms)
            total_len += len(terms)

            for t in tf:
                self.df[t] = self.df.get(t, 0) + 1

        self.avg_dl = total_len / max(1, self.doc_count)
        self.corpus_indexed = True

    def query(self, query_text: str, top_k: int = 20) -> List[Tuple[str, float]]:
        """
        Score all documents against a query, return top-k (path, score) pairs.
        """
        if not self.corpus_indexed or self.doc_count == 0:
            return []

        query_terms = self._tokenize_query(query_text)
        if not query_terms:
            return []

        scores: Dict[str, float] = {}

        for term in query_terms:
            if term not in self.df:
                continue
            # IDF = ln((N - df + 0.5) / (df + 0.5) + 1)
            idf = math.log((self.doc_count - self.df[term] + 0.5) / (self.df[term] + 0.5) + 1.0)

            for path, tf_map in self.doc_tf.items():
                if term not in tf_map:
                    continue
                tf = tf_map[term]
                dl = self.doc_lens[path]
                # BM25 term score
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self.avg_dl)
                scores[path] = scores.get(path, 0.0) + idf * numerator / denominator

        ranked = sorted(scores.items(), key=lambda x: -x[1])
        return ranked[:top_k]

    def _tokenize_file(self, path: str, info: Dict, imports: List[str]) -> List[str]:
        """Convert a file entry into a bag of terms for indexing."""
        terms = []

        # Path segments (split by / and .)
        for part in re.split(r'[/\\._\-]', path.lower()):
            if part and len(part) > 1:
                terms.append(part)
                # Also add camelCase splits
                terms.extend(self._split_camel(part))

        # Signature names
        for sig in info.get("signatures", []):
            name = sig.get("name", "").lower()
            for part in re.split(r'[._]', name):
                if part and len(part) > 1:
                    terms.append(part)
                    terms.extend(self._split_camel(part))

            # Signature text keywords
            sig_str = sig.get("signature", "").lower()
            for word in re.findall(r'[a-z_][a-z0-9_]+', sig_str):
                if len(word) > 2:
                    terms.append(word)

        # Import names
        for imp in imports:
            for part in re.split(r'[./\\]', imp.lower()):
                if part and len(part) > 1:
                    terms.append(part)

        # Language
        lang = info.get("lang", "")
        if lang:
            terms.append(lang)

        return terms

    def _tokenize_query(self, query: str) -> List[str]:
        """Tokenize a search query into terms."""
        terms = []
        for word in re.findall(r'[a-z_][a-z0-9_]+', query.lower()):
            if len(word) > 1:
                terms.append(word)
                terms.extend(self._split_camel(word))
        return terms

    @staticmethod
    def _split_camel(word: str) -> List[str]:
        """Split camelCase/PascalCase into subwords."""
        parts = re.findall(r'[a-z]+|[A-Z][a-z]*', word)
        return [p.lower() for p in parts if len(p) > 1]
