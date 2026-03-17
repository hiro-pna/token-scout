'use strict';
/**
 * report-html-template-builder.cjs
 * Self-contained HTML template builder for benchmark reports.
 * No external dependencies — all CSS inline.
 */

const CSS = [
  '* { box-sizing: border-box; margin: 0; padding: 0; }',
  "body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; padding: 2rem; }",
  'h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: .25rem; color: #f8fafc; }',
  '.subtitle { color: #94a3b8; margin-bottom: 2rem; font-size: .9rem; }',
  '.summary-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 1rem; margin-bottom: 2rem; }',
  '.stat-card { background: #1e2433; border: 1px solid #2d3748; border-radius: 8px; padding: 1.25rem; }',
  '.stat-value { font-size: 2rem; font-weight: 700; color: #38bdf8; }',
  '.stat-label { font-size: .8rem; color: #64748b; margin-top: .25rem; text-transform: uppercase; letter-spacing: .05em; }',
  '.prompt-card { background: #1e2433; border: 1px solid #2d3748; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.25rem; }',
  '.prompt-header { display: flex; gap: .75rem; align-items: center; margin-bottom: .75rem; flex-wrap: wrap; }',
  '.prompt-id { font-weight: 700; color: #38bdf8; } .prompt-cat { background: #2d3748; padding: .2rem .6rem; border-radius: 9999px; font-size: .75rem; color: #94a3b8; }',
  '.savings { font-size: .8rem; padding: .2rem .6rem; border-radius: 4px; }',
  '.positive { background: #064e3b; color: #34d399; } .negative { background: #7f1d1d; color: #f87171; }',
  '.prompt-text { color: #94a3b8; font-size: .9rem; margin-bottom: 1rem; line-height: 1.5; }',
  '.chart-row { margin-bottom: .75rem; } .chart-label { font-size: .75rem; color: #64748b; text-transform: uppercase; margin-bottom: .35rem; }',
  '.bar-group { display: flex; align-items: center; gap: .5rem; margin-bottom: .2rem; }',
  '.bar { height: 14px; border-radius: 2px; min-width: 2px; } .baseline-bar { background: #475569; } .tokenscout-bar { background: #0284c7; }',
  ".bar-legend { font-size: .7rem; color: #64748b; width: 80px; } .baseline-dot::before { content: '\\25A0 '; color: #475569; } .tokenscout-dot::before { content: '\\25A0 '; color: #0284c7; }",
  '.bar-value { font-size: .75rem; color: #94a3b8; }',
  '.quality-table { width: 100%; border-collapse: collapse; font-size: .85rem; }',
  '.quality-table th, .quality-table td { padding: .5rem .75rem; text-align: left; border-bottom: 1px solid #2d3748; }',
  '.quality-table th { color: #64748b; font-weight: 600; text-transform: uppercase; font-size: .75rem; }',
  '.total-row td { font-weight: 700; color: #f8fafc; }',
  'h2 { font-size: 1.2rem; font-weight: 600; margin-bottom: 1rem; color: #f8fafc; }'
].join('\n');

function f(n) { return typeof n === 'number' ? n.toFixed(1) : 'N/A'; }
function num(n) { return typeof n === 'number' ? n.toLocaleString() : 'N/A'; }
function pct(n) { return typeof n === 'number' ? n.toFixed(1) + '%' : 'N/A'; }

function barChart(label, bVal, tVal, maxVal) {
  const bW = maxVal > 0 ? Math.round((bVal / maxVal) * 200) : 0;
  const tW = maxVal > 0 ? Math.round((tVal / maxVal) * 200) : 0;
  return `<div class="chart-row"><div class="chart-label">${label}</div>` +
    `<div class="bar-group"><span class="bar-legend baseline-dot">Baseline</span><div class="bar baseline-bar" style="width:${bW}px"></div><span class="bar-value">${num(bVal)}</span></div>` +
    `<div class="bar-group"><span class="bar-legend tokenscout-dot">TokenScout</span><div class="bar tokenscout-bar" style="width:${tW}px"></div><span class="bar-value">${num(tVal)}</span></div></div>`;
}

