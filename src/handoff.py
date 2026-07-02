"""Generate per-project handoff notes from a session transcript.

Primary path: summarize the transcript tail with a cheap model via
`claude -p`. Fallback (LLM unavailable, or 5h window >= BUDGET_SKIP_PCT):
mechanical extraction of recent instructions / todos / touched files.

Output: ~/.claude/handoffs/<project-slug>.md — injected into new sessions
by the SessionStart hook, so no manual paste is needed.
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

from common import (HOOK_GUARD_ENV, L, ensure_dirs, get_config,
                    handoff_meta_path, handoff_path, log)
from sessions import _extract_text

TAIL_MESSAGES = 60          # how many user/assistant messages to feed
PER_MESSAGE_CHARS = 1500
TOTAL_CHARS = 45000
MIN_MESSAGES = 4            # skip trivial sessions

PROMPT_JA = """あなたはClaude Codeセッションの引き継ぎメモ(handoff)を書くアシスタントです。
以下はあるプロジェクトの直近セッションの会話ログ(抜粋)です。
次のセッションを開始するAIが即座に作業を継続できるよう、日本語で簡潔なhandoffを書いてください。

必ず以下の構成で、事実のみを書くこと(推測で補完しない):
## 目的 / 背景
## 完了したこと
## 未完了・次にやること
## 重要な決定事項・制約
## 関連ファイル・コマンド

会話ログ:
"""

PROMPT_EN = """You write handoff notes for Claude Code sessions.
Below is an excerpt of the most recent session in a project.
Write a concise handoff in English so the AI starting the next session can
continue immediately.

Use exactly this structure and state only facts (no speculation):
## Goal / Context
## Completed
## Remaining / Next steps
## Key decisions & constraints
## Relevant files & commands

