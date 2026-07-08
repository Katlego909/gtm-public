# gtm/recommendation_engine.py
"""
GTM Recommendation Engine
--------------------------
Deterministic, pattern-based recommendation logic for the Funti3r GTM Validator.

Design principles
-----------------
- Zero AI calls. Identical inputs always produce identical outputs.
- Reads category averages from ResultSnapshot.category_breakdown AND individual
  question scores via Response objects to detect the company's specific constraint
  pattern.
- Output is a plain dict consumed by gtm/views.py results view and passed as
  context to the Gemini prompt in gtm/ai_services.py (Gemini enriches, not
  generates the core recommendation).

Wire-in (views.py results view)
--------------------------------
    from .recommendation_engine import get_recommendations
    ...
    engine_output = get_recommendations(snap)   # call before rendering
    ...
    return render(request, "gtm/results.html", {
        ...
        "engine_output": engine_output,
    })

Wire-in (ai_services.py _build_prompt)
---------------------------------------
    Pass engine_output["segment_label"] and engine_output["primary_actions"]
    as context in the Gemini prompt so it enriches rather than generates.

Question weight register (from load_gtm_defaults.py + TM5 Week 2 audit)
------------------------------------------------------------------------
Pillar weights: Demand 0.35 · Conversion 0.35 · Delivery 0.30
NOTE: load_gtm_defaults.py currently seeds Demand 0.4 / Conversion 0.4 / Delivery 0.2.
      The engine reads live category averages from the snapshot, so pillar weights
      do not affect segment detection here — thresholds are applied to raw 1–5 averages.

Two new questions added in TM5 audit (CON-HND-07, CON-CRM-07) are handled
gracefully — if absent from the DB the engine skips their actions silently.

CHANGELOG
---------
- Fixed: companies with no pillar below WEAK_THRESHOLD but not all pillars at/above
  MATURE_THRESHOLD were being silently bucketed into SEGMENT_GTM_MATURE, producing
  identical "scale what's working" advice for genuinely different maturity levels
  (e.g. a 77.5-score company and an 88.5-score company both got the exact same
  four bullet points). These companies now get their own segment,
  SEGMENT_SOLID_FOUNDATION, with actions derived from their actual weakest pillar's
  question data instead of a static list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Segment detection thresholds (raw 1–5 scale)
STRONG_THRESHOLD = 3.0   # avg >= this → pillar is "not the primary constraint"
MATURE_THRESHOLD = 4.0   # avg >= this for ALL pillars → GTM Mature
WEAK_THRESHOLD   = 3.0   # avg < this → pillar is constrained

# Score threshold for BroadlyWeak (maps to overall 0–100 scale)
BROADLY_WEAK_SCORE_THRESHOLD = 60.0

# Threshold used only for Solid Foundation companies: since none of their pillars
# are "weak" (< 3.0), we look for room to improve using a higher bar so the
# recommendations still feel earned rather than generic.
SOLID_FOUNDATION_ACTION_THRESHOLD = 4

# Segment labels — stable strings consumed by templates and the Gemini prompt
SEGMENT_DEMAND_CONSTRAINED     = "Demand-Constrained"
SEGMENT_CONVERSION_CONSTRAINED = "Conversion-Constrained"
SEGMENT_DELIVERY_CONSTRAINED   = "Delivery-Constrained"
SEGMENT_DUAL_CONSTRAINED       = "Dual-Constrained"
SEGMENT_BROADLY_WEAK           = "Broadly Weak"
SEGMENT_SOLID_FOUNDATION       = "Solid Foundation"
SEGMENT_GTM_MATURE             = "GTM Mature"


# ─────────────────────────────────────────────────────────────────────────────
# ACTION CATALOGUE
# Keys are id_codes from load_gtm_defaults.py (stable across DB updates).
# Each entry: (threshold_to_trigger, action_text, quick_win_text)
# threshold_to_trigger: score strictly less than this activates the action.
# ─────────────────────────────────────────────────────────────────────────────

# Threshold below which a per-question action fires
Q_ACTION_THRESHOLD = 3

@dataclass
class QuestionAction:
    id_code: str
    threshold: int          # score < threshold → action fires
    action: str             # primary action text
    quick_win: str          # fast, concrete quick-win


# Demand actions
DEMAND_ACTIONS: List[QuestionAction] = [
    QuestionAction(
        id_code="DEM-ICP-01",
        threshold=3,
        action="Define or refresh your Ideal Customer Profile — document inclusion/exclusion criteria and publish a one-page version for all revenue teams.",
        quick_win="Book a 90-minute ICP workshop with sales, marketing, and CS this week. Agree on the top three firmographic filters.",
    ),
    QuestionAction(
        id_code="DEM-CHN-04",
        threshold=3,
        action="Build a channel scorecard that shows expected CAC and pipeline contribution per channel, and review it monthly.",
        quick_win="Create a one-row-per-channel spreadsheet with spend, leads, and qualified pipeline for the last 30 days.",
    ),
    QuestionAction(
        id_code="DEM-ATT-05",
        threshold=3,
        action="Fix attribution data — standardise UTM governance and enforce campaign-source completeness in your CRM.",
        quick_win="Audit the last 50 leads: flag every one missing a source tag, fix the top two sources causing gaps.",
    ),
    QuestionAction(
        id_code="DEM-CNT-06",
        threshold=3,
        action="Commit to a weekly campaign cadence with clear pipeline targets — assign one KPI owner per motion.",
        quick_win="Block a recurring two-hour slot this week for campaign execution. Publish a simple four-week content calendar.",
    ),
    QuestionAction(
        id_code="DEM-FIT-02",
        threshold=3,
        action="Add ICP-fit fields to your lead capture forms and review the weekly fit-rate by source.",
        quick_win="Add three firmographic fields to your primary lead form and review last month's inbound leads for fit.",
    ),
    QuestionAction(
        id_code="DEM-MSG-03",
        threshold=3,
        action="Create a segment message map and align homepage hero, outbound opener, and sales one-pager language.",
        quick_win="Rewrite your homepage hero headline to lead with the customer outcome, not the product feature.",
    ),
]

# Conversion actions
CONVERSION_ACTIONS: List[QuestionAction] = [
    QuestionAction(
        id_code="CON-SLA-01",
        threshold=3,
        action="Set source-based lead response SLA targets and trigger CRM alerts for any breach.",
        quick_win="Set a 1-hour response SLA for web leads and add a CRM task auto-created on every new inbound lead.",
    ),
    QuestionAction(
        id_code="CON-QLF-02",
        threshold=3,
        action="Standardise qualification — make required CRM fields mandatory at stage transition and audit weekly.",
        quick_win="Make three qualification fields (budget, authority, timeline) mandatory before an opportunity moves to Stage 2.",
    ),
    QuestionAction(
        id_code="CON-PGE-06",
        threshold=3,
        action="Launch a CRO testing programme — run one A/B test per month on a high-traffic conversion page with a clear hypothesis.",
        quick_win="Identify your highest-traffic landing page and set up one headline A/B test using any free testing tool.",
    ),
    QuestionAction(
        id_code="CON-WNL-05",
        threshold=3,
        action="Implement structured win-loss tracking — add a mandatory closed-lost reason taxonomy in CRM and run a monthly improvement retro.",
        quick_win="Add five closed-lost reason options to your CRM deal record and make them required before closing a deal lost.",
    ),
    QuestionAction(
        id_code="CON-STG-03",
        threshold=3,
        action="Define clear stage entry and exit criteria and report stage-to-stage conversion rates monthly to spot bottlenecks.",
        quick_win="Document exit criteria for your top two pipeline stages and review stage conversion in your next sales team call.",
    ),
    QuestionAction(
        id_code="CON-OBJ-04",
        threshold=3,
        action="Build a deal enablement playbook — document the top five objections with approved responses and run coaching sessions.",
        quick_win="Collect the three most common objections from your last five lost deals. Write one approved response per objection.",
    ),
    # New questions added by TM5 — handled gracefully if absent from DB
    QuestionAction(
        id_code="CON-HND-07",
        threshold=3,
        action="Define and document your marketing-to-sales handoff criteria — when exactly does a lead become sales-qualified?",
        quick_win="Write a one-paragraph MQL definition and share it with both marketing and sales this week.",
    ),
    QuestionAction(
        id_code="CON-CRM-07",
        threshold=3,
        action="Clean your CRM data — audit key fields for completeness and establish a weekly data hygiene review.",
        quick_win="Run a CRM report showing percentage of open deals missing company size, industry, or deal value. Fix the top 20.",
    ),
]

# Delivery actions
DELIVERY_ACTIONS: List[QuestionAction] = [
    QuestionAction(
        id_code="DEL-TTV-01",
        threshold=3,
        action="Define a time-to-first-value milestone by segment and create a weekly exception report for accounts exceeding the target.",
        quick_win="Identify the single most common milestone that marks 'first value' for your core customer segment. Start tracking it.",
    ),
    QuestionAction(
        id_code="DEL-HLT-03",
        threshold=3,
        action="Build a simple customer health scoring model — define red/amber/green thresholds and trigger follow-up tasks automatically.",
        quick_win="Score your existing accounts today using three signals: last login date, support tickets (last 30 days), NPS score.",
    ),
    QuestionAction(
        id_code="DEL-RET-04",
        threshold=3,
        action="Review gross and net retention by cohort monthly and launch targeted save motions for your highest-risk segment.",
        quick_win="Pull a retention cohort report for the last two quarters. Identify the cohort with the highest churn and schedule a review.",
    ),
    QuestionAction(
        id_code="DEL-QBR-05",
        threshold=3,
        action="Establish a formal QBR cadence for high-value accounts — define a standard agenda focused on outcomes, roadmap, and expansion.",
        quick_win="Identify your top five accounts by ARR. Schedule a 45-minute business review with each in the next 60 days.",
    ),
    QuestionAction(
        id_code="DEL-ONB-02",
        threshold=3,
        action="Publish an onboarding milestone checklist with clear owners and due dates for every new account.",
        quick_win="Create a five-step onboarding checklist for your most common customer type and assign an owner to each step.",
    ),
    QuestionAction(
        id_code="DEL-ADV-06",
        threshold=3,
        action="Build a systematic advocacy capture process — trigger a testimonial or case study request at every successful milestone completion.",
        quick_win="Email your three happiest customers this week asking for a two-sentence quote you can use on your website.",
    ),
]

# Index all actions by id_code for fast lookup
ALL_ACTIONS: Dict[str, QuestionAction] = {
    a.id_code: a
    for a in DEMAND_ACTIONS + CONVERSION_ACTIONS + DELIVERY_ACTIONS
}

# Pillar name → action catalogue, used by both Solid Foundation and Dual/Broadly Weak logic
PILLAR_CATALOGUE: Dict[str, List[QuestionAction]] = {
    "Demand":     DEMAND_ACTIONS,
    "Conversion": CONVERSION_ACTIONS,
    "Delivery":   DELIVERY_ACTIONS,
}

# Segment-level toolkit suggestions (tools list is informational, not product endorsements)
SEGMENT_TOOLS: Dict[str, List[str]] = {
    SEGMENT_DEMAND_CONSTRAINED: [
        "HubSpot or Salesforce (CRM) — ICP-fit fields, lead source attribution",
        "Google Analytics / UTM builder — channel attribution",
        "Notion / Confluence — ICP one-pager and message map",
        "SEMrush / Ahrefs — channel performance benchmarking",
    ],
    SEGMENT_CONVERSION_CONSTRAINED: [
        "HubSpot / Salesforce — stage-gate enforcement, SLA alerts",
        "Gong / Chorus — call recording for objection pattern analysis",
        "Hotjar / VWO — conversion-rate testing on key pages",
        "Clozd / Wynter — structured win-loss capture",
    ],
    SEGMENT_DELIVERY_CONSTRAINED: [
        "Gainsight / ChurnZero / Totango — customer health scoring",
        "Asana / Monday.com — onboarding milestone tracking",
        "Mixpanel / Amplitude — product usage signals for health scoring",
        "Delighted / Medallia — NPS and CSAT capture",
    ],
    SEGMENT_DUAL_CONSTRAINED: [
        "HubSpot / Salesforce — unified pipeline and attribution",
        "Asana / Notion — onboarding and campaign planning",
        "Hotjar — page-level conversion diagnosis",
    ],
    SEGMENT_BROADLY_WEAK: [
        "A simple CRM (HubSpot Starter) — contacts, deals, activity",
        "Google Analytics — traffic and conversion baseline",
        "Notion / Google Docs — ICP one-pager, playbooks",
        "Calendly — speed-to-lead improvement",
    ],
    SEGMENT_SOLID_FOUNDATION: [
        "HubSpot / Salesforce — tighten reporting on your weakest pillar",
        "Notion / Confluence — document the playbooks that are currently informal",
        "Google Analytics / Mixpanel — add measurement where it's still missing",
    ],
    SEGMENT_GTM_MATURE: [
        "Marketing automation (Marketo / HubSpot Enterprise) — advanced nurture",
        "ABM platform (6sense / Demandbase) — account-based demand",
        "Revenue intelligence (Gong Forecast / Clari) — predictive forecasting",
        "Advocacy platform (Influitive / ReferenceEdge) — reference and case study programmes",
    ],
}

# Segment descriptions shown in the Explainability Report
SEGMENT_DESCRIPTIONS: Dict[str, str] = {
    SEGMENT_DEMAND_CONSTRAINED: (
        "Your pipeline is the primary bottleneck. Conversion and Delivery are relatively healthy "
        "but demand generation is not producing enough qualified pipeline to drive growth."
    ),
    SEGMENT_CONVERSION_CONSTRAINED: (
        "You are generating leads but losing too many before they close. "
        "Demand and Delivery are relatively healthy — the gap is in how leads are handled, "
        "qualified, and converted."
    ),
    SEGMENT_DELIVERY_CONSTRAINED: (
        "You are winning customers but struggling to retain and expand them. "
        "Demand and Conversion are relatively healthy — the constraint is post-sale execution."
    ),
    SEGMENT_DUAL_CONSTRAINED: (
        "Two GTM pillars are underperforming simultaneously. Focus on quick wins in both "
        "weak areas before attempting to optimise. Fix the weakest pillar first."
    ),
    SEGMENT_BROADLY_WEAK: (
        "All three GTM pillars need attention. Start with the foundations: a clear ICP, "
        "one lead response SLA, and basic onboarding milestones. Fix the single weakest "
        "category first before spreading effort."
    ),
    SEGMENT_SOLID_FOUNDATION: (
        "No pillar is at risk, but you're not yet excellent across the board either. "
        "You have a solid, functioning GTM motion — the opportunity is to tighten your "
        "single weakest pillar so it stops capping your overall performance."
    ),
    SEGMENT_GTM_MATURE: (
        "Your GTM foundations are strong across all three pillars. "
        "The opportunity is to scale what is already working, invest in brand, "
        "and build durable competitive advantages."
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# RESULT DATACLASS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EngineOutput:
    """
    Return type of get_recommendations().
    All fields are plain Python types — safe to serialise and pass to templates.
    """
    segment_label: str
    segment_description: str
    primary_actions: List[str]          # ordered list of action strings
    quick_wins: List[str]               # parallel list of quick-win strings
    triggered_question_codes: List[str] # which id_codes fired (for explainability)
    tools: List[str]
    demand_avg: float
    conversion_avg: float
    delivery_avg: float
    overall_score: float
    weakest_pillar: str                 # "Demand" | "Conversion" | "Delivery" | ""

    def as_dict(self) -> dict:
        return {
            "segment_label":             self.segment_label,
            "segment_description":       self.segment_description,
            "primary_actions":           self.primary_actions,
            "quick_wins":                self.quick_wins,
            "triggered_question_codes":  self.triggered_question_codes,
            "tools":                     self.tools,
            "demand_avg":                self.demand_avg,
            "conversion_avg":            self.conversion_avg,
            "delivery_avg":              self.delivery_avg,
            "overall_score":             self.overall_score,
            "weakest_pillar":            self.weakest_pillar,
        }


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _extract_category_avgs(snapshot) -> Tuple[float, float, float]:
    """
    Pull Demand / Conversion / Delivery averages from ResultSnapshot.category_breakdown.

    category_breakdown is a list of dicts:
        [{"category": "Demand", "avg": 2.86}, {"category": "Conversion", "avg": 2.99}, ...]

    Returns (demand_avg, conversion_avg, delivery_avg).
    Falls back to 0.0 for any pillar not found.
    """
    breakdown = getattr(snapshot, "category_breakdown", None) or []
    avgs: Dict[str, float] = {}
    for entry in breakdown:
        name = (entry.get("category") or "").strip()
        avg  = float(entry.get("avg") or 0.0)
        avgs[name] = avg

    return (
        avgs.get("Demand",     0.0),
        avgs.get("Conversion", 0.0),
        avgs.get("Delivery",   0.0),
    )


def _fetch_question_scores(snapshot) -> Dict[str, int]:
    """
    Return a mapping of {id_code: score} for every Response in this snapshot's session.
    Uses a single DB query. Returns empty dict on any error.
    """
    try:
        from .models import Response
        responses = (
            Response.objects
            .filter(session=snapshot.session)
            .select_related("question")
            .values_list("question__id_code", "score")
        )
        return {id_code: score for id_code, score in responses}
    except Exception as exc:
        logger.warning("recommendation_engine: could not fetch question scores: %s", exc)
        return {}


def _detect_segment(
    demand: float,
    conversion: float,
    delivery: float,
    overall: float,
) -> str:
    """
    Pure function — maps pillar averages to a segment label.
    Order of checks matters: more specific checks come first.
    """
    # GTM Mature — all pillars genuinely strong
    if demand >= MATURE_THRESHOLD and conversion >= MATURE_THRESHOLD and delivery >= MATURE_THRESHOLD:
        return SEGMENT_GTM_MATURE

    # Broadly Weak — all pillars constrained AND overall score below threshold
    all_weak = (
        demand     < WEAK_THRESHOLD and
        conversion < WEAK_THRESHOLD and
        delivery   < WEAK_THRESHOLD
    )
    if all_weak and overall < BROADLY_WEAK_SCORE_THRESHOLD:
        return SEGMENT_BROADLY_WEAK

    # Count weak pillars
    weak_pillars = [
        p for p, avg in [("Demand", demand), ("Conversion", conversion), ("Delivery", delivery)]
        if avg < WEAK_THRESHOLD
    ]

    if len(weak_pillars) == 0:
        # No pillar is at risk, but not every pillar cleared the "mature" bar either.
        # This used to fall through to SEGMENT_GTM_MATURE, which incorrectly gave
        # companies with room to grow the same "scale what's working" advice as
        # genuinely excellent companies. They get their own segment instead.
        return SEGMENT_SOLID_FOUNDATION

    if len(weak_pillars) >= 2:
        return SEGMENT_DUAL_CONSTRAINED

    # Single constrained pillar
    constrained = weak_pillars[0]
    if constrained == "Demand":
        return SEGMENT_DEMAND_CONSTRAINED
    if constrained == "Conversion":
        return SEGMENT_CONVERSION_CONSTRAINED
    return SEGMENT_DELIVERY_CONSTRAINED


def _build_solid_foundation_actions(
    question_scores: Dict[str, int],
    demand: float,
    conversion: float,
    delivery: float,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Actions for SEGMENT_SOLID_FOUNDATION.

    No pillar is "weak" (< WEAK_THRESHOLD), so we can't use the normal
    score < 3 trigger — everything would come back empty. Instead we look at
    the single weakest pillar (by raw average) and surface its specific
    question-level gaps using a raised threshold (< 4), so two companies in
    this segment with different weak spots get genuinely different advice.
    """
    pillar_avgs = [("Demand", demand), ("Conversion", conversion), ("Delivery", delivery)]
    pillar_avgs.sort(key=lambda x: x[1])  # weakest first
    weakest_pillar_name, weakest_avg = pillar_avgs[0]
    second_pillar_name, _ = pillar_avgs[1]

    actions: List[str] = []
    quick_wins: List[str] = []
    codes: List[str] = []

    # Pull up to 2 specific gaps from the weakest pillar, 1 from the second-weakest,
    # using the raised threshold so it reflects "good but not excellent" rather than "at risk".
    for pillar_name, limit in [(weakest_pillar_name, 2), (second_pillar_name, 1)]:
        count = 0
        for qa in PILLAR_CATALOGUE[pillar_name]:
            if count >= limit:
                break
            score = question_scores.get(qa.id_code)
            if score is None:
                continue  # question not seeded in DB — skip gracefully
            if score < SOLID_FOUNDATION_ACTION_THRESHOLD:
                actions.append(qa.action)
                quick_wins.append(qa.quick_win)
                codes.append(qa.id_code)
                count += 1

    # Lead with a framing action naming the actual weakest pillar, so the
    # recommendation is specific even before the per-question detail.
    actions.insert(
        0,
        f"Your {weakest_pillar_name.lower()} pillar ({weakest_avg:.2f}/5) is the one holding back "
        f"your overall score — it's not a risk area, but it's your biggest opportunity for a quick lift."
    )
    quick_wins.insert(
        0,
        f"Pick the single lowest-scoring question in {weakest_pillar_name} and fix just that one thing this month."
    )

    # Fallback if every question in the weakest pillars scored >= the raised threshold
    # (can happen if the company is right on the edge of GTM Mature).
    if len(actions) == 1:
        actions.append(
            "Document what's currently working informally in your weakest pillar so it survives "
            "team changes and can be repeated reliably."
        )
        quick_wins.append(
            "Write a one-page playbook for your weakest pillar's current process, even if it's already working."
        )

    return actions, quick_wins, codes


