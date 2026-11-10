"""
A small fixed test set for the AI's written output.

Run `python manage.py run_ai_evals` before changing GEMINI_MODEL or editing
the diagnostic or playbook prompts. It sends the same five fictional
companies through the real prompt builders and the real model, then checks
the output against rules that can be checked without a second AI:

Diagnostic insights (lowest, middle and highest scored question per company)
  hard: not empty, names the company, no emoji, ends on a full sentence
  soft: 2-5 sentences; the raw model output already avoids advice (the
        regex failsafe should rarely have to strip anything)
Playbook
  hard: valid JSON, has a playbook, names the company, covers every one of
        the scoring engine's top-3 priorities (Layer 2 must not invent its
        own diagnosis), no emoji
  soft: risk_status is High/Medium/Low

Any hard failure makes the command exit non-zero. Soft results are reported
for judgement. All outputs are saved to a JSON file so two runs (old model vs
new, old prompt vs new) can be read side by side.

Cases are written inside a transaction that is always rolled back, so a run
leaves the database unchanged. It needs the seeded questions
(`load_gtm_defaults`) and costs roughly 60-90K tokens.
"""
import json
import re
from dataclasses import dataclass, field

from django.db import transaction

from .models import AssessmentSession, Question, Response
from .scoring_engine import QUESTION_META, build_recommendation_context

# Profiles: score per question id_code; missing ids fall back to the pillar default.
CASES = [
    {
        "key": "saas_growth_conversion_leak",
        "session": {"company_name": "Northwind Analytics", "industry": "B2B SaaS", "company_size": "51-200",
                    "revenue_range": "$10-50M", "country": "United States", "crm": "HubSpot", "company_stage": "growth"},
        "pillar_default": {"Demand": 4, "Conversion": 2, "Delivery": 3},
        "overrides": {"CON-STG-03": 1, "DEM-ICP-01": 5},
    },
    {
        "key": "logistics_early_weak_demand",
        "session": {"company_name": "Kopano Logistics", "industry": "Logistics software", "company_size": "11-50",
                    "revenue_range": "$1-10M", "country": "South Africa", "crm": "Pipedrive", "company_stage": "early_revenue"},
        "pillar_default": {"Demand": 2, "Conversion": 3, "Delivery": 3},
        "overrides": {"DEM-ICP-01": 1, "DEM-ATT-05": 1},
    },
    {
        "key": "healthtech_scale_delivery_churn",
        "session": {"company_name": "Fjord Health", "industry": "Healthcare technology", "company_size": "201-500",
                    "revenue_range": ">$50M", "country": "Norway", "crm": "Salesforce", "company_stage": "scale"},
        "pillar_default": {"Demand": 4, "Conversion": 4, "Delivery": 2},
        "overrides": {"DEL-HLT-03": 1},
    },
    {
        "key": "agency_tiny_all_low",
        "session": {"company_name": "Brightline Studio", "industry": "Creative agency services", "company_size": "1-10",
                    "revenue_range": "<$1M", "country": "United Kingdom", "crm": "", "company_stage": "early_revenue"},
        "pillar_default": {"Demand": 2, "Conversion": 1, "Delivery": 2},
        "overrides": {},
    },
    {
        "key": "fintech_mixed_high_variance",
        "session": {"company_name": "Atlas Payments", "industry": "Fintech", "company_size": "51-200",
                    "revenue_range": "$10-50M", "country": "Singapore", "crm": "Salesforce", "company_stage": "growth"},
        "pillar_default": {"Demand": 3, "Conversion": 3, "Delivery": 3},
        "overrides": {"DEM-ICP-01": 5, "DEM-CHN-04": 1, "CON-QLF-02": 5, "CON-SLA-01": 1},
    },
]

EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
# Not "suggest": in a diagnosis it usually means "indicates" ("this suggests immaturity").
ADVICE_WORDS = re.compile(r"\b(should|must|need to|needs to|recommend\w*)\b", re.IGNORECASE)
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def case_scores(case):
    overrides = case["overrides"]
    return {qid: overrides.get(qid, case["pillar_default"][meta["pillar"]]) for qid, meta in QUESTION_META.items()}


@dataclass
class Check:
    name: str
    passed: bool
    hard: bool
    detail: str = ""


@dataclass
class Result:
    case: str
    kind: str          # "insight" or "playbook"
    target: str        # question id_code, or "playbook"
    raw: str
    output: str
    checks: list = field(default_factory=list)

    @property
    def hard_failures(self):
        return [c for c in self.checks if c.hard and not c.passed]


