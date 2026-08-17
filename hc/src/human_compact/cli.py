"""hc-backup: onboard Vault for Claude Code conversation persistence."""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from importlib import resources
from pathlib import Path

HOME = Path(os.environ.get("HC_HOME", Path.home()))
SKILLS_DIR = HOME / ".claude" / "skills" / "vault"
GOALS_UI_SKILL_DIR = HOME / ".claude" / "skills" / "goals-ui"
# Pre-rename install path. Retired by install_plugin() when it is ours.
LEGACY_HC_UI_SKILL_DIR = HOME / ".claude" / "skills" / "hc-ui"
VAULT_BIN = HOME / ".claude-vault" / "bin"
ZSHRC = HOME / ".zshrc"
PATH_LINE = 'export PATH="$HOME/.claude-vault/bin:$PATH"'
ALWAYS_LINE = "export CLAUDE_VAULT=1"
_DETACHED_PROCESSES = []
MIN_CLAUDE_VERSION = (2, 1, 175)
MIN_CLAUDE_VERSION_TEXT = ".".join(map(str, MIN_CLAUDE_VERSION))
MANAGED_MARKER = ".human-compact-managed.json"
_ASSET_FILES = {
    "vault": {
        ".claude-plugin/plugin.json", "README.md", "hooks/hooks.json",
        "scripts/chat-hook.sh", "scripts/vault-backfill.sh",
        "scripts/vault-hook.sh",
    },
    "goals-ui": {"SKILL.md"},
}
# Exact unmarked v0.15.0 assets. This permits migration of installs created by
# this project before ownership markers existed without claiming arbitrary
# directories that merely happen to use the same names.
_LEGACY_DIGESTS = {
    "vault": {"4f5319b78efe7f90eccb967bbcd787b7ddcfbfdae8643e82281f01e6551dda02"},
    # This digest is the v0.15.0 /hc-ui SKILL.md, which the rename
    # superseded; it now only identifies a legacy ~/.claude/skills/hc-ui.
    "goals-ui": {"6ddef8b28e8df3dec16591f7658199158fd97cc02e85b854bbbd79739f398815"},
}


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


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _asset_digest(root: Path, asset: str):
    """Return an exact tree digest, or None for any unexpected path/layout."""
    expected_files = _ASSET_FILES[asset]
    if not root.is_dir() or root.is_symlink():
        return None
    actual_files, actual_dirs = set(), set()
    try:
        for path in root.rglob("*"):
            if path.is_symlink():
                return None
            relative = path.relative_to(root).as_posix()
            if path.is_dir():
                actual_dirs.add(relative)
            elif path.is_file():
                actual_files.add(relative)
            else:
                return None
    except OSError:
        return None
    expected_dirs = {
        parent.as_posix()
        for name in expected_files
        for parent in Path(name).parents
        if parent.as_posix() != "."
    }
    if actual_files != expected_files or actual_dirs != expected_dirs:
        return None
    digest = hashlib.sha256()
    try:
        for name in sorted(expected_files):
            digest.update(name.encode() + b"\0")
            digest.update((root / name).read_bytes())
            digest.update(b"\0")
    except OSError:
        return None
    return digest.hexdigest()


def _owned_asset(destination: Path, asset: str) -> bool:
    marker = destination / MANAGED_MARKER
    if marker.is_symlink():
        return False
    try:
        value = json.loads(marker.read_text())
    except (OSError, ValueError):
        return False
    return (value.get("owner") == "human-compact" and
            value.get("asset") == asset and value.get("format") == 1)


def _legacy_asset(destination: Path, source: Path, asset: str) -> bool:
    digest = _asset_digest(destination, asset)
    if digest is None:
        return False
    return digest == _asset_digest(source, asset) or digest in _LEGACY_DIGESTS[asset]


def _preflight_asset(source: Path, destination: Path, asset: str):
    if not _path_exists(destination):
        return "new"
    if destination.is_symlink() or not destination.is_dir():
        raise RuntimeError(
            f"refusing to replace unmanaged Claude skill path: {destination}; "
            "move it aside, then rerun npx human-vault")
    if _owned_asset(destination, asset):
        return "managed"
    if _legacy_asset(destination, source, asset):
        return "legacy"
    raise RuntimeError(
        f"refusing to replace unmanaged Claude skill directory: {destination}; "
        "move it aside, then rerun npx human-vault")


def _tighten_asset_modes(root: Path):
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"refusing symlink in packaged Claude asset: {path}")
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.is_file():
            executable = (path.suffix == ".sh" or
                          bool(path.stat().st_mode & stat.S_IXUSR))
            os.chmod(path, 0o700 if executable else 0o600)


