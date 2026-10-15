#!/usr/bin/env python
"""Daily: spot accepted LinkedIn requests, draft follow-ups, and nudge silent leads.

- Accepted: matched by /in/<vanity> on your Connections page (one page load). The
  person's current role at the brand is re-checked ("Present" in Experience), then a
  follow-up is drafted and sent to Telegram to paste (LinkedIn messages can't be
  automated: they hit a Premium upsell). Button: "✅ I sent it".
- Silent 5 days after the follow-up (LinkedIn) or the DM (X): a short nudge is drafted.
  X nudges go to the outbox with ✅ Send; LinkedIn nudges are to paste. Each also gets
  a "they replied" button that stops further nudges. One nudge per person, ever.

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

FOLLOWUP = """Write a LinkedIn message from Sharath (22, cofounder of Creonnect) to {name}, who just
accepted his connection request. Their current role (from their LinkedIn Experience):
<experience>
{experience}
</experience>
Their brand {brand}: {raise_info}

Facts about Creonnect (claim nothing else; no customer numbers exist):
<facts>
{facts}
</facts>

Rules: start "Hi {first},", thank them for connecting, congratulate the raise only if it's
listed above, mention one specific thing from their experience that relates to creators or
marketing, one line on Creonnect (helps brands and agencies run creator campaigns at scale
with AI: right creators, judged on real engagement, campaigns without spreadsheets), ask for a 15-min call. Under 350 chars,
warm, no em dashes, no hype. Answer with only the message text."""

NUDGE = """Write a short, friendly follow-up from Sharath (cofounder of Creonnect) to {name} at
{brand}. He messaged them {days} days ago and got no reply:
<previous>
{previous}
</previous>
One or two lines: no guilt-tripping, no "just bumping this", add one useful line or make the
ask easier (e.g. a 10-min call or happy to share what we see for brands in their category).
{greeting_rule} Under 220 chars, no em dashes. Answer with only the message text."""


def facts() -> str:
    p = outbox.ROOT / "references" / "facts.local.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


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
        msg = replies.claude(FOLLOWUP.format(name=c["name"], first=first, experience=exp, brand=c["brand"],
                                             raise_info=raise_info(c["brand"]), facts=facts()),
                             replies.cfg()["claude"]).strip()
        if not msg.startswith(f"Hi {first},"):
            msg = f"Hi {first}, " + msg
        print(f"accepted: {c['name']} ({c['brand']})\n  {msg}")
        if dry:
            continue
        outbox.upsert_contact(c["key"], status="accepted", accepted_at=now(),
                              meta=json.dumps({"followup_draft": msg}))
        outbox.say(f"💼 <b>{escape(c['name'])}</b> accepted · {escape(c['brand'] or '')} (role re-checked ✓)\n"
                   f"Profile → Message (close the Premium pop-up) → paste:\n\n<code>{escape(msg)}</code>\n\n"
                   f"https://www.linkedin.com/in/{vanity}/",
                   [("✅ I sent it", f"fu:{c['rowid']}")])


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
        else:
            outbox.say(f"🔁 <b>{escape(c['name'])}</b> · no reply in {days} days. Paste if you like:\n\n"
                       f"<code>{escape(msg)}</code>\n\n{c['url']}",
                       [("✅ I sent it", f"ndg:{c['rowid']}"), ("💬 they replied", f"rep:{c['rowid']}")])
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
