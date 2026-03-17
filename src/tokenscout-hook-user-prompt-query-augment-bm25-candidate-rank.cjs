#!/usr/bin/env node
'use strict';
/**
 * tokenscout-hook-user-prompt-query-augment-bm25-candidate-rank.cjs
 *
 * Claude Code UserPromptSubmit hook.
 * Ports user_prompt_hook.py:
 *   - Estimates query complexity D_q (heuristic + optional LLM)
 *   - Computes dynamic budget B = D_q * H_r
 *   - BM25 sparse retrieval + keyword candidate merging
 *   - Optional LLM semantic re-ranking
 *   - Outputs additionalContext JSON with top candidates
 */

const fs = require('fs');
const { loadState, saveState, auditLog } = require('./lib/tokenscout-state.cjs');
const { computeBudget, priorityScore } = require('./lib/tokenscout-confidence-and-budget.cjs');
const { createBM25Index, indexRepo: bm25IndexRepo, query: bm25Query } = require('./lib/tokenscout-bm25-sparse-retrieval-ranker.cjs');
const { isLlmAvailable, augmentQuery, semanticRankCandidates, buildRepoSummary, buildFileSummaries } = require('./lib/tokenscout-llm.cjs');
const { isEmbeddingAvailable, queryEmbeddings, queryRepoOverviewSimilarity } = require('./lib/tokenscout-gemini-embedding-dense-retrieval.cjs');
const { scoreByGraphProximity } = require('./lib/tokenscout-graph.cjs');

// ─── Hybrid scoring weights (inspired by FastCode: semantic 0.6, keyword 0.3, graph 0.1) ──
// Extended for 4-signal hybrid: embedding + bm25 + keyword + graph
const W_EMBED = 0.35;   // Dense semantic signal (strongest when available)
const W_BM25 = 0.30;    // Sparse retrieval signal
const W_KEYWORD = 0.20;  // Exact match signal
const W_GRAPH = 0.15;    // Structural proximity signal

// ─── Complexity heuristic ─────────────────────────────────────────────────────

function _estimateComplexity(prompt) {
  let score = 10;
  const lower = prompt.toLowerCase();
  const words = lower.split(/\s+/);
  const n = words.length;

  if (n > 50) score += 20;
  else if (n > 20) score += 10;
  else if (n > 10) score += 5;

  const multiHop = [
    'dependency', 'dependencies', 'import', 'imports', 'calls', 'flow',
    'trace', 'chain', 'across files', 'cross-file', 'inheritance',
    'extends', 'implements', 'interaction', 'relationship', 'connected',
  ];
  score += Math.min(30, multiHop.filter(kw => lower.includes(kw)).length * 5);

  const scope = [
    'architecture', 'design', 'system', 'entire', 'all files',
    'repository', 'codebase', 'project-wide', 'overall',
  ];
  score += Math.min(20, scope.filter(kw => lower.includes(kw)).length * 5);

  const depth = [
    'algorithm', 'performance', 'security', 'race condition', 'deadlock',
    'memory leak', 'optimization', 'complexity', 'concurrency', 'thread', 'async',
  ];
  score += Math.min(20, depth.filter(kw => lower.includes(kw)).length * 5);

  return Math.min(100, score);
}

// ─── Keyword candidate search ─────────────────────────────────────────────────

const STOPWORDS = new Set([
  'the','a','an','and','or','but','in','on','at','to','for','of','with','by',
  'from','as','is','was','are','were','be','been','have','has','had','do',
  'does','did','will','would','could','should','may','might','can','this',
  'that','these','those','i','you','he','she','it','we','they','how','what',
  'where','when','why','which','who','me','my','your','code','file','files',
  'function','class','method','explain','show','find','tell','about','please',
]);

function _extractSearchTerms(prompt) {
  return (prompt.toLowerCase().match(/\b[a-z_][a-z0-9_]+\b/g) || [])
    .filter(w => !STOPWORDS.has(w) && w.length > 2);
}

