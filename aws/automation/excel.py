"""
Microsoft Graph client for the "Chapterwise AI video log" Excel workbook on
SharePoint / OneDrive. Standard library only (urllib), so the API Lambda
needs no extra packages.

Auth: delegated OAuth with a refresh token obtained once by
aws/scripts/automation_microsoft_signin.py (device-code sign-in). Microsoft
rotates refresh tokens, so every new one is handed to `on_new_refresh_token`
to be stored again; used every 30 minutes it never expires.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from rules import SheetRow, classify_color, norm_space

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Files.ReadWrite.All"

# Fixed columns of the log sheet (the team's layout).
COL = {"date": "A", "awarding_body": "B", "course": "C", "unit": "D", "chapter": "E",
       "created_by": "F", "url": "G", "done_by": "H", "added": "I"}


class GraphError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Microsoft Graph error {status}: {message}")
        self.status = status


class AuthError(GraphError):
    """The stored Microsoft sign-in no longer works - someone must sign in again."""


class LayoutError(RuntimeError):
    """The sheet doesn't look like the expected log layout - refuse to write."""


def col_index(letter: str) -> int:
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_letter(index: int) -> str:
    s, index = "", index + 1
    while index:
        index, rem = divmod(index - 1, 26)
        s = chr(65 + rem) + s
    return s


def parse_address(address: str) -> tuple[int, int]:
    """"'Sheet 1'!B3:K90" -> (row 3, column index 1) of the top-left cell."""
    cell = address.split("!")[-1].split(":")[0].replace("$", "")
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", cell)
    if not m:
        raise LayoutError(f"Unexpected range address {address!r}")
    return int(m.group(2)), col_index(m.group(1))


def share_id(url: str) -> str:
    """Graph "shares" id for a sharing link (u! + unpadded base64url)."""
    return "u!" + base64.urlsafe_b64encode(url.strip().encode()).decode().rstrip("=")


@dataclass
class Workbook:
    drive_id: str
    item_id: str
    name: str
    worksheet_id: str
    worksheet_name: str

    @property
    def key(self) -> str:
        return f"{self.drive_id}:{self.item_id}"


