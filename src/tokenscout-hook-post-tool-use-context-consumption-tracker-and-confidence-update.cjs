#!/usr/bin/env node
'use strict';
/**
 * tokenscout-hook-post-tool-use-context-consumption-tracker-and-confidence-update.cjs
 *
 * Claude Code PostToolUse hook.
 * Ports post_tool_use_hook.py:
 *   - Increments iteration counter t
 *   - Estimates lines consumed by the tool call
 *   - Updates cumulative context L_t and epistemic confidence kappa_t
 *   - Computes Information Gain Rate (IGR)
 *   - Optional LLM confidence assessment every 5 tool calls
 *   - Outputs additionalContext with status/warnings on termination
 *   - Audit logs every tool call
 */

const fs = require('fs');
const path = require('path');
const { loadState, saveState, auditLog, getProjectDir } = require('./lib/tokenscout-state.cjs');
const {
  estimateConfidenceBoost,
  computeBudget,
  informationGainRate,
  shouldTerminate,
} = require('./lib/tokenscout-confidence-and-budget.cjs');
const { isLlmAvailable, assessConfidence } = require('./lib/tokenscout-llm.cjs');

// ─── Helpers ──────────────────────────────────────────────────────────────────

function _toRelative(filePath) {
  const projectDir = getProjectDir();
  if (filePath && filePath.startsWith(projectDir)) {
    return path.relative(projectDir, filePath);
  }
  return filePath;
}

function _estimateLinesConsumed(toolName, toolInput, toolResponse) {
  const respStr = String(toolResponse || '');
  if (toolName === 'Read') {
    const limit = (toolInput || {}).limit;
    if (limit) return limit;
    return Math.max(1, (respStr.match(/\n/g) || []).length);
  }
  if (toolName === 'Grep') {
    return Math.max(1, (respStr.match(/\n/g) || []).length);
  }
  if (toolName === 'Glob') {
    return Math.max(1, Math.floor((respStr.match(/\n/g) || []).length / 5));
  }
  if (toolName === 'Bash') {
    return Math.max(1, (respStr.match(/\n/g) || []).length);
  }
  return 10;
}

// ─── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const stdin = fs.readFileSync(0, 'utf-8').trim();
  const hookInput = stdin ? JSON.parse(stdin) : {};
  const toolName = hookInput.tool_name || '';
  const toolInput = hookInput.tool_input || {};
  const toolResponse = hookInput.tool_response || {};

  const state = loadState();
  const ctx = state.context_state;
  const repoMap = state.repo_map || {};

  // Skip tracking for simple queries — zero overhead
  if (ctx.D_q < 20) process.exit(0);

  // Increment iteration depth
  ctx.t += 1;

  // Estimate and accumulate context consumed
  const linesConsumed = _estimateLinesConsumed(toolName, toolInput, toolResponse);
  const LPrev = ctx.L_t;
  const kappaPrev = ctx.kappa_t;
  ctx.L_t += linesConsumed;

  // Update epistemic confidence kappa_t
  if (toolName === 'Read') {
    const relPath = _toRelative((toolInput || {}).file_path || '');
    if (relPath && !state.explored_files.includes(relPath)) {
      state.explored_files.push(relPath);
    }
    const boost = estimateConfidenceBoost(relPath, state, 'Read', toolInput);
    ctx.kappa_t = Math.min(100, ctx.kappa_t + boost);
  } else if (['Grep', 'Glob', 'Bash'].includes(toolName)) {
    const boost = estimateConfidenceBoost('', state, toolName, toolInput);
    ctx.kappa_t = Math.min(100, ctx.kappa_t + boost);
  }

  // Compute IGR and maintain rolling history (last 20)
  const igr = informationGainRate(ctx.kappa_t, kappaPrev, ctx.L_t, LPrev);
  ctx.igr_history.push(Math.round(igr * 10000) / 10000);
  if (ctx.igr_history.length > 20) ctx.igr_history = ctx.igr_history.slice(-20);

  // Ensure budget is set
  if (ctx.budget <= 0) ctx.budget = computeBudget(ctx.D_q, ctx.H_r);

  state.context_state = ctx;
  saveState(state);

  let { terminate, reason } = shouldTerminate(state);
  const contextParts = [];

  // Optional LLM confidence assessment every 5 tool calls
  let llmConfidence = null;
  if (isLlmAvailable() && ctx.t % 5 === 0 && ctx.t > 0) {
    const exploredSigs = [];
    for (const efile of (state.explored_files || []).slice(0, 15)) {
      const finfo = (repoMap.files || {})[efile] || {};
      for (const sig of (finfo.signatures || []).slice(0, 3)) {
        exploredSigs.push(`${efile}::${sig.name || ''}`);
      }
    }
    const exploredSet = new Set(state.explored_files || []);
    const remaining = Object.keys(state.candidates || {}).filter(p => !exploredSet.has(p));
    const budgetUsedPct = (ctx.L_t / Math.max(1, ctx.budget)) * 100;

    llmConfidence = assessConfidence(
      state._last_query || '',
      state.explored_files || [],
      exploredSigs,
      remaining,
      ctx.kappa_t,
      budgetUsedPct,
    );

    if (llmConfidence && llmConfidence.kappa_estimate != null) {
      const blended = 0.7 * llmConfidence.kappa_estimate + 0.3 * ctx.kappa_t;
      ctx.kappa_t = Math.min(100, blended);
      state.context_state = ctx;
      saveState(state);
      const result = shouldTerminate(state);
      terminate = result.terminate;
      reason = result.reason;

      if (llmConfidence.reasoning) {
        contextParts.push(
          `[TokenScout LLM Assessment] κ=${ctx.kappa_t.toFixed(1)} (${llmConfidence.reasoning})`
        );
      }
      if ((llmConfidence.next_targets || []).length > 0) {
        contextParts.push(
          `[TokenScout LLM Suggestion] Explore: ${llmConfidence.next_targets.join(', ')}`
        );
      }
    }
  }

  if (terminate) {
    contextParts.push(
      `[TokenScout Context] ${reason}. ` +
      `κ=${ctx.kappa_t.toFixed(1)}, L=${ctx.L_t}/${ctx.budget}, ` +
      `t=${ctx.t}, files_read=${(state.explored_files || []).length}. ` +
      `Consider synthesizing an answer from gathered context.`
    );
  } else if (ctx.t % 5 === 0 && !llmConfidence) {
    contextParts.push(
      `[TokenScout Status] Step ${ctx.t}: ` +
      `κ=${ctx.kappa_t.toFixed(1)}, L=${ctx.L_t}/${ctx.budget}, ` +
      `IGR=${igr.toFixed(4)}, files_read=${(state.explored_files || []).length}`
    );
  }

  if (contextParts.length > 0) {
    console.log(JSON.stringify({ additionalContext: contextParts.join('\n') }));
  }

  const auditEntry = {
    tool: toolName,
    lines_consumed: linesConsumed,
    L_t: ctx.L_t,
    kappa_t: Math.round(ctx.kappa_t * 100) / 100,
    igr: Math.round(igr * 10000) / 10000,
    t: ctx.t,
    budget: ctx.budget,
    terminate,
    reason,
  };
  if (llmConfidence && llmConfidence.kappa_estimate != null) {
    auditEntry.llm_kappa = Math.round(llmConfidence.kappa_estimate * 100) / 100;
    auditEntry.llm_reasoning = llmConfidence.reasoning || '';
  }
  auditLog('post_tool_use', auditEntry);
}

main();
