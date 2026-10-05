#!/usr/bin/env python
"""Collect the day's raw material - the "what should I post about" step.

Sources, cheapest and safest first:

  hn      Hacker News via the Algolia API - no auth, no login, structured JSON
  rss     RSS/Atom feeds (configured in config.json -> feeds)
  reddit  DOM scrape through the automation browser (Reddit blocks its .json API)
  x       DOM scrape of X search results (needs a logged-in session)

Everything lands in swipe/YYYY-MM-DD.json, normalised to one item shape:

  {source, title, url, score, comments, created, extra}

Usage:
  python research.py collect [--hours 36] [--skip x]
  python research.py sources                  # per-source health only
  python research.py hn --points 150
  python research.py reddit LocalLLaMA --t day
  python research.py x "AI agents"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import browser
import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
SWIPE = ROOT / "swipe"

DEFAULT_WINDOW = 36


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    txt = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return datetime.strptime(txt, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#x27;", "'"),
                 ("&#39;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&hellip;", "..."),
                 ("&mdash;", "-"), ("&ndash;", "-"), ("&#x2F;", "/")):
        txt = txt.replace(a, b)
    return re.sub(r"\s+", " ", txt).strip()


def get(url: str, timeout: float = 25) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": browser.UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


# ------------------------------------------------------------------ sources

def hn(points: int = 150, hours: int = DEFAULT_WINDOW, query: str | None = None,
       limit: int = 60) -> list[dict]:
    """Hacker News stories via the free Algolia API (no key, no account)."""
    since = int((_now() - timedelta(hours=hours)).timestamp())
    params = {
        "tags": "story",
        "numericFilters": f"points>{points},created_at_i>{since}",
        "hitsPerPage": str(limit),
    }
    if query:
        params["query"] = query
    url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(params)
    data = json.loads(get(url))
    out = []
    for h in data.get("hits", []):
        out.append({
            "source": "hn",
            "title": (h.get("title") or "").strip(),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            "score": h.get("points") or 0,
            "comments": h.get("num_comments") or 0,
            "created": h.get("created_at"),
            "extra": {"hn_id": h.get("objectID"),
                      "discuss": f"https://news.ycombinator.com/item?id={h.get('objectID')}"},
        })
    return out


def rss(feeds: dict[str, str] | None = None, hours: int = DEFAULT_WINDOW,
        per_feed: int = 12) -> tuple[list[dict], dict[str, str]]:
    """Parse RSS 2.0 + Atom with the stdlib only. Returns (items, feed_status)."""
    feeds = feeds if feeds is not None else (settings.load().get("feeds") or {})
    cutoff = _now() - timedelta(hours=hours)
    items: list[dict] = []
    status: dict[str, str] = {}
    for name, url in feeds.items():
        try:
            root = ET.fromstring(get(url))
        except Exception as err:
            status[name] = f"FAIL {type(err).__name__}: {str(err)[:70]}"
            continue
        n = 0
        for node in root.iter():
            if _localname(node.tag) not in ("item", "entry"):
                continue
            title = link = when = summary = ""
            for child in node:
                ln = _localname(child.tag)
                if ln == "title" and not title:
                    title = strip_html(child.text or "")
                elif ln == "link" and not link:
                    link = (child.get("href") or child.text or "").strip()
                elif ln in ("pubdate", "published", "updated", "date") and not when:
                    when = (child.text or "").strip()
                elif ln in ("description", "summary", "content") and not summary:
                    summary = strip_html(child.text or "")[:400]
            if not link:
                continue
            dt = _parse_dt(when)
            if dt and dt < cutoff:
                continue
            items.append({
                "source": f"rss:{name}",
                "title": title,
                "url": link,
                "score": 0,
                "comments": 0,
                "created": dt.isoformat() if dt else when,
                "extra": {"feed": name, "summary": summary},
            })
            n += 1
            if n >= per_feed:
                break
        status[name] = f"ok ({n} in window)"
    return items, status


REDDIT_JS = r"""
(() => {
  const ps = [...document.querySelectorAll('shreddit-post')];
  return JSON.stringify(ps.map(p => ({
    title: p.getAttribute('post-title') || '',
    permalink: p.getAttribute('permalink') || '',
    url: p.getAttribute('content-href') || '',
    score: parseInt(p.getAttribute('score') || '0', 10) || 0,
    comments: parseInt(p.getAttribute('comment-count') || '0', 10) || 0,
    created: p.getAttribute('created-timestamp') || '',
    author: p.getAttribute('author') || '',
    sub: p.getAttribute('subreddit-name') || '',
    kind: p.getAttribute('post-type') || '',
    domain: p.getAttribute('domain') || '',
    body: (p.querySelector('[slot="text-body"]') ? p.querySelector('[slot="text-body"]').innerText : '').slice(0, 600)
  })));
})()
"""


def reddit(subs: list[str] | None = None, t: str = "day") -> list[dict]:
    """Scrape subreddit top listings by reading the rendered DOM.

    Reddit serves its .json API with a network-security block, and old.reddit
    now demands a login - but the modern site renders fine in a real (even
    logged-out) browser, so read the page instead of the API.
    """
    subs = subs or (settings.load().get("subreddits") or [])
    urls = [f"https://www.reddit.com/r/{s}/top/?t={t}" for s in subs]
    raw_list = browser.scrape_many(urls, REDDIT_JS, settle=6, close_after="reddit.com")
    out: list[dict] = []
    for sub, raw in zip(subs, raw_list):
        if not raw:
            print(f"  ! r/{sub}: no data returned", file=sys.stderr)
            continue
        try:
            rows = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as err:
            print(f"  ! r/{sub}: bad JSON ({err})", file=sys.stderr)
            continue
        for r in rows:
            if not r.get("title"):
                continue
            out.append({
                "source": f"reddit:r/{sub}",
                "title": r["title"],
                "url": r.get("url") or f"https://www.reddit.com{r.get('permalink','')}",
                "score": r.get("score") or 0,
                "comments": r.get("comments") or 0,
                "created": r.get("created"),
                "extra": {"sub": sub, "author": r.get("author"), "kind": r.get("kind"),
                          "permalink": f"https://www.reddit.com{r.get('permalink','')}",
                          "body": r.get("body")},
            })
    return out


X_JS = r"""
(() => {
  const arts = [...document.querySelectorAll('article[data-testid="tweet"]')];
  const num = s => { const m = (s||'').match(/[\d.,]+[KMB]?/); if(!m) return 0;
    let v = m[0].replace(/,/g,''); const mult = {K:1e3,M:1e6,B:1e9}[v.slice(-1)];
    if (mult) v = parseFloat(v) * mult; return Math.round(parseFloat(v) || 0) || 0; };
  return JSON.stringify(arts.map(a => {
    const txt = a.querySelector('[data-testid="tweetText"]');
    const grp = a.querySelector('[role="group"][aria-label]');
    const lbl = grp ? grp.getAttribute('aria-label') : '';
    const link = a.querySelector('a[href*="/status/"]');
    const timeEl = a.querySelector('time');
    const user = a.querySelector('[data-testid="User-Name"]');
    return {
      text: txt ? txt.innerText : '',
      url: link ? ('https://x.com' + link.getAttribute('href')) : '',
      author: user ? user.innerText.replace(/\n/g,' ').slice(0,60) : '',
      metrics: lbl,
      likes: num((lbl.match(/([\d.,]+[KMB]?)\s*(?:like|Likes)/i)||[])[1]),
      reposts: num((lbl.match(/([\d.,]+[KMB]?)\s*(?:repost|Retweet)/i)||[])[1]),
      replies: num((lbl.match(/([\d.,]+[KMB]?)\s*(?:repl|comment)/i)||[])[1]),
      views: num((lbl.match(/([\d.,]+[KMB]?)\s*(?:view|View)/i)||[])[1]),
      created: timeEl ? timeEl.getAttribute('datetime') : ''
    };
  }).filter(t => t.text));
})()
"""


def x_search(queries: list[str] | None = None, mode: str = "top") -> list[dict]:
    """Scrape X search results from the DOM. Requires a logged-in session."""
    queries = queries or (settings.load().get("x_queries") or [])
    if not queries:
        return []
    import post
    try:
        info = json.loads(browser.evaluate("https://x.com/home", post.LOGIN_JS, settle=4) or "{}")
    finally:
        browser.close_tabs_matching("x.com/home")
    who = (info.get("handle") or "").lower()
    if not who:
        print("  ! x: research browser not signed in - skipping X search", file=sys.stderr)
        return []
    if who == post.handle().lower():
        print(f"  ! x: research browser is signed in as @{who}, the POSTING account - "
              "refusing to scrape with it. Sign in with the dummy account instead.",
              file=sys.stderr)
        return []
    urls =["https://x.com/search?q=" + urllib.parse.quote(q) + f"&src=typed_query&f={mode}"
            for q in queries]
    raw_list = browser.scrape_many(urls, X_JS, settle=7, close_after="x.com/search")
    out: list[dict] = []
    for q, raw in zip(queries, raw_list):
        if not raw:
            print(f"  ! x '{q}': no data (logged in?)", file=sys.stderr)
            continue
        try:
            rows = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as err:
            print(f"  ! x '{q}': bad JSON ({err})", file=sys.stderr)
            continue
        for r in rows:
            out.append({
                "source": f"x:{q}",
                "title": (r.get("text") or "").split("\n")[0][:140],
                "url": r.get("url"),
                "score": r.get("likes") or 0,
                "comments": r.get("replies") or 0,
                "created": r.get("created"),
                "extra": {"text": r.get("text"), "author": r.get("author"),
                          "views": r.get("views"), "reposts": r.get("reposts"),
                          "metrics": r.get("metrics"), "query": q},
            })
    return out


# ------------------------------------------------------------------ pipeline

def dedupe(items: list[dict]) -> list[dict]:
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    out = []
    for it in items:
        u = (it.get("url") or "").split("?")[0].rstrip("/").lower()
        t = re.sub(r"[^a-z0-9 ]", "", (it.get("title") or "").lower())[:70]
        if u and u in seen_url:
            continue
        if t and t in seen_title:
            continue
        if u:
            seen_url.add(u)
        if t:
            seen_title.add(t)
        out.append(it)
    return out


def rank_key(it: dict) -> tuple:
    """Crude signal score. Tune the weights to your own niche."""
    src = it.get("source", "")
    if src == "hn":
        weight = 1.0
    elif src.startswith("reddit"):
        weight = 0.85
    elif src.startswith("x:"):
        weight = 0.5            # self-reported engagement, least trustworthy
    else:
        weight = 0.6
    eng = (it.get("score") or 0) + 2 * (it.get("comments") or 0)
    dt = _parse_dt(it.get("created"))
    age_h = ((_now() - dt).total_seconds() / 3600) if dt else 999
    return (-(eng * weight), age_h)


def collect(hours: int = DEFAULT_WINDOW, skip: tuple[str, ...] = ()) -> dict:
    items: list[dict] = []
    status: dict[str, str] = {}

    if "hn" not in skip:
        try:
            got = hn(points=150, hours=hours)
            items += got
            status["hn"] = f"ok ({len(got)})"
        except Exception as err:
            status["hn"] = f"FAIL {type(err).__name__}: {str(err)[:80]}"
    if "rss" not in skip:
        got, st = rss(hours=hours)
        items += got
        status.update({f"rss:{k}": v for k, v in st.items()})
    if "reddit" not in skip:
        got = reddit(t="day")
        items += got
        status["reddit"] = f"ok ({len(got)})"
    if "x" not in skip:
        got = x_search()
        items += got
        status["x"] = f"ok ({len(got)})" if got else "EMPTY (needs login?)"

    items = dedupe(items)
    items.sort(key=rank_key)
    return {
        "generated_at": _now().isoformat(),
        "window_hours": hours,
        "counts": {"total": len(items),
                   **{s: sum(1 for i in items
                             if i["source"] == s or i["source"].startswith(s + ":"))
                      for s in ("hn", "rss", "reddit", "x")}},
        "status": status,
        "items": items,
    }


def main() -> None:
    os.environ["TWEETY_BROWSER"] = "research"      # scrape in the dummy account's browser
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="collect",
                    choices=["collect", "sources", "hn", "reddit", "x"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--hours", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--points", type=int, default=150)
    ap.add_argument("--t", default="day")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--out", default=None)
    ap.add_argument("--skip", default=None, help="comma list of sources to SKIP: hn,rss,reddit,x")
    ap.add_argument("--only", default=None, help="comma list of sources to RUN: hn,rss,reddit,x")
    a = ap.parse_args()

    if a.cmd == "sources":
        _, st = rss(hours=a.hours)
        print(json.dumps(st, indent=2))
        return
    if a.cmd == "hn":
        print(json.dumps(hn(points=a.points, hours=a.hours)[:a.limit], indent=2)[:4000])
        return
    if a.cmd == "reddit":
        print(json.dumps(reddit(a.args or None, t=a.t)[:a.limit], indent=2)[:4000])
        return
    if a.cmd == "x":
        print(json.dumps(x_search(a.args or None)[:a.limit], indent=2)[:4000])
        return

    all_src = ("hn", "rss", "reddit", "x")
    if a.only:
        skip = tuple(s for s in all_src if s not in a.only.split(","))
    elif a.skip:
        skip = tuple(s.strip() for s in a.skip.split(",") if s.strip())
    else:
        skip = ()
    data = collect(hours=a.hours, skip=skip)
    out = Path(a.out) if a.out else SWIPE / f"{datetime.now():%Y-%m-%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"out": str(out), "counts": data["counts"]}, indent=2))
    print("--- source status ---")
    print(json.dumps(data["status"], indent=2))
    print("--- top 12 ---")
    for i, it in enumerate(data["items"][:12], 1):
        print(f"{i:2}. [{it['source']}] {it['score']}p/{it['comments']}c  {it['title'][:88]}")


if __name__ == "__main__":
    main()
