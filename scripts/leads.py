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
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from html import escape

os.environ["TWEETY_BROWSER"] = "research"      # before anything reads settings

import browser
import funding
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

# Recently funded consumer brands: fresh budget and investors asking for growth.
# These posts are mostly from news accounts, so the leads are the brand/founder
# handles MENTIONED in them, not the author.
FUNDING_QUERIES = [
    'D2C raises seed round',
    'D2C brand raises pre-series A',
    'D2C brand raises Series A India',
    'consumer brand raises funding India',
    'skincare brand raises funding India',
    'food brand raises funding India',
    'fashion brand raises funding India',
    'beverage brand raises funding India',
    'D2C startup funding announcement',
]
# Who they are, straight from bios: one people-search page = ~20 profile cards.
PEOPLE_QUERIES = [
    "D2C founder India", "D2C brand founder", "cofounder D2C brand", "founder skincare brand India",
    "founder beauty brand India", "founder food brand India", "founder fashion brand India",
    "head of marketing D2C", "brand marketing D2C India", "influencer marketing manager India",
    "growth D2C India", "founder consumer brand India",
]
MENTION = re.compile(r"@([A-Za-z0-9_]{2,15})")

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

Candidates (JSON): handle, bio, followers, and the post that surfaced them.
If "signal" starts with "recently raised", the post is a funding announcement
(often by a news account): the candidate may be the brand or its founder named
in it. The author of a news post is never a lead. For a funded lead, open the DM
with a short congrats on the raise and connect it to growth; don't assume
anything about their plans beyond what the post says.
For "profile match" candidates, "bio" is their X profile card and there is no
post: qualify on the bio alone, open the DM on something specific in their bio
(their brand, category, role), and set "public_reply" to "". Bios that are vague,
aspirational ("future founder"), or not clearly tied to a consumer brand: reject.
For funded candidates, "bio" is their X profile card and "brand" is the company
that raised: only qualify if the card clearly IS that brand or its founder/team
(a same-named account in another country or field is a reject). There is no X
post to reply to, so set "public_reply" to "" for them.
{cands}

A lead QUALIFIES only if they are a decision-maker (founder, marketing / growth /
brand / influencer lead) at a brand, or the brand account itself, that runs or
would run Instagram creator campaigns, preferably in India.
REJECT: investors, VCs, angel networks, influencer or marketing agencies, creator platforms / tools (competitors),
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


USER_JS = r"""(async () => {
  // people results load as you scroll; read only the main column (the sidebar's
  // "Who to follow" cells are UserCells too)
  const col = () => document.querySelector('[data-testid="primaryColumn"]') || document;
  for (let i = 0; i < 6; i++) { window.scrollBy(0, 2500); await new Promise(r => setTimeout(r, 1200)); }
  return JSON.stringify([...col().querySelectorAll('[data-testid="UserCell"]')].map(c => ({
    handle: ((c.querySelector('a[href^="/"]') || {}).getAttribute?.('href') || '').replace('/', ''),
    text: c.innerText.replace(/\n+/g, ' | ').slice(0, 260)})));
})()"""


def vet_authors(found: dict, now) -> list[dict]:
    """Keyword-search authors: real, active, not a mega account."""
    hs = list(found)[:25]                      # profile visits are what trip X rate limits
    info = refresh_accounts.vet(hs) if hs else {}
    out = []
    for h in hs:
        d = info[h]
        if (not d.get("missing") and 100 <= d["n"] <= 300_000 and d["last_dt"]
                and d["last_dt"] > now - timedelta(days=LOOKBACK_DAYS)):
            out.append({"handle": h, "bio": d.get("bio", ""), "followers": d["n"], **found[h]})
    return out


def funded_cands(known: dict, n: int, now) -> list[dict]:
    """Brands that just raised (Inc42) -> their X accounts via people search.
    Each candidate carries the brand + raise; Claude decides if it's really them."""
    brands = [b for b in funding.recent(35)
              if re.search(r"D2C|QSR|F&B|Food|Beauty", b["sector"])
              and f"brand:{b['name'].lower()}" not in known][:n]
    urls, keys = [], []
    for b in brands:
        for q in (b["name"], f"{b['name']} founder"):
            urls.append(f"https://x.com/search?q={urllib.parse.quote(q)}&src=typed_query&f=user")
            keys.append(b)
    raw = browser.scrape_many(urls, USER_JS, settle=7) if urls else []
    out, seen = [], set()
    for b, r in zip(keys, raw):
        word = max(re.findall(r"[a-z0-9]+", b["name"].lower()), key=len, default="")
        for c in json.loads(r or "[]")[:8]:
            h = c.get("handle", "")
            if not h or h.lower() in seen or h.lower() in known or word not in c["text"].lower():
                continue
            seen.add(h.lower())
            out.append({"handle": h, "bio": c["text"], "brand": b["name"],
                        "post_url": b["source"], "post": f"{b['name']} raised {b['amount']} "
                        f"({b['round'] or 'round'}) on {b['date']}, investors: {b['investors'][:100]}",
                        "signal": f"recently raised: {b['round'] or 'round'} {b['amount']} on {b['date']}"})
        known[f"brand:{b['name'].lower()}"] = {"status": "searched", "surfaced_at": now.isoformat(), **b}
    return out


