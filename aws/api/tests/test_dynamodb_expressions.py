"""
Runs every DynamoDB update the automation makes against moto (a local
DynamoDB emulator that validates UpdateExpression syntax like AWS does).
Catches broken expressions that fake-table tests can't see.
Skipped if moto isn't installed.
"""
import os
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "automation"))
for k, v in {"JOBS_TABLE": "expr-test-jobs", "MEDIA_BUCKET": "m", "WORKER_CLUSTER": "c", "WORKER_TASK_FAMILY": "f",
             "AWS_REGION": "eu-west-2", "AWS_DEFAULT_REGION": "eu-west-2", "AWS_ACCESS_KEY_ID": "testing",
             "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing"}.items():
    os.environ[k] = v
os.environ.pop("AWS_PROFILE", None)

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None


@unittest.skipIf(mock_aws is None, "moto not installed")
class Expressions(unittest.TestCase):
    def setUp(self):
        os.environ["JOBS_TABLE"] = "expr-test-jobs"  # other test files set their own name at import
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        import boto3
        ddb = boto3.resource("dynamodb", region_name="eu-west-2")
        ddb.create_table(
            TableName="expr-test-jobs", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": t} for n, t in
                                  (("pk", "S"), ("active", "S"), ("created_at", "N"))],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "active",
                "KeySchema": [{"AttributeName": "active", "KeyType": "HASH"},
                              {"AttributeName": "created_at", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["qstate", "sid"]},
            }],
        )
        # Fresh imports bound to the mocked table.
        for m in ("automation", "runner"):
            sys.modules.pop(m, None)
        import automation
        self.a = automation
        self.table = ddb.Table("expr-test-jobs")

    def put(self, state, **kw):
        item = {"pk": "AUTO#x", "kind": "auto", "active": "A", "qstate": state,
                "created_at": Decimal("1"), "updated_at": Decimal("1"), "attempts": 1, "row": 3,
                "owner_id": "o1", "unit": "10", "chapter": "2"}
        item.update(kw)
        self.table.put_item(Item=item)
        return item

    def get(self):
        return self.table.get_item(Key={"pk": "AUTO#x"})["Item"]

    def test_api_state_updates(self):
        a = self.a
        cases = [
            ("downloading", "SET qstate = :p REMOVE owner_id", {":p": "pending"}, None, "pending"),
            ("processing", "SET qstate = :s, saving_since = :now", {":s": "saving"}, None, "saving"),
            ("saving", "SET qstate = :v", {":v": "verifying"}, None, "verifying"),
            ("verifying", "SET qstate = :w, drive_link = :l, drive_file_id = :f",
             {":w": "writing", ":l": "L", ":f": "F"}, None, "writing"),
            ("writing", "SET qstate = :d, finished_at = :now, #ttl = :ttl REMOVE active",
             {":d": "done", ":ttl": 5}, {"#ttl": "ttl"}, "done"),
        ]
        for start, expr, values, names, want in cases:
            self.put(start)
            self.assertTrue(a._set("AUTO#x", start, expr, values, names), expr)
            got = self.get()
            self.assertEqual(got["qstate"], want, expr)
            self.assertGreater(float(got["updated_at"]), 1)

    def test_api_fail_paths(self):
        a = self.a
        for count in (False, True):
            it = self.put("pending", attempts=1)
            a._fail(it, "boom", count_attempt=count)
            got = self.get()
            self.assertEqual((got["qstate"], got["error"]), ("failing", "boom"))
            self.assertEqual(int(got["attempts"]), 2 if count else 1)

    def test_api_requeue(self):
        self.put("failed", job_id="j", error="e", ttl=5)
        self.a._requeue("AUTO#x")
        got = self.get()
        self.assertEqual((got["qstate"], got["active"]), ("pending", "A"))
        self.assertNotIn("error", got)

    def test_runner_updates(self):
        # runner imports playwright/notebooklm; stub them if absent
        try:
            import runner
        except ImportError as e:
            self.skipTest(f"runner deps missing: {e}")
        runner.table = self.table
        runner.OWNER = "o1"
        it = self.put("downloading")
        runner.heartbeat("AUTO#x")
        runner.mark_failing(it, "nope")
        self.assertEqual(self.get()["qstate"], "failing")
        it = self.put("downloading")
        runner.release(it)
        got = self.get()
        self.assertEqual((got["qstate"], int(got["attempts"])), ("pending", 0))
        self.put("downloading")
        self.assertTrue(runner._update_item(
            "AUTO#x", "SET qstate = :pr, job_id = :j, video_title = :t, notebook_id = :n REMOVE owner_id",
            {":pr": "processing", ":j": "j1", ":t": "U10C2", ":n": "nb"}))
        self.assertEqual(self.get()["qstate"], "processing")
        # claim_next uses its own expression
        self.put("pending", created_at=Decimal(str(time.time())))
        claimed = runner.claim_next()
        self.assertEqual(claimed["qstate"], "downloading")


if __name__ == "__main__":
    unittest.main()
