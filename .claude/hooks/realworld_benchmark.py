#!/usr/bin/env python3
"""
TokenScout — Real-World Usage Analyzer

Parses tokenscout_audit.jsonl from actual Claude Code sessions to measure
REAL token savings, not simulated ones. Run after a Claude Code session.

Usage:
    python3 realworld_benchmark.py                    # analyze current session
    python3 realworld_benchmark.py path/to/audit.jsonl # analyze specific log
    python3 realworld_benchmark.py --compare before.jsonl after.jsonl

Output:
    - Files read, lines consumed, confidence trajectory
    - Budget utilization efficiency
    - Estimated vs actual savings
    - IGR curve (when did reading become wasteful?)
"""
import json
import os
import sys
import argparse
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class SessionStats:
    """Aggregated stats from one Claude Code session."""
    label: str = ""
    total_events: int = 0
    total_tool_calls: int = 0
    files_read: List[str] = field(default_factory=list)
    files_scouted: List[str] = field(default_factory=list)
    total_lines_consumed: int = 0
    budget_allocated: int = 0
    final_confidence: float = 0.0
    peak_confidence: float = 0.0
    query_complexity: int = 0
    repo_entropy: float = 0.0
    repo_files: int = 0
    igr_history: List[float] = field(default_factory=list)
    confidence_trajectory: List[float] = field(default_factory=list)
    lines_trajectory: List[int] = field(default_factory=list)
    termination_reason: str = ""
    duration_seconds: float = 0.0
    # Derived
    tokens_estimated: int = 0  # ~8 tokens/line + 50/tool overhead
    wasteful_reads: int = 0    # reads after IGR dropped below epsilon


