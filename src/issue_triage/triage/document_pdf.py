"""Render a saved, citation-bearing summary document as a downloadable PDF."""
from __future__ import annotations

from html import escape
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


NAVY = colors.HexColor("#17324D")
TEAL = colors.HexColor("#2D7058")
PALE = colors.HexColor("#F1F5F8")
INK = colors.HexColor("#24313D")
MUTED = colors.HexColor("#5B6770")


def _paragraph(value: Any, style: ParagraphStyle) -> Paragraph:
    text = escape(str(value or "")).replace("\n", "<br/>")
    return Paragraph(text, style)


def _bullet(value: Any, style: ParagraphStyle) -> Paragraph:
    return Paragraph(f"&#8226;&nbsp; {escape(str(value or ''))}", style)


def build_summary_document_pdf(document: dict[str, Any]) -> bytes:
    """Create a PDF attachment from a persisted summary-document model."""
    output = BytesIO()
    pdf = SimpleDocTemplate(
        output,
        pagesize=letter,
        rightMargin=0.7 * inch,
        leftMargin=0.7 * inch,
        topMargin=0.72 * inch,
        bottomMargin=0.62 * inch,
        title=str(document.get("title") or "Cited summary document"),
        author="Issue Triage Assistant",
        subject="Synthetic, source-cited audit summary draft",
    )
    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SummaryTitle", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=21, leading=25, textColor=NAVY, alignment=0, spaceAfter=8,
    )
    section_style = ParagraphStyle(
        "SummarySection", parent=base["Heading2"], fontName="Helvetica-Bold",
        fontSize=13, leading=16, textColor=NAVY, spaceBefore=14, spaceAfter=6,
        keepWithNext=True,
    )
    body_style = ParagraphStyle(
        "SummaryBody", parent=base["BodyText"], fontName="Helvetica",
        fontSize=9.5, leading=13, textColor=INK, spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "SummaryMeta", parent=body_style, fontSize=8.5, leading=11,
        textColor=MUTED, spaceAfter=2,
    )
    citation_style = ParagraphStyle(
        "SummaryCitation", parent=body_style, fontSize=8, leading=10.5,
        textColor=MUTED, leftIndent=8, rightIndent=5, spaceAfter=3,
    )
    quote_style = ParagraphStyle(
        "SummaryQuote", parent=body_style, fontSize=8.5, leading=11,
        textColor=INK, leftIndent=8, rightIndent=5, spaceBefore=2, spaceAfter=3,
    )
    label_style = ParagraphStyle(
        "SummaryLabel", parent=body_style, fontName="Helvetica-Bold",
        fontSize=8.5, leading=11, textColor=NAVY,
    )

    story: list[Any] = []
    draft_header = str(document.get("draft_header") or "DRAFT - NOT FINAL - PENDING REVIEW")
    banner = Table([[_paragraph(draft_header, label_style)]], colWidths=[7.1 * inch])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#EAF2EE")),
        ("BOX", (0, 0), (-1, -1), 0.6, TEAL),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([banner, Spacer(1, 14), _paragraph(document.get("title", "Cited summary document"), title_style)])

    counts = document.get("counts") or {}
    audits = ", ".join(str(item) for item in document.get("audit_ids", []) if item) or "Not recorded"
    metadata = [
        ["Status", str(document.get("status", "draft")).replace("_", " ").title(), "Version", str(document.get("version", 1))],
        ["Synthetic client", str(document.get("client_id") or "Not recorded"), "RAU", str(document.get("rau_id") or "Not recorded")],
        ["Audit context", str(document.get("audit_id") or "Not specified"), "Source audits", audits],
        ["Template", str(document.get("template") or "Not recorded"), "Source issues", str(counts.get("total_issues", 0))],
    ]
    meta_table = Table(
        [[_paragraph(cell, meta_style) for cell in row] for row in metadata],
        colWidths=[1.05 * inch, 2.4 * inch, 1.05 * inch, 2.6 * inch],
        hAlign="LEFT",
    )
    meta_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), PALE),
        ("BACKGROUND", (2, 0), (2, -1), PALE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#CBD5DC")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([meta_table, Spacer(1, 10)])
    story.append(_paragraph(
        f"Coverage: {document.get('coverage') or 'Not recorded'} · "
        f"Repeat clusters: {counts.get('repeat_clusters', 0)} · "
        f"Open or overdue: {counts.get('open_overdue', 0)} · "
        f"Severity counts: {counts.get('by_severity', {})} · Status counts: {counts.get('by_status', {})}",
        meta_style,
    ))
    story.append(HRFlowable(width="100%", thickness=0.8, color=TEAL, spaceBefore=7, spaceAfter=8))

    for section in document.get("sections", []):
        story.append(_paragraph(section.get("heading", "Summary"), section_style))
        for bullet in section.get("bullets", []):
            story.append(_bullet(bullet.get("text", ""), body_style))
            for citation in bullet.get("citations", []):
                source_line = (
                    f"Source: {citation.get('doc_id', 'Unknown')} · "
                    f"Chunk: {citation.get('chunk_id', 'Unknown')} · "
                    f"Issue: {citation.get('issue_id') or 'Not recorded'} · "
                    f"Audit: {citation.get('audit_id') or 'Not recorded'} · "
                    f"System: {citation.get('source_system') or 'Retrieved source'} · "
                    f"Fetched: {citation.get('fetched_at') or 'Current retrieval'}"
                )
                story.append(_paragraph(source_line, citation_style))
                story.append(Paragraph(
                    f"Quoted source: &#8220;{escape(str(citation.get('quote', '')))}&#8221;",
                    quote_style,
                ))

    for heading, key in (
        ("Unresolved gaps", "unresolved_gaps"),
        ("Warnings", "warnings"),
        ("Suggested next actions - not executed", "suggested_next_actions"),
    ):
        values = document.get(key) or []
        if values:
            story.append(_paragraph(heading, section_style))
            for value in values:
                story.append(_bullet(value, body_style))

    story.append(Spacer(1, 12))
    story.append(_paragraph(
        "Citation note: exact quote checks confirm source text membership. The auditor must still decide whether each source supports its claim.",
        meta_style,
    ))

    def page_chrome(canvas, doc_state):
        canvas.saveState()
        width, height = letter
        canvas.setStrokeColor(colors.HexColor("#D8E0E5"))
        canvas.setLineWidth(0.45)
        canvas.line(0.7 * inch, 0.48 * inch, width - 0.7 * inch, 0.48 * inch)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.7 * inch, 0.32 * inch, "Synthetic audit summary · Human review required")
        canvas.drawRightString(width - 0.7 * inch, 0.32 * inch, f"Page {doc_state.page}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=page_chrome, onLaterPages=page_chrome)
    return output.getvalue()
