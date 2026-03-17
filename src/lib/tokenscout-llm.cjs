'use strict';
/**
 * tokenscout-llm.cjs — Claude Haiku integration for intelligent code navigation.
 * Ports tokenscout_llm.py:
 *   1. Query Augmentation (§3.2.1)
 *   2. Confidence Assessment (§3.3.1)
 *   3. Semantic Ranking (§3.2.2)
 *
 * Auth priority: OAuth (claude -p) > ANTHROPIC_API_KEY > null
 */

const { execFileSync, spawnSync } = require('child_process');
const https = require('https');
const path = require('path');
const fs = require('fs');

const HAIKU_MODEL = 'claude-haiku-4-5-20251001';
const CLI_MODEL = 'haiku';
const MAX_TOKENS_AUGMENT = 300;
const MAX_TOKENS_CONFIDENCE = 200;
const MAX_TOKENS_RANKING = 400;
const API_TIMEOUT_MS = 8000;
const CLI_TIMEOUT_MS = 15000;

// Cached detection result: 'oauth' | 'api_key' | '' (none)
let _llmMethod = null;

function _detectLlmMethod() {
  if (_llmMethod !== null) return _llmMethod || null;
  // 1. OAuth via claude CLI
  try {
    execFileSync('which', ['claude'], { stdio: 'ignore' });
    _llmMethod = 'oauth';
    return 'oauth';
  } catch (_) {}
  // 2. API key
  if ((process.env.ANTHROPIC_API_KEY || '').trim()) {
    _llmMethod = 'api_key';
    return 'api_key';
  }
  _llmMethod = '';
  return null;
}

function isLlmAvailable() {
  // TOKENSCOUT_FAST=1 disables nested LLM calls (saves ~30-60s per query)
  if (process.env.TOKENSCOUT_FAST === '1') return false;
  return _detectLlmMethod() !== null;
}

function _logLlmError(event, error) {
  try {
    const dir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
    const p = path.join(dir, '.claude', '.tokenscout', 'audit.jsonl');
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.appendFileSync(p, JSON.stringify({ ts: Date.now() / 1000, event, error }) + '\n');
  } catch (_) {}
}

function _callHaikuOAuth(system, userMessage) {
  try {
    const prompt = `<system>${system}</system>\n\n${userMessage}`;
    const result = spawnSync('claude', [
      '-p', '--model', CLI_MODEL,
      '--output-format', 'text',
      '--no-session-persistence',
    ], { input: prompt, timeout: CLI_TIMEOUT_MS, encoding: 'utf-8' });

    if (result.status === 0 && result.stdout && result.stdout.trim()) {
      return result.stdout.trim();
    }
    if (result.stderr && result.stderr.trim()) {
      _logLlmError('oauth_stderr', result.stderr.slice(0, 200));
    }
    if (result.error) {
      if (result.error.code === 'ENOENT') {
        _llmMethod = null;
        _logLlmError('oauth_not_found', 'claude CLI not found');
      } else if (result.error.code === 'ETIMEDOUT') {
        _logLlmError('oauth_timeout', `CLI timed out after ${CLI_TIMEOUT_MS}ms`);
      } else {
        _logLlmError('oauth_failed', String(result.error).slice(0, 200));
      }
    }
  } catch (e) {
    _logLlmError('oauth_failed', String(e).slice(0, 200));
  }
  return null;
}

function _callHaikuApiKey(system, userMessage, maxTokens) {
  const apiKey = (process.env.ANTHROPIC_API_KEY || '').trim();
  if (!apiKey) return null;

  const payload = Buffer.from(JSON.stringify({
    model: HAIKU_MODEL,
    max_tokens: maxTokens,
    messages: [{ role: 'user', content: userMessage }],
    system,
  }));

  return new Promise((resolve) => {
    const req = https.request({
      hostname: 'api.anthropic.com',
      path: '/v1/messages',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': apiKey,
        'anthropic-version': '2023-06-01',
        'Content-Length': payload.length,
      },
      timeout: API_TIMEOUT_MS,
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        try {
          const data = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
          for (const block of (data.content || [])) {
            if (block.type === 'text') { resolve(block.text); return; }
          }
          resolve(null);
        } catch (e) {
          _logLlmError('api_parse_failed', String(e).slice(0, 200));
          resolve(null);
        }
      });
    });
    req.on('timeout', () => { req.destroy(); _logLlmError('api_timeout', 'request timed out'); resolve(null); });
    req.on('error', (e) => { _logLlmError('api_key_call_failed', String(e).slice(0, 200)); resolve(null); });
    req.write(payload);
    req.end();
  });
}

