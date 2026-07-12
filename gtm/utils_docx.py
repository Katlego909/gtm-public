# gtm/utils_docx.py
import re
from io import BytesIO
from django.http import HttpResponse
from docx import Document
from docx.shared import Pt, RGBColor

from .utils_pdf import _normalize_ai_markdown, _parse_playbook_priorities, _slugify_filename_part


def render_insight_docx_response(*, company_name, ai_playbook_md, doc_title=None, doc_type_label=None, doc_date=None):
    """
    Build a Word (.docx) export of a single AI insight, structurally matching
    the PDF export: a title, then each parsed "Priority N: Title (Category)"
    section as a heading with its bullet points.

    doc_title/doc_type_label/doc_date: see render_insight_pdf_response's
    docstring in gtm/utils_pdf.py -- same optional-identity filename scheme.
    """
    normalized = _normalize_ai_markdown(ai_playbook_md or "")
    priorities = _parse_playbook_priorities(normalized)

    doc = Document()

    title = doc.add_heading("Funti3r GTM Validator", level=0)
    for run in title.runs:
        run.font.color.rgb = RGBColor(0x1E, 0x40, 0xAF)

    subtitle = doc.add_heading(f"{company_name or 'Company'} — AI Insight", level=1)
    for run in subtitle.runs:
        run.font.color.rgb = RGBColor(0x1E, 0x3A, 0x8A)

    if priorities:
        for priority in priorities:
            heading_text = priority["title"]
            if priority.get("category"):
                heading_text = f"{heading_text} ({priority['category']})"
            doc.add_heading(heading_text, level=2)
            for bullet in priority.get("bullets", []):
                doc.add_paragraph(bullet, style="List Bullet")
    else:
        # Fallback: no structured "Priority N" sections found — dump the
        # normalized markdown as plain paragraphs so the export is never empty.
        plain_text = re.sub(r"[*_#>`-]", "", normalized).strip()
        for line in plain_text.splitlines():
            line = line.strip()
            if line:
                doc.add_paragraph(line)

    buffer = BytesIO()
    doc.save(buffer)
    docx_bytes = buffer.getvalue()
    buffer.close()

    resp = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    company_slug = _slugify_filename_part(company_name, fallback="company")
    _parts = [company_slug]
    if doc_title:
        _parts.append(_slugify_filename_part(doc_title, fallback="document", max_len=50))
    _parts.append(_slugify_filename_part(doc_type_label, fallback="insight") if doc_type_label else "insight")
    if doc_date:
        _parts.append(doc_date.strftime("%Y%m%d"))
    _parts.append("ForgeGTM")
    _filename = "_".join(_parts) + ".docx"
    resp["Content-Disposition"] = f'attachment; filename="{_filename}"'
    resp.write(docx_bytes)
    return resp
