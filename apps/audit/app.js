/**
 * Risk Audit MVP - UI Logic
 *
 * Hash-based routing: #step1, #step2, #score
 * Answers persisted to localStorage under key "audit_answers".
 */

const STORAGE_KEY = 'audit_answers';

// ── Question definitions ───────────────────────────────────────────────────────

const QUESTIONS = {
    step1: [
        {
            id: 'q1',
            text: 'How many LLM providers does your system call?',
            options: [
                { value: '1',       label: '1' },
                { value: '2-3',     label: '2–3' },
                { value: '4plus',   label: '4+' },
                { value: 'unknown', label: "Don't know" },
            ],
        },
        {
            id: 'q2',
            text: 'Do your agents use tool calling (function calling)?',
            options: [
                { value: 'prod',    label: 'Yes, in production' },
                { value: 'proto',   label: 'Yes, prototyping' },
                { value: 'no',      label: 'No' },
                { value: 'unknown', label: "Don't know" },
            ],
        },
        {
            id: 'q3',
            text: 'Do agents retry on failure automatically?',
            options: [
                { value: 'backoff',    label: 'Yes, with backoff' },
                { value: 'no-backoff', label: 'Yes, no backoff' },
                { value: 'none',       label: 'No retries' },
                { value: 'unknown',    label: "Don't know" },
            ],
        },
        {
            id: 'q4',
            text: 'Do you have a hard budget limit per run?',
            options: [
                { value: 'enforced', label: 'Yes, enforced' },
                { value: 'soft',     label: 'Soft limit only' },
                { value: 'no',       label: 'No' },
                { value: 'unknown',  label: "Don't know" },
            ],
        },
    ],
    step2: [
        {
            id: 'q5',
            text: 'Is there a circuit breaker on repeated failures?',
            options: [
                { value: 'yes',     label: 'Yes' },
                { value: 'planned', label: 'Planned' },
                { value: 'no',      label: 'No' },
                { value: 'unknown', label: "Don't know" },
            ],
        },
        {
            id: 'q6',
            text: 'Can you kill a runaway agent in < 30 seconds?',
            options: [
                { value: 'automated', label: 'Yes, automated' },
                { value: 'manual',    label: 'Yes, manual' },
                { value: 'no',        label: 'No' },
                { value: 'unknown',   label: "Don't know" },
            ],
        },
        {
            id: 'q7',
            text: 'Is outbound HTTP from agents allow-listed?',
            options: [
                { value: 'yes',     label: 'Yes' },
                { value: 'partial', label: 'Partially' },
                { value: 'no',      label: 'No' },
                { value: 'unknown', label: "Don't know" },
            ],
        },
        {
            id: 'q8',
            text: 'Do you scan agent outputs for leaked secrets?',
            options: [
                { value: 'yes',     label: 'Yes' },
                { value: 'partial', label: 'Partially' },
                { value: 'no',      label: 'No' },
                { value: 'unknown', label: "Don't know" },
            ],
        },
        {
            id: 'q9',
            text: 'Do you have per-agent step limits?',
            options: [
                { value: 'yes',     label: 'Yes' },
                { value: 'partial', label: 'Partially' },
                { value: 'no',      label: 'No' },
                { value: 'unknown', label: "Don't know" },
            ],
        },
    ],
};

// ── Storage helpers ────────────────────────────────────────────────────────────

function loadAnswers() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        return raw ? JSON.parse(raw) : {};
    } catch (_) {
        return {};
    }
}

function saveAnswer(questionId, value) {
    const answers = loadAnswers();
    answers[questionId] = value;
    localStorage.setItem(STORAGE_KEY, JSON.stringify(answers));
}

function clearAnswers() {
    localStorage.removeItem(STORAGE_KEY);
}

// ── Routing ────────────────────────────────────────────────────────────────────

function getHash() {
    return window.location.hash || '#step1';
}

function navigate(hash) {
    window.location.hash = hash;
}

function renderCurrentScreen() {
    const hash = getHash();
    switch (hash) {
        case '#step2':
            renderStep('step2');
            break;
        case '#score':
            renderScore();
            break;
        default:
            renderStep('step1');
    }
}

// ── Rendering helpers ──────────────────────────────────────────────────────────

