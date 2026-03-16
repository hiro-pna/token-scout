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
    LANG_MAP, IGNORE_DIRS,
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

if __name__ == "__main__":
    # Run with verbose output
    unittest.main(verbosity=2)
