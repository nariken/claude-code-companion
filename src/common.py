"""Shared paths and helpers for aiagent-control."""
import json
import os
import re
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
HANDOFF_DIR = CLAUDE_DIR / "handoffs"
APP_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = APP_DIR / "logs"

# Env guard: set on every claude subprocess we spawn so our own hooks
# don't fire recursively for handoff-generation sessions.
HOOK_GUARD_ENV = "AIAGENT_CONTROL_INTERNAL"

DEFAULT_CONFIG = {
    "language": "auto",          # "ja" | "en" | "auto" (from $LANG)
    "summary_model": "haiku",    # model for handoff summarization
    "budget_skip_pct": 93,       # skip LLM summarization above this 5h usage
    "dashboard_port": 8787,
    "terminal_app": "Terminal",  # "Terminal" | "iTerm"
    "max_projects": 10,          # projects shown in menu/dashboard
    "stale_handoff_days": 14,    # don't inject handoffs older than this
}

_config_cache = None


def get_config() -> dict:
    global _config_cache
    if _config_cache is None:
        cfg = dict(DEFAULT_CONFIG)
        user_cfg = read_json(APP_DIR / "config.json", {}) or {}
        cfg.update({k: v for k, v in user_cfg.items() if k in DEFAULT_CONFIG})
        _config_cache = cfg
    return _config_cache


def get_lang() -> str:
    lang = get_config()["language"]
    if lang in ("ja", "en"):
        return lang
    return "ja" if "ja" in (os.environ.get("LANG") or "").lower() else "en"


def L(ja: str, en: str) -> str:
    """Pick a UI/prompt string by configured language."""
    return ja if get_lang() == "ja" else en


def ensure_dirs():
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def project_slug(cwd: str) -> str:
    """Mirror Claude Code's project-dir encoding: /Users/x/foo -> -Users-x-foo."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def handoff_path(cwd: str) -> Path:
    return HANDOFF_DIR / f"{project_slug(cwd)}.md"


def handoff_meta_path(cwd: str) -> Path:
    return HANDOFF_DIR / f"{project_slug(cwd)}.meta.json"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def log(name: str, message: str):
    ensure_dirs()
    import datetime
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(LOG_DIR / f"{name}.log", "a") as f:
        f.write(f"[{ts}] {message}\n")
