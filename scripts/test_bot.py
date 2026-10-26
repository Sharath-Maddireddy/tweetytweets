"""Self-check for the bot's safety logic (temp DB; no Telegram, no browser).

  python scripts/test_bot.py
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import outbox

tmp = Path(tempfile.mkdtemp())
outbox.DB = tmp / "t.db"
outbox.say = lambda *a, **k: 1                       # no Telegram
import bot

bot.PAUSE = tmp / "PAUSED"
sent = []
bot.execute = lambda row: sent.append(row["id"]) or {"sent": True}
con = outbox.db()
old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="seconds")


def row(oid, kind="li_connect", created=old, status="pending"):
    con.execute("insert into outbox (id, kind, target, text, status, created) values (?,?,?,?,?,?)",
                (oid, kind, "t", "", status, created))


# 1. auto mode off -> nothing approves itself
outbox.setting = lambda k, d=None: None if k == "auto_send_after_min" else d
row("a")
bot.work(con)
assert sent == [] and con.execute("select status from outbox where id='a'").fetchone()[0] == "pending"

# 2. auto mode on, older than the window -> queued and sent; fresh rows wait
outbox.setting = lambda k, d=None: 180 if k == "auto_send_after_min" else d
row("b", created=outbox.now())
bot.work(con)
assert sent == ["a"], sent
assert con.execute("select status from outbox where id='b'").fetchone()[0] == "pending"

# 3. skipped rows never send
row("c", status="skipped")
bot.work(con)
assert "c" not in sent

# 4. /pause stops everything
bot.PAUSE.write_text("x")
row("d")
bot.work(con)
assert "d" not in sent
bot.PAUSE.unlink()

# 5. spacing: right after a send, the next one waits
bot.work(con)
assert "d" not in sent

# 6. daily cap
con.execute("update outbox set sent_at=? where id='a'", (old,))
today = datetime.now(timezone.utc).isoformat(timespec="seconds")
for i in range(bot.DAILY_CAP["li_connect"]):
    con.execute("insert into outbox (id, kind, target, text, status, sent_at) values (?,?,?,?,?,?)",
                (f"x{i}", "li_connect", "t", "", "sent", today))
bot.random.randint = lambda a, b: 0
bot.work(con)
assert "d" not in sent, "cap must hold"

# 7. a sent LinkedIn follow-up / nudge moves the contact forward, never back
def contact_row(oid, kind, key):
    outbox.upsert_contact(key, channel="linkedin", status="accepted")
    con.execute("insert into outbox (id, kind, target, text, status, decided, contact_key) values (?,?,?,?,?,?,?)",
                (oid, kind, "t", "Hi X, hi", "queued", old, key))
con.execute("delete from outbox where kind='li_connect'")
contact_row("m", "li_msg", "li:m")
bot.work(con)
c = con.execute("select status, followup_sent_at from contacts where key='li:m'").fetchone()
assert c["status"] == "followup_sent" and c["followup_sent_at"], dict(c)
contact_row("n", "li_nudge", "li:n")
bot.work(con)
c = con.execute("select status, nudged_at from contacts where key='li:n'").fetchone()
assert c["status"] == "accepted" and c["nudged_at"], dict(c)

# 8. follow-up template: right greeting, raise only when known, agency wording for agencies
import li_watch
li_watch.raise_info = lambda b: "raised $1M Seed on 2026-09-01 (Inc42)" if b == "Acme" else "(no funding info)"
assert li_watch.followup("Ana", "Acme", False).startswith("Hi Ana, thanks for connecting, and congrats on the Acme raise!")
assert "congrats" not in li_watch.followup("Ana", "Zed", False)
assert "for their brand clients" in li_watch.followup("Ana", "AgencyCo", True)
print("ok")
