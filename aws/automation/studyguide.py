"""
Study guides through the team's StudyGen website (https://study-gen.streamlit.app),
the same way a person makes one:

  upload the unit's chapter PDFs -> course name -> unit number ("03") ->
  output name -> cover image (the photo from a chapter PDF's cover page) ->
  Generate DOCX -> Download DOCX

then the footers are laid out like the team's final PDFs (see fix_footers) and
checked on the PDF Word makes from it (see check_footers).
"""
from __future__ import annotations

import copy
import logging
import re
import time
from pathlib import Path

from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PWTimeout

log = logging.getLogger("studyguide")

STUDYGEN_URL = "https://study-gen.streamlit.app/"
BRAND = "© South London College Ltd"
BRAND_EVEN = "©South London College Ltd"


class StudyGuideError(Exception):
    pass


def unit_label(unit: int) -> str:
    """StudyGen's 'Unit no' and the file name: 3 -> '03'."""
    return f"{int(unit):02d}"


def file_name(unit: int) -> str:
    return f"unit-{unit_label(unit)}"


# ── cover image ──────────────────────────────────────────────────────────

def cover_image(pdfs: list[Path], out: Path) -> Path:
    """The photo of a chapter PDF's cover page (the cover's text sits on top of
    it as real text, so the image itself has no chapter title). Largest image
    on page 1 of the first PDF that has one."""
    import fitz
    for pdf in pdfs:
        try:
            doc = fitz.open(str(pdf))
            if not len(doc):
                continue
            images = doc[0].get_images(full=True)
            best = max(images, key=lambda im: im[2] * im[3], default=None)
            if best and best[2] * best[3] >= 400 * 400:
                pix = fitz.Pixmap(doc, best[0])
                if pix.n - pix.alpha >= 4:  # CMYK -> RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix.save(str(out))
                return out
        except Exception as e:  # a broken PDF: try the next one
            log.warning("No cover image from %s: %s", pdf.name, e)
    raise StudyGuideError("None of the chapter PDFs has a cover photo on its first page.")


# ── the StudyGen website ─────────────────────────────────────────────────

def _app_frame(page: Page):
    """Streamlit Cloud shows the app inside an iframe (/~/+/); wake it up if it's asleep."""
    deadline = time.time() + 240
    while time.time() < deadline:
        wake = page.get_by_role("button", name=re.compile(r"get this app back up", re.I))
        if wake.count():
            log.info("StudyGen was asleep - waking it up")
            wake.first.click()
            page.wait_for_timeout(10_000)
            continue
        for f in page.frames:
            if "/~/+/" in f.url:
                try:
                    if f.locator("input[type=file]").count() >= 2:
                        return f
                except Exception:
                    pass
        page.wait_for_timeout(3_000)
    raise StudyGuideError("The StudyGen website did not load (form not found).")


def _fill(frame, label: str, value: str) -> None:
    box = frame.get_by_label(label, exact=True)
    box.fill(value)
    box.press("Tab")


def generate_docx(page: Page, pdfs: list[Path], course: str, unit: int, cover: Path, out_dir: Path,
                  timeout_s: int = 900) -> Path:
    page.goto(STUDYGEN_URL, wait_until="domcontentloaded", timeout=90_000)
    frame = _app_frame(page)
    inputs = frame.locator("input[type=file]")
    inputs.nth(0).set_input_files([str(p) for p in pdfs])
    _fill(frame, "Course name", course)
    _fill(frame, "Unit no", unit_label(unit))
    _fill(frame, "Output file name", file_name(unit))
    inputs.nth(1).set_input_files(str(cover))
    # every PDF and the cover must show as uploaded before generating
    deadline = time.time() + 180
    while time.time() < deadline:
        shown = frame.locator("[data-testid=stFileChipName], [data-testid=stFileUploaderFileName]").all_inner_texts()
        if len(shown) >= len(pdfs) + 1:
            break
        page.wait_for_timeout(2_000)
    else:
        raise StudyGuideError("StudyGen did not take all the uploaded files.")
    frame.get_by_role("button", name="Generate DOCX").click()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.wait_for_timeout(5_000)
        dl = frame.get_by_role("button", name="Download DOCX")
        if dl.count():
            with page.expect_download(timeout=60_000) as d:
                dl.first.click()
            out = out_dir / f"{file_name(unit)}.docx"
            d.value.save_as(str(out))
            if out.stat().st_size < 10_000:
                raise StudyGuideError("StudyGen's DOCX download was empty.")
            return out
        err = frame.locator("[data-testid=stException], [data-testid=stAlertContentError]")
        if err.count():
            raise StudyGuideError("StudyGen reported an error: " + " ".join(err.first.inner_text().split())[:300])
    raise StudyGuideError(f"StudyGen did not finish within {timeout_s // 60} minutes.")


