#!/usr/bin/env python
"""Telegram bot: turns your ✅ taps into sends. Run on ONE machine only (the desktop);
two processes polling the same bot steal each other's updates.

Buttons it handles:
  send:<id> / skip:<id>   outbox actions (x_dm, li_connect), text frozen at queue time
  fu:<rowid>              "I sent the LinkedIn follow-up" (stops it being re-drafted)
  rep:<rowid>             "they replied" (stops nudges)

Safety: only taps from the owner's Telegram id count; a row is claimed atomically so a
double tap or a restart can't send twice; sends run one at a time, 2-4 min apart,
capped per day per channel; the X account and the LinkedIn identity are checked
before every send.

  python scripts/bot.py
"""
from __future__ import annotations

import asyncio
import json
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from html import escape

import outbox
from outbox import db, now, tg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DAILY_CAP = {"x_dm": 5, "li_connect": 8}
PAUSE = outbox.ROOT / "drafts" / "PAUSED"            # /pause creates it, /resume removes it
CHECK = "--check" in sys.argv          # test mode: go all the way to the filled dialog, send nothing
GAP = (120, 240)                      # seconds between sends of the same kind


def owner_id() -> int:
    return int(outbox.tg_cfg()["chat_id"])


def li_identity_ok() -> bool:
    import browser
    want = (outbox.setting("linkedin_name") or "").lower()
    if not want:
        return False
    t = browser.evaluate("https://www.linkedin.com/feed/", "document.body.innerText.slice(0, 1500)", settle=6) or ""
    return want in t.lower()


def execute(row) -> dict:
    """Do one approved action. Returns a result dict; never raises."""
    try:
        if row["kind"] == "x_dm":
            import dm
            import post
            try:
                post.require_session()                       # signed in as the configured handle?
            except SystemExit as err:
                return {"sent": False, "error": f"X account check failed: {err}"}
            r = asyncio.run(dm.run(row["target"], row["text"], CHECK))
            return {**r, "sent": True, "check_only": True} if CHECK and r.get("check") else r
        if row["kind"] == "li_connect":
            import li_connect
            if not li_identity_ok():
                return {"sent": False, "error": "LinkedIn isn't signed in as the configured linkedin_name"}
            r = asyncio.run(li_connect.run(row["target"], row["text"], CHECK, row["expected_name"]))
            return {**r, "sent": True, "check_only": True} if CHECK and r.get("check") else r
        return {"sent": False, "error": f"unknown kind {row['kind']}"}
    except Exception as err:                                  # report, don't crash the bot
        return {"sent": False, "error": f"{type(err).__name__}: {err}",
                "trace": traceback.format_exc()[-400:]}


def due(con, kind: str) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    n = con.execute("select count(*) from outbox where kind=? and status='sent' and sent_at>=?",
                    (kind, today)).fetchone()[0]
    if n >= DAILY_CAP[kind]:
        return False
    last = con.execute("select max(sent_at) from outbox where kind=? and status in ('sent','failed')",
                       (kind,)).fetchone()[0]
    if not last:
        return True
    return datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(seconds=random.randint(*GAP))


def auto_approve(con) -> None:
    """Auto mode: pending actions approve themselves after the veto window."""
    mins = outbox.setting("auto_send_after_min")
    if mins is None:
        return
    cut = (datetime.now(timezone.utc) - timedelta(minutes=int(mins))).isoformat(timespec="seconds")
    for row in con.execute("select id from outbox where status='pending' and created<=?", (cut,)).fetchall():
        con.execute("update outbox set status='queued', decided=? where id=? and status='pending'", (now(), row["id"]))


