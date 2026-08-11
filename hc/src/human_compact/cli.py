"""hc-backup: onboard Vault for Claude Code conversation persistence."""
import argparse
import os
import shutil
import stat
import subprocess
import sys
from importlib import resources
from pathlib import Path

HOME = Path(os.environ.get("HC_HOME", Path.home()))
SKILLS_DIR = HOME / ".claude" / "skills" / "vault"
VAULT_BIN = HOME / ".claude-vault" / "bin"
ZSHRC = HOME / ".zshrc"
PATH_LINE = 'export PATH="$HOME/.claude-vault/bin:$PATH"'
ALWAYS_LINE = "export CLAUDE_VAULT=1"


def say(msg):
    print(f"  {msg}")


def make_exec(p: Path):
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def asset_root() -> Path:
    return Path(str(resources.files("human_compact").joinpath("assets")))


def zshrc_has(line: str) -> bool:
    return ZSHRC.exists() and line in ZSHRC.read_text()


def zshrc_append(line: str, label: str):
    if zshrc_has(line):
        say(f"{label}: already configured in ~/.zshrc")
        return
    with ZSHRC.open("a") as f:
        f.write(f"\n# human-compact ({label})\n{line}\n")
    say(f"{label}: added to ~/.zshrc")


def ask(question: str, default_yes=True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    ans = input(f"  {question} {suffix} ").strip().lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def install_plugin():
    src = asset_root() / "plugin"
    SKILLS_DIR.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, SKILLS_DIR, dirs_exist_ok=True)
    for s in (SKILLS_DIR / "scripts").glob("*.sh"):
        make_exec(s)
    say(f"plugin installed -> {SKILLS_DIR}")


def install_shim():
    VAULT_BIN.mkdir(parents=True, exist_ok=True)
    dest = VAULT_BIN / "claude"
    shutil.copy2(asset_root() / "shim" / "claude", dest)
    make_exec(dest)
    say(f"shim installed   -> {dest}")
    zshrc_append(PATH_LINE, "shim on PATH")


def run_backfill():
    script = SKILLS_DIR / "scripts" / "vault-backfill.sh"
    dry = subprocess.run(["bash", str(script), "--dry-run"],
                         capture_output=True, text=True)
    tail = (dry.stdout.strip().splitlines() or ["backfill: nothing found"])[-1]
    say(f"preview: {tail}")
    if "0 imported" in tail or "nothing found" in tail:
        say("nothing new to import")
        return
    if ask("Import these now?"):
        subprocess.run(["bash", str(script)])


def backup_main():
    ap = argparse.ArgumentParser(prog="hc-backup",
                                 description="Onboard Vault for Claude Code.")
    ap.add_argument("--retroactive", choices=["yes", "no"],
                    help="import existing conversations without prompting")
    ap.add_argument("--mode", choices=["all", "selective"],
                    help="capture mode without prompting")
    args = ap.parse_args()

    print("\nhuman-compact · Vault onboarding\n")

    if shutil.which("jq") is None:
        say("WARNING: jq not found (brew install jq) — Vault hooks need it")
    if not (HOME / ".claude").exists():
        say("WARNING: ~/.claude not found — is Claude Code installed?")

    install_plugin()
    install_shim()
    print()

    retro = args.retroactive == "yes" if args.retroactive \
        else ask("Import your existing Claude Code conversations retroactively?")
    if retro:
        run_backfill()
    print()

    if args.mode:
        mode = args.mode
    else:
        say("How should Vault capture future chats?")
        say("  1. All future chats (always on)")
        say("  2. Only when you ask (run: claude --vault)")
        mode = "all" if input("  Choose [1/2]: ").strip() == "1" else "selective"

    if mode == "all":
        zshrc_append(ALWAYS_LINE, "always-on capture")
    else:
        say("selective mode: start Vault sessions with  claude --vault")

    print()
    say("Done. Open a NEW terminal so PATH changes load, then verify:")
    say("  which claude   ->  ~/.claude-vault/bin/claude")
    say("Vaulted history lives in ~/.claude-vault/sessions/ (by date).")
    print()





