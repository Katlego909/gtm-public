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
    ("Demand", 0.4),
    ("Conversion", 0.4),
    ("Delivery", 0.2),
]

# ────────────────────────────────────────────────────────────────────────────────
# 2) QUESTIONS
#    id_code is the stable key — do not reuse the same id_code for different ideas.
#    Each statement should be easy for a non-marketer to understand.
# ────────────────────────────────────────────────────────────────────────────────
QUESTIONS: Dict[str, List[Dict]] = {
    "Demand": [
        {
            "id_code": "D1",
            "text": "We clearly describe the type of customer we serve (industry, size, and buyer role).",
            "weight": 1.2,
            "diagnostic_note": "If this is unclear, it’s hard to focus efforts. Write it down in one paragraph so anyone on the team can repeat it.",
        },
        {
            "id_code": "D2",
            "text": "Our main message explains the problem we solve and the outcome customers get, in simple words.",
            "weight": 1.2,
            "diagnostic_note": "Aim for a short sentence a new visitor can understand in five seconds.",
        },
        {
            "id_code": "D3",
            "text": "We run ongoing marketing that regularly puts us in front of potential customers.",
            "weight": 1.0,
            "diagnostic_note": "Examples: a weekly post schedule, a small ad budget, a newsletter, or regular outreach.",
        },
        {
            "id_code": "D4",
            "text": "We can see where our leads come from (for example: search, social, ads, partners).",
            "weight": 1.0,
            "diagnostic_note": "Basic tracking helps you decide what to keep, stop, or improve.",
        },
        {
            "id_code": "D5",
            "text": "We bring in enough new visitors or inquiries to meet our goals.",
            "weight": 1.1,
            "diagnostic_note": "Set a monthly target and review it. If traffic is low, focus on a few consistent channels.",
        },
    ],
    "Conversion": [
        {
            "id_code": "C1",
            "text": "We reply to new leads quickly and set clear response time expectations.",
            "weight": 1.1,
            "diagnostic_note": "Fast replies often win deals. Even a friendly auto-reply with next steps helps.",
        },
        {
            "id_code": "C2",
            "text": "We use a simple, repeatable way to decide if a lead is a good fit.",
            "weight": 1.0,
            "diagnostic_note": "Pick 3–5 questions that define fit (budget, need, timing, decision maker) and use them every time.",
        },
        {
            "id_code": "C3",
            "text": "Our landing pages or forms turn a good share of visitors into inquiries or trials.",
            "weight": 1.1,
            "diagnostic_note": "Track your current rate and test small changes: headline, proof, layout, or shorter forms.",
        },
        {
            "id_code": "C4",
            "text": "We have sales materials ready (short deck, one-pager, a few short customer quotes).",
            "weight": 1.0,
            "diagnostic_note": "Keep them easy to find and up to date so everyone shares the same message.",
        },
        {
            "id_code": "C5",
            "text": "We record why we win or lose deals and use this to improve each month.",
            "weight": 1.0,
            "diagnostic_note": "Write down the top reasons and pick one small fix to try next month.",
        },
    ],
    "Delivery": [
        {
            "id_code": "L1",
            "text": "New customers get started smoothly with a simple, repeatable onboarding.",
            "weight": 1.0,
            "diagnostic_note": "List the steps, assign owners, and send a welcome email with what to expect.",
        },
        {
            "id_code": "L2",
            "text": "We ask customers for feedback (like a quick rating) and act on what we learn.",
            "weight": 1.0,
            "diagnostic_note": "A short survey after onboarding or after delivery is often enough to spot patterns.",
        },
        {
            "id_code": "L3",
            "text": "We have a clear way to keep customers coming back or renewing.",
            "weight": 1.0,
            "diagnostic_note": "Examples: a check-in schedule, a renewal reminder, or a small loyalty perk.",
        },
        {
            "id_code": "L4",
            "text": "We collect and share short customer quotes or reviews.",
            "weight": 1.0,
            "diagnostic_note": "Ask after a good outcome. Even one or two lines help future buyers trust you.",
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

def _upsert_questions(cat_map: Dict[str, Category], dry: bool) -> UpsertCounts:
    counts = UpsertCounts()
    for cat_name, items in QUESTIONS.items():
        category = cat_map[cat_name]
        for row in items:
            defaults = {
                "category": category,
                "text": row["text"],
                "weight": row.get("weight", 1.0),
                "diagnostic_note": row.get("diagnostic_note", ""),
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
