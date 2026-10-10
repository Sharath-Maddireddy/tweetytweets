#!/usr/bin/env python
"""Send ONE X DM from the posting account, only when explicitly told to.

Never called by any schedule. Run it per message, after the user has approved
that exact text for that exact recipient.

  python scripts/dm.py --to daily_objects --text "..."            # sends
  python scripts/dm.py --to daily_objects --text "..." --check    # opens the composer, sends nothing
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import browser
import post

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

COMPOSER = '[data-testid="dm-composer-textarea"], [data-testid="dmComposerTextInput"]'
SEND = ('[data-testid="dm-composer-send-button"], [data-testid="dmComposerSendButton"], '
        'button[aria-label="Send"], button[aria-label="Send message"]')

# XChat renders inside an embedded frame / shadow root, so plain querySelector
# can't see it. deep(sel) searches through both; deepText() reads them too.
DEEP = r"""
window.__deep = (sel) => { let hit = null; const walk = (root, d) => {
  if (hit || d > 8) return; const m = root.querySelector(sel); if (m) { hit = m; return; }
  root.querySelectorAll('*').forEach(el => { if (hit) return;
    if (el.shadowRoot) walk(el.shadowRoot, d + 1);
    if (el.tagName === 'IFRAME') { try { if (el.contentDocument) walk(el.contentDocument, d + 1); } catch (e) {} } }); };
  walk(document, 0); return hit; };
window.__deepText = () => { let t = ''; const walk = (root, d) => { if (d > 8) return;
  t += ' ' + (root.body ? root.body.innerText : root.textContent);
  root.querySelectorAll('*').forEach(el => {
    if (el.shadowRoot) walk(el.shadowRoot, d + 1);
    if (el.tagName === 'IFRAME') { try { if (el.contentDocument) walk(el.contentDocument, d + 1); } catch (e) {} } }); };
  walk(document, 0); return t; };
true"""


async def run(to: str, text: str, check: bool) -> dict:
    page = await post._open(f"https://x.com/{to}")
    try:
        await browser.settle_page(page, 6)
        if not await page.eval("""!!document.querySelector('[data-testid="sendDMFromProfile"]')"""):
            return {"sent": False, "error": f"@{to} has no Message button (DMs closed to you)"}
        await page.eval("""document.querySelector('[data-testid="sendDMFromProfile"]').click()""")
        for _ in range(20):                                  # composer renders async
            await asyncio.sleep(0.75)
            await page.eval(DEEP)
            if await page.eval(f"!!__deep({json.dumps(COMPOSER)})"):
                break
        else:
            body = await page.eval("document.body.innerText.slice(0, 400)")
            return {"sent": False, "error": "DM composer never appeared", "page": body}
        existing = await page.eval(f"(__deep({json.dumps(COMPOSER)}).value || "
                                   f"__deep({json.dumps(COMPOSER)}).innerText || '').trim()")
        if existing:                                         # never append to a leftover draft
            return {"sent": False, "error": "composer already has text; clear it first", "draft": existing[:120]}
        await page.eval(f"__deep({json.dumps(COMPOSER)}).focus()")
        await page.send("Input.insertText", text=text)
        await asyncio.sleep(1.0)
        typed = await page.eval(f"__deep({json.dumps(COMPOSER)}).value || __deep({json.dumps(COMPOSER)}).innerText || ''")
        if typed.strip() != text.strip():                    # never send something other than the approved text
            return {"sent": False, "error": "composer text doesn't match the approved text", "typed": typed[:200]}
        if check:
            return {"sent": False, "check": "composer filled, nothing sent",
                    "url": await page.eval("location.href")}
        clicked = await page.eval(f"""(() => {{ const b = __deep({json.dumps(SEND)});
            if (!b || b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
            b.click(); return true; }})()""")
        if not clicked:
            return {"sent": False, "error": "send button missing or disabled"}
        probe = json.dumps(text[:40])
        for _ in range(15):                                  # verify by reading it back
            await asyncio.sleep(1.0)
            box = await page.eval(f"(__deep({json.dumps(COMPOSER)}) || {{}}).value || ''")
            if probe.strip('"') not in box and await page.eval(f"__deepText().includes({probe})"):
                return {"sent": True, "to": to, "url": await page.eval("location.href")}
        return {"sent": "unverified", "to": to,
                "note": "clicked send but couldn't read the message back; check the DM thread"}
    finally:
        await post._close(page)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", required=True)
    ap.add_argument("--text", required=True)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    post.require_session()                                   # refuses unless signed in as config handle
    print(json.dumps(asyncio.run(run(a.to.lstrip("@"), a.text, a.check)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
