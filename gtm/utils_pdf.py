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


def render_gtm_report_pdf_response(*, session, cat_scores, overall, band, engine_output=None):
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
    if engine_output and engine_output.get("primary_actions"):
        for action in engine_output["primary_actions"]:
            content.append(Paragraph(f"• {action}", styles["Body"]))
    elif band and band.actions_markdown:
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

    # ✅ Normalize BEFORE converting to flowables
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

    # --- 30-Day Implementation Framework
    content.append(Paragraph("30-Day Implementation Framework", styles["H2"]))
    content.append(Paragraph(
        "Use this structured roadmap to implement improvements systematically and track your progress:",
        styles["Body"]
    ))
    content.append(Spacer(1, 0.3 * cm))

    def P(txt): return Paragraph(strip_tags(txt), styles["Body"])
    def PBold(txt): return Paragraph(f"<b>{strip_tags(txt)}</b>", styles["Body"])

    table_data = [
        [Paragraph("<b>Week</b>", styles["Body"]),
         Paragraph("<b>Focus Area</b>", styles["Body"]),
         Paragraph("<b>Key Objectives</b>", styles["Body"]),
         Paragraph("<b>Success Metrics</b>", styles["Body"])],
        [PBold("Week 1"), 
         Paragraph('<font color="#1E40AF"><b>Audit & Alignment</b></font>', styles["Body"]),
         P("• Define ideal customer profile (ICP)<br/>• Refine value proposition & messaging<br/>• Review and document current lead sources"),
         P("✓ Documented ICP<br/>✓ Updated messaging<br/>✓ Lead source audit complete")],
        [PBold("Week 2"), 
         Paragraph('<font color="#7C3AED"><b>Process Optimization</b></font>', styles["Body"]),
         P("• Set response time targets (< 5 min ideal)<br/>• Standardize lead qualification criteria<br/>• Implement systematic follow-up sequences"),
         P("✓ Response time < 1 hour<br/>✓ 80%+ leads qualified<br/>✓ Follow-up rate > 90%")],
        [PBold("Week 3"), 
         Paragraph('<font color="#059669"><b>Demand Generation</b></font>', styles["Body"]),
         P("• Launch one consistent content/outreach campaign<br/>• Activate multiple lead channels<br/>• Test and iterate messaging across channels"),
         P("✓ 20%+ increase in traffic<br/>✓ 15%+ more qualified leads<br/>✓ Campaign running weekly")],
        [PBold("Week 4"), 
         Paragraph('<font color="#DC2626"><b>Measure & Refine</b></font>', styles["Body"]),
         P("• Review all metrics and conversion rates<br/>• Identify and fix bottlenecks<br/>• Plan next 30-day improvement cycle"),
         P("✓ Full metrics dashboard<br/>✓ Conversion improved 10%+<br/>✓ Next cycle planned")],
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
        # header with gradient-like effect
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E40AF")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 11),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 10),
        ("TOPPADDING", (0, 0), (-1, 0), 10),

        # body styling
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#1F2937")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),

        # alternating row colors for better readability
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#EFF6FF")),
        ("BACKGROUND", (0, 2), (-1, 2), colors.white),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#F5F3FF")),
        ("BACKGROUND", (0, 4), (-1, 4), colors.white),

        # padding for breathing room
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 1), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 12),
        
        # grid and borders
        ("GRID", (0, 0), (-1, -1), 0.75, colors.HexColor("#CBD5E1")),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#1E3A8A")),
        ("BOX", (0, 0), (-1, -1), 1, colors.HexColor("#94A3B8")),
    ]))
    content.append(table)
    content.append(Spacer(1, 0.6 * cm))
    
    # Enhanced tip with icon-like styling
    content.append(Paragraph(
        "💡 <b>Implementation Tip:</b> Focus on completing one week fully before moving to the next. "
        "Small, consistent improvements compound into significant growth over time.",
        styles["Quote"]
    ))
    content.append(Spacer(1, 0.4 * cm))
    content.append(Paragraph(
        "📊 <b>Tracking Advice:</b> Review progress weekly and adjust tactics based on what's working. "
        "Document wins and lessons learned to build institutional knowledge.",
        styles["Quote"]
    ))

    # Build with header/footer
    doc.build(content, onFirstPage=_header_footer, onLaterPages=_header_footer)

    pdf = buffer.getvalue()
    buffer.close()

    resp = HttpResponse(content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename=\"gtm_report_{session.uuid}.pdf\"'
    resp.write(pdf)
    return resp
