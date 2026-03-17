'use strict';
/**
 * benchmark-quality-scorer-llm-judge.cjs
 * LLM-as-judge: scores baseline vs TokenScout responses on accuracy,
 * completeness, and relevance using claude -p (Haiku).
 */

const { spawnSync } = require('child_process');

const JUDGE_SYSTEM = `You are an impartial evaluator. Score two AI responses to the same prompt on three dimensions.
Return ONLY a JSON object — no prose, no markdown fences.`;

const SCORE_DIMENSIONS = ['accuracy', 'completeness', 'relevance'];

/**
 * Build the judge prompt.
 */
function buildJudgePrompt(prompt, baselineResponse, tokenscoutResponse) {
  return `Task prompt: ${prompt}

--- Response A (Baseline) ---
${baselineResponse.slice(0, 3000)}

--- Response B (TokenScout) ---
${tokenscoutResponse.slice(0, 3000)}

Score each response 1-5 on:
- accuracy: correct and factually sound information
- completeness: covers all aspects of the question
- relevance: stays on topic without unnecessary padding

Return exactly this JSON structure (integers only):
{
  "baseline": { "accuracy": N, "completeness": N, "relevance": N },
  "tokenscout": { "accuracy": N, "completeness": N, "relevance": N }
}`;
}

/**
 * Run claude -p with Haiku model and return stdout.
 * @param {string} userPrompt
 * @returns {string}
 */
function runClaudeJudge(userPrompt) {
  const result = spawnSync('claude', [
    '-p', userPrompt,
    '--model', 'claude-haiku-4-5',
    '--output-format', 'text',
    '--no-session-persistence'
  ], {
    encoding: 'utf-8',
    timeout: 60000,
    env: { ...process.env }
  });

  if (result.error) throw new Error(`claude judge failed: ${result.error.message}`);
  if (result.status !== 0) throw new Error(`claude judge exit ${result.status}: ${result.stderr}`);
  return result.stdout || '';
}

/**
 * Extract JSON block from LLM output (handles stray text).
 */
function extractJson(text) {
  const match = text.match(/\{[\s\S]*\}/);
  if (!match) throw new Error(`No JSON found in judge output: ${text.slice(0, 200)}`);
  return JSON.parse(match[0]);
}

/**
 * Compute totals for a score object.
 */
function addTotals(scores) {
  return {
    ...scores,
    total: SCORE_DIMENSIONS.reduce((sum, d) => sum + (scores[d] || 0), 0)
  };
}

/**
 * Score quality of baseline vs tokenscout response using LLM judge.
 * @param {string} prompt
 * @param {string} baselineResponse
 * @param {string} tokenscoutResponse
 * @returns {{ baseline: object, tokenscout: object }}
 */
function scoreQuality(prompt, baselineResponse, tokenscoutResponse) {
  const judgePrompt = buildJudgePrompt(prompt, baselineResponse, tokenscoutResponse);

  let raw;
  try {
    raw = runClaudeJudge(judgePrompt);
  } catch (err) {
    const fallback = { accuracy: 0, completeness: 0, relevance: 0, total: 0, error: err.message };
    return { baseline: fallback, tokenscout: { ...fallback } };
  }

  try {
    const parsed = extractJson(raw);
    return {
      baseline: addTotals(parsed.baseline || {}),
      tokenscout: addTotals(parsed.tokenscout || {})
    };
  } catch (err) {
    const fallback = { accuracy: 0, completeness: 0, relevance: 0, total: 0, parse_error: err.message };
    return { baseline: fallback, tokenscout: { ...fallback } };
  }
}

module.exports = { scoreQuality };
