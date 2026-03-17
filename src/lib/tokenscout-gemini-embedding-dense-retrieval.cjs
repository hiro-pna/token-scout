'use strict';
/**
 * tokenscout-gemini-embedding-dense-retrieval.cjs
 *
 * Tier 2 dense retrieval using Gemini Embedding 2 (gemini-embedding-2-preview).
 * Complements BM25 sparse retrieval with semantic vector search.
 *
 * Architecture:
 *   - Binary vector store (Float32 buffer) — 3x smaller than JSON, 10x faster I/O
 *   - Pre-normalized vectors at index time → query = fast dot product (no sqrt)
 *   - Flat exhaustive search — optimal for <5000 files (HNSW unnecessary)
 *   - Disk cache with freshness check (file path hash)
 *
 * Flow:
 *   1. Session start: batch-embed all file documents, normalize, cache as binary
 *   2. Query time: embed query, normalize, dot product against all vectors
 *   3. Return top-K dense retrieval candidates
 *
 * Cache: .claude/.tokenscout/embeddings.bin + embeddings.meta.json
 * Auth: GEMINI_API_KEY env var
 *
 * @module tokenscout-gemini-embedding-dense-retrieval
 */

const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const EMBEDDING_MODEL = 'gemini-embedding-2-preview';
const API_BASE = 'generativelanguage.googleapis.com';
const BATCH_SIZE = 250; // Gemini batch limit per request
const EMBED_DIMENSIONS = 768; // Matryoshka: 768/1536/3072 — smallest for cache efficiency
const API_TIMEOUT_MS = 15000;
const MAX_TEXT_LENGTH = 2000; // chars per document text

// ─── API availability ─────────────────────────────────────────────────────────

function getGeminiApiKey() {
  return (process.env.GEMINI_API_KEY || '').trim();
}

function isEmbeddingAvailable() {
  return !!getGeminiApiKey();
}

// ─── Cache paths ──────────────────────────────────────────────────────────────

function _cacheDir() {
  const dir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
  return path.join(dir, '.claude', '.tokenscout');
}

function _vectorBinPath() {
  return path.join(_cacheDir(), 'embeddings.bin');
}

function _metaPath() {
  return path.join(_cacheDir(), 'embeddings.meta.json');
}

// ─── Document text builder ────────────────────────────────────────────────────

/**
 * Build a text representation of a file for embedding.
 * Combines path, language, signature names/texts, and imports.
 * @param {string} filePath
 * @param {Object} fileInfo - from repo_map.files[path]
 * @param {string[]} imports - from repo_map.dependencies[path]
 * @returns {string}
 */
function buildDocumentText(filePath, fileInfo, imports) {
  const parts = [];
  parts.push(`File: ${filePath}`);
  parts.push(`Language: ${fileInfo.lang || 'unknown'}`);

  const sigs = (fileInfo.signatures || []).slice(0, 20);
  if (sigs.length) {
    const sigTexts = sigs.map(s => {
      const sig = s.signature || s.name || '';
      return `${s.type || 'symbol'}: ${sig}`;
    });
    parts.push(`Symbols: ${sigTexts.join('; ')}`);
  }

  const imps = (imports || []).slice(0, 10);
  if (imps.length) {
    parts.push(`Imports: ${imps.join(', ')}`);
  }

  return parts.join('\n').slice(0, MAX_TEXT_LENGTH);
}

// ─── Gemini API call (sync via child process) ─────────────────────────────────

/**
 * Batch embed texts via Gemini API. Synchronous (spawnSync).
 * @param {string[]} texts - Array of text strings to embed
 * @returns {number[][]|null} Array of embedding vectors, or null on failure
 */