def _stage_asset(source: Path, destination: Path, asset: str) -> Path:
    stage = destination.parent / f".{destination.name}.hc-stage-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage, symlinks=True)
        marker = stage / MANAGED_MARKER
        marker.write_text(json.dumps({
            "owner": "human-compact", "asset": asset, "format": 1,
        }, sort_keys=True) + "\n")
        _tighten_asset_modes(stage)
        if not _owned_asset(stage, asset):
            raise RuntimeError(f"staged {asset} ownership marker is invalid")
        # The marker is intentionally the only difference from packaged data.
        marker.unlink()
        source_digest = _asset_digest(source, asset)
        valid = (source_digest is not None and
                 _asset_digest(stage, asset) == source_digest)
        marker.write_text(json.dumps({
            "owner": "human-compact", "asset": asset, "format": 1,
        }, sort_keys=True) + "\n")
        os.chmod(marker, 0o600)
        if not valid:
            raise RuntimeError(f"staged {asset} failed package validation")
        return stage
    except Exception:
        if _path_exists(stage):
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _remove_asset(path: Path):
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _retire_legacy_hc_ui():
    """Remove the pre-rename /hc-ui skill, but only when we installed it."""
    path = LEGACY_HC_UI_SKILL_DIR
    if not _path_exists(path):
        return
    ours = (not path.is_symlink() and path.is_dir()
            and (_owned_asset(path, "hc-ui")
                 or _asset_digest(path, "goals-ui") in _LEGACY_DIGESTS["goals-ui"]))
    if not ours:
        say(f"left unmanaged {path} in place")
        return
    try:
        _remove_asset(path)
    except OSError as exc:
        say(f"could not remove superseded {path}: {exc}")
        return
    say(f"removed superseded {path}")


def install_plugin():
    parent = SKILLS_DIR.parent
    parent.mkdir(parents=True, exist_ok=True)
    specs = [
        {"asset": "vault", "source": asset_root() / "plugin",
         "destination": SKILLS_DIR},
        {"asset": "goals-ui", "source": asset_root() / "goals-ui-skill",
         "destination": GOALS_UI_SKILL_DIR},
    ]
    for spec in specs:
        spec["ownership"] = _preflight_asset(
            spec["source"], spec["destination"], spec["asset"])
    try:
        for spec in specs:
            spec["stage"] = _stage_asset(
                spec["source"], spec["destination"], spec["asset"])
            spec["backup"] = spec["destination"].parent / (
                f".{spec['destination'].name}.hc-backup-{uuid.uuid4().hex}")
            spec["backup_moved"] = False
            spec["promoted"] = False
    except Exception:
        for spec in specs:
            if spec.get("stage"):
                _remove_asset(spec["stage"])
        raise

    try:
        for spec in specs:
            destination = spec["destination"]
            if _path_exists(destination):
                os.replace(destination, spec["backup"])
                spec["backup_moved"] = True
            os.replace(spec["stage"], destination)
            spec["promoted"] = True
    except Exception as install_error:
        rollback_errors = []
        for spec in reversed(specs):
            try:
                if spec["promoted"] and _path_exists(spec["destination"]):
                    _remove_asset(spec["destination"])
                if spec["backup_moved"] and _path_exists(spec["backup"]):
                    os.replace(spec["backup"], spec["destination"])
            except Exception as rollback_error:  # noqa: BLE001
                rollback_errors.append(
                    f"{spec['destination']}: {rollback_error}; backup={spec['backup']}")
            if _path_exists(spec["stage"]):
                _remove_asset(spec["stage"])
        detail = ("; rollback incomplete: " + "; ".join(rollback_errors)
                  if rollback_errors else "; previous install restored")
        raise RuntimeError(f"Claude integration install failed: {install_error}{detail}") \
            from install_error

    for spec in specs:
        if spec["backup_moved"] and _path_exists(spec["backup"]):
            _remove_asset(spec["backup"])
        if spec["ownership"] == "legacy":
            say(f"migrated legacy {spec['asset']} install")
    say(f"plugin installed -> {SKILLS_DIR}")
    say(f"/goals-ui installed -> {GOALS_UI_SKILL_DIR}")
    # Only after promotion: a stale /hc-ui skill would otherwise still claim a
    # workspace URL that nothing supplies.
    _retire_legacy_hc_ui()


