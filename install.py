#!/usr/bin/env python3
"""Installer for aiagent-control.

1. Merges SessionEnd / PreCompact / SessionStart hooks into
   ~/.claude/settings.json (backs it up first, idempotent).
2. Installs a LaunchAgent so the menu bar app starts at login.
3. Starts the menu bar app now.

Run: python3 install.py        (add --uninstall to remove)
"""
import json
import plistlib
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
HOOK_CMD = f"python3 {APP_DIR}/src/hook_entry.py"
SETTINGS = Path.home() / ".claude" / "settings.json"
AGENT_LABEL = "com.aiagent-control.menubar"
AGENT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
# Identifies our hook entries for idempotency regardless of where the
# repo was cloned (the command always ends in "hook_entry.py <mode>").
MARKER = "hook_entry.py"

HOOK_DEFS = {
    "SessionEnd": [{
        "hooks": [{"type": "command", "command": f"{HOOK_CMD} session_end", "timeout": 20}],
    }],
    "PreCompact": [{
        "matcher": "auto|manual",
        "hooks": [{"type": "command", "command": f"{HOOK_CMD} session_end", "timeout": 20}],
    }],
    "SessionStart": [{
        "matcher": "startup|clear",
        "hooks": [{"type": "command", "command": f"{HOOK_CMD} session_start", "timeout": 15}],
    }],
}


def _is_ours(entry: dict) -> bool:
    return any(MARKER in (h.get("command") or "")
               for h in entry.get("hooks", []) if isinstance(h, dict))


def merge_hooks(remove: bool = False):
    settings = {}
    if SETTINGS.exists():
        backup = SETTINGS.with_suffix(
            f".json.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(SETTINGS, backup)
        print(f"  backup: {backup}")
        settings = json.loads(SETTINGS.read_text())
    hooks = settings.setdefault("hooks", {})
    for event, defs in HOOK_DEFS.items():
        existing = [e for e in hooks.get(event, []) if not _is_ours(e)]
        hooks[event] = existing if remove else existing + defs
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    SETTINGS.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"  hooks {'removed from' if remove else 'merged into'} {SETTINGS}")


def install_launch_agent():
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(APP_DIR / "src" / "menubar.py")],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(APP_DIR / "logs" / "menubar.out.log"),
        "StandardErrorPath": str(APP_DIR / "logs" / "menubar.err.log"),
    }
    (APP_DIR / "logs").mkdir(exist_ok=True)
    with open(AGENT_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "unload", str(AGENT_PLIST)],
                   capture_output=True)
    subprocess.run(["launchctl", "load", str(AGENT_PLIST)], check=True)
    print(f"  LaunchAgent loaded: {AGENT_PLIST}")


def uninstall():
    merge_hooks(remove=True)
    if AGENT_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(AGENT_PLIST)],
                       capture_output=True)
        AGENT_PLIST.unlink()
        print(f"  LaunchAgent removed")
    print("uninstalled.")


def main():
    if "--uninstall" in sys.argv:
        uninstall()
        return
    print("Installing aiagent-control ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "--user",
                    "--quiet", "rumps"], check=True)
    merge_hooks()
    install_launch_agent()
    print("done. メニューバーに 🟢 が出るまで数秒かかります。")
    print("初回は Keychain アクセス許可のダイアログが出るので「常に許可」を選んでください。")


if __name__ == "__main__":
    main()