function _batchEmbed(texts) {
  const apiKey = getGeminiApiKey();
  if (!apiKey || !texts.length) return null;

  const requests = texts.map(text => ({
    model: `models/${EMBEDDING_MODEL}`,
    content: { parts: [{ text }] },
    outputDimensionality: EMBED_DIMENSIONS,
  }));

  const payload = JSON.stringify({ requests });

  // Sync HTTP call via child process (header-based auth)
  const script = `
const https = require('https');
const payload = ${JSON.stringify(payload)};
const options = {
  hostname: '${API_BASE}',
  path: '/v1beta/models/${EMBEDDING_MODEL}:batchEmbedContents',
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'x-goog-api-key': ${JSON.stringify(apiKey)},
    'Content-Length': Buffer.byteLength(payload),
  },
};
const req = https.request(options, res => {
  const chunks = [];
  res.on('data', c => chunks.push(c));
  res.on('end', () => process.stdout.write(Buffer.concat(chunks).toString()));
});
req.setTimeout(${API_TIMEOUT_MS}, () => { req.destroy(); });
req.on('error', () => {});
req.write(payload);
req.end();`;

  try {
    const result = spawnSync(process.execPath, ['-e', script], {
      timeout: API_TIMEOUT_MS + 3000,
      encoding: 'utf-8',
      maxBuffer: 50 * 1024 * 1024,
    });

    if (!result.stdout || !result.stdout.trim()) return null;

    const data = JSON.parse(result.stdout.trim());
    if (data.error) {
      _logError('batch_embed_api_error', data.error.message || JSON.stringify(data.error));
      return null;
    }

    const embeddings = (data.embeddings || []).map(e => e.values || []);
    if (embeddings.length !== texts.length) return null;
    return embeddings;
  } catch (e) {
    _logError('batch_embed_failed', String(e).slice(0, 200));
    return null;
  }
}

/**
 * Embed a single text (for query embedding).
 * @param {string} text
 * @returns {number[]|null}
 */
function embedQuery(text) {
  const result = _batchEmbed([text]);
  return result ? result[0] : null;
}

// ─── Vector math ──────────────────────────────────────────────────────────────

/**
 * L2-normalize a vector in place and return it.
 * @param {Float32Array|number[]} vec
 * @returns {Float32Array}
 */
function _normalize(vec) {
  let norm = 0;
  for (let i = 0; i < vec.length; i++) norm += vec[i] * vec[i];
  norm = Math.sqrt(norm);
  if (norm > 0) {
    for (let i = 0; i < vec.length; i++) vec[i] /= norm;
  }
  return vec;
}

/**
 * Cosine similarity between two vectors.
 * For pre-normalized vectors this equals the dot product.
 * @param {Float32Array|number[]} a
 * @param {Float32Array|number[]} b
 * @returns {number}
 */
function cosineSimilarity(a, b) {
  if (!a || !b || a.length !== b.length || a.length === 0) return 0;
  let dot = 0, normA = 0, normB = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    normA += a[i] * a[i];
    normB += b[i] * b[i];
  }
  const denom = Math.sqrt(normA) * Math.sqrt(normB);
  return denom > 0 ? dot / denom : 0;
}

/**
 * Dot product — used for pre-normalized vectors (faster than cosine).
 * @param {Float32Array} a
 * @param {Float32Array} b
 * @returns {number}
 */
function _dotProduct(a, b) {
  let sum = 0;
  for (let i = 0; i < a.length; i++) sum += a[i] * b[i];
  return sum;
}

// ─── Binary vector store ──────────────────────────────────────────────────────
// Layout: flat Float32 buffer, N vectors × D dimensions
// File: embeddings.bin  = raw Float32Array bytes
// File: embeddings.meta.json = {model, cacheKey, timestamp, dimension, paths:[]}

/**
 * Save vectors to binary format.
 * Vectors are L2-normalized before saving for fast dot-product search.
 * @param {Object<string, number[]>} vectors - path -> embedding array
 * @param {string} cacheKey
 */
