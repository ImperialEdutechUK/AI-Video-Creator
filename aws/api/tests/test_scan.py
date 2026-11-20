"""automation.scan() on a sheet like the real log: repeated rows, shared
notebook links across courses, a start row, rows done by people."""
import os
import time
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "automation"))
for k, v in {"JOBS_TABLE": "scan-test-jobs", "MEDIA_BUCKET": "m", "WORKER_CLUSTER": "c", "WORKER_TASK_FAMILY": "f",
             "AWS_REGION": "eu-west-2", "AWS_DEFAULT_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "testing",
             "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing"}.items():
    os.environ[k] = v
os.environ.pop("AWS_PROFILE", None)

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

from rules import SheetRow  # noqa: E402

LINK = "https://notebook.google.com/notebook/shared-1"


def r(row, unit, chapter, course="Level 3 Certificate in Supporting Teaching", h="", i="", url=LINK):
    return SheetRow(row=row, date="", awarding_body="TQUK", course_raw=course, unit_raw=unit, chapter_raw=chapter,
                    url=url, done_by=h, added=i)


class FakeClient:
    def __init__(self, rows):
        self.rows, self.writes = rows, []

    def read_rows(self, wb):
        return list(self.rows)

    def write_cell(self, wb, col, row, value):
        self.writes.append((col, row, value))


