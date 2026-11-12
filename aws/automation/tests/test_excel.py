import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from excel import LayoutError, col_index, col_letter, parse_address, rows_from_used_range, share_id  # noqa: E402

TITLE = ["", "", "", "", "", "Status (Red - need to Re-generate, Yellow - Video isn't available)", "", "WebTool Status", ""]
HEAD = ["Date", "Awarding Body", "Course Name", "Unit No", "Chapter No", "Done By", "URL", "Done by",
        "Added to the final folder"]


def grid():
    values = [
        TITLE, HEAD,
        [46318, "Focus Awards", "Level 5 Diploma", 14, 4, "Piyumi", "https://notebooklm.link.google/Bg5YUO8CHtmm", "nandika", "Yes"],
        [46335, "Focus Awards", "Level 5 Diploma", 15, 5, "Piyumi", "https://notebooklm.link.google/OwYjk2tkmv7Q", "", ""],
        ["", "", "", "", "", "", "", "", ""],
        [46335, "Qualifi", "Qualifi Level 3 Diploma", "16", "1", "Piyumi", "https://notebooklm.link.google/pUXCiBGi2Muv", "auto", "NO"],
    ]
    text = [[str(c) for c in r] for r in values]
    text[2][0] = "10/23/2026"
    text[3][0] = "11/9/2026"
    return values, text


class Columns(unittest.TestCase):
    def test_letters(self):
        for i, letter in [(0, "A"), (6, "G"), (8, "I"), (25, "Z"), (26, "AA"), (27, "AB")]:
            self.assertEqual(col_index(letter), i)
            self.assertEqual(col_letter(i), letter)

    def test_address(self):
        self.assertEqual(parse_address("'Chapterwise AI video log'!A1:K90"), (1, 0))
        self.assertEqual(parse_address("Sheet1!$C$5:$K$90"), (5, 2))

    def test_share_id(self):
        self.assertTrue(share_id("https://x.sharepoint.com/:x:/g/abc?e=1").startswith("u!"))
        self.assertNotIn("=", share_id("https://a"))


class Parse(unittest.TestCase):
    def test_rows(self):
        values, text = grid()
        rows = rows_from_used_range("'Log'!A1:I6", values, text)
        self.assertEqual([r.row for r in rows], [3, 4, 6])  # blank row 5 dropped
        r = rows[1]
        self.assertEqual((r.unit, r.chapter, r.done_by, r.added), ("15", "5", "", ""))
        self.assertEqual(r.url, "https://notebooklm.link.google/OwYjk2tkmv7Q")
        self.assertEqual(r.date, "11/9/2026")
        self.assertEqual(rows[2].course, "Level 3 Diploma")

    def test_used_range_starting_later(self):
        values, text = grid()
        # usedRange begins at row 3 col B -> header search must still work when the range starts at row 1
        rows = rows_from_used_range("'Log'!A1:I6", values, text)
        self.assertEqual(len(rows), 3)
        shifted_v = [r[1:] for r in values]
        shifted_t = [r[1:] for r in text]
        rows2 = rows_from_used_range("'Log'!B1:I6", shifted_v, shifted_t)
        self.assertEqual([(r.row, r.unit) for r in rows2], [(r.row, r.unit) for r in rows])

    def test_layout_change_refused(self):
        values, text = grid()
        values[1] = list(values[1])
        values[1][8] = "Notes"
        with self.assertRaises(LayoutError):
            rows_from_used_range("'Log'!A1:I6", values, text)
        values, text = grid()
        values[1] = ["Date", "URL"] + values[1][2:]
        values[1][6] = "Link"
        with self.assertRaises(LayoutError):
            rows_from_used_range("'Log'!A1:I6", values, text)


if __name__ == "__main__":
    unittest.main()