def people_cands(known: dict, me: str) -> list[dict]:
    urls = [f"https://x.com/search?q={urllib.parse.quote(q)}&src=typed_query&f=user" for q in PEOPLE_QUERIES]
    out, seen = [], set()
    for q, r in zip(PEOPLE_QUERIES, browser.scrape_many(urls, USER_JS, settle=7)):
        for c in json.loads(r or "[]"):
            h = c.get("handle", "")
            if not h or "/" in h or h.lower() in seen or h.lower() in known or h.lower() == me.lower():
                continue
            seen.add(h.lower())
            out.append({"handle": h, "bio": c["text"], "post_url": f"https://x.com/{h}",
                        "post": "", "signal": f"profile match: {q}"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--funding", action="store_true",
                    help="brands that raised in the last 35 days (Inc42 tables), found on X")
    ap.add_argument("--brands", type=int, default=10, help="--funding: brands per run")
    ap.add_argument("--people", action="store_true",
                    help="X people search for D2C founders / brand & marketing leads (bio-based)")
    ap.add_argument("--max", type=int, default=MAX_LEADS, help="max qualified leads per run")
    a = ap.parse_args()
    cfg = settings.load()
    me = cfg["handle"]
    known = load()
    now = datetime.now(timezone.utc)
    days = 30 if a.funding else LOOKBACK_DAYS
    since = (now - timedelta(days=days)).strftime("%Y-%m-%d")

    # 1. signals
    found: dict[str, dict] = {}
    if a.people:
        cands = people_cands(known, me)
        print(f"people search -> {len(cands)} profiles to judge", flush=True)
        if not cands:
            return
    elif a.funding:
        cands = funded_cands(known, a.brands, now)
        print(f"funded brands -> {len(cands)} X accounts to judge", flush=True)
        if not cands:
            return
    else:
        items = research.x_search([f"{q} since:{since} -filter:replies" for q in QUERIES], mode="top")
        for it in items:
            found.setdefault(refresh_accounts.handle_of(it), {
                "post_url": it["url"], "post": (it["extra"]["text"] or "")[:400],
                "signal": it["extra"]["query"].split(" since:")[0]})
        found = {h: v for h, v in found.items() if h and h.lower() != me.lower() and h.lower() not in known}
    if not (a.funding or a.people):
        cands = vet_authors(found, now)
        print(f"signals: {len(items)} posts, {len(found)} new authors, vetted {len(cands)}", flush=True)
        if not cands:
            return

    # 3. qualify + draft
    context = "\n\n".join(p.read_text(encoding="utf-8")
                          for p in sorted((replies.ROOT / "references").glob("*.local.md")))
    txt = replies.claude(PROMPT.format(handle=me, context=context, max_leads=a.max,
                                       cands=json.dumps(cands, ensure_ascii=False, indent=1)),
                         replies.cfg()["claude"])
    try:
        picked = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    except ValueError:
        raise SystemExit(f"model returned no JSON array:\n{txt[:400]}")
    by = {c["handle"].lower(): c for c in cands}
    leads = [{**by[p["handle"].lower().lstrip("@")], **p} for p in picked
             if isinstance(p, dict) and p.get("handle", "").lower().lstrip("@") in by][:a.max]

    # 4. deliver
    if not leads:
        print("no qualified leads this run", flush=True)
    msgs = [f"<b>🎯 {len(leads)} brand leads</b>\nWhere there's a public reply, send it first and "
            f"the DM a day later. Skip anyone who doesn't look right."]
    for n, l in enumerate(leads, 1):
        msgs.append(
            f"<b>{n}. @{escape(l['handle'])}</b> · {escape(l.get('role', ''))}"
            f"{' @ ' + escape(l['company']) if l.get('company') else ''} · fit {l.get('score', '?')}/5\n"
            f"<i>{escape(l.get('bio', '')[:160])}</i>\n"
            f"why: {escape(l.get('why', ''))}\n\n"
            f"<b>signal</b> ({escape(l['signal'])})\n<i>{escape(l['post'][:240])}</i>\n{l['post_url']}\n\n"
            + (f"<b>1. public reply (today)</b>\n<code>{escape(l['public_reply'])}</code>\n\n"
               f"<b>2. DM (tomorrow)</b>\n" if l.get("public_reply") else "<b>DM</b>\n")
            + f"<code>{escape(l.get('dm', ''))}</code>\n"
            f"https://x.com/{l['handle']}")
    if a.dry_run:
        print("\n\n----\n\n".join(msgs))
        (replies.ROOT / "drafts" / "leads_batch.json").write_text(
            json.dumps(leads, indent=1, ensure_ascii=False), encoding="utf-8")   # for review / dm.py
        return
    if leads:
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
