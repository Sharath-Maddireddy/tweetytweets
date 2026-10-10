#!/usr/bin/env python
"""Find brand leads for Creonnect on X and draft outreach. Sends to Telegram.

Never sends a DM or reply itself: you read each lead and send by hand.
Runs in the RESEARCH browser. Only uses what's public on X (handle, bio, the
post that signals intent); nothing is pulled from other sites.

Flow per lead: reply to their signal post first (a cold DM from a small account
usually lands in message requests unseen), then DM a day later.

  python scripts/leads.py              # find + qualify + draft, send to Telegram
  python scripts/leads.py --dry-run    # print only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from html import escape

os.environ["TWEETY_BROWSER"] = "research"      # before anything reads settings

import refresh_accounts
import replies
import research
import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LEADS = replies.ROOT / "drafts" / "leads.json"   # git-ignored; every lead ever surfaced
MAX_LEADS = 8                                    # per run: quality over volume
LOOKBACK_DAYS = 21

# Intent signals: hiring for creator roles, asking for creators, talking about
# influencer results, launching. India-leaning terms; Claude judges the rest.
QUERIES = [
    # intent: creators / influencer spend
    '"looking for creators" brand',
    '"looking for influencers" India',
    'influencer marketing ROI D2C',
    'D2C brand influencer campaign India',
    'UGC creators D2C India',
    'micro influencers D2C brand',
    # who: Indian D2C founders building in public
    '"D2C founder" India',
    '"our D2C brand"',
    '"building a D2C brand"',
    'D2C skincare brand India founder',
    'D2C fashion brand India founder',
    'D2C food brand India founder',
    '"Shark Tank India" our brand',
    'D2C brand instagram marketing India',
]

PROMPT = """You qualify sales leads for Creonnect and draft outreach from its cofounder @{handle}.

About him and Creonnect:
<context>
{context}
</context>

What Creonnect offers brands, use ONLY these facts:
- AI that analyses Instagram creators (engagement quality, comments, audience fit,
  consistency of recent posts) to find the right creators for a brand campaign.
- Brands run campaigns (public or invite-only) and creators apply.
- Right now brands are onboarded personally by the team. The offer is a 15-minute demo call.
- No customer numbers or results exist yet: never claim any.

Candidates (JSON): handle, bio, followers, and the post that surfaced them:
{cands}

A lead QUALIFIES only if they are a decision-maker (founder, marketing / growth /
brand / influencer lead) at a brand, or the brand account itself, that runs or
would run Instagram creator campaigns, preferably in India.
REJECT: influencer or marketing agencies, creator platforms / tools (competitors),
creators and influencers themselves, recruiters, job boards, crypto/web3, news,
huge brands that buy through agencies, anyone whose fit is a guess.

For each qualifying lead write:
- "public_reply": a reply to their signal post, sent first. Adds something real
  to their post; no pitch, no link. Under 140 chars, casual, no em dashes.
- "dm": the follow-up DM. Open on their specific post (name it), one line on
  the problem in their terms, one line on what Creonnect does, then a soft ask
  for a 15-min call. Under 300 chars, lowercase is fine, no em dashes, no hype
  words, no invented numbers, no "hope you're well", no "I'd love to".
- "score": 1-5 fit, "company": name if clear else "", "role": short, "why": under 15 words.

Answer with ONLY a JSON array of qualifying leads (max {max_leads}, best first):
[{{"handle": "...", "company": "...", "role": "...", "score": 4, "why": "...",
   "public_reply": "...", "dm": "..."}}]   or []
"""


def load() -> dict:
    return json.loads(LEADS.read_text(encoding="utf-8")) if LEADS.exists() else {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = settings.load()
    me = cfg["handle"]
    known = load()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    # 1. signals
    found: dict[str, dict] = {}
    items = research.x_search([f"{q} since:{since} -filter:replies" for q in QUERIES], mode="top")
    for it in items:
        h = refresh_accounts.handle_of(it)
        if not h or h.lower() == me.lower() or h.lower() in known:
            continue
        found.setdefault(h, {"post_url": it["url"], "post": (it["extra"]["text"] or "")[:400],
                             "signal": it["extra"]["query"].split(" since:")[0]})
    print(f"signals: {len(items)} posts, {len(found)} new authors", flush=True)

    # 2. vet: real, active, not a mega account
    hs = list(found)[:25]       # profile visits are what trip X rate limits
    info = refresh_accounts.vet(hs) if hs else {}
    cands = []
    for h in hs:
        d = info[h]
        if (not d.get("missing") and 100 <= d["n"] <= 300_000 and d["last_dt"]
                and d["last_dt"] > now - timedelta(days=LOOKBACK_DAYS)):
            cands.append({"handle": h, "bio": d.get("bio", ""), "followers": d["n"], **found[h]})
    print(f"vetted: {len(cands)}", flush=True)
    if not cands:
        return

    # 3. qualify + draft
    context = "\n\n".join(p.read_text(encoding="utf-8")
                          for p in sorted((replies.ROOT / "references").glob("*.local.md")))
    txt = replies.claude(PROMPT.format(handle=me, context=context, max_leads=MAX_LEADS,
                                       cands=json.dumps(cands, ensure_ascii=False, indent=1)),
                         replies.cfg()["claude"])
    try:
        picked = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    except ValueError:
        raise SystemExit(f"model returned no JSON array:\n{txt[:400]}")
    by = {c["handle"].lower(): c for c in cands}
    leads = [{**by[p["handle"].lower().lstrip("@")], **p} for p in picked
             if isinstance(p, dict) and p.get("handle", "").lower().lstrip("@") in by][:MAX_LEADS]

    # 4. deliver
    msgs = [f"<b>🎯 {len(leads)} brand leads</b>\nFor each: send the public reply today, "
            f"the DM tomorrow. Skip anyone who doesn't look right."]
    for n, l in enumerate(leads, 1):
        msgs.append(
            f"<b>{n}. @{escape(l['handle'])}</b> · {escape(l.get('role', ''))}"
            f"{' @ ' + escape(l['company']) if l.get('company') else ''} · fit {l.get('score', '?')}/5\n"
            f"<i>{escape(l.get('bio', '')[:160])}</i>\n"
            f"why: {escape(l.get('why', ''))}\n\n"
            f"<b>their post</b> ({escape(l['signal'])})\n<i>{escape(l['post'][:240])}</i>\n{l['post_url']}\n\n"
            f"<b>1. public reply (today)</b>\n<code>{escape(l.get('public_reply', ''))}</code>\n\n"
            f"<b>2. DM (tomorrow)</b>\n<code>{escape(l.get('dm', ''))}</code>\n"
            f"https://x.com/{l['handle']}")
    if a.dry_run:
        print("\n\n----\n\n".join(msgs))
        return
    replies.send(msgs, cfg["telegram"])
    for l in leads:
        known[l["handle"].lower()] = {**l, "surfaced_at": now.isoformat(), "status": "new"}
    for c in cands:                       # rejected ones too, so they aren't re-judged daily
        known.setdefault(c["handle"].lower(), {"handle": c["handle"], "status": "rejected",
                                               "surfaced_at": now.isoformat()})
    LEADS.parent.mkdir(exist_ok=True)
    LEADS.write_text(json.dumps(known, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"sent {len(leads)} leads to Telegram", flush=True)


if __name__ == "__main__":
    main()
