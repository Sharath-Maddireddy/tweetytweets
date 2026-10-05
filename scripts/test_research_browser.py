"""Self-check: research uses its own browser and never scrapes as the posting account.

  python scripts/test_research_browser.py
"""
import json
import os

import browser
import post
import research
import settings

os.environ.pop("TWEETY_BROWSER", None)
main = settings.load()["browser"]
os.environ["TWEETY_BROWSER"] = "research"
res = settings.load()["browser"]
assert res["port"] != main["port"], (res, main)
assert res["profile_dir"] != main["profile_dir"], (res, main)

scraped = []
browser.close_tabs_matching = lambda s: 0
browser.scrape_many = lambda urls, *a, **k: scraped.extend(urls) or [None] * len(urls)
post.handle = lambda: "RealMe"


def signed_in_as(h):
    browser.evaluate = lambda *a, **k: json.dumps({"handle": h})


signed_in_as("realme")                      # posting account -> refuse
assert research.x_search(["ai"]) == [] and not scraped
signed_in_as(None)                          # not signed in -> skip
assert research.x_search(["ai"]) == [] and not scraped
signed_in_as("dummy123")                    # dummy -> scrape
research.x_search(["ai"])
assert scraped, "dummy account should scrape"
print("ok")
