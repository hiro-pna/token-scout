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

      if (sigs.length > 0) {
        const sigSummary = sigs.slice(0, 10)
          .map(s => `${s.type} ${s.name} (L${s.line || '?'})`)
          .join('; ');
        contextParts.push(
          `[TokenScout Scout] ${relPath} (${size} lines, ${fileInfo.lang || '?'}): ` +
          `Contains: ${sigSummary}`
        );
      }

      if (deps.length > 0) {
        contextParts.push(
          `[TokenScout Deps] ${relPath} imports: ${deps.slice(0, 8).join(', ')}`
        );
      }

      const related = findRelatedViaGraphs(relPath, repoMap);
      if (Object.keys(related).length > 0) {
        const relSummary = Object.entries(related).slice(0, 6)
          .map(([p, info]) => `${p} (${info.relation} via ${info.via})`)
          .join('; ');
        contextParts.push(
          `[TokenScout Graph] Related to ${relPath}: ${relSummary}`
        );
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
