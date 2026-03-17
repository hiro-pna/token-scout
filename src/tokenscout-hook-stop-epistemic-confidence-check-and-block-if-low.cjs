#!/usr/bin/env node
'use strict';
/**
 * tokenscout-hook-stop-epistemic-confidence-check-and-block-if-low.cjs
 *
 * Claude Code Stop hook.
 * Ports stop_hook.py:
 *   - Allows stop if fewer than 3 tool calls (trivial query)
 *   - Allows stop if shouldTerminate passes (confidence sufficient / budget exhausted)
 *   - Blocks with suggestion if confidence < 30 and budget remains (>20% left)
 *   - Lists unexplored high-priority candidates in the block message
 *   - Outputs { decision: "block", reason: "..." } to control behavior
 */

const fs = require('fs');
const { loadState, auditLog } = require('./lib/tokenscout-state.cjs');
const { shouldTerminate } = require('./lib/tokenscout-confidence-and-budget.cjs');

const MIN_CONFIDENCE_TO_STOP = 30.0;
const MIN_TOOL_CALLS = 3;

// ─── Helper ───────────────────────────────────────────────────────────────────

function _findUnexploredCandidates(state) {
  const explored = new Set(state.explored_files || []);
  const candidates = state.candidates || {};
  const scouted = state.scouted_files || [];
  const unexplored = [];

  for (const [filePath] of Object.entries(candidates).sort((a, b) => b[1].score - a[1].score)) {
    if (!explored.has(filePath)) unexplored.push(filePath);
  }

  for (const filePath of scouted) {
    if (!explored.has(filePath) && !unexplored.includes(filePath)) {
      unexplored.push(filePath);
    }
  }

  return unexplored;
}

// ─── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const stdin = fs.readFileSync(0, 'utf-8').trim();
  const hookInput = stdin ? JSON.parse(stdin) : {};

  // Prevent infinite loops if stop hook is already active
  if (hookInput.stop_hook_active) process.exit(0);

  const state = loadState();
  const ctx = state.context_state;

  // Simple queries — don't interfere with Claude's natural stopping
  if (ctx.D_q < 20) process.exit(0);

  const kappa = ctx.kappa_t;
  const t = ctx.t;
  const budget = ctx.budget;
  const Lt = ctx.L_t;
  const explored = state.explored_files || [];

  // Allow stop for trivial queries (few tool calls)
  if (t < MIN_TOOL_CALLS) {
    auditLog('stop_allowed', { reason: 'few_tool_calls', t, kappa });
    process.exit(0);
  }

  // Allow stop if termination conditions are met
  const { terminate, reason } = shouldTerminate(state);
  if (terminate) {
    auditLog('stop_allowed', {
      reason,
      kappa: Math.round(kappa * 100) / 100,
      L_t: Lt,
      budget,
      t,
      files_explored: explored.length,
    });
    process.exit(0);
  }

  // Block if confidence is low and meaningful budget remains
  if (kappa < MIN_CONFIDENCE_TO_STOP && budget > 0 && Lt < budget * 0.8) {
    const remaining = budget - Lt;
    const unexplored = _findUnexploredCandidates(state);

    const parts = [
      `[TokenScout] Confidence is low (κ=${kappa.toFixed(1)}). `,
      `Budget remaining: ${remaining} lines. `,
      `Files explored: ${explored.length}. `,
    ];

    if (unexplored.length > 0) {
      parts.push(`Unexplored high-priority candidates: ${unexplored.slice(0, 5).join(', ')}. `);
      parts.push('Consider reading these files before finalizing your answer.');
    }

    console.log(JSON.stringify({ decision: 'block', reason: parts.join('') }));

    auditLog('stop_blocked', {
      kappa: Math.round(kappa * 100) / 100,
      L_t: Lt,
      budget,
      unexplored_candidates: unexplored.slice(0, 5),
    });
    return;
  }

  // Default: allow
  auditLog('stop_allowed', {
    reason: 'default',
    kappa: Math.round(kappa * 100) / 100,
    L_t: Lt,
    budget,
    t,
  });
  process.exit(0);
}

main();