Conversation log:
"""


def _tool_lines(rec) -> list:
    """Summarize tool_use blocks: which tools ran on which files/commands."""
    msg = rec.get("message") or {}
    content = msg.get("content")
    lines = []
    if not isinstance(content, list):
        return lines
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        name = b.get("name", "?")
        inp = b.get("input") or {}
        detail = (inp.get("file_path") or inp.get("command")
                  or inp.get("description") or "")
        lines.append(f"({name}) {str(detail)[:150]}".strip())
    return lines[:5]


def _collect_tail(transcript_path: Path) -> list:
    """Last TAIL_MESSAGES user/assistant messages, oldest first.

    Includes a compact trace of tool activity (edited files, commands) so
    the summary knows what was actually done, not just what was said.
    """
    msgs = []
    try:
        with open(transcript_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t not in ("user", "assistant"):
                    continue
                if rec.get("isMeta"):
                    continue
                text = _extract_text(rec).strip()
                if text.startswith("<local-command") or text.startswith("<command-"):
                    text = ""
                if t == "assistant":
                    tools = _tool_lines(rec)
                    if tools:
                        text = (text + "\n" if text else "") + L("実行: ", "ran: ") + "; ".join(tools)
                if not text:
                    continue
                msgs.append((t, text[:PER_MESSAGE_CHARS], rec.get("timestamp") or ""))
                if len(msgs) > TAIL_MESSAGES * 3:
                    msgs = msgs[-TAIL_MESSAGES * 2:]
    except OSError as e:
        log("handoff", f"cannot read transcript {transcript_path}: {e}")
        return []
    return msgs[-TAIL_MESSAGES:]


def _budget_allows_llm() -> bool:
    try:
        from usage import fetch_usage
        u = fetch_usage()
        five = (u.get("five_hour") or {}).get("utilization", 0)
        return five < get_config()["budget_skip_pct"]
    except Exception:
        return True  # can't tell -> don't block generation


def _llm_summarize(convo_text: str, cwd: str) -> str:
    env = dict(os.environ, **{HOOK_GUARD_ENV: "1"})
    try:
        out = subprocess.run(
            ["claude", "-p", "--model", get_config()["summary_model"]],
            capture_output=True, text=True, timeout=180, env=env,
            input=L(PROMPT_JA, PROMPT_EN) + convo_text, cwd=str(Path.home()))
    except FileNotFoundError:
        log("handoff", "claude CLI not found")
        return ""
    except subprocess.TimeoutExpired:
        log("handoff", "LLM summarize timed out")
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        log("handoff", f"claude -p failed rc={out.returncode}: {out.stderr[:300]}")
        return ""
    return out.stdout.strip()


def _mechanical_extract(msgs: list) -> str:
    user_msgs = [t for role, t, _ in msgs if role == "user"][-10:]
    lines = [L("## 直近のユーザー指示(自動抽出)", "## Recent user instructions (auto-extracted)")]
    lines += [f"- {m.splitlines()[0][:200]}" for m in user_msgs]
    lines.append(L("\n## 直近のアシスタント報告(自動抽出)", "\n## Recent assistant reports (auto-extracted)"))
    asst = [t for role, t, _ in msgs if role == "assistant"][-3:]
    lines += [f"> {m[:500]}" for m in asst]
    return "\n".join(lines)


def _newer_than_existing_handoff(cwd: str, msgs: list) -> bool:
    """Guard against stale overwrite: resuming an old session and exiting
    without real work must not clobber a handoff built from newer activity.
    Regenerate only if the transcript has >=2 messages newer than the
    existing handoff."""
    from common import read_json
    meta = read_json(handoff_meta_path(cwd), {}) or {}
    gen_at = meta.get("generated_at")
    if not gen_at:
        return True  # no handoff yet
    try:
        threshold = datetime.datetime.fromisoformat(gen_at).astimezone()
    except ValueError:
        return True
    newer = 0
    for _, _, ts in msgs:
        if not ts:
            continue
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if dt > threshold:
            newer += 1
    return newer >= 2


def generate(cwd: str, transcript_path: str, session_id: str,
             trigger: str) -> bool:
    ensure_dirs()
    msgs = _collect_tail(Path(transcript_path))
    if len(msgs) < MIN_MESSAGES:
        log("handoff", f"skip {session_id[:8]} ({trigger}): only {len(msgs)} messages")
        return False
    if not _newer_than_existing_handoff(cwd, msgs):
        log("handoff", f"skip {session_id[:8]} ({trigger}): no activity newer than existing handoff")
        return False

    convo, total = [], 0
    for role, text, _ in msgs:
        total += len(text)
        if total > TOTAL_CHARS:
            break
        convo.append(f"[{role}] {text}")
    convo_text = "\n\n".join(convo)

    body, method = "", "llm"
    if _budget_allows_llm():
        body = _llm_summarize(convo_text, cwd)
    if not body:
        body, method = _mechanical_extract(msgs), "extract"

    now = datetime.datetime.now()
    header = (f"# Session Handoff — {os.path.basename(cwd.rstrip('/'))}\n"
              f"<!-- generated by aiagent-control: {now.isoformat(timespec='seconds')} "
              f"trigger={trigger} session={session_id} method={method} -->\n\n")
    handoff_path(cwd).write_text(header + body + "\n")
    handoff_meta_path(cwd).write_text(json.dumps({
        "generated_at": now.isoformat(timespec="seconds"),
        "session_id": session_id, "trigger": trigger, "method": method,
        "cwd": cwd,
    }, ensure_ascii=False, indent=2))
    log("handoff", f"generated for {cwd} ({method}, trigger={trigger}, {len(msgs)} msgs)")
    return True


if __name__ == "__main__":
    # usage: handoff.py <cwd> <transcript_path> <session_id> <trigger>
    if len(sys.argv) != 5:
        print("usage: handoff.py <cwd> <transcript> <session_id> <trigger>", file=sys.stderr)
        sys.exit(2)
    ok = generate(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    sys.exit(0 if ok else 1)
