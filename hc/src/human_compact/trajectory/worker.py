"""Background worker: drain the queue, extract only new/stale conversations,
then run ONE synthesis from cached representations and atomically replace the
lens. Exits when the queue is empty. Never races another worker (mkdir lock);
never prompts (uses stored provider config or exits)."""
import json
from pathlib import Path

from . import discover as D, extract as X, synthesize as S, state
from . import providers as P
from .secure_io import atomic_write_text, secure_dir


def _providers():
    cfg = {}
    try:
        cfg = json.loads((state.trajdir() / "config.json").read_text())
    except (OSError, ValueError):
        return None, None
    ek, sk = cfg.get("extract_provider"), cfg.get("synth_provider")
    if not ek:
        return None, None
    return P.make(ek, "extract"), P.make(sk or ek, "synthesize")


def _session_by_id(sid, days):
    for s in D.discover(days):
        if s["session_id"] == sid:
            return s
    return None


def load_cached_extractions(days):
    cutoff_sessions = {s["session_id"] for s in D.discover(days)}
    out = []
    convdir = state.trajdir() / "conversations"
    if not convdir.is_dir():
        return out
    for p in sorted(convdir.glob("*.json")):
        if p.stem not in cutoff_sessions:
            continue
        try:
            out.append(json.loads(p.read_text())["extracted"])
        except (OSError, ValueError, KeyError):
            continue
    return out


def corrections_text(trajdir: Path):
    try:
        c = json.loads((trajdir / "corrections.json").read_text())
    except (OSError, ValueError):
        return None
    bits = [f["raw"] for f in c.get("_freeform", []) if f.get("applied")]
    bits += [f"exclude topic: {t}" for t in c.get("_exclusions", [])]
    if c.get("objective", {}).get("edit"):
        bits.append("objective per user: " + c["objective"]["edit"])
    return "\n".join(bits) or None


def synthesize_from_cache(sy, days, log=print):
    trajdir = state.trajdir()
    ext = load_cached_extractions(days)
    if not ext:
        log("worker: no cached extractions; nothing to synthesize")
        return False
    sessions = D.discover(days)
    D.write_evidence_index(sessions, trajdir)
    S.synthesize(ext, sy, days, trajdir,
                 {"extract": "cache", "synthesize": sy.identity()},
                 corrections_text=corrections_text(trajdir))
    try:
        from . import graph_build as G
        ana = json.loads((trajdir / "analysis.json").read_text())
        G.build(ext, ana, trajdir)
    except Exception:                                    # noqa: BLE001
        pass
    return True


BACKLOG_WORKERS = 8      # a backlog is a batch; a single arrival is one call


def drain(days=30, log=print):
    from .secure_io import secure_existing_tree
    secure_existing_tree(state.trajdir(), D.VAULT)
    if not state.acquire_lock():
        log("worker: another worker holds the lock; exiting (queue intact)")
        return 0
    handled = 0
    try:
        ex, sy = _providers()
        if ex is None:
            log("worker: no provider config yet — run `hc refresh` once")
            return 0
        did_work = False
        new_sids = []
        while True:
            q = state.pending()
            if not q:
                break
            # One conversation arriving is one call; a backlog is a batch.
            # Extracting a backlog one at a time made catching up take as long
            # as the conversations took to happen.
            batch, missing = [], []
            for sid in q:
                sess = _session_by_id(sid, days)
                if sess is None:
                    missing.append(sid)
                else:
                    batch.append(sess)
            for sid in missing:
                log(f"worker: {sid[:8]} not found in vault window; dropping")
                (state.qdir() / sid).unlink(missing_ok=True)
            if not batch:
                continue
            state.set_processing(batch[0]["session_id"])
            try:
                _results, fails = X.extract_all(
                    batch, ex, state.trajdir() / "conversations",
                    log=lambda m: log("worker: " + m.strip()),
                    workers=min(BACKLOG_WORKERS, len(batch)))
                failed = dict(fails)
            except Exception as batch_error:             # noqa: BLE001
                # The batch call itself broke, not one conversation in it.
                # Every item in the batch is unfinished; recording each keeps
                # the queue draining instead of retrying the same crash.
                failed = {s["session_id"]: str(batch_error) for s in batch}
            for sess in batch:
                sid = sess["session_id"]
                qfile = state.qdir() / sid
                if sid in failed:
                    log(f"worker: {sid[:8]} failed: {failed[sid]}")
                    secure_dir(state.fdir(), D.VAULT)
                    atomic_write_text(state.fdir() / sid, str(failed[sid]),
                                      root=D.VAULT)
                    qfile.unlink(missing_ok=True)
                    continue
                qfile.unlink(missing_ok=True)
                handled += 1
                did_work = True
                new_sids.append(sid)
            state.clear_processing()
            if did_work or not state.pending():
                state.set_processing(None, phase="synthesizing")
                log("worker: synthesizing lens from cached representations…")
                synthesize_from_cache(sy, days, log=log)
                _update_goals(sy, days, new_sids, log)
                new_sids = []
                state.clear_processing()
                did_work = False
        return handled
    finally:
        state.clear_processing()
        state.release_lock()


def _update_goals(sy, days, new_sids, log):
    """Incremental: classify each newly analyzed conversation into the tree;
    full rebuild when no tree exists yet."""
    from . import goals as GM, goal_synth as GS
    trajdir = state.trajdir()
    goals, important = GM.load(trajdir)
    try:
        if not goals["goals"]:
            ext = load_cached_extractions(days)
            if not ext:
                return
            log("worker: inferring initial goal tree…")
            goals = GM.sanitize(GS.rebuild(sy, ext))
        else:
            convdir = trajdir / "conversations"
            for sid in new_sids:
                p = convdir / f"{sid}.json"
                if not p.is_file():
                    continue
                import json as _j
                e = _j.loads(p.read_text())["extracted"]
                resp = GS.classify(sy, goals, e)
                changes = GM.apply_ops(goals, important,
                                       resp.get("operations"), max_new_top_level=1)
                for ch in changes:
                    log("worker: goal " + ch)
        described = GS.backfill_descriptions(sy, trajdir, goals)
        if described:
            log(f"worker: described {len(described)} goals")
        attached = attach_project_dirs(trajdir, goals)
        if attached:
            log(f"worker: attached a project directory to {attached} goals")
        GM.save(trajdir, goals, important)
    except Exception as e:                                   # noqa: BLE001
        log(f"worker: goal update failed (non-fatal): {e}")


def attach_project_dirs(trajdir, goals):
    """Give each goal the directory its own turns were typed in.

    The evidence already records a working directory per turn, so CODE CONTEXT
    does not have to start empty and wait to be filled in by hand. Only a
    directory that still exists is attached, only to a goal with no sources of
    its own, so this never overwrites the user's choices.
    """
    from . import agent_exec as AE
    from pathlib import Path as _P
    changed = 0
    for goal in goals["goals"]:
        if goal.get("sources"):
            continue
        try:
            cwd = AE.goal_cwd(trajdir, goals, goal["id"])
        except Exception:                                    # noqa: BLE001
            cwd = None
        if cwd and _P(cwd).is_dir():
            goal["sources"] = [{"id": "s1", "type": "local", "label": cwd}]
            changed += 1
    return changed
