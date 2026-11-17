#!/usr/bin/env python3
"""
Sign the video-creation automation in to the southlondoncollege.org WordPress
backend (needed to read each chapter's PDF link from Edit Post).

    py -3.12 aws\\scripts\\automation_wordpress_signin.py

1. A browser window opens on the WordPress sign-in page (fresh, temporary profile).
2. Sign in as usual and TICK "Remember Me" (the session then lasts ~14 days).
3. Wait until the WordPress dashboard shows. The script notices it by itself.

Only the WordPress sign-in cookies are copied into AWS (S3, encrypted, private
bucket). The password is never typed, seen or stored by this script. Don't
click "Log out" in that window afterwards - just let the script close it.

Check what the saved session can see (read-only):

    py -3.12 aws\\scripts\\automation_wordpress_signin.py --check
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from automation_google_signin import aws_session, free_port, media_bucket  # noqa: E402

PROJECT = os.environ.get("PROJECT", "slc-video-aws")
SITE = "https://southlondoncollege.org"
SESSION_KEY = "automation/wordpress-session.json"


def _logged_in(cookies) -> bool:
    return any("logged_in" in c["name"] for c in cookies)


def sign_in(args) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        chromium = p.chromium.executable_path
    port = free_port()
    profile_dir = Path(tempfile.mkdtemp(prefix="slc-wp-signin-"))
    proc = subprocess.Popen([
        chromium, f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
        f"{SITE}/wp-login.php?redirect_to=%2Fwp-admin%2F",
    ])
    try:
        print("\nA browser window is open on the WordPress sign-in page. In that window:", flush=True)
        print("  1. Sign in, and tick 'Remember Me'.", flush=True)
        print("  2. Wait until the dashboard or the website (with the black admin bar) shows.", flush=True)
        print("This script notices the sign-in by itself (waits up to 15 minutes).", flush=True)
        with sync_playwright() as p:
            browser = None
            for _ in range(30):
                try:
                    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    break
                except Exception:
                    time.sleep(1)
            if browser is None:
                sys.exit("Could not connect to the browser window.")
            context = browser.contexts[0]
            deadline, last_report = time.time() + 15 * 60, 0.0
            while time.time() < deadline:
                # Signed in = a site page shows the WordPress admin bar (or the
                # dashboard), whatever the sign-in cookies are called.
                signed_in_page = None
                for pg in context.pages:
                    if "southlondoncollege.org" not in pg.url or "wp-login.php" in pg.url:
                        continue
                    try:
                        if pg.evaluate("() => !!document.getElementById('wpadminbar') "
                                       "|| document.body.classList.contains('logged-in') "
                                       "|| document.body.classList.contains('wp-admin')"):
                            signed_in_page = pg
                            break
                    except Exception:
                        pass  # page still loading
                if signed_in_page is not None:
                    break
                if time.time() - last_report > 15:
                    last_report = time.time()
                    names = sorted({c["name"] for c in context.cookies() if "southlondoncollege" in c.get("domain", "")})
                    print(f"  waiting... pages={[pg.url[:80] for pg in context.pages]} cookies={names}", flush=True)
                time.sleep(3)
            else:
                sys.exit("Timed out waiting for the WordPress sign-in (15 minutes). Run the script again.")
            time.sleep(4)
            print("Signed-in page seen:", signed_in_page.url[:100], flush=True)
            user_agent = signed_in_page.evaluate("navigator.userAgent")
            state = context.storage_state()
        cookies = [c for c in state.get("cookies", []) if "southlondoncollege.org" in c.get("domain", "")]
        remembered = any(_logged_in([c]) and c.get("expires", -1) > time.time() + 2 * 86400 for c in cookies)
        payload = {"cookies": cookies, "origins": [], "user_agent": user_agent, "saved_at": int(time.time())}
        sess = aws_session(args.profile, args.region)
        bucket = media_bucket(sess)
        sess.client("s3").put_object(Bucket=bucket, Key=SESSION_KEY, Body=json.dumps(payload).encode(),
                                     ContentType="application/json", ServerSideEncryption="AES256")
        sess.resource("dynamodb").Table(f"{PROJECT}-jobs").delete_item(Key={"pk": "AUTOCFG#wordpress"})
        print(f"\nSaved the WordPress session for the automation ({len(cookies)} cookies, encrypted in S3).")
        if not remembered:
            print("NOTE: 'Remember Me' was not ticked, so this session may end when the browser closes. "
                  "If the bot reports a WordPress sign-in problem, run this again and tick it.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(profile_dir, ignore_errors=True)


def check(args) -> None:
    """Open wp-admin with the saved session (headless, read-only)."""
    from playwright.sync_api import sync_playwright

    sess = aws_session(args.profile, args.region)
    state = json.loads(sess.client("s3").get_object(Bucket=media_bucket(sess), Key=SESSION_KEY)["Body"].read())
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state={"cookies": state["cookies"], "origins": []},
                                      user_agent=state.get("user_agent"))
        page = context.new_page()
        page.goto(f"{SITE}/wp-admin/", wait_until="domcontentloaded", timeout=60_000)
        ok = "/wp-admin" in page.url and "wp-login.php" not in page.url
        print("Signed in to WordPress:", ok, "-", page.url)
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--profile", default=f"{PROJECT}-deployer")
    ap.add_argument("--region", default="eu-west-2")
    ap.add_argument("--check", action="store_true", help="only check the saved session")
    args = ap.parse_args()
    if not args.check:
        sign_in(args)
    check(args)


if __name__ == "__main__":
    main()
