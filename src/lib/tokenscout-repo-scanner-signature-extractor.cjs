'use strict';
/**
 * tokenscout-repo-scanner-signature-extractor.cjs
 *
 * Ports tokenscout_common.py lines 116-435:
 *   - Lightweight repo walk: scanRepoLightweight()
 *   - Per-file signature extraction: extractSignatures()
 *   - Language parsers: Python, JS/TS, Java, Go, Rust, generic (all regex-based)
 *   - 3-layer graph builders: inheritance (G_inh), call graph (G_call)
 *   - Repository entropy H_r calculation
 */

const fs = require('fs');
const path = require('path');
const { buildInheritanceGraph, buildCallGraph } = require('./tokenscout-graph.cjs');

// ─── Constants ────────────────────────────────────────────────────────────────

const LANG_MAP = {
  '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
  '.jsx': 'javascript', '.tsx': 'typescript',
  '.java': 'java', '.go': 'go',
  '.rs': 'rust', '.c': 'c', '.cpp': 'cpp', '.h': 'c',
  '.cs': 'csharp', '.rb': 'ruby', '.php': 'php',
  '.swift': 'swift', '.kt': 'kotlin',
};

const IGNORE_DIRS = new Set([
  '.git', 'node_modules', '__pycache__', '.venv', 'venv',
  'dist', 'build', '.next', '.nuxt', 'target', 'vendor',
  '.tox', '.mypy_cache', '.pytest_cache', 'env', '.env',
  'site-packages', 'coverage', '.coverage', '.idea', '.vscode',
]);

const IGNORE_FILES = new Set([
  'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
  'Cargo.lock', 'poetry.lock', 'Pipfile.lock',
]);

// ─── Language parsers (regex-based) ──────────────────────────────────────────

function parsePythonSigs(lines) {
  const sigs = [];
  const imports = [];
  let currentClass = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const stripped = line.trim();

    if (stripped.startsWith('import ') || stripped.startsWith('from ')) {
      const m = stripped.match(/(?:from\s+(\S+)\s+)?import\s+(.+)/);
      if (m) {
        const mod = m[1] || m[2].split(',')[0].trim().split(' as ')[0];
        imports.push(mod);
      }
    } else if (stripped.startsWith('class ')) {
      const m = stripped.match(/class\s+(\w+)(\([^)]*\))?:/);
      if (m) {
        currentClass = m[1];
        const bases = m[2] || '';
        sigs.push({ name: currentClass, type: 'class', line: i + 1, signature: `class ${currentClass}${bases}` });
      }
    } else if (/\s*(?:async\s+)?def\s+/.test(stripped)) {
      const m = stripped.match(/\s*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)/);
      if (m) {
        const fname = m[1];
        const params = m[2].trim();
        const indent = line.length - line.trimStart().length;
        const sigType = indent > 0 && currentClass ? 'method' : 'function';
        const name = sigType === 'method' && currentClass ? `${currentClass}.${fname}` : fname;
        sigs.push({ name, type: sigType, line: i + 1, signature: `def ${fname}(${params})` });
      }
    } else if (currentClass && !stripped.startsWith('#') && stripped && line[0] !== ' ' && line[0] !== '\t') {
      if (!stripped.startsWith('class ') && !stripped.startsWith('def ')) {
        currentClass = null;
      }
    }
  }
  return { sigs, imports };
}

