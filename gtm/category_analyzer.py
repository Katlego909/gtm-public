"""
Category Document Analyzer
--------------------------
Extends the delivery analysis pipeline to support Demand and Conversion sections.
Reuses all NLP/ML utilities from delivery_analyzer (TF-IDF, sentence-transformers,
spaCy NER) and the same Gemini scoring approach.

Usage:
    from .category_analyzer import analyze_category_documents
    result = analyze_category_documents(session, 'demand')
    result = analyze_category_documents(session, 'conversion')
"""

import json
import logging

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Question definitions for Demand and Conversion
# These mirror the structure of DELIVERY_QUESTIONS in delivery_analyzer.py
# ---------------------------------------------------------------------------

DEMAND_QUESTIONS = [
    {
        "id_code": "DEM-ICP-01",
        "dimension": "ICP Clarity",
        "text": (
            "Do you have a simple written profile of your ideal customer — including who is a "
            "strong fit and who isn't — that all teams review and stay aligned on every quarter?"
        ),
        "evidence_hint": (
            "Look for: ICP documentation, buyer persona profiles, ideal customer criteria, "
            "fit/disqualification criteria in CRM, segment definitions, quarterly review records."
        ),
    },
    {
        "id_code": "DEM-FIT-02",
        "dimension": "Lead Quality",
        "text": (
            "Do at least 6 out of 10 new inbound leads match your ideal customer profile, "
            "verified with clear qualification fields in your CRM?"
        ),
        "evidence_hint": (
            "Look for: lead qualification rates, CRM field completeness reports, "
            "inbound lead fit percentages, ICP match scoring, lead source quality data."
        ),
    },
    {
        "id_code": "DEM-MSG-03",
        "dimension": "Positioning",
        "text": (
            "Is your core value message easy to understand, tested with each target segment, "
            "and used consistently across your website, outbound messages, and sales materials?"
        ),
        "evidence_hint": (
            "Look for: messaging frameworks, positioning documents, A/B test results on messaging, "
            "brand guidelines, homepage copy, outbound email templates, sales deck messaging."
        ),
    },
    {
        "id_code": "DEM-CHN-04",
        "dimension": "Channel Strategy",
        "text": (
            "Do you have a clear channel plan that shows expected customer acquisition cost and "
            "pipeline contribution for each channel, reviewed every month?"
        ),
        "evidence_hint": (
            "Look for: channel performance reports, CAC by channel, pipeline contribution by source, "
            "marketing spend allocation, monthly channel review cadence data."
        ),
    },
    {
        "id_code": "DEM-ATT-05",
        "dimension": "Attribution",
        "text": (
            "Can you reliably identify which marketing channels generate qualified leads and "
            "pipeline, using reporting data you trust?"
        ),
        "evidence_hint": (
            "Look for: multi-touch attribution reports, UTM tracking data, campaign-to-pipeline "
            "reporting, first/last touch attribution models, revenue attribution dashboards."
        ),
    },
    {
        "id_code": "DEM-CNT-06",
        "dimension": "Execution Cadence",
        "text": (
            "Do you run content or outbound work on a regular, planned schedule — with each "
            "activity tied to target accounts and clear pipeline goals?"
        ),
        "evidence_hint": (
            "Look for: content calendars, campaign schedules, outbound sequence cadence docs, "
            "target account lists, pipeline-tied campaign goals, weekly execution tracking."
        ),
    },
]

CONVERSION_QUESTIONS = [
    {
        "id_code": "CON-SLA-01",
        "dimension": "Speed to Lead",
        "text": (
            "We set clear response-time targets for new leads by source, and we check "
            "every week how quickly reps reply so high-intent leads are not lost."
        ),
        "evidence_hint": (
            "Look for: lead response time SLAs, CRM response time reports, speed-to-lead metrics, "
            "weekly rep response reviews, lead routing rules, alert thresholds for breach."
        ),
    },
    {
        "id_code": "CON-QLF-02",
        "dimension": "Qualification",
        "text": (
            "Required qualification fields are enforced as mandatory gates before opportunities "
            "can advance to the next pipeline stage."
        ),
        "evidence_hint": (
            "Look for: CRM pipeline stage rules, mandatory field requirements, BANT/MEDDIC/SPICED "
            "qualification frameworks in use, stage advancement criteria, qualification audits."
        ),
    },
    {
        "id_code": "CON-STG-03",
        "dimension": "Pipeline Hygiene",
        "text": (
            "Each pipeline stage has documented entry/exit criteria, they are enforced at "
            "stage advancement, and we review stage-to-stage conversion rates on a monthly cadence."
        ),
        "evidence_hint": (
            "Look for: pipeline stage definitions, stage conversion rate reports, monthly pipeline "
            "reviews, CRM workflow rules, stage criteria documentation, bottleneck analysis."
        ),
    },
    {
        "id_code": "CON-OBJ-04",
        "dimension": "Deal Enablement",
        "text": (
            "We have structured tools for handling objections and competitor questions: "
            "a written playbook, coaching sessions, call reviews, or competitive battlecards."
        ),
        "evidence_hint": (
            "Look for: objection handling playbooks, battlecards, competitive intelligence docs, "
            "call recording review schedules, coaching session notes, sales enablement materials."
        ),
    },
    {
        "id_code": "CON-WNL-05",
        "dimension": "Win-Loss Learning",
        "text": (
            "We capture win/loss reasons in a structured way and run regular reviews "
            "to turn findings into coaching and process changes."
        ),
        "evidence_hint": (
            "Look for: win/loss tracking data, closed-lost reason taxonomy in CRM, "
            "monthly win/loss review meeting notes, process changes driven by win/loss findings."
        ),
    },
    {
        "id_code": "CON-PGE-06",
        "dimension": "Funnel Optimization",
        "text": (
            "We test and optimize key conversion pages and forms with defined hypotheses, "
            "success metrics, and results tracked in a shared system."
        ),
        "evidence_hint": (
            "Look for: A/B test records, conversion rate optimization (CRO) reports, hypothesis "
            "documentation, experiment logs, conversion page performance data, test result archives."
        ),
    },
]