function _saveVectorStore(vectors, cacheKey) {
  const paths = Object.keys(vectors);
  const n = paths.length;
  const dim = EMBED_DIMENSIONS;

  // Pack into Float32 buffer (pre-normalized)
  const buf = Buffer.alloc(n * dim * 4);
  const f32 = new Float32Array(buf.buffer, buf.byteOffset, n * dim);

  for (let i = 0; i < n; i++) {
    const vec = vectors[paths[i]];
    const offset = i * dim;
    // Copy and normalize
    let norm = 0;
    for (let j = 0; j < dim; j++) {
      const v = vec[j] || 0;
      f32[offset + j] = v;
      norm += v * v;
    }
    norm = Math.sqrt(norm);
    if (norm > 0) {
      for (let j = 0; j < dim; j++) f32[offset + j] /= norm;
    }
  }

  const meta = {
    model: EMBEDDING_MODEL,
    cacheKey,
    timestamp: Date.now(),
    dimension: dim,
    count: n,
    paths,
  };

  try {
    fs.mkdirSync(_cacheDir(), { recursive: true });
    fs.writeFileSync(_vectorBinPath(), buf);
    fs.writeFileSync(_metaPath(), JSON.stringify(meta), 'utf-8');
  } catch (e) {
    _logError('save_vector_store_failed', String(e).slice(0, 200));
  }
}

/**
 * Load vector store from binary cache.
 * @returns {{meta: Object, vectors: Float32Array}|null}
 */
function _loadVectorStore() {
  try {
    const metaFile = _metaPath();
    const binFile = _vectorBinPath();
    if (!fs.existsSync(metaFile) || !fs.existsSync(binFile)) return null;

    const meta = JSON.parse(fs.readFileSync(metaFile, 'utf-8'));
    const buf = fs.readFileSync(binFile);
    const vectors = new Float32Array(buf.buffer, buf.byteOffset, meta.count * meta.dimension);

    return { meta, vectors };
  } catch (_) {
    return null;
  }
}

// ─── Embedding index (build + query) ──────────────────────────────────────────

/**
 * Build embedding index for all files in repo_map.
 * Batch-embeds documents, normalizes, and caches as binary.
 * @param {Object} repoMap - full repo_map object
 * @returns {{indexed: number, cached: boolean}} stats
 */
function buildEmbeddingIndex(repoMap) {
  if (!isEmbeddingAvailable()) return { indexed: 0, cached: false };

  const files = repoMap.files || {};
  const deps = repoMap.dependencies || {};
  const filePaths = Object.keys(files);

  if (!filePaths.length) return { indexed: 0, cached: false };

  // Check cache freshness
  const cacheKey = _computeCacheKey(filePaths);
  const store = _loadVectorStore();

  if (store && store.meta.cacheKey === cacheKey && store.meta.model === EMBEDDING_MODEL) {
    return { indexed: store.meta.count, cached: true };
  }

  // Also clean up legacy JSON cache
  _cleanupLegacyCache();

  // Build document texts
  const docTexts = [];
  const docPaths = [];

  for (const fp of filePaths) {
    const text = buildDocumentText(fp, files[fp], deps[fp] || []);
    if (text.length > 10) {
      docTexts.push(text);
      docPaths.push(fp);
    }
  }

  if (!docTexts.length) return { indexed: 0, cached: false };

  // Batch embed in chunks
  const vectors = {};
  let success = true;

  for (let i = 0; i < docTexts.length; i += BATCH_SIZE) {
    const batchTexts = docTexts.slice(i, i + BATCH_SIZE);
    const batchPaths = docPaths.slice(i, i + BATCH_SIZE);
    const embeddings = _batchEmbed(batchTexts);

    if (!embeddings) {
      success = false;
      break;
    }

    for (let j = 0; j < batchPaths.length; j++) {
      vectors[batchPaths[j]] = embeddings[j];
    }
  }

  if (!success || !Object.keys(vectors).length) {
    return { indexed: 0, cached: false };
  }

  // Save as binary (pre-normalized)
  _saveVectorStore(vectors, cacheKey);

  return { indexed: Object.keys(vectors).length, cached: false };
}

