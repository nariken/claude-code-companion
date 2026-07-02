"""aiagent-control menu bar app (rumps).

Menu bar title: 5h / 7d rate-limit utilization, refreshed periodically.
Dropdown: per-project recent sessions (what the Recent pane won't tell you),
handoff freshness, and one-click actions (open handoff, copy resume command).
"""
import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rumps

from common import L, get_config, handoff_meta_path, handoff_path, read_json
from sessions import age_str, scan_projects
from usage import UsageError, fetch_usage, format_resets_at, max_utilization

REFRESH_SECONDS = 300
MAX_SESSIONS = 5


def _pct_icon(pct: float) -> str:
    if pct >= 90:
        return "🔴"
    if pct >= 70:
        return "🟡"
    return "🟢"


def _copy_to_clipboard(text: str):
    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
    p.communicate(text.encode())


def _run_in_terminal(cmd: str):
    """Open the configured terminal app and run a command there — no paste needed."""
    script = cmd.replace("\\", "\\\\").replace('"', '\\"')
    if get_config()["terminal_app"] == "iTerm":
        osa = [f'tell application "iTerm" to create window with default profile',
               f'tell application "iTerm" to tell current session of current window to write text "{script}"',
               'tell application "iTerm" to activate']
    else:
        osa = [f'tell application "Terminal" to do script "{script}"',
               'tell application "Terminal" to activate']
    subprocess.run(["osascript"] + [x for pair in (("-e", e) for e in osa) for x in pair])


class AiAgentControl(rumps.App):
    def __init__(self):
        super().__init__("CC …", quit_button=None)
        import webdash
        self.dash_url = webdash.start_in_background()
        self.timer = rumps.Timer(self.refresh, REFRESH_SECONDS)
        self.timer.start()
        self.refresh(None)

    # ---------- data -> UI ----------

    def refresh(self, _):
        usage, usage_err = None, None
        try:
            usage = fetch_usage()
        except UsageError as e:
            usage_err = str(e)
        self._set_title(usage, usage_err)
        self._rebuild_menu(usage, usage_err)

    def _set_title(self, usage, usage_err):
        if usage is None:
            self.title = "CC ⚠️"
            return
        five = (usage.get("five_hour") or {}).get("utilization")
        week = (usage.get("seven_day") or {}).get("utilization")
        worst = max_utilization(usage)
        parts = []
        if five is not None:
            parts.append(f"{five:.0f}%")
        if week is not None:
            parts.append(f"w{week:.0f}%")
        self.title = f"{_pct_icon(worst)} " + "·".join(parts) if parts else "CC"

    def _usage_items(self, usage, usage_err):
        items = []
        if usage_err:
            items.append(rumps.MenuItem(L("使用量取得エラー: ", "usage fetch error: ") + usage_err[:60]))
            items.append(rumps.MenuItem(L("再試行", "Retry"), callback=self.force_refresh))
            return items
        five, week = usage.get("five_hour"), usage.get("seven_day")
        if five:
            items.append(rumps.MenuItem(
                L("5時間枠: ", "5h window: ") + f"{five['utilization']:.0f}%"
                + (f"({L('リセット', 'resets')}{format_resets_at(five.get('resets_at'))})"
                   if five.get("resets_at") else "")))
        if week:
            items.append(rumps.MenuItem(
                L("週間枠: ", "weekly: ") + f"{week['utilization']:.0f}%"
                + (f"({L('リセット', 'resets')}{format_resets_at(week.get('resets_at'))})"
                   if week.get("resets_at") else "")))
        for name, win in (usage.get("extras") or {}).items():
            items.append(rumps.MenuItem(f"{name}: {win['utilization']:.0f}%"))
        return items

    def _project_menu(self, proj):
        sub = rumps.MenuItem(f"{proj.name}  ({age_str(proj.last_active)})")
        cwd = proj.cwd or ""
        meta = read_json(handoff_meta_path(cwd), {}) if cwd else {}
        hpath = handoff_path(cwd) if cwd else None
        if hpath and hpath.exists():
            when = (meta or {}).get("generated_at", "?")[:16].replace("T", " ")
            item = rumps.MenuItem(f"📝 handoff ({when})",
                                  callback=self._make_open(hpath))
        else:
            item = rumps.MenuItem(L("📝 handoff なし", "📝 no handoff yet"))
        sub.add(item)
        if cwd:
            sub.add(rumps.MenuItem(L("🆕 新規セッション(ルートで起動・handoff注入)", "🆕 New session (project root, handoff injected)"),
                                   callback=self._make_new_session(cwd)))
        sub.add(rumps.separator)
        for s in proj.sessions[:MAX_SESSIONS]:
            label = s.label or s.session_id[:8]
            mi = rumps.MenuItem(f"[{age_str(s.mtime)}] {label[:60]}",
                                callback=self._make_resume(s))
            sub.add(mi)
        if cwd:
            sub.add(rumps.separator)
            sub.add(rumps.MenuItem(L("フォルダを開く", "Open folder"), callback=self._make_open(Path(cwd))))
        return sub

    def _rebuild_menu(self, usage, usage_err):
        self.menu.clear()
        items = self._usage_items(usage, usage_err)
        items.append(rumps.separator)
        for proj in scan_projects()[:get_config()["max_projects"]]:
            items.append(self._project_menu(proj))
        items.append(rumps.separator)
        if self.dash_url:
            items.append(rumps.MenuItem(L("📱 スマホ用: ", "📱 Mobile: ") + self.dash_url,
                                        callback=self._copy_dash_url))
        items += [
            rumps.MenuItem(L("今すぐ更新", "Refresh now"), callback=self.force_refresh),
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        self.menu = items

    # ---------- callbacks ----------

    def _copy_dash_url(self, _):
        _copy_to_clipboard(self.dash_url)

    def force_refresh(self, _):
        try:
            fetch_usage(force=True)
        except UsageError:
            pass
        self.refresh(None)

    def _make_open(self, path: Path):
        def cb(_):
            subprocess.run(["open", str(path)])
        return cb

    def _make_resume(self, session):
        def cb(_):
            _run_in_terminal(f"cd {session.cwd or '~'} && claude --resume {session.session_id}")
        return cb

    def _make_new_session(self, cwd: str):
        def cb(_):
            _run_in_terminal(f"cd {cwd} && claude")
        return cb


if __name__ == "__main__":
    AiAgentControl().run()