def _build_actions_for_segment(
    segment: str,
    question_scores: Dict[str, int],
    demand: float,
    conversion: float,
    delivery: float,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Select and order the action list for a given segment.

    Returns (primary_actions, quick_wins, triggered_codes).
    - For single-pillar segments: actions from that pillar where question score < threshold.
    - For DualConstrained: fix weakest pillar first, add top action from second weak pillar.
    - For BroadlyWeak: one foundational action per pillar, in order of weakness.
    - For SolidFoundation: specific gaps from the weakest pillar, using a raised threshold.
    - For GTMMature: scaling / expansion actions (not question-gated).
    """
    if segment == SEGMENT_GTM_MATURE:
        actions = [
            "Scale your highest-performing demand channels — increase budget and test adjacent audiences.",
            "Explore new market segments or geographies using your existing GTM motion.",
            "Build a formal customer advocacy programme — references, case studies, and referral incentives.",
            "Invest in brand: sponsor industry events, publish thought leadership, and build community.",
        ]
        quick_wins = [
            "Identify your top-converting channel from last quarter and increase its budget by 20% this month.",
            "List three adjacent customer segments you have won incidentally — assess whether any merit a dedicated motion.",
            "Contact your five most vocal customers and ask one to be a reference account.",
            "Draft a 30-day content plan for one thought leadership topic your team is uniquely positioned to own.",
        ]
        return actions, quick_wins, []

    if segment == SEGMENT_SOLID_FOUNDATION:
        return _build_solid_foundation_actions(question_scores, demand, conversion, delivery)

    if segment == SEGMENT_BROADLY_WEAK:
        # Order pillars by weakness (worst first), pick one foundational action per pillar
        pillar_order = sorted(
            [("Demand", demand), ("Conversion", conversion), ("Delivery", delivery)],
            key=lambda x: x[1]
        )
        actions, quick_wins, codes = [], [], []
        pillar_action_map = {
            "Demand":     ("DEM-ICP-01", "DEM-CNT-06"),
            "Conversion": ("CON-SLA-01", "CON-QLF-02"),
            "Delivery":   ("DEL-ONB-02", "DEL-TTV-01"),
        }
        for pillar_name, _ in pillar_order:
            for code in pillar_action_map[pillar_name]:
                qa = ALL_ACTIONS.get(code)
                if qa and question_scores.get(code, 0) < qa.threshold:
                    actions.append(qa.action)
                    quick_wins.append(qa.quick_win)
                    codes.append(code)
                    break  # one per pillar
        # Prepend the universal foundation tip
        actions.insert(0, "Start with foundations: write an ICP one-pager, set one lead response SLA, and define onboarding milestones. Fix the single weakest category first.")
        quick_wins.insert(0, "Pick one metric per pillar to track weekly — even a spreadsheet beats nothing.")
        return actions, quick_wins, codes

    if segment == SEGMENT_DUAL_CONSTRAINED:
        # Determine which two pillars are weak; fix the weaker one first
        weak = sorted(
            [(p, a) for p, a in [("Demand", demand), ("Conversion", conversion), ("Delivery", delivery)]
             if a < WEAK_THRESHOLD],
            key=lambda x: x[1]
        )
        actions, quick_wins, codes = [], [], []
        pillar_catalogue = PILLAR_CATALOGUE
        # Up to 3 actions from weakest pillar, up to 2 from second weak pillar
        limits = [3, 2]
        for (pillar_name, _), limit in zip(weak, limits):
            count = 0
            for qa in pillar_catalogue[pillar_name]:
                if count >= limit:
                    break
                score = question_scores.get(qa.id_code)
                if score is None:
                    continue  # question not in DB — skip gracefully
                if score < qa.threshold:
                    actions.append(qa.action)
                    quick_wins.append(qa.quick_win)
                    codes.append(qa.id_code)
                    count += 1
        return actions, quick_wins, codes

    # Single-pillar segments
    catalogue_map = {
        SEGMENT_DEMAND_CONSTRAINED:     DEMAND_ACTIONS,
        SEGMENT_CONVERSION_CONSTRAINED: CONVERSION_ACTIONS,
        SEGMENT_DELIVERY_CONSTRAINED:   DELIVERY_ACTIONS,
    }
    catalogue = catalogue_map.get(segment, [])
    actions, quick_wins, codes = [], [], []
    for qa in catalogue:
        score = question_scores.get(qa.id_code)
        if score is None:
            continue  # question not in DB (e.g. new CON-HND-07 not seeded yet)
        if score < qa.threshold:
            actions.append(qa.action)
            quick_wins.append(qa.quick_win)
            codes.append(qa.id_code)

    return actions, quick_wins, codes


def _weakest_pillar(demand: float, conversion: float, delivery: float) -> str:
    pillar_avgs = {"Demand": demand, "Conversion": conversion, "Delivery": delivery}
    return min(pillar_avgs, key=pillar_avgs.get)


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def get_recommendations(snapshot) -> dict:
    """
    Main entry point.

    Parameters
    ----------
    snapshot : ResultSnapshot
        The saved ResultSnapshot for the completed assessment session.
        Must have: category_breakdown, overall, session (with responses).

    Returns
    -------
    dict
        EngineOutput.as_dict() — safe to pass directly to a Django template
        context or to the Gemini prompt builder.

    Guarantees
    ----------
    - Never raises. Returns a safe fallback dict on any error.
    - Zero AI calls.
    - Deterministic: identical snapshot always produces identical output.
    """
    try:
        demand, conversion, delivery = _extract_category_avgs(snapshot)
        overall = float(getattr(snapshot, "overall", 0.0) or 0.0)
        question_scores = _fetch_question_scores(snapshot)

        segment = _detect_segment(demand, conversion, delivery, overall)
        primary_actions, quick_wins, triggered_codes = _build_actions_for_segment(
            segment, question_scores, demand, conversion, delivery
        )

        return EngineOutput(
            segment_label            = segment,
            segment_description      = SEGMENT_DESCRIPTIONS[segment],
            primary_actions          = primary_actions,
            quick_wins               = quick_wins,
            triggered_question_codes = triggered_codes,
            tools                    = SEGMENT_TOOLS.get(segment, []),
            demand_avg               = round(demand, 2),
            conversion_avg           = round(conversion, 2),
            delivery_avg             = round(delivery, 2),
            overall_score            = round(overall, 1),
            weakest_pillar           = _weakest_pillar(demand, conversion, delivery),
        ).as_dict()

    except Exception as exc:
        logger.error("recommendation_engine.get_recommendations failed: %s", exc, exc_info=True)
        return _safe_fallback(snapshot)


def _safe_fallback(snapshot) -> dict:
    """Return a minimal safe output when the engine errors."""
    return EngineOutput(
        segment_label            = SEGMENT_BROADLY_WEAK,
        segment_description      = SEGMENT_DESCRIPTIONS[SEGMENT_BROADLY_WEAK],
        primary_actions          = [
            "Review your GTM score with your team and identify the single most important area to improve.",
        ],
        quick_wins               = ["Schedule a 30-minute GTM review with your team this week."],
        triggered_question_codes = [],
        tools                    = SEGMENT_TOOLS[SEGMENT_BROADLY_WEAK],
        demand_avg               = 0.0,
        conversion_avg           = 0.0,
        delivery_avg             = 0.0,
        overall_score            = float(getattr(snapshot, "overall", 0.0) or 0.0),
        weakest_pillar           = "Demand",
    ).as_dict()
