/**
 * Risk Audit MVP - Scoring Logic
 *
 * Scoring rules:
 *   "best" answer  = 0 points
 *   "partial"      = 1 point
 *   "no/unknown"   = 2 points
 *
 * Total max = 18 points (9 questions x 2)
 * LOW: 0-6, MED: 7-12, HIGH: 13-18
 * gaps = count of Step 2 answers that are "no" or "don't know"
 */

/**
 * Answer value constants
 */
const ANSWER_BEST    = 0;
const ANSWER_PARTIAL = 1;
const ANSWER_NO      = 2;

/**
 * Map a raw answer string to its point value.
 * @param {string} answer
 * @returns {number}
 */
function answerToPoints(answer) {
    switch (answer) {
        // Best answers (0 pts)
        case '1':           // Q1: single provider
        case 'prod':        // Q2: tool calling in production
        case 'backoff':     // Q3: retry with backoff
        case 'enforced':    // Q4: hard budget enforced
        case 'yes':         // Q5/Q6/Q7/Q8/Q9: fully implemented
        case 'automated':   // Q6: automated kill
            return ANSWER_BEST;

        // Partial answers (1 pt)
        case '2-3':         // Q1: 2-3 providers
        case 'proto':       // Q2: prototyping
        case 'no-backoff':  // Q3: retry without backoff
        case 'soft':        // Q4: soft limit
        case 'planned':     // Q5: planned
        case 'manual':      // Q6: manual kill
        case 'partial':     // Q7/Q8/Q9: partially implemented
            return ANSWER_PARTIAL;

        // No / Don't know (2 pts)
        case '4plus':
        case 'unknown':
        case 'no':
        case 'none':
        default:
            return ANSWER_NO;
    }
}

/**
 * Determine if a Step 2 answer counts as a "gap".
 * Gaps are answers that are "no" or "don't know".
 * @param {string} answer
 * @returns {boolean}
 */
function isGap(answer) {
    return answerToPoints(answer) === ANSWER_NO;
}

/**
 * Calculate audit score from answers object.
 *
 * @param {Object} answers - keys: q1..q9, values: answer strings
 * @returns {{ score: number, rating: string, gaps: number }}
 */
function calculateScore(answers) {
    const allQuestions = ['q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'q7', 'q8', 'q9'];
    const step2Questions = ['q5', 'q6', 'q7', 'q8', 'q9'];

    let score = 0;
    for (const key of allQuestions) {
        score += answerToPoints(answers[key] || 'unknown');
    }

    let gaps = 0;
    for (const key of step2Questions) {
        if (isGap(answers[key] || 'unknown')) {
            gaps++;
        }
    }

    let rating;
    if (score <= 6) {
        rating = 'LOW';
    } else if (score <= 12) {
        rating = 'MED';
    } else {
        rating = 'HIGH';
    }

    return { score, rating, gaps };
}

// ── Self-test (runs in browser console and Node) ──────────────────────────────

function runSelfTests() {
    let passed = 0;
    let failed = 0;

    function assert(condition, message) {
        if (condition) {
            console.log('[PASS] ' + message);
            passed++;
        } else {
            console.error('[FAIL] ' + message);
            failed++;
        }
    }

    // Case 1: All best answers → score=0, rating=LOW, gaps=0
    const allBest = {
        q1: '1',
        q2: 'prod',
        q3: 'backoff',
        q4: 'enforced',
        q5: 'yes',
        q6: 'automated',
        q7: 'yes',
        q8: 'yes',
        q9: 'yes',
    };
    const result1 = calculateScore(allBest);
    assert(result1.score === 0,    'Case 1: score=0');
    assert(result1.rating === 'LOW', 'Case 1: rating=LOW');
    assert(result1.gaps === 0,     'Case 1: gaps=0');

    // Case 2: All "no/don't know" → score=18, rating=HIGH, gaps=5
    const allNo = {
        q1: 'unknown',
        q2: 'unknown',
        q3: 'unknown',
        q4: 'unknown',
        q5: 'no',
        q6: 'no',
        q7: 'no',
        q8: 'no',
        q9: 'no',
    };
    const result2 = calculateScore(allNo);
    assert(result2.score === 18,    'Case 2: score=18');
    assert(result2.rating === 'HIGH', 'Case 2: rating=HIGH');
    assert(result2.gaps === 5,      'Case 2: gaps=5');

    console.log('Self-tests: ' + passed + ' passed, ' + failed + ' failed.');
    return failed === 0;
}

// Export for Node / test runner
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { calculateScore, answerToPoints, isGap, runSelfTests };
}

// Run self-tests when executed directly with Node.js
if (typeof require !== 'undefined' && require.main === module) {
    const ok = runSelfTests();
    process.exit(ok ? 0 : 1);
}
