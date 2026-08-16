"""
pdf_export.py
-------------
Builds a clean, print-ready PDF of the Page 2 "Boss Dashboard" (and, going
forward, any other page - e.g. the future Custom Report / Pivot page - since
this function only needs a title, a list of KPI dicts and a list of chart
dicts, nothing page-specific). Only the finished visuals + labels/legends go
into the PDF - no setting panels, no interactive buttons, nothing that would
look unprofessional in a report.

v2 changes (professional redesign):
  - Every page gets a slim, consistently-colored masthead band (report title +
    page number + timestamp) so a reader always knows which report they're
    looking at, even on page 4 of 10.
  - KPI cards are visually distinct "cards" (colored top accent strip + tinted
    background derived from the theme, not a hardcoded white box that used to
    clash with dark themes) with a small uppercase "KPI" eyebrow label.
  - Every chart page now shows a small colored badge with the chart TYPE
    (e.g. "BAR CHART", "PIE CHART") next to the title, and the auto-insight is
    now a clearly boxed "Insight" callout instead of plain trailing text - so
    it's obvious at a glance what's a heading, what's the chart, and what's
    the takeaway.
  - Card/box backgrounds are tinted from the theme's own accent + background
    colors (instead of hardcoded white/grey), so the report always looks
    coherent no matter what accent color is chosen on the dashboard.
"""

import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
                                 PageBreak, NextPageTemplate, PageTemplate, Frame, BaseDocTemplate)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# Small icon per chart family, used for the badge on each chart page.
CHART_TYPE_ICONS = {
    "Bar": "\u25A4", "Line": "\u2197", "Pie": "\u25CF", "Donut": "\u25CE",
    "Area": "\u25B2", "Scatter": "\u2022", "Box": "\u25A6", "Histogram": "\u2261",
    "Treemap": "\u25A3", "Heatmap": "\u25A0", "Table": "\u2637", "Chart": "\u25A4",
}


def _hex_to_color(hexstr, fallback="#FFFFFF"):
    try:
        return colors.HexColor(hexstr)
    except Exception:
        return colors.HexColor(fallback)


def _blend(hex_a, hex_b, t, fallback="#FFFFFF"):
    """Blend two hex colors: t=0 -> hex_a, t=1 -> hex_b. Used to derive tinted
    card/box backgrounds from the theme's own colors instead of hardcoding
    white, so the report always looks coherent with whatever accent/background
    the user picked on the dashboard."""
    try:
        ca, cb = colors.HexColor(hex_a), colors.HexColor(hex_b)
        r = ca.red + (cb.red - ca.red) * t
        g = ca.green + (cb.green - ca.green) * t
        b = ca.blue + (cb.blue - ca.blue) * t
        return colors.Color(r, g, b)
    except Exception:
        return colors.HexColor(fallback)


def _readable_text_color(bg_hex, dark="#111111", light="#FFFFFF"):
    """Pick black or white text depending on how light/dark the bg color is,
    so labels drawn on the accent-colored masthead/badges stay legible."""
    try:
        c = colors.HexColor(bg_hex)
        luminance = 0.299 * c.red + 0.587 * c.green + 0.114 * c.blue
        return dark if luminance > 0.55 else light
    except Exception:
        return dark


