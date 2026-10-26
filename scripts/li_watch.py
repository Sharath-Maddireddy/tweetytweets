#!/usr/bin/env python
"""Daily: spot accepted LinkedIn requests, draft follow-ups, and nudge silent leads.

- Accepted: matched by /in/<vanity> on your Connections page (one page load). The
  person's current role at the brand is re-checked ("Present" in Experience), then the
  follow-up goes to the outbox (✅ Send / Skip; auto-sends after the veto window).
- Silent 5 days after the follow-up (LinkedIn) or the DM (X): a short nudge goes to the
  outbox. A LinkedIn nudge only sends if the chat still ends with our message (no reply).
  One nudge per person, ever.

  python scripts/li_watch.py            # needs LinkedIn signed in on the posting browser
  python scripts/li_watch.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape

import browser
import outbox
import replies
from outbox import db, now

# replies.py switches browser calls to the RESEARCH browser on import; LinkedIn lives
# in the posting browser, so switch back.
os.environ.pop("TWEETY_BROWSER", None)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

NUDGE_AFTER = timedelta(days=5)
CONNECTIONS = "https://www.linkedin.com/mynetwork/invite-connect/connections/"
LINKS_JS = r"""(async () => { for (let i = 0; i < 4; i++) { window.scrollBy(0, 2500); await new Promise(r => setTimeout(r, 1000)); }
  return JSON.stringify([...new Set([...document.querySelectorAll('main a[href*="/in/"]')]
    .map(a => a.href.split('?')[0].replace(/\/$/, '').split('/in/')[1]))]); })()"""

# Follow-ups are a fixed template (the wording Sharath approved), not free-written drafts:
# free drafts kept adding flattery about people's careers.
PITCH_BRAND = ("At Creonnect we help brands and agencies run creator campaigns at scale with AI, picking creators "
               "on real engagement instead of follower counts and spreadsheets. Open to a quick 15-min call?")
PITCH_AGENCY = ("At Creonnect we help agencies run creator campaigns for their brand clients at scale with AI, "
                "shortlisting creators on real engagement instead of follower counts and spreadsheets. "
                "Open to a quick 15-min call?")


def followup(first: str, brand: str, agency: bool) -> str:
    if agency:
        return f"Hi {first}, thanks for connecting! Saw you're building {brand}. " + PITCH_AGENCY
    if raise_info(brand).startswith("raised"):
        return f"Hi {first}, thanks for connecting, and congrats on the {brand} raise! " + PITCH_BRAND
    return f"Hi {first}, thanks for connecting! Quick question: how does {brand} pick creators for campaigns today? " + PITCH_BRAND


NUDGE = """Write a short, friendly follow-up from Sharath (cofounder of Creonnect) to {name} at
{brand}. He messaged them {days} days ago and got no reply:
<previous>
{previous}
</previous>
One or two lines: no guilt-tripping, no "just bumping this". Make the ask easier: a 10-min
call, or a quick look at how Creo Intelligence would shortlist creators for their brand.
Never offer data, reports, results or "what we're seeing": none exist yet.
{greeting_rule} Under 220 chars, no em dashes. Answer with only the message text."""


def raise_info(brand: str) -> str:
    try:
        import funding
        for b in funding.recent(90):
            if b["name"].lower() == (brand or "").lower():
                return f"raised {b['amount']} {b['round'] or ''} on {b['date']} (Inc42)"
    except Exception:
        pass
    return "(no funding info; don't mention a raise)"


def experience(vanity: str, brand: str) -> tuple[bool, str]:
    t = browser.evaluate(f"https://www.linkedin.com/in/{vanity}/details/experience/", "document.body.innerText",
                         settle=8) or ""
    i = t.find("Experience")
    body = t[i:i + 2500] if i >= 0 else t[:2500]
    word = max(re.findall(r"[a-z0-9]+", (brand or "").lower()), key=len, default="")
    # current role at the brand: brand word within the same block as "Present"
    ok = any(word and word in blk.lower() and "present" in blk.lower() for blk in body.split("\n\n\n")) or \
         bool(word and re.search(rf"{word}[\s\S]{{0,160}}Present|Present[\s\S]{{0,160}}{word}", body, re.I))
    return ok, body[:1500]


def accepted(dry: bool) -> None:
    con = db()
    waiting = con.execute("select rowid, * from contacts where channel='linkedin' and status='requested'").fetchall()
    if not waiting:
        return
    links = set(json.loads(browser.scrape_many([CONNECTIONS], LINKS_JS, settle=8)[0] or "[]"))
    for c in waiting:
        vanity = c["key"].split(":", 1)[1]
        if vanity not in links:
            continue
        ok, exp = experience(vanity, c["brand"])
        first = (c["name"] or "").split()[0]
        if not ok:
            outbox.say(f"💼 <b>{escape(c['name'])}</b> accepted, but I couldn't confirm they still work at "
                       f"{escape(c['brand'] or '?')}. Check before messaging.\nhttps://www.linkedin.com/in/{vanity}/")
            if not dry:
                outbox.upsert_contact(c["key"], status="accepted", accepted_at=now())
            continue
        agency = json.loads(c["meta"] or "{}").get("type") == "agency"
        msg = followup(first, c["brand"] or "your brand", agency)
        print(f"accepted: {c['name']} ({c['brand']})\n  {msg}")
        if dry:
            continue
        outbox.upsert_contact(c["key"], status="accepted", accepted_at=now())
        outbox.add("li_msg", f"https://www.linkedin.com/in/{vanity}/", msg, expected_name=c["name"],
                   contact_key=c["key"], header=f"💼 accepted · {escape(c['brand'] or '')} · role re-checked ✓")


def nudges(dry: bool) -> None:
    con = db()
    cutoff = (datetime.now(timezone.utc) - NUDGE_AFTER).isoformat()
    rows = con.execute(
        "select rowid, * from contacts where replied=0 and nudged_at is null and ("
        " (channel='linkedin' and status='followup_sent' and followup_sent_at < ?) or"
        " (channel='x' and status='dm_sent' and sent_at < ?))", (cutoff, cutoff)).fetchall()
    for c in rows:
        meta = json.loads(c["meta"] or "{}")
        since = c["followup_sent_at"] if c["channel"] == "linkedin" else c["sent_at"]
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(since)).days
        first = (c["name"] or "").split()[0]
        rule = (f'Start with "Hi {first},".' if c["channel"] == "linkedin"
                else "Lowercase casual X DM tone, no greeting needed.")
        msg = replies.claude(NUDGE.format(name=c["name"], brand=c["brand"] or "their brand", days=days,
                                          previous=meta.get("followup_draft") or "(a short intro about Creonnect)",
                                          greeting_rule=rule), replies.cfg()["claude"]).strip()
        print(f"nudge {c['channel']}: {c['name']}\n  {msg}")
        if dry:
            continue
        if c["channel"] == "x":
            outbox.add("x_dm", c["url"].rstrip("/").split("/")[-1], msg, contact_key=c["key"],
                       header=f"🔁 nudge · no reply in {days} days")
        else:                                        # sends only if the chat still ends with our message
            outbox.add("li_nudge", c["url"], msg, expected_name=c["name"], contact_key=c["key"],
                       header=f"🔁 nudge · no reply in {days} days")
        con.execute("update contacts set nudged_at=? where rowid=?", (now(), c["rowid"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    accepted(a.dry_run)
    nudges(a.dry_run)


if __name__ == "__main__":
    main()