// Synchronous wrapper — hooks run synchronously
function callHaiku(system, userMessage, maxTokens = 200) {
  const method = _detectLlmMethod();
  if (!method) return null;
  if (method === 'oauth') return _callHaikuOAuth(system, userMessage);
  // API key path uses synchronous-compatible Promise flush via execFileSync trick
  // We use a sync approach: write payload to temp file and do blocking HTTP
  const apiKey = (process.env.ANTHROPIC_API_KEY || '').trim();
  if (!apiKey) return null;
  try {
    const payload = JSON.stringify({
      model: HAIKU_MODEL,
      max_tokens: maxTokens,
      messages: [{ role: 'user', content: userMessage }],
      system,
    });
    // Use node to make a synchronous HTTPS call via child process
    const script = `
const https=require('https');
const payload=Buffer.from(${JSON.stringify(payload)});
const req=https.request({hostname:'api.anthropic.com',path:'/v1/messages',method:'POST',
headers:{'Content-Type':'application/json','x-api-key':${JSON.stringify(apiKey)},
'anthropic-version':'2023-06-01','Content-Length':payload.length}},res=>{
const c=[];res.on('data',d=>c.push(d));res.on('end',()=>{
try{const d=JSON.parse(Buffer.concat(c).toString());
for(const b of(d.content||[])){if(b.type==='text'){process.stdout.write(b.text);break;}}}catch(e){}
});});
req.setTimeout(${API_TIMEOUT_MS},()=>{req.destroy();});
req.on('error',()=>{});req.write(payload);req.end();`;
    const result = spawnSync(process.execPath, ['-e', script], {
      timeout: API_TIMEOUT_MS + 2000,
      encoding: 'utf-8',
    });
    return (result.stdout && result.stdout.trim()) ? result.stdout.trim() : null;
  } catch (e) {
    _logLlmError('api_key_call_failed', String(e).slice(0, 200));
    return null;
  }
}

