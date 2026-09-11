---
name: calib
description: "Use when the user asks for calibration results."
version: 1.0.0
author: Hermes
license: MIT
platforms: [linux, macos, windows]
---

# calib — is the fund's model honest?

The calibration loop grades every prediction the fund makes against the real
oracle outcome, **traded or not**. It is the only evidence we have about whether
the model works, because the fund may take no trades for weeks.

## Procedure

Run exactly this:

```bash
python3 /root/fund/calibration.py --score
```

`--score` first grades any markets that have newly resolved, then prints the
report. Present the numbers, then **always** give the two interpretation
sections below (Meaning, Implications). Do not just paste the output, and do not
recompute anything by hand.

## What the question actually is

> When the model says 0.20, does it happen 20% of the time?

That is all. Calibration measures the FORECASTER alone. It says nothing about
whether a trade is profitable — see "What this does NOT tell you".

## Reading the output

| field | meaning |
|---|---|
| `distinct markets graded` | the real sample size. Use THIS, not the row count. |
| `graded rows` | every graded prediction; correlated, see below |
| `Brier` | mean squared error of the probabilities. Lower is better. |
| `baseline` | 0.25 = what you get by always saying 0.50 |
| `skill` | `1 − Brier/baseline`. **Positive = better than a coin flip.** |
| `reliability` | bucketed predicted vs realized frequency — where the model lies |
| `pending` | predictions recorded but not yet resolved |

### The two samples — trust only one

A market that lives three days is recorded once per 12-hour bucket, so it can
contribute **six graded rows that all share one outcome**. That correlates the
observations and makes the evidence look far stronger than it is.

- **all predictions** — more data, correlated. Indicative only.
- **one per market** — one row per distinct market (the last forecast before
  settlement). **Independent. This is the headline number.**

If the two disagree materially, trust `one per market`, and quote
`distinct markets graded` as the sample size — never the row count.

## Meaning and implications — the decision ladder

Read the verdict line, then explain what it implies for the fund:

| Verdict | Meaning | Implication |
|---|---|---|
| **NOT ENOUGH EVIDENCE** (<30 distinct markets) | sample too small to conclude anything | say so plainly; do not speculate about the model's quality either way |
| **MODEL FAILS** (skill ≤ 0) | probabilities are no better than a coin flip | `p_trade` is unusable. The fund must not trade on it. The Forecaster needs rework, not the exits. |
| **MARGINAL** (skill 0.00–0.05) | real but thin signal | cannot overcome a 3–9% cost hurdle. Expect continued no-trades; do not loosen the hurdle to force activity. |
| **MODEL HOLDS** (skill > 0.05) | probabilities carry genuine information | *then* check the reliability table before trusting position size |

### Reliability — and what to do about each finding

The gap is `realized − predicted` per bucket.

- **Negative gap (overconfident):** the model says 0.85 and it happens 0.70.
  The fund is overpaying in that bucket, i.e. it buys edges that do not exist.
  → Raise the EV hurdle for that bucket, or shrink `p_trade` toward 1.0.
- **Positive gap (underconfident):** the model says 0.60 and it happens 0.75.
  The fund is declining trades it should take.
  → That bucket's hurdle can be relaxed — with evidence, not hope.
- **The tails matter most.** This fund buys cheap contracts whose real
  probability is small but nonzero. Systematic overconfidence in the 0.0–0.3
  buckets is the single most dangerous finding possible here, because that is
  precisely where every position lives.
- **A good Brier score with a bad tail bucket is still a bad model for this
  fund.** Say so explicitly if the output shows it.

## What this does NOT tell you

- **Not** whether the 3% EV cost hurdle is correct. Calibration and the cost
  hurdle are independent questions; a perfectly calibrated model can still have
  no trade worth taking after fees.
- **Not** a profit forecast. Skill > 0 is not money.
- **Not** a random sample. We only record markets that already passed the
  Scout's liquidity/horizon/spread filter, so this grades the model on
  **liquid, longer-dated markets** — which is the useful question ("is it honest
  where we would actually trade?"), but it is not a general vol-model scorecard.

## Rules

- If `distinct markets graded` is under 30, the ONLY correct answer is that
  there is not enough evidence yet. Never characterise the model on a small
  sample, however good or bad the numbers look.
- Quote the **one per market** skill as the headline figure.
- Never claim or imply profit. This skill reports model honesty, nothing else.
- Keep the numbers in a code block so columns stay aligned, and keep prose
  tight — the interpretation matters more than the table.
- Related: skill `table` (the trade book and P&L), skill `prediction-fund-ops`
  (fund operations, models, limitations).
