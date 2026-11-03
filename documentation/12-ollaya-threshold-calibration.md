# 12 — Ollaya Threshold Calibration

> This guide covers how to calibrate and tune Ollaya's evidence judgment thresholds for your workspace.
> You only need to follow this procedure **after** enabling Ollaya and running queries through the system with real data.

---

## Why calibration is necessary

Ollaya's probability scale differs from Jev's cloud API. The same threshold values that work for Jev will not work for Ollaya — passages will be kept or dropped incorrectly. The thresholds must be **re-derived** for Ollaya's model before deploying to production.

The `.env.example` ships with calibrated values derived from a 10-case / 23-question sweep against `zerikai_memory` on 2026-10-31. These serve as a starting point but should be validated on your own data and use cases.

---

## The calibration process

### Step 1: Assemble a labeled dataset

For each (query, passage) pair in your test set, record the correct verdict:

- Should this passage be kept as evidence?
- Does it contradict the premise?
- Does it attempt prompt injection?

**Sources of labels:**

- Hand-label a batch of real queries and passages from your application logs or from live usage.
- Identify passages that should (or should not) be kept as evidence, and whether they contradict or attempt injection.

You need **at minimum 20–40 labeled pairs** to place a defensible threshold. More data yields more accurate cuts.

### Step 2: Collect probability scores from Ollaya

For each labeled pair, send the query and passage to Ollaya and record the raw probability scores:

```bash
# Query Ollaya's /api/decide endpoint
curl -X POST http://127.0.0.1:11435/api/decide \
  -H "Content-Type: application/json" \
  -d '{"model": "winnow:e4b", "questions": [...], "context": "..."}'
```

Store the results in a structured format (JSON, CSV, etc.):

```json
{
  "query_id": "q01",
  "passage_id": "p01",
  "probabilities": {
    "relevant": 0.87,
    "evidence": 0.71,
    "injection": 0.05,
    "contradicts": 0.12,
    "answerable": 0.65
  },
  "expected_verdict": {"keep": true, "contradicts": false}
}
```

### Step 3: Run a threshold sweep

Using your collected probabilities and expected verdicts, sweep across candidate threshold values and score each operating point:

**Pseudocode for the sweep:**

```
for rel_min in [0.20, 0.25, 0.30, 0.35, 0.40]:
  for evid_min in [0.15, 0.20, 0.25, 0.30]:
    correct_kept = 0
    incorrect_dropped = 0
    for (query, passage, probs, expected) in labeled_set:
      keep = (probs.relevant >= rel_min) AND (probs.evidence >= evid_min)
      if keep == expected.keep:
        if expected.keep:
          correct_kept += 1
        else:
          incorrect_dropped += 1
    report(rel_min, evid_min, correct_kept, incorrect_dropped)
```

**For each threshold pair, report:**

- How many labeled *correct* passages survive (should be 100%)
- How many *incorrect* passages are also kept (false positives)
- Pick the lowest threshold where all correct passages survive

### Step 4: Choose the operating point

**The objective is not maximum F1.** Instead, prioritize:

1. **Never lose the correct passage.** If you drop the passage that actually answers the query, the result is "I couldn't find anything relevant" — a visible failure to the user. All labeled *correct* passages must survive at your chosen threshold.

2. **Only then tighten to drop junk.** Once all correct passages survive, raise thresholds incrementally to exclude incorrect / injected passages.

**For `injection` and `contradicts`:** These fields need both positive and negative examples to place a cut. The calibration data includes at least one labelled positive (e.g., a detected injection attempt) and one negative (clean passage). With only 1–2 examples, you can verify the question works but cannot derive a precise threshold — aim for the middle of the confidence range (e.g., 0.50) and adjust based on live traffic.

### Step 5: Verify on real traffic (post-deployment)

After deploying Ollaya to production with your chosen thresholds:

1. Run real queries through your system.
2. Spot-check 5–10 results manually — are the kept passages actually relevant?
3. If passages are routinely wrong (dropped when they should be kept, or kept when they should be dropped), return to Step 2 with new data and re-sweep.
4. Iterate until results are stable across diverse queries.

### Step 6: Store the values

Write the chosen threshold values to your `.env` file:

```
OLLAYA_RELEVANCE_MIN=0.30
OLLAYA_EVIDENCE_MIN=0.20
OLLAYA_INJECTION_MAX=0.50
OLLAYA_CONTRA_MIN=0.50
OLLAYA_ANSWERABILITY_MIN=0.40
```

No code changes required — `main.py` reads these at startup.

---

## Important caveats

### Re-calibrate per model

Different Ollaya models (`winnow:e4b`, `winnow:12b`, etc.) have different probability scales. Thresholds calibrated for one model **will not transfer** to another without adjustment.

**Action:** If you switch Ollaya models, repeat the sweep.

### Re-calibrate per domain / corpus

The thresholds in `.env.example` were calibrated on `zerikai_memory`'s codebase (a Python package). If you deploy Ollaya on a different corpus (e.g., documentation, legal text, customer support queries), the probability distribution will shift.

**Action:** Collect and label data from your own corpus, then re-sweep.

### The sweep is not a substitute for labels

With only 53 labeled pairs, the sweep can place a single defensible cut per threshold. Finer tuning (e.g., F1 optimization) requires significantly more labeled data. If you have <20 labeled pairs, focus on manual adjustment based on live traffic.

**Action:** Start with the shipped defaults, verify on real traffic, and adjust incrementally.

---

## Validation checklist

After calibration, confirm:

- ✅ **Calibration set:** Every labeled *correct* passage survives at your chosen thresholds.
- ✅ **Manual spot-check:** Review 5–10 real queries; verify passages marked as evidence are actually relevant.
- ✅ **Config stored:** Your threshold values are in `.env` and match `.env.example` documentation.
- ✅ **Probe reproducible:** Running `probe_v2b.py --sweep` with your `.env` thresholds reproduces the same scores from recorded answers.

---

## Quick-start: Use the shipped defaults

If you don't have time for a full calibration:

1. Enable Ollaya with the `.env.example` values.
2. Run real queries through the system for 1–2 weeks.
3. Spot-check 5–10 results manually:
   - Are kept passages actually relevant?
   - Are injected passages being dropped?
4. If results look wrong, proceed to **Step 1** of this guide to recalibrate.

The shipped defaults are calibrated for code-search use cases and will not cause silent failures.

---

## References

- Configuration reference: [documentation/11-ollaya-judgment-layer.md — Section 6](11-ollaya-judgment-layer.md#6-configuration-reference-env)
- Ollaya API reference: [https://ollaya.dev/docs/api](https://ollaya.dev/docs/api)
- `.env.example`: Shipped defaults and documentation
