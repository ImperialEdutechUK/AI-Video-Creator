#!/usr/bin/env python3
"""
Operate the sheet automation from a terminal (uses the deployer profile).

    py -3.12 aws\\scripts\\automation_admin.py dry-run     # what the next run WOULD do (no writes)
    py -3.12 aws\\scripts\\automation_admin.py run-now     # run the 30-minute step immediately
    py -3.12 aws\\scripts\\automation_admin.py status      # every row the bot is handling / handled
    py -3.12 aws\\scripts\\automation_admin.py summary     # send the daily summary email now
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os

PROJECT = os.environ.get("PROJECT", "slc-video-aws")


def main() -> None:
    import boto3
    from boto3.dynamodb.conditions import Attr

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["dry-run", "run-now", "status", "summary"])
    ap.add_argument("--profile", default=f"{PROJECT}-deployer")
    ap.add_argument("--region", default="eu-west-2")
    args = ap.parse_args()
    sess = boto3.Session(profile_name=args.profile, region_name=args.region)

    if args.command == "status":
        table = sess.resource("dynamodb").Table(f"{PROJECT}-jobs")
        items, kwargs = [], {}
        while True:
            resp = table.scan(FilterExpression=Attr("kind").eq("auto"), **kwargs)
            items += resp.get("Items", [])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        google = table.get_item(Key={"pk": "AUTOCFG#google"}).get("Item")
        if google:
            print(f"!! Google sign-in required: {google.get('reason', '')}")
        for i in sorted(items, key=lambda x: float(x.get("updated_at", 0)), reverse=True)[:60]:
            when = dt.datetime.fromtimestamp(float(i.get("updated_at", 0))).strftime("%d %b %H:%M")
            print(f"{when}  row {int(i.get('row', 0)):>4}  U{i.get('unit')}C{i.get('chapter'):<4} "
                  f"{i.get('qstate', '?'):<11} try {int(i.get('attempts', 0))}  {i.get('course_name', '')[:45]}")
            if i.get("error"):
                print(f"{'':>22}reason: {i['error'][:160]}")
        if not items:
            print("No rows handled yet.")
        return

    action = {"dry-run": "automation_dry_run", "run-now": "automation_tick", "summary": "automation_daily"}[args.command]
    resp = sess.client("lambda").invoke(FunctionName=f"{PROJECT}-api",
                                        Payload=json.dumps({"action": action}).encode())
    print(json.dumps(json.loads(resp["Payload"].read() or b"{}"), indent=2, default=str))


if __name__ == "__main__":
    main()
