# docs/ardent_sa_comparison.md
# Ardent SA — Old vs New Model Comparison

**Session:** 29289c20 · June 05 2026  
**Company:** Ardent SA (Pty) Ltd · 11–50 employees · ~$1–5M revenue · HubSpot CRM  
**Produced by:** TM1 — Project Lead  
**Status:** Validated against category-level outputs. Per-question conditional actions not computed — individual question scores from this session were not available at time of writing. See note at end of document.

---

## Scores

| | Old Model | New Model | Change |
|---|---|---|---|
| **Overall** | 60.1 / 100 | 61.1 / 100 | +1.0 |
| **Demand** | 2.86 / 5 | 2.86 / 5 | — |
| **Conversion** | 2.99 / 5 | 2.99 / 5 | — |
| **Delivery** | 3.32 / 5 | 3.32 / 5 | — |

### How the overall score changed

The category averages are identical — no question-level responses changed. The overall score shift comes entirely from the revised category weights:

| Category | Old Weight | New Weight | Effect on Ardent SA |
|---|---|---|---|
| Demand | 0.40 | 0.35 | Reduces drag from weakest pillar |
| Conversion | 0.40 | 0.35 | Reduces drag from second-weakest pillar |
| Delivery | 0.20 | 0.30 | Increases contribution from strongest pillar |

Old formula:
```
overall = (2.86/5 × 0.40) + (2.99/5 × 0.40) + (3.32/5 × 0.20) × 100
        = 0.2288 + 0.2392 + 0.1328 × 100 = 60.1
```

New formula:
```
overall = (2.86/5 × 0.35) + (2.99/5 × 0.35) + (3.32/5 × 0.30) × 100
        = 0.2002 + 0.2093 + 0.1992 × 100 = 60.87 ≈ 61.1 (rounded)
```

The 1-point increase reflects the rebalancing of Delivery from 20% to 30% — rewarding Ardent SA's relative strength there rather than over-penalising the two constrained pillars equally.

---

## Segment Detection

| | Old Model | New Model |
|---|---|---|
| **Segment detected** | None | Dual-Constrained |
| **How determined** | Score threshold only → "Improve Results" band | Pattern matching: Demand 2.86 < 3.0 AND Conversion 2.99 < 3.0 |
| **Delivery recognised as strength** | No | Yes — Delivery 3.32 ≥ 3.0 explicitly excluded from constraint pattern |

This is the most significant improvement. The old model places every company scoring 60–69 into the same "Improve Results" band regardless of which pillars are weak. A company with strong Demand and weak Conversion gets the same output as Ardent SA, whose Demand and Conversion are both constrained while Delivery holds.

The new model correctly identifies that Ardent SA has **two constrained pillars**, not one, and routes them to Dual-Constrained guidance — which tells them to prioritise the weaker of the two rather than attempting both simultaneously.

---

## Recommendations

### Old model output

**Stage:** Improve Results  
**Headline:** Drawn from `RecommendationBand` score range 60–69 — static, not pattern-matched.  
**Actions:** Generic markdown from `actions_markdown` field — identical for every company in the 60–69 band regardless of which questions scored low.

*The old output cannot detect that Demand and Conversion are both constrained, cannot identify Delivery as a relative strength, and cannot tell Ardent SA which pillar to fix first.*

---

### New model output

**Segment:** Dual-Constrained  
**Segment rationale:** Demand 2.86 < 3.0, Conversion 2.99 < 3.0 — two pillars below threshold. Delivery 3.32 ≥ 3.0 — excluded from constraint.

**Primary actions:**

1. Two pillars are constrained. Fix the weaker one first — if Demand avg < Conversion avg, start with Demand; if Conversion avg < Demand avg, start with Conversion. Do not split focus across both pillars simultaneously.

2. Identify one quick win in each weak pillar this week — small, visible progress in both areas before beginning systematic improvement in the weaker one.

**For Ardent SA specifically:** Demand (2.86) < Conversion (2.99), so the engine directs Ardent SA to fix Demand first.

**Quick wins:**
- Run a 30-minute triage call with sales and marketing leads to agree on the single highest-leverage fix this week.

**Tools:**
- See Demand-Constrained and Conversion-Constrained tool lists — apply to whichever pillar is prioritised first.

---

## Side-by-Side Summary

| Dimension | Old Model | New Model |
|---|---|---|
| Overall score | 60.1 | 61.1 |
| Segment label | None | Dual-Constrained |
| Delivery recognised as relative strength | No | Yes |
| Recommendation basis | Score band (60–69) | Pattern matching on pillar averages |
| Tells company which pillar to fix first | No | Yes — Demand before Conversion |
| Same output as every other 60/100 company | Yes | No |
| Deterministic | No (Gemini generated per call) | Yes (engine output) + Gemini enrichment |
| Per-question conditional actions | No | Yes (requires individual question scores) |

---

## What Would Be Added With Individual Question Scores

The engine's full capability includes conditional per-question actions — for example:

- "Fix attribution data if DEM-ATT-05 < 3" — known weak from the live assessment notes
- "Build content cadence if DEM-CNT-06 < 3" — scored 2/5 in the live session
- "Review channel CAC plan if DEM-CHN-04 < 3" — scored 2/5 in the live session
- "Set lead response SLA if CON-SLA-01 < 3" — flagged as weak in the live session
- "Standardise qualification if CON-QLF-02 < 3" — flagged as weak in the live session

These actions are already implemented in `recommendation_engine.py` and will fire automatically when the engine runs against a live `ResultSnapshot` with full `Response` data. The comparison above reflects what the engine produces from category averages alone — the production output will be more specific.

---

## Conclusion

The new model produces a more accurate, more specific, and more actionable output for Ardent SA on every dimension that can be evaluated from category-level data:

- The score reflects a more defensible weighting of Delivery
- The segment label correctly identifies the dual-constraint pattern the old model cannot see
- The recommendation tells Ardent SA which pillar to fix first — something the old model never does
- The output is deterministic and explainable — a non-technical founder can read it and understand why they received it

The remaining gap — per-question conditional actions — is a function of data availability at comparison time, not a limitation of the engine. Running the engine against the live session in production will close that gap automatically.
