# gtm/utils_pdf.py
import re
from io import BytesIO
from django.http import HttpResponse
from django.utils.html import strip_tags
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    ListFlowable, ListItem
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
import markdown as mdlib


def _clean_md(text: str) -> str:
    """Strip markdown formatting and escape XML special chars for ReportLab Paragraph."""
    text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    return text.strip()


def _trunc(text: str, max_len: int = 160) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(' ', 1)[0] + '…'


def _slugify_filename_part(text: str, fallback: str = "untitled", max_len: int = None) -> str:
    """Filesystem-safe filename fragment: strip non-word/space/hyphen chars,
    collapse whitespace to underscores, optionally truncate."""
    slug = re.sub(r'[^\w\s-]', '', text or fallback).strip()
    slug = re.sub(r'[\s]+', '_', slug).strip('_') or fallback
    if max_len and len(slug) > max_len:
        slug = slug[:max_len].rstrip('_')
    return slug


def _parse_playbook_priorities(ai_md: str) -> list:
    """Extract Priority sections from AI playbook markdown."""
    priorities = []
    pattern = re.compile(
        r'(?:^|\n)#{0,3}\s*Priority\s+\d+:\s*(.+?)(?:\s*\(([^)]+)\))?\s*\n(.*?)(?=\n#{0,3}\s*Priority\s+\d+:|\Z)',
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(ai_md):
        title = m.group(1).strip()
        category = (m.group(2) or "").strip()
        body = m.group(3).strip()
        # Only match real bullet lines (• or -), not ** heading lines
        raw_bullets = re.findall(r'(?m)^[•\-]\s*(.+)', body)
        bullets = [_clean_md(b) for b in raw_bullets[:3]]
        if title:
            priorities.append({"title": title, "category": category, "bullets": bullets})
    return priorities[:3]


def _header_footer(canvas, doc):
    # Top brand bar + title
    canvas.saveState()
    w, h = A4
    canvas.setFillColor(colors.HexColor("#1E3A8A"))
    canvas.rect(0, h - 1.0 * cm, w, 1.0 * cm, fill=True, stroke=False)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(2 * cm, h - 0.6 * cm, "Funti3r GTM Validator — Report")
    # Footer page number
    canvas.setFillColor(colors.HexColor("#475569"))
    canvas.setFont("Helvetica", 9)
    canvas.drawRightString(w - 2 * cm, 0.8 * cm, f"Page {doc.page}")
    canvas.restoreState()


# -------------------------------------------------------------------
# Markdown → Flowables (headings, paragraphs, lists, TABLES)
# -------------------------------------------------------------------

def _normalize_ai_markdown(src: str) -> str:
    if not src:
        return ""
    s = src.replace("\r\n", "\n").strip()

    # 1) Inline " * " separators → real bullet lines
    s = re.sub(r"\s\*\s+", "\n- ", s)

    # 2) Promote "Week X:" lines to headings (more breathing room in PDF)
    s = re.sub(r"(?m)^(Week\s+\d+:[^\n]*)$", r"### \1", s)

    # 3) Ensure blank line before lists/numbered items/headings so Markdown makes blocks
    s = re.sub(r"(?m)(?<!\n)\n(?=(?:- |\d+\. |#{1,6}\s))", "\n\n", s)

    # 4) Clean trailing spaces and collapse excessive newlines
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s

def _md_to_flowables(text: str, styles, avail_width: float) -> list:
    """
    Convert Markdown (incl. tables) into ReportLab flowables.

    - Uses markdown with "extra", "sane_lists", "tables"
    - If BeautifulSoup is available, we map HTML elements precisely
    - Falls back to plain Paragraph when bs4 isn't available
    """
    # Render to HTML first (supports tables/lists/etc.)
    html = mdlib.markdown(text or "", extensions=["extra", "sane_lists", "tables"])

    flow = []

    # Fallback if BeautifulSoup isn't installed
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        plain = strip_tags(html)
        if plain.strip():
            flow.append(Paragraph(plain, styles["Body"]))
        return flow

    def P(raw: str) -> Paragraph:
        return Paragraph(raw, styles["Body"])

    def _list_flow(ul_or_ol, numbered=False):
        items = []
        for li in ul_or_ol.find_all("li", recursive=False):
            text = li.get_text(" ", strip=True)
            # each item uses the List style so it adds vertical breathing room
            items.append(ListItem(Paragraph(text, styles["List"]), leftIndent=0))

        # add spacing around the whole list and slightly smaller bullet size
        return ListFlowable(
            items,
            bulletType=("1" if numbered else "bullet"),
            start="•",
            leftIndent=16,                # indent whole list block
            bulletFontName="Helvetica",
            bulletFontSize=9,
            bulletOffsetY=0,
            spaceBefore=4,                # gap before the list starts
            spaceAfter=10,                # gap after the list ends
        )

    def _table_flow(table_tag):
        # Extract rows/cells and convert to Paragraphs
        rows = []
        max_cols = 0
        for tr in table_tag.find_all("tr"):
            row_cells = []
            for cell in tr.find_all(["th", "td"]):
                txt = cell.get_text(" ", strip=True)
                if cell.name == "th":
                    row_cells.append(Paragraph(f"<b>{txt}</b>", styles["Body"]))
                else:
                    row_cells.append(P(txt))
            max_cols = max(max_cols, len(row_cells))
            rows.append(row_cells)

        if max_cols == 0:
            return []

        col_w = [avail_width / max_cols] * max_cols
        t = Table(rows, colWidths=col_w, repeatRows=1, splitByRow=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0F172A")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ]))
        return [t]

    # Walk top-level children and map to flowables
    for el in soup.children:
        name = getattr(el, "name", None)
        if not name:
            continue

        if name in ("h1", "h2", "h3"):
            txt = el.get_text(" ", strip=True)
            flow.append(Paragraph(txt, styles["H1" if name == "h1" else "H2"]))
        elif name == "p":
            txt = el.get_text(" ", strip=True)
            if txt:
                flow.append(P(txt))
        elif name == "ul":
            flow.append(_list_flow(el, numbered=False))
        elif name == "ol":
            flow.append(_list_flow(el, numbered=True))
        elif name == "table":
            flow.extend(_table_flow(el))
        elif name == "blockquote":
            txt = el.get_text(" ", strip=True)
            if txt:
                flow.append(Paragraph(txt, styles["Quote"]))
        # other tags can be added as needed

    return flow


def _build_report_styles():
    """Shared ParagraphStyle set for all reportlab-based PDF exports (full report + single insight)."""
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="H1",
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=26,
        spaceAfter=14,
        spaceBefore=8,
        textColor=colors.HexColor("#1E40AF"),
    ))
    styles.add(ParagraphStyle(
        name="H2",
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=20,
        spaceAfter=8,
        spaceBefore=6,
        textColor=colors.HexColor("#1E3A8A"),
    ))
    styles.add(ParagraphStyle(
        name="Body",
        fontName="Helvetica",
        fontSize=11,
        leading=17,
        spaceAfter=8,
        textColor=colors.HexColor("#1F2937"),
    ))
    styles.add(ParagraphStyle(
        name="Quote",
        fontName="Helvetica-Oblique",
        fontSize=10,
        leading=15,
        leftIndent=20,
        rightIndent=20,
        spaceBefore=8,
        spaceAfter=8,
        textColor=colors.HexColor("#6B7280"),
        backColor=colors.HexColor("#F9FAFB"),
    ))

    styles.add(ParagraphStyle(
        name="List",
        parent=styles["Body"],
        leading=16,
        spaceBefore=2,
        spaceAfter=6,
    ))

    styles.add(ParagraphStyle(
        name="Highlight",
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=18,
        spaceAfter=6,
        textColor=colors.HexColor("#DC2626"),
    ))
    return styles


