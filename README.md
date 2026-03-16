# TokenScout — Scouting-First Hooks for Claude Code

A hook system for Claude Code that implements the **scouting-first** paradigm for code reasoning: build a structural map first, then read only what matters.

Instead of letting Claude iteratively open files until it finds relevant context (expensive), TokenScout provides a lightweight repo map up front, guides Claude toward high-priority targets, tracks context consumption in real time, and prevents wasteful over-reading.

## How It Works

```
┌──────────────────────────────────────────────────────────────────┐
│                   Without TokenScout (Baseline)                 │
│                                                                  │
│  Question → Glob → Read file → Read file → Read file → ... → ✓  │
│                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^            │
│                    Reads ~65% of files blindly                   │
│                    Tokens scale linearly with repo size          │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    With TokenScout                               │
│                                                                  │
│  Question → Map → Candidates → Grep confirm → Read targets → ✓  │
│             ^^^   ^^^^^^^^^^                   ^^^^^^^^^^^^      │
│             Lightweight        Only reads what the map says      │
│             signatures only    is relevant (3-5 files)           │
└──────────────────────────────────────────────────────────────────┘
```

## Benchmark Results

Measured on synthetic repos of increasing size (8 → 30 → 72 files):

```
┌──────────────────┬────────────┬──────────────┬─────────┬─────────────┐
│ Scenario         │ Repo Size  │ Baseline Tok │ SS Tok  │ Savings     │
├──────────────────┼────────────┼──────────────┼─────────┼─────────────┤
│ Small (auth)     │ 8 files    │ 2,340        │ 1,898   │ 18.9%       │
│ Medium (payment) │ 30 files   │ 10,640       │ 4,650   │ 56.3%       │
│ Large (search)   │ 72 files   │ 31,022       │ 4,850   │ 84.4%       │
├──────────────────┼────────────┼──────────────┼─────────┼─────────────┤
│ TOTAL            │            │ 44,002       │ 11,398  │ 74.1%       │
└──────────────────┴────────────┴──────────────┴─────────┴─────────────┘

Key: savings grow with repo size — the larger the codebase, the more waste is pruned.
```

Token comparison (visual):
```
Baseline:     [████████████████████████████████████████] 44,002
TokenScout:  [██████████                              ] 11,398
              ▼ 74.1% savings
```

## Architecture

TokenScout uses 5 Claude Code hooks that map to a three-phase pipeline:

| Phase | Hook Event | What It Does |
|-------|-----------|--------------|
| **1 — Map** | `SessionStart` | Scans repo, extracts file signatures, imports, symbols (no full reads) |
| **2 — Scout** | `UserPromptSubmit` | Estimates query complexity D_q, computes token budget, ranks candidate files |
| **2 — Scout** | `PreToolUse(Read)` | Injects structural metadata before Claude reads a file (signatures, deps, related files) |
| **3 — Budget** | `PostToolUse` | Tracks context lines consumed L_t, confidence κ_t, Information Gain Rate |
| **3 — Budget** | `Stop` | Blocks stopping if confidence is low but budget remains; allows stop when sufficient |

State vector tracked per query: **S_t = {D_q, H_r, L_t, t, κ_t}**

- **D_q** — Query complexity (0-100)
- **H_r** — Repository entropy (0.5-2.0, based on file count, depth, language diversity)
- **L_t** — Cumulative context lines consumed
- **t** — Tool call count
- **κ_t** — Epistemic confidence (0-100)
- **Budget** B ∝ D_q · H_r — Dynamic line budget

## Installation

### 1. Copy to your project

```bash
# From the root of your project:
cp -r path/to/tokenscout/.claude .claude
```

This places the hooks in `.claude/hooks/` and the config in `.claude/settings.json`.

### 2. Verify structure

```
your-project/
├── .claude/
│   ├── settings.json              # Hook configuration
│   └── hooks/
│       ├── tokenscout_common.py  # Shared library
│       ├── session_start_hook.py  # Phase 1: repo map
│       ├── session_start_hook.sh  # Shell wrapper
│       ├── user_prompt_hook.py    # Phase 2: query analysis
│       ├── user_prompt_hook.sh
│       ├── pre_tool_use_hook.py   # Phase 2: scouting
│       ├── pre_tool_use_hook.sh
│       ├── post_tool_use_hook.py  # Phase 3: context tracking
│       ├── post_tool_use_hook.sh
│       ├── stop_hook.py           # Phase 3: confidence gate
│       └── stop_hook.sh
```

### 3. Make shell scripts executable

```bash
chmod +x .claude/hooks/*.sh
```

### 4. Start Claude Code

```bash
claude
```

Hooks activate automatically. You'll see status messages like:
```
TokenScout: Building repo structural map...
TokenScout: Analyzing query & identifying candidates...
TokenScout: Scouting file metadata...
```

### Requirements

- Python 3.8+
- No external dependencies (stdlib only)

## Configuration

All tuning parameters are in `tokenscout_common.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_files` | 5000 | Max files to scan during map build |
| `tau` (termination threshold) | 70.0 | Confidence level to allow stopping |
| `epsilon` (IGR floor) | 0.01 | IGR below this = diminishing returns |
| `base` (budget base) | 2000 | Base line budget before D_q/H_r scaling |
| `MIN_CONFIDENCE_TO_STOP` | 30.0 | Below this, stop hook blocks if budget remains |

## Monitoring

### Audit Log

Every hook event is logged to `.claude/hooks/tokenscout_audit.jsonl`:

```jsonl
{"ts": 1710000000, "event": "session_start_scan", "total_files": 72, "entropy": 1.35}
{"ts": 1710000001, "event": "query_augmentation", "D_q": 45, "budget": 2700, "num_candidates": 8}
{"ts": 1710000002, "event": "pre_read_scout", "file": "src/auth/login.py", "sigs_count": 5}
{"ts": 1710000003, "event": "post_tool_use", "tool": "Read", "L_t": 120, "kappa_t": 35, "igr": 0.29}
```

### State File

Live state at `.claude/hooks/tokenscout_state.json` — inspect during a session:

```bash
cat .claude/hooks/tokenscout_state.json | python3 -m json.tool
```

## Running Tests

### Unit + integration tests (51 tests)

```bash
python3 -m pytest .claude/hooks/test_tokenscout_hooks.py -v
```

### Token savings benchmark (12 tests + visual report)

```bash
python3 -m pytest .claude/hooks/benchmark_token_savings.py -v -s
```

### Benchmark CLI (visual report only)

```bash
python3 .claude/hooks/benchmark_token_savings.py
python3 .claude/hooks/benchmark_token_savings.py --json          # Machine-readable
python3 .claude/hooks/benchmark_token_savings.py -o report.txt   # Save to file
```

## Language Support

The structural scanner extracts signatures from: Python, JavaScript, TypeScript, Java, Go, Rust, C/C++, C#, Ruby, PHP, Swift, Kotlin.

## Limitations

- The token estimates in benchmarks are approximate (~8 tokens/line heuristic) — actual savings depend on model, prompt structure, and file content density.
- The scouting map is regex-based (not a full AST parser), so it may miss some signatures in complex syntax patterns.
- The stop hook's confidence model is heuristic-based. Real confidence depends on the actual query relevance of explored files.

## Inspired By

This project implements the scouting-first code reasoning paradigm described in academic research on decoupling repository exploration from content consumption, using lightweight metadata navigation and cost-aware context management.
