"""
Delivery Document Analyzer
--------------------------
Extracts text from uploaded business documents (CSV, XLSX, PDF, DOCX, images,
TXT, JSON) and uses Gemini to auto-score the 6 Delivery assessment questions.

Human-AI design principle: the agent proposes scores grounded in document
evidence; the human reviews and overrides before submitting the assessment.
"""

import csv
import json
import logging
import os
import zipfile
import xml.etree.ElementTree as ET
from io import BytesIO, StringIO
from pathlib import Path

logger = logging.getLogger(__name__)

# Per-document text limit (chars) sent to Gemini.  Large enough to capture
# full retention tables, onboarding trackers, etc.
_TEXT_LIMIT_PER_DOC = 30_000
# Combined text limit across all documents for the analysis prompt.
_COMBINED_TEXT_LIMIT = 50_000

DELIVERY_QUESTIONS = [
    {
        "id_code": "DEL-TTV-01",
        "dimension": "Time to Value",
        "text": (
            "We track how long it takes new customers to get their first real value, "
            "by segment, and we actively work to shorten that time."
        ),
        "evidence_hint": (
            "Look for: time-to-value (TTV) metrics, activation timelines by customer segment, "
            "onboarding completion rates, TTV improvement initiatives, first-value milestone data."
        ),
    },
    {
        "id_code": "DEL-ONB-02",
        "dimension": "Onboarding Discipline",
        "text": (
            "Our onboarding process has clear milestones, clear owners, and realistic "
            "completion targets, and progress is tracked in one shared system."
        ),
        "evidence_hint": (
            "Look for: onboarding checklists, milestone definitions with owners, "
            "completion-date targets, project or task-tracker data, onboarding process documentation."
        ),
    },
    {
        "id_code": "DEL-HLT-03",
        "dimension": "Health Monitoring",
        "text": (
            "We score customer health using product usage, engagement, and support signals, "
            "and we use clear playbooks to act early on at-risk accounts."
        ),
        "evidence_hint": (
            "Look for: health-score models or rubrics, usage/engagement metrics, "
            "support-ticket trend data, at-risk account lists, CS intervention playbooks."
        ),
    },
    {
        "id_code": "DEL-RET-04",
        "dimension": "Retention",
        "text": (
            "We review gross and net retention by cohort every month, and we launch "
            "targeted actions quickly when any segment starts to decline."
        ),
        "evidence_hint": (
            "Look for: gross/net retention rates, cohort tables, monthly review cadence evidence, "
            "churn data by segment, save-motion records or win-back campaigns."
        ),
    },
    {
        "id_code": "DEL-QBR-05",
        "dimension": "Success Governance",
        "text": (
            "Our high-value customers receive regular business reviews focused on outcomes, "
            "roadmap alignment, and practical expansion opportunities."
        ),
        "evidence_hint": (
            "Look for: QBR schedules or meeting cadence, business-review agendas or notes, "
            "customer outcome tracking, expansion pipeline linked to reviews."
        ),
    },
    {
        "id_code": "DEL-ADV-06",
        "dimension": "Advocacy",
        "text": (
            "After customers achieve clear value, we consistently capture proof points like "
            "quotes, case studies, and references to support future selling."
        ),
        "evidence_hint": (
            "Look for: testimonial requests or a testimonial pipeline, case-study drafts, "
            "reference customer lists, an advocacy or customer-marketing programme."
        ),
    },
]


# ---------------------------------------------------------------------------
# Text extractors (path-based, reading from disk after upload)
# ---------------------------------------------------------------------------

def _extract_csv(file_path: str) -> str:
    rows = []
    try:
        with open(file_path, newline='', encoding='utf-8', errors='replace') as f:
            reader = csv.reader(f)
            for i, row in enumerate(reader):
                if i > 500:
                    rows.append("... (truncated at 500 rows)")
                    break
                rows.append(", ".join(str(c) for c in row))
        return "\n".join(rows)
    except Exception as exc:
        logger.warning("CSV extraction failed for %s: %s", file_path, exc)
        return ""