function _parseJson(text) {
  let t = (text || '').trim();
  if (t.startsWith('```')) t = t.split('\n').slice(1).join('\n').replace(/```[\s\S]*$/, '');
  return JSON.parse(t);
}

// ─── 1. Query Augmentation ────────────────────────────────────────────────────

function augmentQuery(prompt, repoSummary, currentCandidates) {
  if (!isLlmAvailable()) return { expanded_terms: [], D_q: null, intent: null, strategy: null };
  const system = 'You are a code navigation assistant. Analyze the user\'s query about a codebase and respond with ONLY a JSON object (no markdown, no explanation).';
  const candidatesStr = (currentCandidates || []).slice(0, 10).join(', ') || 'none yet';
  const userMsg = `Analyze this code query and help me find relevant files.\n\nQuery: "${prompt}"\n\nRepository summary:\n${(repoSummary || '').slice(0, 1500)}\n\nCurrent candidate files: ${candidatesStr}\n\nRespond with ONLY this JSON:\n{\n  "expanded_terms": ["list", "of", "additional", "search", "keywords", "synonyms"],\n  "D_q": <complexity 0-100>,\n  "intent": "<one of: bug_fix, feature_understanding, refactor, architecture, debugging, testing, documentation>",\n  "strategy": "<brief 1-line strategy suggestion>"\n}`;

  const response = callHaiku(system, userMsg, MAX_TOKENS_AUGMENT);
  if (!response) return { expanded_terms: [], D_q: null, intent: null, strategy: null };
  try {
    const result = _parseJson(response);
    return {
      expanded_terms: (result.expanded_terms || []).slice(0, 15),
      D_q: Math.min(100, Math.max(0, parseInt(result.D_q || 50, 10))),
      intent: result.intent || 'unknown',
      strategy: result.strategy || '',
    };
  } catch (_) {
    return { expanded_terms: [], D_q: null, intent: null, strategy: null };
  }
}

// ─── 2. Confidence Assessment ─────────────────────────────────────────────────

function assessConfidence(query, exploredFiles, exploredSignatures, candidatesRemaining, currentKappa, budgetUsedPct) {
  if (!isLlmAvailable()) return { kappa_estimate: null, reasoning: null, should_continue: null, next_targets: [] };
  const system = 'You are a code navigation confidence assessor. Evaluate whether enough codebase context has been gathered to answer the query. Respond with ONLY a JSON object.';
  const exploredStr = (exploredFiles || []).slice(0, 15).map(f => `  - ${f}`).join('\n');
  const sigsStr = (exploredSignatures || []).slice(0, 20).map(s => `  - ${s}`).join('\n');
  const remainingStr = (candidatesRemaining || []).slice(0, 10).join(', ');
  const userMsg = `Query: "${query}"\n\nFiles explored (${(exploredFiles || []).length}):\n${exploredStr}\n\nKey signatures found:\n${sigsStr}\n\nUnexplored candidates: ${remainingStr}\nCurrent heuristic confidence: ${currentKappa.toFixed(1)}/100\nBudget used: ${budgetUsedPct.toFixed(0)}%\n\nRespond with ONLY this JSON:\n{\n  "kappa_estimate": <your confidence estimate 0-100>,\n  "reasoning": "<brief explanation>",\n  "should_continue": <true/false>,\n  "next_targets": ["file1", "file2"]\n}`;

  const response = callHaiku(system, userMsg, MAX_TOKENS_CONFIDENCE);
  if (!response) return { kappa_estimate: null, reasoning: null, should_continue: null, next_targets: [] };
  try {
    const result = _parseJson(response);
    return {
      kappa_estimate: Math.min(100, Math.max(0, parseFloat(result.kappa_estimate || 50))),
      reasoning: result.reasoning || '',
      should_continue: !!result.should_continue,
      next_targets: (result.next_targets || []).slice(0, 5),
    };
  } catch (_) {
    return { kappa_estimate: null, reasoning: null, should_continue: null, next_targets: [] };
  }
}

// ─── 3. Semantic Ranking ──────────────────────────────────────────────────────

function semanticRankCandidates(query, candidates, fileSummaries) {
  if (!isLlmAvailable() || !candidates || Object.keys(candidates).length === 0) {
    return Object.entries(candidates || {}).map(([p, info]) => [p, info.score || 0]);
  }
  const system = 'You are a code relevance ranker. Given a query and candidate files, rank them by relevance. Respond with ONLY a JSON array.';
  const candDesc = Object.entries(candidates).slice(0, 15).map(([p, info]) => {
    const summary = (fileSummaries || {})[p] || 'no summary';
    return `  ${p} (bm25=${(info.score || 0).toFixed(2)}): ${summary.slice(0, 100)}`;
  }).join('\n');
  const userMsg = `Query: "${query}"\n\nCandidate files:\n${candDesc}\n\nRank these files by relevance to the query. Respond with ONLY a JSON array of objects:\n[{"path": "file/path", "score": 0.0-1.0, "reason": "brief"}]\n\nOrder from most to least relevant. Score 1.0 = highly relevant, 0.0 = irrelevant.`;

  const response = callHaiku(system, userMsg, MAX_TOKENS_RANKING);
  if (!response) return Object.entries(candidates).map(([p, info]) => [p, info.score || 0]);
  try {
    const rankings = _parseJson(response);
    const result = [];
    const rankedPaths = new Set();
    for (const item of rankings) {
      const p = item.path || '';
      const score = Math.min(1.0, Math.max(0.0, parseFloat(item.score || 0)));
      if (p && p in candidates) { result.push([p, score]); rankedPaths.add(p); }
    }
    for (const [p, info] of Object.entries(candidates)) {
      if (!rankedPaths.has(p)) result.push([p, (info.score || 0) * 0.5]);
    }
    return result;
  } catch (_) {
    return Object.entries(candidates).map(([p, info]) => [p, info.score || 0]);
  }
}

// ─── Repo summary utilities ───────────────────────────────────────────────────

function buildRepoSummary(repoMap) {
  const stats = (repoMap || {}).stats || {};
  const files = (repoMap || {}).files || {};
  const symbols = (repoMap || {}).symbols || {};
  const parts = [
    `Files: ${stats.total_files || 0}`,
    `Lines: ${stats.total_lines || 0}`,
    `Languages: ${JSON.stringify(stats.languages || {})}`,
  ];
  const dirCounts = {};
  for (const p of Object.keys(files)) {
    const d = p.split('/')[0] || 'root';
    dirCounts[d] = (dirCounts[d] || 0) + 1;
  }
  const topDirs = Object.entries(dirCounts).sort((a, b) => b[1] - a[1]).slice(0, 8);
  parts.push(`Key dirs: ${topDirs.map(([d, n]) => `${d}(${n})`).join(', ')}`);
  const classes = [];
  for (const [key, info] of Object.entries(symbols).slice(0, 300)) {
    if (['class', 'struct', 'interface'].includes(info.type)) {
      classes.push(key.includes('::') ? key.split('::').pop() : key);
    }
  }
  if (classes.length) parts.push(`Key types: ${classes.slice(0, 15).join(', ')}`);
  return parts.join('\n');
}

function buildFileSummaries(repoMap) {
  const files = (repoMap || {}).files || {};
  const summaries = {};
  for (const [p, info] of Object.entries(files)) {
    const sigs = (info.signatures || []).slice(0, 5).map(s => s.name || '').filter(Boolean);
    summaries[p] = sigs.length
      ? `${info.lang || '?'} (${info.lines || 0} lines): ${sigs.join(', ')}`
      : `${info.lang || '?'} (${info.lines || 0} lines)`;
  }
  return summaries;
}

module.exports = {
  isLlmAvailable,
  callHaiku,
  augmentQuery,
  assessConfidence,
  semanticRankCandidates,
  buildRepoSummary,
  buildFileSummaries,
};
