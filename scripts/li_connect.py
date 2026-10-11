#!/usr/bin/env python
"""Send ONE LinkedIn connection request (with note if LinkedIn allows) from the
posting browser, only after the user approved that exact person and note.

  python scripts/li_connect.py --url https://www.linkedin.com/in/x --note "..." --check   # fills, sends nothing
  python scripts/li_connect.py --url https://www.linkedin.com/in/x --note "..."           # sends

If LinkedIn refuses a note (free-plan note limit), it sends the request without
one and says so, so the note can be used as the first message after they accept.
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

# Find a visible button by its visible text or aria-label, anywhere on the page.
FIND = r"""
window.__all = (sel) => { const out = []; const walk = (root, d) => { if (d > 8) return;
  root.querySelectorAll(sel).forEach(e => out.push(e));
  root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) walk(el.shadowRoot, d + 1);
    if (el.tagName === 'IFRAME') { try { if (el.contentDocument) walk(el.contentDocument, d + 1); } catch (e) {} } }); };
  walk(document, 0); return out; };
window.__btn = (re) => __all('button, a, div[role="button"], span[role="button"]')
  .find(b => (re.test((b.innerText || '').trim()) || re.test((b.getAttribute('aria-label') || '').trim()))
               && (b.offsetParent !== null || b.getClientRects().length));
window.__dlgBtn = (re) => { const d = __all('[role="dialog"]').find(x => /invitation/i.test(x.innerText || ''));
  if (!d) return null; const scope = [...d.querySelectorAll('button')].concat(d.shadowRoot ? [...d.shadowRoot.querySelectorAll('button')] : []);
  return scope.find(b => re.test((b.innerText || '').trim()) || re.test((b.getAttribute('aria-label') || '').trim())); };
window.__box = () => __all('textarea[name="message"], #custom-message, textarea')[0];
true"""

STATE = r"""JSON.stringify({
  pending: !!__btn(/^Pending|Withdraw invitation/i),
  connected: !!__btn(/^Message\b/) && !__btn(/Invite .* to connect|^Connect$/i) && /1st/.test(document.body.innerText.slice(0, 4000)),
  name: document.title.split(' | ')[0]})"""


async def click(page, pattern: str) -> bool:
    return await page.eval(f"(() => {{ const b = __btn({pattern}); if (!b) return false; b.click(); return true; }})()")


NOTE_MAX = 200          # free plan: 200 chars, and only a few noted invites per month


async def run(url: str, note: str, check: bool) -> dict:
    if len(note) > NOTE_MAX:
        return {"sent": False, "error": f"note is {len(note)} chars; LinkedIn free plan allows {NOTE_MAX}"}
    page = await post._open(url)
    try:
        await browser.settle_page(page, 7)
        await page.eval(FIND)
        st = json.loads(await page.eval(STATE))
        if st["pending"]:
            return {"sent": False, "status": "already pending", **st}
        # Connect is either on the top card or inside the "More" menu
        if not await click(page, r"/Invite .* to connect|^Connect$/i"):
            await click(page, r"/^More$|More actions/i")
            await asyncio.sleep(1.2)
            await page.eval(FIND)
            if not await click(page, r"/Invite .* to connect|^Connect$/i"):
                return {"sent": False, "error": "no Connect option (already connected, or Follow-only profile)", **st}
        await asyncio.sleep(1.8)
        await page.eval(FIND)
        used_note = False
        if note and await click(page, r"/^Add a note$/i"):
            await asyncio.sleep(1.2)
            box = await page.eval("""(() => { const t = __box();
                if (!t) return 'none'; if (t.value.trim()) return 'dirty'; t.focus(); return 'ok'; })()""")
            if box == "ok":
                await page.send("Input.insertText", text=note)
                await asyncio.sleep(0.8)
                typed = await page.eval("(__box() || {}).value || ''")
                if typed.strip() != note.strip():
                    return {"sent": False, "error": "note box text doesn't match the approved note", "typed": typed[:120]}
                used_note = True
            else:
                # premium upsell / no box: back out to the no-note path
                await page.eval(FIND)
                await click(page, r"/^Dismiss$|^Close$|Got it|^Back$/i")
                await asyncio.sleep(1)
        if check:
            return {"sent": False, "check": "dialog ready, nothing sent", "with_note": used_note, **st}
        await page.eval(FIND)
        btn = r"/^Send( invitation| now)?$|Send without a note/i"
        if await page.eval(f"(() => {{ const b = __dlgBtn({btn}); return !b ? 'missing' : (b.disabled || b.getAttribute('aria-disabled') === 'true') ? 'disabled' : 'ok'; }})()") != "ok":
            limit = await page.eval(r"""(__all('[role="dialog"]').map(d => d.innerText).join(' ').match(/[^\n]*(remaining|limit)[^\n]*/i) || [''])[0]""")
            return {"sent": False, "error": "send button missing or disabled", "with_note": used_note, "dialog": limit}
        # a real (trusted) mouse click at the dialog's Send button; LinkedIn ignored el.click() here
        pos = json.loads(await page.eval(f"""(() => {{ const b = __dlgBtn({btn}); b.scrollIntoView({{block: 'center'}});
            const r = b.getBoundingClientRect(); return JSON.stringify({{x: r.x + r.width / 2, y: r.y + r.height / 2}}); }})()"""))
        for ev in ("mousePressed", "mouseReleased"):
            await page.send("Input.dispatchMouseEvent", type=ev, x=pos["x"], y=pos["y"], button="left", clickCount=1)
        for _ in range(10):
            await asyncio.sleep(1.0)
            await page.eval(FIND)
            if json.loads(await page.eval(STATE))["pending"]:
                return {"sent": True, "with_note": used_note, "name": st["name"]}
        return {"sent": "unverified", "with_note": used_note, "name": st["name"],
                "note": "clicked send but no 'Pending' state; check the profile"}
    finally:
        await post._close(page)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(run(a.url, a.note, a.check)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