def trajectory_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc trajectory",
        description="Infer your primary current goal from recent vaulted conversations.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--provider", choices=["ollama", "claude", "mock"])
    ap.add_argument("--synth-provider", choices=["ollama", "claude", "mock"])
    ap.add_argument("--model")
    ap.add_argument("--synth-model")
    ap.add_argument("--refresh", action="store_true", help="ignore extraction cache")
    ap.add_argument("--workers", type=int, default=4, help="parallel extraction workers (default 4)")
    ap.add_argument("--no-serve", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--browser", action="store_true", help="also open the legacy browser evidence view")
    ap.add_argument("--no-interact", action="store_true", help="print the lens and exit")
    ap.add_argument("--serve-only", action="store_true", help="skip analysis, open existing map")
    ap.add_argument("--port", type=int, default=7710)
    args = ap.parse_args(argv)

    from pathlib import Path
    import json as _json
    from .trajectory import discover as D, providers as P, extract as X
    from .trajectory import synthesize as S, serve as V, graph_build as G

    trajdir = D.VAULT / "trajectory"
    trajdir.mkdir(parents=True, exist_ok=True)
    cfgf = trajdir / "config.json"
    cfg = _json.loads(cfgf.read_text()) if cfgf.is_file() else {}

    if args.serve_only:
        V.run(trajdir, port=args.port)
        return

    CLAUDE_NOTICE = ("  NOTE: the claude provider sends conversation-derived digests\n"
                     "  (your user-turn excerpts and per-conversation summaries) to\n"
                     "  Anthropic's API through your own claude CLI.")

    def choose(stage, flag_value, prior):
        if flag_value:                       # explicit flag = explicit selection
            if flag_value == "claude":
                print(CLAUDE_NOTICE)
            return flag_value
        if prior:
            return prior
        print(f"\n  Choose {stage} provider:")
        print("    1. ollama — fully local; nothing leaves this machine")
        print("    2. claude — your Claude Code CLI; sends conversation-derived digests to Anthropic")
        pick = input("  [1/2]: ").strip()
        kind = "ollama" if pick != "2" else "claude"
        if kind == "claude":
            print(CLAUDE_NOTICE)
            if input("  Proceed with claude? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  Staying local: ollama."); kind = "ollama"
        return kind

    ex_kind = choose("extraction", args.provider, cfg.get("extract_provider"))
    sy_kind = args.synth_provider or cfg.get("synth_provider") or ex_kind
    if sy_kind == "claude" and ex_kind != "claude" and not args.synth_provider and not cfg.get("synth_provider"):
        sy_kind = ex_kind                    # never silently escalate off-device
    cfg.update({"extract_provider": ex_kind, "synth_provider": sy_kind})
    cfgf.write_text(_json.dumps(cfg, indent=1))

    ex = P.make(ex_kind, "extract", args.model)
    sy = P.make(sy_kind, "synthesize", args.synth_model)
    print(f"\n  providers: extraction={ex.identity()}  synthesis={sy.identity()}")

    print(f"  discovering vault sessions (last {args.days} days)…")
    sessions = D.discover(args.days)
    if not sessions:
        print("  no vaulted conversations in the window — run hc backup or have some chats first.")
        return
    print(f"  {len(sessions)} conversations found "
          f"({sum(1 for s in sessions if s['low_evidence'])} low-evidence, kept and downweighted)")
    D.write_evidence_index(sessions, trajdir)

    ext, failures = X.extract_all(sessions, ex, trajdir / "conversations",
                                  refresh=args.refresh, workers=args.workers)
    if not ext:
        print("  extraction produced nothing — check the provider and retry."); return
    print(f"  synthesizing across {len(ext)} conversations…")
    cor_text = None
    cfile = trajdir / "corrections.json"
    if cfile.is_file():
        try:
            _c = _json.loads(cfile.read_text())
            bits = [f["raw"] for f in _c.get("_freeform", []) if f.get("applied")]
            bits += [f"exclude topic: {t}" for t in _c.get("_exclusions", [])]
            if _c.get("objective", {}).get("edit"):
                bits.append("objective per user: " + _c["objective"]["edit"])
            cor_text = "\n".join(bits) or None
        except (ValueError, KeyError):
            pass
    ana = S.synthesize(ext, sy, args.days, trajdir,
                 {"extract": ex.identity(), "synthesize": sy.identity()},
                 corrections_text=cor_text)
    try:
        G.build(ext, ana, trajdir)
    except Exception:                                  # graph is optional now
        pass
    _lens_loop(trajdir, sy, args)


def _print_pending_notice(snap):
    n = snap["newer_pending"]
    if n:
        s = "s are" if n != 1 else " is"
        print(f"\n  \033[33m{n} newer conversation{s} still being analyzed.\033[0m"
              if __import__("sys").stdout.isatty() else
              f"\n  {n} newer conversation{s} still being analyzed.")


def status_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc status")
    ap.add_argument("--all", action="store_true", help="full per-conversation status")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv or [])
    from .trajectory import state
    SEP = "─" * 40
    snap = state.snapshot(args.days)
    print("\nVAULT STATUS")
    print(SEP)
    print(f"{snap['total']} conversations · {snap['analyzed']} analyzed · "
          f"{snap['pending']} pending · {snap['failed']} failed")
    w = snap["worker"]
    if w and w.get("phase") == "synthesizing":
        print("Worker: synthesizing lens")
    elif w and w.get("current"):
        print(f"Worker: extracting {w['current'][:8]}")
    else:
        print("Worker: idle")
    recent = snap["recent"] if args.all else snap["recent"][:5]
    if recent:
        print(SEP)
        print("RECENT" if not args.all else "ALL ANALYZED")
        for r in recent:
            print(f"✓ {r['date']}  {r['title']}")
    print(SEP)
    print("CONTEXT LENS")
    bits = [f"Updated {snap['lens_updated']}"]
    if snap["lens_sessions"]:
        bits.append(f"{snap['lens_sessions']} conversations")
    if snap["evidence_records"]:
        bits.append(f"{snap['evidence_records']} evidence turns")
    print(" · ".join(bits))
    if snap["newer_pending"]:
        n = snap["newer_pending"]
        used = snap["lens_sessions"] or snap["analyzed"]
        verb = "is" if n == 1 else "are"
        print(f"Current lens uses {used} / {snap['total']} conversations · "
              f"{n} newer conversation{'s' if n != 1 else ''} {verb} being analyzed.")
    else:
        print("✓ Current")
    print()


