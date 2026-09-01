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
from .secure_io import atomic_write_json, atomic_write_text, secure_dir
from ..platform_compat import pid_alive


def trajdir():
    return D.VAULT / "trajectory"


def qdir():   return trajdir() / "queue"
def fdir():   return trajdir() / "failed"
def lockdir(): return trajdir() / "worker.lock"
def statef(): return trajdir() / "worker.state"


def enqueue(sid):
    secure_dir(qdir(), D.VAULT)
    atomic_write_text(qdir() / sid, str(int(time.time())), root=D.VAULT)


def pending():
    return sorted(p.name for p in qdir().glob("*")) if qdir().is_dir() else []


def failed():
    return sorted(p.name for p in fdir().glob("*")) if fdir().is_dir() else []


def _pid_alive(pid):
    return pid_alive(pid)


def acquire_lock(wait_s=0):
    deadline = time.time() + wait_s
    while True:
        try:
            secure_dir(trajdir(), D.VAULT)
            lockdir().mkdir(mode=0o700)
            os.chmod(lockdir(), 0o700)
            atomic_write_text(lockdir() / "pid", str(os.getpid()), root=D.VAULT)
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


def worker_active():
    """Is any analysis running, by whatever entry point?

    `processing` is written per conversation and cleared between phases; the
    lock is held for the whole run. Reporting only the former made a live
    analysis look idle whenever it was between two conversations.
    """
    try:
        owner = int((lockdir() / "pid").read_text())
    except (OSError, ValueError):
        return False
    return _pid_alive(owner)


def release_lock():
    try:
        (lockdir() / "pid").unlink(missing_ok=True)
        lockdir().rmdir()
    except OSError:
        pass


def set_processing(sid, phase="extracting", active=None):
    secure_dir(statef().parent, D.VAULT)
    atomic_write_json(statef(),
        {"pid": os.getpid(), "phase": phase, "current": sid,
         # Several conversations run at once; naming one of them and calling
         # it "now" understates what is happening by a factor of the pool.
         "active": list(active or ([sid] if sid else [])),
         "started": int(time.time())}, root=D.VAULT)


def clear_processing():
    statef().unlink(missing_ok=True)


def processing():
    """Live worker state: {"phase": "extracting"|"synthesizing", "current": sid|None}."""
    try:
        st = json.loads(statef().read_text())
        if _pid_alive(st.get("pid")):
            return {"phase": st.get("phase", "extracting"),
                    "current": st.get("current"),
                    "active": st.get("active") or []}
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
