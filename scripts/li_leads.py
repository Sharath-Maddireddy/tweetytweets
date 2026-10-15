#!/usr/bin/env python
"""Weekly: newly funded brands -> their marketing person on LinkedIn -> queued for ✅.

For each consumer brand that raised in the last 35 days (Inc42) and isn't contacted
yet: search LinkedIn people (posting browser), let Claude pick the one person who owns
marketing / creators AT that brand (else a founder), re-check their Experience shows
the brand as current, then queue a connection request in the outbox. You tap ✅ in
Telegram; the bot sends it (paced, capped). Notes are off: the free plan allows 3/month.

  python scripts/li_leads.py [--brands 12] [--days 35] [--dry-run]
  python scripts/li_leads.py --agencies [--dry-run]   # influencer / social agencies in India
  python scripts/li_leads.py --agencies --from seed/agencies.txt   # one person at each listed agency
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse

import replies                                   # sets TWEETY_BROWSER=research on import...
os.environ.pop("TWEETY_BROWSER", None)           # ...but LinkedIn is in the posting browser

import browser
import funding
import li_watch
import outbox

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SEARCH_JS = r"""(async () => {
  for (let i = 0; i < 3; i++) { window.scrollBy(0, 1500); await new Promise(r => setTimeout(r, 1000)); }
  const seen = new Set(), out = [];
  document.querySelectorAll('main a[href*="/in/"]').forEach(a => {
    const url = a.href.split('?')[0]; if (seen.has(url)) return;
    const card = a.closest('li') || a.parentElement;
    const text = (card ? card.innerText : a.innerText).replace(/\n+/g, ' | ').slice(0, 300);
    if (text.length < 15) return; seen.add(url); out.push({url, text}); });
  return JSON.stringify(out.slice(0, 8)); })()"""

PICK = """For each Indian consumer brand below (each just raised money), pick the ONE best person to
contact about influencer / creator marketing, from LinkedIn search results.
Priority: CMO / Head of Marketing / brand / influencer or creator partnerships / growth lead who
clearly works AT that brand > a founder or co-founder of that brand > nobody.
Reject interns, designers, engineers, agencies or freelancers listing the brand as a client,
people at other companies, and anyone not clearly tied to the brand. If unsure: null.

Search results per brand: {cands}

Answer ONLY JSON: [{{"brand": "...", "name": "exact name as on the card", "url": "...", "title": "...",
"why": "under 12 words"}}] (name/url null when nobody fits)."""

AGENCY_QUERIES = ["influencer marketing agency founder", "influencer marketing agency co-founder India",
                  "creator marketing agency founder", "head of influencer marketing agency",
                  "social media agency founder influencer campaigns"]

AGENCY_PICK = """From these LinkedIn search results, pick people who run or lead influencer / creator
campaigns at an AGENCY in India (influencer marketing agency, social or digital agency that runs
creator campaigns for brands). Priority: head of influencer / creator partnerships / social at bigger agencies, founder / co-founder /
CEO at small ones. At most one person per agency.
Reject: freelancers, creators or influencers themselves, interns, people at brands (not agencies),
creator-tool startups, talent managers for a single creator, anyone outside India, anyone unclear.

Already contacted (skip): {done}
Search results: {cands}

