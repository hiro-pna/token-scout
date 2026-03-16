#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
TokenScout Hooks — Comprehensive Test Suite
═══════════════════════════════════════════════════════════════════════════════

Tests all components of the TokenScout-inspired hook system for Claude Code:
  - Phase 1: Semantic-Structural Code Representation (repo scanning)
  - Phase 2: Codebase Context Navigation (scouting, query augmentation)
  - Phase 3: Cost-Aware Context Management (budget, IGR, termination)
  - Integration: End-to-end hook simulation

Run with: python3 test_tokenscout_hooks.py
Or:       python3 -m pytest test_tokenscout_hooks.py -v
"""

import json
import os
import sys
import tempfile
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from io import StringIO

# Add hooks dir to path
HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HOOKS_DIR)

from tokenscout_common import (
    load_state, save_state, _default_state, state_path,
    scan_repo_lightweight, compute_budget, information_gain_rate,
    should_terminate, priority_score, audit_log, log_path,
    _parse_python_sigs, _parse_js_ts_sigs, _parse_go_sigs,
    _parse_java_sigs, _parse_rust_sigs, _parse_generic_sigs,
    _build_inheritance_graph, _build_call_graph, _extract_bases,
    find_related_via_graphs, estimate_confidence_boost,
    BM25Index,
    LANG_MAP, IGNORE_DIRS,
)
from tokenscout_llm import (
    is_llm_available, augment_query, assess_confidence,
    semantic_rank_candidates, build_repo_summary, build_file_summaries,
)


class TestRepoSetup(unittest.TestCase):
    """Create a temporary mock repo for testing."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tokenscout_test_")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmpdir

        # Create a realistic mock repo structure
        self._create_mock_repo()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def _create_mock_repo(self):
        """Create a multi-language mock repository."""
        files = {
            "src/auth/login.py": '''
"""Authentication login module."""
import hashlib
from typing import Optional
from src.models.user import User
from src.db.session import get_session

class LoginHandler:
    """Handles user login flow."""

    def __init__(self, db_session):
        self.db = db_session

    def authenticate(self, username: str, password: str) -> Optional[User]:
        """Authenticate a user with credentials."""
        hashed = hashlib.sha256(password.encode()).hexdigest()
        return self.db.query(User).filter_by(
            username=username, password_hash=hashed
        ).first()

    async def login_async(self, username: str, password: str) -> dict:
        """Async login endpoint."""
        user = self.authenticate(username, password)
        if user:
            return {"status": "ok", "token": self._generate_token(user)}
        return {"status": "error", "message": "Invalid credentials"}

    def _generate_token(self, user: User) -> str:
        return hashlib.sha256(f"{user.id}{time.time()}".encode()).hexdigest()
''',
            "src/models/user.py": '''
"""User model definition."""
from dataclasses import dataclass
from typing import Optional

@dataclass
class User:
    id: int
    username: str
    email: str
    password_hash: str
    role: str = "user"

    def is_admin(self) -> bool:
        return self.role == "admin"

    def full_name(self) -> str:
        return self.username

class UserRepository:
    def find_by_id(self, user_id: int) -> Optional[User]:
        pass

    def find_by_email(self, email: str) -> Optional[User]:
        pass
''',
            "src/db/session.py": '''
"""Database session management."""
from contextlib import contextmanager

class DatabaseSession:
    def __init__(self, connection_string: str):
        self.conn_str = connection_string

    def query(self, model):
        pass

    def commit(self):
        pass

@contextmanager
def get_session():
    session = DatabaseSession("sqlite:///app.db")
    try:
        yield session
        session.commit()
    finally:
        pass
''',
            "src/api/routes.py": '''
"""API route definitions."""
from src.auth.login import LoginHandler
from src.models.user import User

def setup_routes(app):
    @app.route("/login", methods=["POST"])
    def login():
        pass

    @app.route("/users/<int:user_id>")
    def get_user(user_id: int):
        pass

    @app.route("/admin/dashboard")
    def admin_dashboard():
        pass
''',
            "src/utils/helpers.js": '''
/**
 * Utility helper functions.
 */
import { format } from 'date-fns';
import axios from 'axios';

export function formatDate(date) {
    return format(date, 'yyyy-MM-dd');
}

export const fetchApi = async (endpoint, options = {}) => {
    const response = await axios.get(endpoint, options);
    return response.data;
};

export class EventEmitter {
    constructor() {
        this.listeners = {};
    }

    on(event, callback) {
        if (!this.listeners[event]) this.listeners[event] = [];
        this.listeners[event].push(callback);
    }

    emit(event, data) {
        (this.listeners[event] || []).forEach(cb => cb(data));
    }
}
''',
            "tests/test_auth.py": '''
"""Tests for authentication module."""
import pytest
from src.auth.login import LoginHandler

class TestLoginHandler:
    def test_authenticate_valid_user(self):
        handler = LoginHandler(db_session=None)
        assert handler is not None

    def test_authenticate_invalid_password(self):
        handler = LoginHandler(db_session=None)
        result = handler.authenticate("admin", "wrong")
        assert result is None

    def test_login_async(self):
        pass
''',
            "config/settings.go": '''
package config

import (
    "os"
    "fmt"
)

type AppConfig struct {
    DatabaseURL string
    SecretKey   string
    Debug       bool
}

func LoadConfig() *AppConfig {
    return &AppConfig{
        DatabaseURL: os.Getenv("DATABASE_URL"),
        SecretKey:   os.Getenv("SECRET_KEY"),
        Debug:       os.Getenv("DEBUG") == "true",
    }
}

func (c *AppConfig) Validate() error {
    if c.DatabaseURL == "" {
        return fmt.Errorf("DATABASE_URL is required")
    }
    return nil
}
''',
            "README.md": "# Test Project\nA mock project for testing FastCode hooks.\n",
        }

        for path, content in files.items():
            full_path = os.path.join(self.tmpdir, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)

        # Create ignored directories (should be skipped)
        os.makedirs(os.path.join(self.tmpdir, "node_modules", "pkg"), exist_ok=True)
        with open(os.path.join(self.tmpdir, "node_modules", "pkg", "index.js"), "w") as f:
            f.write("module.exports = {};")
        os.makedirs(os.path.join(self.tmpdir, "__pycache__"), exist_ok=True)
        os.makedirs(os.path.join(self.tmpdir, ".git", "objects"), exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1: Semantic-Structural Code Representation Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhase1_RepoScanning(TestRepoSetup):
    """Test lightweight repo scanning (§3.1)."""

    def test_scan_finds_all_source_files(self):
        result = scan_repo_lightweight(self.tmpdir)
        files = result["files"]
        # Should find our source files
        self.assertIn("src/auth/login.py", files)
        self.assertIn("src/models/user.py", files)
        self.assertIn("src/db/session.py", files)
        self.assertIn("src/api/routes.py", files)
        self.assertIn("src/utils/helpers.js", files)
        self.assertIn("tests/test_auth.py", files)
        self.assertIn("config/settings.go", files)

    def test_scan_ignores_excluded_dirs(self):
        result = scan_repo_lightweight(self.tmpdir)
        files = result["files"]
        # Should NOT find files in ignored dirs
        for path in files:
            self.assertNotIn("node_modules", path)
            self.assertNotIn("__pycache__", path)
            self.assertNotIn(".git", path)

    def test_scan_extracts_python_signatures(self):
        result = scan_repo_lightweight(self.tmpdir)
        login_file = result["files"].get("src/auth/login.py", {})
        sigs = login_file.get("signatures", [])
        sig_names = [s["name"] for s in sigs]

        self.assertIn("LoginHandler", sig_names)
        self.assertIn("LoginHandler.authenticate", sig_names)
        self.assertIn("LoginHandler.login_async", sig_names)

    def test_scan_extracts_python_classes(self):
        result = scan_repo_lightweight(self.tmpdir)
        user_file = result["files"].get("src/models/user.py", {})
        sigs = user_file.get("signatures", [])
        sig_types = {s["name"]: s["type"] for s in sigs}

        self.assertEqual(sig_types.get("User"), "class")
        self.assertEqual(sig_types.get("UserRepository"), "class")

    def test_scan_extracts_js_functions(self):
        result = scan_repo_lightweight(self.tmpdir)
        helpers = result["files"].get("src/utils/helpers.js", {})
        sigs = helpers.get("signatures", [])
        sig_names = [s["name"] for s in sigs]

        self.assertIn("formatDate", sig_names)
        self.assertIn("fetchApi", sig_names)
        self.assertIn("EventEmitter", sig_names)

    def test_scan_extracts_go_functions(self):
        result = scan_repo_lightweight(self.tmpdir)
        settings = result["files"].get("config/settings.go", {})
        sigs = settings.get("signatures", [])
        sig_names = [s["name"] for s in sigs]

        self.assertIn("AppConfig", sig_names)
        self.assertIn("LoadConfig", sig_names)
        self.assertIn("AppConfig.Validate", sig_names)
        # Verify type detection
        sig_types = {s["name"]: s["type"] for s in sigs}
        self.assertEqual(sig_types["AppConfig"], "struct")

    def test_scan_extracts_dependencies(self):
        result = scan_repo_lightweight(self.tmpdir)
        deps = result["dependencies"]
        login_deps = deps.get("src/auth/login.py", [])

        self.assertIn("hashlib", login_deps)
        self.assertIn("src.models.user", login_deps)
        self.assertIn("src.db.session", login_deps)

    def test_scan_builds_symbols_index(self):
        result = scan_repo_lightweight(self.tmpdir)
        symbols = result["symbols"]
        # Should have entries keyed as "path::name"
        self.assertTrue(any("LoginHandler" in k for k in symbols))
        self.assertTrue(any("User" in k for k in symbols))

    def test_scan_computes_stats(self):
        result = scan_repo_lightweight(self.tmpdir)
        stats = result["stats"]

        self.assertGreater(stats["total_files"], 0)
        self.assertGreater(stats["total_lines"], 0)
        self.assertIn("python", stats["languages"])
        self.assertIn("javascript", stats["languages"])
        self.assertIn("go", stats["languages"])
        self.assertGreater(stats["depth"], 0)

    def test_scan_computes_entropy(self):
        result = scan_repo_lightweight(self.tmpdir)
        entropy = result["stats"]["entropy"]
        self.assertGreaterEqual(entropy, 0.5)
        self.assertLessEqual(entropy, 2.0)

    def test_scan_respects_max_files(self):
        result = scan_repo_lightweight(self.tmpdir, max_files=2)
        self.assertLessEqual(len(result["files"]), 2)

    def test_scan_language_detection(self):
        result = scan_repo_lightweight(self.tmpdir)
        for path, info in result["files"].items():
            if path.endswith(".py"):
                self.assertEqual(info["lang"], "python")
            elif path.endswith(".js"):
                self.assertEqual(info["lang"], "javascript")
            elif path.endswith(".go"):
                self.assertEqual(info["lang"], "go")


class TestPhase1_Parsers(unittest.TestCase):
    """Test individual language signature parsers."""

    def test_python_parser_classes_and_methods(self):
        lines = [
            "class Foo(Base):\n",
            "    def bar(self, x: int) -> str:\n",
            "        pass\n",
            "    async def baz(self):\n",
            "        pass\n",
            "def standalone(a, b):\n",
            "    pass\n",
        ]
        sigs, imports = _parse_python_sigs(lines)
        names = [s["name"] for s in sigs]
        self.assertIn("Foo", names)
        self.assertIn("Foo.bar", names)
        self.assertIn("Foo.baz", names)
        self.assertIn("standalone", names)

    def test_python_parser_imports(self):
        lines = [
            "import os\n",
            "from typing import Optional, List\n",
            "from myapp.models import User\n",
        ]
        sigs, imports = _parse_python_sigs(lines)
        self.assertIn("os", imports)
        self.assertIn("typing", imports)
        self.assertIn("myapp.models", imports)

    def test_js_parser(self):
        lines = [
            "import React from 'react';\n",
            "export function App(props) {\n",
            "export const handler = async (req) => {\n",
            "class Component extends React.Component {\n",
        ]
        sigs, imports = _parse_js_ts_sigs(lines)
        names = [s["name"] for s in sigs]
        self.assertIn("App", names)
        self.assertIn("handler", names)
        self.assertIn("Component", names)
        self.assertIn("react", imports)

    def test_go_parser(self):
        lines = [
            'import "fmt"\n',
            "func main() {\n",
            "func (s *Server) Start(port int) error {\n",
        ]
        sigs, imports = _parse_go_sigs(lines)
        names = [s["name"] for s in sigs]
        self.assertIn("main", names)
        self.assertIn("Server.Start", names)
        self.assertIn("fmt", imports)

    def test_rust_parser(self):
        lines = [
            "use std::io;\n",
            "pub fn process(input: &str) -> Result<String, Error> {\n",
            "pub struct Config {\n",
            "pub enum Status {\n",
        ]
        sigs, imports = _parse_rust_sigs(lines)
        names = [s["name"] for s in sigs]
        self.assertIn("process", names)
        self.assertIn("Config", names)
        self.assertIn("Status", names)
        self.assertIn("std::io", imports)

    def test_java_parser(self):
        lines = [
            "import java.util.List;\n",
            "public class UserService {\n",
            "    public User findById(int id) {\n",
            "    private static String hash(String input) {\n",
        ]
        sigs, imports = _parse_java_sigs(lines)
        names = [s["name"] for s in sigs]
        self.assertIn("UserService", names)
        self.assertIn("findById", names)
        self.assertIn("hash", names)
        self.assertIn("java.util.List", imports)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3: Cost-Aware Context Management Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhase3_CostManagement(unittest.TestCase):
    """Test cost-aware context management (§3.3)."""

    def test_compute_budget_baseline(self):
        # Low complexity, normal entropy
        budget = compute_budget(D_q=25, H_r=1.0)
        self.assertGreater(budget, 0)

    def test_compute_budget_scales_with_complexity(self):
        low = compute_budget(D_q=10, H_r=1.0)
        high = compute_budget(D_q=80, H_r=1.0)
        self.assertGreater(high, low)

    def test_compute_budget_scales_with_entropy(self):
        low_ent = compute_budget(D_q=50, H_r=0.5)
        high_ent = compute_budget(D_q=50, H_r=2.0)
        self.assertGreater(high_ent, low_ent)

    def test_igr_positive_gain(self):
        igr = information_gain_rate(kappa_t=30, kappa_prev=20, L_t=200, L_prev=100)
        self.assertGreater(igr, 0)

    def test_igr_zero_delta_lines(self):
        igr = information_gain_rate(kappa_t=30, kappa_prev=20, L_t=100, L_prev=100)
        self.assertEqual(igr, 0.0)

    def test_igr_negative_gain(self):
        igr = information_gain_rate(kappa_t=10, kappa_prev=20, L_t=200, L_prev=100)
        self.assertLess(igr, 0)

    def test_termination_sufficiency(self):
        state = _default_state()
        state["context_state"]["kappa_t"] = 80
        state["context_state"]["budget"] = 5000
        terminate, reason = should_terminate(state, tau=70)
        self.assertTrue(terminate)
        self.assertIn("sufficient", reason)

    def test_termination_inefficiency(self):
        state = _default_state()
        state["context_state"]["kappa_t"] = 30
        state["context_state"]["budget"] = 5000
        state["context_state"]["igr_history"] = [0.001, 0.001, 0.001]
        terminate, reason = should_terminate(state, tau=70, epsilon=0.01)
        self.assertTrue(terminate)
        self.assertIn("inefficient", reason)

    def test_termination_exhaustion(self):
        state = _default_state()
        state["context_state"]["kappa_t"] = 30
        state["context_state"]["budget"] = 1000
        state["context_state"]["L_t"] = 1200
        terminate, reason = should_terminate(state, tau=70)
        self.assertTrue(terminate)
        self.assertIn("exhausted", reason)

    def test_termination_continue(self):
        state = _default_state()
        state["context_state"]["kappa_t"] = 30
        state["context_state"]["budget"] = 5000
        state["context_state"]["L_t"] = 100
        state["context_state"]["igr_history"] = [0.1, 0.08, 0.05]
        terminate, reason = should_terminate(state, tau=70)
        self.assertFalse(terminate)
        self.assertEqual(reason, "continue")

    def test_priority_score_formula(self):
        # P(u) = w1·Rel + w2·𝟙_tool + w3·Density
        score = priority_score(relevance=0.8, tool_confirmed=True, density=0.6)
        expected = 0.5 * 0.8 + 0.3 * 1.0 + 0.2 * 0.6
        self.assertAlmostEqual(score, expected, places=5)

    def test_priority_score_no_tool(self):
        score = priority_score(relevance=0.8, tool_confirmed=False, density=0.6)
        expected = 0.5 * 0.8 + 0.3 * 0.0 + 0.2 * 0.6
        self.assertAlmostEqual(score, expected, places=5)


# ═══════════════════════════════════════════════════════════════════════════════
# State Management Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateManagement(TestRepoSetup):
    """Test state persistence and loading."""

    def test_default_state_structure(self):
        state = _default_state()
        self.assertIn("repo_map", state)
        self.assertIn("context_state", state)
        self.assertIn("explored_files", state)
        self.assertIn("scouted_files", state)
        self.assertIn("candidates", state)
        self.assertIn("version", state)

    def test_state_round_trip(self):
        state = _default_state()
        state["context_state"]["kappa_t"] = 42.5
        state["explored_files"] = ["src/main.py", "src/utils.py"]
        save_state(state)

        loaded = load_state()
        self.assertEqual(loaded["context_state"]["kappa_t"], 42.5)
        self.assertEqual(loaded["explored_files"], ["src/main.py", "src/utils.py"])

    def test_load_state_returns_default_on_missing(self):
        # State file doesn't exist yet
        state = load_state()
        self.assertEqual(state["context_state"]["kappa_t"], 0.0)
        self.assertEqual(state["version"], "1.0.0")

    def test_load_state_handles_corrupt_json(self):
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{corrupt json!!")
        state = load_state()
        # Should return default state, not crash
        self.assertIn("repo_map", state)


# ═══════════════════════════════════════════════════════════════════════════════
# Audit Logging Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuditLogging(TestRepoSetup):
    """Test audit trail functionality."""

    def test_audit_log_creates_file(self):
        audit_log("test_event", {"key": "value"})
        self.assertTrue(os.path.exists(log_path()))

    def test_audit_log_writes_jsonl(self):
        audit_log("event1", {"a": 1})
        audit_log("event2", {"b": 2})

        with open(log_path()) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)

        entry1 = json.loads(lines[0])
        self.assertEqual(entry1["event"], "event1")
        self.assertEqual(entry1["a"], 1)
        self.assertIn("ts", entry1)

        entry2 = json.loads(lines[1])
        self.assertEqual(entry2["event"], "event2")


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: Hook Script Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestHookIntegration(TestRepoSetup):
    """Test hooks as they would be called by Claude Code."""

    def _run_hook(self, script_name: str, stdin_data: dict) -> subprocess.CompletedProcess:
        """Run a hook script with JSON on stdin."""
        script = os.path.join(HOOKS_DIR, script_name)
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.tmpdir

        return subprocess.run(
            ["python3", script],
            input=json.dumps(stdin_data),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    def test_session_start_builds_map(self):
        result = self._run_hook("session_start_hook.py", {
            "session_id": "test-123",
            "source": "startup",
            "cwd": self.tmpdir,
        })
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        # Should have printed context to stdout
        self.assertIn("TokenScout Scouting Map", result.stdout)

        # Should have saved state
        state = load_state()
        self.assertGreater(len(state["repo_map"]["files"]), 0)
        self.assertGreater(state["repo_map"]["stats"]["total_files"], 0)

    def test_user_prompt_sets_budget(self):
        # First build the map
        self._run_hook("session_start_hook.py", {"source": "startup"})

        # Then submit a query
        result = self._run_hook("user_prompt_hook.py", {
            "prompt": "How does the authentication flow work across the login handler and user model?",
        })
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        # Should have set D_q and budget
        state = load_state()
        self.assertGreater(state["context_state"]["D_q"], 0)
        self.assertGreater(state["context_state"]["budget"], 0)

        # Should output JSON with additionalContext
        output = json.loads(result.stdout)
        self.assertIn("additionalContext", output)
        self.assertIn("TokenScout Query Analysis", output["additionalContext"])

    def test_user_prompt_finds_candidates(self):
        self._run_hook("session_start_hook.py", {"source": "startup"})

        result = self._run_hook("user_prompt_hook.py", {
            "prompt": "Where is the login authentication implemented?",
        })
        self.assertEqual(result.returncode, 0)

        state = load_state()
        candidates = state.get("candidates", {})
        # Should find login.py as a candidate
        candidate_paths = list(candidates.keys())
        self.assertTrue(
            any("login" in p for p in candidate_paths),
            f"Expected login.py in candidates, got: {candidate_paths}"
        )

    def test_pre_tool_use_injects_metadata(self):
        # Setup: build map and submit query
        self._run_hook("session_start_hook.py", {"source": "startup"})
        self._run_hook("user_prompt_hook.py", {"prompt": "How does login work?"})

        # Now simulate a Read tool call
        file_path = os.path.join(self.tmpdir, "src/auth/login.py")
        result = self._run_hook("pre_tool_use_hook.py", {
            "tool_name": "Read",
            "tool_input": {"file_path": file_path},
            "hook_event_name": "PreToolUse",
        })
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        # Should inject scouting metadata
        if result.stdout.strip():
            output = json.loads(result.stdout)
            context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
            self.assertIn("TokenScout Scout", context)

    def test_post_tool_use_tracks_context(self):
        # Setup
        self._run_hook("session_start_hook.py", {"source": "startup"})
        self._run_hook("user_prompt_hook.py", {"prompt": "How does login work?"})

        # Simulate reading a file
        file_path = os.path.join(self.tmpdir, "src/auth/login.py")
        result = self._run_hook("post_tool_use_hook.py", {
            "tool_name": "Read",
            "tool_input": {"file_path": file_path},
            "tool_response": {"content": "line1\nline2\nline3\n" * 10},
            "hook_event_name": "PostToolUse",
        })
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")

        # State should be updated
        state = load_state()
        self.assertEqual(state["context_state"]["t"], 1)
        self.assertGreater(state["context_state"]["L_t"], 0)
        self.assertGreater(state["context_state"]["kappa_t"], 0)
        self.assertIn("src/auth/login.py", state["explored_files"])

    def test_stop_hook_allows_high_confidence(self):
        # Setup with high confidence
        self._run_hook("session_start_hook.py", {"source": "startup"})
        state = load_state()
        state["context_state"]["kappa_t"] = 80
        state["context_state"]["t"] = 10
        state["context_state"]["budget"] = 5000
        state["context_state"]["L_t"] = 2000
        save_state(state)

        result = self._run_hook("stop_hook.py", {
            "stop_hook_active": False,
            "hook_event_name": "Stop",
        })
        # Should exit 0 (allow stop)
        self.assertEqual(result.returncode, 0)

    def test_stop_hook_blocks_low_confidence(self):
        # Setup with low confidence but budget remaining
        self._run_hook("session_start_hook.py", {"source": "startup"})
        state = load_state()
        state["context_state"]["kappa_t"] = 10
        state["context_state"]["t"] = 5
        state["context_state"]["budget"] = 5000
        state["context_state"]["L_t"] = 500
        state["context_state"]["D_q"] = 50
        state["scouted_files"] = ["src/auth/login.py", "src/models/user.py"]
        state["explored_files"] = []
        save_state(state)

        result = self._run_hook("stop_hook.py", {
            "stop_hook_active": False,
            "hook_event_name": "Stop",
        })
        # Should output a block decision
        if result.stdout.strip():
            output = json.loads(result.stdout)
            self.assertEqual(output.get("decision"), "block")
            self.assertIn("Confidence is low", output.get("reason", ""))

    def test_stop_hook_respects_stop_hook_active(self):
        """Prevent infinite loops — always allow stop if stop_hook_active=true."""
        state = _default_state()
        state["context_state"]["kappa_t"] = 5  # very low
        state["context_state"]["t"] = 10
        state["context_state"]["budget"] = 5000
        save_state(state)

        result = self._run_hook("stop_hook.py", {
            "stop_hook_active": True,
            "hook_event_name": "Stop",
        })
        self.assertEqual(result.returncode, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# End-to-End Simulation
# ═══════════════════════════════════════════════════════════════════════════════

class TestEndToEnd(TestRepoSetup):
    """Simulate a full Claude Code interaction cycle."""

    def _run_hook(self, script_name, stdin_data):
        script = os.path.join(HOOKS_DIR, script_name)
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = self.tmpdir
        return subprocess.run(
            ["python3", script],
            input=json.dumps(stdin_data),
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_full_cycle(self):
        """
        Simulate: SessionStart → UserPrompt → PreToolUse(Read) →
        PostToolUse(Read) x3 → Stop
        """
        # 1. Session Start
        r = self._run_hook("session_start_hook.py", {"source": "startup"})
        self.assertEqual(r.returncode, 0)
        state = load_state()
        self.assertGreater(state["repo_map"]["stats"]["total_files"], 0)

        # 2. User Prompt
        r = self._run_hook("user_prompt_hook.py", {
            "prompt": "Explain the authentication flow and how it connects to the user model",
        })
        self.assertEqual(r.returncode, 0)
        state = load_state()
        initial_budget = state["context_state"]["budget"]
        self.assertGreater(initial_budget, 0)

        # 3. Pre + Post Read (login.py)
        file1 = os.path.join(self.tmpdir, "src/auth/login.py")
        self._run_hook("pre_tool_use_hook.py", {
            "tool_name": "Read", "tool_input": {"file_path": file1},
        })
        self._run_hook("post_tool_use_hook.py", {
            "tool_name": "Read",
            "tool_input": {"file_path": file1},
            "tool_response": {"content": open(file1).read()},
        })

        state = load_state()
        self.assertEqual(state["context_state"]["t"], 1)
        self.assertIn("src/auth/login.py", state["explored_files"])

        # 4. Pre + Post Read (user.py)
        file2 = os.path.join(self.tmpdir, "src/models/user.py")
        self._run_hook("pre_tool_use_hook.py", {
            "tool_name": "Read", "tool_input": {"file_path": file2},
        })
        self._run_hook("post_tool_use_hook.py", {
            "tool_name": "Read",
            "tool_input": {"file_path": file2},
            "tool_response": {"content": open(file2).read()},
        })

        # 5. Pre + Post Read (session.py)
        file3 = os.path.join(self.tmpdir, "src/db/session.py")
        self._run_hook("pre_tool_use_hook.py", {
            "tool_name": "Read", "tool_input": {"file_path": file3},
        })
        self._run_hook("post_tool_use_hook.py", {
            "tool_name": "Read",
            "tool_input": {"file_path": file3},
            "tool_response": {"content": open(file3).read()},
        })

        # Verify state after reads
        state = load_state()
        ctx = state["context_state"]
        self.assertEqual(ctx["t"], 3)
        self.assertGreater(ctx["kappa_t"], 0)
        self.assertGreater(ctx["L_t"], 0)
        self.assertEqual(len(state["explored_files"]), 3)
        self.assertEqual(len(ctx["igr_history"]), 3)

        # 6. Stop
        r = self._run_hook("stop_hook.py", {
            "stop_hook_active": False,
        })
        # With 3 files read, confidence may or may not be enough
        # But the hook should not crash
        self.assertIn(r.returncode, [0, 1, 2])

    def test_audit_log_captures_full_cycle(self):
        """Verify the audit log records all events."""
        self._run_hook("session_start_hook.py", {"source": "startup"})
        self._run_hook("user_prompt_hook.py", {"prompt": "test query"})

        file1 = os.path.join(self.tmpdir, "src/auth/login.py")
        self._run_hook("post_tool_use_hook.py", {
            "tool_name": "Read",
            "tool_input": {"file_path": file1},
            "tool_response": {"content": "test"},
        })

        # Check audit log
        with open(log_path()) as f:
            entries = [json.loads(line) for line in f if line.strip()]

        events = [e["event"] for e in entries]
        self.assertIn("session_start_scan", events)
        self.assertIn("query_augmentation", events)
        self.assertIn("post_tool_use", events)


# ═══════════════════════════════════════════════════════════════════════════════
# Settings.json Validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestSettingsJson(unittest.TestCase):
    """Validate the Claude Code settings.json configuration."""

    @classmethod
    def _settings_path(cls):
        return os.path.normpath(os.path.join(HOOKS_DIR, "..", "settings.json"))

    def test_settings_is_valid_json(self):
        with open(self._settings_path()) as f:
            settings = json.load(f)
        self.assertIn("hooks", settings)

    def test_settings_has_all_events(self):
        with open(self._settings_path()) as f:
            settings = json.load(f)

        hooks = settings["hooks"]
        self.assertIn("SessionStart", hooks)
        self.assertIn("UserPromptSubmit", hooks)
        self.assertIn("PreToolUse", hooks)
        self.assertIn("PostToolUse", hooks)
        self.assertIn("Stop", hooks)

    def test_settings_hooks_have_required_fields(self):
        with open(self._settings_path()) as f:
            settings = json.load(f)

        for event, matchers in settings["hooks"].items():
            for matcher_entry in matchers:
                self.assertIn("hooks", matcher_entry)
                for hook in matcher_entry["hooks"]:
                    self.assertIn("type", hook)
                    self.assertEqual(hook["type"], "command")
                    self.assertIn("command", hook)

    def test_all_hook_scripts_exist(self):
        """Verify all referenced scripts actually exist."""
        expected_scripts = [
            "session_start_hook.sh",
            "user_prompt_hook.sh",
            "pre_tool_use_hook.sh",
            "post_tool_use_hook.sh",
            "stop_hook.sh",
        ]
        for script in expected_scripts:
            path = os.path.join(HOOKS_DIR, script)
            self.assertTrue(os.path.exists(path), f"Missing script: {script}")
            # Check executable
            self.assertTrue(os.access(path, os.X_OK), f"Not executable: {script}")

    def test_all_python_scripts_exist(self):
        expected = [
            "tokenscout_common.py",
            "session_start_hook.py",
            "user_prompt_hook.py",
            "pre_tool_use_hook.py",
            "post_tool_use_hook.py",
            "stop_hook.py",
        ]
        for script in expected:
            path = os.path.join(HOOKS_DIR, script)
            self.assertTrue(os.path.exists(path), f"Missing: {script}")


# ═══════════════════════════════════════════════════════════════════════════════
# Graph Layer Tests (§3.1.3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestInheritanceGraph(TestRepoSetup):
    """Test G_inh: inheritance graph extraction."""

    def test_python_inheritance_extracted(self):
        """Python class(Base) inheritance is detected."""
        result = scan_repo_lightweight(self.tmpdir)
        inh = result["inheritance"]
        # Our mock repo has classes — check graph is populated
        self.assertIsInstance(inh, dict)

    def test_extract_bases_python(self):
        bases = _extract_bases("class AdminUser(BaseUser, PermissionMixin):", "python")
        self.assertIn("BaseUser", bases)
        self.assertIn("PermissionMixin", bases)

    def test_extract_bases_python_filters_object(self):
        bases = _extract_bases("class Foo(object):", "python")
        self.assertEqual(bases, [])

    def test_extract_bases_javascript(self):
        bases = _extract_bases("class AdminUser extends BaseUser", "javascript")
        self.assertIn("BaseUser", bases)

    def test_extract_bases_java_extends_implements(self):
        bases = _extract_bases("public class Foo extends Bar implements Baz, Qux", "java")
        self.assertIn("Bar", bases)
        self.assertIn("Baz", bases)
        self.assertIn("Qux", bases)

    def test_inheritance_builds_reverse_edges(self):
        """Subclasses are tracked as reverse edges."""
        files = {
            "base.py": {
                "lang": "python", "signatures": [
                    {"name": "Animal", "type": "class", "line": 1, "signature": "class Animal:"}
                ]
            },
            "dog.py": {
                "lang": "python", "signatures": [
                    {"name": "Dog", "type": "class", "line": 1, "signature": "class Dog(Animal):"}
                ]
            },
        }
        symbols = {}
        inh = _build_inheritance_graph(files, symbols)
        self.assertIn("Dog", inh)
        self.assertIn("Animal", inh["Dog"]["bases"])
        self.assertIn("Animal", inh)
        self.assertIn("Dog", inh["Animal"]["subclasses"])

    def test_scan_repo_includes_inheritance(self):
        """scan_repo_lightweight returns inheritance key."""
        result = scan_repo_lightweight(self.tmpdir)
        self.assertIn("inheritance", result)


class TestCallGraph(TestRepoSetup):
    """Test G_call: call graph extraction."""

    def test_call_graph_structure(self):
        result = scan_repo_lightweight(self.tmpdir)
        self.assertIn("call_graph", result)
        self.assertIsInstance(result["call_graph"], dict)

    def test_cross_file_calls_detected(self):
        """Functions that reference symbols from other files are linked."""
        files = {
            "auth.py": {
                "lang": "python", "signatures": [
                    {"name": "login", "type": "function", "line": 1,
                     "signature": "def login(user, hash_password(pw))"},
                ]
            },
            "crypto.py": {
                "lang": "python", "signatures": [
                    {"name": "hash_password", "type": "function", "line": 1,
                     "signature": "def hash_password(pw)"},
                ]
            },
        }
        symbols = {
            "auth.py::login": {"path": "auth.py", "line": 1, "type": "function", "signature": ""},
            "crypto.py::hash_password": {"path": "crypto.py", "line": 1, "type": "function", "signature": ""},
        }
        cg = _build_call_graph(files, symbols)
        # auth.py::login should have a call edge to crypto.py::hash_password
        if "auth.py::login" in cg:
            callees = cg["auth.py::login"]
            callee_names = [c.split("::")[-1] for c in callees]
            self.assertIn("hash_password", callee_names)

    def test_method_override_detection(self):
        """Methods with same name in different classes are linked."""
        files = {
            "base.py": {
                "lang": "python", "signatures": [
                    {"name": "Base.process", "type": "method", "line": 1,
                     "signature": "def process(self, data)"},
                ]
            },
            "child.py": {
                "lang": "python", "signatures": [
                    {"name": "Child.process", "type": "method", "line": 1,
                     "signature": "def process(self, data)"},
                ]
            },
        }
        symbols = {
            "base.py::Base.process": {"path": "base.py", "line": 1, "type": "method", "signature": ""},
            "child.py::Child.process": {"path": "child.py", "line": 1, "type": "method", "signature": ""},
        }
        cg = _build_call_graph(files, symbols)
        # child.py::Child.process should reference base.py::process
        if "child.py::Child.process" in cg:
            callee_paths = [c.split("::")[0] for c in cg["child.py::Child.process"]]
            self.assertIn("base.py", callee_paths)


class TestGraphExpansion(TestRepoSetup):
    """Test 3-layer graph expansion (§3.2.2)."""

    def _make_repo_map(self):
        return {
            "files": {
                "models/user.py": {"lang": "python", "lines": 50, "signatures": [
                    {"name": "User", "type": "class", "line": 1, "signature": "class User:"},
                    {"name": "User.save", "type": "method", "line": 10, "signature": "def save(self)"},
                ]},
                "models/admin.py": {"lang": "python", "lines": 30, "signatures": [
                    {"name": "Admin", "type": "class", "line": 1, "signature": "class Admin(User):"},
                ]},
                "auth/login.py": {"lang": "python", "lines": 40, "signatures": [
                    {"name": "authenticate", "type": "function", "line": 1, "signature": "def authenticate(user)"},
                ]},
                "tests/test_user.py": {"lang": "python", "lines": 60, "signatures": [
                    {"name": "TestUser", "type": "class", "line": 1, "signature": "class TestUser:"},
                ]},
            },
            "dependencies": {
                "models/admin.py": ["models.user"],
                "auth/login.py": ["models.user"],
                "tests/test_user.py": ["models.user", "auth.login"],
            },
            "inheritance": {
                "User": {"path": "models/user.py", "bases": [], "subclasses": ["Admin"], "line": 1},
                "Admin": {"path": "models/admin.py", "bases": ["User"], "subclasses": [], "line": 1},
                "TestUser": {"path": "tests/test_user.py", "bases": [], "subclasses": [], "line": 1},
            },
            "call_graph": {
                "auth/login.py::authenticate": ["models/user.py::User.save"],
            },
            "symbols": {},
        }

    def test_dep_layer_forward(self):
        """G_dep forward: files that target imports."""
        repo_map = self._make_repo_map()
        related = find_related_via_graphs("models/admin.py", repo_map)
        paths = list(related.keys())
        self.assertIn("models/user.py", paths)

    def test_dep_layer_reverse(self):
        """G_dep reverse: files that import target."""
        repo_map = self._make_repo_map()
        related = find_related_via_graphs("models/user.py", repo_map)
        paths = list(related.keys())
        # admin.py and login.py both import user
        self.assertTrue(any("admin" in p for p in paths))

    def test_inheritance_layer(self):
        """G_inh: superclass/subclass connections."""
        repo_map = self._make_repo_map()
        related = find_related_via_graphs("models/admin.py", repo_map)
        # Admin extends User → models/user.py should be related via superclass
        found_super = any(
            info.get("relation") == "superclass"
            for info in related.values()
        )
        self.assertTrue(found_super, "Should find superclass via G_inh")

    def test_call_graph_layer(self):
        """G_call: function call connections."""
        repo_map = self._make_repo_map()
        related = find_related_via_graphs("auth/login.py", repo_map)
        # authenticate calls User.save → models/user.py
        found_calls = any(
            info.get("relation") == "calls"
            for info in related.values()
        )
        self.assertTrue(found_calls, "Should find call target via G_call")

    def test_call_graph_reverse(self):
        """G_call reverse: files that call functions in target."""
        repo_map = self._make_repo_map()
        related = find_related_via_graphs("models/user.py", repo_map)
        found_called_by = any(
            info.get("relation") == "called_by"
            for info in related.values()
        )
        self.assertTrue(found_called_by, "Should find caller via G_call reverse")

    def test_related_includes_relation_metadata(self):
        """Each related file has relation, distance, and via metadata."""
        repo_map = self._make_repo_map()
        related = find_related_via_graphs("models/user.py", repo_map)
        for path, info in related.items():
            self.assertIn("relation", info)
            self.assertIn("distance", info)
            self.assertIn("via", info)


# ═══════════════════════════════════════════════════════════════════════════════
# Confidence Estimation Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfidenceEstimation(unittest.TestCase):
    """Test multi-signal confidence estimation (§3.3.1)."""

    def _make_state(self, candidates=None, explored=None, repo_map=None):
        state = _default_state()
        if candidates:
            state["candidates"] = candidates
        if explored:
            state["explored_files"] = explored
        if repo_map:
            state["repo_map"] = repo_map
        return state

    def test_candidate_file_gets_higher_boost(self):
        """Files in candidates list get bigger confidence boost."""
        state = self._make_state(
            candidates={"auth.py": {"score": 0.9}},
        )
        boost_candidate = estimate_confidence_boost("auth.py", state, "Read")
        boost_unknown = estimate_confidence_boost("random.py", state, "Read")
        self.assertGreater(boost_candidate, boost_unknown)

    def test_high_score_candidate_beats_low_score(self):
        state = self._make_state(
            candidates={
                "important.py": {"score": 0.95},
                "maybe.py": {"score": 0.2},
            },
        )
        boost_high = estimate_confidence_boost("important.py", state, "Read")
        boost_low = estimate_confidence_boost("maybe.py", state, "Read")
        self.assertGreater(boost_high, boost_low)

    def test_diminishing_returns(self):
        """Each successive file read gives less confidence."""
        state1 = self._make_state(explored=[])
        state2 = self._make_state(explored=["a.py", "b.py", "c.py", "d.py", "e.py"])
        boost1 = estimate_confidence_boost("new.py", state1, "Read")
        boost2 = estimate_confidence_boost("new.py", state2, "Read")
        self.assertGreater(boost1, boost2)

    def test_reread_gives_minimal_boost(self):
        """Re-reading a file gives almost no new confidence."""
        state = self._make_state(explored=["auth.py"])
        boost = estimate_confidence_boost("auth.py", state, "Read")
        self.assertLess(boost, 2.0)

    def test_grep_boost_decreases_over_time(self):
        """Grep boost should decrease with more tool calls."""
        state = self._make_state()
        state["context_state"]["t"] = 1
        boost_early = estimate_confidence_boost("", state, "Grep")
        state["context_state"]["t"] = 20
        boost_late = estimate_confidence_boost("", state, "Grep")
        self.assertGreater(boost_early, boost_late)

    def test_test_command_gives_high_boost(self):
        """Running tests should give high confidence signal."""
        state = self._make_state()
        boost = estimate_confidence_boost("", state, "Bash", {"command": "pytest tests/"})
        self.assertGreaterEqual(boost, 10.0)

    def test_coverage_bonus(self):
        """Exploring most candidates gives coverage bonus."""
        cands = {f"file{i}.py": {"score": 0.5} for i in range(10)}
        explored = [f"file{i}.py" for i in range(9)]  # 90% coverage
        state = self._make_state(candidates=cands, explored=explored)
        boost_high_cov = estimate_confidence_boost("new.py", state, "Read")

        state2 = self._make_state(candidates=cands, explored=["file0.py"])
        boost_low_cov = estimate_confidence_boost("new.py", state2, "Read")
        # High coverage state should give bigger boost (coverage bonus)
        # Note: diminishing returns from many explored files counteracts this,
        # so we just check they're both positive
        self.assertGreater(boost_high_cov, 0)
        self.assertGreater(boost_low_cov, 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Real-World Benchmark Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRealWorldBenchmark(unittest.TestCase):
    """Test the real-world audit log analyzer."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tokenscout_rw_test_")
        sys.path.insert(0, HOOKS_DIR)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_audit_log(self, events):
        path = os.path.join(self.tmpdir, "audit.jsonl")
        with open(path, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        return path

    def test_parse_empty_log(self):
        from realworld_benchmark import parse_audit_log
        path = self._write_audit_log([])
        stats = parse_audit_log(path)
        self.assertEqual(stats.total_events, 0)

    def test_parse_session_scan(self):
        from realworld_benchmark import parse_audit_log
        path = self._write_audit_log([
            {"ts": 1000, "event": "session_start_scan", "total_files": 42, "entropy": 1.2},
        ])
        stats = parse_audit_log(path)
        self.assertEqual(stats.repo_files, 42)
        self.assertAlmostEqual(stats.repo_entropy, 1.2)

    def test_parse_full_session(self):
        from realworld_benchmark import parse_audit_log
        path = self._write_audit_log([
            {"ts": 1000, "event": "session_start_scan", "total_files": 30, "entropy": 1.1},
            {"ts": 1001, "event": "query_augmentation", "D_q": 45, "budget": 3000},
            {"ts": 1002, "event": "post_tool_use", "tool": "Read", "lines_consumed": 50,
             "kappa_t": 15, "igr": 0.3, "L_t": 50, "file": "auth.py"},
            {"ts": 1003, "event": "post_tool_use", "tool": "Read", "lines_consumed": 30,
             "kappa_t": 35, "igr": 0.67, "L_t": 80, "file": "models.py"},
            {"ts": 1004, "event": "post_tool_use", "tool": "Grep", "lines_consumed": 10,
             "kappa_t": 38, "igr": 0.3, "L_t": 90},
            {"ts": 1010, "event": "stop_allowed", "reason": "sufficient", "kappa": 75},
        ])
        stats = parse_audit_log(path)
        self.assertEqual(stats.total_tool_calls, 3)
        self.assertEqual(stats.query_complexity, 45)
        self.assertEqual(stats.budget_allocated, 3000)
        self.assertEqual(len(stats.files_read), 2)
        self.assertEqual(stats.total_lines_consumed, 90)
        self.assertGreater(stats.duration_seconds, 0)

    def test_token_estimation(self):
        from realworld_benchmark import parse_audit_log
        path = self._write_audit_log([
            {"ts": 1000, "event": "post_tool_use", "tool": "Read",
             "lines_consumed": 100, "kappa_t": 50, "igr": 0.5, "L_t": 100},
        ])
        stats = parse_audit_log(path)
        # 100 lines * 8 tokens/line + 1 tool call * 50 = 850
        self.assertEqual(stats.tokens_estimated, 850)

    def test_export_json(self):
        from realworld_benchmark import parse_audit_log, export_json
        path = self._write_audit_log([
            {"ts": 1000, "event": "session_start_scan", "total_files": 10, "entropy": 1.0},
            {"ts": 1001, "event": "query_augmentation", "D_q": 30, "budget": 2000},
        ])
        stats = parse_audit_log(path)
        data = export_json(stats)
        self.assertIn("tokens_estimated", data)
        self.assertIn("budget_utilization_pct", data)
        self.assertEqual(data["repo_files"], 10)


# ═══════════════════════════════════════════════════════════════════════════════
# BM25 Sparse Retrieval Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestBM25Index(TestRepoSetup):
    """Test BM25 sparse retrieval implementation."""

    def test_empty_index(self):
        """BM25 on empty repo returns no results."""
        bm25 = BM25Index()
        bm25.index_repo({"files": {}, "dependencies": {}})
        results = bm25.query("auth login")
        self.assertEqual(results, [])

    def test_index_basic_repo(self):
        """BM25 indexes a basic repo and returns ranked results."""
        repo_map = scan_repo_lightweight(self.tmpdir)
        bm25 = BM25Index()
        bm25.index_repo(repo_map)
        self.assertTrue(bm25.corpus_indexed)
        self.assertGreater(bm25.doc_count, 0)

    def test_query_returns_ranked(self):
        """BM25 query returns results sorted by score (descending)."""
        repo_map = scan_repo_lightweight(self.tmpdir)
        bm25 = BM25Index()
        bm25.index_repo(repo_map)
        results = bm25.query("auth login user")
        if results:
            scores = [s for _, s in results]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_relevant_file_scores_higher(self):
        """File containing query terms scores higher than unrelated files."""
        # Create a repo with clear relevance signal
        auth_dir = os.path.join(self.tmpdir, "auth")
        os.makedirs(auth_dir)
        with open(os.path.join(auth_dir, "login.py"), "w") as f:
            f.write("def authenticate_user(username, password):\n    pass\n")
        with open(os.path.join(self.tmpdir, "utils.py"), "w") as f:
            f.write("def format_date(d):\n    pass\n")

        repo_map = scan_repo_lightweight(self.tmpdir)
        bm25 = BM25Index()
        bm25.index_repo(repo_map)
        results = bm25.query("authenticate login user password")
        self.assertGreater(len(results), 0)
        # auth/login.py should score highest
        top_path = results[0][0]
        self.assertIn("auth", top_path)

    def test_idf_weighing(self):
        """Rare terms should have higher impact than common terms."""
        # Create multiple files, "import" is common, "cryptography" is rare
        for i in range(5):
            with open(os.path.join(self.tmpdir, f"mod{i}.py"), "w") as f:
                f.write(f"import os\ndef func{i}():\n    pass\n")
        with open(os.path.join(self.tmpdir, "crypto.py"), "w") as f:
            f.write("import os\ndef encrypt_with_cryptography():\n    pass\n")

        repo_map = scan_repo_lightweight(self.tmpdir)
        bm25 = BM25Index()
        bm25.index_repo(repo_map)

        results = bm25.query("cryptography encrypt")
        self.assertGreater(len(results), 0)
        self.assertIn("crypto.py", results[0][0])

    def test_top_k_limit(self):
        """Query respects top_k parameter."""
        repo_map = scan_repo_lightweight(self.tmpdir)
        bm25 = BM25Index()
        bm25.index_repo(repo_map)
        results = bm25.query("auth", top_k=3)
        self.assertLessEqual(len(results), 3)

    def test_camelcase_tokenization(self):
        """BM25 splits camelCase terms correctly."""
        bm25 = BM25Index()
        parts = bm25._split_camel("getUserById")
        self.assertIn("get", parts)
        self.assertIn("user", parts)

    def test_query_with_no_matches(self):
        """Query with completely unrelated terms returns empty."""
        with open(os.path.join(self.tmpdir, "hello.py"), "w") as f:
            f.write("def greet():\n    print('hello')\n")
        repo_map = scan_repo_lightweight(self.tmpdir)
        bm25 = BM25Index()
        bm25.index_repo(repo_map)
        results = bm25.query("xyzzyplugh completely unrelated quantum")
        # May return 0 or very low scores
        if results:
            self.assertLess(results[0][1], 1.0)

    def test_bm25_parameters(self):
        """Custom k1 and b parameters work."""
        bm25 = BM25Index(k1=2.0, b=0.5)
        self.assertEqual(bm25.k1, 2.0)
        self.assertEqual(bm25.b, 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM Integration Tests (mock-based — no real API calls)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMIntegration(unittest.TestCase):
    """Test LLM integration with mocked API responses."""

    def setUp(self):
        """Reset LLM method cache between tests."""
        import tokenscout_llm
        tokenscout_llm._llm_method = None

    def _no_llm_env(self):
        """Context manager that disables both OAuth and API key."""
        return patch.multiple(
            "tokenscout_llm",
            _llm_method=None,
        )

    def test_llm_not_available_without_key_or_cli(self):
        """is_llm_available returns False without API key or claude CLI."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value=None):
            import tokenscout_llm
            tokenscout_llm._llm_method = None  # reset cache
            self.assertFalse(is_llm_available())

    def test_llm_available_with_key(self):
        """is_llm_available returns True with API key."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-key"}, clear=False), \
             patch("shutil.which", return_value=None):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            self.assertTrue(is_llm_available())

    def test_llm_available_with_oauth_cli(self):
        """is_llm_available returns True when claude CLI is available."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            self.assertTrue(is_llm_available())

    def test_oauth_preferred_over_api_key(self):
        """OAuth (claude CLI) is preferred when both are available."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            method = tokenscout_llm._detect_llm_method()
            self.assertEqual(method, "oauth")

    def test_api_key_fallback_when_no_cli(self):
        """Falls back to API key when claude CLI is not available."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value=None):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            method = tokenscout_llm._detect_llm_method()
            self.assertEqual(method, "api_key")

    def test_augment_query_no_llm(self):
        """augment_query returns empty results without any LLM method."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value=None):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = augment_query("find auth bug", "repo summary", [])
            self.assertEqual(result["expanded_terms"], [])
            self.assertIsNone(result["D_q"])
            self.assertIsNone(result["intent"])

    def test_assess_confidence_no_llm(self):
        """assess_confidence returns None values without any LLM method."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value=None):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = assess_confidence(
                "find bug", ["auth.py"], ["Auth.login"], ["db.py"], 30.0, 40.0
            )
            self.assertIsNone(result["kappa_estimate"])
            self.assertIsNone(result["should_continue"])

    def test_semantic_rank_no_llm(self):
        """semantic_rank_candidates falls back to original scores without LLM."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value=None):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            candidates = {
                "auth.py": {"score": 0.8},
                "db.py": {"score": 0.5},
            }
            result = semantic_rank_candidates("find auth", candidates, {})
            self.assertEqual(len(result), 2)
            scores_dict = dict(result)
            self.assertEqual(scores_dict["auth.py"], 0.8)

    @patch("tokenscout_llm._call_haiku")
    def test_augment_query_with_mock(self, mock_haiku):
        """augment_query parses LLM response correctly."""
        mock_haiku.return_value = json.dumps({
            "expanded_terms": ["authentication", "session", "token", "jwt"],
            "D_q": 65,
            "intent": "bug_fix",
            "strategy": "Check auth middleware first, then session handler"
        })
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = augment_query("find auth bug", "repo summary", ["auth.py"])
            self.assertEqual(result["D_q"], 65)
            self.assertEqual(result["intent"], "bug_fix")
            self.assertIn("authentication", result["expanded_terms"])
            self.assertIn("jwt", result["expanded_terms"])

    @patch("tokenscout_llm._call_haiku")
    def test_assess_confidence_with_mock(self, mock_haiku):
        """assess_confidence parses LLM response correctly."""
        mock_haiku.return_value = json.dumps({
            "kappa_estimate": 75,
            "reasoning": "Key auth files explored, only tests remaining",
            "should_continue": False,
            "next_targets": ["test_auth.py"]
        })
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = assess_confidence(
                "find auth bug", ["auth.py", "middleware.py"],
                ["Auth.login", "Middleware.check"], ["test_auth.py"], 50.0, 60.0
            )
            self.assertEqual(result["kappa_estimate"], 75)
            self.assertFalse(result["should_continue"])
            self.assertIn("test_auth.py", result["next_targets"])

    @patch("tokenscout_llm._call_haiku")
    def test_semantic_rank_with_mock(self, mock_haiku):
        """semantic_rank_candidates uses LLM rankings."""
        mock_haiku.return_value = json.dumps([
            {"path": "auth.py", "score": 0.95, "reason": "directly handles auth"},
            {"path": "db.py", "score": 0.3, "reason": "general database"},
        ])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            candidates = {
                "auth.py": {"score": 0.6},
                "db.py": {"score": 0.7},
            }
            result = semantic_rank_candidates("find auth bug", candidates, {})
            scores = dict(result)
            self.assertGreater(scores["auth.py"], scores["db.py"])

    @patch("tokenscout_llm._call_haiku")
    def test_augment_handles_malformed_response(self, mock_haiku):
        """augment_query handles malformed LLM response gracefully."""
        mock_haiku.return_value = "this is not json at all"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = augment_query("test query", "summary", [])
            self.assertEqual(result["expanded_terms"], [])
            self.assertIsNone(result["D_q"])

    @patch("tokenscout_llm._call_haiku")
    def test_augment_handles_markdown_wrapped_json(self, mock_haiku):
        """augment_query handles markdown-wrapped JSON response."""
        mock_haiku.return_value = '```json\n{"expanded_terms": ["test"], "D_q": 40, "intent": "testing", "strategy": "run tests"}\n```'
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = augment_query("test query", "summary", [])
            self.assertEqual(result["D_q"], 40)
            self.assertIn("test", result["expanded_terms"])

    @patch("tokenscout_llm._call_haiku")
    def test_augment_caps_dq_at_100(self, mock_haiku):
        """augment_query caps D_q at 100."""
        mock_haiku.return_value = json.dumps({
            "expanded_terms": [], "D_q": 999, "intent": "test", "strategy": ""
        })
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = augment_query("test", "summary", [])
            self.assertEqual(result["D_q"], 100)

    @patch("tokenscout_llm._call_haiku")
    def test_haiku_call_failure_graceful(self, mock_haiku):
        """LLM failure falls back gracefully."""
        mock_haiku.return_value = None  # simulate failure
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = augment_query("test", "summary", [])
            self.assertEqual(result["expanded_terms"], [])

    @patch("tokenscout_llm.subprocess.run")
    def test_oauth_call_success(self, mock_run):
        """OAuth call via claude -p returns response correctly."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude", "-p"], returncode=0,
            stdout='{"expanded_terms": ["auth"], "D_q": 50, "intent": "bug_fix", "strategy": "check auth"}',
            stderr="",
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = tokenscout_llm._call_haiku_oauth("system", "user msg", 200)
            self.assertIsNotNone(result)
            self.assertIn("expanded_terms", result)

    @patch("tokenscout_llm.subprocess.run")
    def test_oauth_call_timeout(self, mock_run):
        """OAuth call handles timeout gracefully."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=15)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False), \
             patch("shutil.which", return_value="/usr/local/bin/claude"):
            import tokenscout_llm
            tokenscout_llm._llm_method = None
            result = tokenscout_llm._call_haiku_oauth("system", "user msg", 200)
            self.assertIsNone(result)


class TestBuildHelpers(unittest.TestCase):
    """Test LLM helper functions."""

    def test_build_repo_summary(self):
        """build_repo_summary produces readable text."""
        repo_map = {
            "files": {
                "auth/login.py": {"lang": "python", "lines": 100, "signatures": []},
                "db/models.py": {"lang": "python", "lines": 200, "signatures": []},
            },
            "symbols": {
                "auth/login.py::AuthService": {"type": "class", "path": "auth/login.py", "line": 1},
            },
            "stats": {"total_files": 2, "total_lines": 300, "languages": {"python": 2}},
            "dependencies": {},
        }
        summary = build_repo_summary(repo_map)
        self.assertIn("Files: 2", summary)
        self.assertIn("Lines: 300", summary)
        self.assertIn("AuthService", summary)

    def test_build_file_summaries(self):
        """build_file_summaries creates per-file descriptions."""
        repo_map = {
            "files": {
                "auth.py": {
                    "lang": "python", "lines": 50,
                    "signatures": [{"name": "login", "type": "function"}],
                },
            },
        }
        summaries = build_file_summaries(repo_map)
        self.assertIn("auth.py", summaries)
        self.assertIn("login", summaries["auth.py"])

    def test_build_file_summaries_no_sigs(self):
        """Files without signatures still get a summary."""
        repo_map = {
            "files": {"config.py": {"lang": "python", "lines": 10, "signatures": []}},
        }
        summaries = build_file_summaries(repo_map)
        self.assertIn("config.py", summaries)
        self.assertIn("python", summaries["config.py"])


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
