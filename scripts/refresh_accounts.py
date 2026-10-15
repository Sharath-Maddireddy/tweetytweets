#!/usr/bin/env python
"""Keep the reply list fresh: drop dead or too-big accounts, add relevant new ones.

Runs weekly in the RESEARCH browser. Candidates come from people replying to
you (future mutuals) and authors of the week's strong posts in your niche;
each is vetted (exists, active, right size) and Claude keeps only the ones that
fit the audience in references/*.local.md. Priority accounts are never touched.

  python scripts/refresh_accounts.py             # update config.json + Telegram summary
  python scripts/refresh_accounts.py --dry-run   # print only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape, unescape

os.environ["TWEETY_BROWSER"] = "research"      # before anything reads settings

import browser
import replies
import research
import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

INACTIVE_DAYS = 14
MAX_FOLLOWERS = 500_000      # replies under bigger accounts get buried
MIN_FOLLOWERS = 1_000        # niche authors below this rarely bring reach...
MIN_FOLLOWERS_FAN = 100      # ...but people already replying to you are worth it
MAX_ADD = 5                  # per run, so the list drifts instead of churning
MAX_LIST = 35

PROFILE_JS = r"""JSON.stringify({
  followers: ([...document.querySelectorAll('a[href$="/verified_followers"],a[href$="/followers"]')]
              .map(a => a.innerText)[0] || ''),
  bio: ((document.querySelector('[data-testid="UserDescription"]') || {}).innerText || '').slice(0, 160),
  last: [...document.querySelectorAll('article time')].map(t => t.getAttribute('datetime')).sort().pop() || '',
  missing: /This account doesn.t exist|Account suspended/.test(document.body.innerText)
})"""

PROMPT = """You maintain the list of X accounts @{handle} replies to, to grow his account.

Who he is and what he posts about:
<context>
{context}
</context>

Candidates (JSON). "why" says how each was found, "sample" is one of their posts:
{cands}

Pick the ones whose followers are his audience: brand marketers, D2C founders,
agency founders and operators, growth / performance marketers, and AI builders.
Not creator coaches or creator-side tools: his customers are brands and agencies. Reject: crypto/memecoin/trading, engagement-bait or giveaway farms,
politics, news aggregators, brand accounts, anything off-topic.
Pick at most {max_add}. Prefer people replying to him (mutuals count most).

Answer with ONLY a JSON array of handles, e.g. ["handle1", "handle2"], or [].
"""


def followers(text: str) -> int:
    m = re.match(r"([\d.,]+)\s*([KMB]?)", text.strip())
    if not m:
        return 0
    return int(float(m.group(1).replace(",", "")) * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[m.group(2)])


def vet(handles: list[str]) -> dict[str, dict]:
    raw = browser.scrape_many([f"https://x.com/{h}" for h in handles], PROFILE_JS, settle=6)
    out = {}
    for h, r in zip(handles, raw):
        d = json.loads(r) if r else {"missing": True}
        d["n"] = followers(d.get("followers", ""))
        d["last_dt"] = research._parse_dt(d.get("last"))
        out[h] = d
    return out


def handle_of(item: dict) -> str:
    return (item["extra"].get("author") or "").split("@")[-1].split(" ")[0].strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = settings.load()
    me = cfg["handle"]
    r = cfg.get("replies") or {}
    current, priority = r.get("accounts") or [], r.get("priority") or []
    known = {h.lower() for h in current + priority + [me]}
    now = datetime.now(timezone.utc)

    # 1. prune
    info = vet(current)
    keep, dropped = [], []
    for h in current:
        d = info[h]
        if d.get("missing"):
            dropped.append((h, "gone"))
        elif not d["last_dt"] or d["last_dt"] < now - timedelta(days=INACTIVE_DAYS):
            dropped.append((h, f"quiet {INACTIVE_DAYS}d+"))
        elif d["n"] > MAX_FOLLOWERS:
            dropped.append((h, "too big"))
        else:
            keep.append(h)

    # 2. discover
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    found: dict[str, dict] = {}
    for it in research.x_search([f"to:{me} since:{since}"], mode="live"):
        h = handle_of(it)
        if h and h.lower() not in known:
            found.setdefault(h, {"why": "replied to you", "sample": it["extra"]["text"][:200]})
    niche = [f"{q} min_faves:100 since:{since} -filter:replies lang:en"
             for q in cfg.get("x_queries") or []]
    for it in sorted(research.x_search(niche, mode="top"), key=lambda i: -i["score"]):
        h = handle_of(it)
        if h and h.lower() not in known:
            found.setdefault(h, {"why": f"strong post on '{it['extra']['query'].split(' min_')[0]}'",
                                 "sample": it["extra"]["text"][:200]})
    cands = list(found)[:25]
    vetted = vet(cands) if cands else {}
    ok = []
    for h in cands:
        d = vetted[h]
        floor = MIN_FOLLOWERS_FAN if found[h]["why"] == "replied to you" else MIN_FOLLOWERS
        if (not d.get("missing") and floor <= d["n"] <= MAX_FOLLOWERS and d["last_dt"]
                and d["last_dt"] > now - timedelta(days=7)):
            ok.append({"handle": h, "followers": d["n"], "bio": d.get("bio", ""), **found[h]})

    # 3. choose
    room = max(0, min(MAX_ADD, MAX_LIST - len(keep)))
    added = []
    if ok and room:
        context = "\n\n".join(p.read_text(encoding="utf-8")
                              for p in sorted((replies.ROOT / "references").glob("*.local.md")))
        txt = replies.claude(PROMPT.format(handle=me, context=context, max_add=room,
                                           cands=json.dumps(ok, ensure_ascii=False, indent=1)),
                             replies.cfg()["claude"])
        try:
            picked = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
        except ValueError:
            picked = []
        valid = {c["handle"].lower(): c["handle"] for c in ok}
        added = [valid[p.lower().lstrip("@")] for p in picked
                 if isinstance(p, str) and p.lower().lstrip("@") in valid][:room]

    new_list = keep + added
    why = {c["handle"]: c["why"] for c in ok}
    lines = [f"<b>Reply list refreshed</b> · {len(new_list)} accounts"]
    lines += [f"+ @{escape(h)} ({escape(why[h])})" for h in added]
    lines += [f"− @{escape(h)} ({escape(reason)})" for h, reason in dropped]
    if not added and not dropped:
        lines.append("no changes")
    print(unescape("\n".join(lines)))
    if a.dry_run:
        return

    raw = json.loads(settings.CONFIG_PATH.read_text(encoding="utf-8"))   # unmerged: keep the file as-is
    raw.setdefault("replies", {})["accounts"] = new_list
    settings.CONFIG_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    if cfg.get("telegram", {}).get("bot_token"):
        replies.send(["\n".join(lines)], cfg["telegram"])


if __name__ == "__main__":
    main()
