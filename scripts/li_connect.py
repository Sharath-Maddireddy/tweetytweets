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
import re
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
// the profile's own action buttons: highest visible match in the main column
// (sidebar suggestions are on the right; posts and footers are lower down)
window.__top = (re) => { window.scrollTo(0, 0); return __all('button, a, div[role="button"], span[role="button"]')
  .filter(b => { const r = b.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.left < 860 && r.top > 60 && r.top < 900 &&
      (re.test((b.innerText || '').trim()) || re.test((b.getAttribute('aria-label') || '').trim())); })
  .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0]; };
// the profile's own "More": next to its "Follow <name>" / "Message <name>" button
window.__profileMore = (name) => { const anchor = __all('button, a').find(b =>
    ['Follow ' + name, 'Message ' + name, 'Invite ' + name + ' to connect'].includes((b.getAttribute('aria-label') || '').trim()) && b.getClientRects().length);
  let el = anchor; for (let i = 0; el && i < 6; i++) { el = el.parentElement;
    const m = el && [...el.querySelectorAll('button')].find(b => /^More$/.test((b.innerText || '').trim()) || /More actions/i.test(b.getAttribute('aria-label') || ''));
    if (m) return m; } return null; };
// "Connect" inside the menu that also offers "Report <first>" (only this profile's menu does)
window.__menuConnect = (first) => { const menus = __all('div, ul').filter(e => e.getClientRects().length &&
    (e.innerText || '').includes('Report ' + first) && /(^|\n)Connect(\n|$)/.test(e.innerText || ''));
  menus.sort((a, b) => a.innerText.length - b.innerText.length); const m = menus[0]; if (!m) return null;
  const items = [...m.querySelectorAll('*')].filter(e => (e.innerText || '').trim() === 'Connect' && e.getClientRects().length);
  return items.find(e => e.matches('button, a, [role="button"], [role="menuitem"], li')) || items[0] || null; };
