"""Queue, lock, and status for continuously-maintained Context Lens state.
Filesystem-only: no daemon, no database. Queue entries are files named by
session id (touch = enqueue, idempotent). The worker lock is an atomic
mkdir with a pid file; dead owners are stolen."""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import discover as D


def trajdir():
    return D.VAULT / "trajectory"


def qdir():   return trajdir() / "queue"
def fdir():   return trajdir() / "failed"
def lockdir(): return trajdir() / "worker.lock"
def statef(): return trajdir() / "worker.state"


def enqueue(sid):
    qdir().mkdir(parents=True, exist_ok=True)
    (qdir() / sid).write_text(str(int(time.time())))


def pending():
    return sorted(p.name for p in qdir().glob("*")) if qdir().is_dir() else []


def failed():
    return sorted(p.name for p in fdir().glob("*")) if fdir().is_dir() else []


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, ValueError, TypeError):
        return False


def acquire_lock(wait_s=0):
    deadline = time.time() + wait_s
    while True:
        try:
            lockdir().mkdir(parents=True)
            (lockdir() / "pid").write_text(str(os.getpid()))
            return True
        except FileExistsError:
            try:
                owner = int((lockdir() / "pid").read_text())
            except (OSError, ValueError):
                owner = None
            if owner and not _pid_alive(owner):
                release_lock()          # steal from the dead
                continue
            if time.time() >= deadline:
                return False
            time.sleep(0.5)


def release_lock():
    try:
        (lockdir() / "pid").unlink(missing_ok=True)
        lockdir().rmdir()
    except OSError:
        pass


def set_processing(sid, phase="extracting"):
    statef().parent.mkdir(parents=True, exist_ok=True)
    statef().write_text(json.dumps(
        {"pid": os.getpid(), "phase": phase, "current": sid,
         "started": int(time.time())}))


def clear_processing():
    statef().unlink(missing_ok=True)


def processing():
    """Live worker state: {"phase": "extracting"|"synthesizing", "current": sid|None}."""
    try:
        st = json.loads(statef().read_text())
        if _pid_alive(st.get("pid")):
            return {"phase": st.get("phase", "extracting"), "current": st.get("current")}
    except (OSError, ValueError, KeyError):
        pass
    return None


def _fmt_ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone() \
            .strftime("%b %-d %-I:%M %p")
    except (ValueError, AttributeError):
        return iso or "?"


def snapshot(days=30):
    """Operational snapshot: vault health -> analysis queue -> lens freshness."""
    sessions = {s["session_id"]: s for s in D.discover(days)}
    convdir = trajdir() / "conversations"
    pend, fail, proc = set(pending()), set(failed()), processing()
    cur_sid = proc["current"] if proc else None
    rows = []
    if convdir.is_dir():
        for p in sorted(convdir.glob("*.json"), key=lambda p: p.stat().st_mtime,
                        reverse=True):
            sid = p.stem
            if sid not in sessions:
                continue
            try:
                ext = json.loads(p.read_text()).get("extracted", {})
            except (OSError, ValueError):
                continue
            title = (ext.get("apparent_objectives") or
                     ext.get("projects_or_topics") or [""])[0]
            if not title:
                continue                    # omit rows without usable metadata
            rows.append({"sid": sid, "date": sessions[sid]["date"],
                         "title": title[:64],
                         "processed": _fmt_ts(datetime.fromtimestamp(
                             p.stat().st_mtime).astimezone().isoformat())})
    analyzed = {p.stem for p in convdir.glob("*.json")} if convdir.is_dir() else set()
    analyzed &= set(sessions)
    ana, ev_n = {}, None
    try:
        ana = json.loads((trajdir() / "analysis.json").read_text())
    except (OSError, ValueError):
        pass
    try:
        ev_n = len(json.loads((trajdir() / "evidence_index.json").read_text()))
    except (OSError, ValueError):
        pass
    stale = pend & set(sessions)
    if cur_sid in sessions:
        stale = stale | {cur_sid}
    n_pending = len(stale)
    return {"total": len(sessions), "analyzed": len(analyzed),
            "pending": len(pend & set(sessions)),
            "processing": cur_sid, "worker": proc,
            "failed": len(fail & set(sessions)), "recent": rows,
            "lens_updated": _fmt_ts(ana.get("generated_at", "")),
            "lens_sessions": ana.get("sessions_analyzed"),
            "evidence_records": ev_n,
            "newer_pending": n_pending}