def install_main(argv=None):
    """Install the chat-scoped UI without enabling the global Vault layer."""
    ap = argparse.ArgumentParser(
        prog="hc install",
        description="Install /goals-ui for Claude Code (no global context layer).")
    ap.parse_args(argv or [])
    print("\nhuman-compact · chat goal UI\n")
    if not (HOME / ".claude").exists():
        say("WARNING: ~/.claude not found — install Claude Code first")
    install_plugin()
    print()
    say("Done. Start a new Claude Code session (or run /reload-plugins),")
    say("then type /goals-ui in any chat.")
    print()


def install_shim():
    VAULT_BIN.mkdir(parents=True, exist_ok=True)
    dest = VAULT_BIN / "claude"
    shutil.copy2(asset_root() / "shim" / "claude", dest)
    make_exec(dest)
    say(f"shim installed   -> {dest}")
    zshrc_append(PATH_LINE, "shim on PATH")


def run_backfill(assume_yes=False):
    script = SKILLS_DIR / "scripts" / "vault-backfill.sh"
    dry = subprocess.run(["bash", str(script), "--dry-run"],
                         capture_output=True, text=True)
    if dry.returncode != 0:
        raise RuntimeError(dry.stderr.strip() or dry.stdout.strip() or
                           "Vault backfill preview failed")
    tail = (dry.stdout.strip().splitlines() or ["backfill: nothing found"])[-1]
    say(f"preview: {tail}")
    if "0 imported" in tail or "nothing found" in tail:
        say("nothing new to import")
        return
    if assume_yes or ask("Import these now?"):
        subprocess.run(["bash", str(script)], check=True)


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
        run_backfill(assume_yes=args.retroactive == "yes")
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
        # New installs use this shell-independent sentinel. Keep the zshrc
        # export above so existing pipx/selective workflows remain unchanged.
        from . import global_vault
        global_vault.enable_always_on()
    else:
        say("selective mode: start Vault sessions with  claude --vault")

    print()
    say("Done. Open a NEW terminal so PATH changes load, then verify:")
    say("  which claude   ->  ~/.claude-vault/bin/claude")
    say("Vaulted history lives in ~/.claude-vault/sessions/ (by date).")
    print()


def _validate_claude_cli():
    import re
    executable = shutil.which("claude")
    if not executable:
        raise RuntimeError(
            "Claude Code is required; install it, then rerun npx human-vault")
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Claude Code validation failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:200]
        raise RuntimeError("Claude Code validation failed" +
                           (f": {detail}" if detail else ""))
    raw_version = (result.stdout or "").strip() or (result.stderr or "").strip()
    match = re.fullmatch(
        r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?"
        r"(?:\+[0-9A-Za-z.-]+)?\s+\(Claude Code\)",
        raw_version,
    )
    if not match:
        raise RuntimeError(
            f"unsupported Claude Code version output {raw_version!r}; "
            f"human-compact requires Claude Code {MIN_CLAUDE_VERSION_TEXT} or newer")
    installed = tuple(int(part) for part in match.groups())
    if installed < MIN_CLAUDE_VERSION:
        installed_text = ".".join(map(str, installed))
        raise RuntimeError(
            f"Claude Code {installed_text} is too old; human-compact requires "
            f"Claude Code {MIN_CLAUDE_VERSION_TEXT} or newer")
    return executable


def setup_main(argv=None):
    """One noninteractive orchestration seam for the npm installer."""
    ap = argparse.ArgumentParser(
        prog="hc setup",
        description="Install /goals-ui and optionally initialize global Vault state.")
    ap.add_argument("--global-vault", required=True,
                    choices=["yes", "no", "keep"])
    ap.add_argument("--goals", required=True, choices=["yes", "no"])
    args = ap.parse_args(argv or [])
    if args.goals == "yes" and args.global_vault != "yes":
        ap.error("--goals yes requires --global-vault yes")

    # This is deliberately first: /goals-ui remains installed even when optional
    # global-history setup fails later and the user retries the installer.
    install_main([])
    if args.global_vault == "keep":
        # The installer has no opinion: onboarding happens in the UI. Saying
        # "no" here would silently stop capturing a vault the user already
        # turned on, and they would lose history without being told.
        say("global Vault unchanged by this install")
        _validate_claude_cli()
        return
    if args.global_vault == "no":
        from . import global_vault
        global_vault.disable_always_on()
    _validate_claude_cli()
    if args.global_vault == "no":
        return

    from . import global_vault
    counts = global_vault.backfill()
    config = global_vault.enable_always_on()
    say("global Vault enabled -> " + str(config))
    say(f"history import: {counts['imported']} imported, "
        f"{counts['skipped']} already present")

    if args.goals == "yes":
        trajectory_main([
            "--provider", "claude", "--synth-provider", "claude",
            "--refresh", "--no-interact", "--strict",
        ])
        goals_main(["--rebuild", "--no-interact"])
        if not (global_vault.vault_root() / "trajectory" / "goals.json").is_file():
            raise RuntimeError("goal inference completed without writing goals.json")
        say("global goal tree rebuilt")


