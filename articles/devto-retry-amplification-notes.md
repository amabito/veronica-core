# Article Notes (do not publish)

## 3 Stronger Alternative Titles

1. **"The $0.64 bug: how nested retries silently multiply your LLM costs"**
   - Angle: Cost horror with a specific, memorable dollar amount
   - Strength: The number is small enough to be believable, large enough to be alarming at scale
   - Risk: Requires the reader to care about $0.64 (they will once they multiply by request volume)

2. **"Your LLM agent retries 3 times per layer. That's 64 calls, not 9."**
   - Angle: Correcting a common misconception
   - Strength: The "not 9" directly contradicts what most developers assume (3 layers x 3 = 9)
   - Risk: Slightly more technical, might lose readers who don't build layered agents yet

3. **"I benchmarked retry amplification in LangChain. The worst case is 64x."**
   - Angle: Benchmark-driven, data-first
   - Strength: "I benchmarked" signals this is measured, not theorized
   - Risk: "LangChain" in the title may narrow the audience (the problem applies to all frameworks)

---

## 5 dev.to Tag Combinations

1. `python, llm, langchain, ai` -- broadest reach, hits all relevant tag feeds
2. `python, openai, costoptimization, webdev` -- targets cost-conscious developers
3. `python, ai, architecture, tutorial` -- positions as engineering knowledge, not product
4. `python, langchain, devops, monitoring` -- reaches the infra/ops crowd
5. `python, ai, beginners, tutorial` -- wider funnel, but lower signal from experienced developers

**Recommendation:** Option 1 for first post. Option 2 if retargeting after feedback.

---

## Shorter Version of the Intro

> One user click. One document. My LangChain agent made 64 API calls to GPT-4o. The bill for that single request was $1.92. The correct answer needed 1 call. The bug wasn't in the retries -- it was in how retries multiply across layers.

(2 sentences shorter. Drops "Last month I was debugging" framing. More direct.)

---

## Harsher Version of the Intro

> I looked at my OpenAI usage dashboard and found a single user request that made 64 API calls. Not a DDoS. Not a bug in the prompt. Just three layers of retry logic doing exactly what they were told. Each layer retried 3 times. 4 x 4 x 4 = 64. Cost: $1.92 for a task that should have cost a penny. Nobody in the stack tracked the total. That's the bug.

(More confrontational. Names the dashboard. "Not a DDoS" sets the tone immediately.)

---

## 5 Brutal Editor Notes

1. **The "before" code example is too clean.** Real retry logic in LangChain uses `tenacity` decorators, callback handlers, and nested chain invocations -- not hand-rolled for loops. The reader who actually has this problem will look at the "before" code and think "that's not what my code looks like." Consider adding a note: "In practice this retry nesting happens across `@retry` decorators, chain callbacks, and provider-level retries -- not in code you wrote yourself. That's why it's hard to spot." Without this caveat, experienced developers will dismiss the example as a strawman.

2. **The $1.92 number in the intro needs a footnote or it looks made up.** Show the math: 64 calls x ~2000 input tokens x $2.50/1M = $0.32 input + 64 x ~500 output tokens x $10.00/1M = $0.32 output = $0.64 at GPT-4o pricing. $1.92 works if the prompt is longer (~6K tokens) or if using GPT-4-turbo pricing. Either show the exact calculation or use $0.64 (which is mathematically airtight at the token counts stated in the article). An inaccurate cost number in the opening line destroys credibility for the entire piece.

3. **The "What this does not do" section is too short.** This is where skeptical readers look to judge whether the author is honest. Add: "It does not prevent the LLM from generating wrong answers. It does not make your agent smarter. It does not reduce latency -- it just stops the bleeding when something goes wrong." The current version lists 4 negatives. Add 2 more that address what readers will incorrectly assume.

4. **Missing: what happens to the user when HALT fires.** The article shows `Decision.HALT` but doesn't explain what the user sees. Does the request return a 500? A partial result? A friendly error? Developers evaluating a library care about failure UX, not just failure detection. Add 3 lines showing how to return a useful error to the end user when containment triggers.

5. **The closing is too soft.** "If you've looked at your OpenAI bill and thought 'that's higher than expected'" is a passive suggestion. The reader who made it to the end already agrees with you. End with something actionable: "Run `python benchmarks/bench_retry_amplification.py` in the repo. It takes 2 seconds, no API key needed. If the numbers surprise you, your production stack probably has the same bug." Give them a next step that costs them nothing and proves your point.