CATEGORY_QUESTIONS = {
    'demand': DEMAND_QUESTIONS,
    'conversion': CONVERSION_QUESTIONS,
}

CATEGORY_LABELS = {
    'demand': 'Demand',
    'conversion': 'Conversion',
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_category_documents(session, category: str) -> dict:
    """
    Three-stage pipeline for Demand or Conversion sections:
      1. Collect extracted text from all CategoryDocuments for this session+category.
      2. Run NLP/ML pre-processing (TF-IDF · sentence-transformers · spaCy NER).
      3. Call Gemini with the enriched, structured prompt.

    Returns a dict keyed by question id_code, or {"_error": "..."} on failure.
    """
    from .models import CategoryDocument
    from .ai_services import (
        _get_client, _clean_json_response, _is_quota_error,
        _extract_retry_delay_seconds, _set_quota_cooldown, _quota_cooldown_active,
    )
    from .delivery_analyzer import (
        extract_text_from_path, _build_nlp_evidence, _build_analysis_prompt,
        _scoring_memory_fallback, _capture_scoring_examples,
        _COMBINED_TEXT_LIMIT,
    )
    from google.genai import types as genai_types

    questions = CATEGORY_QUESTIONS.get(category)
    if not questions:
        return {"_error": f"Unknown category: {category}"}

    label = CATEGORY_LABELS[category]

    docs = CategoryDocument.objects.filter(
        session=session, category=category
    ).exclude(extracted_text='')
    if not docs.exists():
        logger.warning("No documents with extracted text for session %s category %s", session.uuid, category)
        return {}

    combined_parts = [
        f"=== Document: {doc.original_filename} ===\n{doc.extracted_text}"
        for doc in docs
    ]
    combined_text = "\n\n".join(combined_parts)[:_COMBINED_TEXT_LIMIT]

    logger.info("%s analysis — starting NLP/ML pre-processing…", label)
    nlp_evidence = _build_nlp_evidence(combined_text, questions)

    client = _get_client()
    if not client or _quota_cooldown_active():
        logger.warning("Gemini unavailable for %s analysis — using scoring memory fallback", category)
        return _scoring_memory_fallback(nlp_evidence, questions)

    model_id = "gemini-2.5-flash"
    prompt = _build_analysis_prompt(combined_text, nlp_evidence, questions, label)

    try:
        config = genai_types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=config,
        )
        raw = getattr(response, 'text', '') or ''
        cleaned = _clean_json_response(raw)
        result = json.loads(cleaned)

        expected_keys = {q['id_code'] for q in questions}
        if not expected_keys.issubset(result.keys()):
            logger.warning(
                "%s analysis returned incomplete keys: %s", label, list(result.keys())
            )
            return {}

        for key in expected_keys:
            entry = result[key]
            entry['score'] = max(1, min(5, int(entry.get('score', 1))))
            if entry.get('confidence') not in ('high', 'medium', 'low'):
                entry['confidence'] = 'medium'

        _capture_scoring_examples(nlp_evidence, result, questions)
        return result

    except json.JSONDecodeError as exc:
        logger.warning("%s analysis JSON parse error: %s | raw[:300]=%s", label, exc, raw[:300])
        return {"_error": f"JSON parse failed: {exc}"}
    except Exception as exc:
        logger.warning("%s analysis Gemini error: %s", label, exc)
        if _is_quota_error(exc):
            _set_quota_cooldown(_extract_retry_delay_seconds(exc))
        fallback = _scoring_memory_fallback(nlp_evidence, questions)
        if fallback:
            return fallback
        return {"_error": str(exc)}
