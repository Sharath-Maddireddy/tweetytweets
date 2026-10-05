#!/usr/bin/env python
"""Reply queue: find fresh posts worth replying to, draft replies, send to Telegram.

It never replies on X. You open each link, edit the draft, and post it yourself:
automated replies break X's automation rules, and the conversation that follows
is the part that actually grows an account.

Runs in the RESEARCH browser (the dummy account), like research.py.

  python scripts/replies.py             # find + draft + send to Telegram
  python scripts/replies.py --dry-run   # print instead of sending
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

os.environ["TWEETY_BROWSER"] = "research"      # before anything reads settings

import browser
import research
import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SENT = ROOT / "drafts" / "replies_sent.json"

RESTRICTED_JS = r"/can reply\./i.test(document.body.innerText)"

PROMPT = """You draft X replies for @{handle}. He posts them himself after editing.

Context about him, his content strategy and rules (follow the NEVER list):
<context>
{context}
</context>

For each post below, write 2 reply options he could post, OR "skip" if he has
no genuine angle on it (better no reply than a forced one).

How replies must read - like a quick reply typed on a phone by a 22-year-old founder:
- lowercase is fine, contractions, casual. under 200 characters.
- add something: an opinion, a counterpoint, a small specific detail. never just praise.
- no em dashes, no "it's not X, it's Y", no tidy lists of three, no hashtags,
  no emoji except maybe one "lol"/"tbh"-level casual marker, no questions-as-bait.
- only claim personal experience that is stated in the context. otherwise give an opinion.
- never invent numbers, quotes or facts. numbers only if they are in the post itself.

Posts (JSON):
{posts}

