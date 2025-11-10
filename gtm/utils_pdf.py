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

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="H1",
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        spaceAfter=10,
        textColor=colors.HexColor("#1E3A8A"),
    ))
    styles.add(ParagraphStyle(
        name="H2",
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=18,
        spaceAfter=6,
        textColor=colors.HexColor("#334155"),
    ))
    styles.add(ParagraphStyle(
        name="Body",
        fontName="Helvetica",
        fontSize=11,
        leading=16,
        spaceAfter=6,
        textColor=colors.HexColor("#334155"),
    ))
    styles.add(ParagraphStyle(
        name="Quote",
        fontName="Helvetica-Oblique",
        fontSize=10,
        leading=14,
        leftIndent=18,
        textColor=colors.HexColor("#475569"),
    ))
    
    styles.add(ParagraphStyle(
        name="List",
        parent=styles["Body"],
        leading=15,        # a bit more line height
        spaceBefore=0,
        spaceAfter=4,      # vertical gap BETWEEN list items
    ))

    content = []

    # ── Cover / Summary
    content.append(Paragraph("Funti3r GTM Validator – Health Report", styles["H1"]))
    content.append(Paragraph(f"<b>Company:</b> {session.company_name or '-'}", styles["Body"]))
    content.append(Paragraph(f"<b>Industry:</b> {session.industry or '-'}", styles["Body"]))
    content.append(Paragraph(f"<b>Date:</b> {session.created_at.strftime('%Y-%m-%d %H:%M')}", styles["Body"]))
    content.append(Spacer(1, 0.3 * cm))
    content.append(Paragraph(f"<b>Overall Score:</b> {round(overall, 1)} / 100", styles["H2"]))
    if band:
        content.append(Paragraph(f"<b>Stage:</b> {band.stage}", styles["Body"]))
        content.append(Paragraph(f"<b>Summary:</b> {band.headline}", styles["Body"]))

    # Category breakdown
    content.append(Spacer(1, 0.5 * cm))
    content.append(Paragraph("Category Averages (1–5):", styles["H2"]))
    for row in cat_scores:
        content.append(Paragraph(f"• {row['category'].name}: {round(row['avg'], 2)}", styles["Body"]))

    # --- Recommended Next Moves (band.actions_markdown) as formatted bullets/sections
    content.append(Spacer(1, 0.6 * cm))
    content.append(Paragraph("Recommended Next Moves", styles["H1"]))
    if band and band.actions_markdown:
        content.extend(_md_to_flowables(band.actions_markdown, styles, doc.width))
    else:
        content.append(Paragraph("No recommendations available for this score range.", styles["Body"]))

    # --- New page: AI-Generated Playbook (the SAME markdown as the Playbook page)
    content.append(PageBreak())
    content.append(Paragraph("AI-Generated GTM Playbook", styles["H1"]))

    ai_md = ""
    snap = getattr(session, "snapshot", None)
    if snap and getattr(snap, "ai_playbook", ""):
        ai_md = snap.ai_playbook
    elif band and band.actions_markdown:
        # Fallback to band actions if AI text not present
        ai_md = band.actions_markdown

    if ai_md.strip():
        content.extend(_md_to_flowables(ai_md, styles, doc.width))
    else:
        content.append(Paragraph("No AI playbook was generated for this session.", styles["Body"]))

    # --- New page: 30-Day GTM Improvement Playbook (crisp, proportional columns)
    content.append(PageBreak())
    content.append(Paragraph("30-Day GTM Improvement Playbook", styles["H1"]))
    
    ai_md = ""
    snap = getattr(session, "snapshot", None)
    if snap and getattr(snap, "ai_playbook", ""):
        ai_md = snap.ai_playbook
    elif band and band.actions_markdown:
        ai_md = band.actions_markdown

    # ✅ Normalize BEFORE converting to flowables
    if ai_md.strip():
        ai_md = _normalize_ai_markdown(ai_md)
        content.extend(_md_to_flowables(ai_md, styles, doc.width))
    else:
        content.append(Paragraph("No AI playbook was generated for this session.", styles["Body"]))
    
    
    content.append(Paragraph(
        "Use this 4-week roadmap to focus your team, measure progress, and build momentum.",
        styles["Body"]
    ))
    content.append(Spacer(1, 0.3 * cm))

    def P(txt): return Paragraph(strip_tags(txt), styles["Body"])

    table_data = [
        [Paragraph("<b>Week</b>", styles["Body"]),
         Paragraph("<b>Focus Area</b>", styles["Body"]),
         Paragraph("<b>Objectives</b>", styles["Body"]),
         Paragraph("<b>Metrics to Track</b>", styles["Body"])],
        [P("Week 1"), P("Audit & Alignment"),
         P("Define your ideal customer type, refine your main message, review your lead sources."),
         P("Clear target profile & updated message")],
        [P("Week 2"), P("Process Optimization"),
         P("Set reply-time goals, standardize lead fit checks, and improve follow-ups."),
         P("Faster replies, higher qualification rate")],
        [P("Week 3"), P("Demand Generation"),
         P("Launch one consistent campaign (content, outreach, or ads) that runs every week."),
         P("Traffic & leads increasing")],
        [P("Week 4"), P("Measure & Refine"),
         P("Review results, fix weak spots, and plan the next cycle."),
         P("Improved conversion & retention")],
    ]

    # Proportional widths based on available frame width
    avail = doc.width
    col_widths = [0.13 * avail, 0.22 * avail, 0.43 * avail, 0.22 * avail]

    table = Table(
        table_data,
        colWidths=col_widths,
        repeatRows=1,
        splitByRow=1,
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        # header
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E3A8A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 9),
        ("TOPPADDING", (0, 0), (-1, 0), 8),

        # body
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#334155")),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),   # Center the 'Week' column
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),

        # padding & grid
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),

        # subtle box + thicker header bottom line
        ("LINEBEFORE", (0, 0), (0, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("LINEAFTER", (-1, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("LINEBELOW", (0, 0), (-1, 0), 1.0, colors.HexColor("#0F172A")),
    ]))
    content.append(table)
    content.append(Spacer(1, 0.4 * cm))
    content.append(Paragraph(
        "Tip: Keep it simple — small weekly improvements compound into real growth.",
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
