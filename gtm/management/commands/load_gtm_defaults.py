# gtm/management/commands/load_gtm_defaults.py
"""
Load the default Go-To-Market (GTM) definitions: categories, questions, score bands,
and simple tool suggestions.

Goals
-----
- Keep wording simple and clear. Avoid jargon and unexplained abbreviations.
- Keep the structure identical to what the app expects, so views/templates continue to work.
- Safe to run many times (idempotent). Uses update_or_create.
- Optional --dry-run flag to preview without changing the database.

What this adds or updates
-------------------------
1) Categories   — Three areas we measure: Demand, Conversion, Delivery.
2) Questions    — Short statements people rate from 1 to 5.
3) Score Bands  — Ranges that map the final score to a stage and a helpful write-up.
4) Tool Hints   — Light suggestions of tools people can look into.

Usage
-----
$ python manage.py load_gtm_defaults
$ python manage.py load_gtm_defaults --dry-run
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from django.core.management.base import BaseCommand
from django.db import transaction

from gtm.models import (
    Category,
    Question,
    RecommendationBand,
    ToolRecommendation,
)

# ────────────────────────────────────────────────────────────────────────────────
# 1) CATEGORIES
#    Keep names the same so templates and views continue to match.
#    Weights help indicate relative importance in the overall score.
# ────────────────────────────────────────────────────────────────────────────────
CATEGORIES: List[Tuple[str, float]] = [
    ("Demand", 0.35),
    ("Conversion", 0.35),
    ("Delivery", 0.3),
]

# ────────────────────────────────────────────────────────────────────────────────
# 2) QUESTIONS
#    id_code is a stable semantic key (e.g., DEM-ICP-01, CON-SLA-01, DEL-TTV-01).
#    Keep these ids stable over time to preserve analytics continuity.
# ────────────────────────────────────────────────────────────────────────────────
QUESTIONS: Dict[str, List[Dict]] = {
    "Demand": [
        {
            "id_code": "DEM-ICP-01",
            "text": "We have a simple written profile of our ideal customer, including who is a strong fit and who is not, and we review it every quarter so all teams stay aligned.",
            "weight": 1.25,
            "diagnostic_note": "If this is unclear, teams chase more leads instead of the right leads.",
            "ai_metadata": {
                "pillar": "Demand",
                "dimension": "ICP Clarity",
                "question_type": "process",
                "evidence_type": "qualitative",
                "time_horizon": "90d",
                "owner_role": "marketing",
                "maturity_stage": "foundation",
                "quick_win_if_low": "Define ICP inclusion/exclusion criteria and publish one-page version for all revenue teams.",
            },
        },
        {
            "id_code": "DEM-FIT-02",
            "text": "At least 6 out of 10 new inbound leads match our ideal customer profile, and we can verify this with clear qualification fields captured at lead creation in our CRM.",
            "weight": 1.2,
            "diagnostic_note": "If lead fit is low, spend goes up and conversion goes down.",
            "ai_metadata": {
                "pillar": "Demand",
                "dimension": "Lead Quality",
                "question_type": "outcome",
                "evidence_type": "quantitative",
                "time_horizon": "90d",
                "owner_role": "revops",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Add ICP-fit fields to lead capture and review weekly fit-rate by source.",
            },
        },
        {
            "id_code": "DEM-MSG-03",
            "text": "Our core value message is easy to understand, tested with each target segment, and used consistently across our website, outbound messages, and sales materials.",
            "weight": 1.15,
            "diagnostic_note": "If the message is inconsistent, fewer buyers move forward.",
            "ai_metadata": {
                "pillar": "Demand",
                "dimension": "Positioning",
                "question_type": "diagnostic",
                "evidence_type": "qualitative",
                "time_horizon": "current",
                "owner_role": "marketing",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Create a segment message map and align homepage hero, outbound opener, and one-pager language.",
            },
        },
        {
            "id_code": "DEM-CHN-04",
            "text": "We have a clear channel plan that shows expected customer acquisition cost and pipeline contribution for each channel, and we review performance every month.",
            "weight": 1.1,
            "diagnostic_note": "Without this, channel spend becomes guesswork.",
            "ai_metadata": {
                "pillar": "Demand",
                "dimension": "Channel Strategy",
                "question_type": "process",
                "evidence_type": "system-data",
                "time_horizon": "30d",
                "owner_role": "revops",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Define one scorecard with spend, leads, qualified pipeline, and CAC by channel.",
            },
        },
        {
            "id_code": "DEM-ATT-05",
            "text": "We can clearly see which channel first touched a lead and which channels later influenced the deal in attribution reporting, we trust.",
            "weight": 1.1,
            "diagnostic_note": "Without clear attribution, it is hard to know where to invest.",
            "ai_metadata": {
                "pillar": "Demand",
                "dimension": "Attribution",
                "question_type": "evidence",
                "evidence_type": "system-data",
                "time_horizon": "90d",
                "owner_role": "revops",
                "maturity_stage": "optimized",
                "quick_win_if_low": "Standardize UTM governance and enforce campaign-source completeness in CRM.",
            },
        },
        {
            "id_code": "DEM-CNT-06",
            "text": "We run content or outbound work on a regular schedule, not randomly, and each activity is tied to target accounts and clear pipeline goals.",
            "weight": 1.1,
            "diagnostic_note": "If execution is irregular, pipeline becomes harder to predict.",
            "ai_metadata": {
                "pillar": "Demand",
                "dimension": "Execution Cadence",
                "question_type": "process",
                "evidence_type": "quantitative",
                "time_horizon": "30d",
                "owner_role": "marketing",
                "maturity_stage": "foundation",
                "quick_win_if_low": "Commit to a weekly campaign cadence with one KPI owner per motion.",
            },
        },
    ],
    "Conversion": [
        {
            "id_code": "CON-SLA-01",
            "text": "We set clear response-time targets for new leads by source, and we check every week how quickly reps reply so high-intent leads are not lost.",
            "weight": 1.2,
            "diagnostic_note": "Slow replies reduce meetings and close rates.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Speed to Lead",
                "question_type": "outcome",
                "evidence_type": "quantitative",
                "time_horizon": "30d",
                "owner_role": "sales",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Set source-based SLA targets and trigger alerts for breaches.",
            },
        },
        {
            "id_code": "CON-QLF-02",
            "text": "We use one shared sales-stage qualification checklist, and required CRM fields must be completed before an opportunity advances to the next pipeline stage, distinct from initial inbound lead capture.",
            "weight": 1.15,
            "diagnostic_note": "Weak qualification fills pipeline with poor-fit deals.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Qualification",
                "question_type": "process",
                "evidence_type": "system-data",
                "time_horizon": "current",
                "owner_role": "revops",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Make qualification fields mandatory at stage transition and audit weekly.",
            },
        },
        {
            "id_code": "CON-STG-03",
            "text": "Each pipeline stage has clear entry and exit rules, and sales leaders review stage-to-stage conversion rates monthly to spot bottlenecks early.",
            "weight": 1.1,
            "diagnostic_note": "If stage rules are unclear, forecasts become unreliable.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Pipeline Hygiene",
                "question_type": "evidence",
                "evidence_type": "system-data",
                "time_horizon": "30d",
                "owner_role": "sales",
                "maturity_stage": "optimized",
                "quick_win_if_low": "Define stage exit criteria and report stage-to-stage conversion by segment.",
            },
        },
        {
            "id_code": "CON-OBJ-04",
            "text": "We keep a simple playbook of common buyer objections and competitor risks, and managers coach the team on it so responses stay consistent.",
            "weight": 1.1,
            "diagnostic_note": "Without this, reps answer objections inconsistently.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Deal Enablement",
                "question_type": "diagnostic",
                "evidence_type": "qualitative",
                "time_horizon": "90d",
                "owner_role": "sales",
                "maturity_stage": "foundation",
                "quick_win_if_low": "Document top five objections with approved responses and examples.",
            },
        },
        {
            "id_code": "CON-WNL-05",
            "text": "We capture why deals are won or lost in structured fields, then use those patterns in a monthly review to run focused improvement experiments.",
            "weight": 1.1,
            "diagnostic_note": "If win/loss reasons are not tracked, the same problems repeat.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Win-Loss Learning",
                "question_type": "evidence",
                "evidence_type": "system-data",
                "time_horizon": "30d",
                "owner_role": "revops",
                "maturity_stage": "optimized",
                "quick_win_if_low": "Add mandatory closed-lost reason taxonomy and run a monthly improvement retro.",
            },
        },
        {
            "id_code": "CON-PGE-06",
            "text": "We run regular tests on key conversion pages and forms, using clear hypotheses and success metrics, and we track whether results improve.",
            "weight": 1.0,
            "diagnostic_note": "Without regular tests, conversion pages get stale.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Funnel Optimization",
                "question_type": "process",
                "evidence_type": "quantitative",
                "time_horizon": "90d",
                "owner_role": "marketing",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Launch one monthly A/B test on a high-traffic conversion page.",
            },
        },
        {
            "id_code": "CON-HND-07",
            "text": "Marketing and sales agree on formal handoff rules — including when a lead is sales-ready, who owns it, and response-time expectations — and we review compliance monthly.",
            "weight": 1.10,
            "diagnostic_note": "Without a clear handoff definition, leads are passed too early or too late, wasting rep time and losing high-intent buyers.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "Sales-Marketing Handoff",
                "question_type": "process",
                "evidence_type": "system-data",
                "time_horizon": "30d",
                "owner_role": "revops",
                "maturity_stage": "foundation",
                "quick_win_if_low": "Write a one-paragraph MQL definition, get sign-off from both marketing and sales, and add it as a required CRM field before lead transfer.",
            },
        },
        {   
            "id_code": "CON-CRM-07",
            "text": "Our CRM data is complete and trustworthy: required fields for ICP fit, source, and stage are enforced, and we audit data quality monthly so reporting and qualification decisions are reliable.",
            "weight": 1.10,
            "diagnostic_note": "Incomplete CRM data makes lead scoring, attribution, pipeline reporting, and qualification unreliable — corrupting several other GTM metrics at once.",
            "ai_metadata": {
                "pillar": "Conversion",
                "dimension": "RevOps Data Hygiene",
                "question_type": "evidence",
                "evidence_type": "system-data",
                "time_horizon": "30d",
                "owner_role": "revops",
                "maturity_stage": "foundation",
                "quick_win_if_low": "Run a CRM completeness report on open deals — identify the three most-missed fields and make them mandatory before a deal can advance.",
            },
        },
    ],  
    "Delivery": [
        {
            "id_code": "DEL-TTV-01",
            "text": "We measure and report how long it takes new customers to reach their first documented value milestone, by segment, and we set reduction targets we review monthly.",
            "weight": 1.15,
            "diagnostic_note": "If first value takes too long, churn risk rises.",
            "ai_metadata": {
                "pillar": "Delivery",
                "dimension": "Time to Value",
                "question_type": "outcome",
                "evidence_type": "quantitative",
                "time_horizon": "30d",
                "owner_role": "cs",
                "maturity_stage": "optimized",
                "quick_win_if_low": "Define TTV milestone events and create a weekly exception report.",
            },
        },
        {
            "id_code": "DEL-ONB-02",
            "text": "Our onboarding process has documented milestones, assigned owners, and realistic completion targets tracked in one shared system — independent of whether we yet track time-to-value metrics.",
            "weight": 1.1,
            "diagnostic_note": "Poor onboarding slows activation and increases support load.",
            "ai_metadata": {
                "pillar": "Delivery",
                "dimension": "Onboarding Discipline",
                "question_type": "process",
                "evidence_type": "system-data",
                "time_horizon": "current",
                "owner_role": "cs",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Publish milestone checklist with owner and due date for every new account.",
            },
        },
        {
            "id_code": "DEL-HLT-03",
            "text": "We score customer health using product usage, engagement, and support signals, and we use clear playbooks to act early on at-risk accounts.",
            "weight": 1.15,
            "diagnostic_note": "Without health signals, risk is found too late.",
            "ai_metadata": {
                "pillar": "Delivery",
                "dimension": "Health Monitoring",
                "question_type": "evidence",
                "evidence_type": "system-data",
                "time_horizon": "30d",
                "owner_role": "cs",
                "maturity_stage": "optimized",
                "quick_win_if_low": "Define a simple red-amber-green health model and trigger follow-up tasks automatically.",
            },
        },
        {
            "id_code": "DEL-RET-04",
            "text": "We review gross and net retention by cohort every month, and we launch targeted actions quickly when any segment starts to decline.",
            "weight": 1.2,
            "diagnostic_note": "If retention drops go unseen, growth slows quietly.",
            "ai_metadata": {
                "pillar": "Delivery",
                "dimension": "Retention",
                "question_type": "outcome",
                "evidence_type": "quantitative",
                "time_horizon": "90d",
                "owner_role": "revops",
                "maturity_stage": "optimized",
                "quick_win_if_low": "Track retention by segment and launch targeted save motions for highest-risk cohort.",
            },
        },
        {
            "id_code": "DEL-QBR-05",
            "text": "Our high-value customers receive regular business reviews focused on outcomes, roadmap alignment, and practical expansion opportunities.",
            "weight": 1.1,
            "diagnostic_note": "Without regular reviews, expansion opportunities are missed.",
            "ai_metadata": {
                "pillar": "Delivery",
                "dimension": "Success Governance",
                "question_type": "process",
                "evidence_type": "qualitative",
                "time_horizon": "90d",
                "owner_role": "cs",
                "maturity_stage": "repeatable",
                "quick_win_if_low": "Establish quarterly review cadence for top-tier accounts with defined agenda.",
            },
        },
        {
            "id_code": "DEL-ADV-06",
            "text": "After customers achieve clear value, we consistently capture proof points like quotes, case studies, and references to support future selling.",
            "weight": 1.05,
            "diagnostic_note": "If proof points are not captured, future buyers trust you less.",
            "ai_metadata": {
                "pillar": "Delivery",
                "dimension": "Advocacy",
                "question_type": "evidence",
                "evidence_type": "qualitative",
                "time_horizon": "30d",
                "owner_role": "marketing",
                "maturity_stage": "foundation",
                "quick_win_if_low": "Trigger testimonial request at successful milestone completion.",
            },
        },
    ],
}

# ────────────────────────────────────────────────────────────────────────────────
# 3) SCORE BANDS
#    Keep headings "Action Plan:" and "Recommended Tools:" — your templates rely on them.
# ────────────────────────────────────────────────────────────────────────────────
BANDS: List[Tuple[float, float, str, str, str]] = [
    (
        0, 39, "Foundations", "Put the basics in place",
        """Action Plan:
