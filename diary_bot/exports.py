"""Monthly Excel and PDF exports of confirmed entries (English notes; Kannada original in Excel)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .utils import fmt_inr

INR_FMT = '[>=10000000]"₹"##\\,##\\,##\\,##0.00;[>=100000]"₹"##\\,##\\,##0.00;"₹"##,##0.00'
TEAL = "1F5F5B"


def _text_cell(ws, row: int, col: int, value):
    """Write text as a literal string so '=...' typed by speech/typo can never become a formula."""
    c = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str):
        c.data_type = "s"
    return c


def build_xlsx(rows, label: str, path: Path, integrity_ok: bool = True,
               opening_paise: int | None = None) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Diary"
    headers = ["Date", "Item (English)", "Item (ಕನ್ನಡ)", "Category", "Party", "Notes",
               "Expense", "Income", "Entry #"]
    thin = Side(style="thin", color="D0D7D6")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=TEAL)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border
    for r, row in enumerate(rows, 2):
        d = date.fromisoformat(row["entry_date"])
        vals = [d, row["item_en"], row["item_kn"] or "", row["category"], row["party"] or "",
                row["notes_en"] or "", None, None, row["entry_id"]]
        amount = Decimal(row["amount_paise"]) / 100
        vals[6 if row["direction"] == "expense" else 7] = amount
        for ci, v in enumerate(vals, 1):
            c = _text_cell(ws, r, ci, v)
            c.border = border
            c.alignment = Alignment(vertical="top", wrap_text=ci in (2, 3, 6))
            if ci == 1:
                c.number_format = "DD-MMM-YYYY"
            if ci in (7, 8):
                c.number_format = INR_FMT
    last = len(rows) + 1
    tot = last + 1
    ws.cell(row=tot, column=6, value="TOTAL").font = Font(bold=True)
    for col in (7, 8):
        L = get_column_letter(col)
        c = ws.cell(row=tot, column=col, value=f"=SUM({L}2:{L}{max(last, 2)})")
        c.font = Font(bold=True)
        c.number_format = INR_FMT
    for i, w in enumerate([13, 38, 30, 20, 22, 36, 16, 16, 9], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:I{max(last, 2)}"

    ss = wb.create_sheet("Summary")
    ss["A1"], ss["A1"].font = f"Summary - {label}", Font(bold=True, size=14, color=TEAL)
    for i, h in enumerate(["Category", "Expense", "Income"], 1):
        c = ss.cell(row=3, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=TEAL)
    cats = sorted({r["category"] for r in rows})
    rng = max(last, 2)
    r = 4
    for cat in cats:
        _text_cell(ss, r, 1, cat)
        ss.cell(row=r, column=2, value=f'=SUMIFS(Diary!$G$2:$G${rng},Diary!$D$2:$D${rng},$A{r})').number_format = INR_FMT
        ss.cell(row=r, column=3, value=f'=SUMIFS(Diary!$H$2:$H${rng},Diary!$D$2:$D${rng},$A{r})').number_format = INR_FMT
        r += 1
    ss.cell(row=r, column=1, value="TOTAL").font = Font(bold=True)
    for col, L in ((2, "B"), (3, "C")):
        c = ss.cell(row=r, column=col, value=f"=SUM({L}4:{L}{max(r - 1, 4)})")
        c.font = Font(bold=True)
        c.number_format = INR_FMT
    ss.cell(row=r + 1, column=1, value="NET (Income - Expense)").font = Font(bold=True)
    n = ss.cell(row=r + 1, column=3, value=f"=C{r}-B{r}")
    n.font = Font(bold=True)
    n.number_format = INR_FMT
    rr = r + 3
    if opening_paise is not None:
        ss.cell(row=rr, column=1, value="Opening balance (brought forward)")
        o = ss.cell(row=rr, column=3, value=Decimal(opening_paise) / 100)
        o.number_format = INR_FMT
        ss.cell(row=rr + 1, column=1, value="CLOSING BALANCE").font = Font(bold=True)
        cb = ss.cell(row=rr + 1, column=3, value=f"=C{rr}+C{r}-B{r}")
        cb.font = Font(bold=True)
        cb.number_format = INR_FMT
        rr += 3
    ss.cell(row=rr, column=1,
            value="Ledger integrity check: " + ("PASSED" if integrity_ok else "FAILED - do not rely on this file"))
    ss.cell(row=rr + 1, column=1, value=f"Generated {datetime.now().strftime('%d-%b-%Y %H:%M')}")
    ss.column_dimensions["A"].width = 28
    ss.column_dimensions["B"].width = 18
    ss.column_dimensions["C"].width = 18

    from openpyxl.worksheet.properties import PageSetupProperties

    for sheet in (ws, ss):  # print nicely: landscape, one page wide
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "1:1"

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _pdf(text) -> str:
    """Core PDF fonts are Latin-1; the English notes are plain English so this is lossless."""
    return escape((text or "").encode("latin-1", "replace").decode("latin-1"))


def build_pdf(rows, label: str, path: Path, integrity_ok: bool = True,
              opening_paise: int | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.5, leading=10.5)
    small = ParagraphStyle("small", parent=body, fontSize=7.2, leading=9, textColor=colors.HexColor("#5b6b69"))
    right = ParagraphStyle("right", parent=body, alignment=2)
    head = ParagraphStyle("head", parent=body, textColor=colors.white, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=styles["Title"], textColor=colors.HexColor("#" + TEAL), fontSize=18)
    generated = datetime.now().strftime("%d %b %Y, %H:%M")
    status = "Ledger integrity: verified" if integrity_ok else "LEDGER INTEGRITY CHECK FAILED"

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#5b6b69" if integrity_ok else "#b00020"))
        canvas.drawString(15 * mm, 9 * mm, f"{status}  |  Generated {generated}")
        canvas.drawRightString(195 * mm, 9 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story = [Paragraph("Daily Diary - Expenses &amp; Income", title),
             Paragraph(f"Period: <b>{_pdf(label)}</b>", body), Spacer(1, 6 * mm)]

    data = [[Paragraph(h, head) for h in ("Date", "Item", "Category", "Party", "Expense (Rs)", "Income (Rs)")]]
    exp_total = inc_total = 0
    for r in rows:
        d = date.fromisoformat(r["entry_date"]).strftime("%d %b %Y")
        item = Paragraph(_pdf(r["item_en"]) + (f"<br/><font size='7' color='#5b6b69'>{_pdf(r['notes_en'])}</font>" if r["notes_en"] else ""), body)
        e = fmt_inr(r["amount_paise"]) if r["direction"] == "expense" else ""
        i = fmt_inr(r["amount_paise"]) if r["direction"] == "income" else ""
        if r["direction"] == "expense":
            exp_total += r["amount_paise"]
        else:
            inc_total += r["amount_paise"]
        data.append([Paragraph(d, body), item, Paragraph(_pdf(r["category"]), body),
                     Paragraph(_pdf(r["party"]), body), Paragraph(e, right), Paragraph(i, right)])
    if not rows:
        data.append([Paragraph("No confirmed entries in this period.", body), "", "", "", "", ""])
    data.append(["", "", "", Paragraph("<b>Total</b>", body),
                 Paragraph(f"<b>{fmt_inr(exp_total)}</b>", right), Paragraph(f"<b>{fmt_inr(inc_total)}</b>", right)])
    t = Table(data, colWidths=[22 * mm, 58 * mm, 28 * mm, 24 * mm, 24 * mm, 24 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + TEAL)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f2f7f6")]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d7d6")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#" + TEAL)),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story += [t, Spacer(1, 6 * mm)]

    if opening_paise is not None:
        closing = opening_paise + inc_total - exp_total
        bt = Table([[Paragraph("Opening balance (brought forward)", body), Paragraph(fmt_inr(opening_paise), right)],
                    [Paragraph("<b>Closing balance</b>", body), Paragraph(f"<b>{fmt_inr(closing)}</b>", right)]],
                   colWidths=[70 * mm, 40 * mm])
        bt.setStyle(TableStyle([("LINEABOVE", (0, 1), (-1, 1), 0.8, colors.HexColor("#" + TEAL)),
                                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f2f7f6"))]))
        story += [bt, Spacer(1, 8 * mm)]
    else:
        story.append(Spacer(1, 2 * mm))

    cats: dict[str, list[int]] = {}
    for r in rows:
        c = cats.setdefault(r["category"], [0, 0])
        c[0 if r["direction"] == "expense" else 1] += r["amount_paise"]
    if cats:
        sdata = [[Paragraph(h, head) for h in ("Category summary", "Expense (Rs)", "Income (Rs)")]]
        for k in sorted(cats):
            sdata.append([Paragraph(_pdf(k), body), Paragraph(fmt_inr(cats[k][0]) if cats[k][0] else "", right),
                          Paragraph(fmt_inr(cats[k][1]) if cats[k][1] else "", right)])
        sdata.append([Paragraph("<b>Net (Income - Expense)</b>", body), "",
                      Paragraph(f"<b>{fmt_inr(inc_total - exp_total)}</b>", right)])
        st = Table(sdata, colWidths=[70 * mm, 40 * mm, 40 * mm])
        st.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#" + TEAL)),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d0d7d6")),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#" + TEAL)),
        ]))
        story.append(st)

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                      topMargin=15 * mm, bottomMargin=16 * mm,
                      title=f"Daily Diary {label}").build(story, onFirstPage=footer, onLaterPages=footer)
    return path