function promptCard(p) {
  const maxTok = Math.max(p.baseline.tokens, p.tokenscout.tokens, 1);
  const maxFiles = Math.max(p.baseline.files_read, p.tokenscout.files_read, 1);
  const sc = p.token_savings_pct >= 0 ? 'positive' : 'negative';
  const qc = p.quality_delta >= 0 ? 'positive' : 'negative';
  const qd = (p.quality_delta >= 0 ? '+' : '') + f(p.quality_delta);
  return `<div class="prompt-card">
  <div class="prompt-header">
    <span class="prompt-id">#${p.id}</span><span class="prompt-cat">${p.category}</span>
    <span class="savings ${sc}">Token savings: ${pct(p.token_savings_pct)}</span>
    <span class="savings ${qc}">Quality delta: ${qd}</span>
  </div>
  <p class="prompt-text">${p.prompt}</p>
  <div class="charts">${barChart('Tokens', p.baseline.tokens, p.tokenscout.tokens, maxTok)}${barChart('Files read', p.baseline.files_read, p.tokenscout.files_read, maxFiles)}</div>
  <table class="quality-table"><thead><tr><th>Metric</th><th>Baseline</th><th>TokenScout</th></tr></thead><tbody>
    <tr><td>Accuracy</td><td>${f(p.baseline.quality.accuracy)}/5</td><td>${f(p.tokenscout.quality.accuracy)}/5</td></tr>
    <tr><td>Completeness</td><td>${f(p.baseline.quality.completeness)}/5</td><td>${f(p.tokenscout.quality.completeness)}/5</td></tr>
    <tr><td>Relevance</td><td>${f(p.baseline.quality.relevance)}/5</td><td>${f(p.tokenscout.quality.relevance)}/5</td></tr>
    <tr class="total-row"><td>Total</td><td>${f(p.baseline.quality.total)}/15</td><td>${f(p.tokenscout.quality.total)}/15</td></tr>
    <tr><td>Wall time</td><td>${num(p.baseline.wall_time_ms)}ms</td><td>${num(p.tokenscout.wall_time_ms)}ms</td></tr>
    <tr><td>Final kappa</td><td>—</td><td>${f(p.tokenscout.final_kappa)}</td></tr>
  </tbody></table>
</div>`;
}

/**
 * Build self-contained HTML report string.
 * @param {{ summary: object, prompts: object[] }} report
 * @returns {string}
 */
function buildHtml(report) {
  const { summary, prompts } = report;
  const sc = summary.avg_token_savings_pct >= 0 ? 'positive' : 'negative';
  const qc = summary.avg_quality_delta >= 0 ? 'positive' : 'negative';
  const qd = (summary.avg_quality_delta >= 0 ? '+' : '') + f(summary.avg_quality_delta);
  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>TokenScout Benchmark Report</title><style>${CSS}</style></head><body>
<h1>TokenScout Benchmark Report</h1>
<p class="subtitle">Repo: ${summary.repo || 'N/A'} &nbsp;|&nbsp; ${summary.total_prompts} prompts &nbsp;|&nbsp; ${summary.timestamp || ''}</p>
<div class="summary-grid">
  <div class="stat-card"><div class="stat-value ${sc}">${pct(summary.avg_token_savings_pct)}</div><div class="stat-label">Avg Token Savings</div></div>
  <div class="stat-card"><div class="stat-value">${pct(summary.avg_file_savings_pct)}</div><div class="stat-label">Avg File Read Savings</div></div>
  <div class="stat-card"><div class="stat-value ${qc}">${qd}</div><div class="stat-label">Avg Quality Delta</div></div>
</div>
<h2>Per-Prompt Results</h2>
${prompts.map(promptCard).join('\n')}
</body></html>`;
}

module.exports = { buildHtml };