window.__box = () => __all('textarea[name="message"], #custom-message, textarea')[0];
true"""

STATE = r"""JSON.stringify({
  pending: !!__btn(/^Pending|Withdraw invitation/i),
  connected: !!__btn(/^Message\b/) && !__btn(/Invite .* to connect|^Connect$/i) && /1st/.test(document.body.innerText.slice(0, 4000)),
  name: document.title.split(' | ')[0]})"""


async def click(page, pattern: str) -> bool:
    return await page.eval(f"(() => {{ const b = __btn({pattern}); if (!b) return false; b.click(); return true; }})()")


NOTE_MAX = 200          # free plan: 200 chars, and only a few noted invites per month


async def run(url: str, note: str, check: bool, name: str = "") -> dict:
    """Open LinkedIn's direct invite dialog for exactly this profile, verify it names
    `name`, add the note, send, and confirm on the Sent invitations page."""
    if len(note) > NOTE_MAX:
        return {"sent": False, "error": f"note is {len(note)} chars; LinkedIn free plan allows {NOTE_MAX}"}
    if not name:
        return {"sent": False, "error": "expected recipient name is required"}
    first = name.split()[0]
    if note and not note.startswith(f"Hi {first},"):
        return {"sent": False, "error": f"note greets someone other than {first}; refusing"}
    vanity = url.rstrip("/").split("/in/")[1].split("/")[0]
    page = await post._open(f"https://www.linkedin.com/preload/custom-invite/?vanityName={vanity}")
    try:
        await browser.settle_page(page, 8)
        await page.eval(FIND)
        dlg = await page.eval(r"""__all('[role="dialog"]').map(d => d.innerText).join(' ')""")
        if name.lower() not in dlg.lower():
            return {"sent": False, "error": f"invite dialog isn't for {name} (pending, connected, or invites off)",
                    "dialog": dlg[:160]}
        used_note = False
        if note and await trusted_click(page, r"__dlgBtn(/^Add a note$/i)"):
            await asyncio.sleep(1.2)
            await page.eval(FIND)
            box = await page.eval("(() => { const t = __box(); if (!t) return 'none'; if (t.value.trim()) return 'dirty'; t.focus(); return 'ok'; })()")
            if box != "ok":
                return {"sent": False, "error": f"note box {box}"}
            await page.send("Input.insertText", text=note)
            await asyncio.sleep(0.8)
            if (await page.eval("(__box() || {}).value || ''")).strip() != note.strip():
                return {"sent": False, "error": "note box text doesn't match the approved note"}
            used_note = True
        if check:
            return {"sent": False, "check": "dialog ready, nothing sent", "with_note": used_note, "name": name}
        await page.eval(FIND)
        btn = r"/^Send( invitation| now)?$|^Send without a note$/i"
        state = await page.eval(f"(() => {{ const b = __dlgBtn({btn}); return !b ? 'missing' : (b.disabled || b.getAttribute('aria-disabled') === 'true') ? 'disabled' : 'ok'; }})()")
        if state != "ok":
            limit = await page.eval(r"""(__all('[role="dialog"]').map(d => d.innerText).join(' ').match(/[^\n]*(remaining|limit)[^\n]*/i) || [''])[0]""")
            return {"sent": False, "error": f"send button {state}", "dialog": limit}
        await trusted_click(page, f"__dlgBtn({btn})")
        await asyncio.sleep(3)
    finally:
        await post._close(page)
    sent = await browser._eval_new_tab("https://www.linkedin.com/mynetwork/invitation-manager/sent/", "document.body.innerText", 8) or ""
    return {"sent": name.lower() in sent.lower() or "unverified", "with_note": used_note, "name": name}


async def trusted_click(page, el_js: str) -> bool:
    pos = await page.eval(f"""(() => {{ const b = {el_js}; if (!b) return '';
        const r0 = b.getBoundingClientRect(); if (r0.top < 0 || r0.bottom > innerHeight) b.scrollIntoView({{block: 'center'}});
        const r = b.getBoundingClientRect(); return JSON.stringify({{x: r.x + r.width / 2, y: r.y + r.height / 2}}); }})()""")
    if not pos:
        return False
    pos = json.loads(pos)
    for ev in ("mousePressed", "mouseReleased"):
        await page.send("Input.dispatchMouseEvent", type=ev, x=pos["x"], y=pos["y"], button="left", clickCount=1)
    return True


async def follow(url: str) -> dict:
    """Follow ONLY the person whose profile is open."""
    page = await post._open(url)
    try:
        await browser.settle_page(page, 7)
        await page.eval(FIND)
        name = json.loads(await page.eval(STATE))["name"].strip()
        if not name:
            return {"followed": False, "error": "couldn't read profile name"}
        esc = re.escape(name).replace("/", "\\/")
        if await page.eval(f"!!__btn(/^(Unfollow|Following) {esc}/i)"):
            return {"followed": "already", "name": name}
        ok = await trusted_click(page, f"__all('button').find(b => (b.getAttribute('aria-label') || '').trim() === {json.dumps('Follow ' + name)} && b.getClientRects().length)")
        await asyncio.sleep(2)
        await page.eval(FIND)
        done = await page.eval(f"!!__btn(/^(Unfollow|Following) {esc}/i)")
        return {"followed": bool(ok and done) or ("unverified" if ok else False), "name": name}
    finally:
        await post._close(page)


async def message(url: str, text: str, check: bool, expect: str = "", allow_thread: bool = False) -> dict:
    """Message the person whose profile is open (works without connecting when their
    profile offers Message, e.g. Services pages). Refuses if the greeting's name differs."""
    page = await post._open(url)
    try:
        await browser.settle_page(page, 7)
        await page.eval(FIND)
        name = json.loads(await page.eval(STATE))["name"].strip()
        if expect and name.lower() != expect.lower():
            return {"sent": False, "error": f"profile is {name!r}, expected {expect!r}; refusing"}
        first = name.split()[0] if name else ""
        if not first or not text.startswith(f"Hi {first},"):
            return {"sent": False, "error": f"message doesn't greet {first or '?'}; refusing"}
        # the top card's Message link (no aria-label; sidebar ones carry other people's names).
        # Open its compose URL as a full page: the overlay chat doesn't render reliably.
        href = await page.eval("""(__all('a').find(x => (x.innerText || '').trim() === 'Message'
            && !(x.getAttribute('aria-label') || '').trim()
            && /messaging\\/compose\\/\\?profileUrn=/.test(x.getAttribute('href') || '') && x.getClientRects().length) || {}).href || ''""")
        if not href:
            return {"sent": False, "error": "no Message link (not connected yet?)"}
        await page.send("Page.navigate", url=href.replace("&interop=msgOverlay", ""))
        await browser.settle_page(page, 8)
        box_js = "__all('div.msg-form__contenteditable[contenteditable=\"true\"]').filter(e => e.getClientRects().length).pop()"
        for _ in range(15):
            await page.eval(FIND)
            if await page.eval(f"!!({box_js})"):
                break
            await asyncio.sleep(0.8)
        else:
            return {"sent": False, "error": "message box never appeared (may need Premium/InMail)"}
        # the compose pane must be addressed to this one person, a 1st-degree connection
        pane = await page.eval(f"""(() => {{ let p = {box_js}; for (let i = 0; i < 12 && p.parentElement; i++) {{
            p = p.parentElement; if (p.innerText.length > 300) break; }} return p.innerText; }})()""")
        to = pane.split("Show suggested recipients")[0]
        if name.lower() not in to.lower() or (not allow_thread and "1st degree connection" not in pane):
            return {"sent": False, "error": f"compose pane isn't addressed to {name}; refusing", "pane": pane[:150]}
        n_msgs = await page.eval("__all('.msg-s-event-listitem, .msg-s-message-list__event').filter(e => e.getClientRects().length).length")
        listed = await page.eval(f"""__all('.msg-conversation-listitem, .msg-conversation-card, li.msg-conversations-container__convo-item')
            .some(e => e.innerText.toLowerCase().includes({json.dumps(name.lower())}) && !/draft/i.test(e.innerText))""")
        if (n_msgs or listed) and not allow_thread:         # never double-message a follow-up
            return {"sent": False, "error": f"conversation with {name} already exists; refusing"}
        if allow_thread:                                    # a nudge: only while the chat ends with our message
            import outbox
            me = (outbox.setting("linkedin_name") or "").lower()
            last = await page.eval(r"""(() => { const t = __all('.msg-s-message-list__event, .msg-s-event-listitem')
                .filter(e => e.getClientRects().length).map(e => e.innerText).join('\n');
                const m = [...t.matchAll(/([^\n|]+?) sent the following messages? at/g)]; return m.length ? m[m.length - 1][1].trim() : ''; })()""")
            if not me or last.lower() != me:
                return {"sent": False, "error": f"last message in the chat is from {last or 'unknown'}, not us; refusing"}
        draft = (await page.eval(f"(({box_js}) || {{}}).innerText || ''")).strip()
        if draft and " ".join(draft.split()) != " ".join(text.split()):
            return {"sent": False, "error": "message box already has other text"}
        if not draft:                                       # a --check run leaves the approved text as a draft
            await page.eval(f"({box_js}).focus()")
            await page.send("Input.insertText", text=text)
            for t in ("keyDown", "keyUp"):                  # a real keystroke so LinkedIn's editor registers the text
                await page.send("Input.dispatchKeyEvent", type=t, key="End", code="End", windowsVirtualKeyCode=35)
            await asyncio.sleep(1)
        typed = await page.eval(f"(({box_js}) || {{}}).innerText || ''")
        if " ".join(typed.split()) != " ".join(text.split()):
            return {"sent": False, "error": "typed text doesn't match approved text", "typed": typed[:120]}
        send_js = "__all('button.msg-form__send-button').filter(b => b.getClientRects().length && !b.disabled).pop()"
        if not await page.eval(f"!!({send_js})"):
            return {"sent": False, "error": "Send button still disabled after typing"}
        if check:
            return {"sent": False, "check": "message typed, Send enabled, nothing sent", "to": name}
        if not await trusted_click(page, send_js):
            return {"sent": False, "error": "send button missing or disabled"}
        probe = json.dumps(text[:40])
        for _ in range(10):
            await asyncio.sleep(1)
            await page.eval(FIND)
            if await page.eval(f"__all('.msg-s-event-listitem__body, .msg-s-message-list-content').some(e => e.innerText.includes({probe}))"):
                return {"sent": True, "to": name}
        return {"sent": "unverified", "to": name}
    finally:
        await post._close(page)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--name", required=True, help="expected recipient name, as on their profile")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(run(a.url, a.note, a.check, a.name)), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
