'use strict';
/**
 * tokenscout-state.cjs - State management for TokenScout hook system.
 *
 * Ports tokenscout_common.py lines 25-113:
 *   - Path resolution (isolated to .claude/.tokenscout/)
 *   - State load/save with default state vector S_t = {D_q, H_r, L_t, t, κ_t}
 *   - Audit log (JSONL)
 *   - stdin hook input reader
 */

const fs = require('fs');
const path = require('path');

// ─── Paths ────────────────────────────────────────────────────────────────────

function getProjectDir() {
  return process.env.CLAUDE_PROJECT_DIR || process.cwd();
}

function statePath() {
  return path.join(getProjectDir(), '.claude', '.tokenscout', 'state.json');
}

function logPath() {
  return path.join(getProjectDir(), '.claude', '.tokenscout', 'audit.jsonl');
}

// ─── State management ─────────────────────────────────────────────────────────

function defaultState() {
  return {
    // Phase 1: Repo representation
    repo_map: {
      files: {},           // path -> {type, size, lang, signatures:[]}
      dependencies: {},    // G_dep: path -> [imported_modules]
      inheritance: {},     // G_inh: class -> {bases:[], subclasses:[], path}
      call_graph: {},      // G_call: func -> [called_funcs]
      symbols: {},         // "module.Class.method" -> {path, line, type}
      stats: {
        total_files: 0,
        total_lines: 0,
        languages: {},
        depth: 0,
      },
    },

    // Phase 3: Cost-aware context management
    context_state: {
      D_q: 0,          // Query complexity (0-100)
      H_r: 1.0,        // Repository entropy (0.5-2.0)
      L_t: 0,          // Cumulative context lines consumed
      t: 0,            // Iteration depth (tool call count)
      kappa_t: 0.0,    // Epistemic confidence (0-100)
      budget: 0,       // Dynamic line budget B ∝ D_q · H_r
      igr_history: [], // Information Gain Rate per step
    },

    // Bookkeeping
    explored_files: [],   // Files already read in full
    scouted_files: [],    // Files scouted via metadata only
    candidates: {},       // path -> {score, provenance, cost}
    session_start: Date.now() / 1000,
    version: '1.0.0',
  };
}

function loadState() {
  const p = statePath();
  if (fs.existsSync(p)) {
    try {
      const raw = fs.readFileSync(p, 'utf-8');
      return JSON.parse(raw);
    } catch (_) {
      // fall through to default
    }
  }
  return defaultState();
}

function saveState(state) {
  const p = statePath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(state, null, 2), 'utf-8');
}

// ─── Audit logging ────────────────────────────────────────────────────────────

function auditLog(event, data) {
  const entry = Object.assign({ ts: Date.now() / 1000, event }, data);
  const p = logPath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.appendFileSync(p, JSON.stringify(entry) + '\n', 'utf-8');
}

// ─── Stdin helper ─────────────────────────────────────────────────────────────

function readHookInput() {
  try {
    const raw = fs.readFileSync('/dev/stdin', 'utf-8');
    return JSON.parse(raw);
  } catch (_) {
    return {};
  }
}

module.exports = {
  getProjectDir,
  statePath,
  logPath,
  defaultState,
  loadState,
  saveState,
  auditLog,
  readHookInput,
};
