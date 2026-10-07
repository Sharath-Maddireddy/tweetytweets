#!/usr/bin/env python
"""Minimal Chrome DevTools Protocol driver for tweetytweets.

Talks straight to a Chrome/Chromium debugging port, against a dedicated profile
directory that only this tool uses. No browser-automation framework, no
Playwright, no Selenium - one dependency (`websockets`).

Why a *dedicated* profile: your everyday browser has several accounts signed in.
Driving that one risks posting as the wrong account. This launches its own
profile, so the session is isolated and unambiguous.

CLI:
  python browser.py info      # is Chrome up, which tabs are open
  python browser.py launch    # start the automation browser
  TWEETY_BROWSER=research python browser.py launch   # the research (dummy account) browser
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class CDPError(RuntimeError):
    pass


def _port() -> int:
    return int((settings.load().get("browser") or {}).get("port") or 9222)


def _base() -> str:
    return f"http://127.0.0.1:{_port()}"


def http_json(path: str, timeout: float = 5) -> Any:
    with urllib.request.urlopen(f"{_base()}{path}", timeout=timeout) as resp:
        return json.load(resp)


def browser_ws() -> str:
    return http_json("/json/version")["webSocketDebuggerUrl"]


def list_tabs() -> list[dict]:
    return [t for t in http_json("/json/list") if t.get("type") == "page"]


def alive() -> bool:
    try:
        http_json("/json/version", timeout=3)
        return True
    except Exception:
        return False


def ensure_chrome(wait: float = 25, url: str = "about:blank") -> bool:
    """Return True if the debug port answers, launching the browser if needed.

    Detached so that an agent process exiting does not kill the browser (and so
    a browser outliving the agent is fine).
    """
    if alive():
        return True
    cfg = settings.load()
    bcfg = cfg.get("browser") or {}
    chrome = settings.find_chrome(bcfg.get("chrome_path") or "")
    if not chrome:
        raise CDPError(
            "No Chrome/Chromium/Edge found. Install one, or set "
            "browser.chrome_path in config.json to the executable.")
    prof = settings.profile_dir(cfg)
    flags = [
        str(chrome),
        f"--remote-debugging-port={_port()}",
        f"--user-data-dir={prof}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        url,
    ]
    if bcfg.get("headless"):
        flags.append("--headless=new")
    DETACHED = 0x00000008 | 0x00000200           # Windows: DETACHED_PROCESS|NEW_GROUP
    kwargs: dict[str, Any] = {"stdout": subprocess.DEVNULL,
                              "stderr": subprocess.DEVNULL,
                              "stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = DETACHED
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(flags, **kwargs)
    deadline = time.time() + wait
    while time.time() < deadline:
        if alive():
            time.sleep(2.0)
            return True
        time.sleep(0.5)
    raise CDPError(f"browser did not open port {_port()} within {wait}s")


class Session:
    """One CDP websocket connection (page or browser scope)."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._id = 0

    async def __aenter__(self) -> "Session":
        import websockets
        self.ws = await websockets.connect(self.ws_url, max_size=256 * 1024 * 1024,
                                           ping_interval=None)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.ws.close()

    async def send(self, method: str, **params) -> dict:
        self._id += 1
        mid = self._id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=60)
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def eval(self, expression: str, await_promise: bool = False) -> Any:
        res = await self.send("Runtime.evaluate", expression=expression,
                              awaitPromise=await_promise, returnByValue=True)
        if "exceptionDetails" in res:
            raise CDPError(f"JS error: {res['exceptionDetails'].get('text')} "
                           f"{res['exceptionDetails'].get('exception', {}).get('description','')}")
        return res.get("result", {}).get("value")

    async def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.eval("document.readyState") == "complete":
                    return
            except (CDPError, OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.5)

    async def goto(self, url: str, settle: float = 4.0) -> None:
        await self.send("Page.navigate", url=url)
        await self.wait_ready()
        await asyncio.sleep(settle)


async def open_page(url: str = "about:blank") -> str:
    """Create a tab already pointed at `url`, return its page websocket url.

    Create the target WITH the final URL. Creating it on about:blank and then
    calling Page.navigate drops the websocket mid-flight ("no close frame
    received or sent") on heavy pages like Reddit and X.
    """
    async with Session(browser_ws()) as browser:
        target = await browser.send("Target.createTarget", url=url)
        tid = target["targetId"]
    for _ in range(40):
        for tab in list_tabs():
            if tab["id"] == tid:
                return tab["webSocketDebuggerUrl"]
        await asyncio.sleep(0.3)
    raise CDPError("new tab never appeared in /json/list")


