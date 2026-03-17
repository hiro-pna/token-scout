<div align="center">

# TokenScout

### Scout first. Read less. Save tokens.

[![Node.js](https://img.shields.io/badge/Node.js-CJS-green.svg)](https://nodejs.org)
[![Claude Code](https://img.shields.io/badge/Claude_Code-Hooks_API-blueviolet.svg)](https://docs.anthropic.com/en/docs/claude-code/hooks)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Zero Dependencies](https://img.shields.io/badge/Core-Zero%20Deps-orange.svg)]()

**Stop burning tokens on blind file reads.** TokenScout is a drop-in hook system for Claude Code that builds a structural map of your repo and uses 4-tier hybrid retrieval to guide Claude to the right files first.

[Quick Start](#-quick-start) · [How It Works](#-how-it-works) · [Tiered Architecture](#-tiered-retrieval-architecture) · [Benchmarks](#-benchmarks) · [Configuration](#-configuration)

</div>

---

## Quick Start

```bash
# Clone TokenScout
git clone https://github.com/vibex-corp/fast-hooks.git

# Install into your project
bash fast-hooks/install.sh /path/to/your-project

# Optional: Enable Gemini Embedding (Tier 2)
echo 'GEMINI_API_KEY=your-key' >> /path/to/your-project/.claude/.env

# Done — start Claude Code as usual
cd /path/to/your-project && claude
```

### Requirements

- **Node.js 18+** (no npm install needed — stdlib only)
- **Claude Code** with hooks support

### Optional enhancements

| Tier | Requires | Benefit |
|------|----------|---------|
| Tier 1: BM25 + Keyword + Graph | Nothing | Fast local retrieval |
| Tier 2: Dense Embedding | `GEMINI_API_KEY` | Semantic search via Gemini Embedding 2 |
| Tier 3: LLM Re-ranking | `claude` CLI or `ANTHROPIC_API_KEY` | Query augmentation + semantic re-ranking |

Each tier gracefully degrades — no API key = tier skipped, zero performance penalty.

---

## How It Works

TokenScout implements a **3-phase scouting pipeline** using 5 Claude Code hooks:

```
Phase 1: MAP              Phase 2: SCOUT              Phase 3: BUDGET
─────────────             ──────────────              ──────────────

┌──────────┐    ┌──────────────┐  ┌──────────────┐    ┌────────────┐
│ Session  │    │ UserPrompt   │  │  PreToolUse  │    │ PostToolUse│
│  Start   │───▶│   Submit     │─▶│  (Read/Grep) │───▶│   + Stop   │
└──────────┘    └──────────────┘  └──────────────┘    └────────────┘
     │                │                  │                    │
  Scan repo      4-tier hybrid      Inject file         Track L_t
  Build graphs   retrieval &        metadata &          Update κ_t
  Index embed    rank candidates    scout context       Gate stop
```

### The 5 Hooks

| # | Hook | Phase | What It Does |
|---|------|-------|-------------|
| 1 | `SessionStart` | Map | Scans repo, extracts signatures, builds 3-layer graph, indexes embeddings |
| 2 | `UserPromptSubmit` | Scout | 4-tier hybrid retrieval: BM25 + keyword + embedding + graph → unified scoring |
| 3 | `PreToolUse(Read)` | Scout | Injects file signatures, imports, dependencies as free context before Read |
| 4 | `PostToolUse` | Budget | Updates lines consumed L_t, confidence κ_t, Information Gain Rate (IGR) |
| 5 | `Stop` | Budget | Blocks premature stop if confidence < 30% and budget remains |

---

## Tiered Retrieval Architecture

TokenScout uses a **4-tier hybrid scoring formula** inspired by [FastCode](https://arxiv.org/abs/2603.01012):

```
S_final = w₁·S_embed + w₂·S_bm25 + w₃·S_keyword + w₄·S_graph
          (0.35)       (0.30)       (0.20)          (0.15)
```

Weights auto-adjust when tiers are unavailable (e.g., no Gemini key → redistribute to BM25+keyword).

### Tier 0: Keyword Matching (free, instant)
Exact string matching on file paths and signature names.

### Tier 1: BM25 Sparse Retrieval (free, instant)
Okapi BM25 index over tokenized file documents (paths + signatures + imports).
Parameters: k1=1.5, b=0.75.

### Tier 2: Gemini Embedding Dense Retrieval (API, cached)
- Model: `gemini-embedding-2-preview` (768 dimensions)
- Binary vector store (Float32 buffer, pre-normalized for dot-product search)
- Cached to disk — only re-indexes when file structure changes
- Includes repo overview embedding for query-repo relevance

### Tier 3: LLM Semantic Re-ranking (API, per-query)
- Query augmentation: expands search terms, detects intent
- Semantic re-ranking: LLM scores candidate relevance (40% LLM + 60% hybrid)
- Auth: OAuth via `claude` CLI (free) or `ANTHROPIC_API_KEY`

### Graph-as-Retrieval-Signal
3-layer code graph (G_dep + G_inh + G_call) used as a retrieval signal:
- Seeds from top BM25/keyword/embedding candidates
- Traverses dependency, inheritance, and call edges
- Relation weights: inheritance (1.0) > calls (0.7) > imports (0.4)

---

## Project Structure

```
fast-hooks/
├── src/
│   ├── tokenscout-hook-session-start-repo-scan-and-context-inject.cjs
│   ├── tokenscout-hook-user-prompt-query-augment-bm25-candidate-rank.cjs
│   ├── tokenscout-hook-pre-tool-use-scout-metadata-and-budget-gate.cjs
│   ├── tokenscout-hook-post-tool-use-context-consumption-tracker-and-confidence-update.cjs
│   ├── tokenscout-hook-stop-epistemic-confidence-check-and-block-if-low.cjs
│   └── lib/
│       ├── tokenscout-state.cjs                          # State management
│       ├── tokenscout-graph.cjs                          # 3-layer graph + proximity scoring
│       ├── tokenscout-confidence-and-budget.cjs          # Confidence, budget, IGR, termination
│       ├── tokenscout-bm25-sparse-retrieval-ranker.cjs   # BM25 index + query
│       ├── tokenscout-gemini-embedding-dense-retrieval.cjs # Gemini embedding + binary vector store
│       ├── tokenscout-llm.cjs                            # LLM integration (Haiku)
│       └── tokenscout-repo-scanner-signature-extractor.cjs # Repo scanner + signature extraction
├── benchmark/                    # Benchmark runner + metrics + prompts
├── install.sh                    # One-command installer
├── uninstall.sh                  # Clean uninstaller
└── README.md
```

---

## Benchmarks

### Run benchmarks

```bash
# Single prompt test
node benchmark/benchmark-runner-orchestrator.cjs \
  --repo https://github.com/fastapi/fastapi \
  --prompts benchmark/prompts/benchmark-prompts-python-fastapi.json

# Results in benchmark/results/report.html
```

Available prompt sets: **FastAPI** (Python) · **Next.js** (TypeScript) · **Kubernetes** (Go)

### Fast mode

Set `TOKENSCOUT_FAST=1` to skip nested LLM calls in hooks (Tier 3 disabled). Keeps BM25 + keyword + embedding + graph active with <100ms hook overhead.

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Enables Tier 2 dense embedding retrieval |
| `ANTHROPIC_API_KEY` | — | Fallback for Tier 3 LLM (if no `claude` CLI) |
| `TOKENSCOUT_FAST` | `0` | Set `1` to disable nested LLM calls |

### Tuning Parameters

| Parameter | Default | Location |
|-----------|---------|----------|
| `tau` (confidence threshold) | 70 | `tokenscout-confidence-and-budget.cjs` |
| `epsilon` (IGR floor) | 0.01 | `tokenscout-confidence-and-budget.cjs` |
| `base` (line budget) | 2,000 | `tokenscout-confidence-and-budget.cjs` |
| `EMBED_DIMENSIONS` | 768 | `tokenscout-gemini-embedding-dense-retrieval.cjs` |
| `BATCH_SIZE` | 250 | `tokenscout-gemini-embedding-dense-retrieval.cjs` |

---

## Language Support

Signature extraction (regex-based) for:

**Python** · **JavaScript** · **TypeScript** · **Java** · **Go** · **Rust** · **C/C++** · **C#** · **Ruby** · **PHP** · **Swift** · **Kotlin**

---

## License

MIT

</div>
