"""
Read a course's unit chapters and their PDF links from the
southlondoncollege.org WordPress (WPLMS) backend.

Plain HTTPS with the saved sign-in cookies (no browser):
  1. public course page -> post id (shortlink ?p=ID) and "Awarded by ..."
  2. /wp-admin/post.php?post=ID&action=edit -> "Course Curriculum": an ordered
     list of section names ("Unit 1 - ...") and unit post ids ("Chapter 1 - ...")
  3. each chapter's edit page -> the [tnc-pdf-viewer-iframe file="...pdf"] link

Read-only: only GET requests, never saves anything in WordPress.
"""
from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

SITE = "https://southlondoncollege.org"


class WordPressError(Exception):
    """A problem with this unit's course pages (the unit is skipped)."""


class WordPressSignInRequired(Exception):
    """The saved WordPress session no longer works - ask for a sign-in."""


@dataclass
class Chapter:
    number: int
    title: str
    post_id: str
    pdf_urls: list[str]


@dataclass
class CourseUnit:
    course_post_id: str
    course_title: str
    awarding_body: str          # as shown on the public course page ("" if not found)
    unit_number: int
    unit_title: str
    chapters: list[Chapter]


_CURRICULUM_RE = re.compile(
    r'name="vibe_course_curriculum\[(\d+)\]"[^>]*?value="([^"]*)"[^>]*>\s*'
    r'(?:<a href="([^"]*)"><span>([^<]*)</span>)?', re.S)
_UNIT_SECTION_RE = re.compile(r"^\s*unit\s*0*(\d+)\b", re.I)
_CHAPTER_RE = re.compile(r"^\s*chapter\s*0*(\d+)\b", re.I)
# Extra items some units have next to each chapter ("Chapter 01: Quick Recap").
_NOT_A_CHAPTER_RE = re.compile(r"(?i)\b(quick\s*recap|recap|summary|podcast|quiz)\b")
_PDF_RE = re.compile(r'''file\s*=\s*["']([^"']+?\.pdf)["']''', re.I)
_ANY_PDF_RE = re.compile(r'''https?://[^\s"'<>\]]+?\.pdf''', re.I)


def _text(s: str) -> str:
    return " ".join(html.unescape(s).replace(" ", " ").split())


class WordPress:
    def __init__(self, cookies: list[dict], user_agent: str, timeout: int = 60):
        self.cookie = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                                if "southlondoncollege.org" in c.get("domain", ""))
        self.user_agent = user_agent or "Mozilla/5.0"
        self.timeout = timeout

    def get(self, url: str, auth: bool = True, retries: int = 2) -> tuple[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/xhtml+xml"}
        if auth:
            headers["Cookie"] = self.cookie
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.geturl(), r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                if e.code == 404:
                    raise WordPressError(f"Page not found: {url}") from None
                if e.code in (401, 403) and auth:
                    raise WordPressSignInRequired(f"WordPress refused the saved sign-in ({e.code}).") from None
                raise WordPressError(f"WordPress returned {e.code} for {url}") from None
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise WordPressError(f"Could not reach WordPress: {e.reason}") from None
        raise WordPressError("unreachable")

    def admin_get(self, url: str) -> str:
        final, body = self.get(url)
        if "wp-login.php" in final or 'id="loginform"' in body:
            raise WordPressSignInRequired("WordPress asked to sign in again.")
        return body

    # ── course page ──
    def course_info(self, course_url: str) -> tuple[str, str, str]:
        """(post id, title, awarding body) from the public course page."""
        if not course_url.startswith(SITE):
            raise WordPressError(f"Not a southlondoncollege.org course link: {course_url[:80]}")
        _, page = self.get(course_url, auth=False)
        m = re.search(r"""rel=['"]shortlink['"]\s+href=['"][^'"]*[?&]p=(\d+)""", page)
        if not m:
            raise WordPressError("Could not find the course's WordPress id on the course page.")
        title = ""
        t = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
        if t:
            title = _text(re.sub(r"<[^>]+>", " ", t.group(1)))
        ab = re.search(r"""(?i)awarded\s+by\s*['"‘’“”]+\s*([^'"‘’“”<]{2,40}?)\s*['"‘’“”]""", page)
        return m.group(1), title, _text(ab.group(1)) if ab else ""

    # ── curriculum ──
    def curriculum(self, post_id: str) -> list[tuple[str, str, str]]:
        """Ordered [(kind, value, title)]: ("section", name, "") or ("item", post id, title)."""
        body = self.admin_get(f"{SITE}/wp-admin/post.php?post={post_id}&action=edit")
        out = []
        for _, value, href, span in _CURRICULUM_RE.findall(body):
            value = _text(value)
            if href and value.isdigit():
                out.append(("item", value, _text(span)))
            elif value:
                out.append(("section", value, ""))
        if not out:
            raise WordPressError("The course editor has no 'Course Curriculum' list.")
        return out

    def chapter_pdfs(self, post_id: str) -> list[str]:
        body = self.admin_get(f"{SITE}/wp-admin/post.php?post={post_id}&action=edit")
        m = re.search(r'<textarea[^>]*id="content"[^>]*>(.*?)</textarea>', body, re.S)
        content = html.unescape(m.group(1)) if m else ""
        urls = _PDF_RE.findall(content) or _ANY_PDF_RE.findall(content)
        seen, out = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def unit(self, course_url: str, unit_number: int) -> CourseUnit:
        post_id, title, ab = self.course_info(course_url)
        items = self.curriculum(post_id)
        sections = [(i, v) for i, (k, v, _) in enumerate(items) if k == "section"]
        hits = [(i, v) for i, v in sections if (m := _UNIT_SECTION_RE.match(v)) and int(m.group(1)) == unit_number]
        if not hits:
            names = "; ".join(v for _, v in sections if _UNIT_SECTION_RE.match(v))[:300]
            raise WordPressError(f"No 'Unit {unit_number}' section in the course curriculum (units found: {names or 'none'}).")
        if len(hits) > 1:
            raise WordPressError(f"The course curriculum has {len(hits)} sections for Unit {unit_number}.")
        start, unit_title = hits[0]
        chapters: list[Chapter] = []
        for kind, value, item_title in items[start + 1:]:
            if kind == "section":
                break
            m = _CHAPTER_RE.match(item_title)
            if m and not _NOT_A_CHAPTER_RE.search(item_title):
                chapters.append(Chapter(int(m.group(1)), item_title, value, []))
        if not chapters:
            raise WordPressError(f"Unit {unit_number} has no 'Chapter ...' items in the course curriculum.")
        numbers = [c.number for c in chapters]
        if len(set(numbers)) != len(numbers):
            raise WordPressError(f"Unit {unit_number} has repeated chapter numbers: {numbers}.")
        for ch in chapters:
            ch.pdf_urls = self.chapter_pdfs(ch.post_id)
        return CourseUnit(post_id, title, ab, unit_number, unit_title, chapters)
