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
print("ok")