/**
 * Query the embedding index with a text query.
 * Uses dot product on pre-normalized vectors (equivalent to cosine similarity).
 * @param {string} queryText - search query
 * @param {number} [topK=20] - max results
 * @returns {Array<[string, number]>} [[path, score], ...] descending by score
 */
function queryEmbeddings(queryText, topK = 20) {
  if (!isEmbeddingAvailable()) return [];

  // Load binary vector store
  const store = _loadVectorStore();
  if (!store || !store.meta.count) return [];

  const { meta, vectors } = store;
  const dim = meta.dimension;
  const n = meta.count;
  const paths = meta.paths;

  // Embed and normalize query
  const rawQuery = embedQuery(queryText);
  if (!rawQuery) return [];

  const queryVec = new Float32Array(rawQuery);
  _normalize(queryVec);

  // Flat exhaustive dot-product search (fast for <5000 vectors)
  const scores = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let dot = 0;
    const offset = i * dim;
    for (let j = 0; j < dim; j++) {
      dot += queryVec[j] * vectors[offset + j];
    }
    scores[i] = dot;
  }

  // Find top-K via partial sort (selection)
  const indices = Array.from({ length: n }, (_, i) => i);
  const k = Math.min(topK, n);

  // Partial sort: find top-k elements
  for (let i = 0; i < k; i++) {
    let maxIdx = i;
    for (let j = i + 1; j < n; j++) {
      if (scores[indices[j]] > scores[indices[maxIdx]]) maxIdx = j;
    }
    if (maxIdx !== i) {
      const tmp = indices[i];
      indices[i] = indices[maxIdx];
      indices[maxIdx] = tmp;
    }
  }

  const results = [];
  for (let i = 0; i < k; i++) {
    const idx = indices[i];
    const score = scores[idx];
    if (score > 0.0) {
      results.push([paths[idx], score]);
    }
  }

  return results;
}

// ─── Repo overview embedding ──────────────────────────────────────────────────

/**
 * Build a repo overview text from repo_map stats + README (if found).
 * Used to create a single "repo-level" embedding for query priming.
 * @param {Object} repoMap
 * @param {string} projectDir
 * @returns {string}
 */