function renderStep(stepKey) {
    const questions = QUESTIONS[stepKey];
    const answers   = loadAnswers();
    const stepNum   = stepKey === 'step1' ? 1 : 2;
    const totalSteps = 2;

    const app = document.getElementById('app');
    app.innerHTML = '';

    // Progress
    const progress = document.createElement('div');
    progress.className = 'progress';
    progress.textContent = 'Step ' + stepNum + ' of ' + totalSteps;
    app.appendChild(progress);

    // Title
    const title = document.createElement('h2');
    title.textContent = stepKey === 'step1' ? 'LLM Stack' : 'Safety Layer';
    app.appendChild(title);

    // Questions
    questions.forEach(function(q) {
        const block = document.createElement('div');
        block.className = 'question-block';

        const label = document.createElement('p');
        label.className = 'question-text';
        label.textContent = q.text;
        block.appendChild(label);

        const optGroup = document.createElement('div');
        optGroup.className = 'option-group';

        q.options.forEach(function(opt) {
            const btn = document.createElement('button');
            btn.className = 'option-btn';
            btn.dataset.questionId = q.id;
            btn.dataset.value = opt.value;
            btn.textContent = opt.label;

            if (answers[q.id] === opt.value) {
                btn.classList.add('selected');
            }

            btn.addEventListener('click', function() {
                // Deselect siblings
                optGroup.querySelectorAll('.option-btn').forEach(function(b) {
                    b.classList.remove('selected');
                });
                btn.classList.add('selected');
                saveAnswer(q.id, opt.value);
            });

            optGroup.appendChild(btn);
        });

        block.appendChild(optGroup);
        app.appendChild(block);
    });

    // Navigation buttons
    const nav = document.createElement('div');
    nav.className = 'nav-buttons';

    if (stepKey === 'step1') {
        const nextBtn = document.createElement('button');
        nextBtn.className = 'nav-btn primary';
        nextBtn.textContent = 'Next: Safety Layer';
        nextBtn.addEventListener('click', function() {
            if (!allAnswered('step1')) {
                alert('Please answer all questions before continuing.');
                return;
            }
            navigate('#step2');
        });
        nav.appendChild(nextBtn);
    } else {
        const backBtn = document.createElement('button');
        backBtn.className = 'nav-btn secondary';
        backBtn.textContent = 'Back';
        backBtn.addEventListener('click', function() { navigate('#step1'); });
        nav.appendChild(backBtn);

        const scoreBtn = document.createElement('button');
        scoreBtn.className = 'nav-btn primary';
        scoreBtn.textContent = 'See My Score';
        scoreBtn.addEventListener('click', function() {
            if (!allAnswered('step2')) {
                alert('Please answer all questions before continuing.');
                return;
            }
            navigate('#score');
        });
        nav.appendChild(scoreBtn);
    }

    app.appendChild(nav);
}

function allAnswered(stepKey) {
    const answers = loadAnswers();
    return QUESTIONS[stepKey].every(function(q) {
        return answers[q.id] !== undefined;
    });
}

function renderScore() {
    const answers = loadAnswers();
    const result  = calculateScore(answers);

    const app = document.getElementById('app');
    app.innerHTML = '';

    const title = document.createElement('h2');
    title.textContent = 'Your Risk Score';
    app.appendChild(title);

    // Score display
    const scoreBox = document.createElement('div');
    scoreBox.className = 'score-box rating-' + result.rating.toLowerCase();

    const scoreNum = document.createElement('div');
    scoreNum.className = 'score-number';
    scoreNum.textContent = result.score + ' / 18';
    scoreBox.appendChild(scoreNum);

    const ratingBadge = document.createElement('div');
    ratingBadge.className = 'rating-badge';
    ratingBadge.textContent = result.rating + ' RISK';
    scoreBox.appendChild(ratingBadge);

    app.appendChild(scoreBox);

    // Gaps summary
    const gapText = document.createElement('p');
    gapText.className = 'gap-summary';
    if (result.gaps === 0) {
        gapText.textContent = 'No safety gaps detected in Step 2. Well done!';
    } else {
        gapText.textContent = result.gaps + ' safety gap' + (result.gaps > 1 ? 's' : '') +
            ' found (questions answered "No" or "Don\'t know" in the Safety Layer).';
    }
    app.appendChild(gapText);

    // Interpretation
    const interpretation = document.createElement('p');
    interpretation.className = 'interpretation';
    if (result.rating === 'LOW') {
        interpretation.textContent = 'Your agent system has solid coverage. Keep iterating to maintain this posture.';
    } else if (result.rating === 'MED') {
        interpretation.textContent = 'Several areas need attention. Prioritize the gaps in your Safety Layer.';
    } else {
        interpretation.textContent = 'Critical gaps present. Consider pausing production deployments until safety controls are in place.';
    }
    app.appendChild(interpretation);

    // Actions
    const nav = document.createElement('div');
    nav.className = 'nav-buttons';

    const restartBtn = document.createElement('button');
    restartBtn.className = 'nav-btn secondary';
    restartBtn.textContent = 'Retake Audit';
    restartBtn.addEventListener('click', function() {
        clearAnswers();
        navigate('#step1');
    });
    nav.appendChild(restartBtn);

    app.appendChild(nav);
}

// ── Boot ───────────────────────────────────────────────────────────────────────

window.addEventListener('hashchange', renderCurrentScreen);
window.addEventListener('DOMContentLoaded', function() {
    // Run self-tests in console (scoring.js must be loaded first)
    if (typeof runSelfTests === 'function') {
        runSelfTests();
    }
    renderCurrentScreen();
});