def lens_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc lens")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-interact", action="store_true")
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--global", dest="global_view", action="store_true",
                    help="show the full ranked objective stack")
    ap.add_argument("--port", type=int, default=7710)
    args = ap.parse_args(argv)
    from .trajectory import state, providers as Pr
    import json as _j
    try:
        cfg = _j.loads((state.trajdir() / "config.json").read_text())
        provider = Pr.make(cfg.get("synth_provider") or cfg.get("extract_provider"), "synthesize")
    except (OSError, ValueError, KeyError):
        provider = None
    _lens_loop(state.trajdir(), provider, args)
    _print_pending_notice(state.snapshot(args.days))


def refresh_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc refresh",
        description="Process all pending/stale conversations and regenerate the lens.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)
    from .trajectory import state
    if not state.acquire_lock(wait_s=60):
        print("  a worker is active and did not finish within 60s; try again")
        return
    try:
        trajectory_main(["--days", str(args.days), "--workers", str(args.workers),
                         "--no-interact"])
    finally:
        state.release_lock()
    status_main()


def worker_main(argv=None):
    from .trajectory import worker
    worker.drain(log=print)


def goals_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc goals",
        description="Your goal tree, inferred from your work. Correct it in plain language.")
    ap.add_argument("--rebuild", action="store_true", help="re-infer the full tree")
    ap.add_argument("--all", dest="show_all", action="store_true",
                    help="include completed/abandoned")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-interact", action="store_true")
    args = ap.parse_args(argv or [])
    import json as _j
    from .trajectory import goals as GM, goal_synth as GS, state, worker as W
    from .trajectory import providers as Pr, lens as L
    trajdir = state.trajdir(); trajdir.mkdir(parents=True, exist_ok=True)
    try:
        cfg = _j.loads((trajdir / "config.json").read_text())
        provider = Pr.make(cfg.get("synth_provider") or cfg["extract_provider"], "synthesize")
    except (OSError, ValueError, KeyError):
        print("  no provider configured yet — run `hc refresh` once"); return
    goals, important = GM.load(trajdir)
    if args.rebuild or not goals["goals"]:
        ext = W.load_cached_extractions(args.days)
        if not ext:
            print("  no analyzed conversations yet — run `hc refresh` first"); return
        print(f"  inferring goal tree from {len(ext)} conversations…")
        goals = GM.sanitize(GS.rebuild(provider, ext,
                    corrections_text=_goal_corrections_text(trajdir)))
        GM.save(trajdir, goals, important)
    _, _, idx = L.load(trajdir)
    itemmap = GM.render(goals, important, show_all=args.show_all)
    if args.no_interact:
        return
    def read(prompt):
        try:
            return input(prompt)
        except EOFError:
            raise SystemExit(0)
    while True:
        try:
            cmd = read("> ").strip().lower()
        except (KeyboardInterrupt, SystemExit):
            print(); return
        if cmd in ("q", "quit", ""):
            return
        if cmd in ("c", "correct"):
            _goal_nl_flow(trajdir, provider, read)
            goals, important = GM.load(trajdir)
            itemmap = GM.render(goals, important, show_all=args.show_all)
        elif cmd in ("m", "mark"):
            text = read("  what matters? paste or type it: ").strip()
            if text:
                why = read("  why (optional): ").strip() or None
                iid, gid = GM.mark_important(trajdir, goals, important, text, why=why)
                GM.save(trajdir, goals, important)
                tgt = GM.by_id(goals, gid)
                print(("  ★ saved under " + tgt["title"][:40]) if tgt
                      else "  ★ saved (no goal association yet)")
                goals, important = GM.load(trajdir)
                itemmap = GM.render(goals, important, show_all=args.show_all)
        elif cmd in ("e", "evidence"):
            pick = read("  evidence for which item number? ").strip()
            try:
                entry = itemmap[int(pick) - 1]
            except (ValueError, IndexError):
                print("  no such item"); continue
            obj = entry["obj"]
            ids = (obj.get("evidence_ids") or [])[:4] or                   ([obj["turn_id"]] if obj.get("turn_id") else [])
            shown = 0
            for i in ids:
                v = idx.get(i)
                if v:
                    shown += 1
                    print(f"  {v['date']} · {v['session_id'][:8]} · “{v['text'][:140]}”")
            if not shown:
                print("  no directly resolvable turns")
        else:
            print("  C correct · M mark important · E evidence · Q quit")


