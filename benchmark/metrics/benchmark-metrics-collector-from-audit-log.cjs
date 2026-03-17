'use strict';
/**
 * benchmark-metrics-collector-from-audit-log.cjs
 * Parses TokenScout audit.jsonl and extracts structured metrics.
 */

const fs = require('fs');

/**
 * Parse all lines from audit.jsonl into event objects.
 * @param {string} auditLogPath
 * @returns {object[]}
 */
function parseAuditLog(auditLogPath) {
  if (!fs.existsSync(auditLogPath)) return [];
  return fs.readFileSync(auditLogPath, 'utf-8')
    .split('\n')
    .filter(Boolean)
    .map(line => { try { return JSON.parse(line); } catch { return null; } })
    .filter(Boolean);
}

/**
 * Collect TokenScout metrics from an audit log file.
 * @param {string} auditLogPath
 * @returns {object}
 */
function collectMetrics(auditLogPath) {
  const events = parseAuditLog(auditLogPath);

  const session = { total_files: 0, total_lines: 0, entropy: 0, scan_time_ms: 0 };
  const query = { D_q: 0, budget: 0, num_candidates: 0, llm_available: false };
  const exploration = {
    files_read: 0,
    files_scouted: 0,
    total_lines_consumed: 0,
    tool_calls: 0,
    final_kappa: 0,
    igr_values: [],
    termination_reason: 'unknown'
  };
  const timing = { total_hook_time_ms: 0, per_hook_avg_ms: 0 };

  let hookCount = 0;
  const scoutedFiles = new Set();
  const readFiles = new Set();

  for (const ev of events) {
    switch (ev.event) {
      case 'session_start_scan':
        session.total_files = ev.total_files || 0;
        session.total_lines = ev.total_lines || 0;
        session.entropy = ev.entropy || 0;
        session.scan_time_ms = ev.scan_time_ms || 0;
        break;

      case 'query_augmentation':
        query.D_q = ev.D_q || 0;
        query.budget = ev.budget || 0;
        query.num_candidates = ev.num_candidates || 0;
        query.llm_available = !!ev.llm_available;
        break;

      case 'pre_read_scout':
        if (ev.file) scoutedFiles.add(ev.file);
        if (ev.hook_time_ms) { timing.total_hook_time_ms += ev.hook_time_ms; hookCount++; }
        break;

      case 'post_tool_use':
        exploration.tool_calls++;
        exploration.total_lines_consumed += ev.lines_consumed || ev.L_t || 0;
        if (ev.igr != null) exploration.igr_values.push(ev.igr);
        if (ev.kappa_t != null) exploration.final_kappa = ev.kappa_t;
        if (ev.file) readFiles.add(ev.file);
        if (ev.hook_time_ms) { timing.total_hook_time_ms += ev.hook_time_ms; hookCount++; }
        break;

      case 'stop_allowed':
      case 'stop_blocked':
        exploration.final_kappa = ev.kappa != null ? ev.kappa : exploration.final_kappa;
        exploration.termination_reason = ev.event === 'stop_allowed' ? 'confidence_met' : 'confidence_low';
        if (ev.files_explored != null) exploration.files_read = ev.files_explored;
        break;
    }
  }

  exploration.files_scouted = scoutedFiles.size;
  if (exploration.files_read === 0) exploration.files_read = readFiles.size;
  timing.per_hook_avg_ms = hookCount > 0 ? timing.total_hook_time_ms / hookCount : 0;

  return { session, query, exploration, timing };
}

/**
 * Parse claude -p JSON output for baseline metrics.
 * claude --output-format json returns structured usage data.
 * @param {string|object} claudeOutput — raw stdout string or parsed object
 * @returns {object}
 */
function collectBaselineMetrics(claudeOutput) {
  let parsed = claudeOutput;
  if (typeof claudeOutput === 'string') {
    try { parsed = JSON.parse(claudeOutput); } catch { parsed = {}; }
  }

  // claude -p --output-format json returns: { usage: { input_tokens, output_tokens, cache_* }, result, ... }
  const usage = parsed.usage || {};
  const modelUsage = parsed.modelUsage || {};

  // Extract token counts (including cache tokens for accurate billing)
  const inputTokens = usage.input_tokens || 0;
  const outputTokens = usage.output_tokens || 0;
  const cacheCreation = usage.cache_creation_input_tokens || 0;
  const cacheRead = usage.cache_read_input_tokens || 0;

  // Total cost from claude CLI
  const costUsd = parsed.total_cost_usd || 0;

  // Duration
  const durationMs = parsed.duration_ms || 0;
  const durationApiMs = parsed.duration_api_ms || 0;
  const numTurns = parsed.num_turns || 0;

  // Response text
  const response = parsed.result || parsed.content || '';
  const responseText = typeof response === 'string' ? response : JSON.stringify(response);

  return {
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    cache_creation_tokens: cacheCreation,
    cache_read_tokens: cacheRead,
    total_tokens: inputTokens + outputTokens + cacheCreation + cacheRead,
    cost_usd: Math.round(costUsd * 1000000) / 1000000,
    duration_ms: durationMs,
    duration_api_ms: durationApiMs,
    num_turns: numTurns,
    response_length: responseText.length,
  };
}

module.exports = { collectMetrics, collectBaselineMetrics };