def global_hook_main(argv=None):
    """Python lifecycle hook used by npm-managed and pipx runtimes."""
    ap = argparse.ArgumentParser(prog="hc global-hook")
    ap.parse_args(argv or [])
    import json
    from . import global_vault
    try:
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if isinstance(payload, dict):
        try:
            global_vault.handle_hook(payload)
        except Exception as exc:  # noqa: BLE001 - Claude hooks are fail-open
            try:
                global_vault._debug(  # noqa: SLF001 - same hook layer
                    str(payload.get("hook_event_name") or "?"),
                    f"Python hook failed: {exc}")
            except Exception:  # noqa: BLE001 - debug logging is best-effort
                pass
    return 0





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
    ap.add_argument("--strict", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--workers", type=int, default=8, help="parallel extraction workers (default 8)")
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
    from .trajectory import secure_io as IO

    trajdir = D.VAULT / "trajectory"
    IO.secure_existing_tree(trajdir, D.VAULT)
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
    IO.atomic_write_json(cfgf, cfg, root=D.VAULT)

    ex = P.make(ex_kind, "extract", args.model)
    sy = P.make(sy_kind, "synthesize", args.synth_model)
    print(f"\n  providers: extraction={ex.identity()}  synthesis={sy.identity()}")

    print(f"  discovering vault sessions (last {args.days} days)…")
    sessions = D.discover(args.days)
    if not sessions:
        message = "no vaulted conversations in the window — run hc backup or have some chats first"
        if args.strict:
            raise RuntimeError(message)
        print("  " + message + ".")
        return
    print(f"  {len(sessions)} conversations found "
          f"({sum(1 for s in sessions if s['low_evidence'])} low-evidence, kept and downweighted)")
    D.write_evidence_index(sessions, trajdir)

    ext, failures = X.extract_all(sessions, ex, trajdir / "conversations",
                                  refresh=args.refresh, workers=args.workers)
    if failures and args.strict:
        detail = "; ".join(f"{sid[:8]}: {reason}" for sid, reason in failures[:3])
        raise RuntimeError(
            f"{len(failures)} conversation extraction(s) failed: {detail}")
    if not ext:
        message = "extraction produced nothing — check the provider and retry"
        if args.strict:
            raise RuntimeError(message)
        print("  " + message + "."); return
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
    ap.add_argument("--workers", type=int, default=8)
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


def analyze_main(argv=None):
    """Everything the UI's analysis button promises, in one command.

    `refresh` extracts conversations and rebuilds the lens; it has never built
    the goal tree. Spawning it alone left a vault with 91 analyzed
    conversations and no goals — indistinguishable, on screen, from an
    analysis that silently failed.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="hc analyze",
        description="Analyze the vaulted history and build the goal tree.")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv or [])
    from .trajectory import state as ST
    refresh_main(["--days", str(args.days), "--workers", str(args.workers)])
    say("building your goal tree…")
    # Keep the UI's banner up for this phase: the tree is the part the user is
    # waiting for, and it is the longest silence in the whole run.
    ST.set_processing(None, phase="synthesizing")
    try:
        goals_main(["--rebuild", "--days", str(args.days), "--no-interact"])
    finally:
        ST.clear_processing()


def worker_main(argv=None):
    from .trajectory import worker
    worker.drain(log=print)


def goals_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc goals",
        description="Your goal tree, inferred from your work. Correct it in plain language.")
    ap.add_argument("--rebuild", action="store_true", help="re-infer the full tree")
    ap.add_argument("--describe", action="store_true",
                    help="fill in missing goal descriptions, changing nothing else")
    ap.add_argument("--redescribe", metavar="IDS",
                    help="clear these goals' descriptions first (comma-separated)")
    ap.add_argument("--all", dest="show_all", action="store_true",
                    help="include completed/abandoned")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-interact", action="store_true")
    args = ap.parse_args(argv or [])
    import json as _j
    from .trajectory import goals as GM, goal_synth as GS, state, worker as W
    from .trajectory import providers as Pr, lens as L
    from .trajectory import secure_io as IO
    trajdir = state.trajdir(); IO.secure_existing_tree(trajdir, trajdir.parent)
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
        try:
            described = GS.backfill_descriptions(provider, trajdir, goals)
            if described:
                print(f"  described {len(described)} goals from their evidence")
        except Exception as exc:                             # noqa: BLE001
            print(f"  descriptions skipped (non-fatal): {exc}")
        if W.attach_project_dirs(trajdir, goals):
            print("  attached each goal's project directory")
        GM.save(trajdir, goals, important)
    if args.describe or args.redescribe:
        if args.redescribe:
            wanted = {gid.strip() for gid in args.redescribe.split(",") if gid.strip()}
            for g in goals["goals"]:
                if g["id"] in wanted:
                    g["description"] = ""
        blanks = [g for g in goals["goals"]
                  if not str(g.get("description") or "").strip()]
        if not blanks:
            print("  every goal already has a description")
        else:
            print(f"  describing {len(blanks)} goals from their own evidence…")
            try:
                idx = _j.loads((trajdir / "evidence_index.json").read_text())
            except (OSError, ValueError):
                idx = {}
            written = GS.describe(provider, goals, idx)
            for g in goals["goals"]:
                if g["id"] in written:
                    g["description"] = written[g["id"]]
                    g["updated_at"] = GM._now()
            GM.save(trajdir, goals, important)
            for gid, text in written.items():
                print(f"    {gid}: {text[:72]}")
            missing = [g["id"] for g in blanks if g["id"] not in written]
            if missing:
                print(f"  left empty (evidence did not support one): "
                      f"{', '.join(missing)}")
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
    from .trajectory.secure_io import atomic_write_json
    atomic_write_json(p, cur, root=trajdir.parent)
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


def ui_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hc ui",
        description="Open the local goal-state browser.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-replace", action="store_true",
                    help="leave any server already running in place")
    args = ap.parse_args(argv or [])
    from .trajectory import ui
    ui.run(port=args.port, open_browser=not args.no_open,
           replace=not args.no_replace)


def _resolve_work_goal(goals, wanted):
    """Accept a goal id, or an unambiguous case-insensitive title fragment."""
    from .trajectory import goals as GM
    exact = GM.by_id(goals, wanted)
    if exact:
        return exact, []
    needle = wanted.strip().lower()
    matches = [g for g in goals["goals"] if needle in g.get("title", "").lower()]
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def work_main(argv=None):
    """Start Claude on one Vault goal, with that goal bound to the session."""
    import argparse
    ap = argparse.ArgumentParser(prog="hc work",
        description="Launch Claude Code to work on a Vault goal or subgoal.")
    ap.add_argument("goal", nargs="?", help="goal id (g3) or title fragment")
    ap.add_argument("--list", action="store_true",
                    help="list goals you can work on")
    ap.add_argument("--print-context", action="store_true",
                    help="print the goal briefing this session would receive")
    ap.add_argument("--dry-run", action="store_true",
                    help="bind nothing and print the launch command")
    ap.add_argument("--start", action="store_true",
                    help="open the session on the goal instead of an empty prompt")
    ap.epilog = "anything after `--` is passed straight through to claude"
    # argparse.REMAINDER would swallow `hc work g2 --dry-run`, so split on the
    # explicit passthrough marker instead.
    argv = list(argv or [])
    passthrough = []
    if "--" in argv:
        marker = argv.index("--")
        argv, passthrough = argv[:marker], argv[marker + 1:]
    args = ap.parse_args(argv)
    from .trajectory import agent_exec as AE, goals as GM, state
    trajdir = state.trajdir()
    goals, _ = GM.load(trajdir)
    GM.sanitize(goals)

    if args.list or not args.goal:
        active = [g for g in goals["goals"] if g["status"] in ("active", "in_progress")]
        if not active:
            print("no active goals yet — run `hc goals` or `hc ui` first")
            return 0
        print("\n  goals you can work on:\n")
        for g in active:
            parent = GM.by_id(goals, g.get("parent_goal_id") or "")
            trail = f"  ({parent['title'][:34]})" if parent else ""
            print(f"  {g['id']:>4}  {g['title'][:56]}{trail}")
        print("\n  hc work <id>\n")
        return 0

    goal, near = _resolve_work_goal(goals, args.goal)
    if goal is None:
        if near:
            print(f"'{args.goal}' matches {len(near)} goals:")
            for g in near[:8]:
                print(f"  {g['id']:>4}  {g['title'][:60]}")
        else:
            print(f"no goal matches '{args.goal}' — try `hc work --list`")
        return 2

    if args.print_context:
        print(AE.goal_context(trajdir, goals, goal["id"]))
        return 0

    directories, _references = AE.goal_sources(goals, goal["id"])
    for directory in directories:
        passthrough = passthrough + ["--add-dir", directory]
    if args.start:
        opening = AE.launch_prompt(goals, goal["id"])
        if opening:
            # First, not last: `--add-dir` takes a variadic list, so a prompt
            # placed after it is swallowed as one more directory and the
            # session opens with nothing to work on.
            passthrough = [opening] + passthrough
    claude = os.environ.get("HC_CLAUDE_BIN") or shutil.which("claude")
    if args.dry_run or not claude:
        if not claude and not args.dry_run:
            print("could not find the `claude` CLI on PATH "
                  "(set HC_CLAUDE_BIN to override)", file=sys.stderr)
        print(f"{AE.GOAL_ENV}={goal['id']} "
              f"{claude or 'claude'} {' '.join(passthrough)}".strip())
        return 0 if args.dry_run else 1

    print(f"\n  working on {goal['id']}: {goal['title'][:60]}")
    print("  Claude's execution plan will appear on this goal in `hc ui`\n",
          flush=True)
    env = os.environ.copy()
    # The hooks Claude spawns inherit this, which is what binds the session it
    # is about to create to this goal. A stale claim would bind the wrong one.
    env[AE.GOAL_ENV] = goal["id"]
    AE.clear_claim(trajdir)
    os.execvpe(claude, [claude] + passthrough, env)
    return 0


def _request_chat_refresh(session_id):
    """Coalesce a background goal refresh; hooks must remain fail-open."""
    from .trajectory import chat_synth
    return chat_synth.spawn_refresh(session_id)


def _chat_context_active(session_id):
    """True when this chat opened its goal workspace and that workspace is live.

    Goal context is this chat's own state, so it is injected only into the
    chat that asked for it, and only while the window showing it is open.
    """
    from .trajectory import chat_state as CS
    try:
        if not CS.goals_ui_invoked(session_id):
            return False
        record = _read_server_registry(CS.paths(session_id).session_dir)
    except (OSError, ValueError, TypeError):
        return False
    return bool(_healthy_chat_server(record, session_id))


def chat_hook_main(argv=None, stdin=None, stdout=None):
    """Ingest one Claude Code hook payload and inject cached goal context."""
    import json
    ap = argparse.ArgumentParser(prog="hc chat-hook",
        description="Internal Claude Code chat-state hook.")
    ap.parse_args(argv or [])
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if os.environ.get("HC_CHAT_INFERENCE") == "1":
        return 0
    payload = {}
    event = ""
    try:
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            return 0
        event = str(payload.get("hook_event_name") or "")
        from .trajectory import chat_state as CS
        result = CS.ingest_hook(payload)
    except (OSError, ValueError, TypeError, TimeoutError) as exc:
        if event == "UserPromptExpansion":
            json.dump({
                "decision": "block",
                "reason": f"goals-ui could not initialize chat state: {exc}",
            }, stdout)
            stdout.write("\n")
        return 0

    # Claude's live task list for a Vault goal is execution state, not goal
    # state, so it is observed into its own store and never edits goals.json.
    run = None
    try:
        from .trajectory import agent_exec as AE
        run = AE.observe_hook(payload)
    except Exception:  # noqa: BLE001 - a hook may never block Claude
        run = None

    if event == "UserPromptExpansion":
        # Launch from the hook rather than a skill `!` shell expansion. This
        # keeps /goals-ui functional under disableSkillShellExecution policies.
        import contextlib
        import io
        launch_args = ["--session", result.session_id,
                       "--cwd", str(payload.get("cwd") or os.getcwd())]
        try:
            launched = io.StringIO()
            with contextlib.redirect_stdout(launched):
                chat_ui_main(launch_args)
            url = next(
                (line.strip() for line in reversed(launched.getvalue().splitlines())
                 if line.strip().startswith("http://127.0.0.1:")),
                "",
            )
            if not url:
                raise RuntimeError("launcher returned no localhost URL")
            json.dump({"hookSpecificOutput": {
                "hookEventName": "UserPromptExpansion",
                "additionalContext": f"goals-ui opened for this chat at {url}",
            }}, stdout)
            stdout.write("\n")
        except (OSError, RuntimeError, SystemExit, TimeoutError, ValueError) as exc:
            json.dump({
                "decision": "block",
                "reason": f"goals-ui could not open: {exc}",
            }, stdout)
            stdout.write("\n")
        return 0
    if event in ("Stop", "TaskCompleted", "PostCompact", "SessionEnd"):
        try:
            # Ingestion above is unconditional so history exists whenever the
            # user opens /goals-ui; paying for inference is not.
            if CS.goals_ui_invoked(result.session_id):
                _request_chat_refresh(result.session_id)
        except Exception:  # noqa: BLE001 - a hook may never block Claude
            pass

    if event in ("SessionStart", "UserPromptSubmit"):
        context = ""
        if _chat_context_active(result.session_id):
            try:
                context = CS.paths(result.session_id).goal_context.read_text(
                    encoding="utf-8"
                )[:8000]
            except OSError:
                context = ""
        if run is not None and event == "SessionStart":
            try:
                from .trajectory import goals as GM, state as ST
                trajdir = ST.trajdir()
                tree = GM.sanitize(GM.load(trajdir)[0])
                briefing = AE.goal_context(trajdir, tree, run["vault_goal_id"])
            except Exception:  # noqa: BLE001 - context is best-effort
                briefing = ""
            if briefing.strip():
                context = (briefing + "\n" + context)[:12000]
        if context.strip():
            json.dump({
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": context,
                }
            }, stdout)
            stdout.write("\n")
    return 0


def _server_registry(session_dir):
    return Path(session_dir) / "server.json"


def _read_server_registry(session_dir):
    import json
    try:
        value = json.loads(_server_registry(session_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_server_registry(session_dir, value):
    import json
    target = _server_registry(session_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _healthy_chat_server(record, session_id, timeout=0.5):
    import http.client
    import json
    import urllib.parse
    if not isinstance(record, dict) or not _pid_alive(record.get("pid")):
        return False
    url = record.get("url")
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlparse(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.path not in ("", "/")):
        return False
    connection = None
    try:
        # Speak HTTP directly to loopback. urllib inherits corporate/system
        # proxy behavior on macOS; a readiness probe must never leave the host.
        connection = http.client.HTTPConnection(
            "127.0.0.1", parsed.port, timeout=timeout
        )
        connection.request("GET", "/api/health", headers={
            "Host": parsed.netloc,
        })
        response = connection.getresponse()
        body = json.loads(response.read())
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()
    return (
        isinstance(body, dict)
        and
        response.status == 200
        and body.get("ok") is True
        and body.get("scope") == "chat"
        and body.get("session_id") == session_id
    )


def chat_serve_main(argv=None):
    """Run one scoped server in the detached child process."""
    ap = argparse.ArgumentParser(prog="hc chat-serve",
        description="Internal session-scoped goal server.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv or [])
    from .trajectory import chat_state as CS, ui
    p = CS.paths(args.session)

    def ready(url, _server):
        _write_server_registry(p.session_dir, {
            "schema_version": 1,
            "session_id": args.session,
            "pid": os.getpid(),
            "url": url,
            "started_at": time.time(),
        })

    try:
        ui.run(port=args.port, open_browser=False, trajdir=p.session_dir,
               ready_callback=ready, label="Chat goals")
    finally:
        current = _read_server_registry(p.session_dir)
        if current and current.get("pid") == os.getpid():
            _server_registry(p.session_dir).unlink(missing_ok=True)
    return 0


def chat_refresh_main(argv=None):
    """Run one coalescing goal refresh in a detached child."""
    ap = argparse.ArgumentParser(prog="hc chat-refresh",
        description="Internal session-scoped goal analyzer.")
    ap.add_argument("--session", required=True)
    args = ap.parse_args(argv or [])
    from .trajectory import chat_synth
    before = int(
        chat_synth.CS.get_analyzer_state(args.session)
        .get("last_analyzed_ordinal") or 0
    )
    try:
        result = chat_synth.refresh(args.session)
    finally:
        chat_synth.clear_worker_record(args.session)
    if result.get("status") == "error":
        print(result.get("error") or "chat goal analysis failed",
              file=sys.stderr)
        return 1

    # One worker intentionally has a bounded number of provider calls. If it
    # made progress but more evidence remains, hand the next bounded slice to
    # a fresh worker after releasing our lease. This drains long resumed chats
    # without turning one hook subprocess into an unbounded job.
    state = chat_synth.CS.get_analyzer_state(args.session)
    after = int(state.get("last_analyzed_ordinal") or 0)
    if result.get("needs_handoff") or (
        state.get("status") == "pending" and after > before
    ):
        try:
            chat_synth.spawn_refresh(args.session)
        except Exception as exc:  # noqa: BLE001 - retain pending state for retry
            print(f"could not continue chat goal analysis: {exc}", file=sys.stderr)
            return 1
    return 0


def chat_ui_main(argv=None):
    """Open or reuse the detached UI belonging to one Claude chat."""
    import webbrowser
    ap = argparse.ArgumentParser(prog="hc chat-ui",
        description="Open a session-scoped goal workspace.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--cwd")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv or [])
    from .trajectory import chat_state as CS
    session_cwd = str(Path(args.cwd or os.getcwd()).expanduser().resolve())
    try:
        p = CS.paths(args.session)
        CS.ingest_hook({
            "session_id": args.session,
            "hook_event_name": "SessionStart",
            # Do not overwrite the stable project root already captured by
            # SessionStart merely because the user invoked /goals-ui after `cd`.
            "cwd": CS.load_manifest(args.session).get("cwd") or session_cwd,
        })
        # Opening the workspace is the opt-in: from here this chat may be
        # analyzed, and may have its goal context injected while it is open.
        CS.mark_goals_ui_invoked(args.session)
    except (OSError, ValueError, TypeError, TimeoutError) as exc:
        raise SystemExit(f"could not initialize chat state: {exc}") from exc

    with CS.session_lock(args.session, wait_s=8):
        record = _read_server_registry(p.session_dir)
        if not _healthy_chat_server(record, args.session):
            log_path = p.session_dir / "server.log"
            log_path.touch(mode=0o600, exist_ok=True)
            log_path.chmod(0o600)
            command = [sys.executable, "-m", "human_compact.cli", "chat-serve",
                       "--session", args.session, "--port", str(args.port)]
            child_env = os.environ.copy()
            child_env.pop("HC_CHAT_INFERENCE", None)
            with log_path.open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                    env=child_env,
                )
            # Keep the Popen object alive while this short-lived parent polls
            # readiness. Tests can reap it explicitly; the real CLI exits and
            # the new session becomes an orphan adopted by the OS.
            _DETACHED_PROCESSES.append(process)
            deadline = time.monotonic() + 8
            record = None
            candidate = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                candidate = _read_server_registry(p.session_dir)
                if _healthy_chat_server(candidate, args.session):
                    record = candidate
                    break
                time.sleep(0.05)
            if record is None:
                exit_at_deadline = process.poll()
                if exit_at_deadline is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                detail = ""
                try:
                    detail = log_path.read_text(errors="replace")[-600:].strip()
                except OSError:
                    pass
                startup = (
                    f"pid={process.pid}, exit_at_deadline={exit_at_deadline}, "
                    f"exit_after_cleanup={process.returncode}, "
                    f"registry={candidate!r}"
                )
                if detail:
                    startup += f", log={detail}"
                raise SystemExit(f"chat UI server did not start: {startup}")

    try:
        _request_chat_refresh(args.session)
    except Exception:  # noqa: BLE001 - the UI remains useful without inference
        pass
    url = record["url"]
    if not args.no_open:
        webbrowser.open(url)
    print(url)
    return 0


def hc_main():
    import sys
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: hc <command>\n\n"
              "  goals       your goal tree + important items (primary)\n"
              "  work        start Claude working on one goal\n"
              "  ui          goal tree in the browser (localhost)\n"
              "  chat-ui     goal tree for one Claude chat\n"
              "  mark        mark something important — never lose it\n"
              "  lens        the derived compaction lens\n"
              "  status      vault + analysis pipeline status\n"
              "  refresh     process pending conversations, regenerate lens\n"
              "  install     install /goals-ui without enabling global Vault\n"
              "  setup       noninteractive npm onboarding\n"
              "  backup      onboard Vault / import history\n"
              "  trajectory  full analyze + lens (alias)\n")
        return
    cmd, rest = args[0], args[1:]
    if cmd == "backup":
        sys.argv = ["hc-backup"] + rest
        backup_main()
    elif cmd == "setup":
        setup_main(rest)
    elif cmd == "install":
        install_main(rest)
    elif cmd == "trajectory":
        trajectory_main(rest)
    elif cmd == "lens":
        lens_main(rest)
    elif cmd == "goals":
        goals_main(rest)
    elif cmd == "work":
        raise SystemExit(work_main(rest) or 0)
    elif cmd == "ui":
        ui_main(rest)
    elif cmd == "chat-ui":
        chat_ui_main(rest)
    elif cmd == "chat-serve":
        chat_serve_main(rest)
    elif cmd == "chat-hook":
        chat_hook_main(rest)
    elif cmd == "chat-refresh":
        raise SystemExit(chat_refresh_main(rest))
    elif cmd == "global-hook":
        raise SystemExit(global_hook_main(rest))
    elif cmd == "mark":
        mark_main(rest)
    elif cmd == "status":
        status_main(rest)
    elif cmd == "refresh":
        refresh_main(rest)
    elif cmd == "analyze":
        analyze_main(rest)
    elif cmd == "worker":
        worker_main(rest)
    else:
        print(f"unknown command: {cmd}"); sys.exit(2)


def main():   # keep hc-backup entry point working
    backup_main()



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

if __name__ == "__main__":
    hc_main()
