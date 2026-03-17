#!/usr/bin/env node
/**
 * tokenscout-graph.cjs - Graph builders for TokenScout (G_dep, G_inh, G_call)
 *
 * Ported from tokenscout_common.py lines 438-679.
 * Builds inheritance, call, and dependency graphs for symbol-aware relation modeling.
 *
 * @module tokenscout-graph
 */

'use strict';

const path = require('path');

// ─── Internal helpers ────────────────────────────────────────────────────────

/**
 * Extract base classes/interfaces from a class signature string.
 * @param {string} signature
 * @param {string} lang
 * @returns {string[]}
 */
function _extractBases(signature, lang) {
  const bases = [];

  if (lang === 'python') {
    const m = signature.match(/class\s+\w+\(([^)]+)\)/);
    if (m) {
      for (let b of m[1].split(',')) {
        b = b.trim().split('[')[0].split('(')[0];
        if (b && !['object', 'ABC'].includes(b) && !b.startsWith('metaclass')) {
          bases.push(b);
        }
      }
    }
  } else if (lang === 'javascript' || lang === 'typescript') {
    const mExt = signature.match(/extends\s+(\w+)/);
    if (mExt) bases.push(mExt[1]);
    const mImpl = signature.match(/implements\s+([\w,\s]+)/);
    if (mImpl) {
      for (const b of mImpl[1].split(',')) {
        const t = b.trim();
        if (t) bases.push(t);
      }
    }
  } else if (lang === 'java') {
    const mExt = signature.match(/extends\s+(\w+)/);
    if (mExt) bases.push(mExt[1]);
    const mImpl = signature.match(/implements\s+([\w,\s]+)/);
    if (mImpl) {
      for (const b of mImpl[1].split(',')) {
        const t = b.trim();
        if (t) bases.push(t);
      }
    }
  } else if (lang === 'rust') {
    const m = signature.match(/:\s*([\w\s+<>]+)/);
    if (m) {
      for (let b of m[1].split('+')) {
        b = b.trim().split('<')[0];
        if (b) bases.push(b);
      }
    }
  }

  return bases;
}

// ─── Graph builders ──────────────────────────────────────────────────────────

/**
 * Build inheritance graph G_inh.
 * Maps class hierarchy — base classes and subclasses.
 * @param {Object} files - repo_map.files
 * @param {Object} symbols - repo_map.symbols
 * @returns {Object} graph: className -> {path, bases, subclasses, line}
 */
function buildInheritanceGraph(files, symbols) {
  const graph = {};

  // Pass 1: collect all classes and their declared bases
  for (const [filePath, info] of Object.entries(files)) {
    for (const sig of (info.signatures || [])) {
      if (!['class', 'struct', 'interface'].includes(sig.type)) continue;
      const name = sig.name;
      const sigStr = sig.signature || '';
      const bases = _extractBases(sigStr, info.lang || '');

      if (!(name in graph)) {
        graph[name] = { path: filePath, bases: [], subclasses: [], line: sig.line || 0 };
      }
      graph[name].bases = bases;
      graph[name].path = filePath;
    }
  }

  // Pass 2: fill reverse edges (subclasses)
  for (const [cls, info] of Object.entries(graph)) {
    for (const base of info.bases) {
      if (base in graph && !graph[base].subclasses.includes(cls)) {
        graph[base].subclasses.push(cls);
      }
    }
  }

  return graph;
}

/**
 * Build call graph G_call.
 * Maps functions to called symbols via regex on signatures.
 * @param {Object} files - repo_map.files
 * @param {Object} symbols - repo_map.symbols
 * @returns {Object} graph: "path::funcName" -> ["path::callee", ...]
 */