def work(con) -> None:
    """Send at most one queued action, if its kind is due."""
    if PAUSE.exists():
        return
    auto_approve(con)
    for row in con.execute("select * from outbox where status='queued' order by decided").fetchall():
        if not due(con, row["kind"]):
            continue
        if con.execute("update outbox set status='sending' where id=? and status='queued'",
                       (row["id"],)).rowcount != 1:
            continue
        r = execute(row)
        ok = r.get("sent") is True
        con.execute("update outbox set status=?, sent_at=?, result=? where id=?",
                    ("sent" if ok else "failed", now(), json.dumps(r, ensure_ascii=False)[:2000], row["id"]))
        if ok and row["contact_key"] and not CHECK:
            field = "nudged_at" if row["kind"] == "x_dm" and con.execute(
                "select sent_at from contacts where key=?", (row["contact_key"],)).fetchone() else "sent_at"
            outbox.upsert_contact(row["contact_key"], **{field: now()})
            if row["kind"] == "li_connect":
                outbox.upsert_contact(row["contact_key"], status="requested")
            elif field == "sent_at":
                outbox.upsert_contact(row["contact_key"], status="dm_sent")
        who = row["expected_name"] or row["target"]
        if CHECK:
            con.execute("update outbox set status='checked' where id=?", (row["id"],))
        msg = f"🧪 check OK for {escape(who)} (nothing sent)" if ok and CHECK else f"✅ sent to {escape(who)}" if ok else f"⚠️ not sent to {escape(who)}: {escape(str(r.get('error') or r))[:300]}"
        outbox.say(msg)
        return                                               # one send per loop; spacing does the rest


def on_callback(con, cq: dict) -> None:
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if int(cq["from"]["id"]) != owner_id():
        return                                               # someone else tapped; ignore
    data = cq.get("data", "")
    action, _, ref = data.partition(":")
    msg = cq.get("message") or {}
    if msg:
        try:
            tg("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
               reply_markup={"inline_keyboard": []})
        except Exception:
            pass
    if action in ("send", "skip"):
        new = "queued" if action == "send" else "skipped"
        changed = con.execute("update outbox set status=?, decided=? where id=? and status='pending'",
                              (new, now(), ref)).rowcount
        if changed and action == "send":
            outbox.say("⏳ queued, sends within a few minutes (2-4 min apart)")
        elif not changed:
            outbox.say("already handled")
    elif action == "fu":
        con.execute("update contacts set followup_sent_at=?, status='followup_sent' where rowid=?", (now(), ref))
        outbox.say("noted: follow-up sent")
    elif action == "ndg":
        con.execute("update contacts set nudged_at=? where rowid=?", (now(), ref))
        outbox.say("noted: nudge sent")
    elif action == "rep":
        con.execute("update contacts set replied=1, status='replied' where rowid=?", (ref,))
        outbox.say("noted: they replied 🎉 no more nudges")


def on_message(con, m: dict) -> None:
    if int((m.get("from") or {}).get("id", 0)) != owner_id():
        return
    t = (m.get("text") or "").strip().lower()
    if t == "/pause":
        PAUSE.write_text(now())
        outbox.say("⏸ paused: nothing will be sent until /resume")
    elif t == "/resume":
        PAUSE.unlink(missing_ok=True)
        outbox.say("▶️ resumed")
    elif t == "/status":
        n = con.execute("select status, count(*) c from outbox group by 1").fetchall()
        outbox.say(("⏸ PAUSED\n" if PAUSE.exists() else "") + "outbox: " + ", ".join(f"{r['status']} {r['c']}" for r in n))


def main() -> None:
    con = db()
    offset = None
    print("bot running" + (" in CHECK mode (sends nothing)" if CHECK else ""), flush=True)
    while True:
        try:
            upd = tg("getUpdates", timeout=25, allowed_updates=["callback_query", "message"],
                     **({"offset": offset} if offset else {}))
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    on_callback(con, u["callback_query"])
                elif "message" in u:
                    on_message(con, u["message"])
            work(con)
        except Exception as err:                              # network blips: wait and carry on
            print(f"{now()} {type(err).__name__}: {err}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
