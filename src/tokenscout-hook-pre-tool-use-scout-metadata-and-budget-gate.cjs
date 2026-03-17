#!/usr/bin/env node
'use strict';
/**
 * tokenscout-hook-pre-tool-use-scout-metadata-and-budget-gate.cjs
 *
 * Claude Code PreToolUse hook.
 * Ports pre_tool_use_hook.py:
 *   - For Read: inject file metadata (signatures, deps, related files via graphs)
 *     before Claude reads the full file (structural scouting §3.2.2)
 *   - For Grep/Glob: inject budget warning if budget exhausted
 *   - Budget check via shouldTerminate
 *   - Outputs hookSpecificOutput JSON (never blocks)
 */

const fs = require('fs');
const path = require('path');
const { loadState, saveState, auditLog, getProjectDir } = require('./lib/tokenscout-state.cjs');
const { computeBudget, shouldTerminate } = require('./lib/tokenscout-confidence-and-budget.cjs');
const { findRelatedViaGraphs } = require('./lib/tokenscout-graph.cjs');

// ─── Helper ───────────────────────────────────────────────────────────────────

function _toRelative(filePath) {
  const projectDir = getProjectDir();
  if (filePath && filePath.startsWith(projectDir)) {
    return path.relative(projectDir, filePath);
  }
  return filePath;
}

// ─── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const stdin = fs.readFileSync(0, 'utf-8').trim();
  const hookInput = stdin ? JSON.parse(stdin) : {};
  const toolName = hookInput.tool_name || '';
  const toolInput = hookInput.tool_input || {};

  const state = loadState();
  const ctx = state.context_state;
  const repoMap = state.repo_map;

  // Recompute budget if not set
  if (ctx.budget <= 0 && ctx.D_q > 0) {
    ctx.budget = computeBudget(ctx.D_q, ctx.H_r);
    state.context_state = ctx;
    saveState(state);
  }

  const { terminate, reason } = shouldTerminate(state);

  // Skip scouting for simple queries — let Claude work naturally
  if (ctx.D_q < 20) {
    process.exit(0);
  }

  // ── Read tool: structural scouting ──
  if (toolName === 'Read') {
    const filePath = toolInput.file_path || '';
    const relPath = _toRelative(filePath);
    const fileInfo = (repoMap.files || {})[relPath] || {};
    const deps = (repoMap.dependencies || {})[relPath] || [];

    if (Object.keys(fileInfo).length > 0) {
      const sigs = fileInfo.signatures || [];
      const size = fileInfo.lines || 0;

      if (!state.scouted_files.includes(relPath)) {
        state.scouted_files.push(relPath);
      }

      const contextParts = [];

      if (terminate) {
        contextParts.push(
          `[TokenScout Budget Warning] ${reason}. ` +
          `Consider whether this read is essential. ` +
          `Current context: ${ctx.L_t} lines / budget ${ctx.budget}.`
        );
      }

      // Compact metadata — minimal tokens, maximum signal
      const sigNames = sigs.slice(0, 5).map(s => s.name).join(', ');
      if (sigNames) {
        contextParts.push(`[TokenScout] ${relPath} (${size}L): ${sigNames}`);
      }

      // Only inject related files if budget allows
      if (!terminate && deps.length > 0) {
        const related = findRelatedViaGraphs(relPath, repoMap);
        const relPaths = Object.keys(related).slice(0, 3);
        if (relPaths.length) {
          contextParts.push(`[TokenScout] Related: ${relPaths.join(', ')}`);
        }
      }

      if (contextParts.length > 0) {
        const output = {
          hookSpecificOutput: {
            hookEventName: 'PreToolUse',
            additionalContext: contextParts.join('\n'),
          },
        };
        console.log(JSON.stringify(output));
        saveState(state);
        auditLog('pre_read_scout', {
          file: relPath,
          sigs_count: sigs.length,
          deps_count: deps.length,
          budget_remaining: Math.max(0, ctx.budget - ctx.L_t),
          terminate_advised: terminate,
        });
        return;
      }
    }

    auditLog('pre_read_unknown', { file: relPath });
    return;
  }

  // ── Grep/Glob: budget awareness only ──
  if (toolName === 'Grep' || toolName === 'Glob') {
    if (terminate) {
      const output = {
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          additionalContext:
            `[TokenScout Budget] ${reason}. ` +
            `Context: ${ctx.L_t}/${ctx.budget} lines. ` +
            `Focus on confirming existing candidates.`,
        },
      };
      console.log(JSON.stringify(output));
    }
    return;
  }

  // Default: allow all other tools
  process.exit(0);
}

main();
