#!/usr/bin/env python
"""Outreach database + Telegram helpers shared by the bot, the watcher and the runner.

drafts/outreach.db (sqlite, git-ignored) holds:
  outbox    actions waiting for a ✅ tap in Telegram (x_dm, li_connect). The text is
            frozen when the row is added; a tap sends exactly that text.
  contacts  everyone contacted on any channel and where they are in the funnel.
  runs      job results, for failure alerts and the daily heartbeat.

  python scripts/outbox.py import     # one-time: load the old JSON trackers
  python scripts/outbox.py status     # print the funnel
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "drafts" / "outreach.db"
sys.path.insert(0, str(ROOT / "scripts"))

SCHEMA = """
create table if not exists outbox (
  id text primary key, kind text not null, target text not null, expected_name text,
  text text not null, status text not null default 'pending', created text, decided text,
  sent_at text, result text, tg_message_id integer, contact_key text);
create table if not exists contacts (
  key text primary key, channel text, name text, brand text, url text, status text,
  sent_at text, accepted_at text, followup_sent_at text, nudged_at text, replied integer default 0,
  meta text);
create table if not exists runs (job text, started text, ok integer, detail text);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=30, isolation_level=None)   # autocommit; WAL for 2 processes
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------ telegram

def tg_cfg() -> dict:
    import settings
    return settings.load().get("telegram") or {}


def tg(method: str, **params) -> dict:
    t = tg_cfg()
    req = urllib.request.Request(f"https://api.telegram.org/bot{t['bot_token']}/{method}",
                                 data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def say(html: str, buttons: list[tuple[str, str]] | None = None) -> int:
    """Send a message to the owner; buttons = [(label, callback_data<=64 bytes)]."""
    p = {"chat_id": tg_cfg()["chat_id"], "text": html, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": [[{"text": l, "callback_data": d} for l, d in buttons]]}
    return tg("sendMessage", **p)["result"]["message_id"]


# ------------------------------------------------------------------ outbox

def add(kind: str, target: str, text: str, expected_name: str = "", contact_key: str = "",
        header: str = "") -> str:
    """Queue an action and send it to Telegram with ✅ Send / Skip buttons."""
    from html import escape
    assert kind in ("x_dm", "li_connect"), kind
    oid = secrets.token_hex(4)
    con = db()
    con.execute("insert into outbox (id, kind, target, expected_name, text, created, contact_key) "
                "values (?,?,?,?,?,?,?)", (oid, kind, target, expected_name, text, now(), contact_key))
    where = {"x_dm": f"X DM to @{target}", "li_connect": f"LinkedIn request to {expected_name or target}"}[kind]
    body = (f"{header}\n" if header else "") + f"<b>{escape(where)}</b>\n\n" + \
           (f"<code>{escape(text)}</code>" if text else "<i>(no note)</i>")
    mid = say(body, [("✅ Send", f"send:{oid}"), ("✖️ Skip", f"skip:{oid}")])
    con.execute("update outbox set tg_message_id=? where id=?", (mid, oid))
    return oid


def record_run(job: str, ok: bool, detail: str = "") -> None:
    db().execute("insert into runs values (?,?,?,?)", (job, now(), int(ok), detail[:2000]))


# ------------------------------------------------------------------ contacts

def upsert_contact(key: str, **fields) -> None:
    con = db()
    if not con.execute("select 1 from contacts where key=?", (key,)).fetchone():
        con.execute("insert into contacts (key) values (?)", (key,))
    for k, v in fields.items():
        con.execute(f"update contacts set {k}=? where key=?", (v, key))


def import_json() -> None:
    """One-time: bring the JSON trackers into the database."""
    li = ROOT / "drafts" / "linkedin_sent.json"
    for url, v in (json.loads(li.read_text()) if li.exists() else {}).items():
        r = v.get("result") or {}
        sent = r.get("sent") is True
        upsert_contact("li:" + url.rstrip("/").split("/in/")[-1], channel="linkedin", name=v.get("name"),
                       brand=v.get("brand"), url=url,
                       status="followup_sent" if v.get("followup_sent") else "accepted" if v.get("accepted")
                       else "requested" if sent else "not_sent",
                       sent_at=v.get("at") if sent else None,
                       accepted_at=v.get("followup_sent") if v.get("accepted") else None,
                       followup_sent_at=v.get("followup_sent"),
                       meta=json.dumps({"followup_draft": v.get("followup"), "title": v.get("title")}))
    x = ROOT / "drafts" / "leads.json"
    for k, v in (json.loads(x.read_text()) if x.exists() else {}).items():
        if v.get("status") in ("dm_sent", "public_reply_sent"):
            upsert_contact("x:" + k, channel="x", name=v.get("handle", k), brand=v.get("company", ""),
                           url=f"https://x.com/{v.get('handle', k)}", status=v["status"],
                           sent_at=v.get("dm_sent_at") or v.get("contacted_at") or v.get("replied_at"))


def status() -> None:
    con = db()
    for r in con.execute("select channel, status, count(*) n from contacts group by 1, 2 order by 1, 2"):
        print(f"{r['channel']:9} {r['status']:16} {r['n']}")
    for r in con.execute("select status, count(*) n from outbox group by 1"):
        print(f"outbox    {r['status']:16} {r['n']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "import":
        import_json()
    status()