Answer ONLY JSON: [{{"agency": "agency name as on the card", "name": "exact name as on the card",
"url": "...", "title": "...", "why": "under 12 words"}}]. Return [] if nobody fits."""


def agencies(a, con) -> None:
    done = sorted({r["brand"] for r in con.execute("select brand from contacts where brand is not null")})
    queries = AGENCY_QUERIES
    if a.from_:                                      # named agencies: search people at each one
        names = [l.strip() for l in open(a.from_, encoding="utf-8") if l.strip() and not l.startswith("#")]
        queries = [f"{n} influencer" for n in names if n.lower() not in {d.lower() for d in done}]
    urls = ["https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(q)
            for q in queries]
    cands = [c for r in browser.scrape_many(urls, SEARCH_JS, settle=9) for c in json.loads(r or "[]")]
    cands = list({c["url"]: c for c in cands}.values())
    txt = replies.claude(AGENCY_PICK.format(done=json.dumps(done), cands=json.dumps(cands, ensure_ascii=False)),
                         replies.cfg()["claude"])
    picks = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    seen, queued = set(), 0
    for p in picks:
        ag = p.get("agency") or ""
        if not p.get("url") or not p.get("name") or not ag or ag.lower() in seen:
            continue                                 # one person per agency
        vanity = p["url"].rstrip("/").split("/in/")[-1].split("/")[0]
        key = f"li:{vanity}"
        if con.execute("select 1 from contacts where key=?", (key,)).fetchone():
            continue
        ok, _ = li_watch.experience(vanity, ag)
        if not ok:
            print(f"skip {p['name']} ({ag}): agency not current in Experience"); continue
        seen.add(ag.lower())
        print(f"queue {p['name']} · {p['title']} · {ag}")
        if a.dry_run:
            continue
        outbox.upsert_contact(key, channel="linkedin", name=p["name"], brand=ag,
                              url=f"https://www.linkedin.com/in/{vanity}/", status="queued",
                              meta=json.dumps({"title": p["title"], "type": "agency"}))
        outbox.add("li_connect", f"https://www.linkedin.com/in/{vanity}/", "", expected_name=p["name"],
                   contact_key=key, header=f"🏢 Agency: {ag}\n{p['title']} · role re-checked ✓ · {p['why']}")
        queued += 1
    print(f"queued {queued} agency requests for your ✅")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brands", type=int, default=12)
    ap.add_argument("--days", type=int, default=35, help="raised within this many days")
    ap.add_argument("--agencies", action="store_true", help="find agency people instead of funded brands")
    ap.add_argument("--from", dest="from_", help="with --agencies: file of agency names, one per line")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    con = outbox.db()
    if a.agencies:
        return agencies(a, con)
    done = {r["brand"].lower() for r in con.execute("select brand from contacts where brand is not null")}
    brands = [b for b in funding.recent(a.days)
              if b["sector"] and any(k in b["sector"] for k in ("D2C", "QSR", "F&B", "Food", "Beauty"))
              and b["name"].lower() not in done][:a.brands]
    if not brands:
        print("no new funded brands"); return
    urls, keys = [], []
    for b in brands:
        for q in (f"{b['name']} marketing", f"{b['name']} founder"):
            urls.append("https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(q))
            keys.append(b["name"])
    cands: dict[str, list] = {}
    for name, r in zip(keys, browser.scrape_many(urls, SEARCH_JS, settle=9)):
        cands.setdefault(name, []).extend(json.loads(r or "[]"))
    txt = replies.claude(PICK.format(cands=json.dumps(cands, ensure_ascii=False)), replies.cfg()["claude"])
    picks = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    info = {b["name"]: b for b in brands}
    queued = 0
    for p in picks:
        b = info.get(p.get("brand"))
        if not b or not p.get("url") or not p.get("name"):
            print(f"skip {p.get('brand')}: nobody clear"); continue
        vanity = p["url"].rstrip("/").split("/in/")[-1].split("/")[0]
        ok, _ = li_watch.experience(vanity, b["name"])
        if not ok:
            print(f"skip {p['name']} ({b['name']}): brand not current in Experience"); continue
        key = f"li:{vanity}"
        if con.execute("select 1 from contacts where key=?", (key,)).fetchone():
            continue
        print(f"queue {p['name']} · {p['title']} · {b['name']} ({b['amount']} {b['round'] or ''})")
        if a.dry_run:
            continue
        outbox.upsert_contact(key, channel="linkedin", name=p["name"], brand=b["name"],
                              url=f"https://www.linkedin.com/in/{vanity}/", status="queued",
                              meta=json.dumps({"title": p["title"], "raise": f"{b['amount']} {b['round'] or ''} {b['date']}"}))
        outbox.add("li_connect", f"https://www.linkedin.com/in/{vanity}/", "", expected_name=p["name"],
                   contact_key=key,
                   header=f"🎯 {b['name']} raised {b['amount']} {b['round'] or ''} ({b['date']})\n"
                          f"{p['title']} · role re-checked ✓ · {p['why']}")
        queued += 1
    print(f"queued {queued} LinkedIn requests for your ✅")


if __name__ == "__main__":
    main()
