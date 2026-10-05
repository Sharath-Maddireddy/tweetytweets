#!/usr/bin/env python
"""Post to X through a logged-in browser session - no X API, no developer account.

Why the browser: X's API free tier needs a developer account and phone
verification, and its read quota is useless for research. Driving the real
composer costs nothing and works with the account you already have.

Every post is verified by READING THE PROFILE BACK. A click on the Post button
proves nothing.

CLI:
  python post.py check                  # session + handle + next Day number
  python post.py measure                # empirically find this account's char limit
  python post.py day                    # next "Daily AI updates | Day N"
  python post.py compose --text "..." --dry-run
  python post.py post --text-file draft.txt [--image pic.png] [--kind ai_update]
  python post.py verify                 # read the newest posts back
  python post.py shot --what profile    # screenshot for a visual check
  python post.py status
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import browser
import settings
from browser import CDPError, Session

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state.json"
SHOTS = ROOT / "shots"

EDITOR = '[data-testid="tweetTextarea_0"]'
POST_BTN = '[data-testid="tweetButton"]'
FILE_INPUT = 'input[type="file"][data-testid="fileInput"]'


# ------------------------------------------------------------------ ledger

def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"handle": settings.load().get("handle", ""), "ai_update_day": 0, "posts": []}


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def next_day(st: dict | None = None) -> int:
    """Next AI-updates day number.

    Increments ONLY when an ai_update post actually lands, so a missed day burns
    no number and the series never shows a phantom day.
    """
    st = st or load_state()
    return int(st.get("ai_update_day", 0)) + 1


def cfg() -> dict:
    return settings.load()


def handle() -> str:
    return (cfg().get("handle") or "").strip().lstrip("@")


def max_chars() -> int:
    """The ceiling for THIS account, from settings.limit() (never hardcoded)."""
    return settings.limit(cfg())["chars"]


async def measure_limit(ladder: tuple[int, ...] = (280, 1000, 5000, 25000, 30000),
                        max_refine: int = 6) -> dict:
    """Empirically find this account's character ceiling.

    Never assume a number: free accounts cap at 280 while Premium ones do not,
    and the ceiling decides what content is even possible. This types n
    characters into the composer and watches whether the Post button enables.
    **It never clicks Post, so it cannot publish anything.**

    Probes a ladder first (cheap, and spans free tier through Premium), then
    refines by binary search between the largest passing and smallest failing
    rung.
    """
    async def fits(n: int) -> bool:
        page = await _open("https://x.com/compose/post")
        try:
            await browser.settle_page(page, 5)
            await wait_login(page)
            await _insert_text(page, "y" * (n - 1) + ".")
            await asyncio.sleep(2.0)
            st = json.loads(await page.eval(COMPOSE_STATE_JS))
            return not st.get("btn_disabled")
        finally:
            await _close(page)

    lo, hi = 0, None
    probes: list[dict] = []
    for n in ladder:
        ok = await fits(n)
        probes.append({"chars": n, "accepted": ok})
        if ok:
            lo = n
        else:
            hi = n
            break

    if hi is None:                       # every rung passed
        return {"limit": f">={lo}", "limit_value": lo, "probes": probes,
                "premium": lo > 280,
                "note": "the account accepts at least the largest probe; the true ceiling "
                        "is higher. Raise the ladder to find it."}

    for _ in range(max_refine):          # narrow the gap
        if lo + 1 >= hi:
            break
        mid = (lo + hi) // 2
        if await fits(mid):
            lo = mid
        else:
            hi = mid

    return {"limit": lo, "limit_value": lo, "refuses": hi, "probes": probes,
            "premium": lo > 280,
            "note": f"the Post button accepts {lo} chars and refuses {hi}"}


# ------------------------------------------------------------------ session

LOGIN_JS = r"""
(() => {
  const url = location.href;
  const btn = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
  const raw = btn ? (btn.innerText || '') : '';
  const m = raw.match(/@([A-Za-z0-9_]{1,20})/);
  const prof = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
  const ph = prof ? (prof.getAttribute('href') || '').replace(/^\//, '') : null;
  const h = (m ? m[1] : ph) || null;
  const login_form = !!document.querySelector('input[name="text"], input[autocomplete="username"]');
  return JSON.stringify({url, handle: h, logged_in: !!h, login_form,
                         raw: raw.replace(/\n/g, ' ').slice(0, 120)});
})()
"""


async def wait_login(page: Session, timeout: float = 25.0) -> dict:
    """Poll until the session proves itself by exposing an account handle.

    Do NOT trust the URL: x.com/home renders the login page inline for a beat
    before redirecting, so a URL-only test reports logged_in=True while the
    account switcher is still absent.
    """
    deadline = time.time() + timeout
    info: dict = {}
    while time.time() < deadline:
        try:
            info = json.loads(await page.eval(LOGIN_JS) or "{}")
        except (CDPError, OSError, asyncio.TimeoutError):
            info = {}
        if info.get("handle") or info.get("login_form"):
            return info
        await asyncio.sleep(1.0)
    return info


async def _open(url: str) -> Session:
    ws = await browser.open_page(url)
    sess = Session(ws)
    page = await sess.__aenter__()
    await page.send("Page.enable")
    await page.send("Runtime.enable")
    return page


async def _close(page: Session) -> None:
    """Disconnect AND close the tab; disconnecting alone leaks one tab per call."""
    await page.__aexit__(None, None, None)
    browser.close_tab(page.ws_url.rsplit("/", 1)[-1])


async def session_info() -> dict:
    browser.ensure_chrome()
    page = await _open("https://x.com/home")
    try:
        await browser.settle_page(page, 4)
        return await wait_login(page)
    finally:
        await _close(page)


def require_session(expect: str | None = None) -> dict:
    """Refuse to continue unless the signed-in account is the configured one."""
    info = asyncio.run(session_info())
    expect = (expect if expect is not None else handle()).lower()
    if not info.get("logged_in") or not info.get("handle"):
        raise SystemExit(json.dumps({
            "error": "not logged in",
            "fix": "sign in to x.com in the automation browser window, then re-run",
            **info}, indent=2))
    if expect and info["handle"].lower() != expect:
        raise SystemExit(json.dumps({
            "error": "WRONG ACCOUNT",
            "expected": expect, "found": info["handle"],
            "why": "refusing to post; the automation profile is signed into a different account"},
            indent=2))
    return info


# ------------------------------------------------------------------ composer

FOCUS_JS = r"""
(() => {
  const el = document.querySelector('[data-testid="tweetTextarea_0"]');
  if (!el) return 'no-editor';
  el.focus();
  try {
    const r = document.createRange();
    r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r);
  } catch (e) {}
  return 'focused:' + (document.activeElement === el);
})()
"""

COMPOSE_STATE_JS = r"""
(() => {
  const btn = document.querySelector('[data-testid="tweetButton"]');
  const ed  = document.querySelector('[data-testid="tweetTextarea_0"]');
  const toast = document.querySelector('[data-testid="toast"]');
  const txt = document.body ? document.body.innerText : '';
  return JSON.stringify({
    has_editor: !!ed,
    editor_text: ed ? (ed.innerText || '') : '',
    btn_found: !!btn,
    btn_disabled: btn ? (btn.getAttribute('aria-disabled') === 'true' || !!btn.disabled) : null,
    toast: toast ? (toast.innerText || '').replace(/\n/g,' ').slice(0,160) : null,
    modal_open: !!document.querySelector('[aria-labelledby="modal-header"]'),
    sent: /Your post was sent/i.test(txt),
    error: ((txt.match(/(Something went wrong|Whoops|Try again|exceeded|too long)[^\n]{0,90}/i)||[''])[0]||'').trim(),
    url: location.href,
  });
})()
"""


async def _insert_text(page: Session, text: str) -> str:
    """Insert into the contenteditable composer so React registers it.

    Setting innerText directly does NOT update X's React state and leaves the
    Post button disabled forever - so focus the editor, then use
    Input.insertText, which fires real input events.
    """
    state = await page.eval(FOCUS_JS)
    if state != "focused:true":
        await asyncio.sleep(0.4)
        await page.eval("document.querySelector('%s') && document.querySelector('%s').focus()"
                        % (EDITOR, EDITOR))
    await page.send("Input.insertText", text=text)
    await asyncio.sleep(0.8)
    return await page.eval("(document.querySelector('%s')||{}).innerText || ''" % EDITOR)


async def _attach_image(page: Session, path: Path) -> str:
    doc = await page.send("DOM.getDocument", depth=-1)
    node = await page.send("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector=FILE_INPUT)
    if not node.get("nodeId"):
        return "no-file-input"
    await page.send("DOM.setFileInputFiles", files=[str(path)], nodeId=node["nodeId"])
    for _ in range(30):
        await asyncio.sleep(1.0)
        st = json.loads(await page.eval(COMPOSE_STATE_JS))
        if st.get("btn_found") and not st.get("btn_disabled"):
            return "attached"
    return "attached-but-button-still-disabled"


async def _click_post(page: Session) -> str:
    for _ in range(45):
        await asyncio.sleep(1.0)
        st = json.loads(await page.eval(COMPOSE_STATE_JS))
        if st.get("btn_disabled") is False:
            await page.eval("document.querySelector('%s').click()" % POST_BTN)
            return "clicked"
    return "button never enabled"


# ------------------------------------------------------------------ read-back

PROFILE_JS = r"""
(() => {
  const out = [...document.querySelectorAll('article[data-testid="tweet"]')]
    .slice(0, %d).map(a => {
      const t = a.querySelector('[data-testid="tweetText"]');
      const link = a.querySelector('a[href*="/status/"]');
      const time = a.querySelector('time');
      return {text: t ? t.innerText : '',
              url: link ? 'https://x.com' + link.getAttribute('href') : '',
              at: time ? time.getAttribute('datetime') : ''};
    });
  return JSON.stringify(out);
})()
"""


async def _read_profile(h: str | None = None, limit: int = 4,
                        wait: float = 30.0) -> list[dict]:
    """Read the newest posts back off the profile - the only honest proof.

    Polls until the timeline renders: right after a send, X serves a stale
    partial render, and an immediate read reports a months-old post as "newest".
    """
    h = h or handle()
    page = await _open(f"https://x.com/{h}")
    try:
        await browser.settle_page(page, 6)
        deadline = time.time() + wait
        while time.time() < deadline:
            rows = json.loads(await page.eval(PROFILE_JS % limit) or "[]")
            if rows:
                return rows
            await asyncio.sleep(2)
        return []
    finally:
        await _close(page)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


# ------------------------------------------------------------------ capture

SCROLL_TO_ARTICLE_JS = r"""
(() => {
  const el = document.querySelector('article[data-testid="tweet"]');
  if (!el) return 'no-article';
  el.scrollIntoView({block: 'start'});
  // scrollIntoView aligns the post with the viewport top, which hides its FIRST
  // LINE - the hook, the line most worth verifying - under the sticky header.
  window.scrollBy(0, -110);
  return 'scrolled:' + (el.querySelector('[data-testid="tweetText"]') ? 'with-text' : 'no-text');
})()
"""


async def capture(what: str = "profile", h: str | None = None) -> dict:
    """Save a VIEWPORT screenshot at a stable path for a vision check.

    Viewport, never captureBeyondViewport: a tall image is downscaled into
    illegibility and a vision model then misreads it (it has been observed
    claiming a composer was empty - quoting its placeholder - while
    simultaneously reporting the Post button as enabled).
    """
    h = h or handle()
    url = f"https://x.com/{h}" if what == "profile" else "https://x.com/compose/post"
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{what}.png"
    page = await _open(url)
    try:
        await browser.settle_page(page, 9 if what == "profile" else 5)
        await page.send("Emulation.setDeviceMetricsOverride", width=1280, height=1000,
                        deviceScaleFactor=1, mobile=False)
        note = None
        if what == "profile":
            note = await page.eval(SCROLL_TO_ARTICLE_JS)
            await asyncio.sleep(1.5)
        res = await page.send("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(res["data"]))
        title = await page.eval("document.title")
    finally:
        await _close(page)
    return {"screenshot": str(path.resolve()), "what": what, "url": url,
            "title": title, "framing": note}


# ------------------------------------------------------------------ posting

async def do_post(text: str, image: Path | None = None, kind: str = "value",
                  dry_run: bool = False, settle: float = 6.0) -> dict:
    c = cfg()
    st = load_state()
    day = next_day(st) if kind == "ai_update" else None
    header = None
    if kind == "ai_update" and c.get("ai_update_enabled", True):
        header = (c.get("ai_update_header") or "Daily AI updates | Day {day}").replace("{day}", str(day))
    body = f"{header}\n\n{text}" if header else text

    limit = max_chars()
    if len(body) > limit:
        raise SystemExit(json.dumps({
            "error": "text too long", "chars": len(body), "limit": limit,
            "hint": "trim the draft; for an ai_update slot the header already eats "
                    f"{len(header) + 2 if header else 0} chars"}, indent=2))

    page = await _open("https://x.com/compose/post")
    try:
        await browser.settle_page(page, settle)
        info = await wait_login(page)
        if not info.get("logged_in"):
            raise SystemExit(json.dumps({"error": "session expired mid-flow", **info}, indent=2))
        if handle() and info.get("handle", "").lower() != handle().lower():
            raise SystemExit(json.dumps({"error": "WRONG ACCOUNT", "found": info.get("handle")},
                                        indent=2))

        got = await _insert_text(page, body)
        if header and header[:20] not in got:
            raise SystemExit(json.dumps({"error": "text did not land in the composer",
                                         "editor": got[:200]}, indent=2))

        img_status = None
        if image:
            img_status = await _attach_image(page, image)

        SHOTS.mkdir(parents=True, exist_ok=True)
        shot = SHOTS / "compose.png"
        shotdata = await page.send("Page.captureScreenshot", format="png")
        shot.write_bytes(base64.b64decode(shotdata["data"]))

        if dry_run:
            return {"dry_run": True, "chars": len(body), "editor_text": got[:200],
                    "screenshot": str(shot), "image": img_status, "next_day": day,
                    "note": "nothing was published"}

        click = await _click_post(page)
        sent = False
        for _ in range(30):
            await asyncio.sleep(1.0)
            cs = json.loads(await page.eval(COMPOSE_STATE_JS))
            if cs.get("sent") or (not cs.get("modal_open") and not cs.get("has_editor")):
                sent = True
                break
            if cs.get("error"):
                return {"error": cs["error"], "click": click, "screenshot": str(shot)}
    finally:
        await _close(page)

    # Honest verification: read the profile back, polling (a single read can be stale).
    head = _norm(body)[:60]
    match = None
    feed: list[dict] = []
    deadline = time.time() + 90
    while time.time() < deadline:
        await asyncio.sleep(5)
        try:
            feed = await _read_profile()
        except (CDPError, OSError, asyncio.TimeoutError) as err:
            print(f"  ! read-back failed: {err}", file=sys.stderr)
            continue
        if not feed:
            continue
        match = next((p for p in feed if head and head[:40] in _norm(p.get("text"))), None)
        if match:
            break

    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": kind, "day": day,
        "slot": datetime.now().strftime("%H:%M"),
        "text": body, "image": img_status,
        "tweet_url": (match or {}).get("url"),
        "verified": bool(match),
        "toast_sent": sent,
        "screenshot": str(shot),
    }
    st["posts"].append(entry)
    if kind == "ai_update":
        if match:
            st["ai_update_day"] = day
            st["last_ai_update"] = entry["at"]
        else:
            entry["note"] = "NOT counted - verification failed, so the Day number was not consumed"
    save_state(st)

    shot_profile = None
    try:
        shot_profile = (await capture("profile"))["screenshot"]
    except (CDPError, OSError, asyncio.TimeoutError) as err:
        print(f"  ! profile screenshot failed: {err}", file=sys.stderr)

    return {"posted": bool(match), "verified": bool(match), "day": day,
            "tweet_url": entry["tweet_url"], "toast_sent": sent,
            "screenshot": str(shot), "profile_screenshot": shot_profile,
            "profile_head": feed[:1]}


# ------------------------------------------------------------------ cli

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["check", "day", "post", "compose", "verify", "status",
                             "shot", "measure", "limits"])
    ap.add_argument("--save", action="store_true",
                    help="measure: write the measured ceiling into config.json")
    ap.add_argument("--text", default=None)
    ap.add_argument("--text-file", default=None)
    ap.add_argument("--image", default=None)
    ap.add_argument("--kind", default="value", choices=["value", "ai_update"])
    ap.add_argument("--what", default="profile", choices=["profile", "compose"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.cmd == "status":
        st = load_state()
        print(json.dumps({"handle": handle() or "(not set - run onboarding)",
                          "max_chars": max_chars(),
                          "ai_update_day": st.get("ai_update_day", 0),
                          "next_day": next_day(st),
                          "posts": len(st.get("posts", [])),
                          "last": st.get("posts", [])[-1:]}, indent=2)[:2500])
        return
    if a.cmd == "day":
        st = load_state()
        print(json.dumps({"last_day": st.get("ai_update_day", 0),
                          "next_day": next_day(st)}, indent=2))
        return
    if a.cmd == "check":
        info = require_session()
        print(json.dumps({**info, "max_chars": max_chars(), "ok": True}, indent=2))
        return
    if a.cmd == "verify":
        print(json.dumps(asyncio.run(_read_profile()), indent=2, ensure_ascii=False)[:2000])
        return
    if a.cmd == "shot":
        print(json.dumps(asyncio.run(capture(a.what)), indent=2))
        return
    if a.cmd == "limits":
        print(json.dumps(settings.limit(cfg()), indent=2))
        return
    if a.cmd == "measure":
        out = asyncio.run(measure_limit())
        if a.save:
            out["saved"] = settings.save_limit(out.get("limit_value") or 280,
                                               out.get("premium"))
        else:
            out["hint"] = ("nothing was saved - re-run with --save to write this ceiling "
                           "into config.json so due.py enforces the right number")
        print(json.dumps(out, indent=2))
        return

    text = a.text
    if a.text_file:
        text = Path(a.text_file).read_text(encoding="utf-8")
    if not text:
        raise SystemExit("need --text or --text-file")
    require_session()
    out = asyncio.run(do_post(text, image=Path(a.image) if a.image else None,
                              kind=a.kind, dry_run=a.dry_run))
    print(json.dumps(out, indent=2, ensure_ascii=False)[:2500])


if __name__ == "__main__":
    main()
