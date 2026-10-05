# tweetytweets

**An autonomous X/Twitter posting pipeline for your AI agent.**

It researches the day's news in your niche, writes posts in *your* voice, publishes
them through a real logged-in browser, and proves each one actually went live.

- **No X API key.** No developer account, no phone verification, no $215/month read tier.
- **No paid scrapers.** Research comes from sources that are free and open.
- **One dependency.** `websockets`. Everything else is the Python standard library.
- **It verifies its own work.** Reading the profile back is the only accepted proof.

```
        ┌──────────┐   ┌────────┐   ┌───────────┐   ┌─────────┐   ┌────────┐
  11:00 │ RESEARCH │ → │  PLAN  │ → │ SCHEDULE  │ → │ PUBLISH │ → │ VERIFY │
        └──────────┘   └────────┘   └───────────┘   └─────────┘   └────────┘
         HN, RSS,       agent        cron gate        real          read the
         Reddit, X      writes 4     wakes only       browser       profile
         ~150 items     drafts       when due         composer      back
```

---

## Table of contents

- [What it actually does](#what-it-actually-does)
- [Requirements](#requirements)
- [Setup](#setup)
- [Step 0 — the interview (this is the point)](#step-0--the-interview-this-is-the-point)
- [Using it](#using-it)
- [Scheduling](#scheduling)
- [Configuration reference](#configuration-reference)
- [What it costs](#what-it-costs)
- [Guardrails](#guardrails)
- [Risks you should read before running it](#risks-you-should-read-before-running-it)
- [Troubleshooting](#troubleshooting)
- [Design notes](#design-notes)
- [Repository layout](#repository-layout)

---

## What it actually does

Every day, unattended:

| Stage | What happens |
|---|---|
| **Research** | Pulls ~150 candidate stories from Hacker News (Algolia API), RSS feeds, Reddit (6 subreddits) and X search (8 queries), then ranks them by engagement and recency. |
| **Plan** | Your agent reads the swipe file, picks stories, **opens the actual source article**, and writes one post per slot — including a daily news roundup with its own day counter. |
| **Schedule** | Each slot gets a random minute inside its window. A monitor script decides when something is due. When nothing is due it prints `IDLE` and **the model is never called** — idle time is free. |
| **Publish** | Types into the real X composer over Chrome DevTools Protocol, respecting the account's true character limit, then clicks Post. |
| **Verify** | Polls the profile until the new post appears, screenshots it, and has a vision model read the text back. Only then is the slot closed. |

### What makes it different from the usual "AI posts for you" repo

1. **It reads before it writes.** Every claim in a post traces to an article the
   agent actually opened. Anything it couldn't verify gets dropped — that
   behaviour is enforced, not just suggested.
2. **It refuses to publish on doubt.** Wrong account, over the character limit,
   empty draft, no source — it stops and tells you, rather than posting anyway.
3. **The schedule is free when idle.** The gate is a plain Python script; a cron
   monitor suppresses the agent run entirely when nothing is due.
4. **It knows it can be wrong.** The verification step documents exactly what the
   vision model is and isn't trustworthy for, because getting that wrong once
   caused a successfully-published post to be reported as a failure.

---

## Requirements

- Python **3.10+**
- Chrome, Chromium or Edge (auto-detected; overridable)
- An X/Twitter account you can log into in a browser
- An AI agent that can run shell commands (Claude Code, Hermes Agent, Cursor, Codex, …)

---

## Setup

```bash
git clone <this repo> tweetytweets
cd tweetytweets
pip install -r requirements.txt

cp config.example.json config.json     # fill this in during the interview
python scripts/doctor.py               # tells you exactly what's missing
```

Then start the automation browser and sign in **once**:

```bash
python scripts/browser.py launch
#  a separate Chrome window opens with its own profile.
#  Sign in to x.com there.
#  Use "Email or username" — NOT "Continue with Google", which can
#  silently create a second account instead of logging into yours.
```

Research (Reddit + X search) runs in a **second** browser, signed into a
throwaway X account, so scraping never touches the account you post from:

```bash
TWEETY_BROWSER=research python scripts/browser.py launch
#  sign in to x.com there with the DUMMY account, not your real one.
#  research.py refuses to scrape X if this window is signed in as `handle`.
```

Verify, and measure your real character ceiling:

```bash
python scripts/post.py check      # should print your handle
python scripts/post.py measure --save  # measure THIS account's ceiling, then store it
```

**Use a dedicated browser profile.** That is what the launch command gives you.
Driving your everyday browser risks posting as the wrong account.

---

## Step 0 — the interview (this is the point)

Point your agent at `SKILL.md` and let it interview you. The single most
important question it will ask is for **your best-performing posts** — ideally
ones with **100,000+ impressions**.

> "Give me 5-20 of your best posts. Paste them raw, keep the line breaks,
> separate each with a line of `---`. I'm not copying them — I'm measuring how
> you open, how long you go, whether you use numbers or questions, what you
> actually talk about."

The agent saves them and runs:

```bash
python scripts/voice_profile.py --in scratch/my_top_tweets.txt
```

which reports, measured rather than guessed:

```
LENGTH
  min 132 | median 138 | mean 161 | max 216 chars
  within the account's 25000 -char ceiling: 5/5   -> all posts fit the account's ceiling

STRUCTURE
  beats per post (blank-line separated): median 3
  uses blank-line breaks: 5/5

DEVICES (how many posts use each)
  numbers       4/5
  questions     0/5
  links         0/5
  hashtags      0/5 posts, 0 total
  emoji         0/5 posts, 0 total

HOOK SHAPE — how they open
    2x  directive / addressed to the reader
    1x  statement / claim
    1x  number / specific figure
    1x  question

THE ACTUAL FIRST 6 WORDS (pattern-match these):
   1. An open-weight model just made exploits
   2. Stop writing prompts. Write evals.
   3. Everyone is wasting Claude on mid
   4. I analyzed 200 viral dev posts
   5. How I cut a 6-hour review
```

That output becomes `references/voice-profile.local.md`, plus a do-not list
(words and formats you never use), and **every draft is written against it**.

It is git-ignored on purpose — it is your voice, not part of the shared tool.

You can also feed it posts you admire from *other* accounts. Treat those as
structural references (shape, hook construction) — never as voice.

---

## Using it

```bash
# 1. gather the day's material
python scripts/research.py collect --hours 36
python scripts/research.py sources                  # per-feed health

# 2. have your agent write 4 drafts against the voice profile, then:
python scripts/due.py make-plan                     # random due times, empty slots
python scripts/due.py fill --slot 13:00 --file scratch/x1.txt
python scripts/due.py fill --slot 16:00 --file scratch/xai.txt
python scripts/due.py show                          # what's ready, what's posted

# 3. publish (or let the schedule do it)
python scripts/post.py post --text-file scratch/x1.txt
python scripts/post.py post --text-file scratch/xai.txt --kind ai_update

# 4. verify by looking
python scripts/post.py shot --what profile          # -> shots/profile.png
python scripts/post.py verify                       # newest posts, read back
```

Useful extras:

```bash
python scripts/post.py compose --text "..." --dry-run   # fills the box, posts nothing
python scripts/post.py day                              # next roundup number
python scripts/due.py check                             # what the cron monitor sees
python scripts/doctor.py --live                         # network + browser health
```

---

## Scheduling

`due.py` is designed to be the thing a scheduler *monitors*, not the thing it
runs blindly:

- nothing due → prints `IDLE`, **byte-identical every tick**, so a monitor can
  suppress the agent run entirely → **idle ticks cost nothing**
- something due → prints the draft plus an **incrementing tick counter**, so a
  failed run is retried on the next tick instead of stalling silently
- anything more than 6 hours overdue is **retired, never posted late**

### With Hermes Agent

Two cron jobs:

```
x-plan     every day at 11am   (agent): research + write + fill the 4 slots
x-autopost  every 10m, monitor=x_autopost_due.py  (agent): publish whatever is due
```

The monitor shim is ~20 lines: it runs `due.py check` and prints its stdout.
See `examples/` for both job definitions and a plain-cron equivalent.

### With plain cron

```cron
0 11 * * *   cd /path/to/tweetytweets && python scripts/research.py collect --hours 36
*/10 * * * * cd /path/to/tweetytweets && python scripts/cron_gate.py
```

`cron_gate.py` runs `due.py check`; when the output is not `IDLE` it hands the
draft to your agent. Wire it to whatever you use to invoke a model.

> **Timing note:** a slot publishes on the first tick *after* it comes due, so a
> post can land 0–20 minutes after its scheduled minute. That is a feature, not a
> bug — it is part of not looking like a metronome.

---

## Configuration reference

`config.json` (copy from `config.example.json`). Everything is optional except
`handle`.

| Key | Meaning |
|---|---|
| `handle` | Your handle, no `@`. The publisher refuses to post as anyone else. |
| `premium` | Whether the account can post above 280 chars. **Ask, never assume** — `post.py measure --save` fills this in from a real measurement. |
| `max_chars` | Hard ceiling. `due.py fill` rejects drafts over it before the scheduled time. |
| `timezone` | `"local"` or an IANA name like `"Asia/Kolkata"`. Slots are interpreted here. |
| `slots` | Anchor times, e.g. `["13:00","16:00","20:00","00:00"]`. A `00:00` slot belongs to the previous day's run. |
| `jitter_minutes` | Each slot lands within ± this many minutes of its anchor. |
| `min_gap_hours` | Minimum spacing enforced between slots. |
| `ai_update_enabled` | Whether one slot is a repeating "Daily … \| Day N" roundup. |
| `ai_update_slot` | Which anchor gets the roundup. |
| `ai_update_header` | The header template. `{day}` is substituted from the ledger. |
| `window_hours` | How far back research looks. 36 is a good default. |
| `subreddits` | Subreddits to scrape for material. |
| `x_queries` | X search queries for material. |
| `feeds` | `name -> RSS/Atom URL` map. Add your own niche sources here. |
| `require_verified_source` | If true, the agent must read a source before writing a claim about it. |
| `browser.port` | Chrome debugging port. |
| `browser.profile_dir` | Dedicated profile dir. Empty → `~/.tweetytweets/chrome-profile`. |
| `browser.chrome_path` | Override Chrome auto-detection. |
| `browser.headless` | Run without a window. Some sites behave differently headless — leave `false` unless you need it. |

---

## What it costs

Everything except the model is free.

| Component | Cost |
|---|---|
| Hacker News, RSS, Reddit DOM, X search | **$0** — no keys, no accounts, no proxies |
| Publishing | **$0** — drives a browser, not the paid API |
| Idle schedule ticks (144/day) | **$0** — no model call at all |
| Agent runs (1 plan + 4 posts a day) | **≈$0.03/day** on a cheap model, less with prompt caching |
| Vision verification (4 screenshots/day) | **≈$0.005/day** |
| **Total** | **≈$0.90/month** |

For reference, X's own API read tier for this volume of research is roughly
**$215/month**.

---

## Guardrails

These are enforced in code, not just documented:

- **Account guard** — refuses to publish unless the signed-in handle matches
  `config.handle`.
- **Length guard** — rejects over-limit drafts at *planning* time, and accounts
  for the characters the roundup header will consume.
- **Verify-before-closing** — a slot is only marked done after the post is read
  back off the profile.
- **Retry cap** — at most two attempts. A duplicate post is worse than a missed one.
- **Stale retirement** — a slot more than 6h overdue is dropped, so the machine
  waking from sleep doesn't publish yesterday's news.
- **No engagement bait** — the skill explicitly forbids "comment X and I'll DM
  you" patterns, and explains why: X's ranking notes single out engagement bait
  as the one category where even large accounts get no pass.
- **No fabricated figures** — every number must trace to a source the agent read.

---

## Risks you should read before running it

Being honest about this, because a README that hides it is not worth trusting:

- **Automating X through the browser is against X's automation rules.** Those
  rules expect you to use their API. Realistically, driving a real logged-in
  browser with human-like pacing is the least detectable approach available —
  but it is still a rule violation, and **you could lose the account.**
- **New accounts posting automated content at volume look like spam networks.**
  Warm the account up first: a real bio, an avatar, some manual posts and
  replies. Post less at the start than you eventually want to.
- **Reply-count confusion is normal.** A profile's post count includes replies,
  so the number rising doesn't always mean the automation posted. Check the
  Posts tab before panicking.
- **The scraping paths depend on DOM attributes** that X and Reddit change
  without notice. If a source starts returning zero items, that is usually why —
  `research.py sources` will show which one broke.
- **Don't automate replies, likes and follows.** This pipeline deliberately only
  publishes. Automated engagement is the fastest route to being actioned, and
  genuine replies are the thing that actually grows an account.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `not logged in` | Sign in to x.com in the automation browser window, then `post.py check`. |
| `WRONG ACCOUNT` | The profile is signed into a different account. Fix the sign-in; the guard is working. |
| Post button never enables | Text is over the real limit, or the page didn't finish loading. `post.py compose --dry-run` to inspect. |
| `verified: false` after a successful post | Expected sometimes — the timeline renders stale. Run `post.py verify` before concluding anything. |
| A slot never publishes | `python scripts/due.py check`. `IDLE` means nothing is due or the slots are still empty. |
| A source returns 0 items | `research.py sources` for feed health. Reddit/X paths break when their DOM changes. |
| Browser won't start | `doctor.py` for the detected binary; set `browser.chrome_path` if auto-detection picked wrong. |
| `ModuleNotFoundError: websockets` | `pip install -r requirements.txt`. |

---

## Design notes

Three decisions worth understanding, because they are the difference between a
demo and something you'd leave running.

**Publish through the browser, not the API.** X's free API tier requires a
developer account and phone verification, caps reads at a level useless for
research, and the paid tier for this workload runs about $215/month. Driving the
real composer with the account you already have costs nothing. The trade-off is
fragility in exchange for cost — a fair trade for a personal account, and the
reason the DOM selectors are isolated in one place.

**Gate the schedule with a script, not the model.** Cron can't express "at a
random time each day", and waking a model every ten minutes to ask "is it time
yet?" is pure waste. So the randomness is baked into the plan file, and a plain
Python script decides what's due. Identical output ⇒ the scheduler suppresses the
agent entirely. Idle time genuinely costs zero.

**Verify by reading back, and know the limits of your verifier.** A click on the
Post button proves nothing — a successful toast has accompanied a post that never
published. So every post is read back off the profile. And because that read can
itself be stale, the check polls. Equally important: the vision check has a
documented trust boundary, because it once reported a post count that sent us
chasing a phantom post that turned out to be a reply sent from a phone.

---

## Repository layout

```
SKILL.md                  agent-facing instructions, including the interview
README.md                 this file
config.example.json       template config
requirements.txt          one dependency
LICENSE                   MIT

scripts/
  settings.py             config loading, path + Chrome discovery, timezone
  browser.py              minimal CDP driver (launch, tabs, eval, screenshots)
  research.py             HN / RSS / Reddit / X collectors -> swipe/<date>.json
  post.py                 check, measure, compose, post, verify, shot
  due.py                  the schedule gate: make-plan / check / fill / mark
  voice_profile.py        measures the user's own posts -> voice profile
  doctor.py               readiness check
  cron_gate.py            plain-cron entry point for the schedule gate

examples/                 cron job definitions (Hermes and plain cron)
references/
  voice-profile.local.md  YOUR voice — git-ignored
```

---

## Credits and license

MIT — see `LICENSE`. Use it, fork it, improve it.

The algorithm weights referenced in `SKILL.md` come from X's published ranking
source. The research sources are the free public APIs and feeds of the sites
listed in `config.example.json`; be a good citizen with them.

If you build something with this, the credit it actually wants is a real post
that a real person found useful.