function parseJsTsSigs(lines) {
  const sigs = [];
  const imports = [];

  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();

    if (stripped.startsWith('import ')) {
      const m = stripped.match(/from\s+["']([^"']+)['"]/);
      if (m) imports.push(m[1]);
    }

    let m = stripped.match(/^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)/);
    if (m) {
      sigs.push({ name: m[1], type: 'function', line: i + 1, signature: `function ${m[1]}(${m[2]})` });
      continue;
    }

    m = stripped.match(/^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(/);
    if (m) {
      sigs.push({ name: m[1], type: 'function', line: i + 1, signature: `const ${m[1]} = (...)` });
      continue;
    }

    m = stripped.match(/^(?:export\s+)?class\s+(\w+)(?:\s+extends\s+(\w+))?/);
    if (m) {
      const ext = m[2] ? ` extends ${m[2]}` : '';
      sigs.push({ name: m[1], type: 'class', line: i + 1, signature: `class ${m[1]}${ext}` });
    }
  }
  return { sigs, imports };
}

function parseJavaSigs(lines) {
  const sigs = [];
  const imports = [];

  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();

    if (stripped.startsWith('import ')) {
      const m = stripped.match(/import\s+(?:static\s+)?([^;]+);/);
      if (m) imports.push(m[1]);
    }

    let m = stripped.match(/^(?:public|private|protected)?\s*(?:static\s+)?(?:abstract\s+)?class\s+(\w+)/);
    if (m) {
      sigs.push({ name: m[1], type: 'class', line: i + 1, signature: stripped.replace(/\{$/, '').trim() });
      continue;
    }

    m = stripped.match(/^(?:public|private|protected)\s+(?:static\s+)?(?:\w+(?:<[^>]+>)?)\s+(\w+)\s*\(([^)]*)\)/);
    if (m) {
      sigs.push({ name: m[1], type: 'method', line: i + 1, signature: stripped.replace(/\{$/, '').trim() });
    }
  }
  return { sigs, imports };
}

function parseGoSigs(lines) {
  const sigs = [];
  const imports = [];

  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();

    if (stripped.startsWith('import ')) {
      const m = stripped.match(/"([^"]+)"/);
      if (m) imports.push(m[1]);
    }

    let m = stripped.match(/^type\s+(\w+)\s+(struct|interface)\b/);
    if (m) {
      sigs.push({ name: m[1], type: m[2], line: i + 1, signature: stripped.split('{')[0].trim() });
      continue;
    }

    m = stripped.match(/^func\s+(?:\(\w+\s+\*?(\w+)\)\s+)?(\w+)\s*\(([^)]*)\)/);
    if (m) {
      const receiver = m[1];
      const fname = m[2];
      const name = receiver ? `${receiver}.${fname}` : fname;
      sigs.push({ name, type: receiver ? 'method' : 'function', line: i + 1, signature: stripped.split('{')[0].trim() });
    }
  }
  return { sigs, imports };
}

function parseRustSigs(lines) {
  const sigs = [];
  const imports = [];

  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();

    if (stripped.startsWith('use ')) {
      const m = stripped.match(/use\s+([^;]+);/);
      if (m) imports.push(m[1]);
    }

    let m = stripped.match(/^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[(<]/);
    if (m) {
      sigs.push({ name: m[1], type: 'function', line: i + 1, signature: stripped.split('{')[0].trim() });
      continue;
    }

    m = stripped.match(/^(?:pub\s+)?struct\s+(\w+)/);
    if (m) {
      sigs.push({ name: m[1], type: 'struct', line: i + 1, signature: stripped.split('{')[0].trim() });
    }

    m = stripped.match(/^(?:pub\s+)?enum\s+(\w+)/);
    if (m) {
      sigs.push({ name: m[1], type: 'enum', line: i + 1, signature: stripped.split('{')[0].trim() });
    }
  }
  return { sigs, imports };
}

function parseGenericSigs(lines) {
  const sigs = [];
  const imports = [];

  for (let i = 0; i < lines.length; i++) {
    const stripped = lines[i].trim();

    let m = stripped.match(/^(?:function|def|fn|func|sub|proc)\s+(\w+)/);
    if (m) {
      sigs.push({ name: m[1], type: 'function', line: i + 1, signature: stripped.slice(0, 120) });
    }

    m = stripped.match(/^(?:class|struct|enum|interface|trait|type)\s+(\w+)/);
    if (m) {
      sigs.push({ name: m[1], type: 'class', line: i + 1, signature: stripped.slice(0, 120) });
    }
  }
  return { sigs, imports };
}