class GraphClient:
    def __init__(self, tenant: str, client_id: str, refresh_token: str,
                 on_new_refresh_token: Callable[[str], None] | None = None, timeout: int = 30):
        self.tenant = tenant or "organizations"
        self.client_id = client_id
        self.refresh_token = refresh_token
        self.on_new_refresh_token = on_new_refresh_token
        self.timeout = timeout
        self._token = ""
        self._token_expires = 0.0

    # ── auth ──
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 120:
            return self._token
        if not (self.client_id and self.refresh_token):
            raise AuthError(401, "Microsoft sign-in is not set up (run automation_microsoft_signin.py)")
        body = urllib.parse.urlencode({
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "scope": SCOPES,
        }).encode()
        url = f"https://login.microsoftonline.com/{urllib.parse.quote(self.tenant)}/oauth2/v2.0/token"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = _error_text(e)
            raise AuthError(e.code, f"Microsoft sign-in refresh failed: {detail}") from None
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        new_rt = data.get("refresh_token")
        if new_rt and new_rt != self.refresh_token:
            self.refresh_token = new_rt
            if self.on_new_refresh_token:
                try:
                    self.on_new_refresh_token(new_rt)
                except Exception:
                    log.exception("Could not store the rotated Microsoft refresh token")
        return self._token

    def request(self, method: str, path: str, body: dict | None = None, retries: int = 3) -> dict:
        url = path if path.startswith("https://") else GRAPH + path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = _error_text(e)
                # 429/5xx: throttled or busy. 423/409: workbook locked or being
                # co-authored right now - wait briefly and retry.
                if e.code in (409, 423, 429, 500, 502, 503, 504) and attempt < retries:
                    wait = int(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
                    time.sleep(min(wait, 20))
                    continue
                if e.code == 401:
                    self._token = ""
                    raise AuthError(401, detail) from None
                raise GraphError(e.code, detail) from None
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise GraphError(0, str(e.reason)) from None
        raise GraphError(0, "unreachable")

    # ── Word -> PDF (Word for the web renders it, with the document's real fonts) ──
    def docx_to_pdf(self, docx: bytes, name: str, folder: str = "SLC automation temp") -> bytes:
        """Upload `docx` to a temporary file in the signed-in account's OneDrive,
        download it converted to PDF, then delete the temporary file."""
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "-", name).strip() or "document"
        path = urllib.parse.quote(f"{folder}/{int(time.time())}-{safe}.docx")
        item = self._raw("PUT", f"{GRAPH}/me/drive/root:/{path}:/content", docx,
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        item_id = json.loads(item)["id"]
        try:
            pdf = b""
            for attempt in range(4):  # conversion can be briefly unavailable right after the upload
                try:
                    pdf = self._raw("GET", f"{GRAPH}/me/drive/items/{item_id}/content?format=pdf")
                    break
                except GraphError as e:
                    if attempt == 3 or e.code not in (404, 406, 409, 423, 429, 500, 502, 503, 504):
                        raise
                    time.sleep(3 * (attempt + 1))
            if not pdf.startswith(b"%PDF"):
                raise GraphError(0, "Word did not return a PDF")
            return pdf
        finally:
            try:
                self._raw("DELETE", f"{GRAPH}/me/drive/items/{item_id}")
            except GraphError:
                pass

    def _raw(self, method: str, url: str, data: bytes | None = None, content_type: str = "") -> bytes:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 120)) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise GraphError(e.code, _error_text(e)) from None
        except urllib.error.URLError as e:
            raise GraphError(0, str(e.reason)) from None

    # ── workbook ──
    def open_workbook(self, share_url: str, tab_hint: str = "chapterwise") -> Workbook:
        item = self.request("GET", f"/shares/{share_id(share_url)}/driveItem?$select=id,name,parentReference")
        drive_id = item["parentReference"]["driveId"]
        base = f"/drives/{drive_id}/items/{item['id']}/workbook"
        sheets = self.request("GET", f"{base}/worksheets?$select=id,name").get("value", [])
        hint = (tab_hint or "").strip().lower()
        exact = [s for s in sheets if s["name"].strip().lower() == hint]
        partial = [s for s in sheets if hint and hint in s["name"].lower()]
        chosen = exact or partial
        if len(chosen) != 1:
            names = ", ".join(s["name"] for s in sheets)
            raise LayoutError(
                f"Could not pick the log tab using {tab_hint!r} "
                f"({'none' if not chosen else 'several'} match). Tabs: {names}"
            )
        ws = chosen[0]
        return Workbook(drive_id, item["id"], item.get("name", ""), ws["id"], ws["name"])

    def _ws(self, wb: Workbook) -> str:
        return (f"/drives/{wb.drive_id}/items/{wb.item_id}/workbook/worksheets/"
                f"{urllib.parse.quote(wb.worksheet_id, safe='')}")

    def read_rows(self, wb: Workbook) -> list[SheetRow]:
        data = self.request("GET", f"{self._ws(wb)}/usedRange(valuesOnly=true)?$select=address,values,text")
        return rows_from_used_range(data.get("address", "A1"), data.get("values") or [], data.get("text") or [])

    def read_row(self, wb: Workbook, row: int) -> SheetRow | None:
        data = self.request("GET", f"{self._ws(wb)}/range(address='A{row}:I{row}')?$select=address,values,text")
        values, text = data.get("values") or [[]], data.get("text") or [[]]
        rows = _rows_from_grid(row, 0, values, text)
        return rows[0] if rows else None

    def cell_colors(self, wb: Workbook, row: int, fill_cols=("F", "G"), font_cols=("G",)) -> list[str]:
        """Colour names of the status cells of one row (see rules.classify_color)."""
        found = []
        for col in fill_cols:
            fill = self.request("GET", f"{self._ws(wb)}/range(address='{col}{row}')/format/fill")
            c = classify_color(fill.get("color"))
            if c:
                found.append(c)
        for col in font_cols:
            font = self.request("GET", f"{self._ws(wb)}/range(address='{col}{row}')/format/font")
            c = classify_color(font.get("color"))
            if c and c != "blue":  # hyperlinks are blue by default
                found.append(c)
        return found

    def write_cell(self, wb: Workbook, col: str, row: int, value: str) -> None:
        self.request("PATCH", f"{self._ws(wb)}/range(address='{col}{row}')", {"values": [[value]]})

    # ── generic helpers (other tabs, appending rows) ──
    def open_tab(self, wb: Workbook, tab_name: str) -> Workbook:
        """The same workbook, another tab (exact name, spaces ignored at the ends)."""
        base = f"/drives/{wb.drive_id}/items/{wb.item_id}/workbook"
        sheets = self.request("GET", f"{base}/worksheets?$select=id,name").get("value", [])
        hits = [s for s in sheets if s["name"].strip().lower() == tab_name.strip().lower()]
        if len(hits) != 1:
            raise LayoutError(f"Tab {tab_name!r} not found (tabs: {', '.join(s['name'] for s in sheets)})")
        return Workbook(wb.drive_id, wb.item_id, wb.name, hits[0]["id"], hits[0]["name"])

    def read_grid(self, wb: Workbook) -> tuple[int, list[list[str]]]:
        """(first row number, text of every used row from column A)."""
        data = self.request("GET", f"{self._ws(wb)}/usedRange(valuesOnly=true)?$select=address,text")
        first_row, first_col = parse_address(data.get("address", "A1"))
        pad = [""] * (first_col - 1)
        return first_row, [pad + [str(x) for x in r] for r in (data.get("text") or [])]

    def read_range(self, wb: Workbook, address: str) -> list[list[str]]:
        data = self.request("GET", f"{self._ws(wb)}/range(address='{address}')?$select=text")
        return [[str(x) for x in r] for r in (data.get("text") or [])]

    def write_range(self, wb: Workbook, address: str, values: list[list[str]]) -> None:
        self.request("PATCH", f"{self._ws(wb)}/range(address='{address}')", {"values": values})

    def last_used_row(self, wb: Workbook) -> int:
        data = self.request("GET", f"{self._ws(wb)}/usedRange(valuesOnly=true)?$select=address")
        last = data.get("address", "A1").split("!")[-1].split(":")[-1].replace("$", "")
        m = re.fullmatch(r"[A-Za-z]+(\d+)", last)
        return int(m.group(1)) if m else 1


