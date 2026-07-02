"""Scan ~/.claude/projects and build a project -> sessions map.

Session transcripts are JSONL. We read a bounded slice from head and tail
of each file to extract: real cwd, a human label (summary line or first
user message), and last activity time. Files can be >10MB so we never
load one fully.
"""
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from common import HANDOFF_DIR, PROJECTS_DIR

# Sessions created by our own summarizer (or old versions of it) must not
# show up as user projects/sessions.
_INTERNAL_CWD = str(HANDOFF_DIR)
_INTERNAL_LABEL_PREFIXES = ("あなたはClaude Codeセッションの引き継ぎメモ",
                            "You write handoff notes")

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 64 * 1024


@dataclass
class Session:
    session_id: str
    path: Path
    mtime: float
    label: str = ""
    cwd: str = ""


@dataclass
class Project:
    dir_name: str  # encoded dir name under ~/.claude/projects
    cwd: str = ""  # real path, recovered from transcripts
    sessions: list = field(default_factory=list)

    @property
    def name(self) -> str:
        base = self.cwd or self.dir_name
        return os.path.basename(base.rstrip("/")) or base

    @property
    def last_active(self) -> float:
        return max((s.mtime for s in self.sessions), default=0.0)


def _iter_head_lines(path: Path):
    with open(path, "rb") as f:
        chunk = f.read(HEAD_BYTES)
    for line in chunk.split(b"\n"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue  # possibly truncated last line of the chunk


def _extract_text(record) -> str:
    """Pull display text out of a user/assistant transcript record."""
    msg = record.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def _session_label_and_cwd(path: Path) -> tuple:
    label, cwd = "", ""
    for rec in _iter_head_lines(path):
        t = rec.get("type")
        if not cwd and isinstance(rec.get("cwd"), str):
            cwd = rec["cwd"]
        if not label and t == "summary" and rec.get("summary"):
            label = rec["summary"]
        if not label and t == "user":
            text = _extract_text(rec).strip()
            # skip tool results / command wrappers
            if text and not text.startswith("<"):
                label = text.splitlines()[0]
        if label and cwd:
            break
    return label[:80], cwd


def scan_projects(max_sessions_per_project: int = 8,
                  max_age_days: int = 30) -> list:
    """Return projects sorted by recent activity, sessions newest first."""
    cutoff = time.time() - max_age_days * 86400
    projects = []
    if not PROJECTS_DIR.is_dir():
        return projects
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        jsonls = []
        for f in proj_dir.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                jsonls.append((mtime, f))
        if not jsonls:
            continue
        jsonls.sort(reverse=True)
        proj = Project(dir_name=proj_dir.name)
        for mtime, f in jsonls[:max_sessions_per_project]:
            label, cwd = _session_label_and_cwd(f)
            if cwd == _INTERNAL_CWD or label.startswith(_INTERNAL_LABEL_PREFIXES):
                continue
            if cwd and not proj.cwd:
                proj.cwd = cwd
            proj.sessions.append(Session(
                session_id=f.stem, path=f, mtime=mtime, label=label, cwd=cwd))
        if not proj.sessions:
            continue
        projects.append(proj)
    projects.sort(key=lambda p: p.last_active, reverse=True)
    return projects


def age_str(mtime: float) -> str:
    delta = int(time.time() - mtime)
    if delta < 3600:
        return f"{max(delta // 60, 1)}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


if __name__ == "__main__":
    for p in scan_projects()[:10]:
        print(f"\n{p.name}  ({p.cwd or p.dir_name})")
        for s in p.sessions[:5]:
            print(f"  [{age_str(s.mtime)}] {s.label or s.session_id}")
