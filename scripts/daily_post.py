#!/usr/bin/env python
"""Draft today's X post (3 options + a pick) and send it to Telegram. Never posts.

Reads what's already been posted (and how it did) from the profile via the
RESEARCH browser, plus references/*.local.md (strategy, facts).

  python scripts/daily_post.py             # send to Telegram
  python scripts/daily_post.py --dry-run   # print only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from html import escape

os.environ["TWEETY_BROWSER"] = "research"      # before anything reads settings

import browser
import replies
import research
import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROMPT = """You write today's X post for @{handle}. He posts it himself.

Strategy, voice and facts (only claim what's stated here; never invent numbers,
quotes, customers or results):
<context>
{context}
</context>

His recent posts, newest first, with how they did:
{history}

Posts that went viral in his niche (structure only, never copy wording or claims):
{examples}

STYLE (he asked for this explicitly): SPICY TAKES. Strong, defensible opinions about
industry practices that a real share of his audience will disagree with and reply to.
Topics he owns: how brands and agencies run influencer campaigns (spreadsheets, DMs,
guesswork, follower counts, vanity metrics), creator marketing in India, building
a startup with AI agents. Creonnect now serves brands and agencies, not creators.
Good: "Influencer marketing in India is mostly brands paying for followers they can't
verify." / "Micro creators aren't cheaper. They're priced correctly. Big creators are
overpriced." / "Vibe coding didn't kill software engineers. It killed the ones whose
whole job was typing."
Rules for spice: attack practices and ideas, NEVER a named person, brand or company;
no politics, religion, gender or region-bashing; nothing he couldn't defend in the
replies. Safe, balanced or "it depends" takes are a fail. Short: 1-3 lines usually.
A real data point can back a take, but the take comes first.

Write 3 options, each a DIFFERENT format and topic, none repeating a recent post:
- formats: one-line claim + twist, contrast (A does X, B does Y), short list then verdict.
- one sharp idea, under 280 characters, short lines, blank line between beats,
  hook in the first 6-8 words. No hashtags, no emoji, no em dashes, no links,
  no "bookmark this", no questions-as-bait, no product pitch line.
- posts about him / what he's building / creator-brand insight did better than pitches.
- never add story details the facts don't state: no "today", "this week", "first we
  tried", "it got worse", no invented sequence of events. Opinions are fine; events
  must be exactly as stated in the facts.
Then pick the one most likely to get people arguing in the replies, and say why in under 15 words.

Answer ONLY JSON: {{"options": [{{"format": "...", "text": "..."}}, ...], "pick": 1, "why": "..."}}
"""


def history(handle: str) -> list[dict]:
    rows = json.loads(browser.scrape_many([f"https://x.com/{handle}"], research.X_JS, settle=9)[0] or "[]")
    mine = [r for r in rows if f"@{handle.lower()}" in (r.get("author") or "").lower()]
    return sorted(mine, key=lambda r: r["created"], reverse=True)[:10]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = settings.load()
    handle = cfg["handle"]
    posts = history(handle)
    hist = "\n".join(f"- {p['created'][:10]} | {p.get('metrics', '')} | {p['text'][:200]!r}" for p in posts) or "(none)"
    context = "\n\n".join(p.read_text(encoding="utf-8")
                          for p in sorted((replies.ROOT / "references").glob("*.local.md")))
    ex_file = replies.ROOT / "references" / "viral_examples.txt"
    examples = ex_file.read_text(encoding="utf-8") if ex_file.exists() else "(none)"
    txt = replies.claude(PROMPT.format(handle=handle, context=context, history=hist, examples=examples),
                         replies.cfg()["claude"])
    try:
        out = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
    except ValueError:
        raise SystemExit(f"model returned no JSON:\n{txt[:400]}")
    opts = [o for o in out.get("options", []) if isinstance(o, dict) and 0 < len(o.get("text", "")) <= 280]
    if not opts:
        raise SystemExit("no usable options")
    pick = out.get("pick") if isinstance(out.get("pick"), int) and 1 <= out["pick"] <= len(opts) else 1
    msgs = [f"<b>🌶 Today's post</b> · post at 21:00 IST, then answer every comment for 2 hours\n"
            f"👉 pick: option {pick} ({escape(out.get('why', ''))})"]
    for i, o in enumerate(opts, 1):
        msgs.append(f"<b>{'👉 ' if i == pick else ''}{i}. {escape(o.get('format', ''))}</b>\n\n<code>{escape(o['text'])}</code>")
    if a.dry_run:
        print("\n\n----\n\n".join(msgs))
        return
    replies.send(msgs, cfg["telegram"])
    print(f"sent {len(opts)} options to Telegram (pick {pick})")


if __name__ == "__main__":
    main()