def _extract_xlsx(file_path: str) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        parts = []
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            parts.append(f"[Sheet: {sheet_name}]")
            row_count = 0
            for row in ws.iter_rows(values_only=True):
                if row_count >= 300:
                    parts.append("... (truncated)")
                    break
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    parts.append(", ".join(cells))
                    row_count += 1
        wb.close()
        return "\n".join(parts)
    except ImportError:
        logger.warning("openpyxl not installed; cannot parse XLSX %s", file_path)
        return ""
    except Exception as exc:
        logger.warning("XLSX extraction failed for %s: %s", file_path, exc)
        return ""


def _extract_pdf(file_path: str) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        pages = []
        for i, page in enumerate(reader.pages):
            if i >= 40:
                pages.append("... (truncated at 40 pages)")
                break
            text = page.extract_text() or ""
            if text:
                pages.append(text)
        return "\n".join(pages)
    except Exception as exc:
        logger.warning("PDF extraction failed for %s: %s", file_path, exc)
        return ""


def _extract_docx(file_path: str) -> str:
    # Try python-docx first, fall back to ZIP+XML stdlib
    try:
        import docx as _docx
        doc = _docx.Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        pass
    except Exception as exc:
        logger.warning("python-docx failed for %s: %s", file_path, exc)

    try:
        with zipfile.ZipFile(file_path) as z:
            if 'word/document.xml' not in z.namelist():
                return ""
            with z.open('word/document.xml') as f:
                tree = ET.parse(f)
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        paragraphs = []
        for para in tree.findall('.//w:p', ns):
            texts = [node.text or '' for node in para.findall('.//w:t', ns)]
            text = ''.join(texts).strip()
            if text:
                paragraphs.append(text)
        return "\n".join(paragraphs)
    except Exception as exc:
        logger.warning("DOCX ZIP fallback failed for %s: %s", file_path, exc)
        return ""


def _extract_txt(file_path: str) -> str:
    try:
        with open(file_path, encoding='utf-8', errors='replace') as f:
            return f.read(_TEXT_LIMIT_PER_DOC)
    except Exception as exc:
        logger.warning("TXT extraction failed for %s: %s", file_path, exc)
        return ""