# ── footers ──────────────────────────────────────────────────────────────
# The template lines its footers up with runs of spaces, so any course name
# longer or shorter than the template's pushes the parts out of place (e.g.
# "...(RQF)2 | P a g e"). The final study guides look like the team's reference:
#   odd pages:  "1 | Page"  <bold course name, centred>  "© South London College Ltd"
#   even pages: "©South London College Ltd"  <course name, centred>  "2 | Page"
# fix_footers rebuilds each footer as a borderless 3-column row under the teal
# line: left part | course name (centred, wraps onto a 2nd line when long) |
# right part, keeping every part's own template formatting (font, size, bold,
# grey). The course name has no awarding body and always stays on ONE line:
# when it's too long for the middle column, its text is made smaller (the team's
# choice) - see course_size.

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
TEAL = "1A99A0"
SIDE_TWIPS = 2100          # left / right columns (~105pt): fits "©South London College Ltd"
ODD_COURSE_HALF_POINTS = 18   # template: 9pt bold on odd pages
EVEN_COURSE_HALF_POINTS = 16  # template: 8pt on even pages
MIN_HALF_POINTS = 6           # never below 3pt


def text_width_pt(text: str, half_points: int, bold: bool) -> float:
    from garamond_widths import GARAMOND, GARAMOND_BOLD
    table = GARAMOND_BOLD if bold else GARAMOND
    avg = table.get("n", 500)
    return sum(table.get(ch, avg) for ch in text) / 1000 * half_points / 2


def course_size(course: str, page_width_twips: int, odd: bool, shrink_steps: int = 0) -> int:
    """Largest size (half-points, never above the template's) that keeps the
    course name on one line in the middle column; `shrink_steps` makes it a
    little smaller still (used when Word still wrapped it)."""
    room = (page_width_twips - 2 * SIDE_TWIPS) / 20 * 0.95   # pt, small safety margin
    size = ODD_COURSE_HALF_POINTS if odd else EVEN_COURSE_HALF_POINTS
    while size > MIN_HALF_POINTS and text_width_pt(course, size, bold=odd) > room:
        size -= 1
    return max(MIN_HALF_POINTS, size - shrink_steps)


def _q(tag: str) -> str:
    return f"{{{W}}}{tag}"


def _el(parent, tag: str, **attrs):
    from lxml import etree
    el = etree.SubElement(parent, _q(tag)) if parent is not None else etree.Element(_q(tag))
    for k, v in attrs.items():
        el.set(_q(k), str(v))
    return el


def _run_text(r) -> str:
    return "".join(t.text or "" for t in r.iter(_q("t")))


def _props(rpr, half_points: int | None = None):
    """A copy of run properties without character spacing, optionally resized."""
    rpr = copy.deepcopy(rpr) if rpr is not None else _el(None, "rPr")
    for el in rpr.findall(_q("spacing")):
        rpr.remove(el)
    if half_points:
        for tag in ("sz", "szCs"):
            el = rpr.find(_q(tag))
            if el is None:
                el = _el(rpr, tag)
            el.set(_q("val"), str(half_points))
    return rpr