def check_insight(raw, filtered, company_name, strip_imperatives):
    sentences = [s for s in SENTENCE_SPLIT.split(filtered.strip()) if s]
    raw_sentences = [s for s in SENTENCE_SPLIT.split(raw.strip()) if s]
    advice = [s for s in raw_sentences if ADVICE_WORDS.search(s)]
    imperative_stripped = len(raw_sentences) - len([s for s in SENTENCE_SPLIT.split(strip_imperatives(raw)) if s])
    return [
        Check("not_empty", bool(filtered.strip()), True),
        Check("names_company", company_name.lower() in filtered.lower(), True),
        Check("no_emoji", not EMOJI.search(raw), True),
        Check("ends_on_full_sentence", filtered.strip().endswith((".", "!", "?")), True,
              filtered.strip()[-40:]),
        Check("sentence_count_2_to_5", 2 <= len(sentences) <= 5, False, f"{len(sentences)} sentences"),
        Check("raw_avoids_advice", not advice and imperative_stripped == 0, False,
              f"{imperative_stripped} imperative sentence(s) stripped; advice wording: {advice[:2]}"),
    ]


def check_playbook(text, company_name, top_dimensions, parse):
    try:
        parsed = json.loads(parse(text))
        parsed = parsed if isinstance(parsed, dict) else {}
        parse_ok = bool(parsed)
    except (ValueError, TypeError):
        parsed, parse_ok = {}, False
    markdown = str(parsed.get("markdown_playbook", "") or "")
    lower = markdown.lower()
    missing = [d for d in top_dimensions if d.lower() not in lower]
    return markdown, [
        Check("valid_json", parse_ok, True),
        Check("has_playbook", bool(markdown.strip()), True),
        Check("names_company", company_name.lower() in lower, True),
        Check("covers_top_priorities", not missing, True, f"missing: {missing}" if missing else ""),
        Check("no_emoji", not EMOJI.search(text), True),
        Check("risk_status_valid", parsed.get("risk_status") in ("High", "Medium", "Low"), False,
              repr(parsed.get("risk_status"))),
    ]


class _Rollback(Exception):
    pass


def _usage_tokens(response):
    usage = getattr(response, "usage_metadata", None)
    return getattr(usage, "total_token_count", 0) or 0


def run_evals(client, model, cases=CASES):
    """Run every case through the real prompts. Returns (results, total_tokens)."""
    from . import ai_services as ai
    from .services import _band_for_score, _compute_scores, _save_snapshot

    questions = {q.id_code: q for q in Question.objects.filter(id_code__in=QUESTION_META)}
    missing = set(QUESTION_META) - set(questions)
    if missing:
        raise RuntimeError(f"Seeded questions missing ({len(missing)}); run `python manage.py load_gtm_defaults`.")

    results, tokens = [], 0
    try:
        with transaction.atomic():
            for case in cases:
                scores = case_scores(case)
                # is_completed stays False: the ResultSnapshot post_save signal
                # emails a report (internal BCC included) for completed sessions.
                session = AssessmentSession.objects.create(
                    owner_client_id=f"ai-eval-{case['key']}", **case["session"])
                Response.objects.bulk_create(
                    [Response(session=session, question=questions[qid], score=s) for qid, s in scores.items()])
                cat_scores, overall = _compute_scores(session)
                snapshot = _save_snapshot(
                    session, cat_scores, overall, _band_for_score(overall),
                    [c["category"].name for c in cat_scores], [round(c["avg"], 2) for c in cat_scores])
                company = case["session"]["company_name"]

                ordered = sorted(scores.items(), key=lambda kv: (kv[1], kv[0]))
                picks = [ordered[0], ordered[len(ordered) // 2], ordered[-1]]
                for qid, score in picks:
                    prompt = ai._build_diagnostic_prompt(session, questions[qid], score)
                    response = client.models.generate_content(model=model, contents=prompt, config=ai._FAST_CONFIG)
                    tokens += _usage_tokens(response)
                    raw = (response.text or "").strip()
                    filtered = ai._strip_imperative_sentences(raw)
                    results.append(Result(case["key"], "insight", qid, raw, filtered,
                                          check_insight(raw, filtered, company, ai._strip_imperative_sentences)))

                top = [p["dimension"] for p in build_recommendation_context(scores)["top_priorities"]]
                response = client.models.generate_content(
                    model=model, contents=ai._build_prompt(snapshot), config=ai._PLAYBOOK_CONFIG)
                tokens += _usage_tokens(response)
                raw = (response.text or "").strip()
                markdown, checks = check_playbook(raw, company, top, ai._clean_json_response)
                results.append(Result(case["key"], "playbook", "playbook", raw, markdown, checks))
            raise _Rollback
    except _Rollback:
        pass
    return results, tokens


def results_to_json(results, model, tokens):
    return {
        "model": model,
        "total_tokens": tokens,
        "results": [{
            "case": r.case, "kind": r.kind, "target": r.target, "output": r.output, "raw": r.raw,
            "checks": [{"name": c.name, "passed": c.passed, "hard": c.hard, "detail": c.detail} for c in r.checks],
        } for r in results],
    }