def _extract_json(file_path: str) -> str:
    try:
        with open(file_path, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
        return json.dumps(data, indent=2)[:_TEXT_LIMIT_PER_DOC]
    except Exception as exc:
        logger.warning("JSON extraction failed for %s: %s", file_path, exc)
        return ""


def _extract_image_via_gemini(file_path: str, client, model_id: str) -> str:
    try:
        from PIL import Image
        img = Image.open(file_path)
        response = client.models.generate_content(
            model=model_id,
            contents=[
                (
                    "Describe all content in this image in full detail — including every number, "
                    "metric, table value, chart label, and text string you can read. "
                    "Return plain text only."
                ),
                img,
            ]
        )
        return (getattr(response, 'text', '') or '').strip()
    except Exception as exc:
        logger.warning("Gemini image extraction failed for %s: %s", file_path, exc)
        return ""


def extract_text_from_path(file_path: str, file_type: str,
                            client=None, model_id: str = "") -> str:
    """Route a saved file to the correct extractor and return extracted text."""
    if file_type == 'csv':
        text = _extract_csv(file_path)
    elif file_type == 'xlsx':
        text = _extract_xlsx(file_path)
    elif file_type == 'pdf':
        text = _extract_pdf(file_path)
    elif file_type == 'docx':
        text = _extract_docx(file_path)
    elif file_type == 'json':
        text = _extract_json(file_path)
    elif file_type == 'image':
        if client:
            text = _extract_image_via_gemini(file_path, client, model_id)
        else:
            text = ""
    else:  # txt, other
        text = _extract_txt(file_path)

    return text[:_TEXT_LIMIT_PER_DOC]


# ---------------------------------------------------------------------------
# Gemini analysis prompt
# ---------------------------------------------------------------------------

def _build_analysis_prompt(combined_text: str) -> str:
    question_block = "\n\n".join(
        f"**{q['id_code']} — {q['dimension']}**\n"
        f"Statement: \"{q['text']}\"\n"
        f"What to look for: {q['evidence_hint']}"
        for q in DELIVERY_QUESTIONS
    )

    return f"""You are a senior GTM analyst evaluating a company's Delivery capabilities.

Your task: read the uploaded business documents below and score the company on 6 Delivery \
assessment statements using a 1–5 scale.

SCORING SCALE:
1 = No / Not in place
2 = Ad-hoc / Rarely
3 = In progress / Sometimes
4 = Consistent / Often
5 = Best-in-class / Always

SCORING GUIDANCE:
- Score ONLY based on evidence found in the documents. Do not assume.
- No evidence at all → score 1 or 2.
- Partial or informal evidence → score 2 or 3.
- Clear documented process but not measured/optimised → score 3 or 4.
- Measured, reviewed, and continuously improved → score 4 or 5.
- Be conservative: overconfident scores mislead the playbook generation.

DELIVERY STATEMENTS TO SCORE:
{question_block}

UPLOADED DOCUMENTS:
---
{combined_text}
---

Return ONLY a valid JSON object — no markdown, no extra text, no explanation outside the JSON:
{{
  "DEL-TTV-01": {{"score": <int 1-5>, "reasoning": "<one clear sentence>", "evidence": "<exact quote or metric from document, or 'No direct evidence found'>", "confidence": "<high|medium|low>"}},
  "DEL-ONB-02": {{"score": <int 1-5>, "reasoning": "<one clear sentence>", "evidence": "<exact quote or metric from document, or 'No direct evidence found'>", "confidence": "<high|medium|low>"}},
  "DEL-HLT-03": {{"score": <int 1-5>, "reasoning": "<one clear sentence>", "evidence": "<exact quote or metric from document, or 'No direct evidence found'>", "confidence": "<high|medium|low>"}},
  "DEL-RET-04": {{"score": <int 1-5>, "reasoning": "<one clear sentence>", "evidence": "<exact quote or metric from document, or 'No direct evidence found'>", "confidence": "<high|medium|low>"}},
  "DEL-QBR-05": {{"score": <int 1-5>, "reasoning": "<one clear sentence>", "evidence": "<exact quote or metric from document, or 'No direct evidence found'>", "confidence": "<high|medium|low>"}},
  "DEL-ADV-06": {{"score": <int 1-5>, "reasoning": "<one clear sentence>", "evidence": "<exact quote or metric from document, or 'No direct evidence found'>", "confidence": "<high|medium|low>"}}
}}"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze_delivery_documents(session) -> dict:
    """
    Combine extracted text from all DeliveryDocuments for this session and
    call Gemini to score the 6 Delivery questions.

    Returns a dict keyed by question id_code:
        {
          "DEL-TTV-01": {"score": 3, "reasoning": "...", "evidence": "...", "confidence": "medium"},
          ...
        }
    Returns an empty dict on failure.
    """
    from .models import DeliveryDocument
    from .ai_services import _get_client, _clean_json_response
    from google.genai import types as genai_types

    docs = DeliveryDocument.objects.filter(session=session).exclude(extracted_text='')
    if not docs.exists():
        logger.warning("No documents with extracted text found for session %s", session.uuid)
        return {}

    combined_parts = []
    for doc in docs:
        combined_parts.append(
            f"=== Document: {doc.original_filename} ===\n{doc.extracted_text}"
        )
    combined_text = "\n\n".join(combined_parts)[:_COMBINED_TEXT_LIMIT]

    client = _get_client()
    if not client:
        logger.warning("Gemini client unavailable for delivery analysis")
        return {}

    model_id = "gemini-2.5-flash"
    prompt = _build_analysis_prompt(combined_text)

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

        expected_keys = {q['id_code'] for q in DELIVERY_QUESTIONS}
        if not expected_keys.issubset(result.keys()):
            logger.warning(
                "Gemini delivery analysis returned incomplete keys: %s", list(result.keys())
            )
            return {}

        for key in expected_keys:
            entry = result[key]
            entry['score'] = max(1, min(5, int(entry.get('score', 1))))
            if entry.get('confidence') not in ('high', 'medium', 'low'):
                entry['confidence'] = 'medium'

        return result

    except json.JSONDecodeError as exc:
        logger.warning("Delivery analysis JSON parse error: %s | raw[:300]=%s", exc, raw[:300])
        return {}
    except Exception as exc:
        logger.warning("Delivery analysis Gemini error: %s", exc)
        return {}
