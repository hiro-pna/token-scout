'use strict';
/**
 * benchmark-runner-orchestrator.cjs
 * Main orchestrator: clones a repo twice, runs prompts against baseline and
 * TokenScout-instrumented versions, captures metrics, generates report.
 *
 * Usage:
 *   node benchmark/benchmark-runner-orchestrator.cjs \
 *     --repo <github-url> \
 *     --prompts <prompts-file.json> \
 *     [--output-dir benchmark/results/]
 */

const { execSync, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const { collectMetrics, collectBaselineMetrics } = require('./metrics/benchmark-metrics-collector-from-audit-log.cjs');
const { scoreQuality } = require('./metrics/benchmark-quality-scorer-llm-judge.cjs');
const { generateReport } = require('./benchmark-report-generator-html-and-json.cjs');

const SCRIPT_DIR = path.resolve(__dirname);
const REPO_ROOT = path.resolve(SCRIPT_DIR, '..');
const INSTALL_SH = path.join(REPO_ROOT, 'install.sh');
const PROMPT_TIMEOUT_MS = 180000; // 3 minutes to accommodate hook LLM calls

// ── CLI arg parsing ───────────────────────────────────────────────────────────

function parseArgs(argv) {
  const args = { repo: null, prompts: null, outputDir: path.join(SCRIPT_DIR, 'results') };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--repo') args.repo = argv[++i];
    else if (argv[i] === '--prompts') args.prompts = argv[++i];
    else if (argv[i] === '--output-dir') args.outputDir = argv[++i];
  }
  return args;
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function repoHash(url) {
  return crypto.createHash('sha256').update(url).digest('hex').slice(0, 8);
}

function timestamp() {
  return new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
}

function log(msg) { console.log(`[bench] ${msg}`); }

function cloneRepo(url, destDir) {
  log(`Cloning ${url} -> ${destDir}`);
  execSync(`git clone --depth 1 "${url}" "${destDir}"`, { stdio: 'pipe', timeout: 120000 });
}

function installTokenScout(targetDir) {
  log(`Installing TokenScout in ${targetDir}`);
  execSync(`bash "${INSTALL_SH}" "${targetDir}"`, { stdio: 'pipe', cwd: REPO_ROOT });
}

// ── Run a single claude prompt ────────────────────────────────────────────────

function runClaudePrompt(promptText, workDir) {
  const start = Date.now();
  const result = spawnSync('claude', [
    '-p', promptText,
    '--output-format', 'json',
    '--no-session-persistence'
  ], {
    cwd: workDir,
    encoding: 'utf-8',
    timeout: PROMPT_TIMEOUT_MS,
    env: { ...process.env, CLAUDE_PROJECT_DIR: workDir, TOKENSCOUT_FAST: '1' }
  });

  return {
    stdout: result.stdout || '',
    stderr: result.stderr || '',
    exitCode: result.status,
    wallTimeMs: Date.now() - start,
    timedOut: result.error && result.error.code === 'ETIMEDOUT'
  };
}

// ── Audit log path for a given target repo dir ────────────────────────────────

function auditLogPath(targetDir) {
  return path.join(targetDir, '.claude', '.tokenscout', 'audit.jsonl');
}

function clearAuditLog(targetDir) {
  const p = auditLogPath(targetDir);
  if (fs.existsSync(p)) fs.writeFileSync(p, '');
}

// ── Per-prompt benchmark run ──────────────────────────────────────────────────

function runPrompt(promptObj, baselineDir, tokenscoutDir) {
  const { id, prompt, category } = promptObj;
  log(`  [${id}] Running baseline...`);

  const baselineRun = runClaudePrompt(prompt, baselineDir);
  const baselineMetrics = collectBaselineMetrics(baselineRun.stdout);

  log(`  [${id}] Running TokenScout...`);
  clearAuditLog(tokenscoutDir);
  const tsRun = runClaudePrompt(prompt, tokenscoutDir);
  const tsMetrics = collectMetrics(auditLogPath(tokenscoutDir));
  const tsBaseMetrics = collectBaselineMetrics(tsRun.stdout);

  log(`  [${id}] Scoring quality...`);
  const baselineText = extractResponseText(baselineRun.stdout);
  const tsText = extractResponseText(tsRun.stdout);
  const quality = scoreQuality(prompt, baselineText, tsText);

  return {
    id,
    category,
    prompt,
    baseline_wall_time_ms: baselineRun.wallTimeMs,
    tokenscout_wall_time_ms: tsRun.wallTimeMs,
    baseline_exit_code: baselineRun.exitCode,
    tokenscout_exit_code: tsRun.exitCode,
    baseline_metrics: baselineMetrics,
    tokenscout_metrics: tsBaseMetrics,
    tokenscout_session: tsMetrics.session,
    tokenscout_query: tsMetrics.query,
    tokenscout_exploration: tsMetrics.exploration,
    tokenscout_timing: tsMetrics.timing,
    quality
  };
}

function extractResponseText(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    const r = parsed.result || parsed.content || parsed.response || '';
    return typeof r === 'string' ? r : JSON.stringify(r);
  } catch {
    return stdout;
  }
}

// ── Main ──────────────────────────────────────────────────────────────────────

function main() {
  const args = parseArgs(process.argv.slice(2));

  if (!args.repo) { console.error('ERROR: --repo <url> is required'); process.exit(1); }
  if (!args.prompts) { console.error('ERROR: --prompts <file> is required'); process.exit(1); }
  if (!fs.existsSync(args.prompts)) { console.error(`ERROR: prompts file not found: ${args.prompts}`); process.exit(1); }

  const promptsData = JSON.parse(fs.readFileSync(args.prompts, 'utf-8'));
  const prompts = promptsData.prompts || [];
  const repoUrl = args.repo || promptsData.repo;

  if (!repoUrl) { console.error('ERROR: no repo URL (--repo or prompts.repo)'); process.exit(1); }

  const hash = repoHash(repoUrl);
  const baselineDir = `/tmp/bench-baseline-${hash}`;
  const tokenscoutDir = `/tmp/bench-tokenscout-${hash}`;

  // Clone repos (skip if already present from a prior run)
  if (!fs.existsSync(baselineDir)) cloneRepo(repoUrl, baselineDir);
  else log(`Reusing cached baseline clone at ${baselineDir}`);

  if (!fs.existsSync(tokenscoutDir)) cloneRepo(repoUrl, tokenscoutDir);
  else log(`Reusing cached tokenscout clone at ${tokenscoutDir}`);

  // Install TokenScout into the tokenscout copy
  installTokenScout(tokenscoutDir);

  // Run prompts
  log(`Running ${prompts.length} prompt(s)...`);
  const results = prompts.map(p => {
    try { return runPrompt(p, baselineDir, tokenscoutDir); }
    catch (err) {
      log(`  ERROR on prompt [${p.id}]: ${err.message}`);
      return { id: p.id, category: p.category, prompt: p.prompt, error: err.message };
    }
  });

  // Save raw results
  fs.mkdirSync(args.outputDir, { recursive: true });
  const ts = timestamp();
  const rawPath = path.join(args.outputDir, `raw-${ts}.json`);
  const raw = { repo: repoUrl, timestamp: ts, prompts: results };
  fs.writeFileSync(rawPath, JSON.stringify(raw, null, 2));
  log(`Raw results saved: ${rawPath}`);

  // Generate report
  generateReport(rawPath, args.outputDir);
  log('Benchmark complete.');
}

main();
