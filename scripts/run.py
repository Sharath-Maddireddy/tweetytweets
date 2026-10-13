#!/usr/bin/env python
"""Run a scheduled job with a sign-in pre-check; alert Telegram on any failure.

Task Scheduler throws exit codes away, so without this a broken job is silent.

  python scripts/run.py replies | daily_post | leads | refresh | li_watch | heartbeat
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html import escape

import outbox

ROOT = outbox.ROOT
PY = sys.executable

# job -> (script args, pre-check)
JOBS = {
    "replies":    (["scripts/replies.py"], "research"),
    "daily_post": (["scripts/daily_post.py"], "research"),
    "leads":      (["scripts/leads.py", "--funding"], "research"),
    "refresh":    (["scripts/refresh_accounts.py"], "research"),
    "li_watch":   (["scripts/li_watch.py"], "linkedin"),
    "li_leads":   (["scripts/li_leads.py"], "linkedin"),
}


def check(kind: str) -> str | None:
    """None if fine, else a human-readable problem."""
    import browser
    import post
    import settings
    try:
        if kind == "research":
            os.environ["TWEETY_BROWSER"] = "research"
            try:
                import json
                info = json.loads(browser.evaluate("https://x.com/home", post.LOGIN_JS, settle=6) or "{}")
            finally:
                os.environ.pop("TWEETY_BROWSER", None)
            h = (info.get("handle") or "").lower()
            if not h:
                return "research browser (dummy X account) isn't signed in, or X shows a blank page"
            if h == (settings.load().get("handle") or "").lower():
                return "research browser is signed in as your REAL account; sign in with the dummy"
        elif kind == "linkedin":
            import bot
            if not bot.li_identity_ok():
                return "LinkedIn isn't signed in (as linkedin_name) in the posting browser"
    except Exception as err:
        return f"pre-check crashed: {type(err).__name__}: {err}"
    return None


def heartbeat() -> None:
    con = outbox.db()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    runs = con.execute("select job, ok, count(*) n from runs where started>=? group by 1, 2", (since,)).fetchall()
    lines = [f"{'✅' if r['ok'] else '⚠️'} {r['job']} ×{r['n']}" for r in runs] or ["(no jobs ran in 24h ⚠️)"]
    pend = con.execute("select count(*) from outbox where status='pending'").fetchone()[0]
    funnel = con.execute("select status, count(*) n from contacts group by 1").fetchall()
    outbox.say("💓 <b>daily check</b>\n" + "\n".join(lines) +
               f"\n\nwaiting for your tap: {pend}\nfunnel: " +
               ", ".join(f"{r['status']} {r['n']}" for r in funnel))


def main() -> None:
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job == "heartbeat":
        heartbeat()
        return
    if job not in JOBS:
        raise SystemExit(f"usage: run.py {{{'|'.join(JOBS)}|heartbeat}}")
    args, pre = JOBS[job]
    problem = check(pre)
    if problem:
        outbox.record_run(job, False, problem)
        outbox.say(f"⚠️ <b>{job}</b> skipped: {escape(problem)}")
        sys.exit(1)
    p = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=3600)
    out = (p.stdout or "") + (p.stderr or "")
    (ROOT / "logs").mkdir(exist_ok=True)
    with open(ROOT / "logs" / f"{job}.log", "a", encoding="utf-8") as f:
        f.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} exit {p.returncode}\n{out}")
    ok = p.returncode == 0 and "Traceback" not in out
    outbox.record_run(job, ok, out[-1500:])
    if not ok:
        outbox.say(f"⚠️ <b>{job}</b> failed (exit {p.returncode}):\n<code>{escape(out[-700:])}</code>")
        sys.exit(1)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "scripts"))
    main()