def _error_text(e: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(e.read() or b"{}")
    except ValueError:
        return e.reason or str(e.code)
    err = payload.get("error")
    if isinstance(err, dict):
        return f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
    return f"{err or ''} {payload.get('error_description', '')}".strip()[:300] or str(e.code)


# ── parsing ──────────────────────────────────────────────────────────────

def _find_header_row(values: list[list], first_row: int, first_col: int) -> int:
    g = col_index(COL["url"]) - first_col
    for i, row in enumerate(values[:15]):
        if 0 <= g < len(row) and norm_space(row[g]).lower() == "url":
            return first_row + i
    raise LayoutError("Could not find the header row: expected 'URL' in column G")


def _check_header(values: list[list], header_idx: int, first_col: int) -> None:
    """Refuse to touch a sheet whose columns have moved."""
    def cell(r, letter):
        c = col_index(letter) - first_col
        row = values[r] if 0 <= r < len(values) else []
        return norm_space(row[c]).lower() if 0 <= c < len(row) else ""

    window = range(max(0, header_idx - 2), header_idx + 1)  # headers may span 2-3 rows
    h = " ".join(cell(r, COL["done_by"]) for r in window)
    i = " ".join(cell(r, COL["added"]) for r in window)
    d = " ".join(cell(r, COL["unit"]) for r in window)
    e = " ".join(cell(r, COL["chapter"]) for r in window)
    problems = []
    if "done by" not in h:
        problems.append(f"column H header is {h!r}, expected 'Done by'")
    if "final folder" not in i and "added" not in i:
        problems.append(f"column I header is {i!r}, expected 'Added to the final folder'")
    if "unit" not in d:
        problems.append(f"column D header is {d!r}, expected 'Unit No'")
    if "chapter" not in e:
        problems.append(f"column E header is {e!r}, expected 'Chapter No'")
    if problems:
        raise LayoutError("The sheet layout changed: " + "; ".join(problems))


def rows_from_used_range(address: str, values: list[list], text: list[list]) -> list[SheetRow]:
    first_row, first_col = parse_address(address)
    if first_col > col_index("A"):
        # usedRange starts after column A: pad so letters still line up.
        values = [[""] * first_col + list(r) for r in values]
        text = [[""] * first_col + list(r) for r in text]
        first_col = 0
    header_row = _find_header_row(values, first_row, first_col)
    _check_header(values, header_row - first_row, first_col)
    start = header_row - first_row + 1
    return _rows_from_grid(header_row + 1, first_col, values[start:], text[start:])


def _rows_from_grid(first_row: int, first_col: int, values: list[list], text: list[list]) -> list[SheetRow]:
    out = []
    for i, vals in enumerate(values):
        txt = text[i] if i < len(text) else []

        def v(letter):
            c = col_index(letter) - first_col
            return vals[c] if 0 <= c < len(vals) else ""

        def t(letter):
            c = col_index(letter) - first_col
            return norm_space(txt[c]) if 0 <= c < len(txt) else norm_space(v(letter))

        if not any(norm_space(x) for x in vals):
            continue
        out.append(SheetRow(
            row=first_row + i,
            date=t(COL["date"]),
            awarding_body=t(COL["awarding_body"]),
            course_raw=t(COL["course"]),
            unit_raw=v(COL["unit"]),
            chapter_raw=v(COL["chapter"]),
            url=t(COL["url"]),
            done_by=t(COL["done_by"]),
            added=t(COL["added"]),
            date_value=v(COL["date"]),
        ))
    return out