@unittest.skipIf(mock_aws is None, "moto not installed")
class Scan(unittest.TestCase):
    def setUp(self):
        os.environ["JOBS_TABLE"] = "scan-test-jobs"
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3
        boto3.resource("dynamodb", region_name="eu-west-2").create_table(
            TableName="scan-test-jobs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "active", "AttributeType": "S"},
                                  {"AttributeName": "created_at", "AttributeType": "N"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{  # same as the real table (template.yaml)
                "IndexName": "active",
                "KeySchema": [{"AttributeName": "active", "KeyType": "HASH"},
                              {"AttributeName": "created_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["qstate", "sid"]}}])
        sys.modules.pop("automation", None)
        import automation
        from excel import Workbook
        self.a = automation
        self.wb = Workbook("drv", "itm", "Additional Material Tracker Sheet New.xlsx", "ws", "Chapter-wise AI video log")

    def run_scan(self, rows, start_row=0, per_run=5):
        client = FakeClient(rows)
        self.a.open_sheet = lambda: (client, self.wb)
        self.a.settings = lambda: {"since": None, "skip_colors": set(), "start_row": start_row}
        self.a.MAX_PER_RUN = per_run
        return self.a.scan(dry_run=False), client

    def test_repeated_rows_and_shared_links(self):
        rows = [
            r(10, 2, 4),                                               # claimed
            r(11, 2, 4),                                               # exact repeat of row 10 -> left alone
            r(12, 2, 4, course="Level 3 Diploma in Specialist Support"),  # same link+U2C4, other course -> claimed
            r(13, 2, 5, h="Menuka", i="Yes"),                          # done by a person
            r(14, 2, 5, h="Menuka", i="Yes"),                          # repeat of a person's row
        ]
        report, client = self.run_scan(rows)
        self.assertEqual([c["row"] for c in report["claimed"]], [10, 12])
        self.assertEqual(client.writes, [("H", 10, "auto"), ("H", 12, "auto")])
        self.assertEqual(report["other_skips"]["repeat of another row (same link, unit, chapter and course)"], 2)
        # next run: nothing new, both in progress, no crash on repeated keys
        report2, client2 = self.run_scan([r(10, 2, 4, h="auto"), r(11, 2, 4), r(12, 2, 4, course="Level 3 Diploma in Specialist Support", h="auto")])
        self.assertEqual(report2["claimed"], [])
        self.assertEqual(report2["in_progress"], 2)
        self.assertEqual(client2.writes, [])

    def test_person_takes_over_a_waiting_row(self):
        self.run_scan([r(10, 2, 4)])                                   # bot claims row 10
        report, client = self.run_scan([r(10, 2, 4, h="Menuka")])      # person writes their name over "auto"
        self.assertEqual(report.get("taken_over"), 1)
        pk = next(iter(self.a.table.scan()["Items"]))["pk"]
        item = self.a.table.get_item(Key={"pk": pk})["Item"]
        self.assertEqual(item["qstate"], "abandoned")
        self.assertNotIn("active", item)                               # the download task won't see it
        self.assertEqual(client.writes, [])

    def test_paused_while_google_signed_out(self):
        self.a.ENABLED = True
        self.a.table.put_item(Item={"pk": "AUTOCFG#google", "signin_required": True, "reason": "signed out"})
        sent = []
        self.a.alert = lambda kind, subject, message, *x, **k: sent.append(kind) or True
        client = FakeClient([r(10, 2, 4)])
        self.a.open_sheet = lambda: (client, self.wb)
        report = self.a.tick()
        self.assertEqual(report["paused"], "Google sign-in required")
        self.assertEqual(client.writes, [])                            # no row claimed while paused
        self.assertEqual(sent, ["google_signin"])
        # a row already waiting is not timed out into NO while paused
        self.a.table.put_item(Item={"pk": "AUTO#p", "qstate": "pending", "updated_at": 0, "row": 10})
        item = {"pk": "AUTO#p", "qstate": "pending", "updated_at": 0, "row": 10}
        self.a._advance(item, time.time(), lambda: None, google_paused=True)
        stored = self.a.table.get_item(Key={"pk": "AUTO#p"})["Item"]
        self.assertEqual(stored["qstate"], "pending")
        # after the sign-in the 2-hour limit starts again from now, not from last night
        self.a._advance(dict(stored), time.time() + 60, lambda: None, google_paused=False)
        self.assertEqual(self.a.table.get_item(Key={"pk": "AUTO#p"})["Item"]["qstate"], "pending")

    def test_keepalive_when_nothing_waits(self):
        self.a.ENABLED = True
        self.a.automation_root = lambda drive: None
        fake_handler = type(sys)("handler")
        fake_handler._drive_module = lambda: None
        from unittest import mock
        self.enterContext(mock.patch.dict(sys.modules, {"handler": fake_handler}))
        started = []
        self.a._start_download_task = lambda: started.append(1) or True
        client = FakeClient([r(10, 2, 4, h="Menuka", i="Yes")])      # nothing for the bot
        self.a.open_sheet = lambda: (client, self.wb)
        self.a.settings = lambda: {"since": None, "skip_colors": set(), "start_row": 0}
        self.a._active_auto_items = lambda: []
        self.a._session_age = lambda: 3 * 3600                        # fresh: no task
        self.assertFalse(self.a.tick()["download_task_started"])
        self.a._session_age = lambda: 7 * 3600                        # stale: refresh it
        report = self.a.tick()
        self.assertTrue(report["download_task_started"])
        self.assertIn("keepalive", report)
        self.assertEqual(started, [1])

    def test_reads_records_only_for_rows_that_need_them(self):
        # Rows the sheet settles (person, Yes, no URL, not NotebookLM) cost no reads;
        # a failed bot row (I = NO) still needs its record to decide on a retry.
        self.run_scan([r(20, 3, 1)])                                   # bot claims row 20
        pk20 = next(iter(self.a.table.scan()["Items"]))["pk"]
        self.a.table.update_item(Key={"pk": pk20}, UpdateExpression="SET qstate = :f, attempts = :one REMOVE active",
                                 ExpressionAttributeValues={":f": "failed", ":one": 1})
        rows = [r(10, 2, 4),                                           # new -> needs its record
                r(11, 2, 5, h="Menuka", i="Yes"),                       # done by a person
                r(12, 2, 6, h="auto", i="Yes"),                         # already added
                r(13, 2, 7, url=""),                                    # no URL
                r(14, 2, 8, url="https://example.com/x"),               # not NotebookLM
                r(20, 3, 1, h="auto", i="NO")]                          # failed -> retry needs the record
        asked = []
        real = self.a._batch_get
        self.a._batch_get = lambda pks: asked.extend(pks) or real(pks)
        self.addCleanup(setattr, self.a, "_batch_get", real)
        report, _ = self.run_scan(rows)
        key = lambda row: self.a.rules.row_key(self.wb.key, row.url, row.unit, row.chapter, row.course)
        self.assertEqual(sorted(asked), sorted([key(rows[0]), pk20]))  # no SKIP keys: colour check is off
        self.assertEqual([c["row"] for c in report["claimed"]], [10])
        self.assertEqual([c["row"] for c in report["retrying"]], [20])

    def test_start_row_order_and_limit(self):
        rows = [r(n, 1, n % 9 + 1, url=f"{LINK}-{n}") for n in (6340, 6345, 6646, 6647, 6650, 6700)]
        report, _ = self.run_scan(rows, start_row=6646, per_run=5)
        self.assertEqual([c["row"] for c in report["claimed"]], [6646, 6647, 6650, 6700, 6345])
        self.assertEqual(report["other_skips"]["waiting for a later run"], 1)


if __name__ == "__main__":
    unittest.main()
