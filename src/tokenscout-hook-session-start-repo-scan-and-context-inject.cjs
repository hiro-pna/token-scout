#!/usr/bin/env node
'use strict';
/**
 * tokenscout-hook-session-start-repo-scan-and-context-inject.cjs
 *
 * Claude Code SessionStart hook.
 * Ports session_start_hook.py:
 *   - Scans repo on startup/clear or when state is stale (>600s)
 *   - Updates shared state with repo_map, entropy H_r, resets tracking
 *   - Prints a structural scouting summary into Claude's context
 */

const fs = require('fs');
const { loadState, saveState, auditLog, getProjectDir } = require('./lib/tokenscout-state.cjs');
const { scanRepoLightweight } = require('./lib/tokenscout-repo-scanner-signature-extractor.cjs');
const { computeBudget } = require('./lib/tokenscout-confidence-and-budget.cjs');
const { isEmbeddingAvailable, buildEmbeddingIndex, buildRepoOverviewEmbedding } = require('./lib/tokenscout-gemini-embedding-dense-retrieval.cjs');

// ─── Helpers ──────────────────────────────────────────────────────────────────

function _topDirectories(files, limit = 10) {
  const dirCounts = {};
  for (const p of Object.keys(files)) {
    const parts = p.split('/');
    const top = parts.length > 1 ? parts[0] : '';
    if (top) dirCounts[top] = (dirCounts[top] || 0) + 1;
  }
  return Object.entries(dirCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([d]) => d);
}

function _topSymbols(symbols, limit = 15) {
  const names = [];
  const seen = new Set();
  for (const [key, info] of Object.entries(symbols).slice(0, 200)) {
    if (['class', 'struct', 'enum'].includes(info.type)) {
      const name = key.includes('::') ? key.split('::').pop() : key;
      if (!seen.has(name)) { seen.add(name); names.push(name); }
    }
  }
  return names.slice(0, limit);
}

// ─── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const stdin = fs.readFileSync(0, 'utf-8').trim();
  const hookInput = stdin ? JSON.parse(stdin) : {};
  const source = hookInput.source || 'startup';
  const projectDir = getProjectDir();

  const state = loadState();
  const nowSec = Date.now() / 1000;

  const needsScan = (
    source === 'startup' ||
    source === 'clear' ||
    Object.keys(state.repo_map.files).length === 0 ||
    (nowSec - (state.session_start || 0)) > 600
  );

  if (needsScan) {
    const repoMap = scanRepoLightweight(projectDir);
    state.repo_map = repoMap;
    state.session_start = nowSec;

    const Hr = repoMap.stats.entropy || 1.0;
    state.context_state.H_r = Hr;
    state.context_state.L_t = 0;
    state.context_state.t = 0;
    state.context_state.kappa_t = 0.0;
    state.context_state.igr_history = [];
    state.explored_files = [];
    state.scouted_files = [];
    state.candidates = {};

    saveState(state);

    // Tier 2: Build Gemini embedding index + repo overview (cached, skipped if no API key)
    let embeddingStats = { indexed: 0, cached: false };
    let overviewStats = { embedded: false };
    if (isEmbeddingAvailable()) {
      embeddingStats = buildEmbeddingIndex(repoMap);
      overviewStats = buildRepoOverviewEmbedding(repoMap, projectDir);
    }

    auditLog('session_start_scan', {
      source,
      total_files: repoMap.stats.total_files,
      total_lines: repoMap.stats.total_lines,
      languages: repoMap.stats.languages,
      entropy: Hr,
      embedding_indexed: embeddingStats.indexed,
      embedding_cached: embeddingStats.cached,
      repo_overview_embedded: overviewStats.embedded,
    });
  }

  // ── Build structural summary for Claude's context ──
  const stats = state.repo_map.stats;
  const nFiles = stats.total_files || 0;
  const langs = stats.languages || {};
  const entropy = stats.entropy || 1.0;

  const topDirs = _topDirectories(state.repo_map.files);
  const topSymbols = _topSymbols(state.repo_map.symbols || {});

  const baseName = projectDir.split('/').pop() || projectDir;
  const langStr = Object.entries(langs)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}(${v})`)
    .join(', ');

  // Compact session context — minimize token overhead
  const lines = [
    `[TokenScout] ${baseName}: ${nFiles} files, ${langStr}. Read only ranked candidates.`,
  ];

  console.log(lines.join('\n'));
}

main();
