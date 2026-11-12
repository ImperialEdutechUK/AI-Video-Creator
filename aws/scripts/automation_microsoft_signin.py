#!/usr/bin/env python3
"""
Connect the video automation to the Excel log on SharePoint/OneDrive.

Run it yourself:

    py -3.12 -m pip install boto3                        (once)
    py -3.12 aws\\scripts\\automation_microsoft_signin.py

It asks for:
  * Directory (tenant) ID and Application (client) ID of the Entra app
    registration (see aws/README.md, "Sheet automation" -> step 1)
  * the sharing link of the workbook. While testing this MUST be a COPY whose
    file name contains TEST - the automation refuses any other workbook until
    cutover.
  * the tab name (default: the tab whose name contains "chapterwise")
  * optional start date: rows dated before it are ignored (avoids old rows)
  * the Google Drive folder for the automation's uploads. Only automated rows
    use it (the web app keeps its own folder). While testing it MUST be a new,
    separate TEST folder - the API refuses the folder the team uses.

Then it shows a short code: open https://microsoft.com/devicelogin, enter it
and sign in with a Microsoft account that can EDIT the workbook. Only a
refresh token is stored (SSM Parameter Store, SecureString) - no password.

Finally it opens the workbook READ-ONLY and lists the rows the automation
would pick up, so you can check the course-name clean-up before anything runs.

    --check-only   skip the questions and sign-in; just re-run the read-only check
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "aws" / "automation"))

import rules  # noqa: E402
from excel import SCOPES, GraphClient  # noqa: E402

PROJECT = os.environ.get("PROJECT", "slc-video-aws")
PREFIX = f"/{PROJECT}/automation"


def post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def device_code_sign_in(tenant: str, client_id: str) -> str:
    base = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant)}/oauth2/v2.0"
    dc = post_form(f"{base}/devicecode", {"client_id": client_id, "scope": SCOPES})
    if "device_code" not in dc:
        sys.exit(f"Could not start the sign-in: {dc.get('error')}: {dc.get('error_description', '')[:300]}")
    print("\n" + dc["message"] + "\n")
    deadline = time.time() + int(dc.get("expires_in", 900))
    interval = int(dc.get("interval", 5))
    while time.time() < deadline:
        time.sleep(interval)
        tok = post_form(f"{base}/token", {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": dc["device_code"],
        })
        if "refresh_token" in tok:
            return tok["refresh_token"]
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        sys.exit(f"Sign-in failed: {err}: {tok.get('error_description', '')[:400]}")
    sys.exit("The sign-in code expired - run the script again.")


def ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def main() -> None:
    import boto3

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=f"{PROJECT}-deployer")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()

    ssm = boto3.Session(profile_name=args.profile, region_name=args.region).client("ssm")

    def put(name: str, value: str, secure: bool = False) -> None:
        ssm.put_parameter(Name=f"{PREFIX}/{name}", Value=value, Overwrite=True,
                          Type="SecureString" if secure else "String")

    def get_all() -> dict:
        names = [f"{PREFIX}/{n}" for n in ("ms/tenant", "ms/client_id", "ms/refresh_token",
                                           "sheet_url", "sheet_tab", "since_date", "drive_folder_url")]
        resp = ssm.get_parameters(Names=names, WithDecryption=True)
        return {p["Name"].removeprefix(PREFIX + "/"): p["Value"] for p in resp["Parameters"]}

    if not args.check_only:
        cur = get_all()
        tenant = ask("Directory (tenant) ID", cur.get("ms/tenant", ""))
        client_id = ask("Application (client) ID", cur.get("ms/client_id", ""))
        sheet_url = ask("Sharing link of the TEST copy of the log workbook", cur.get("sheet_url", ""))
        tab = ask("Tab name (or part of it)", cur.get("sheet_tab", "chapterwise"))
        since = ask("Only rows dated on/after (YYYY-MM-DD, blank = all rows)", cur.get("since_date", ""))
        folder = ask("Google Drive TEST folder link for the automation's uploads", cur.get("drive_folder_url", ""))
        if not (tenant and client_id and sheet_url and folder):
            sys.exit("Tenant ID, client ID, the sheet link and the Drive test folder are required.")
        if since:
            dt.date.fromisoformat(since)  # validate
        refresh = device_code_sign_in(tenant, client_id)
        put("ms/tenant", tenant)
        put("ms/client_id", client_id)
        put("ms/refresh_token", refresh, secure=True)
        put("sheet_url", sheet_url, secure=True)
        put("sheet_tab", tab)
        put("drive_folder_url", folder)
        if since:
            put("since_date", since)
        else:
            try:
                ssm.delete_parameter(Name=f"{PREFIX}/since_date")
            except ssm.exceptions.ParameterNotFound:
                pass
        print("Saved the Microsoft sign-in and sheet settings in SSM Parameter Store.")

    cfg = get_all()

    def store(rt: str) -> None:
        put("ms/refresh_token", rt, secure=True)

    client = GraphClient(cfg["ms/tenant"], cfg["ms/client_id"], cfg["ms/refresh_token"], store)
    wb = client.open_workbook(cfg["sheet_url"], cfg.get("sheet_tab", "chapterwise"))
    rows = client.read_rows(wb)
    print(f"\nWorkbook: {wb.name}   Tab: {wb.worksheet_name}   Rows: {len(rows)}")
    if "test" not in wb.name.lower():
        print("!! This file name has no TEST in it. The automation will refuse to touch it until cutover "
              "(AllowProductionSheet=true). For testing, point it at a copy named 'TEST - ...'.")
    since = dt.date.fromisoformat(cfg["since_date"]) if cfg.get("since_date") else None
    picked = []
    for r in rows:
        d = rules.decide(r, None)
        if d.action != "new":
            continue
        if since and ((rules.row_date(r) or dt.date.min) < since):
            continue
        picked.append(r)
    print(f"Rows the automation would pick up (H and I empty, NotebookLM link, unit+chapter set): {len(picked)}")
    for r in picked[:25]:
        print(f"  row {r.row:>4}: {r.awarding_body} | {r.course}  ->  Unit {r.unit} | Chapter {r.chapter}")
        if r.course != rules.norm_space(r.course_raw):
            print(f"             (course name was: {r.course_raw})")
    if len(picked) > 25:
        print(f"  ... and {len(picked) - 25} more")
    print("\n(Read-only check: nothing was written to the workbook.)")


if __name__ == "__main__":
    main()
