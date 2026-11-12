#!/usr/bin/env python3
"""
Sign the video automation in to Google (the content team's NotebookLM account).

Run it yourself on a Windows PC with Chrome installed:

    py -3.12 -m pip install playwright boto3            (once)
    py -3.12 aws\\scripts\\automation_google_signin.py

1. A normal Chrome window opens with a fresh, temporary profile.
2. Sign in to the content team's Google account and wait until NotebookLM shows the notebooks.
3. Come back here and press Enter.

The script copies only the Google sign-in cookies into AWS (S3, encrypted,
private bucket). The password is never typed, seen or stored by this script.
Do NOT click "Sign out" in that window afterwards: signing out ends the
session the automation uses. Just close the window.

Check the download steps on this PC before relying on AWS (headed browser,
uses the session just saved; the video is saved in a temp folder only):

    py -3.12 aws\\scripts\\automation_google_signin.py --probe <notebook link> --unit 14 --chapter 4

Options: --profile (default slc-video-aws-deployer), --region (eu-west-2),
--skip-signin (probe only).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "aws" / "automation"))

PROJECT = os.environ.get("PROJECT", "slc-video-aws")
SESSION_KEY = "automation/google-session.json"
KEEP_DOMAINS = ("google.com", "google.co.uk", "googleusercontent.com", "notebooklm.google", "gstatic.com",
                "youtube.com")


def find_chrome() -> str:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "", shutil.which("chrome") or "",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("Google Chrome was not found. Install Chrome, or run this on a PC that has it.")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def aws_session(profile: str, region: str):
    import boto3
    return boto3.Session(profile_name=profile, region_name=region)


def media_bucket(sess) -> str:
    cf = sess.client("cloudformation")
    outs = cf.describe_stacks(StackName=PROJECT)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outs if o["OutputKey"] == "MediaBucketName")


def sign_in(args) -> None:
    from playwright.sync_api import sync_playwright

    # Chromium (the browser the bot itself runs in AWS) by default. Google
    # Chrome can lock a Google sign-in to this PC, and the copied session was
    # signed out after ~2 hours in AWS when it was made in Chrome.
    if args.use_chrome:
        chrome = find_chrome()
    else:
        with sync_playwright() as p:
            chrome = p.chromium.executable_path
        if not Path(chrome).exists():
            sys.exit("Playwright's Chromium is missing: run  py -3.12 -m playwright install chromium")
    port = free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="slc-google-signin-"))
    # A plain Chrome process (not started by an automation driver), so Google
    # treats it as a normal browser sign-in.
    proc = subprocess.Popen([
        chrome, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
        "https://notebooklm.google.com/",
    ])
    try:
        print("\nA browser window is open. In that window:", flush=True)
        print("  1. Sign in to the content team's Google account.", flush=True)
        print("  2. Wait until NotebookLM shows the list of notebooks.", flush=True)
        print("This script notices the sign-in by itself (waits up to 15 minutes).", flush=True)
        with sync_playwright() as p:
            browser = None
            for _ in range(30):  # Chrome needs a moment to open its debugging port
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    time.sleep(1)
            if browser is None:
                sys.exit("Could not connect to the Chrome window.")
            context = browser.contexts[0]
            hosts = ("notebooklm.google.com", "notebook.google.com")
            page, deadline = None, time.time() + 15 * 60
            while time.time() < deadline:
                signed_in = any(c["name"] in ("SID", "__Secure-1PSID") for c in context.cookies())
                page = next((pg for pg in context.pages if any(h in pg.url for h in hosts)
                             and "accounts.google.com" not in pg.url), None)
                if signed_in and page is not None:
                    break
                time.sleep(3)
            else:
                sys.exit("Timed out waiting for the Google sign-in (15 minutes). Run the script again.")
            time.sleep(8)  # let NotebookLM finish loading and set its cookies
            user_agent = page.evaluate("navigator.userAgent")
            state = context.storage_state()
        cookies = [c for c in state.get("cookies", [])
                   if any(c.get("domain", "").lstrip(".").endswith(d) for d in KEEP_DOMAINS)]
        if not any(c["name"] in ("SID", "__Secure-1PSID") for c in cookies):
            sys.exit("No Google sign-in found in that window - sign in first, then run this script again.")
        payload = {
            "cookies": cookies,
            "origins": [o for o in state.get("origins", []) if "notebooklm" in o.get("origin", "")],
            "user_agent": user_agent.replace("HeadlessChrome", "Chrome"),
            "saved_at": int(time.time()),
        }
        sess = aws_session(args.profile, args.region)
        bucket = media_bucket(sess)
        sess.client("s3").put_object(Bucket=bucket, Key=SESSION_KEY, Body=json.dumps(payload).encode(),
                                     ContentType="application/json", ServerSideEncryption="AES256")
        table = sess.resource("dynamodb").Table(f"{PROJECT}-jobs")
        table.delete_item(Key={"pk": "AUTOCFG#google"})            # clear "sign-in required"
        table.delete_item(Key={"pk": "AUTOCFG#alert#google_signin"})
        print(f"\nSaved the Google session for the automation ({len(cookies)} cookies, encrypted in S3).")
        print("You can close the Chrome window now. Do NOT sign out in it.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(profile_dir, ignore_errors=True)


def probe(args) -> None:
    """Run the exact download steps the AWS task runs, in a visible browser."""
    from playwright.sync_api import sync_playwright

    import notebooklm

    sess = aws_session(args.profile, args.region)
    bucket = media_bucket(sess)
    state = json.loads(sess.client("s3").get_object(Bucket=bucket, Key=SESSION_KEY)["Body"].read())
    out = Path(tempfile.mkdtemp(prefix="slc-probe-"))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        context = browser.new_context(
            storage_state={"cookies": state["cookies"], "origins": state.get("origins", [])},
            accept_downloads=True, user_agent=state.get("user_agent"), viewport={"width": 1600, "height": 1000},
        )
        page = context.new_page()
        print("Signed in:", notebooklm.check_signed_in(page))
        page.close()
        try:
            res = notebooklm.download_video(context, args.probe, args.unit, args.chapter, out)
            print(f"OK: downloaded {res.path} ({res.path.stat().st_size / 1e6:.1f} MB) "
                  f"from notebook {res.notebook_id}; item: {res.title}")
        except Exception as e:
            shot = out / "failure.png"
            for pg in context.pages:
                try:
                    pg.screenshot(path=str(shot), full_page=True)
                except Exception:
                    pass
            print(f"FAILED: {e.__class__.__name__}: {e}\nScreenshot: {shot}")
        browser.close()
    print(f"Files are in {out} (delete the folder when done).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=f"{PROJECT}-deployer")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--probe", metavar="NOTEBOOK_LINK")
    ap.add_argument("--unit")
    ap.add_argument("--chapter")
    ap.add_argument("--headless", action="store_true", help="probe without showing the browser")
    ap.add_argument("--skip-signin", action="store_true")
    ap.add_argument("--use-chrome", action="store_true", help="sign in with Google Chrome instead of Chromium")
    args = ap.parse_args()
    if args.probe and not (args.unit and args.chapter):
        ap.error("--probe needs --unit and --chapter")
    if not args.skip_signin:
        sign_in(args)
    if args.probe:
        probe(args)


if __name__ == "__main__":
    main()