function buildRepoOverviewText(repoMap, projectDir) {
  const stats = repoMap.stats || {};
  const files = repoMap.files || {};
  const symbols = repoMap.symbols || {};
  const parts = [];

  // Project name
  const baseName = (projectDir || '').split('/').pop() || 'unknown';
  parts.push(`Project: ${baseName}`);

  // Languages and stats
  const langs = Object.entries(stats.languages || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${k}(${v})`)
    .join(', ');
  parts.push(`Languages: ${langs}`);
  parts.push(`Files: ${stats.total_files || 0}, Lines: ${stats.total_lines || 0}`);

  // Top directories
  const dirCounts = {};
  for (const p of Object.keys(files)) {
    const d = p.split('/')[0] || 'root';
    dirCounts[d] = (dirCounts[d] || 0) + 1;
  }
  const topDirs = Object.entries(dirCounts).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (topDirs.length) parts.push(`Directories: ${topDirs.map(([d, n]) => `${d}(${n})`).join(', ')}`);

  // Key classes/types
  const classes = [];
  for (const [key, info] of Object.entries(symbols).slice(0, 500)) {
    if (['class', 'struct', 'interface', 'enum'].includes(info.type)) {
      const name = key.includes('::') ? key.split('::').pop() : key;
      if (!classes.includes(name)) classes.push(name);
    }
  }
  if (classes.length) parts.push(`Key types: ${classes.slice(0, 20).join(', ')}`);

  // Key entry points
  const entryPatterns = ['main', 'index', 'app', 'server', 'cli', 'run', 'start'];
  const entries = Object.keys(files).filter(f => {
    const base = f.split('/').pop().split('.')[0].toLowerCase();
    return entryPatterns.includes(base);
  }).slice(0, 10);
  if (entries.length) parts.push(`Entry points: ${entries.join(', ')}`);

  // Try to read README (first 2000 chars)
  if (projectDir) {
    for (const name of ['README.md', 'readme.md', 'README.rst', 'README.txt', 'README']) {
      try {
        const readmePath = require('path').join(projectDir, name);
        if (fs.existsSync(readmePath)) {
          const content = fs.readFileSync(readmePath, 'utf-8').slice(0, 2000);
          parts.push(`README:\n${content}`);
          break;
        }
      } catch (_) {}
    }
  }

  return parts.join('\n').slice(0, MAX_TEXT_LENGTH * 2); // allow 4000 chars for overview
}

function _overviewCachePath() {
  return path.join(_cacheDir(), 'repo-overview-embedding.json');
}

/**
 * Build and cache the repo overview embedding.
 * @param {Object} repoMap
 * @param {string} projectDir
 * @returns {{embedded: boolean}}
 */
function buildRepoOverviewEmbedding(repoMap, projectDir) {
  if (!isEmbeddingAvailable()) return { embedded: false };

  const text = buildRepoOverviewText(repoMap, projectDir);
  if (text.length < 20) return { embedded: false };

  const vec = embedQuery(text);
  if (!vec) return { embedded: false };

  // Normalize
  const f32 = new Float32Array(vec);
  _normalize(f32);

  try {
    fs.mkdirSync(_cacheDir(), { recursive: true });
    fs.writeFileSync(_overviewCachePath(), JSON.stringify({
      model: EMBEDDING_MODEL,
      timestamp: Date.now(),
      vector: Array.from(f32),
      textPreview: text.slice(0, 200),
    }), 'utf-8');
  } catch (_) {}

  return { embedded: true };
}

/**
 * Compute similarity between a query and the repo overview.
 * Returns a single score (0-1) indicating how relevant the query is to the repo.
 * @param {string} queryText
 * @returns {number} similarity score, 0 if unavailable
 */
function queryRepoOverviewSimilarity(queryText) {
  if (!isEmbeddingAvailable()) return 0;

  try {
    const cachePath = _overviewCachePath();
    if (!fs.existsSync(cachePath)) return 0;
    const cache = JSON.parse(fs.readFileSync(cachePath, 'utf-8'));
    const overviewVec = new Float32Array(cache.vector);

    const rawQuery = embedQuery(queryText);
    if (!rawQuery) return 0;

    const queryVec = new Float32Array(rawQuery);
    _normalize(queryVec);

    return _dotProduct(queryVec, overviewVec);
  } catch (_) {
    return 0;
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function _computeCacheKey(filePaths) {
  const str = filePaths.sort().join('|');
  let hash = 5381;
  for (let i = 0; i < Math.min(str.length, 10000); i++) {
    hash = ((hash << 5) + hash + str.charCodeAt(i)) & 0x7fffffff;
  }
  return `${filePaths.length}-${hash}`;
}

function _cleanupLegacyCache() {
  try {
    const legacy = path.join(_cacheDir(), 'embeddings.json');
    if (fs.existsSync(legacy)) fs.unlinkSync(legacy);
  } catch (_) {}
}

function _logError(event, message) {
  try {
    const p = path.join(_cacheDir(), 'audit.jsonl');
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.appendFileSync(p, JSON.stringify({ ts: Date.now() / 1000, event, error: message }) + '\n');
  } catch (_) {}
}

// ─── Exports ──────────────────────────────────────────────────────────────────

module.exports = {
  isEmbeddingAvailable,
  buildEmbeddingIndex,
  queryEmbeddings,
  embedQuery,
  cosineSimilarity,
  buildDocumentText,
  buildRepoOverviewEmbedding,
  queryRepoOverviewSimilarity,
  EMBED_DIMENSIONS,
};