- Write down who your customers are and the main problem you solve.
- Update your homepage or main page so the benefit is obvious.
- Start one small, steady marketing activity (for example: weekly posts or a small ad).

Recommended Tools:
- A CRM to track contacts and deals.
- A simple website builder or editor to update pages quickly.
- A basic analytics tool to see where visitors come from."""
    ),
    (
        40, 54, "Process & Response", "Make responses and steps consistent",
        """Action Plan:
- Set a goal for how fast you reply to new leads, and meet it.
- Use the same few fit questions for every lead so decisions are fair and quick.
- Write down the steps from first contact to closing a deal so the team follows the same path.

Recommended Tools:
- Calendar or booking links to speed up meeting scheduling.
- Shared docs for checklists and sales materials.
- A helpdesk or shared inbox if multiple people reply to leads."""
    ),
    (
        55, 69, "Improve Results", "Tidy up weak spots and measure changes",
        """Action Plan:
- Track simple weekly numbers (new leads, qualified leads, wins).
- Test one small change at a time on your landing page or form.
- Make sure you can tell which channels bring the best leads.

Recommended Tools:
- Website analytics and form analytics.
- A/B testing tool for headlines and layouts.
- A spreadsheet or dashboard to review weekly numbers."""
    ),
    (
        70, 84, "Grow with What Works", "Do more of the proven steps",
        """Action Plan:
