#!/usr/bin/env python
"""Recently funded Indian consumer brands, from Inc42's weekly funding tables.

Company-level public news only (name, sector, round, amount, investors). These
are the brands with fresh budget and investors asking for growth.

  python scripts/funding.py              # last 35 days, consumer/D2C only, as JSON
  python scripts/funding.py --days 60 --all
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime, timedelta
from html.parser import HTMLParser

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"}
INDEX = ["https://inc42.com/tag/funding-galore/", "https://inc42.com/tag/weekly-funding-report/"]
ROUNDUP = re.compile(r"https://inc42\.com/buzz/from-[a-z0-9-]+-indian-startups-raised-[a-z0-9-]+/")
CACHE = Path(__file__).resolve().parent.parent / "drafts" / "funding_cache.json"   # published roundups never change
CONSUMER = re.compile(r"D2C|QSR|Food|Beauty|Fashion|Personal Care|Consumer|Beverage|Jewel|Pet", re.I)


def get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


class Tables(HTMLParser):
    """Collect every <tr> as a list of cell texts."""

    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(" ".join(self.cell.split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data


def rows(url: str) -> list[dict]:
    p = Tables()
    p.feed(get(url))
    head = next((r for r in p.rows if r[:2] == ["Date", "Name"]), None)
    if not head:
        return []
    out = []
    for r in p.rows:
        if len(r) != len(head) or r == head:
            continue
        d = dict(zip(head, r))
        try:
            d["date"] = datetime.strptime(d["Date"], "%d %b %Y").date().isoformat()
        except ValueError:
            continue                                   # typos like "2926" just get skipped
        d["source"] = url
        out.append(d)
    return out


def cached_rows(urls: list[str]) -> list[dict]:
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    out = []
    for u in urls:
        if u not in cache:
            try:
                cache[u] = rows(u)
            except urllib.error.HTTPError as err:          # 429 etc: skip now, retry next run
                print(f"  ! {u}: {err}", file=sys.stderr)
                continue
        out += cache[u]
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return out


def recent(days: int = 35, consumer_only: bool = True) -> list[dict]:
    urls = set()
    for i in INDEX:
        try:
            urls |= set(ROUNDUP.findall(get(i)))
        except urllib.error.HTTPError as err:
            print(f"  ! {i}: {err}", file=sys.stderr)
    if CACHE.exists():                                # index rate-limited? still use what we have
        urls |= set(json.loads(CACHE.read_text(encoding="utf-8")))
    cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
    seen, out = set(), []
    for d in cached_rows(sorted(urls)):
        if d["date"] < cutoff or d["Name"].lower() in seen:
            continue
        biz = d.get("Business Model", "")
        sub = f"{d.get('Sector', '')} {d.get('Subsector', '')}"
        if consumer_only and not (("B2C" in biz) and CONSUMER.search(sub)):
            continue
        seen.add(d["Name"].lower())
        out.append({"name": d["Name"], "date": d["date"], "sector": sub.strip(),
                    "round": d.get("Funding Round Type", ""),
                    "amount": d.get("Funding Round Size", ""),
                    "investors": d.get("Investors", ""), "source": d["source"]})
    return sorted(out, key=lambda x: x["date"], reverse=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=35)
    ap.add_argument("--all", action="store_true", help="every sector, not just consumer brands")
    a = ap.parse_args()
    print(json.dumps(recent(a.days, not a.all), indent=1, ensure_ascii=False))