// ─── Signature extraction dispatcher ─────────────────────────────────────────

function extractSignatures(fpath, lang) {
  let lines;
  try {
    lines = fs.readFileSync(fpath, 'utf-8').split('\n');
  } catch (_) {
    return { sigs: [], imports: [], lineCount: 0 };
  }

  const lineCount = lines.length;
  let result;

  if (lang === 'python') {
    result = parsePythonSigs(lines);
  } else if (lang === 'javascript' || lang === 'typescript') {
    result = parseJsTsSigs(lines);
  } else if (lang === 'java') {
    result = parseJavaSigs(lines);
  } else if (lang === 'go') {
    result = parseGoSigs(lines);
  } else if (lang === 'rust') {
    result = parseRustSigs(lines);
  } else {
    result = parseGenericSigs(lines);
  }

  return { sigs: result.sigs, imports: result.imports, lineCount };
}

// ─── Main repo scanner ────────────────────────────────────────────────────────

function scanRepoLightweight(root, maxFiles = 5000) {
  const files = {};
  const dependencies = {};
  const symbols = {};
  const langCounts = {};
  let totalLines = 0;
  let maxDepth = 0;
  let count = 0;

  root = path.resolve(root);

  function walk(dir, depth) {
    if (count > maxFiles) return;

    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }

    maxDepth = Math.max(maxDepth, depth);

    for (const entry of entries) {
      if (count > maxFiles) break;

      if (entry.isDirectory()) {
        if (!IGNORE_DIRS.has(entry.name)) {
          walk(path.join(dir, entry.name), depth + 1);
        }
      } else if (entry.isFile()) {
        const fname = entry.name;
        if (IGNORE_FILES.has(fname) || fname.startsWith('.')) continue;

        const ext = path.extname(fname).toLowerCase();
        const lang = LANG_MAP[ext];
        if (!lang) continue;

        count++;
        if (count > maxFiles) break;

        const fpath = path.join(dir, fname);
        const rel = path.relative(root, fpath);

        let size = 0;
        try { size = fs.statSync(fpath).size; } catch (_) {}

        const { sigs, imports, lineCount } = extractSignatures(fpath, lang);
        totalLines += lineCount;
        langCounts[lang] = (langCounts[lang] || 0) + 1;

        files[rel] = {
          lang,
          size,
          lines: lineCount,
          signatures: sigs.slice(0, 50),
        };

        if (imports.length > 0) {
          dependencies[rel] = imports;
        }

        for (const sig of sigs) {
          const symKey = `${rel}::${sig.name}`;
          symbols[symKey] = {
            path: rel,
            line: sig.line || 0,
            type: sig.type || 'unknown',
            signature: sig.signature || '',
          };
        }
      }
    }
  }

  walk(root, 0);

  const nFiles = Object.keys(files).length;
  const nLangs = Object.keys(langCounts).length;
  const entropy = Math.min(2.0, Math.max(0.5,
    0.5
    + 0.3 * Math.min(nFiles / 500, 1.0)
    + 0.2 * Math.min(maxDepth / 10, 1.0)
    + 0.5 * Math.min(nLangs / 4, 1.0)
  ));

  const inheritance = buildInheritanceGraph(files, symbols);
  const callGraph = buildCallGraph(files, symbols);

  return {
    files,
    dependencies,
    inheritance,
    call_graph: callGraph,
    symbols,
    stats: {
      total_files: nFiles,
      total_lines: totalLines,
      languages: langCounts,
      depth: maxDepth,
      entropy: Math.round(entropy * 1000) / 1000,
    },
  };
}

module.exports = {
  LANG_MAP,
  IGNORE_DIRS,
  IGNORE_FILES,
  scanRepoLightweight,
  extractSignatures,
  parsePythonSigs,
  parseJsTsSigs,
  parseJavaSigs,
  parseGoSigs,
  parseRustSigs,
  parseGenericSigs,
};
