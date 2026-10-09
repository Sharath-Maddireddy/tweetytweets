#!/usr/bin/env python
"""Watch your latest post and send reply drafts to Telegram for each new comment.

Runs in the RESEARCH browser. Never replies on X itself.

  python scripts/watch_comments.py                  # latest post, 150 min, every 4 min
  python scripts/watch_comments.py --minutes 120 --every 5
  python scripts/watch_comments.py --url https://x.com/you/status/123
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from html import escape

os.environ["TWEETY_BROWSER"] = "research"      # before anything reads settings

import browser
import replies
import research
import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROMPT = """@{handle} posted this on X:
<post>
{post}
</post>

Someone commented. Write 2 replies he could send back, from him, as the author.

Context about him (follow the NEVER list):
<context>
{context}
</context>

Goal: keep the conversation going, because replies under your own post are the
strongest reach signal. So:
- answer what they actually said. if they asked something, answer it plainly.
- when it fits, end with a short real question back to them. not on every reply.
- match their tone: joke back to a joke, straight answer to a question,
  friendly and unbothered to pushback (concede what's fair, never defensive).
- one or two short lines, casual, lowercase fine, under 140 characters.
- no em dashes, no hashtags, "lol" only rarely, no "great question", no "thanks for sharing".
- only claim things stated in the post or context. never invent numbers.

Comment from {author}:
<comment>
{comment}
</comment>

Answer with ONLY a JSON array: [{{"tone": "...", "text": "..."}}, {{"tone": "...", "text": "..."}}]
"""


def comments(url: str) -> list[dict]:
    """Every reply in the conversation. Search, not the thread page: the page also
    lists 'more posts' that aren't replies."""
    pid = url.rstrip("/").split("/")[-1]
    return [{"url": i["url"], "author": i["extra"]["author"] or "", "text": i["extra"]["text"] or ""}
            for i in research.x_search([f"conversation_id:{pid}"], mode="live")]


def latest_post(handle: str) -> dict:
    rows = json.loads(browser.scrape_many([f"https://x.com/{handle}"], research.X_JS, settle=8)[0] or "[]")
    mine = [r for r in rows if f"@{handle.lower()}" in r["author"].lower()]
    return max(mine, key=lambda r: r["created"])          # newest, so a pinned post doesn't win


def draft(post: str, c: dict, handle: str) -> list[dict]:
    context = "\n\n".join(p.read_text(encoding="utf-8")
                          for p in sorted((replies.ROOT / "references").glob("*.local.md")))
    txt = replies.claude(PROMPT.format(handle=handle, post=post, context=context,
                                       author=c["author"].split(" · ")[0], comment=c["text"]),
                         replies.cfg()["claude"])
    try:
        out = json.loads(txt[txt.index("["): txt.rindex("]") + 1])
    except ValueError:
        return []
    return [d for d in out if isinstance(d, dict) and d.get("text")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--minutes", type=int, default=150)
    ap.add_argument("--every", type=int, default=4)
    a = ap.parse_args()
    cfg = settings.load()
    handle, tg = cfg["handle"], cfg["telegram"]

    post = {"url": a.url, "text": ""} if a.url else latest_post(handle)
    if not post["text"]:
        post["text"] = (json.loads(browser.scrape_many([post["url"]], research.X_JS, settle=8)[0]
                                   or "[{}]")[0].get("text") or "")
    url = post["url"].split("/analytics")[0]
    print(f"watching {url}", flush=True)
    seen: set[str] = set()
    end = time.time() + a.minutes * 60
    while time.time() < end:
        try:
            rows = comments(url)
        except Exception as err:                         # one bad poll shouldn't end the watch
            print(f"poll failed: {err}", flush=True)
            rows = []
        for c in rows:
            if (not c["url"] or c["url"] in seen or f"@{handle.lower()}" in c["author"].lower()):
                continue
            seen.add(c["url"])
            drafts = draft(post["text"], c, handle)
            if not drafts:
                continue
            body = "\n\n".join(f"<b>{escape(d.get('tone', ''))}</b>\n<code>{escape(d['text'])}</code>"
                               for d in drafts)
            replies.send([f"💬 <b>New comment</b> from {escape(c['author'].split(' · ')[0])}\n"
                          f"<i>{escape(c['text'][:280])}</i>\n\n{body}\n\n{c['url']}"], tg)
            print(f"sent drafts for {c['author'].split(' · ')[0]}", flush=True)
        time.sleep(a.every * 60)
    print("done", flush=True)


if __name__ == "__main__":
    main()