def _goal_corrections_text(trajdir):
    import json as _j
    try:
        c = _j.loads((trajdir / "corrections.json").read_text())
    except (OSError, ValueError):
        return None
    return "\n".join(f["raw"] for f in c.get("_goal_freeform", [])
                     if f.get("applied")) or None


def _goal_nl_flow(trajdir, provider, read):
    import json as _j
    from datetime import datetime, timezone
    from .trajectory import goals as GM, goal_synth as GS
    goals, important = GM.load(trajdir)
    raw = read("  What did I get wrong, or what should matter more?\n  > ").strip()
    if not raw:
        return
    try:
        resp = GS.translate_nl(provider, goals, important, raw)
    except Exception as e:                                   # noqa: BLE001
        print(f"  could not interpret: {e}"); return
    interp, ops = resp.get("interpretation") or [], resp.get("operations") or []
    print()
    print("  I understood that as:")
    for b in interp:
        print(f"   • {b}")
    if not ops:
        print("  (no actionable operations)")
    applied = False
    if ops:
        yn = read("  Apply? [Y/n] ").strip().lower()
        applied = yn in ("", "y", "yes")
    entry = {"raw": raw, "interpretation": interp, "operations": ops,
             "applied": applied,
             "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    p = trajdir / "corrections.json"
    cur = {}
    try:
        cur = _j.loads(p.read_text())
    except (OSError, ValueError):
        pass
    cur.setdefault("_goal_freeform", []).append(entry)
    p.write_text(_j.dumps(cur, indent=1))
    if applied:
        # mark_important ops need the important store; route them
        plain = [o for o in ops if o.get("op") != "mark_important"]
        for o in ops:
            if o.get("op") == "mark_important":
                GM.mark_important(trajdir, goals, important, o.get("text", ""),
                                  why=o.get("why"), goal_id=o.get("goal_id"))
        changes = GM.apply_ops(goals, important, plain, max_new_top_level=0)
        GM.save(trajdir, goals, important)
        for ch in changes:
            print("   · " + ch)
        print("  applied — tree updated.")
    else:
        print("  saved but not applied.")


def mark_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc mark",
        description="Mark something as important — it will not be lost.")
    ap.add_argument("--text", help="the important text (skips the picker)")
    ap.add_argument("--why")
    args = ap.parse_args(argv or [])
    from .trajectory import goals as GM, state, discover as D
    trajdir = state.trajdir()
    goals, important = GM.load(trajdir)
    text, sid, tid = args.text, None, None
    if not text:
        sessions = sorted(D.discover(30), key=lambda s: s["date"])[-1:]
        if sessions:
            s = sessions[0]; sid = s["session_id"]
            print(f"\n  recent turns from {sid[:8]} ({s['date']}):")
            tail = s["turns"][-8:]
            for i, t in enumerate(tail, 1):
                print(f"  [{i}] {t['role'][:4]}: {t['text'][:88]}")
            pick = input("  which turn (or paste text instead)? ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(tail):
                text, tid = tail[int(pick) - 1]["text"], tail[int(pick) - 1]["id"]
            else:
                text = pick
    if not text:
        print("  nothing to mark"); return
    iid, gid = GM.mark_important(trajdir, goals, important, text,
                                 session_id=sid, turn_id=tid, why=args.why)
    GM.save(trajdir, goals, important)
    tgt = GM.by_id(goals, gid)
    print(("  ★ saved under: " + tgt["title"]) if tgt
          else "  ★ saved (no goal association yet — `hc goals` → C to place it)")


def hc_main():
    import sys
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: hc <command>\n\n"
              "  goals       your goal tree + important items (primary)\n"
              "  mark        mark something important — never lose it\n"
              "  lens        the derived compaction lens\n"
              "  status      vault + analysis pipeline status\n"
              "  refresh     process pending conversations, regenerate lens\n"
              "  backup      onboard Vault / import history\n"
              "  trajectory  full analyze + lens (alias)\n")
        return
    cmd, rest = args[0], args[1:]
    if cmd == "backup":
        sys.argv = ["hc-backup"] + rest
        backup_main()
    elif cmd == "trajectory":
        trajectory_main(rest)
    elif cmd == "lens":
        lens_main(rest)
    elif cmd == "goals":
        goals_main(rest)
    elif cmd == "mark":
        mark_main(rest)
    elif cmd == "status":
        status_main(rest)
    elif cmd == "refresh":
        refresh_main(rest)
    elif cmd == "worker":
        worker_main(rest)
    else:
        print(f"unknown command: {cmd}"); sys.exit(2)