Answer with ONLY a JSON array, one object per post, same order:
[{{"url": "...", "replies": ["...", "..."]}}]  or  [{{"url": "...", "replies": "skip"}}]
"""


def cfg() -> dict:
    c = settings.load()
    r = c.get("replies") or {}
    return {
        "handle": c.get("handle", ""),
        "accounts": r.get("accounts") or [],
        "queries": r.get("queries") or [],
        "max_age_min": int(r.get("max_age_min", 90)),
        "max_posts": int(r.get("max_posts", 6)),
        "claude": r.get("claude_path") or shutil.which("claude") or "claude",
        "telegram": c.get("telegram") or {},
    }


def find(c: dict) -> list[dict]:
    """Fresh, open-to-replies posts: watched accounts first, then niche searches."""
    qs = []
    if c["accounts"]:
        qs.append("(" + " OR ".join(f"from:{a}" for a in c["accounts"]) + ") -filter:replies")
    qs += [f"{q} min_faves:10 -filter:replies lang:en" for q in c["queries"]]
    items = research.x_search(qs, mode="live")

    sent = set(json.loads(SENT.read_text()) if SENT.exists() else [])
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=c["max_age_min"])
    me = "@" + c["handle"].lower()
    seen, fresh = set(), []
    for it in items:
        url, created = it.get("url") or "", research._parse_dt(it.get("created"))
        if (not url or url in seen or url in sent or not created or created < cutoff
                or me in (it["extra"].get("author") or "").lower()):
            continue
        seen.add(url)
        it["watched"] = it["source"] == f"x:{qs[0]}" and bool(c["accounts"])
        fresh.append(it)
    # watched accounts first, then the least crowded threads (your reply gets seen)
    fresh.sort(key=lambda i: (not i["watched"], i["comments"]))
    authors, pool = set(), []
    for i in fresh:                       # one post per account per round
        a = (i["extra"].get("author") or "").split("@")[-1].split(" ")[0].lower()
        if a not in authors:
            authors.add(a)
            pool.append(i)
    pool = pool[: c["max_posts"] * 2]
    restricted = browser.scrape_many([i["url"] for i in pool], RESTRICTED_JS, settle=5,
                                     close_after="x.com/")
    return [i for i, r in zip(pool, restricted) if r is False][: c["max_posts"]]


def draft(posts: list[dict], c: dict) -> list[dict]:
    context = "\n\n".join(p.read_text(encoding="utf-8")
                          for p in sorted((ROOT / "references").glob("*.local.md")))
    slim = [{"url": p["url"], "author": p["extra"].get("author"), "text": p["extra"].get("text")}
            for p in posts]
    prompt = PROMPT.format(handle=c["handle"], context=context,
                           posts=json.dumps(slim, ensure_ascii=False, indent=1))
    txt = claude(prompt, c["claude"])
    try:
        rows = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    except ValueError as err:
        raise SystemExit(f"model returned no JSON array: {err}\n{txt[:400]}")
    by_url = {r.get("url"): r.get("replies") for r in rows}
    return [{**p, "drafts": by_url[p["url"]]} for p in posts
            if isinstance(by_url.get(p["url"]), list) and by_url[p["url"]]]


def claude(prompt: str, exe: str) -> str:
    """Claude Code headless, authed by the long-lived token from `claude setup-token`.

    The token is passed explicitly so a scheduled run doesn't depend on an
    interactive login (which expires) or on whatever env the caller has.
    """
    token_file = Path.home() / ".tweetytweets" / "claude_token"
    token = token_file.read_text().strip() if token_file.exists() else ""
    if not token:
        raise SystemExit(f"paste the `claude setup-token` token into {token_file}")
    # drop any inherited Claude/Anthropic session settings (e.g. when launched from
    # inside the Claude app) so only this token decides who we are
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    out = subprocess.run([exe, "-p", "--tools", "", "--model", "sonnet"], input=prompt,
                         capture_output=True, text=True, timeout=300, env=env)
    if out.returncode:
        raise SystemExit(f"claude failed: {(out.stderr or out.stdout)[:400]}")
    return out.stdout


def age(p: dict) -> str:
    mins = int((datetime.now(timezone.utc) - research._parse_dt(p["created"])).total_seconds() // 60)
    return f"{mins}m ago"


def messages(posts: list[dict]) -> list[str]:
    """One Telegram message per post; drafts in <code> so a tap copies them."""
    out = [f"<b>Reply queue</b> · {len(posts)} posts · edit before posting, then answer anyone who replies"]
    for n, p in enumerate(posts, 1):
        e = p["extra"]
        drafts = "\n\n".join(f"<code>{escape(d)}</code>" for d in p["drafts"])
        out.append(f"<b>{n}. {escape((e.get('author') or '').split(' · ')[0])}</b> · {age(p)} · {p['comments']} replies\n"
                   f"<i>{escape((e.get('text') or '')[:280])}</i>\n\n{drafts}\n\n{p['url']}")
    return out


def send(texts: list[str], tg: dict) -> None:
    for t in texts:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
            data=json.dumps({"chat_id": tg["chat_id"], "text": t, "parse_mode": "HTML",
                             "disable_web_page_preview": True}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    a = ap.parse_args()
    c = cfg()
    tg = c["telegram"]
    if not a.dry_run and not (tg.get("bot_token") and tg.get("chat_id")):
        raise SystemExit("set telegram.bot_token and telegram.chat_id in config.json, or use --dry-run")

    posts = find(c)
    if not posts:
        print("no fresh open posts found")
        return
    found = len(posts)
    posts = draft(posts, c)
    print(f"found {found} fresh open posts, drafted {len(posts)} (rest skipped: no genuine angle)")
    texts = messages(posts)
    if a.dry_run:
        print("\n\n----\n\n".join(texts))
    else:
        send(texts, tg)
        print(f"sent {len(posts)} posts to Telegram")
        SENT.parent.mkdir(exist_ok=True)
        old = json.loads(SENT.read_text()) if SENT.exists() else []
        SENT.write_text(json.dumps((old + [p["url"] for p in posts])[-500:]))   # never re-send


if __name__ == "__main__":
    main()