function _findCandidatesKeyword(prompt, repoMap) {
  const candidates = {};
  const terms = _extractSearchTerms(prompt.toLowerCase());
  if (!terms.length) return candidates;

  const files = repoMap.files || {};
  const sep = '/';

  for (const [filePath, info] of Object.entries(files)) {
    let score = 0.0;
    const pathLower = filePath.toLowerCase();

    for (const term of terms) {
      if (pathLower.includes(term)) score += 0.3;
      for (const part of pathLower.split(sep)) {
        if (part.includes(term)) score += 0.2;
      }
    }

    for (const sig of (info.signatures || [])) {
      const sigName = (sig.name || '').toLowerCase();
      const sigStr = (sig.signature || '').toLowerCase();
      for (const term of terms) {
        if (sigName.includes(term)) score += 0.4;
        else if (sigStr.includes(term)) score += 0.2;
      }
    }

    if (score > 0.1) {
      const nSigs = (info.signatures || []).length;
      const density = Math.min(1.0, nSigs / Math.max(1, info.lines || 1) * 20);
      const final = priorityScore(Math.min(1.0, score), false, density);
      candidates[filePath] = {
        score: Math.round(final * 1000) / 1000,
        provenance: 'keyword',
        cost: info.lines || 0,
      };
    }
  }

  const sorted = Object.entries(candidates).sort((a, b) => b[1].score - a[1].score).slice(0, 20);
  return Object.fromEntries(sorted);
}

// ─── Main ─────────────────────────────────────────────────────────────────────