def _new_run(rpr, text: str):
    r = _el(None, "r")
    r.append(rpr)
    t = _el(r, "t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    return r


def _field_span(runs) -> tuple[int, int]:
    """Indexes of the PAGE field's first and last run (fldChar begin .. end)."""
    def kind(r):
        f = r.find(_q("fldChar"))
        return f.get(_q("fldCharType")) if f is not None else None
    begin = next(i for i, r in enumerate(runs) if kind(r) == "begin")
    end = next(i for i, r in enumerate(runs) if i > begin and kind(r) == "end")
    return begin, end


def _cell(tr, width: int, align: str, runs, top: bool = True):
    tc = _el(tr, "tc")
    tcpr = _el(tc, "tcPr")
    _el(tcpr, "tcW", w=width, type="dxa")
    _el(tcpr, "vAlign", val="bottom")   # one shared baseline even when the course text is smaller
    p = _el(tc, "p")
    ppr = _el(p, "pPr")
    _el(ppr, "pStyle", val="Footer")
    _el(ppr, "spacing", before=0, after=0)
    _el(ppr, "jc", val=align)
    for r in runs:
        p.append(r)
    return tc


def _footer_table(width: int, left, middle, right):
    side = SIDE_TWIPS
    tbl = _el(None, "tbl")
    pr = _el(tbl, "tblPr")
    _el(pr, "tblW", w=width, type="dxa")
    _el(pr, "jc", val="left")
    borders = _el(pr, "tblBorders")   # no borders: the teal line stays the template's own (above)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        _el(borders, edge, val="nil")
    _el(pr, "tblLayout", type="fixed")
    mar = _el(pr, "tblCellMar")
    _el(mar, "top", w=40, type="dxa")
    _el(mar, "left", w=0, type="dxa")
    _el(mar, "bottom", w=0, type="dxa")
    _el(mar, "right", w=0, type="dxa")
    grid = _el(tbl, "tblGrid")
    for w in (side, width - 2 * side, side):
        _el(grid, "gridCol", w=w)
    tr = _el(tbl, "tr")
    _cell(tr, side, "left", left)
    _cell(tr, width - 2 * side, "center", middle)
    _cell(tr, side, "right", right)
    return tbl


def _rebuild(p, course: str, odd: bool, width: int, course_half_points: int) -> None:
    runs = list(p.iter(_q("r")))
    b, e = _field_span(runs)
    field, after = runs[b:e + 1], runs[e + 1:]
    for r in field:
        rpr = r.find(_q("rPr"))
        if rpr is not None:
            for sp in rpr.findall(_q("spacing")):
                rpr.remove(sp)
    brand = next((r for r in runs if "South London" in _run_text(r)), None)
    if brand is None:
        raise StudyGuideError("The footer has no 'South London College Ltd' text.")
    brand_rpr = brand.find(_q("rPr"))
    if odd:
        label = next((r for r in after if "Page" in _run_text(r)), None)
        if label is None:
            raise StudyGuideError("The odd-page footer has no '| Page' text.")
        label_rpr = label.find(_q("rPr"))
        left = field + [_new_run(_props(label_rpr), " | Page")]
        middle = [_new_run(_props(label_rpr, course_half_points), course)]
        right = [_new_run(_props(brand_rpr), BRAND)]
    else:
        texts = [r for r in after if _run_text(r).strip()]
        sep_rpr = texts[0].find(_q("rPr")) if texts else field[0].find(_q("rPr"))
        word_rpr = next((r.find(_q("rPr")) for r in texts if "Page" in _run_text(r)), sep_rpr)
        left = [_new_run(_props(brand_rpr), BRAND_EVEN)]
        middle = [_new_run(_props(brand_rpr, course_half_points), course)]
        right = field + [_new_run(_props(sep_rpr), " | "), _new_run(_props(word_rpr), "Page")]
    for r in runs:
        if r.getparent() is not None and r not in field:
            r.getparent().remove(r)
    # The original paragraph keeps the template's teal line (its top border) and
    # becomes an empty, tiny line above the row; another tiny paragraph follows
    # the row (a footer must end with a paragraph).
    for r in list(p.iter(_q("r"))):
        r.getparent().remove(r)
    _tiny(p)
    table = _footer_table(width, left, middle, right)
    p.addnext(table)
    end = _el(None, "p")
    _el(_el(end, "pPr"), "pStyle", val="Footer")
    _tiny(end)
    table.addnext(end)


def _tiny(p) -> None:
    ppr = p.find(_q("pPr"))
    if ppr is None:
        ppr = _el(None, "pPr")
        p.insert(0, ppr)
    for tag in ("tabs", "rPr", "spacing"):
        for el in ppr.findall(_q(tag)):
            ppr.remove(el)
    sp = _el(None, "spacing", before=0, after=0, line=40, lineRule="exact")
    ppr.append(sp)
    ppr.append(_props(None, 2))


def fix_footers(docx_in: Path, docx_out: Path, course: str, shrink_steps: int = 0) -> None:
    """Rebuild the odd/even footers (see above) and give the cover page its own
    empty footer, like the reference. `course` must already be without the
    awarding body."""
    from docx import Document
    doc = Document(str(docx_in))
    sec = doc.sections[0]
    width = int((sec.page_width - sec.left_margin - sec.right_margin) / 635)  # EMU -> twips
    done = set()
    for section in doc.sections:
        for footer, odd in ((section.footer, True), (section.even_page_footer, False)):
            el = footer._element
            if id(el) in done:
                continue
            done.add(id(el))
            paras = [p for p in el.iter(_q("p")) if "South London" in "".join(t.text or "" for t in p.iter(_q("t")))]
            if len(paras) != 1:
                raise StudyGuideError(f"Expected one footer line on {'odd' if odd else 'even'} pages, found {len(paras)}.")
            _rebuild(paras[0], course, odd, width, course_size(course, width, odd, shrink_steps))
        section.different_first_page_header_footer = True  # the cover shows no footer text
    doc.save(str(docx_out))


def _footer_spans(page) -> list[dict]:
    """Text spans below the footer's teal line (or in the bottom 8% if no line is found)."""
    H = page.rect.height
    top = H * 0.92
    for d in page.get_drawings():
        col = d.get("fill") or d.get("color")
        r = d.get("rect")
        if col and r and r.y0 > H * 0.8 and r.width > page.rect.width * 0.5 and \
                abs(col[0] - 0x1A / 255) < 0.08 and abs(col[1] - 0x99 / 255) < 0.08 and abs(col[2] - 0xA0 / 255) < 0.08:
            top = min(top, r.y0)
    return [s for b in page.get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]
            if s["bbox"][1] >= top - 1 and s["text"].strip(" ​")]