def parse_audit_log(filepath: str) -> SessionStats:
    """Parse a tokenscout_audit.jsonl file into SessionStats."""
    stats = SessionStats(label=os.path.basename(filepath))
    events = []

    try:
        with open(filepath, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except (IOError, OSError) as e:
        print(f"Error reading {filepath}: {e}", file=sys.stderr)
        return stats

    if not events:
        return stats

    stats.total_events = len(events)

    # Extract timestamps for duration
    timestamps = [e.get("ts", 0) for e in events if e.get("ts")]
    if len(timestamps) >= 2:
        stats.duration_seconds = timestamps[-1] - timestamps[0]

    low_igr_streak = 0
    epsilon = 0.01

    for event in events:
        ev_type = event.get("event", "")

        if ev_type == "session_start_scan":
            stats.repo_files = event.get("total_files", 0)
            stats.repo_entropy = event.get("entropy", 0.0)

        elif ev_type == "query_augmentation":
            stats.query_complexity = event.get("D_q", 0)
            stats.budget_allocated = event.get("budget", 0)

        elif ev_type == "pre_read_scout":
            file_name = event.get("file", "")
            if file_name and file_name not in stats.files_scouted:
                stats.files_scouted.append(file_name)

        elif ev_type == "post_tool_use":
            stats.total_tool_calls += 1
            tool = event.get("tool", "")
            lines = event.get("lines_consumed", 0)
            kappa = event.get("kappa_t", 0.0)
            igr = event.get("igr", 0.0)
            L_t = event.get("L_t", 0)

            stats.total_lines_consumed = max(stats.total_lines_consumed, L_t)
            stats.confidence_trajectory.append(kappa)
            stats.lines_trajectory.append(L_t)
            stats.igr_history.append(igr)
            stats.peak_confidence = max(stats.peak_confidence, kappa)

            if tool == "Read":
                file_name = event.get("file", "")
                if file_name:
                    stats.files_read.append(file_name)

            # Track wasteful reads (low IGR streak)
            if abs(igr) < epsilon:
                low_igr_streak += 1
                if low_igr_streak >= 3 and tool == "Read":
                    stats.wasteful_reads += 1
            else:
                low_igr_streak = 0

            if event.get("terminate"):
                stats.termination_reason = event.get("reason", "unknown")

        elif ev_type in ("stop_allowed", "stop_blocked"):
            stats.final_confidence = event.get("kappa", stats.peak_confidence)
            if event.get("reason"):
                stats.termination_reason = event.get("reason", "")

    # Estimate tokens: ~8 tokens/line + 50 tokens/tool call overhead
    stats.tokens_estimated = stats.total_lines_consumed * 8 + stats.total_tool_calls * 50

    return stats


# ─── Report generation ────────────────────────────────────────────────────────

def print_session_report(stats: SessionStats) -> str:
    """Generate a visual report for a single session."""
    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════╗")
    lines.append("║       TokenScout — Real-World Session Analysis              ║")
    lines.append("╠══════════════════════════════════════════════════════════════╣")
    lines.append(f"║  Source: {stats.label:<50}║")
    lines.append(f"║  Duration: {stats.duration_seconds:.1f}s | Events: {stats.total_events:<25}║")
    lines.append("╠══════════════════════════════════════════════════════════════╣")

    # Repo info
    lines.append("║  REPOSITORY                                                 ║")
    lines.append(f"║    Files scanned: {stats.repo_files:<10} Entropy H_r: {stats.repo_entropy:<14.2f}║")
    lines.append("║                                                              ║")

    # Query info
    lines.append("║  QUERY                                                      ║")
    lines.append(f"║    Complexity D_q: {stats.query_complexity:<10} Budget: {stats.budget_allocated:<16} lines║")
    lines.append("║                                                              ║")

    # Exploration
    lines.append("║  EXPLORATION                                                ║")
    lines.append(f"║    Tool calls:     {stats.total_tool_calls:<41}║")
    lines.append(f"║    Files scouted:  {len(stats.files_scouted):<41}║")
    lines.append(f"║    Files read:     {len(set(stats.files_read)):<41}║")
    lines.append(f"║    Lines consumed: {stats.total_lines_consumed:<41}║")
    lines.append(f"║    Tokens (est):   {stats.tokens_estimated:,}{' ' * (40 - len(f'{stats.tokens_estimated:,}'))}║")
    lines.append("║                                                              ║")

    # Budget efficiency
    budget_pct = (stats.total_lines_consumed / max(1, stats.budget_allocated)) * 100
    lines.append("║  BUDGET EFFICIENCY                                          ║")
    lines.append(f"║    Budget used: {budget_pct:>6.1f}% ({stats.total_lines_consumed}/{stats.budget_allocated} lines){' ' * max(0, 20 - len(f'({stats.total_lines_consumed}/{stats.budget_allocated} lines)'))}║")
    lines.append(f"║    Wasteful reads: {stats.wasteful_reads} (after IGR dropped){' ' * max(0, 22 - len(f'{stats.wasteful_reads} (after IGR dropped)'))}║")
    lines.append("║                                                              ║")

    # Confidence
    lines.append("║  CONFIDENCE                                                 ║")
    lines.append(f"║    Final κ:  {stats.final_confidence:>6.1f}  Peak κ: {stats.peak_confidence:>6.1f}{' ' * 26}║")
    lines.append(f"║    Termination: {stats.termination_reason:<44}║")

    # IGR curve (ASCII sparkline)
    if stats.igr_history:
        lines.append("║                                                              ║")
        lines.append("║  IGR CURVE (Information Gain Rate over time)                 ║")
        sparkline = _ascii_sparkline(stats.igr_history, width=50)
        lines.append(f"║    {sparkline}        ║")
        lines.append(f"║    {'↑ high gain' + ' ' * 20 + 'low gain ↓':<55} ║")

    # Confidence trajectory
    if stats.confidence_trajectory:
        lines.append("║                                                              ║")
        lines.append("║  CONFIDENCE TRAJECTORY (κ over tool calls)                   ║")
        sparkline = _ascii_sparkline(stats.confidence_trajectory, width=50, min_val=0, max_val=100)
        lines.append(f"║    {sparkline}        ║")
        lines.append(f"║    {'0%' + ' ' * 24 + '50%' + ' ' * 22 + '100%':<55} ║")

    lines.append("╚══════════════════════════════════════════════════════════════╝")

    report = "\n".join(lines)
    print(report)
    return report


def print_comparison_report(before: SessionStats, after: SessionStats) -> str:
    """Compare two sessions (without hooks vs with hooks)."""
    lines = []
    lines.append("╔══════════════════════════════════════════════════════════════════════╗")
    lines.append("║         TokenScout — Before vs After Comparison                     ║")
    lines.append("╠══════════════════╦════════════════╦════════════════╦═════════════════╣")
    lines.append("║  Metric          ║ Before (no TS) ║ After (w/ TS)  ║ Savings         ║")
    lines.append("╠══════════════════╬════════════════╬════════════════╬═════════════════╣")

    def _row(label, before_val, after_val, fmt="d", higher_is_better=False):
        if fmt == "d":
            b_str = f"{before_val:>12,}"
            a_str = f"{after_val:>12,}"
        elif fmt == "f":
            b_str = f"{before_val:>12.1f}"
            a_str = f"{after_val:>12.1f}"
        else:
            b_str = f"{before_val!s:>12}"
            a_str = f"{after_val!s:>12}"

        if before_val > 0:
            pct = ((before_val - after_val) / before_val) * 100
            if not higher_is_better:
                sign = "↓" if pct > 0 else "↑"
            else:
                sign = "↑" if pct < 0 else "↓"
            sav = f"{sign} {abs(pct):.1f}%"
        else:
            sav = "N/A"
        return f"║  {label:<16}║ {b_str:<14} ║ {a_str:<14} ║ {sav:<15} ║"

    lines.append(_row("Tool calls", before.total_tool_calls, after.total_tool_calls))
    lines.append(_row("Files read", len(set(before.files_read)), len(set(after.files_read))))
    lines.append(_row("Lines consumed", before.total_lines_consumed, after.total_lines_consumed))
    lines.append(_row("Tokens (est)", before.tokens_estimated, after.tokens_estimated))
    lines.append(_row("Confidence", before.final_confidence, after.final_confidence, "f", higher_is_better=True))
    lines.append(_row("Wasteful reads", before.wasteful_reads, after.wasteful_reads))
    lines.append("╠══════════════════╩════════════════╩════════════════╩═════════════════╣")

    # Overall savings
    if before.tokens_estimated > 0:
        overall = (1 - after.tokens_estimated / before.tokens_estimated) * 100
        lines.append(f"║  OVERALL TOKEN SAVINGS: {overall:>6.1f}%{' ' * 37}║")
    else:
        lines.append("║  OVERALL TOKEN SAVINGS: N/A (no baseline data)                     ║")

    lines.append("╚══════════════════════════════════════════════════════════════════════╝")

    report = "\n".join(lines)
    print(report)
    return report


def _ascii_sparkline(values: List[float], width: int = 50, min_val=None, max_val=None) -> str:
    """Create ASCII sparkline from a list of values."""
    if not values:
        return ""
    blocks = " ▁▂▃▄▅▆▇█"
    mn = min_val if min_val is not None else min(values)
    mx = max_val if max_val is not None else max(values)
    rng = mx - mn if mx != mn else 1.0

    # Downsample if too many points
    if len(values) > width:
        step = len(values) / width
        sampled = [values[int(i * step)] for i in range(width)]
    else:
        sampled = values

    chars = []
    for v in sampled:
        normalized = (v - mn) / rng
        idx = int(normalized * (len(blocks) - 1))
        idx = max(0, min(len(blocks) - 1, idx))
        chars.append(blocks[idx])

    return "".join(chars)


# ─── JSON export ──────────────────────────────────────────────────────────────

def export_json(stats: SessionStats) -> Dict[str, Any]:
    """Export session stats as JSON for programmatic use."""
    return {
        "label": stats.label,
        "total_events": stats.total_events,
        "total_tool_calls": stats.total_tool_calls,
        "unique_files_read": len(set(stats.files_read)),
        "files_scouted": len(stats.files_scouted),
        "total_lines_consumed": stats.total_lines_consumed,
        "budget_allocated": stats.budget_allocated,
        "budget_utilization_pct": round(
            (stats.total_lines_consumed / max(1, stats.budget_allocated)) * 100, 1
        ),
        "final_confidence": round(stats.final_confidence, 2),
        "peak_confidence": round(stats.peak_confidence, 2),
        "tokens_estimated": stats.tokens_estimated,
        "wasteful_reads": stats.wasteful_reads,
        "termination_reason": stats.termination_reason,
        "duration_seconds": round(stats.duration_seconds, 1),
        "query_complexity": stats.query_complexity,
        "repo_entropy": stats.repo_entropy,
        "repo_files": stats.repo_files,
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TokenScout Real-World Usage Analyzer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 realworld_benchmark.py                           # current session
  python3 realworld_benchmark.py path/to/audit.jsonl       # specific log
  python3 realworld_benchmark.py --compare before.jsonl after.jsonl
  python3 realworld_benchmark.py --json                    # JSON output
        """,
    )
    parser.add_argument("logfile", nargs="?", default=None,
                        help="Path to tokenscout_audit.jsonl (default: auto-detect)")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="Compare two session logs (before/after hooks)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON instead of visual report")

    args = parser.parse_args()

    if args.compare:
        before = parse_audit_log(args.compare[0])
        after = parse_audit_log(args.compare[1])
        if args.json:
            print(json.dumps({
                "before": export_json(before),
                "after": export_json(after),
                "savings_pct": round(
                    (1 - after.tokens_estimated / max(1, before.tokens_estimated)) * 100, 1
                ),
            }, indent=2))
        else:
            print_comparison_report(before, after)
        return

    # Single session analysis
    logfile = args.logfile
    if not logfile:
        # Auto-detect from CLAUDE_PROJECT_DIR or current directory
        candidates = [
            os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "."),
                         ".claude", "hooks", "tokenscout_audit.jsonl"),
            os.path.join(".", ".claude", "hooks", "tokenscout_audit.jsonl"),
        ]
        for c in candidates:
            if os.path.exists(c):
                logfile = c
                break
        if not logfile:
            print("No audit log found. Run a Claude Code session with TokenScout first.",
                  file=sys.stderr)
            print("Usage: python3 realworld_benchmark.py [path/to/audit.jsonl]",
                  file=sys.stderr)
            sys.exit(1)

    stats = parse_audit_log(logfile)

    if args.json:
        print(json.dumps(export_json(stats), indent=2))
    else:
        print_session_report(stats)


if __name__ == "__main__":
    main()
