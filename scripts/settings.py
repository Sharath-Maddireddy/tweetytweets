"""Shared settings for tweetytweets.

Everything install-specific lives in config.json (git-ignored). Scripts never
hardcode a handle, a path, or a character limit: they read it from here, or fall
back to a sane default that works on a fresh machine.

`config.example.json` is the template that ships with the repo; the onboarding
step copies it to config.json and fills in the answers from the interview.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent          # repo root
CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

# Conservative fallback used ONLY until the real ceiling is measured on the
# account. Deliberately the free-tier minimum: guessing low means a long draft
# gets trimmed, guessing high means a post fails at publish time. Nothing here
# assumes what any particular account can do - the limit is a property of THEIR
# account, so it gets asked for and then measured, never hardcoded.
ASSUMED_LIMIT = 280

DEFAULTS: dict[str, Any] = {
    "handle": "",                       # set during onboarding
    "premium": None,                    # None = unknown. Ask the user, then confirm with measure.
    "max_chars": None,                  # None = not measured. Fill via: post.py measure --save
    "max_chars_verified": False,        # True only after a real measurement on the account
    "timezone": "local",                # e.g. "Asia/Kolkata"; "local" = machine tz
    "slots": ["13:00", "16:00", "20:00", "00:00"],
    "jitter_minutes": 45,
    "min_gap_hours": 3,
    "ai_update_slot": "16:00",
    "ai_update_header": "Daily AI updates | Day {day}",
    "ai_update_enabled": True,
    "research_hour": 11,
    "window_hours": 36,
    "subreddits": ["LocalLLaMA", "artificial", "singularity", "ChatGPTCoding",
                   "n8n", "SideProject"],
    "x_queries": ["AI agents", "AI coding", "open source LLM", "developer tools"],
    "feeds": {
        "techcrunch-ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "arxiv-cs-ai": "https://export.arxiv.org/rss/cs.AI",
    },
    "require_verified_source": True,    # refuse to write claims it hasn't read
    "browser": {
        "port": 9222,
        "profile_dir": "",              # empty => <home>/.tweetytweets/chrome-profile
        "chrome_path": "",              # empty => auto-detect
        "headless": False,
    },
    # Research (Reddit + X search) runs in its own browser, signed into a
    # throwaway account, so scraping never touches the posting account.
    # Merged over "browser" when TWEETY_BROWSER=research (research.py sets it).
    "research_browser": {
        "port": 9223,
        "profile_dir": "~/.tweetytweets/chrome-research",
    },
}


def _merged(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merged(out[k], v)
        else:
            out[k] = v
    return out


def load(required: bool = False) -> dict:
    """Merged config. With required=True, exits with guidance if missing."""
    cfg = _load(required)
    if os.environ.get("TWEETY_BROWSER") == "research":
        cfg["browser"] = _merged(cfg["browser"], cfg.get("research_browser") or {})
    return cfg


def _load(required: bool) -> dict:
    if not CONFIG_PATH.exists():
        if required:
            print(json.dumps({
                "error": "config.json not found",
                "fix": f"cp {EXAMPLE_PATH.name} config.json  — then run the onboarding interview",
                "docs": "see README.md > Setup",
            }, indent=2), file=sys.stderr)
            raise SystemExit(2)
        return dict(DEFAULTS)
    try:
        user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SystemExit(f"config.json is not valid JSON: {err}")
    return _merged(DEFAULTS, user)


def home() -> Path:
    return Path(os.path.expanduser("~"))


def profile_dir(cfg: dict | None = None) -> Path:
    if cfg is None:
        cfg = load()
    raw = (cfg.get("browser") or {}).get("profile_dir") or ""
    p = Path(os.path.expanduser(raw)) if raw else home() / ".tweetytweets" / "chrome-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_chrome(explicit: str = "") -> Path | None:
    """Locate a Chrome/Chromium binary on the three desktop platforms."""
    import shutil

    if explicit:
        p = Path(os.path.expanduser(explicit))
        return p if p.exists() else None

    candidates: list[str] = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
                candidates.append(os.path.join(base, "Chromium", "Application", "chrome.exe"))
        candidates += [
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                candidates.append(found)

    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    return None


def state_path() -> Path:
    return ROOT / "state.json"


def data_dir(name: str) -> Path:
    p = ROOT / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def limit(cfg: dict | None = None) -> dict:
    """The character ceiling for THIS account, and how much to trust it.

    Never hardcode a limit: it is a property of the user's account (free tiers
    cap at 280, Premium does not). Ask the user, then MEASURE it with
    `post.py measure`, which types into the composer and reads the Post button's
    state without ever clicking it.

    Returns {chars, verified, source, fix} so callers can warn instead of
    silently assuming.
    """
    if cfg is None:
        cfg = load()
    raw = cfg.get("max_chars")
    verified = bool(cfg.get("max_chars_verified"))
    fix = "run: python scripts/post.py measure --save"
    if not raw:
        return {"chars": ASSUMED_LIMIT, "verified": False,
                "source": f"ASSUMED free-tier minimum ({ASSUMED_LIMIT}), not measured",
                "fix": fix}
    if not verified:
        return {"chars": int(raw), "verified": False,
                "source": f"from config ({int(raw)}), never measured on this account",
                "fix": fix}
    return {"chars": int(raw), "verified": True, "source": "measured on this account",
            "fix": None}


def save_limit(chars: int, premium: bool | None = None) -> dict:
    """Persist a MEASURED ceiling into config.json (used by post.py measure --save)."""
    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["max_chars"] = int(chars)
    cfg["max_chars_verified"] = True
    if premium is None:
        premium = int(chars) > ASSUMED_LIMIT
    cfg["premium"] = bool(premium)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"max_chars": cfg["max_chars"], "premium": cfg["premium"],
            "max_chars_verified": True, "saved_to": str(CONFIG_PATH)}


def tzinfo():
    """The timezone slots are interpreted in."""
    from datetime import datetime, timedelta, timezone
    name = (load().get("timezone") or "local")
    if name and name.lower() != "local":
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo or timezone(timedelta(0))
