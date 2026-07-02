"""Fetch subscription rate-limit usage via Claude Code's OAuth credentials.

The access token lives in the macOS Keychain item "Claude Code-credentials"
(created by Claude Code itself). We read it at runtime and call the same
usage endpoint the /usage command uses. The token is never written to disk
or logs by this module.
"""
import json
import subprocess
import time
import urllib.error
import urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"

_cache = {"ts": 0.0, "data": None, "error": None}
CACHE_TTL = 240  # seconds


class UsageError(Exception):
    pass


def _get_access_token() -> str:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise UsageError(f"keychain access failed: {e}")
    if out.returncode != 0:
        raise UsageError("credentials not found in Keychain (is Claude Code logged in?)")
    raw = out.stdout.strip()
    try:
        creds = json.loads(raw)
        token = (creds.get("claudeAiOauth") or {}).get("accessToken") or creds.get("accessToken")
    except json.JSONDecodeError:
        token = raw  # some versions store the bare token
    if not token:
        raise UsageError("access token missing from credentials")
    return token


def _parse_window(d: dict) -> dict:
    if not isinstance(d, dict):
        return {}
    util = d.get("utilization")
    if util is None:
        return {}
    return {"utilization": float(util), "resets_at": d.get("resets_at")}


def fetch_usage(force: bool = False) -> dict:
    """Return {"five_hour": {...}, "seven_day": {...}, "extras": {...}}.

    Values are utilization percentages (0-100). Cached for CACHE_TTL.
    Raises UsageError on failure.
    """
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    if not force and _cache["error"] and now - _cache["ts"] < 60:
        raise UsageError(_cache["error"])

    try:
        token = _get_access_token()
        req = urllib.request.Request(USAGE_URL, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": "aiagent-control/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode())
    except UsageError:
        raise
    except urllib.error.HTTPError as e:
        from common import L
        msg = (L("認証エラー(401): ターミナルで claude を起動し /login してください",
                 "Auth error (401): run `claude` in a terminal and /login")
               if e.code == 401 else f"usage API HTTP {e.code}")
        _cache.update(ts=now, error=msg)
        raise UsageError(msg)
    except Exception as e:
        _cache.update(ts=now, error=str(e))
        raise UsageError(f"usage API failed: {e}")

    result = {"five_hour": {}, "seven_day": {}, "extras": {}}
    for key, val in payload.items():
        win = _parse_window(val)
        if not win:
            continue
        if key == "five_hour":
            result["five_hour"] = win
        elif key == "seven_day":
            result["seven_day"] = win
        else:
            result["extras"][key] = win
    if not (result["five_hour"] or result["seven_day"] or result["extras"]):
        _cache.update(ts=now, error="unexpected usage payload shape")
        raise UsageError(f"unexpected usage payload: {list(payload)[:8]}")
    _cache.update(ts=now, data=result, error=None)
    return result


def max_utilization(usage: dict) -> float:
    vals = []
    for w in [usage.get("five_hour"), usage.get("seven_day"),
              *usage.get("extras", {}).values()]:
        if w and "utilization" in w:
            vals.append(w["utilization"])
    return max(vals, default=0.0)


def format_resets_at(iso: str) -> str:
    if not iso:
        return ""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%m/%d %H:%M")
    except Exception:
        return iso


if __name__ == "__main__":
    try:
        u = fetch_usage(force=True)
        print(json.dumps(u, indent=2, ensure_ascii=False))
    except UsageError as e:
        print(f"ERROR: {e}")