def render_gtm_report_pdf_response(*, session, cat_scores, overall, band):
    """
    Build a professional, multi-page PDF (with the AI Playbook + 30-Day plan)
    using built-in Helvetica fonts for maximum compatibility.
    Returns an HttpResponse ready to send.
    """
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = _build_report_styles()

    content = []

    # ── Cover / Summary with improved layout
    content.append(Spacer(1, 0.5 * cm))
    content.append(Paragraph("Funti3r GTM Validator", styles["H1"]))
    content.append(Paragraph("Assessment Report", styles["H2"]))
    content.append(Spacer(1, 0.8 * cm))
    
    # Company info box
    company_data = [
        [Paragraph("<b>Company</b>", styles["Body"]), Paragraph(session.company_name or '-', styles["Body"])],
        [Paragraph("<b>Industry</b>", styles["Body"]), Paragraph(session.industry or '-', styles["Body"])],
        [Paragraph("<b>Assessment Date</b>", styles["Body"]), Paragraph(session.created_at.strftime('%B %d, %Y at %H:%M'), styles["Body"])],
    ]
    company_table = Table(company_data, colWidths=[4*cm, 12*cm])
    company_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F5F9")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#1F2937")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
    ]))
    content.append(company_table)
    content.append(Spacer(1, 0.8 * cm))
    
    # Score highlight box
    score_color = colors.HexColor("#10B981") if overall >= 70 else (colors.HexColor("#F59E0B") if overall >= 50 else colors.HexColor("#EF4444"))
    content.append(Paragraph(f'<font size="16" color="{score_color.hexval()}"><b>Overall Score: {round(overall, 1)} / 100</b></font>', styles["Body"]))
    
    if band:
        content.append(Spacer(1, 0.3 * cm))
        content.append(Paragraph(f"<b>GTM Maturity Stage:</b> {band.stage}", styles["Body"]))
        content.append(Paragraph(f"{band.headline}", styles["Body"]))

    # Category breakdown with better formatting
    content.append(Spacer(1, 0.8 * cm))
    content.append(Paragraph("Category Performance Breakdown", styles["H2"]))
    content.append(Spacer(1, 0.2 * cm))
    
    cat_data = [[Paragraph("<b>Category</b>", styles["Body"]), Paragraph("<b>Score (1-5)</b>", styles["Body"]), Paragraph("<b>Performance</b>", styles["Body"])]]
    for row in cat_scores:
        score_val = round(row['avg'], 2)
        performance = "Excellent" if score_val >= 4 else ("Good" if score_val >= 3 else ("Needs Improvement" if score_val >= 2 else "Critical"))
        perf_color = "#10B981" if score_val >= 4 else ("#3B82F6" if score_val >= 3 else ("#F59E0B" if score_val >= 2 else "#EF4444"))
        cat_data.append([
            Paragraph(row['category'].name, styles["Body"]),
            Paragraph(f"<b>{score_val}</b>", styles["Body"]),
            Paragraph(f'<font color="{perf_color}"><b>{performance}</b></font>', styles["Body"])
        ])
    
    cat_table = Table(cat_data, colWidths=[10*cm, 3*cm, 4*cm])
    cat_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
    ]))
    content.append(cat_table)

    # --- Recommended Next Moves (band.actions_markdown) as formatted bullets/sections
    content.append(Spacer(1, 0.6 * cm))
    content.append(Paragraph("Recommended Next Moves", styles["H1"]))
    if band and band.actions_markdown:
        content.extend(_md_to_flowables(band.actions_markdown, styles, doc.width))
    else:
        content.append(Paragraph("No recommendations available for this score range.", styles["Body"]))

    # --- New page: AI-Generated GTM Playbook
    content.append(PageBreak())
    content.append(Paragraph("AI-Generated GTM Playbook & 30-Day Action Plan", styles["H1"]))
    content.append(Spacer(1, 0.2 * cm))
    
    ai_md = ""
    snap = getattr(session, "snapshot", None)
    if snap and getattr(snap, "ai_playbook", ""):
        ai_md = snap.ai_playbook
    elif band and band.actions_markdown:
        # Fallback to band actions if AI text not present
        ai_md = band.actions_markdown

    # Normalize BEFORE converting to flowables
    if ai_md.strip():
        ai_md = _normalize_ai_markdown(ai_md)
        content.append(Paragraph(
            "This customized playbook provides week-by-week guidance to improve your GTM execution:",
            styles["Body"]
        ))
        content.append(Spacer(1, 0.3 * cm))
        content.extend(_md_to_flowables(ai_md, styles, doc.width))
        content.append(Spacer(1, 0.5 * cm))
    else:
        content.append(Paragraph(
            "No AI playbook was generated for this session. Complete the assessment to generate personalized recommendations.",
            styles["Body"]
        ))
        content.append(Spacer(1, 0.5 * cm))

    # --- Financial Estimates & Competitive Position (AI enrichment sections)
    fin_md = (getattr(snap, "ai_financial_summary", "") or "").strip() if snap else ""
    comp_md = (getattr(snap, "ai_competitor_analysis", "") or "").strip() if snap else ""

    def _boxed_section(heading, body_md, bg_hex, border_hex, title_hex, footnote=None):
        heading_style = ParagraphStyle(
            name="BoxHeading", fontName="Helvetica-Bold", fontSize=12,
            leading=16, spaceAfter=6, textColor=colors.HexColor(title_hex),
        )
        inner = [Paragraph(heading, heading_style)]
        # Strip the leading "## ..." markdown heading since we render our own styled heading
        body_clean = re.sub(r'(?m)^#{1,3}\s*.+\n?', '', body_md, count=1).strip()
        inner.extend(_md_to_flowables(body_clean, styles, doc.width - 1.2 * cm))
        if footnote:
            inner.append(Paragraph(footnote, ParagraphStyle(
                name="BoxFootnote", fontName="Helvetica-Oblique", fontSize=8.5,
                leading=12, textColor=colors.HexColor("#475569"), spaceBefore=4,
            )))
        box = Table([[inner]], colWidths=[doc.width])
        box.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(bg_hex)),
            ("BOX", (0, 0), (-1, -1), 1, colors.HexColor(border_hex)),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return box

    if fin_md:
        content.append(_boxed_section(
            "Financial Impact Estimates", fin_md,
            bg_hex="#ECFDF5", border_hex="#A7F3D0", title_hex="#065F46",
            footnote="AI-generated estimates based on your company data. Validate with your finance team before making budget decisions.",
        ))
        content.append(Spacer(1, 0.4 * cm))

    if comp_md:
        content.append(_boxed_section(
            "Competitive Gap Analysis", comp_md,
            bg_hex="#EFF6FF", border_hex="#BFDBFE", title_hex="#1E3A8A",
        ))
        content.append(Spacer(1, 0.5 * cm))

    # --- 30-Day Implementation Framework
    content.append(Paragraph("30-Day Implementation Framework", styles["H2"]))
    content.append(Paragraph(
        "Use this structured roadmap to implement improvements systematically and track your progress:",
        styles["Body"]
    ))
    content.append(Spacer(1, 0.3 * cm))

    def P(txt): return Paragraph(strip_tags(txt), styles["Body"])
    def PBold(txt): return Paragraph(f"<b>{strip_tags(txt)}</b>", styles["Body"])

    _week_hex = ["#1E40AF", "#7C3AED", "#059669"]
    _week_bg  = [colors.HexColor("#EFF6FF"), colors.white, colors.HexColor("#F5F3FF")]

    priorities = _parse_playbook_priorities(ai_md)

    table_data = [
        [Paragraph("<b>Week</b>", styles["Body"]),
         Paragraph("<b>Focus Area</b>", styles["Body"]),
         Paragraph("<b>Key Objectives</b>", styles["Body"]),
         Paragraph("<b>Success Metrics</b>", styles["Body"])],
    ]

    def PBr(raw): return Paragraph(raw, styles["Body"])

    for i, pri in enumerate(priorities):
        obj_lines = "<br/>".join(f"• {b}" for b in pri["bullets"]) if pri["bullets"] else f"• Complete {_clean_md(pri['title'])} actions"
        cat_clean = _clean_md(pri["category"])
        title_clean = _clean_md(pri["title"])
        metric_lines = (
            f"&#10003; {title_clean} actions completed<br/>&#10003; {cat_clean} score improving"
            if cat_clean else f"&#10003; {title_clean} actions completed"
        )
        table_data.append([
            PBold(f"Week {i + 1}"),
            Paragraph(f'<font color="{_week_hex[i]}"><b>{_clean_md(pri["title"])}</b></font>', styles["Body"]),
            PBr(obj_lines),
            PBr(metric_lines),
        ])

    # Week 4: Measure & Refine — mention the specific weak categories
    _weak_cats = [r["category"].name for r in cat_scores if r["avg"] < 3.0][:2]
    _weak_str = " &amp; ".join(_clean_md(c) for c in _weak_cats)
    _measure_focus = (
        f"• Track improvements in {_weak_str}<br/>• Review all metrics and conversion rates<br/>• Plan next 30-day improvement cycle"
        if _weak_cats
        else "• Review all metrics and conversion rates<br/>• Identify and fix bottlenecks<br/>• Plan next 30-day improvement cycle"
    )
    table_data.append([
        PBold("Week 4"),
        Paragraph('<font color="#DC2626"><b>Measure &amp; Refine</b></font>', styles["Body"]),
        PBr(_measure_focus),
        PBr("&#10003; Full metrics dashboard<br/>&#10003; Conversion improved 10%+<br/>&#10003; Next cycle planned"),
    ])

    # Alternating row backgrounds (dynamic row count)
    _row_bg_cycle = [colors.HexColor("#EFF6FF"), colors.white, colors.HexColor("#F5F3FF"), colors.white]
    _row_bg_cmds = [
        ("BACKGROUND", (0, r), (-1, r), _row_bg_cycle[(r - 1) % len(_row_bg_cycle)])
        for r in range(1, len(table_data))
    ]

    # Proportional widths based on available frame width
    avail = doc.width
    col_widths = [0.12 * avail, 0.24 * avail, 0.42 * avail, 0.22 * avail]

    table = Table(
        table_data,
        colWidths=col_widths,
        repeatRows=1,
        splitByRow=1,
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E40AF")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 11),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#1F2937")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 1), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 12),
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#CBD5E1")),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#1E3A8A")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#94A3B8")),
        *_row_bg_cmds,
    ]))
    content.append(table)
    content.append(Spacer(1, 0.6 * cm))
    
    content.append(Paragraph(
        "<b>Implementation Tip:</b> Focus on completing one week fully before moving to the next. "
        "Small, consistent improvements compound into significant growth over time.",
        styles["Quote"]
    ))
    content.append(Spacer(1, 0.4 * cm))
    content.append(Paragraph(
        "<b>Tracking Advice:</b> Review progress weekly and adjust tactics based on what's working. "
        "Document wins and lessons learned to build institutional knowledge.",
        styles["Quote"]
    ))

    # Build with header/footer
    doc.build(content, onFirstPage=_header_footer, onLaterPages=_header_footer)

    pdf = buffer.getvalue()
    buffer.close()

    resp = HttpResponse(content_type="application/pdf")
    _company_slug = _slugify_filename_part(session.company_name, fallback="company")
    resp["Content-Disposition"] = f'attachment; filename=\"{_company_slug}_AI_playbook_report_ForgeGTM.pdf\"'
    resp.write(pdf)
    return resp


