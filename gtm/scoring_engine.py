"""
GTM Validator — Deterministic Scoring & Pattern Detection Engine
================================================================
Implements the Option D hybrid recommendation architecture:

  Layer 1 (this file — pure Python, deterministic):
    - Opportunity scoring: how many overall score points each question is worth
    - Pattern detection:   named GTM failure modes from individual question scores
    - Context builder:     structured dict consumed by the constrained AI prompt

  Layer 2 (ai_services.py):
    - Gemini receives the structured context and writes industry-specific prose
    - If Gemini is unavailable, the pre-written root_cause/quick_win text renders instead

Same inputs always produce same outputs. No randomness, no API calls.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Pillar weights — must match CATEGORIES in load_gtm_defaults.py
# ---------------------------------------------------------------------------
PILLAR_WEIGHTS: dict[str, float] = {
    "Demand":     0.4,
    "Conversion": 0.4,
    "Delivery":   0.2,
}

# ---------------------------------------------------------------------------
# Per-question metadata
# Intra-pillar weights must sum to 1.0 within each pillar.
# ---------------------------------------------------------------------------
QUESTION_META: dict[str, dict] = {
    # ── Demand (Σ = 1.00) ───────────────────────────────────────────────────
    "DEM-ICP-01": {"pillar": "Demand",     "weight": 0.22, "dimension": "ICP Clarity"},
    "DEM-FIT-02": {"pillar": "Demand",     "weight": 0.16, "dimension": "Lead Quality"},
    "DEM-MSG-03": {"pillar": "Demand",     "weight": 0.18, "dimension": "Positioning"},
    "DEM-CHN-04": {"pillar": "Demand",     "weight": 0.20, "dimension": "Channel Strategy"},
    "DEM-ATT-05": {"pillar": "Demand",     "weight": 0.14, "dimension": "Attribution"},
    "DEM-CNT-06": {"pillar": "Demand",     "weight": 0.10, "dimension": "Execution Cadence"},
    # ── Conversion (Σ = 1.00) ───────────────────────────────────────────────
    "CON-SLA-01": {"pillar": "Conversion", "weight": 0.13, "dimension": "Speed to Lead"},
    "CON-QLF-02": {"pillar": "Conversion", "weight": 0.19, "dimension": "Qualification"},
    "CON-STG-03": {"pillar": "Conversion", "weight": 0.22, "dimension": "Pipeline Hygiene"},
    "CON-OBJ-04": {"pillar": "Conversion", "weight": 0.08, "dimension": "Deal Enablement"},
    "CON-WNL-05": {"pillar": "Conversion", "weight": 0.22, "dimension": "Win-Loss Learning"},
    "CON-PGE-06": {"pillar": "Conversion", "weight": 0.16, "dimension": "Funnel Optimization"},
    # ── Delivery (Σ = 1.00) ─────────────────────────────────────────────────
    "DEL-TTV-01": {"pillar": "Delivery",   "weight": 0.18, "dimension": "Time to Value"},
    "DEL-ONB-02": {"pillar": "Delivery",   "weight": 0.16, "dimension": "Onboarding Discipline"},
    "DEL-HLT-03": {"pillar": "Delivery",   "weight": 0.24, "dimension": "Health Monitoring"},
    "DEL-RET-04": {"pillar": "Delivery",   "weight": 0.22, "dimension": "Retention"},
    "DEL-QBR-05": {"pillar": "Delivery",   "weight": 0.12, "dimension": "Success Governance"},
    "DEL-ADV-06": {"pillar": "Delivery",   "weight": 0.08, "dimension": "Advocacy"},
}

# ---------------------------------------------------------------------------
# GTM failure pattern definitions
#
# Each pattern has:
#   name               — stable string key
#   severity           — Critical | High | Medium | Low
#   affected_dims      — list of dimension labels this pattern covers
#   headline           — one-line description shown in the UI
#   root_cause         — 2-3 sentence explanation (used as AI prompt input
#                        and as static fallback when AI is unavailable)
#   quick_win          — concrete first action (same dual use)
#   trigger(q, p)      — pure function; q = {id_code: score}, p = {pillar: avg}
#
# Patterns are evaluated in order; multiple can fire simultaneously.
# ---------------------------------------------------------------------------

def _answered_pillars(pillar_avgs: dict) -> list[float]:
    """Averages for pillars that actually have answers (avg > 0).

    Used to gate the all-pillar catch-all patterns: without this, an empty/
    unanswered assessment (every average 0) makes ``all(...)`` over an empty
    sequence return True, firing both the "all low" and "all high" patterns at once.
    """
    return [v for v in pillar_avgs.values() if v > 0]


_PATTERNS: list[dict] = [
    # ── Cross-pillar catch-alls (check first so they can be overridden) ─────
    {
        "name": "EARLY_STAGE_ALL_LOW",
        "severity": "Critical",
        "affected_dims": ["ICP Clarity", "Qualification", "Onboarding Discipline"],
        "headline": "No GTM foundation yet — basics must come before optimisation",
        "root_cause": (
            "All three pillars score below 2.5, which means the GTM motion is "
            "largely informal and ad-hoc. Adding more channels, tools, or headcount "
            "at this stage won't help — the foundation must come first."
        ),
        "quick_win": (
            "One action per pillar: (1) write a one-page ICP, "
            "(2) define 3 qualification questions for every lead, "
            "(3) create a 5-step onboarding checklist for new customers."
        ),
        "trigger": lambda _, p: bool(_answered_pillars(p)) and all(v < 2.5 for v in _answered_pillars(p)),
    },
    {
        "name": "SCALING_READY",
        "severity": "Low",
        "affected_dims": [],
        "headline": "Strong GTM foundations — ready to scale and accelerate",
        "root_cause": (
            "All three pillars score above 3.5, indicating solid execution across "
            "demand, conversion, and delivery. Focus should shift from fixing basics "
            "to compounding what's already working."
        ),
        "quick_win": (
            "Double investment in the one channel or motion with the highest proven ROI "
            "before diversifying. Build a systematic referral or advocacy programme "
            "from your healthiest customer segment."
        ),
        "trigger": lambda _, p: bool(_answered_pillars(p)) and all(v >= 3.5 for v in _answered_pillars(p)),
    },

    # ── Demand patterns ──────────────────────────────────────────────────────
    {
        "name": "ICP_UNDEFINED",
        "severity": "Critical",
        "affected_dims": ["ICP Clarity", "Lead Quality"],
        "headline": "Target customer is not clearly defined",
        "root_cause": (
            "When ICP Clarity or Lead Quality score low, demand generation is "
            "misaligned from the start. Marketing attracts the wrong audience, "
            "sales wastes cycles on poor-fit deals, and every downstream metric "
            "becomes harder to improve."
        ),
        "quick_win": (
            "Document a one-page ICP with 3–5 inclusion criteria and 2–3 hard "
            "exclusion signals. Enforce an ICP-fit field on lead capture forms and CRM."
        ),
        "trigger": lambda q, _: q.get("DEM-ICP-01", 5) <= 2 or q.get("DEM-FIT-02", 5) <= 2,
    },
    {
        "name": "MESSAGING_GAP",
        "severity": "High",
        "affected_dims": ["Positioning"],
        "headline": "Value proposition is not landing with buyers",
        "root_cause": (
            "Weak positioning means buyers can't quickly understand why to choose "
            "you over alternatives. This suppresses inbound conversion rates, "
            "increases sales cycle length, and makes every GTM motion less efficient."
        ),
        "quick_win": (
            "Create a segment message map: one-line value statement per target persona. "
            "Align homepage hero, outbound opener, and one-pager language to it."
        ),
        "trigger": lambda q, _: q.get("DEM-MSG-03", 5) <= 2,
    },
    {
        "name": "DEMAND_UNTRACKED",
        "severity": "High",
        "affected_dims": ["Attribution", "Channel Strategy"],
        "headline": "Can't tell which demand channels are working",
        "root_cause": (
            "Without attribution and channel ROI data, budget decisions are guesswork. "
            "This leads to over-investing in low-ROI channels and inadvertently cutting "
            "the ones actually driving pipeline."
        ),
        "quick_win": (
            "Standardise UTM parameters across all campaigns. Build one channel scorecard "
            "with spend, leads, qualified pipeline, and cost-per-SQL by source."
        ),
        "trigger": lambda q, _: q.get("DEM-ATT-05", 5) <= 2 or q.get("DEM-CHN-04", 5) <= 2,
    },
    {
        "name": "DEMAND_STARVED",
        "severity": "Critical",
        "affected_dims": ["ICP Clarity", "Positioning", "Channel Strategy", "Execution Cadence"],
        "headline": "Good process but insufficient pipeline feeding it",
        "root_cause": (
            "Conversion and Delivery processes are stronger than the demand engine "
            "feeding them. The constraint on growth is not how well leads convert — "
            "it's that not enough qualified demand is being generated in the first place."
        ),
        "quick_win": (
            "Commit to one repeatable weekly demand activity with a named owner and "
            "a target pipeline contribution. Don't diversify channels until one is proven."
        ),
        "trigger": lambda _, p: p.get("Demand", 5) < 2.5 and p.get("Conversion", 0) >= 3.0,
    },

    # ── Conversion patterns ──────────────────────────────────────────────────
    {
        "name": "CONVERSION_LEAK",
        "severity": "Critical",
        "affected_dims": ["Pipeline Hygiene", "Qualification", "Win-Loss Learning"],
        "headline": "Leads arrive but the funnel leaks — revenue left on the table",
        "root_cause": (
            "Demand generation is outperforming the conversion process. Leads are "
            "entering but not progressing to close. This is usually caused by weak "
            "qualification, unclear stage criteria, or slow follow-up."
        ),
        "quick_win": (
            "Identify the pipeline stage with the highest drop-off rate. Fix that "
            "stage's entry and exit criteria before addressing others."
        ),
        "trigger": lambda _, p: p.get("Demand", 0) >= 3.0 and p.get("Conversion", 5) < 2.5,
    },
    {
        "name": "QUALIFICATION_GAP",
        "severity": "High",
        "affected_dims": ["Qualification"],
        "headline": "Pipeline filled with unqualified deals",
        "root_cause": (
            "When qualification fields aren't enforced at stage transitions, reps "
            "move deals forward on optimism rather than evidence. This inflates "
            "pipeline, distorts forecasts, and wastes closing resources on deals "
            "that should have been disqualified early."
        ),
        "quick_win": (
            "Make 3–5 qualification fields mandatory before a deal can advance past "
            "discovery. Run a one-time audit of current open pipeline against these criteria."
        ),
        "trigger": lambda q, _: q.get("CON-QLF-02", 5) <= 2,
    },
    {
        "name": "PIPELINE_BLIND",
        "severity": "Critical",
        "affected_dims": ["Pipeline Hygiene", "Win-Loss Learning"],
        "headline": "No visibility into why deals win or lose",
        "root_cause": (
            "Without clear stage criteria and structured win/loss analysis, the same "
            "deal failures repeat indefinitely. Reps can't improve because no one knows "
            "what's causing losses, and forecasts are built on intuition rather than data."
        ),
        "quick_win": (
            "Define exit criteria for each pipeline stage. Add a mandatory closed-lost "
            "reason taxonomy with 5-7 categories. Run a monthly 30-minute win/loss retro."
        ),
        "trigger": lambda q, _: q.get("CON-STG-03", 5) <= 2 or q.get("CON-WNL-05", 5) <= 2,
    },
    {
        "name": "SLOW_FOLLOWUP",
        "severity": "High",
        "affected_dims": ["Speed to Lead"],
        "headline": "High-intent leads going cold before first contact",
        "root_cause": (
            "Lead conversion rates drop sharply when response time exceeds one hour. "
            "Without tracked SLAs by source, high-intent leads who are actively "
            "evaluating are being lost to faster-responding competitors."
        ),
        "quick_win": (
            "Set source-specific SLA targets (e.g. web form: 15 min, email: 2 hr). "
            "Add a daily breach alert and measure median first-response time per source."
        ),
        "trigger": lambda q, _: q.get("CON-SLA-01", 5) <= 2,
    },

    # ── Delivery patterns ────────────────────────────────────────────────────
    {
        "name": "DELIVERY_CHURN_RISK",
        "severity": "Critical",
        "affected_dims": ["Health Monitoring", "Retention"],
        "headline": "Customer health and retention are unmonitored — churn risk is hidden",
        "root_cause": (
            "When health signals and retention data aren't tracked, churn is only "
            "discovered at renewal — too late to intervene. Revenue leaks quietly "
            "through the back door while demand and conversion absorb all the attention."
        ),
        "quick_win": (
            "Define a simple red-amber-green health model using product usage and "
            "support ticket signals. Review the red list weekly and trigger a CS call "
            "for any account that turns red."
        ),
        "trigger": lambda q, _: q.get("DEL-RET-04", 5) <= 2 or q.get("DEL-HLT-03", 5) <= 2,
    },
    {
        "name": "TTV_DRAG",
        "severity": "High",
        "affected_dims": ["Time to Value", "Onboarding Discipline"],
        "headline": "Slow time to value — customers leave before seeing ROI",
        "root_cause": (
            "When time to value is long and onboarding lacks clear milestones, "
            "customers lose confidence before they are embedded. The first 90 days "
            "determine whether a customer becomes a long-term advocate or churns early."
        ),
        "quick_win": (
            "Define the single most important 'first value' moment per segment. "
            "Build a milestone checklist with owner and target date for every new account. "
            "Track time-to-value weekly and flag any account exceeding the segment benchmark."
        ),
        "trigger": lambda q, _: q.get("DEL-TTV-01", 5) <= 2 or q.get("DEL-ONB-02", 5) <= 2,
    },
]

_SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_pillar_avgs(question_scores: dict[str, int]) -> dict[str, float]:
    """
    Weighted average score (1–5) per pillar.
    Mirrors the formula in services._compute_scores so numbers are consistent.
    """
    sums:    dict[str, float] = {p: 0.0 for p in PILLAR_WEIGHTS}
    weights: dict[str, float] = {p: 0.0 for p in PILLAR_WEIGHTS}
    for q_id, score in question_scores.items():
        meta = QUESTION_META.get(q_id)
        if not meta:
            continue
        p, w = meta["pillar"], meta["weight"]
        sums[p]    += score * w
        weights[p] += w
    return {
        p: round(sums[p] / weights[p], 3) if weights[p] else 0.0
        for p in PILLAR_WEIGHTS
    }


def compute_opportunity_scores(question_scores: dict[str, int]) -> list[dict]:
    """
    For each answered question, compute how many overall score points are gained
    if it improves from its current score to the maximum (5).

    Formula derivation
    ------------------
    overall = Σ_pillars [ (pillar_avg / 5) × pillar_weight × 100 ]

    Since intra-pillar weights sum to 1.0, a single question's marginal contribution
    when its score increases by Δ is:

        Δ_overall = Δ × question_weight × pillar_weight × 20

    Returns a list sorted by opportunity_pts descending.
    """
    results = []
    for q_id, score in question_scores.items():
        meta = QUESTION_META.get(q_id)
        if not meta:
            continue
        delta = max(0, 5 - score)
        opp   = round(delta * meta["weight"] * PILLAR_WEIGHTS[meta["pillar"]] * 20, 2)
        results.append({
            "id_code":        q_id,
            "dimension":      meta["dimension"],
            "pillar":         meta["pillar"],
            "current_score":  score,
            "opportunity_pts": opp,
            "question_weight": meta["weight"],
            "pillar_weight":   PILLAR_WEIGHTS[meta["pillar"]],
        })
    return sorted(results, key=lambda x: -x["opportunity_pts"])


def detect_patterns(
    question_scores: dict[str, int],
    pillar_avgs:     dict[str, float],
) -> list[dict]:
    """
    Evaluate all pattern triggers and return the fired patterns ordered by severity.
    Multiple patterns can fire simultaneously.
    """
    fired = []
    for pat in _PATTERNS:
        try:
            if pat["trigger"](question_scores, pillar_avgs):
                fired.append({
                    "name":          pat["name"],
                    "severity":      pat["severity"],
                    "affected_dims": pat["affected_dims"],
                    "headline":      pat["headline"],
                    "root_cause":    pat["root_cause"],
                    "quick_win":     pat["quick_win"],
                })
        except Exception:
            pass
    return sorted(fired, key=lambda x: _SEVERITY_ORDER.get(x["severity"], 99))


def build_recommendation_context(question_scores: dict[str, int]) -> dict:
    """
    Main entry point.

    Returns a structured dict used by:
      - ai_services._build_prompt()  →  Gemini receives this as structured input
      - views.results()              →  template renders priority table + patterns
      - (future) pytest fixtures     →  deterministic assertions on Ardent SA scores
    """
    pillar_avgs   = compute_pillar_avgs(question_scores)
    opportunities = compute_opportunity_scores(question_scores)
    patterns      = detect_patterns(question_scores, pillar_avgs)
    top_3         = opportunities[:3]

    current_overall = round(
        sum(
            (pillar_avgs[p] / 5.0) * PILLAR_WEIGHTS[p] * 100
            for p in PILLAR_WEIGHTS
            if pillar_avgs[p] > 0
        ),
        1,
    )
    total_opportunity = round(sum(o["opportunity_pts"] for o in opportunities), 1)

    return {
        "pillar_avgs":         pillar_avgs,
        "current_overall":     current_overall,
        "top_priorities":      top_3,
        "all_opportunities":   opportunities,
        "patterns":            patterns,
        "total_opportunity_pts": total_opportunity,
    }
