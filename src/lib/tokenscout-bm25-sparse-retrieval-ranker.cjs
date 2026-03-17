#!/usr/bin/env node
/**
 * tokenscout-bm25-sparse-retrieval-ranker.cjs - BM25 index and query for code file ranking
 *
 * Ported from tokenscout_common.py lines 844-992 (BM25Index class).
 * Implements Okapi BM25 sparse retrieval over repo_map files.
 * Indexes path segments, signature names, and import names to score
 * relevance against a search query.
 *
 * Formula: score(D,Q) = Σ IDF(q) · tf(q,D)·(k1+1) / (tf(q,D) + k1·(1 - b + b·|D|/avgdl))
 * Parameters: k1=1.5 (TF saturation), b=0.75 (length normalization)
 *
 * @module tokenscout-bm25-sparse-retrieval-ranker
 */

'use strict';

// ─── Tokenization helpers ─────────────────────────────────────────────────────

/**
 * Split camelCase/PascalCase into lowercase subwords.
 * e.g. "buildCallGraph" → ["build", "call", "graph"]
 * @param {string} word
 * @returns {string[]}
 */
function _splitCamel(word) {
  const parts = word.match(/[a-z]+|[A-Z][a-z]*/g) || [];
  return parts.map(p => p.toLowerCase()).filter(p => p.length > 1);
}

/**
 * Tokenize a file entry into a bag of terms for BM25 indexing.
 * Combines path segments, signature names, signature keywords, import names, and lang.
 * @param {string} filePath
 * @param {Object} info - file info object from repo_map.files
 * @param {string[]} imports - dependency list for this file
 * @returns {string[]}
 */
function _tokenizeFile(filePath, info, imports) {
  const terms = [];

  // Path segments (split on /, \, ., _, -)
  for (const part of filePath.toLowerCase().split(/[/\\._\-]/)) {
    if (part && part.length > 1) {
      terms.push(part);
      terms.push(..._splitCamel(part));
    }
  }

  // Signature names and keywords
  for (const sig of (info.signatures || [])) {
    const name = (sig.name || '').toLowerCase();
    for (const part of name.split(/[._]/)) {
      if (part && part.length > 1) {
        terms.push(part);
        terms.push(..._splitCamel(part));
      }
    }
    // Keywords from signature text
    const sigStr = (sig.signature || '').toLowerCase();
    for (const word of (sigStr.match(/[a-z_][a-z0-9_]+/g) || [])) {
      if (word.length > 2) terms.push(word);
    }
  }

  // Import names
  for (const imp of imports) {
    for (const part of imp.toLowerCase().split(/[./\\]/)) {
      if (part && part.length > 1) terms.push(part);
    }
  }

  // Language tag
  const lang = info.lang || '';
  if (lang) terms.push(lang);

  return terms;
}

/**
 * Tokenize a search query into terms.
 * @param {string} queryText
 * @returns {string[]}
 */
function _tokenizeQuery(queryText) {
  const terms = [];
  for (const word of (queryText.toLowerCase().match(/[a-z_][a-z0-9_]+/g) || [])) {
    if (word.length > 1) {
      terms.push(word);
      terms.push(..._splitCamel(word));
    }
  }
  return terms;
}

// ─── BM25 index factory ───────────────────────────────────────────────────────

/**
 * Create a new BM25 index object.
 * @param {number} [k1=1.5] - Term frequency saturation parameter
 * @param {number} [b=0.75] - Length normalization parameter
 * @returns {Object} index object for use with indexRepo / query
 */
function createBM25Index(k1 = 1.5, b = 0.75) {
  return {
    k1,
    b,
    docCount: 0,
    avgDl: 0.0,
    docLens: {},   // path -> term count
    docTf: {},     // path -> {term: count}
    df: {},        // term -> num docs containing it
    indexed: false
  };
}

/**
 * Build BM25 index from a repo_map.
 * Each document is a file represented by path + signatures + imports.
 * Mutates index in place.
 * @param {Object} index - BM25 index from createBM25Index()
 * @param {Object} repoMap - full repo_map object
 */
function indexRepo(index, repoMap) {
  const files = repoMap.files || {};
  const deps = repoMap.dependencies || {};

  index.docCount = 0;
  index.avgDl = 0.0;
  index.docLens = {};
  index.docTf = {};
  index.df = {};
  index.indexed = false;

  let totalLen = 0;

  for (const [filePath, info] of Object.entries(files)) {
    const terms = _tokenizeFile(filePath, info, deps[filePath] || []);
    if (!terms.length) continue;

    index.docCount += 1;
    const tf = {};
    for (const t of terms) {
      tf[t] = (tf[t] || 0) + 1;
    }

    index.docTf[filePath] = tf;
    index.docLens[filePath] = terms.length;
    totalLen += terms.length;

    for (const t of Object.keys(tf)) {
      index.df[t] = (index.df[t] || 0) + 1;
    }
  }

  index.avgDl = totalLen / Math.max(1, index.docCount);
  index.indexed = true;
}

/**
 * Score all indexed documents against a query, return top-k results.
 * @param {Object} index - BM25 index built with indexRepo()
 * @param {string} queryText
 * @param {number} [topK=20]
 * @returns {Array<[string, number]>} [[path, score], ...] descending by score
 */
function query(index, queryText, topK = 20) {
  if (!index.indexed || index.docCount === 0) return [];

  const queryTerms = _tokenizeQuery(queryText);
  if (!queryTerms.length) return [];

  const scores = {};
  const { k1, b, avgDl, docCount, df, docTf, docLens } = index;

  for (const term of queryTerms) {
    if (!(term in df)) continue;

    // IDF = ln((N - df + 0.5) / (df + 0.5) + 1)
    const idf = Math.log((docCount - df[term] + 0.5) / (df[term] + 0.5) + 1.0);

    for (const [filePath, tfMap] of Object.entries(docTf)) {
      if (!(term in tfMap)) continue;
      const tf = tfMap[term];
      const dl = docLens[filePath];
      const numerator = tf * (k1 + 1);
      const denominator = tf + k1 * (1 - b + b * dl / avgDl);
      scores[filePath] = (scores[filePath] || 0.0) + idf * numerator / denominator;
    }
  }

  return Object.entries(scores)
    .sort((a, b) => b[1] - a[1])
    .slice(0, topK);
}

// ─── Exports ─────────────────────────────────────────────────────────────────

module.exports = {
  createBM25Index,
  indexRepo,
  query,
  _tokenizeFile,
  _tokenizeQuery,
  _splitCamel
};