function main() {
  const stdin = fs.readFileSync(0, 'utf-8').trim();
  const hookInput = stdin ? JSON.parse(stdin) : {};
  const prompt = hookInput.prompt || '';
  if (!prompt.trim()) process.exit(0);

  const state = loadState();
  const ctx = state.context_state;
  const repoMap = state.repo_map;

  // Step 1: heuristic complexity
  const DqHeuristic = _estimateComplexity(prompt);

  // Step 2: optional LLM augmentation
  let llmResult = { expanded_terms: [], D_q: null, intent: null, strategy: null };
  if (isLlmAvailable()) {
    const repoSummary = buildRepoSummary(repoMap);
    const kwCandidates = Object.keys(_findCandidatesKeyword(prompt, repoMap)).slice(0, 10);
    llmResult = augmentQuery(prompt, repoSummary, kwCandidates);
  }

  const Dq = llmResult.D_q != null ? llmResult.D_q : DqHeuristic;
  ctx.D_q = Dq;

  // Step 3: reset per-query tracking
  ctx.L_t = 0;
  ctx.t = 0;
  ctx.kappa_t = 0.0;
  ctx.igr_history = [];
  state.explored_files = [];
  state.scouted_files = [];

  // Step 4: compute dynamic budget
  const Hr = ctx.H_r || 1.0;
  const budget = computeBudget(Dq, Hr);
  ctx.budget = budget;

  // Step 5: BM25 ranking
  const bm25 = createBM25Index();
  bm25IndexRepo(bm25, repoMap);
  const expandedTerms = llmResult.expanded_terms || [];
  const searchQuery = expandedTerms.length ? `${prompt} ${expandedTerms.join(' ')}` : prompt;
  const bm25Results = bm25Query(bm25, searchQuery, 20);

  // ─── Step 6: Collect per-signal scores (all normalized 0-1) ────────────────

  // 6a: BM25 scores
  const bm25Scores = {};
  if (bm25Results.length) {
    const maxBm25 = Math.max(...bm25Results.map(([, s]) => s)) || 1.0;
    for (const [fp, score] of bm25Results) {
      bm25Scores[fp] = score / Math.max(0.01, maxBm25);
    }
  }

  // 6b: Keyword scores
  const kwScores = {};
  const kwCandidates = _findCandidatesKeyword(prompt, repoMap);
  for (const [fp, info] of Object.entries(kwCandidates)) {
    kwScores[fp] = info.score;
  }

  // 6c: Embedding scores (Tier 2)
  const embedScores = {};
  let embeddingResultCount = 0;
  if (isEmbeddingAvailable()) {
    const embeddingResults = queryEmbeddings(searchQuery, 30);
    embeddingResultCount = embeddingResults.length;
    for (const [fp, sim] of embeddingResults) {
      embedScores[fp] = sim;
    }
  }

  // 6d: Graph proximity scores — seed from top BM25+keyword+embedding candidates
  const seedPaths = [
    ...Object.keys(bm25Scores).slice(0, 5),
    ...Object.keys(kwScores).slice(0, 3),
    ...Object.keys(embedScores).slice(0, 3),
  ];
  const uniqueSeeds = [...new Set(seedPaths)];
  const graphScores = scoreByGraphProximity(uniqueSeeds, repoMap, 10);

  // ─── Step 7: Unified hybrid scoring formula ──────────────────────────────
  // S_final = w₁·S_embed + w₂·S_bm25 + w₃·S_keyword + w₄·S_graph
  // Weights auto-adjust if embedding unavailable

  const allPaths = new Set([
    ...Object.keys(bm25Scores),
    ...Object.keys(kwScores),
    ...Object.keys(embedScores),
    ...Object.keys(graphScores),
  ]);

  // Adjust weights if embedding not available (redistribute to bm25+keyword)
  const hasEmbed = embeddingResultCount > 0;
  const wEmbed = hasEmbed ? W_EMBED : 0;
  const wBm25 = hasEmbed ? W_BM25 : W_BM25 + W_EMBED * 0.6;
  const wKeyword = hasEmbed ? W_KEYWORD : W_KEYWORD + W_EMBED * 0.4;
  const wGraph = W_GRAPH;

  const candidates = {};
  for (const fp of allPaths) {
    const sEmbed = embedScores[fp] || 0;
    const sBm25 = bm25Scores[fp] || 0;
    const sKeyword = kwScores[fp] || 0;
    const sGraph = graphScores[fp] || 0;

    const hybridScore = wEmbed * sEmbed + wBm25 * sBm25 + wKeyword * sKeyword + wGraph * sGraph;

    // Build provenance string
    const signals = [];
    if (sBm25 > 0) signals.push('bm25');
    if (sKeyword > 0) signals.push('kw');
    if (sEmbed > 0) signals.push('embed');
    if (sGraph > 0) signals.push('graph');

    const fileInfo = (repoMap.files || {})[fp] || {};
    candidates[fp] = {
      score: Math.round(Math.min(1.0, hybridScore) * 1000) / 1000,
      provenance: signals.join('+'),
      cost: fileInfo.lines || 0,
      signals: { embed: sEmbed, bm25: sBm25, keyword: sKeyword, graph: sGraph },
    };
  }

  // ─── Step 7.5: Optional LLM re-ranking (Tier 3 — boost, not replace) ────
  if (isLlmAvailable() && Object.keys(candidates).length > 0) {
    const fileSummaries = buildFileSummaries(repoMap);
    const ranked = semanticRankCandidates(prompt, candidates, fileSummaries);
    for (const [fp, llmScore] of ranked) {
      if (fp in candidates) {
        // LLM re-rank blends with hybrid: 40% LLM + 60% hybrid
        const old = candidates[fp].score;
        candidates[fp].score = Math.round(Math.min(1.0, 0.4 * llmScore + 0.6 * old) * 1000) / 1000;
        candidates[fp].provenance += '+llm';
      }
    }
  }

  // ─── Step 8: Repo overview boost ─────────────────────────────────────────
  // If query is highly relevant to the repo overview, boost all candidates slightly
  let repoOverviewSim = 0;
  if (isEmbeddingAvailable()) {
    repoOverviewSim = queryRepoOverviewSimilarity(searchQuery);
  }

  // Keep top 20
  const sorted = Object.entries(candidates).sort((a, b) => b[1].score - a[1].score).slice(0, 20);
  const finalCandidates = Object.fromEntries(sorted);

  state.candidates = finalCandidates;
  state._last_query = prompt;
  state.context_state = ctx;
  saveState(state);

  // Build additionalContext — MINIMAL to reduce token overhead
  // Skip context for simple queries (D_q < 20) — not worth the token cost
  if (Dq < 20 || Object.keys(finalCandidates).length === 0) {
    // No injection for trivial queries
    console.log(JSON.stringify({ additionalContext: '' }));
  } else {
    // Compact format: just file paths, no verbose metadata
    const topPaths = Object.keys(finalCandidates).slice(0, 5);
    const ctx_line = `[TokenScout] D_q=${Dq} B=${budget} | Read these first: ${topPaths.join(', ')}`;
    console.log(JSON.stringify({ additionalContext: ctx_line }));
  }

  auditLog('query_augmentation', {
    D_q: Dq,
    D_q_heuristic: DqHeuristic,
    D_q_llm: llmResult.D_q,
    budget,
    H_r: Hr,
    llm_available: isLlmAvailable(),
    llm_intent: llmResult.intent,
    expanded_terms: expandedTerms.slice(0, 10),
    num_candidates: Object.keys(finalCandidates).length,
    num_bm25: bm25Results.length,
    num_embedding: embeddingResultCount,
    num_graph: Object.keys(graphScores).length,
    embedding_available: isEmbeddingAvailable(),
    repo_overview_similarity: Math.round(repoOverviewSim * 1000) / 1000,
    hybrid_weights: { embed: wEmbed, bm25: wBm25, keyword: wKeyword, graph: wGraph },
    top_candidates: Object.keys(finalCandidates).slice(0, 5),
  });
}

main();
