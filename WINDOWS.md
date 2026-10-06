# Windows setup

Moving the install from the Mac to an always-on Windows desktop. Run everything
in PowerShell from the repo folder unless noted.

## 1. Install

- Python 3.12+ (python.org; tick "Add python.exe to PATH")
- Brave (or Chrome)
- Git
- Claude Code, native installer (gives a real `claude.exe`, which the reply
  queue calls directly):
  ```powershell
  irm https://claude.ai/install.ps1 | iex
  ```

## 2. Code

```powershell
git clone https://github.com/Sharath-Maddireddy/tweetytweets.git
cd tweetytweets
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## 3. Config and secrets

The non-secret config is in git as `config.desktop.json` (Windows Brave path
already set). Secrets are recreated here, never copied or committed:

```powershell
copy config.desktop.json config.json
```

1. **Telegram bot token**: in Telegram, BotFather → `/mybots` → the bot →
   API Token. Paste it into `"bot_token": ""` in `config.json`.
2. **Claude token**: run `claude setup-token`, approve in the browser, then save
   the printed `sk-ant-oat01-...` token on one line in
   `%USERPROFILE%\.tweetytweets\claude_token`:
   ```powershell
   mkdir $env:USERPROFILE\.tweetytweets -Force; notepad $env:USERPROFILE\.tweetytweets\claude_token
   ```
3. If Brave lives elsewhere, fix `browser.chrome_path` (JSON needs `\\`).

```powershell
.venv\Scripts\python scripts\doctor.py     # should report nothing blocking
```

## 4. Sign in to both browsers (fresh profiles on this machine)

```powershell
$env:TWEETY_BROWSER="research"; .venv\Scripts\python scripts\browser.py launch; Remove-Item Env:TWEETY_BROWSER
```
Sign in to x.com there with the **dummy** account.

```powershell
.venv\Scripts\python scripts\browser.py launch
```
Sign in there with the **real** account (Email or username, not Google). Then:

```powershell
.venv\Scripts\python scripts\post.py check            # must print the real handle
.venv\Scripts\python scripts\replies.py --dry-run     # finds posts + drafts, prints only
```

## 5. Schedule the reply queue (20:25 IST daily)

Make sure Windows' time zone is India Standard Time. Replace `C:\path\to` with
the real repo path:

```powershell
mkdir logs -Force
schtasks /Create /TN "tweetytweets replies" /SC DAILY /ST 20:25 /TR "cmd /c cd /d C:\path\to\tweetytweets && .venv\Scripts\python.exe scripts\replies.py >> logs\replies.log 2>&1"
schtasks /Run /TN "tweetytweets replies"     # test once, then check logs\replies.log
```

The task runs only while you're logged in, which is what we want: it drives
the visible Brave windows.

## 6. Keep it awake

Settings → System → Power → Screen and sleep → "When plugged in, put my device
to sleep after" → **Never**. Leave both Brave windows open (minimised is fine).

## 7. Turn the Mac job off

Each machine keeps its own record of what it has sent, so leaving both on
means duplicate Telegram messages. On the Mac:

```bash
launchctl bootout gui/$(id -u)/local.tweetytweets.replies
```