function buildCallGraph(files, symbols) {
  const graph = {};
  const SKIP_KEYWORDS = new Set([
    'def', 'async', 'return', 'if', 'for', 'while', 'print',
    'len', 'str', 'int', 'float', 'bool', 'list', 'dict',
    'set', 'tuple', 'range', 'type', 'super', 'self'
  ]);

  // Build symbol lookup: name -> [paths]
  const symbolLookup = {};
  for (const [symKey, info] of Object.entries(symbols)) {
    let name = symKey.includes('::') ? symKey.split('::').pop() : symKey;
    const baseName = name.includes('.') ? name.split('.').pop() : name;
    if (!(baseName in symbolLookup)) symbolLookup[baseName] = [];
    symbolLookup[baseName].push(info.path);
  }

  // For each function/method, find likely callees
  for (const [filePath, info] of Object.entries(files)) {
    for (const sig of (info.signatures || [])) {
      if (!['function', 'method'].includes(sig.type)) continue;

      const callerKey = `${filePath}::${sig.name}`;
      const callees = new Set();
      const sigStr = sig.signature || '';

      // Extract function calls from signature text
      const callsInSig = [...sigStr.matchAll(/(\w+)\s*\(/g)].map(m => m[1]);
      for (const call of callsInSig) {
        if (SKIP_KEYWORDS.has(call)) continue;
        if (call in symbolLookup) {
          for (const calleePath of symbolLookup[call]) {
            if (calleePath !== filePath) {
              callees.add(`${calleePath}::${call}`);
            }
          }
        }
      }

      // For methods: check if same method exists in other classes
      if (sig.type === 'method' && sig.name.includes('.')) {
        const methodName = sig.name.split('.').pop();
        if (methodName in symbolLookup) {
          for (const calleePath of symbolLookup[methodName]) {
            if (calleePath !== filePath) {
              callees.add(`${calleePath}::${methodName}`);
            }
          }
        }
      }

      if (callees.size > 0) {
        graph[callerKey] = [...callees].sort().slice(0, 10);
      }
    }
  }

  return graph;
}

/**
 * Find files related to targetPath via all 3 graph layers.
 * @param {string} targetPath
 * @param {Object} repoMap - full repo_map object
 * @param {number} [maxHops=2]
 * @returns {Object} {path: {relation, distance, via}} sorted by priority, capped at 15
 */
function findRelatedViaGraphs(targetPath, repoMap, maxHops = 2) {
  const related = {};
  const deps = repoMap.dependencies || {};
  const inheritance = repoMap.inheritance || {};
  const callGraph = repoMap.call_graph || {};
  const files = repoMap.files || {};

  // ── G_dep: dependency layer ──
  // Forward: files that target imports
  for (const dep of (deps[targetPath] || [])) {
    for (const [filePath, info] of Object.entries(files)) {
      if (filePath === targetPath) continue;
      const sigNames = (info.signatures || []).map(s => s.name || '');
      if (filePath.includes(dep) || sigNames.some(n => n.includes(dep))) {
        if (!(filePath in related)) {
          related[filePath] = { relation: 'imports', distance: 1, via: dep };
        }
      }
    }
  }

  // Reverse: files that import target
  const targetExt = path.extname(targetPath);
  const targetStem = targetPath.replace(targetExt, '').replace(/[/\\]/g, '.');
  const targetName = path.basename(targetPath, targetExt);

  for (const [filePath, pathDeps] of Object.entries(deps)) {
    if (filePath === targetPath) continue;
    for (const d of pathDeps) {
      if (targetName === d || targetStem.endsWith(d) || d.endsWith(targetName)) {
        if (!(filePath in related)) {
          related[filePath] = { relation: 'imported_by', distance: 1, via: d };
        }
        break;
      }
    }
  }

  // ── G_inh: inheritance layer ──
  const targetClasses = new Set();
  for (const sig of (files[targetPath] || {}).signatures || []) {
    if (['class', 'struct', 'interface'].includes(sig.type)) {
      targetClasses.add(sig.name);
    }
  }

  for (const clsName of targetClasses) {
    const clsInfo = inheritance[clsName] || {};
    for (const base of (clsInfo.bases || [])) {
      const basePath = (inheritance[base] || {}).path || '';
      if (basePath && basePath !== targetPath && !(basePath in related)) {
        related[basePath] = { relation: 'superclass', distance: 1, via: `${clsName} extends ${base}` };
      }
    }
    for (const sub of (clsInfo.subclasses || [])) {
      const subPath = (inheritance[sub] || {}).path || '';
      if (subPath && subPath !== targetPath && !(subPath in related)) {
        related[subPath] = { relation: 'subclass', distance: 1, via: `${sub} extends ${clsName}` };
      }
    }
  }

  // ── G_call: call graph layer ──
  // Functions in target that call things in other files
  for (const [symKey, callees] of Object.entries(callGraph)) {
    const callerPath = symKey.includes('::') ? symKey.split('::')[0] : '';
    if (callerPath !== targetPath) continue;
    for (const calleeKey of callees) {
      const calleePath = calleeKey.includes('::') ? calleeKey.split('::')[0] : '';
      if (calleePath && calleePath !== targetPath && !(calleePath in related)) {
        related[calleePath] = { relation: 'calls', distance: 1, via: calleeKey.split('::').pop() };
      }
    }
  }

  // Reverse: functions in other files that call things in target
  const targetFuncs = new Set();
  for (const sig of (files[targetPath] || {}).signatures || []) {
    if (['function', 'method'].includes(sig.type)) {
      const n = sig.name || '';
      targetFuncs.add(n.includes('.') ? n.split('.').pop() : n);
    }
  }

  for (const [symKey, callees] of Object.entries(callGraph)) {
    const callerPath = symKey.includes('::') ? symKey.split('::')[0] : '';
    if (callerPath === targetPath) continue;
    for (const calleeKey of callees) {
      const calleeName = calleeKey.includes('::') ? calleeKey.split('::').pop() : calleeKey;
      const calleeBase = calleeName.includes('.') ? calleeName.split('.').pop() : calleeName;
      const calleePath = calleeKey.includes('::') ? calleeKey.split('::')[0] : '';
      if (
        calleePath === targetPath ||
        targetFuncs.has(calleeBase) ||
        targetFuncs.has(calleeName)
      ) {
        if (!(callerPath in related) || ['imports', 'imported_by'].includes(related[callerPath].relation)) {
          related[callerPath] = { relation: 'called_by', distance: 1, via: calleeName };
        }
      }
    }
  }

  // Sort by relevance: inheritance > calls > imports, cap at 15
  const priority = { superclass: 0, subclass: 0, calls: 1, called_by: 1, imports: 2, imported_by: 2 };
  return Object.fromEntries(
    Object.entries(related)
      .sort((a, b) => (priority[a[1].relation] ?? 3) - (priority[b[1].relation] ?? 3))
      .slice(0, 15)
  );
}

// ─── Graph-as-retrieval-signal ────────────────────────────────────────────────

// Relation weights: inheritance > calls > imports (from FastCode: 0.6/0.3/0.1 paradigm)
const RELATION_WEIGHT = {
  superclass: 1.0, subclass: 0.9,
  calls: 0.7, called_by: 0.7,
  imports: 0.4, imported_by: 0.3,
};

/**
 * Score ALL repo files by graph proximity to a set of seed candidates.
 * Traverses G_dep + G_inh + G_call from each seed, accumulates weighted scores.
 * Returns normalized scores (0-1) for files reachable within graph distance.
 *
 * @param {string[]} seedPaths - Initial candidate file paths (from BM25/keyword/embedding)
 * @param {Object} repoMap - Full repo_map object
 * @param {number} [maxSeeds=10] - Limit seeds to avoid O(N²)
 * @returns {Object<string, number>} filePath -> graph proximity score (0-1)
 */
function scoreByGraphProximity(seedPaths, repoMap, maxSeeds = 10) {
  const scores = {};
  const seeds = seedPaths.slice(0, maxSeeds);

  for (const seed of seeds) {
    const related = findRelatedViaGraphs(seed, repoMap, 2);
    for (const [relPath, info] of Object.entries(related)) {
      if (seeds.includes(relPath)) continue; // don't score seeds themselves
      const weight = RELATION_WEIGHT[info.relation] || 0.2;
      const distDecay = 1.0 / (info.distance || 1);
      const contribution = weight * distDecay;
      scores[relPath] = (scores[relPath] || 0) + contribution;
    }
  }

  // Normalize to 0-1
  const maxScore = Math.max(...Object.values(scores), 0.001);
  for (const p of Object.keys(scores)) {
    scores[p] = Math.round((scores[p] / maxScore) * 1000) / 1000;
  }

  return scores;
}

// ─── Exports ─────────────────────────────────────────────────────────────────

module.exports = {
  buildInheritanceGraph,
  buildCallGraph,
  findRelatedViaGraphs,
  scoreByGraphProximity,
  _extractBases
};