- Repeat the few channels that already bring good leads.
- Try a simple online event, partner mention, or referral idea.
- Add a short outreach routine with a clear script and target list.

Recommended Tools:
- Contact data tools to build a careful, relevant list.
- Email tools for outreach with reminders.
- Light personalization tools for your website or emails."""
    ),
    (
        85, 100, "Build Strength", "Turn good results into long-term advantage",
        """Action Plan:
- Create something people remember you for (helpful guides, small community, or events).
- Share useful studies, tips, or checklists in your space.
- Connect with partners who serve the same customers so you both grow.

Recommended Tools:
- Community or group tools (forums or chat).
- Newsletter or blog tools to share updates.
- Automation tools to connect your forms, emails, and docs."""
    ),
]

# ────────────────────────────────────────────────────────────────────────────────
# 4) TOOL RECOMMENDATIONS
#    These are lightweight hints. Keep category + keyword stable.
# ────────────────────────────────────────────────────────────────────────────────
TOOL_RECS: List[Tuple[str, str, str, str, str]] = [
    ("Demand", "customer-type", "Write down who you serve so everyone is aligned.", "Any CRM or shared doc", ""),
    ("Demand", "message", "Explain the problem you solve and the outcome in plain words.", "Website editor, shared doc", ""),
    ("Demand", "regular-marketing", "Keep a steady activity that brings attention every week.", "Social scheduler, email tool, simple ad platform", ""),
    ("Demand", "source-tracking", "Know which places bring you leads.", "Basic analytics or link tracking", ""),
    ("Demand", "traffic", "Increase useful visits with consistent content and simple promotion.", "Blog tool, newsletter tool", ""),

    ("Conversion", "fast-reply", "Reply to leads quickly and set expectations.", "Shared inbox, autoresponder, booking links", ""),
    ("Conversion", "fit-check", "Use the same 3–5 questions to judge fit.", "CRM fields or shared form", ""),
    ("Conversion", "page-improve", "Test small page changes to lift sign-ups or inquiries.", "Form tool, A/B test tool", ""),
    ("Conversion", "sales-kit", "Keep a short deck, one-pager, and a few quotes ready.", "Slides, docs, shared drive", ""),
    ("Conversion", "reasons", "Track top reasons for wins and losses and review monthly.", "CRM notes or simple spreadsheet", ""),

    ("Delivery", "onboarding", "Make the first steps smooth and predictable.", "Checklist tool or project tracker", ""),
    ("Delivery", "feedback", "Collect quick ratings and act on patterns.", "Short survey tool", ""),
    ("Delivery", "renew", "Have a simple plan for renewals or repeat purchases.", "Calendar reminders or CRM tasks", ""),
    ("Delivery", "reviews", "Ask for short quotes when things go well.", "Form link or review site", ""),
]

# Legacy short IDs kept for backward-compatibility migration.
LEGACY_ID_CODE_MAP: Dict[str, str] = {
    "D1": "DEM-ICP-01",
    "D2": "DEM-MSG-03",
    "D3": "DEM-CNT-06",
    "D4": "DEM-ATT-05",
    "D5": "DEM-CHN-04",
    "C1": "CON-SLA-01",
    "C2": "CON-QLF-02",
    "C3": "CON-PGE-06",
    "C4": "CON-OBJ-04",
    "C5": "CON-WNL-05",
    "L1": "DEL-ONB-02",
    "L2": "DEL-HLT-03",
    "L3": "DEL-RET-04",
    "L4": "DEL-ADV-06",
}

# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────
@dataclass
class UpsertCounts:
    created: int = 0
    updated: int = 0

    def bump(self, created: bool):
        if created:
            self.created += 1
        else:
            self.updated += 1

def _ensure_categories() -> Dict[str, Category]:
    """Create/update categories and return a map by name."""
    out: Dict[str, Category] = {}
    for name, weight in CATEGORIES:
        obj, _ = Category.objects.update_or_create(name=name, defaults={"weight": weight})
        out[name] = obj
    return out


def _derive_question_ai_metadata(cat_name: str, row: Dict) -> Dict[str, str]:
    """Generate lightweight AI metadata so prompts can remain structured and stable."""
    intent_map = {
        "Demand": "Validate predictable demand generation and audience-message clarity.",
        "Conversion": "Validate lead handling discipline and conversion process repeatability.",
        "Delivery": "Validate post-sale execution quality, retention, and social proof capture.",
    }
    note = (row.get("diagnostic_note") or "").strip()
    fallback_quick_win = "Define one repeatable weekly action, assign ownership, and review outcomes."
    return {
        "intent": intent_map.get(cat_name, "Validate GTM execution quality for this area."),
        "evidence_hint": f"Look for concrete process evidence related to question {row.get('id_code', '')}.",
        "risk_if_low": note or "Low maturity in this area can reduce pipeline quality and execution consistency.",
        "quick_win_if_low": note or fallback_quick_win,
    }


def _migrate_legacy_question_ids(dry: bool) -> None:
    """Rename legacy question IDs in place to semantic IDs without breaking existing FKs."""
    if dry:
        return

    for legacy_id, semantic_id in LEGACY_ID_CODE_MAP.items():
        legacy_q = Question.objects.filter(id_code=legacy_id).first()
        if not legacy_q:
            continue
        # If semantic ID already exists, keep both rows untouched to avoid collisions.
        if Question.objects.filter(id_code=semantic_id).exists():
            continue
        legacy_q.id_code = semantic_id
        legacy_q.save(update_fields=["id_code"])

def _upsert_questions(cat_map: Dict[str, Category], dry: bool) -> UpsertCounts:
    counts = UpsertCounts()
    for cat_name, items in QUESTIONS.items():
        category = cat_map[cat_name]
        for row in items:
            ai_metadata = row.get("ai_metadata") or _derive_question_ai_metadata(cat_name, row)
            defaults = {
                "category": category,
                "text": row["text"],
                "weight": row.get("weight", 1.0),
                "diagnostic_note": row.get("diagnostic_note", ""),
                "ai_metadata": ai_metadata,
            }
            if dry:
                counts.updated += 1
            else:
                _, created = Question.objects.update_or_create(
                    id_code=row["id_code"],
                    defaults=defaults,
                )
                counts.bump(created)
    return counts

def _upsert_bands(dry: bool) -> UpsertCounts:
    counts = UpsertCounts()
    for min_s, max_s, stage, headline, md in BANDS:
        defaults = {"stage": stage, "headline": headline, "actions_markdown": md}
        if dry:
            counts.updated += 1
        else:
            _, created = RecommendationBand.objects.update_or_create(
                min_score=min_s, max_score=max_s, defaults=defaults
            )
            counts.bump(created)
    return counts

def _upsert_tools(cat_map: Dict[str, Category], dry: bool) -> UpsertCounts:
    counts = UpsertCounts()
    for cat_name, keyword, description, tools_csv, url in TOOL_RECS:
        defaults = {"description": description, "tools": tools_csv, "url": (url or None)}
        if dry:
            counts.updated += 1
        else:
            _, created = ToolRecommendation.objects.update_or_create(
                category=cat_map[cat_name],
                keyword=keyword,
                defaults=defaults,
            )
            counts.bump(created)
    return counts

def _print_summary(cmd: BaseCommand, title: str, counts: UpsertCounts):
    cmd.stdout.write(f"  {title:<22} created: {counts.created:>2} • updated: {counts.updated:>2}")

# ────────────────────────────────────────────────────────────────────────────────
# Management Command
# ────────────────────────────────────────────────────────────────────────────────
class Command(BaseCommand):
    help = "Load or update GTM defaults (categories, questions, score bands, and tool hints)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without writing to the database.",
        )

    def handle(self, *args, **opts):
        dry = bool(opts.get("dry_run"))
        mode = "DRY-RUN (no writes)" if dry else "APPLY"

        self.stdout.write(self.style.MIGRATE_HEADING(f"Loading GTM defaults — {mode}"))
        if dry:
            self.stdout.write(self.style.WARNING("No database changes will be made."))

        cat_map = _ensure_categories()
        _migrate_legacy_question_ids(dry=dry)

        if dry:
            q_counts = _upsert_questions(cat_map, dry=True)
            b_counts = _upsert_bands(dry=True)
            t_counts = _upsert_tools(cat_map, dry=True)
        else:
            with transaction.atomic():
                q_counts = _upsert_questions(cat_map, dry=False)
                b_counts = _upsert_bands(dry=False)
                t_counts = _upsert_tools(cat_map, dry=False)

        self.stdout.write(self.style.HTTP_INFO("\nSummary"))
        _print_summary(self, "Categories (ensure)", UpsertCounts(created=0, updated=len(CATEGORIES)))
        _print_summary(self, "Questions", q_counts)
        _print_summary(self, "Score bands", b_counts)
        _print_summary(self, "Tool hints", t_counts)

        if dry:
            self.stdout.write(self.style.WARNING("\nℹ️  Dry-run complete. Re-run without --dry-run to apply."))
        else:
            self.stdout.write(self.style.SUCCESS("\n✅ Defaults loaded successfully."))