async def settle_page(page: "Session", settle: float, timeout: float = 40) -> None:
    """Wait for load as best we can; never let navigation kill the run."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await page.eval("document.readyState") == "complete":
                break
        except (CDPError, OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.4)
    await asyncio.sleep(settle)


async def _eval_new_tab(url: str, expr: str, settle: float) -> Any:
    ws = await open_page(url)
    try:
        async with Session(ws) as page:
            await page.send("Page.enable")
            await page.send("Runtime.enable")
            await settle_page(page, settle)
            return await page.eval(expr)
    finally:
        close_tab(ws.rsplit("/", 1)[-1])      # only the tab we opened


def evaluate(url: str, expr: str, settle: float = 4.0) -> Any:
    """Sync: open `url` in a new tab and evaluate `expr` there."""
    ensure_chrome()
    return asyncio.run(_eval_new_tab(url, expr, settle))


async def _scrape_many(urls: list[str], expr: str, settle: float) -> list[Any]:
    """One tab, many URLs: open the first, then Page.navigate through the rest.

    Navigating an already-loaded tab is safe; the unsafe case was creating a
    target on about:blank. A failure on one URL yields None for that entry
    rather than aborting the whole batch.
    """
    ws = await open_page(urls[0])
    out: list[Any] = []
    try:
        return await _scrape_tab(ws, urls, expr, settle, out)
    finally:
        close_tab(ws.rsplit("/", 1)[-1])      # only the tab we opened


async def _scrape_tab(ws: str, urls: list[str], expr: str, settle: float,
                      out: list[Any]) -> list[Any]:
    async with Session(ws) as page:
        await page.send("Page.enable")
        await page.send("Runtime.enable")
        await settle_page(page, settle)
        for i, u in enumerate(urls):
            if i:
                try:
                    await page.send("Page.navigate", url=u)
                except (CDPError, OSError, asyncio.TimeoutError) as err:
                    out.append(None)
                    print(f"  ! navigate failed for {u[:60]}: {err}", file=sys.stderr)
                    continue
                await settle_page(page, settle)
            try:
                out.append(await page.eval(expr))
            except (CDPError, OSError, asyncio.TimeoutError) as err:
                out.append(None)
                print(f"  ! eval failed on {u[:60]}: {err}", file=sys.stderr)
    return out


def scrape_many(urls: list[str], expr: str, settle: float = 6.0) -> list[Any]:
    """Scrape a list of URLs reusing ONE tab, then close that tab (and only it:
    sweeping by URL also closed the user's own x.com tab, and closing a
    browser's last tab quits it)."""
    if not urls:
        return []
    ensure_chrome()
    return asyncio.run(_scrape_many(urls, expr, settle))


def close_tab(tab_id: str) -> bool:
    try:
        with urllib.request.urlopen(f"{_base()}/json/close/{tab_id}", timeout=5):
            return True
    except Exception:
        return False


def close_tabs_matching(substr: str) -> int:
    return sum(close_tab(t["id"]) for t in list_tabs() if substr in (t.get("url") or ""))


def screenshot(url: str, out: str, settle: float = 5.0,
               full_page: bool = True) -> dict:
    """Save a PNG. full_page=False captures the viewport only.

    Viewport-only is what you want for anything a vision model has to READ: a
    full-page grab of a long document gets downscaled into illegibility.
    """
    import base64

    async def _shot() -> dict:
        ws = await open_page(url)
        try:
            async with Session(ws) as page:
                await page.send("Page.enable")
                await page.send("Runtime.enable")
                await settle_page(page, settle)
                res = await page.send("Page.captureScreenshot", format="png",
                                      captureBeyondViewport=full_page)
                meta = await page.eval("JSON.stringify({url:location.href,title:document.title})")
        finally:
            close_tab(ws.rsplit("/", 1)[-1])
        return {"png": base64.b64decode(res["data"]), "meta": json.loads(meta or "{}")}

    ensure_chrome()
    got = asyncio.run(_shot())
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(got["png"])
    return {"screenshot": str(path.resolve()), **got["meta"]}


def plain(url: str, timeout: float = 25) -> str:
    """Plain HTTP GET with a browser UA (for sites that don't block us)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def og_image(url: str, timeout: float = 20) -> str | None:
    """Best-effort og:image / twitter:image for a URL (HTTP, browser fallback)."""
    import re

    def _scan(html: str) -> str | None:
        for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
            m = re.search(pat, html, re.I)
            if m:
                return m.group(1)
        return None

    try:
        found = _scan(plain(url, timeout))
        if found:
            return found
    except Exception:
        pass
    try:
        return _scan(evaluate(url, "document.head.innerHTML", settle=4) or "")
    except Exception:
        return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "info":
        print(json.dumps({
            "alive": alive(),
            "port": _port(),
            "profile_dir": str(settings.profile_dir()),
            "chrome": str(settings.find_chrome(
                (settings.load().get("browser") or {}).get("chrome_path") or "") or "NOT FOUND"),
            "tabs": [{"title": t.get("title"), "url": (t.get("url") or "")[:90]}
                     for t in (list_tabs() if alive() else [])],
        }, indent=2))
    elif cmd == "launch":
        print(json.dumps({"started": ensure_chrome(), "port": _port()}))
    else:
        raise SystemExit(__doc__)
