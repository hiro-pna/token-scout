'use strict';
/**
 * benchmark-report-generator-html-and-json.cjs
 * Reads raw benchmark results JSON, emits structured report.json + report.html.
 * HTML generation is delegated to report-html-template-builder.cjs.
 */

const fs = require('fs');
const path = require('path');
const { buildHtml } = require('./report-html-template-builder.cjs');

// ── Helpers ───────────────────────────────────────────────────────────────────

function savingsPct(baseline, tokenscout) {
  if (!baseline || baseline === 0) return 0;
  return ((baseline - tokenscout) / baseline) * 100;
}

// ── Build structured report.json from raw results ─────────────────────────────

function buildReportJson(raw) {
  const prompts = raw.prompts || [];
  const summary = {
    repo: raw.repo,
    timestamp: raw.timestamp,
    total_prompts: prompts.length,
    avg_token_savings_pct: 0,
    avg_quality_delta: 0,
    avg_file_savings_pct: 0
  };

  let totalTokenSavings = 0, totalQualityDelta = 0, totalFileSavings = 0;

  const rows = prompts.map(p => {
    const bTokens = p.baseline_metrics?.total_tokens || 0;
    const tTokens = p.tokenscout_metrics?.total_tokens ||
      p.tokenscout_exploration?.total_lines_consumed || 0;
    const tokenSavPct = savingsPct(bTokens, tTokens);

    const bFiles = (p.baseline_metrics?.files_read || []).length;
    const tFiles = p.tokenscout_exploration?.files_read || 0;
    const fileSavPct = savingsPct(bFiles, tFiles);

    const bQuality = p.quality?.baseline?.total || 0;
    const tQuality = p.quality?.tokenscout?.total || 0;
    const qualityDelta = tQuality - bQuality;

    totalTokenSavings += tokenSavPct;
    totalQualityDelta += qualityDelta;
    totalFileSavings += fileSavPct;

    return {
      id: p.id,
      category: p.category,
      prompt: p.prompt,
      token_savings_pct: tokenSavPct,
      file_savings_pct: fileSavPct,
      quality_delta: qualityDelta,
      baseline: {
        tokens: bTokens,
        files_read: bFiles,
        tool_calls: p.baseline_metrics?.tool_calls || 0,
        quality: p.quality?.baseline || {},
        wall_time_ms: p.baseline_wall_time_ms || 0
      },
      tokenscout: {
        tokens: tTokens,
        files_read: tFiles,
        tool_calls: p.tokenscout_exploration?.tool_calls || 0,
        quality: p.quality?.tokenscout || {},
        wall_time_ms: p.tokenscout_wall_time_ms || 0,
        final_kappa: p.tokenscout_exploration?.final_kappa || 0
      }
    };
  });

  const n = prompts.length || 1;
  summary.avg_token_savings_pct = totalTokenSavings / n;
  summary.avg_quality_delta = totalQualityDelta / n;
  summary.avg_file_savings_pct = totalFileSavings / n;

  return { summary, prompts: rows };
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Generate report.json and report.html from raw benchmark results.
 * @param {string} resultsPath — path to raw-<timestamp>.json
 * @param {string} outputDir
 * @returns {{ jsonOut: string, htmlOut: string, report: object }}
 */
function generateReport(resultsPath, outputDir) {
  const raw = JSON.parse(fs.readFileSync(resultsPath, 'utf-8'));
  fs.mkdirSync(outputDir, { recursive: true });

  const report = buildReportJson(raw);

  const jsonOut = path.join(outputDir, 'report.json');
  fs.writeFileSync(jsonOut, JSON.stringify(report, null, 2));

  const htmlOut = path.join(outputDir, 'report.html');
  fs.writeFileSync(htmlOut, buildHtml(report));

  console.log(`Report JSON: ${jsonOut}`);
  console.log(`Report HTML: ${htmlOut}`);

  return { jsonOut, htmlOut, report };
}

module.exports = { generateReport };

// CLI entry point
if (require.main === module) {
  const args = process.argv.slice(2);
  const resultsPath = args[0];
  const outputDir = args[1] || path.dirname(resultsPath);
  if (!resultsPath) {
    console.error('Usage: node benchmark-report-generator-html-and-json.cjs <results.json> [output-dir]');
    process.exit(1);
  }
  generateReport(resultsPath, outputDir);
}
