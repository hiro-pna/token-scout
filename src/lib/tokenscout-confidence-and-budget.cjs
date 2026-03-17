#!/usr/bin/env node
/**
 * tokenscout-confidence-and-budget.cjs - Confidence estimation, budget, IGR, termination, priority scoring
 *
 * Ported from tokenscout_common.py lines 682-831.
 * Implements multi-signal epistemic confidence updates and cost-aware helpers
 * from the FastCode paper §3.3.
 *
 * @module tokenscout-confidence-and-budget
 */

'use strict';

// ─── Confidence estimation (§3.3.1) ─────────────────────────────────────────

/**
 * Multi-signal confidence boost for a tool use event.
 *
 * Signals (Read tool):
 *   1. Candidate score (0-1) → base_boost 8-20
 *   2. Graph centrality bonus (connections * 0.5, capped at 5)
 *   3. Diminishing returns (1 / (1 + nExplored * 0.15))
 *   4. Coverage boost (>80% explored candidates → +5, >50% → +2)
 *   5. Re-read penalty (0.1x)
 *
 * @param {string} filePath - Relative path of file being read
 * @param {Object} state - Scout state object
 * @param {string} [toolName='Read'] - Tool name: Read | Grep | Glob | Bash
 * @param {Object|null} [toolInput=null] - Tool input payload (used for Bash)
 * @returns {number} Confidence boost capped at 25.0
 */
function estimateConfidenceBoost(filePath, state, toolName = 'Read', toolInput = null) {
  const candidates = state.candidates || {};
  const explored = state.explored_files || [];
  const repoMap = state.repo_map || {};
  const ctx = state.context_state || {};

  if (toolName === 'Read') {
    const relPath = filePath;
    let baseBoost = 5.0;

    // Signal 1: candidate score → base boost 8-20
    if (relPath in candidates) {
      const score = candidates[relPath].score != null ? candidates[relPath].score : 0.5;
      baseBoost = 8.0 + score * 12.0;
    }

    // Signal 2: graph centrality bonus
    const inh = repoMap.inheritance || {};
    const call = repoMap.call_graph || {};
    const deps = repoMap.dependencies || {};
    let connections = (deps[relPath] || []).length;

    for (const key of Object.keys(call)) {
      if (key.startsWith(relPath + '::')) {
        connections += (call[key] || []).length;
      }
    }
    for (const info of Object.values(inh)) {
      if (info.path === relPath) {
        connections += (info.bases || []).length + (info.subclasses || []).length;
      }
    }
    baseBoost += Math.min(5.0, connections * 0.5);

    // Signal 3: diminishing returns
    const nExplored = explored.length;
    const diminishFactor = 1.0 / (1.0 + nExplored * 0.15);
    baseBoost *= diminishFactor;

    // Signal 4: coverage boost
    if (Object.keys(candidates).length > 0) {
      const exploredSet = new Set(explored);
      const candidateKeys = Object.keys(candidates);
      const overlap = candidateKeys.filter(k => exploredSet.has(k)).length;
      const coverage = overlap / candidateKeys.length;
      if (coverage > 0.8) {
        baseBoost += 5.0;
      } else if (coverage > 0.5) {
        baseBoost += 2.0;
      }
    }

    // Signal 5: re-read penalty
    if (explored.includes(relPath)) {
      baseBoost = Math.max(0.5, baseBoost * 0.1);
    }

    return Math.round(Math.min(25.0, baseBoost) * 100) / 100;
  }

  if (toolName === 'Grep') {
    const n = ctx.t || 0;
    return Math.round(Math.max(1.0, 5.0 / (1 + n * 0.1)) * 100) / 100;
  }

  if (toolName === 'Glob') {
    return 1.0;
  }

  if (toolName === 'Bash') {
    const command = (toolInput || {}).command || '';
    if (['test', 'pytest', 'jest', 'cargo test', 'go test'].some(kw => command.includes(kw))) {
      return 10.0;
    }
    if (['grep', 'find', 'rg', 'ag', 'fd'].some(kw => command.includes(kw))) {
      return 3.0;
    }
    return 2.0;
  }

  return 1.0;
}

// ─── Cost-aware helpers (§3.3.2) ─────────────────────────────────────────────

/**
 * Dynamic line budget: B ∝ D_q · H_r
 * @param {number} Dq - Query difficulty score
 * @param {number} Hr - Historical relevance factor
 * @param {number} [base=2000]
 * @returns {number} Integer budget
 */
function computeBudget(Dq, Hr, base = 2000) {
  return Math.trunc(base * Math.max(1, Dq / 50) * Hr);
}

/**
 * Information Gain Rate: IGR_t = (κ_t − κ_{t−1}) / (L_t − L_{t−1})
 * Confidence improvement per unit of context expansion.
 * @param {number} kappaT - Current confidence
 * @param {number} kappaPrev - Previous confidence
 * @param {number} Lt - Current context length
 * @param {number} Lprev - Previous context length
 * @returns {number}
 */
function informationGainRate(kappaT, kappaPrev, Lt, Lprev) {
  const deltaL = Lt - Lprev;
  if (deltaL <= 0) return 0.0;
  return (kappaT - kappaPrev) / deltaL;
}

/**
 * Termination check based on three conditions from paper §3.3.2.
 *  1. Sufficiency:  κ_t ≥ τ
 *  2. Inefficiency: 3 consecutive IGR < ε
 *  3. Exhaustion:   L_t ≥ budget
 *
 * @param {Object} state - Scout state with context_state sub-object
 * @param {number} [tau=70] - Sufficiency threshold
 * @param {number} [epsilon=0.01] - Inefficiency threshold
 * @returns {{terminate: boolean, reason: string}}
 */
function shouldTerminate(state, tau = 70, epsilon = 0.01) {
  const ctx = state.context_state;
  const kappa = ctx.kappa_t;
  const budget = ctx.budget;
  const Lt = ctx.L_t;
  const igrHist = ctx.igr_history || [];

  // 1. Sufficiency
  if (kappa >= tau) {
    return { terminate: true, reason: `sufficient (κ=${kappa.toFixed(1)} ≥ τ=${tau})` };
  }

  // 2. Inefficiency: 3 consecutive low IGR
  if (igrHist.length >= 3) {
    const recent = igrHist.slice(-3);
    if (recent.every(r => Math.abs(r) < epsilon)) {
      return { terminate: true, reason: `inefficient (last 3 IGR < ε=${epsilon})` };
    }
  }

  // 3. Exhaustion
  if (budget > 0 && Lt >= budget) {
    return { terminate: true, reason: `exhausted (L_t=${Lt} ≥ B=${budget})` };
  }

  return { terminate: false, reason: 'continue' };
}

/**
 * Priority selection score: P(u) = w1·Rel(u) + w2·𝟙_tool(u) + w3·Density(u)
 * Paper §3.3.2 Equation 2.
 * @param {number} relevance
 * @param {boolean} toolConfirmed
 * @param {number} density
 * @param {number} [w1=0.5]
 * @param {number} [w2=0.3]
 * @param {number} [w3=0.2]
 * @returns {number}
 */
function priorityScore(relevance, toolConfirmed, density, w1 = 0.5, w2 = 0.3, w3 = 0.2) {
  return w1 * relevance + w2 * (toolConfirmed ? 1.0 : 0.0) + w3 * density;
}

// ─── Exports ─────────────────────────────────────────────────────────────────

module.exports = {
  estimateConfidenceBoost,
  computeBudget,
  informationGainRate,
  shouldTerminate,
  priorityScore
};