def check_footers(pdf: Path, course: str) -> list[str]:
    """Problems with the footers of the final PDF (empty list = all good).
    Every page after the cover needs a footer with its page number, the course
    name (centred, ONE line) and the copyright on alternating sides, nothing
    overlapping; the cover has no footer text."""
    import fitz
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    want = norm(course)
    doc = fitz.open(str(pdf))
    if len(doc) < 2:
        return ["The study guide has only a cover page."]
    problems = []
    if any("South London" in s["text"] for s in _footer_spans(doc[0])):
        problems.append("page 1 (cover): shows footer text")
    for i in range(1, len(doc)):
        page = doc[i]
        Wd = page.rect.width
        spans = _footer_spans(page)
        n = f"page {i + 1}"
        if not spans:
            problems.append(f"{n}: no footer")
            continue
        base = lambda s: s["origin"][1]    # baseline: same for big and small text on one line
        first_y = min(base(s) for s in spans)
        first = sorted([s for s in spans if base(s) - first_y < 2], key=lambda s: s["bbox"][0])
        text = " ".join(s["text"] for s in sorted(spans, key=lambda s: (round(s["bbox"][1]), s["bbox"][0])))
        if want not in norm(text):
            problems.append(f"{n}: the course name is missing or different in the footer")
        if "southlondoncollegeltd" not in norm(text):
            problems.append(f"{n}: '© South London College Ltd' is missing")
        if any(base(s) - first_y >= 2 for s in spans):
            problems.append(f"{n}: the footer runs onto two lines (course name too long)")
        by_line = {}
        for s in spans:
            by_line.setdefault(round(base(s) / 2), []).append(s)
        for row in by_line.values():
            row.sort(key=lambda s: s["bbox"][0])
            if any(b["bbox"][0] < a["bbox"][2] - 0.5 for a, b in zip(row, row[1:])):
                problems.append(f"{n}: footer texts overlap")
                break
        left = " ".join(s["text"] for s in first if s["bbox"][0] < Wd * 0.25).strip()
        right = " ".join(s["text"] for s in first if s["bbox"][2] > Wd * 0.75).strip()
        if i % 2 == 1:   # Word numbers the cover 0: PDF page 2 is "1 | Page", on the left
            if not re.match(rf"^{i}\s*\|\s*Page", left):
                problems.append(f"{n}: '{i} | Page' should be on the left")
            if "South London" not in right:
                problems.append(f"{n}: the copyright should be on the right")
        else:
            if not re.search(rf"{i}\s*\|\s*Page$", right):
                problems.append(f"{n}: '{i} | Page' should be on the right")
            if "South London" not in left:
                problems.append(f"{n}: the copyright should be on the left")
        middle = [s for s in spans if s["bbox"][0] > Wd * 0.2 and s["bbox"][2] < Wd * 0.8 and norm(s["text"])]
        if middle:
            c0, c1 = min(s["bbox"][0] for s in middle), max(s["bbox"][2] for s in middle)
            if abs((c0 + c1) / 2 - Wd / 2) > 12:
                problems.append(f"{n}: the course name is not centred")
    return problems