def build_pdf_report(report_title, subtitle, kpis, chart_items, theme, filters_summary="", watermark=None):
    """
    kpis: list of {"label","value","sub"}
    chart_items: list of {"title","insight","png_bytes","type"(optional)}
    theme: {"bg_color","font_color","accent_color","font_name","wallpaper_bytes"}
    watermark: optional string (e.g. "FREE TRIAL") stamped diagonally, faint,
      across every page — used for free-plan exports so there's a real reason
      to upgrade (a client-facing report shouldn't carry a trial watermark).
      None/empty = no watermark, unchanged from before.
    """
    buf = io.BytesIO()
    bg_hex = theme.get("bg_color", "#FFFFFF")
    font_hex = theme.get("font_color", "#111111")
    accent_hex = theme.get("accent_color", "#2C6E49")

    bg_color = _hex_to_color(bg_hex)
    font_color = _hex_to_color(font_hex)
    accent = _hex_to_color(accent_hex)
    card_bg = _blend(bg_hex, accent_hex, 0.10)          # subtle tint of accent into the page bg
    box_border = _blend(bg_hex, accent_hex, 0.55)
    masthead_text_hex = _readable_text_color(accent_hex)
    masthead_text = _hex_to_color(masthead_text_hex)

    page_size = landscape(A4)
    MASTHEAD_H = 0.95 * cm

    def draw_background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(bg_color)
        canvas.rect(0, 0, page_size[0], page_size[1], fill=1, stroke=0)
        wp = theme.get("wallpaper_bytes")
        if wp:
            try:
                from reportlab.lib.utils import ImageReader
                img = ImageReader(io.BytesIO(wp))
                canvas.drawImage(img, 0, 0, width=page_size[0], height=page_size[1],
                                  preserveAspectRatio=False, mask='auto')
            except Exception:
                pass

        # Consistent colored masthead on EVERY page so it's always obvious
        # which report / which page a reader is looking at.
        canvas.setFillColor(accent)
        canvas.rect(0, page_size[1] - MASTHEAD_H, page_size[0], MASTHEAD_H, fill=1, stroke=0)
        canvas.setFillColor(masthead_text)
        canvas.setFont(theme.get("font_name", "Helvetica-Bold"), 9.5)
        canvas.drawString(1.2 * cm, page_size[1] - MASTHEAD_H + 0.28 * cm, report_title.upper())
        canvas.setFont(theme.get("font_name", "Helvetica"), 8)
        canvas.drawRightString(page_size[0] - 1.2 * cm, page_size[1] - MASTHEAD_H + 0.28 * cm,
                                f"Page {doc.page}")

        # Footer
        canvas.setFont(theme.get("font_name", "Helvetica"), 8)
        canvas.setFillColor(font_color)
        canvas.drawString(1.2 * cm, 0.6 * cm, f"Generated {datetime.now().strftime('%d-%b-%Y %H:%M')}")
        canvas.drawCentredString(page_size[0] / 2, 0.6 * cm, "Confidential - Internal Use Only")

        if watermark:
            canvas.saveState()
            try:
                canvas.setFillColor(colors.grey)
                canvas.setFillAlpha(0.16)
            except Exception:
                canvas.setFillColorRGB(0.65, 0.65, 0.65)  # older reportlab without alpha support
            canvas.setFont("Helvetica-Bold", 44)
            canvas.translate(page_size[0] / 2, page_size[1] / 2)
            canvas.rotate(32)
            canvas.drawCentredString(0, 0, watermark)
            canvas.restoreState()

        canvas.restoreState()

    doc = BaseDocTemplate(buf, pagesize=page_size,
                           leftMargin=1.4 * cm, rightMargin=1.4 * cm,
                           topMargin=MASTHEAD_H + 0.6 * cm, bottomMargin=1.2 * cm)
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="normal")
    template = PageTemplate(id="bg", frames=[frame], onPage=draw_background)
    doc.addPageTemplates([template])

    styles = getSampleStyleSheet()
    font_name = theme.get("font_name", "Helvetica")
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], textColor=font_color,
                                  fontName=font_name, fontSize=22, alignment=TA_CENTER)
    sub_style = ParagraphStyle("SubX", parent=styles["Normal"], textColor=font_color,
                                fontName=font_name, fontSize=11, alignment=TA_CENTER, spaceAfter=10)
    section_style = ParagraphStyle("SecX", parent=styles["Heading2"], textColor=accent,
                                    fontName=font_name, fontSize=14, spaceBefore=6, spaceAfter=4)
    badge_style = ParagraphStyle("Badge", parent=styles["Normal"], textColor=masthead_text,
                                  fontName=font_name, fontSize=8, alignment=TA_CENTER)
    insight_label_style = ParagraphStyle("InsLabel", parent=styles["Normal"], textColor=accent,
                                          fontName=font_name, fontSize=9.5, alignment=TA_LEFT)
    insight_style = ParagraphStyle("InsX", parent=styles["Normal"], textColor=font_color,
                                    fontName=font_name, fontSize=9.5, alignment=TA_LEFT)
    kpi_eyebrow_style = ParagraphStyle("KEyebrow", parent=styles["Normal"], fontName=font_name, fontSize=7,
                                        textColor=accent, alignment=TA_CENTER)
    kpi_label_style = ParagraphStyle("KLabel", parent=styles["Normal"], fontName=font_name, fontSize=9,
                                      textColor=font_color, alignment=TA_CENTER)
    kpi_value_style = ParagraphStyle("KValue", parent=styles["Normal"], fontName=font_name, fontSize=17,
                                      textColor=accent, alignment=TA_CENTER, leading=19)
    kpi_sub_style = ParagraphStyle("KSub", parent=styles["Normal"], fontName=font_name, fontSize=8,
                                    textColor=font_color, alignment=TA_CENTER)

    story = []
    story.append(Spacer(1, 4))
    story.append(Paragraph(report_title, title_style))
    if subtitle:
        story.append(Paragraph(subtitle, sub_style))
    if filters_summary:
        story.append(Paragraph(f"<i>Filters applied: {filters_summary}</i>", sub_style))
    story.append(Spacer(1, 6))

    # ---- KPI grid - 4 per row, styled as distinct "cards" ----
    if kpis:
        story.append(Paragraph("\u25A0 KEY PERFORMANCE INDICATORS", section_style))
        rows, row = [], []
        for k in kpis:
            cell = [Paragraph("KPI", kpi_eyebrow_style),
                    Paragraph(k["label"], kpi_label_style),
                    Paragraph(str(k["value"]), kpi_value_style),
                    Paragraph(k.get("sub", "") or "", kpi_sub_style)]
            row.append(cell)
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            while len(row) < 4:
                row.append([Paragraph("", kpi_label_style)])
            rows.append(row)

        table_data = [[_stack(c) for c in r] for r in rows]
        t = Table(table_data, colWidths=[doc.width / 4.0] * 4)
        style_cmds = [
            ("BOX", (0, 0), (-1, -1), 0.6, box_border),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, box_border),
            ("BACKGROUND", (0, 0), (-1, -1), card_bg),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        for r_idx in range(len(rows)):
            for c_idx in range(4):
                style_cmds.append(("LINEABOVE", (c_idx, r_idx), (c_idx, r_idx), 2.2, accent))
        t.setStyle(TableStyle(style_cmds))
        story.append(t)

    # ---- One chart per page, each clearly labelled with its TYPE ----
    for item in chart_items:
        story.append(PageBreak())
        ctype = (item.get("type") or "Chart")
        icon = CHART_TYPE_ICONS.get(ctype, CHART_TYPE_ICONS["Chart"])
        badge = Table([[Paragraph(f"{icon}  {ctype.upper()} CHART", badge_style)]],
                       colWidths=[4.6 * cm])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), accent),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
        story.append(badge)
        story.append(Spacer(1, 4))
        story.append(Paragraph(item["title"], section_style))
        if item.get("png_bytes"):
            img = Image(io.BytesIO(item["png_bytes"]), width=doc.width, height=doc.width * 0.46)
            story.append(img)
        if item.get("insight"):
            insight_box = Table(
                [[Paragraph("\U0001F4A1 Insight", insight_label_style)],
                 [Paragraph(item["insight"], insight_style)]],
                colWidths=[doc.width],
            )
            insight_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), card_bg),
                ("BOX", (0, 0), (-1, -1), 0.6, box_border),
                ("LINEBEFORE", (0, 0), (0, -1), 2.5, accent),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
            ]))
            story.append(Spacer(1, 6))
            story.append(insight_box)

    doc.build(story)
    return buf.getvalue()


def _stack(paragraphs):
    from reportlab.platypus import Table as InnerTable
    it = InnerTable([[p] for p in paragraphs])
    it.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("TOPPADDING", (0, 0), (-1, -1), 1),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return it