def render_insight_pdf_response(*, company_name, ai_playbook_md, doc_title=None, doc_type_label=None, doc_date=None):
    """
    Build a short, single-insight PDF (one AI-generated playbook, not the full
    multi-section assessment report). Reuses the same styles/header/footer and
    markdown pipeline as render_gtm_report_pdf_response for visual parity.

    doc_title/doc_type_label/doc_date are optional -- when supplied (by the
    AgentDocument export flow) they make the filename distinct per document
    instead of colliding on company_name alone; when omitted (the original
    ResultSnapshot insight-export caller) the filename falls back to its
    original "_insight_" shape, just with a date appended.
    """
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = _build_report_styles()

    content = [
        Spacer(1, 0.5 * cm),
        Paragraph("Funti3r GTM Validator", styles["H1"]),
        Paragraph(f"{company_name or 'Company'} — AI Insight", styles["H2"]),
        Spacer(1, 0.4 * cm),
    ]

    normalized = _normalize_ai_markdown(ai_playbook_md or "")
    content.extend(_md_to_flowables(normalized, styles, doc.width))

    doc.build(content, onFirstPage=_header_footer, onLaterPages=_header_footer)

    pdf = buffer.getvalue()
    buffer.close()

    resp = HttpResponse(content_type="application/pdf")
    _company_slug = _slugify_filename_part(company_name, fallback="company")
    _parts = [_company_slug]
    if doc_title:
        _parts.append(_slugify_filename_part(doc_title, fallback="document", max_len=50))
    _parts.append(_slugify_filename_part(doc_type_label, fallback="insight") if doc_type_label else "insight")
    if doc_date:
        _parts.append(doc_date.strftime("%Y%m%d"))
    _parts.append("ForgeGTM")
    _filename = "_".join(_parts) + ".pdf"
    resp["Content-Disposition"] = f'attachment; filename="{_filename}"'
    resp.write(pdf)
    return resp