def main():   # keep hc-backup entry point working
    backup_main()


if __name__ == "__main__":
    hc_main()


def _lens_loop(trajdir, provider, args):
    from .trajectory import lens as L
    show_global = getattr(args, "global_view", False)
    analysis, corrections, idx = L.load(trajdir)
    itemmap, obj = L.render(analysis, corrections, trajdir, show_global=show_global)
    if show_global:
        return
    if args.browser:
        import threading
        from .trajectory import serve as V
        threading.Thread(target=V.run, args=(trajdir,),
                         kwargs={"port": args.port}, daemon=True).start()
    if args.no_interact:
        return
    def read(prompt):
        try:
            return input(prompt)
        except EOFError:
            raise SystemExit(0)
    while True:
        try:
            cmd = read("> ").strip().lower()
        except (KeyboardInterrupt, SystemExit):
            print(); return
        if cmd in ("q", "quit", "exit", ""):
            return
        if cmd in ("t", "test", "test lens"):
            L.test_flow(analysis, corrections, trajdir, provider, args.days, read)
        elif cmd in ("e", "evidence"):
            pick = read("  evidence for which? item number or 'o': ").strip().lower()
            L.show_evidence(itemmap, obj, idx, pick)
        elif cmd in ("c", "correct"):
            handled = L.nl_correct_flow(analysis, corrections, trajdir, provider, read)
            if not handled:
                L.correct_flow(itemmap, trajdir, read)
            analysis, corrections, idx = L.load(trajdir)
            itemmap, obj = L.render(analysis, corrections, trajdir, show_global=show_global)
        else:
            print("  T = test lens · E = evidence · C = correct · Q = quit")
