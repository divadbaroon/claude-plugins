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
            for sid in q:
                state.set_processing(sid)
                sess = _session_by_id(sid, days)
                qfile = state.qdir() / sid
                if sess is None:
                    log(f"worker: {sid[:8]} not found in vault window; dropping")
                    qfile.unlink(missing_ok=True)
                    continue
                try:
                    res, fails = X.extract_all([sess], ex, state.trajdir() / "conversations",
                                               log=lambda m: log("worker: " + m.strip()))
                    if fails:
                        raise RuntimeError(fails[0][1])
                    qfile.unlink(missing_ok=True)
                    handled += 1
                    did_work = True
                    new_sids.append(sid)
                except Exception as e:                   # noqa: BLE001
                    log(f"worker: {sid[:8]} failed: {e}")
                    secure_dir(state.fdir(), D.VAULT)
                    atomic_write_text(state.fdir() / sid, str(e), root=D.VAULT)
                    qfile.unlink(missing_ok=True)
                finally:
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
        GM.save(trajdir, goals, important)
    except Exception as e:                                   # noqa: BLE001
        log(f"worker: goal update failed (non-fatal): {e}")
