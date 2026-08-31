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

from .platform_compat import (
    detached_popen_kwargs, pid_alive, replace_process, terminate_pid,
)

HOME = Path(os.environ.get("HC_HOME", Path.home()))
SKILLS_DIR = HOME / ".claude" / "skills" / "vault"
BART_SKILL_DIR = HOME / ".claude" / "skills" / "bart"
# Pre-rename install paths, oldest first. Each is retired by install_plugin()
# when it is one we put there; a skill someone else owns is left alone.
LEGACY_SKILL_DIRS = (
    (HOME / ".claude" / "skills" / "hc-ui", "hc-ui"),
    (HOME / ".claude" / "skills" / "goals-ui", "goals-ui"),
)
VAULT_BIN = HOME / ".claude-vault" / "bin"
ZSHRC = HOME / ".zshrc"
PATH_LINE = 'export PATH="$HOME/.claude-vault/bin:$PATH"'
ALWAYS_LINE = "export CLAUDE_VAULT=1"
_DETACHED_PROCESSES = []
# The address that says "show me every project" to a workspace window.
# The page is an overlay on a running workspace rather than a route of its
# own, so the ask has to survive the hop from a terminal into a browser.
_HOME_HASH = "projects"
MIN_CLAUDE_VERSION = (2, 1, 175)
MIN_CLAUDE_VERSION_TEXT = ".".join(map(str, MIN_CLAUDE_VERSION))
MANAGED_MARKER = ".human-compact-managed.json"
_ASSET_FILES = {
    "vault": {
        ".claude-plugin/plugin.json", "README.md", "hooks/hooks.json",
        "hooks/hooks.experimental.json", "scripts/chat-hook.cjs",
        "scripts/hc-runtime.cjs", "scripts/vault-hook.cjs",
    },
    "bart": {"SKILL.md"},
}
# This release ships the chat-scoped goal UI. The global Vault layer and the
# analysis commands built on it stay in the tree, but nothing reaches them
# unless the operator asks for them by name.
EXPERIMENTAL_COMMANDS = ("ui", "backup", "trajectory", "lens", "goals", "work",
                         "mark", "status", "refresh", "analyze", "worker")
_LAUNCH_COMMAND_HELP = (
    ("install", "install /bart for Claude Code"),
    ("setup", "noninteractive npm onboarding"),
    ("chat-ui", "goal tree for one Claude chat"),
    ("setup-ui", "set a first project up, before there is a chat"),
    ("setup-import", "create a project from an approved web setup"),
    ("supabase", "connect this workspace to your own Supabase"),
    ("chat-serve", "session-scoped goal server (internal)"),
    ("chat-hook", "Claude Code chat-state hook (internal)"),
    ("chat-refresh", "session-scoped goal analyzer (internal)"),
    ("global-hook", "Vault lifecycle hook (internal)"),
)
_EXPERIMENTAL_COMMAND_HELP = (
    ("goals", "your goal tree + important items"),
    ("work", "start Claude working on one goal"),
    ("ui", "goal tree in the browser (localhost)"),
    ("mark", "mark something important — never lose it"),
    ("lens", "the derived compaction lens"),
    ("status", "vault + analysis pipeline status"),
    ("refresh", "process pending conversations, regenerate lens"),
    ("backup", "onboard Vault / import history"),
    ("trajectory", "full analyze + lens (alias)"),
    ("analyze", "analyze the vaulted history, build the goal tree"),
    ("worker", "internal analysis worker"),
)
# Exact unmarked v0.15.0 assets. This permits migration of installs created by
# this project before ownership markers existed without claiming arbitrary
# directories that merely happen to use the same names.
_LEGACY_DIGESTS = {
    "vault": {"4f5319b78efe7f90eccb967bbcd787b7ddcfbfdae8643e82281f01e6551dda02"},
    # This digest is the v0.15.0 /hc-ui SKILL.md, which the rename
    # superseded; it now only identifies a legacy ~/.claude/skills/hc-ui.
    "goals-ui": {"6ddef8b28e8df3dec16591f7658199158fd97cc02e85b854bbbd79739f398815"},
}


def experimental_enabled() -> bool:
    return os.environ.get("HC_EXPERIMENTAL") == "1"


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


def _asset_digest(root: Path, asset: str, overrides=None):
    """Return an exact tree digest, or None for any unexpected path/layout.

    `overrides` substitutes file contents for the digest only, so a staged tree
    that intentionally differs from the package can still be validated against
    exactly the difference we made.
    """
    overrides = overrides or {}
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
            digest.update(overrides[name] if name in overrides
                          else (root / name).read_bytes())
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
            "move it aside, then rerun `engelbart install`")
    if _owned_asset(destination, asset):
        return "managed"
    if _legacy_asset(destination, source, asset):
        return "legacy"
    raise RuntimeError(
        f"refusing to replace unmanaged Claude skill directory: {destination}; "
        "move it aside, then rerun `engelbart install`")


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


def _asset_overrides(source: Path, asset: str):
    """Packaged files this install replaces before anything is promoted."""
    if asset != "vault" or not experimental_enabled():
        return {}
    return {"hooks/hooks.json":
            (source / "hooks" / "hooks.experimental.json").read_bytes()}


def _stage_asset(source: Path, destination: Path, asset: str) -> Path:
    stage = destination.parent / f".{destination.name}.hc-stage-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, stage, symlinks=True)
        # Applied here so the promoted tree, its modes, and the digest that
        # validates it all describe the same bytes.
        overrides = _asset_overrides(source, asset)
        for name, content in overrides.items():
            (stage / name).write_bytes(content)
        marker = stage / MANAGED_MARKER
        marker.write_text(json.dumps({
            "owner": "human-compact", "asset": asset, "format": 1,
        }, sort_keys=True) + "\n")
        _tighten_asset_modes(stage)
        if not _owned_asset(stage, asset):
            raise RuntimeError(f"staged {asset} ownership marker is invalid")
        # Beyond the overrides above, the marker is intentionally the only
        # difference from packaged data.
        marker.unlink()
        source_digest = _asset_digest(source, asset, overrides)
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


def _retire_legacy_skills():
    """Remove skills this command used to be called, when we installed them."""
    for path, name in LEGACY_SKILL_DIRS:
        _retire_one_legacy_skill(path, name)


def _retire_one_legacy_skill(path, name):
    if not _path_exists(path):
        return
    ours = (not path.is_symlink() and path.is_dir()
            and (_owned_asset(path, name)
                 or _owned_asset(path, "goals-ui")
                 or _asset_digest(path, "bart") in _LEGACY_DIGESTS["goals-ui"]))
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
        {"asset": "bart", "source": asset_root() / "bart-skill",
         "destination": BART_SKILL_DIR},
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
    say("hooks: chat-scoped + global Vault (HC_EXPERIMENTAL=1)"
        if experimental_enabled() else
        "hooks: chat-scoped only (set HC_EXPERIMENTAL=1 at install to wire "
        "global Vault hooks)")
    say(f"/bart installed -> {BART_SKILL_DIR}")
    # Only after promotion: a stale /hc-ui or /bart skill would otherwise
    # still claim a workspace URL that nothing supplies.
    _retire_legacy_skills()


def install_main(argv=None):
    """Install the chat-scoped UI without enabling the global Vault layer."""
    ap = argparse.ArgumentParser(
        prog="hc install",
        description="Install /bart for Claude Code (no global context layer).")
    ap.parse_args(argv or [])
    print("\nhc · /bart\n")
    if not (HOME / ".claude").exists():
        say("WARNING: ~/.claude not found — install Claude Code first")
    install_plugin()
    print()
    say("Done. Start a new Claude Code session (or run /reload-plugins),")
    say("then type /bart in any chat.")
    print()


def install_shim():
    VAULT_BIN.mkdir(parents=True, exist_ok=True)
    dest = VAULT_BIN / "claude"
    shutil.copy2(asset_root() / "shim" / "claude", dest)
    make_exec(dest)
    say(f"shim installed   -> {dest}")
    zshrc_append(PATH_LINE, "shim on PATH")


def run_backfill(assume_yes=False):
    # Import through the runtime's own idempotent backfill rather than a bash
    # script: it needs no jq/coreutils and so runs the same on every OS. The
    # caller has already gathered consent for the retroactive import.
    from . import global_vault
    counts = global_vault.backfill()
    if not counts["imported"] and not counts["skipped"]:
        say("nothing new to import")
        return
    say(f"history import: {counts['imported']} imported, "
        f"{counts['skipped']} already present")


def backup_main():
    ap = argparse.ArgumentParser(prog="hc-backup",
                                 description="Onboard Vault for Claude Code.")
    ap.add_argument("--retroactive", choices=["yes", "no"],
                    help="import existing conversations without prompting")
    ap.add_argument("--mode", choices=["all", "selective"],
                    help="capture mode without prompting")
    args = ap.parse_args()

    print("\nhc · Vault onboarding (experimental)\n")

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
            "Claude Code is required; install it, then rerun `engelbart install`")
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


def _say_global_vault_state():
    """Describe the capture the install actually leaves running."""
    from . import global_vault
    if not global_vault.is_enabled():
        say("global Vault not enabled; nothing is captured")
    elif experimental_enabled():
        say("global Vault stays enabled; its capture hooks are installed")
    else:
        say("global Vault stays enabled on disk, but its capture hooks are "
            "not installed in this release; reinstall with HC_EXPERIMENTAL=1 "
            "to wire them")


def setup_main(argv=None):
    """One noninteractive orchestration seam for the Engelbart installer."""
    ap = argparse.ArgumentParser(
        prog="hc setup",
        description="Install /bart and optionally initialize global Vault state.")
    ap.add_argument("--global-vault", required=True,
                    choices=["yes", "no", "keep"])
    ap.add_argument("--goals", required=True, choices=["yes", "no"])
    args = ap.parse_args(argv or [])
    if args.goals == "yes" and args.global_vault != "yes":
        ap.error("--goals yes requires --global-vault yes")
    if args.global_vault == "yes" and not experimental_enabled():
        ap.error("--global-vault yes is experimental in this release; "
                 "set HC_EXPERIMENTAL=1")

    # This is deliberately first: /bart remains installed even when optional
    # global-history setup fails later and the user retries the installer.
    install_main([])
    if args.global_vault in ("keep", "no"):
        if args.global_vault == "no":
            from . import global_vault
            global_vault.disable_always_on()
        # "keep" leaves the recorded choice alone, but the install just rewrote
        # hooks.json. Report what is on disk now, not what was requested: a
        # vault that stays enabled is no longer being captured.
        _say_global_vault_state()
        _validate_claude_cli()
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
    # POSIX replaces this process with claude; Windows cannot, so it runs claude
    # to completion and exits with its status. Either way this call never returns.
    replace_process(claude, passthrough, env)
    return 0


def _request_chat_refresh(session_id):
    """Coalesce a background goal refresh; hooks must remain fail-open."""
    from .trajectory import chat_synth
    return chat_synth.spawn_refresh(session_id)


def _chat_context_active(session_id):
    """True when this chat opted in to goal context and has not turned it off.

    Goal context is this chat's own state, so it is injected only into the
    chat that asked for it -- but one /bart is the whole ask.  The
    workspace server does not have to still be running: goals outlive the
    browser tab that wrote them, and a chat that silently stopped honouring
    them the moment a window closed would read as broken.
    """
    from .trajectory import chat_state as CS
    try:
        return bool(CS.goals_ui_active(session_id))
    except Exception:  # noqa: BLE001 - a hook may never block Claude
        # Corrupt session state must cost the user their goal context, never
        # their prompt.
        return False


def chat_hook_main(argv=None, stdin=None, stdout=None):
    """Ingest one Claude Code hook payload and inject cached goal context."""
    import json
    ap = argparse.ArgumentParser(prog="hc chat-hook",
        description="Internal Claude Code chat-state hook.")
    # PostToolBatch is wired twice: an async entry that ingests the batch off
    # the critical path, and this one, which runs inside the model's own turn
    # and may therefore only read cached state and speak.
    ap.add_argument("--inject-only", action="store_true",
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv or [])
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    if os.environ.get("HC_CHAT_INFERENCE") == "1":
        return 0
    payload = {}
    event = ""
    session_id = ""
    try:
        payload = json.loads(stdin.read())
        if not isinstance(payload, dict):
            return 0
        event = str(payload.get("hook_event_name") or "")
        from .trajectory import chat_state as CS
        if args.inject_only:
            session_id = str(payload.get("session_id")
                             or payload.get("sessionId") or "")
            CS.paths(session_id)  # reject a path-like id before touching disk
        else:
            session_id = CS.ingest_hook(payload).session_id
    except (OSError, ValueError, TypeError, TimeoutError) as exc:
        if event == "UserPromptExpansion":
            json.dump({
                "decision": "block",
                "reason": f"bart could not initialize chat state: {exc}",
            }, stdout)
            stdout.write("\n")
        return 0

    # Claude's live task list for a Vault goal is execution state, not goal
    # state, so it is observed into its own store and never edits goals.json.
    run = None
    if not args.inject_only:
        try:
            from .trajectory import agent_exec as AE
            run = AE.observe_hook(payload)
        except Exception:  # noqa: BLE001 - a hook may never block Claude
            run = None

    # The workspace's Build: every hook is proof the connected session is
    # alive, and the ones that see the transcript read Claude's protocol lines
    # back out of it so the rail's rows move. Stop and UserPromptSubmit are
    # also where a queued build is handed over, below.
    build_text = ""
    try:
        from .trajectory import build as BUILD
        if session_id:
            BUILD.note_hook(session_id, None, event)
            if event in ("Stop", "SubagentStop", "PostToolBatch",
                         "TaskCompleted") and not args.inject_only:
                BUILD.scan_transcript(session_id, None,
                                      payload.get("transcript_path"))
            if event in ("Stop", "UserPromptSubmit"):
                build_text = BUILD.deliver(session_id, None, event)
    except Exception:  # noqa: BLE001 - a hook may never block Claude
        build_text = ""

    if event == "UserPromptExpansion":
        if str(payload.get("command_args") or "").strip().lower() == "disable":
            try:
                CS.disable_goals_ui(session_id)
            except (OSError, ValueError, TypeError, TimeoutError) as exc:
                json.dump({
                    "decision": "block",
                    "reason": f"bart could not be disabled: {exc}",
                }, stdout)
                stdout.write("\n")
                return 0
            json.dump({
                "decision": "block",
                "reason": "bart: disabled for this chat — run /bart "
                          "to turn it back on",
            }, stdout)
            stdout.write("\n")
            return 0
        # Launch from the hook rather than a skill `!` shell expansion. This
        # keeps /bart functional under disableSkillShellExecution policies.
        import contextlib
        import io
        launch_args = ["--session", session_id,
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
            # Anything the launcher wanted the reader to know -- so far, only
            # a workspace it declined to replace -- said after the URL.
            aside = next(
                (line.strip()[len("note: "):]
                 for line in launched.getvalue().splitlines()
                 if line.strip().startswith("note: ")),
                "",
            )
            # Blocking the expansion ends the turn with no model call, and
            # Claude Code prints `reason` to the user. That is the closest a
            # plugin gets to a built-in local command like /model: the
            # workspace opens and Claude never speaks. Handing back
            # additionalContext instead would buy a whole turn to say a URL
            # the user can already see.
            json.dump({"decision": "block",
                       "reason": f"bart: {url}"
                                 + (f" — {aside}" if aside else "")},
                      stdout)
            stdout.write("\n")
        except (OSError, RuntimeError, SystemExit, TimeoutError, ValueError) as exc:
            json.dump({
                "decision": "block",
                "reason": f"bart could not open: {exc}",
            }, stdout)
            stdout.write("\n")
        return 0
    if event in ("Stop", "TaskCompleted", "PostCompact", "SessionEnd"):
        try:
            # Ingestion above is unconditional so history exists whenever the
            # user opens /bart; paying for inference is not.
            if CS.goals_ui_active(session_id):
                _request_chat_refresh(session_id)
                # Keep the user-readable copy current with what inference
                # last wrote, not only with what was last injected.
                if event == "Stop":
                    CS.mirror_goal_context(session_id,
                                           payload.get("transcript_path"),
                                           payload.get("cwd"))
        except Exception:  # noqa: BLE001 - a hook may never block Claude
            pass
    if event == "Stop" and build_text:
        # Claude Code reads a blocked Stop's reason as the next thing to do,
        # in this same session: the build reaches Claude the moment its turn
        # ends, with everything the conversation already holds.
        json.dump({"decision": "block", "reason": build_text}, stdout)
        stdout.write("\n")
        return 0

    # A subagent begins with an empty context and a tool batch may have just
    # created tasks, so both are injection points -- but only the synchronous
    # PostToolBatch entry speaks, or the async one would consume the delta
    # into a stdout nobody reads.
    if (event in ("SessionStart", "UserPromptSubmit", "SubagentStart")
            or (event == "PostToolBatch" and args.inject_only)):
        context = ""
        if _chat_context_active(session_id):
            try:
                context = CS.render_context_injection(
                    session_id,
                    "full" if event in ("SessionStart", "SubagentStart")
                    else "delta",
                    transcript_path=payload.get("transcript_path"),
                    cwd=payload.get("cwd"),
                    # A subagent reads on its own account; what it was shown
                    # says nothing about what this conversation has seen.
                    remember=event != "SubagentStart",
                    # This runs inside the model's turn, against a 5s hook
                    # budget, while the async ingest of the same batch may
                    # hold the session lock. Recording the render is worth
                    # half a second and no more: timing out raises, the
                    # `except` below drops the injection, and the snapshot
                    # stays put so the next one restates the same change.
                    snapshot_wait_s=0.5,
                )
            except Exception:  # noqa: BLE001 - a hook may never block Claude
                context = ""
        if build_text and event == "UserPromptSubmit":
            # The session was idle when Build was pressed; it rides along
            # with the user's next message.
            context = (context + "\n\n" if context.strip() else "") + build_text
        if run is not None and event == "SessionStart":
            try:
                from .trajectory import goals as GM, state as ST
                trajdir = ST.trajdir()
                tree = GM.sanitize(GM.load(trajdir)[0])
                briefing = AE.goal_context(trajdir, tree, run["vault_goal_id"])
            except Exception:  # noqa: BLE001 - context is best-effort
                briefing = ""
            if briefing.strip():
                context = briefing + "\n" + context
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
    return pid_alive(pid)


def _package_code_stamp():
    """The newest edit time among this package's own Python files, or 0.0."""
    newest = 0.0
    try:
        for path in Path(__file__).resolve().parent.rglob("*.py"):
            newest = max(newest, path.stat().st_mtime)
    except OSError:                       # noqa: BLE001 - a hint, never logic
        return 0.0
    return newest


def _server_outran_its_code(record):
    """Whether the server in `record` started before the code it serves.

    The browser's half of the workspace is re-read from disk on every page
    load and the server's half is not, so editing the plugin with a workspace
    open leaves a new page talking to an old process -- which answers every
    control added since with "unknown operation". The page says so and tells
    the reader to restart it, which only means anything if reopening actually
    replaces the server rather than handing back the same one.
    """
    if not isinstance(record, dict):
        return False
    try:
        started = float(record.get("started_at") or 0.0)
    except (TypeError, ValueError):
        return False
    newest = _package_code_stamp()
    # A second of slack: a file copied into place can carry a timestamp a hair
    # past the process that read it, and nobody edited anything.
    return bool(started and newest and newest > started + 1.0)


def _chat_server_is_building(session_dir):
    """Whether a build this server is reading is still running.

    A build is a subprocess, but the thread that reads what it prints and
    records how each row ended belongs to the server. Replacing the server
    under a live build would leave the rows saying "building" forever, so an
    out-of-date workspace with work in flight is left alone: it can be
    restarted once the build lands.
    """
    import json
    try:
        runs = sorted((Path(session_dir) / "builds").glob("*.json"))
    except OSError:
        return False
    for path in runs:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        if (str(record.get("status") or "") in ("running", "retrying")
                and _pid_alive(record.get("pid"))):
            return True
    return False


def _stop_chat_server(record, timeout=6.0):
    """Stop the session server named in `record`; True once it is gone."""
    import signal
    try:
        pid = int(record.get("pid")) if isinstance(record, dict) else None
    except (TypeError, ValueError):
        return False
    if pid is None:
        return False
    try:
        terminate_pid(pid)
    except OSError:
        # Already gone, or not ours to signal. Only the first is a success.
        return not _pid_alive(pid)
    # A server this process launched itself is its own child, and a child has
    # to be waited on: unreaped, it answers a liveness check as if it were
    # still serving. Orphaned daemons -- the ordinary case, where the launcher
    # exited long ago -- are reaped by the system, so polling is enough.
    mine = next((proc for proc in _DETACHED_PROCESSES if proc.pid == pid), None)
    if mine is not None:
        try:
            mine.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
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


def supabase_main(argv=None):
    """Set up, sign in to, or sign out of the reader's own Supabase.

    The password is typed here and exchanged once for tokens: it is not
    stored, not logged, and not passed on the command line, where it would
    sit in the shell's history for anyone who reads the file.
    """
    import getpass
    ap = argparse.ArgumentParser(
        prog="hc supabase",
        description="Connect the goal workspace to your own Supabase project.")
    ap.add_argument("action",
                    choices=("setup", "login", "logout", "status", "whoami"))
    # The browser is the ordinary way in: the reader is already signed in
    # there, and a password typed at a terminal is a password the terminal
    # has seen. By default it opens the connect page, which sends a sign-in
    # link to an email; --provider skips the page for a provider button.
    # --password is the way through for anyone who cannot open a browser
    # -- a remote shell, a machine with none.
    ap.add_argument("--provider", default=None,
                    help="go straight to GitHub at Supabase"
                         " instead of the connect page")
    ap.add_argument("--password", action="store_true",
                    help="type an email and password instead of the browser")
    args = ap.parse_args(argv or [])
    from .trajectory import supabase_client as SB

    if args.action == "setup":
        path, created = SB.write_template()
        say(("wrote %s" if created else "%s is already there") % path)
        say("Put your project URL and anon (public) key in it, then run"
            " `hc supabase login`.")
        say("Find both under Project Settings -> API in the Supabase"
            " dashboard.")
        say("Use the ANON key, not the service key: the workspace signs in"
            " as you, and row security does the rest.")
        return 0

    if args.action == "logout":
        SB.sign_out()
        say("signed out; the stored tokens are gone")
        return 0

    if args.action == "whoami":
        try:
            session = SB.current_session()
        except SB.SupabaseError as exc:
            say(str(exc))
            return 1
        say(session.get("email") or session.get("user_id") or "signed in")
        return 0

    if args.action == "status":
        state = SB.status()
        say(f"config    {state['config_path']}")
        say(f"configured {'yes' if state['configured'] else 'no'}")
        say(f"signed in  {'yes' if state['signed_in'] else 'no'}"
            + (f" ({state['email']})" if state.get("email") else ""))
        return 0

    try:
        config = SB.load_config()
    except SB.SupabaseError as exc:
        say(str(exc))
        return 1
    if not config["url"] or not config["anon_key"]:
        say(f"fill in {SB.config_path()} first (run `hc supabase setup`)")
        return 1
    if not args.password:
        say("Opening your browser to sign in\u2026")
        try:
            session = SB.sign_in_with_browser(
                args.provider,
                announce=lambda url: say("If it did not open: " + url))
        except SB.SupabaseError as exc:
            say(str(exc))
            say("If this machine has no browser, use"
                " `hc supabase login --password`.")
            return 1
        say(f"signed in as {session['email']}")
        say("the workspace can now send this project from the project"
            " overview")
        return 0

    email = config.get("email") or ""
    if not email or email == "you@example.com":
        email = input("email: ").strip()
    else:
        say(f"email: {email}")
    password = getpass.getpass("password (not stored): ")
    try:
        session = SB.sign_in(email, password)
    except SB.SupabaseError as exc:
        say(str(exc))
        return 1
    say(f"signed in as {session['email']}")
    say("the workspace can now send this project from the project overview")
    return 0


def chat_serve_main(argv=None):
    """Run one scoped server in the detached child process."""
    ap = argparse.ArgumentParser(prog="hc chat-serve",
        description="Internal session-scoped goal server.")
    ap.add_argument("--session", required=True)
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv or [])
    from .trajectory import chat_state as CS, ui
    p = CS.paths(args.session)

    from .trajectory import project_store as PS
    # Only an explicit binding: recording this window against the directory
    # the store happens to name would hand an unbound chat's workspace to a
    # project it has never joined.
    home = ""
    try:
        home = CS.bound_project(args.session)
    except Exception:  # noqa: BLE001 - a store with no manifest has no project
        home = ""

    def ready(url, _server):
        record = {
            "schema_version": 1,
            "session_id": args.session,
            "pid": os.getpid(),
            "url": url,
            "started_at": time.time(),
        }
        _write_server_registry(p.session_dir, record)
        # And on the project, which is what every other chat of it reads:
        # a workspace nobody else can find is a second window waiting to
        # happen.
        if home:
            try:
                PS.set_server_record(None, home, record)
            except Exception:  # noqa: BLE001 - never fail to serve over this
                pass

    try:
        ui.run(port=args.port, open_browser=False, trajdir=p.session_dir,
               ready_callback=ready, label="Chat goals")
    finally:
        current = _read_server_registry(p.session_dir)
        if current and current.get("pid") == os.getpid():
            _server_registry(p.session_dir).unlink(missing_ok=True)
        if home:
            try:
                noted = PS.server_record(None, home)
                if noted and noted.get("pid") == os.getpid():
                    PS.clear_server_record(None, home)
            except Exception:  # noqa: BLE001 - shutdown must still finish
                pass
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


def setup_ui_main(argv=None):
    """Open the setup page: what someone sees having only just installed.

    There is no chat to scope a workspace to -- that is the whole situation
    this page is for -- so a workspace of the vault's own is minted for the
    directory the installer ran in, and the ordinary launcher is asked for a
    server on it. The page it opens needs nothing of that workspace except
    the process: it posts its own operations and writes nothing until the
    reader presses the button at the end.
    """
    import contextlib
    import io
    import webbrowser
    ap = argparse.ArgumentParser(
        prog="hc setup-ui",
        description="Set up a first project, before there is a chat.")
    ap.add_argument("--cwd")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv or [])
    from .trajectory import chat_state as CS
    here = str(Path(args.cwd or os.getcwd()).expanduser().resolve())
    session = CS.open_workspace_for(here)
    # The launcher prints one line, the URL, and knows how to find a server
    # that is already up. Its output is the mechanism, so it is captured
    # rather than printed twice.
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        chat_ui_main(["--session", session, "--port", str(args.port),
                      "--no-open"])
    url = next((line.strip() for line in reversed(said.getvalue().splitlines())
                if line.strip().startswith("http://127.0.0.1:")), "")
    if not url:
        sys.stderr.write("hc: the workspace did not start\n")
        raise SystemExit(1)
    page = url.rstrip("/") + "/setup"
    print(page)
    if not args.no_open:
        with contextlib.suppress(Exception):
            webbrowser.open(page)


def setup_import_main(argv=None):
    """Create the project a member approved on the web, then open it.

    The conversation already happened -- on berkeley.mathetic.com, before
    this machine had Engelbart at all -- so there is nothing to ask here.
    The payload is the web page's saved answers in setup_chat's own commit
    vocabulary; commit() re-normalizes every field, so a payload from a
    different (or hostile) origin can make at most a project with odd text
    in it, never anything else. On success the one line on stdout is the
    workspace URL, which is the same contract setup-ui keeps with the
    installer that spawns it.
    """
    import contextlib
    import io
    import json
    import webbrowser
    ap = argparse.ArgumentParser(
        prog="hc setup-import",
        description="Create a project from an approved web setup.")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="path to the saved setup payload")
    source.add_argument("--stdin", action="store_true",
                        help="read the payload from standard input")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv or [])
    try:
        raw = sys.stdin.read() if args.stdin else Path(args.file).read_text()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"hc: could not read the setup payload: {exc}\n")
        raise SystemExit(1)
    if not isinstance(payload, dict):
        sys.stderr.write("hc: the setup payload is not an object\n")
        raise SystemExit(1)
    from .trajectory import setup_chat as SETUP
    result = SETUP.commit(None, payload.get("name"), payload.get("plan"),
                          payload.get("goals"), payload.get("chosen"),
                          payload.get("todos"), payload.get("subgoals") or [],
                          bind="", paper=payload.get("paper"),
                          provenance=payload.get("provenance"))
    if not result.get("ok"):
        sys.stderr.write(f"hc: {result.get('error') or 'could not create the project'}\n")
        raise SystemExit(1)
    # The workspace launcher prints one line, the URL; it is the mechanism
    # here rather than the message, so it is captured, not printed twice.
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        chat_ui_main(["--session", result["tree_session"], "--cwd",
                      result["cwd"], "--port", str(args.port), "--no-open"])
    url = next((line.strip() for line in reversed(said.getvalue().splitlines())
                if line.strip().startswith("http://127.0.0.1:")), "")
    if not url:
        sys.stderr.write("hc: the project was created but its workspace "
                         "did not start; run `hc setup-ui` to open it\n")
        raise SystemExit(1)
    print(url)
    if not args.no_open:
        with contextlib.suppress(Exception):
            webbrowser.open(url)


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
            # SessionStart merely because the user invoked /bart after `cd`.
            "cwd": CS.load_manifest(args.session).get("cwd") or session_cwd,
        })
        # Opening the workspace is the opt-in: from here this chat may be
        # analyzed, and may have its goal context injected while it is open.
        CS.mark_goals_ui_invoked(args.session)
    except (OSError, ValueError, TypeError, TimeoutError) as exc:
        raise SystemExit(f"could not initialize chat state: {exc}") from exc

    # The workspace is where the user reads their goals; the mirror is where
    # they read them once it is closed. Only the manifest knows where Claude
    # keeps this session's transcript. Mirroring itself never raises.
    CS.mirror_goal_context(args.session,
                           CS.load_manifest(args.session).get("transcript_path"),
                           session_cwd)

    # A workspace belongs to a project, not to a chat. A project has many
    # chats, and one window between them: the question asked here used to be
    # "is a server running for ME?", which every chat but the first answered
    # no to -- three chats in a project meant three ports, three windows and
    # three trees. It is asked of the project now, and the project is the one
    # that remembers, so a chat that has never run a server of its own still
    # finds the one that is up.
    from .trajectory import project_store as PS
    serve = args.session
    try:
        serve = CS.tree_session(args.session) or args.session
    except Exception:  # noqa: BLE001 - an unresolvable project serves itself
        serve = args.session
    home = ""
    try:
        home = CS.bound_project(args.session)
    except Exception:  # noqa: BLE001 - an unbound chat has only itself
        home = ""
    sp = CS.paths(serve)
    sp.session_dir.mkdir(parents=True, exist_ok=True)

    def _known():
        """What is running for this project, as the project and the store
        that serves it each remember it. The project is asked first: it is
        the answer that holds when the chat asking has never served."""
        if home:
            noted = PS.server_record(None, home)
            if isinstance(noted, dict) and noted.get("url"):
                return noted
        return _read_server_registry(sp.session_dir)

    def _note(value):
        if home and isinstance(value, dict):
            PS.set_server_record(None, home, value)

    def _forget():
        if home:
            PS.clear_server_record(None, home)

    with CS.session_lock(serve, wait_s=8):
        record = _known()
        note = ""
        # Reopening a workspace whose code has moved on is a restart, not a
        # reuse: otherwise the reader is handed back the same old server that
        # just told them to restart it, and every control added since keeps
        # failing with no way out.
        # Staleness first: it is a handful of stat calls, and the health probe
        # below is a request the common path should only pay for once.
        if (_server_outran_its_code(record)
                and _healthy_chat_server(record, serve)):
            if _chat_server_is_building(sp.session_dir):
                note = ("kept the running workspace: a build is in flight."
                        " Reopen it when the build lands to pick up the"
                        " newer code.")
            elif _stop_chat_server(record):
                record = None
                _forget()
        # Whose store the running workspace serves, as it says itself: a
        # record made before this chat joined the project names the store it
        # was started on, which is the store this chat now reads too.
        running = str((record or {}).get("session_id") or serve)
        if _healthy_chat_server(record, running):
            _note(record)
        else:
            _forget()
            record = None
            log_path = sp.session_dir / "server.log"
            log_path.touch(mode=0o600, exist_ok=True)
            log_path.chmod(0o600)
            command = [sys.executable, "-m", "human_compact.cli", "chat-serve",
                       "--session", serve, "--port", str(args.port)]
            child_env = os.environ.copy()
            child_env.pop("HC_CHAT_INFERENCE", None)
            with log_path.open("ab", buffering=0) as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    env=child_env,
                    **detached_popen_kwargs(),
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
                # Either place the new server registers itself: its own
                # store, which is what it writes, or the project's note,
                # which is what the next chat will read.
                candidate = _known()
                if _healthy_chat_server(candidate, serve):
                    record = candidate
                    _note(record)
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
    # The URL last: the hook that prints this reads back the last line that
    # looks like one, and a note above it rides along in what the reader sees.
    if note:
        print("note: " + note)
    print(url)
    return 0


def _hc_usage() -> str:
    commands = list(_LAUNCH_COMMAND_HELP)
    if experimental_enabled():
        commands += list(_EXPERIMENTAL_COMMAND_HELP)
    width = max(len(name) for name, _ in commands) + 2
    lines = "".join(f"  {name:<{width}}{description}\n"
                    for name, description in commands)
    text = f"usage: hc <command>\n\n{lines}"
    if not experimental_enabled():
        text += "\nExperimental commands are available with HC_EXPERIMENTAL=1.\n"
    return text


def hc_main():
    import sys
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(_hc_usage(), end="")
        return
    cmd, rest = args[0], args[1:]
    if cmd in EXPERIMENTAL_COMMANDS and not experimental_enabled():
        print(f"hc {cmd} is experimental in this release; "
              "set HC_EXPERIMENTAL=1 to enable it", file=sys.stderr)
        sys.exit(2)
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
    elif cmd == "setup-ui":
        setup_ui_main(rest)
    elif cmd == "setup-import":
        setup_import_main(rest)
    elif cmd == "supabase":
        raise SystemExit(supabase_main(rest) or 0)
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


def bart_start_main(argv=None):
    """Open the vault's Projects page from a terminal.

    The page lists every project this vault knows, and it is drawn by
    whichever workspace server is up -- the question is "is one running?",
    not "is mine running?". So a healthy server anywhere in the vault is the
    answer, and the directory this was run in only decides which server is
    asked first.

    Nothing is minted. A viewer that created a project for the directory it
    happened to be run in would fill the list it exists to show; a vault with
    no projects yet is told to open one from a chat instead.
    """
    import contextlib
    import io
    import webbrowser
    ap = argparse.ArgumentParser(
        prog="bart start",
        description="Open the workspace listing every project in this vault.")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv or [])
    from .trajectory import project_store as PS

    rows = PS.list_projects(None)
    if not rows:
        sys.stderr.write(
            "bart: this vault has no projects yet. Run /bart inside a Claude "
            "Code chat to open the first one.\n")
        raise SystemExit(1)

    # Newest first, except the project underfoot, which goes first: a reader
    # standing in one asked for that window, not the last one they touched.
    try:
        here = PS.repo_home(os.getcwd())
    except (OSError, ValueError):
        here = ""
    order = ([r for r in rows if r["cwd"] == here]
             + [r for r in rows if r["cwd"] != here])

    url = ""
    for row in order:
        record = PS.server_record(None, row["cwd"])
        if isinstance(record, dict) and _healthy_chat_server(
                record, str(record.get("session_id") or "")):
            url = str(record.get("url") or "")
            if url:
                break
            url = ""

    if not url:
        # Nothing is up. A server serves one project's store, so the first
        # project in the order that HAS a store is the one started; a project
        # nobody has ever opened has none, and cannot be served.
        for row in order:
            session = PS.tree_session(None, row["cwd"])
            if not session:
                held = PS.project_sessions(None, row["cwd"])
                session = held[-1] if held else ""
            if not session:
                continue
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                chat_ui_main(["--session", session, "--cwd", row["cwd"],
                              "--port", str(args.port), "--no-open"])
            url = next((line.strip()
                        for line in reversed(said.getvalue().splitlines())
                        if line.strip().startswith("http://127.0.0.1:")), "")
            if url:
                break

    if not url:
        sys.stderr.write("bart: no workspace could be started for this vault\n")
        raise SystemExit(1)

    page = url + ("" if url.endswith("/") else "/") + "#" + _HOME_HASH
    print(page)
    if not args.no_open:
        with contextlib.suppress(Exception):
            webbrowser.open(page)
    return 0


def bart_main(argv=None):
    """Open projects; keep old ``bart token`` helpers on the one auth store.

    Account mutation belongs to the npm device flow.  ``bart`` remains the
    installed project launcher, and ``token`` reads that flow's record so a
    pre-merge Claude setting does not break during upgrade.
    """

    said = list(sys.argv[1:] if argv is None else argv)
    # Opening the workspace is not an authentication question, and it carries
    # its own flags, so it is taken before the account parser sees anything.
    if said and said[0] == "start":
        return bart_start_main(said[1:])

    ap = argparse.ArgumentParser(
        prog="bart",
        description="Open Engelbart projects (account commands use engelbart).")
    ap.add_argument("action", nargs="?", default="status",
                    choices=("auth", "status", "token", "logout", "start"))
    args = ap.parse_args(said)

    managed = Path(os.environ.get("HUMAN_COMPACT_HOME")
                   or Path.home() / ".human-compact")
    try:
        record = json.loads((managed / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}

    if args.action in ("auth", "logout"):
        print(f"bart: run `engelbart {args.action}`; account changes "
              "use the device-auth flow", file=sys.stderr)
        return 2
    claude = record.get("claude") if isinstance(record.get("claude"), dict) else {}
    if args.action == "token":
        key = str(claude.get("apiKey") or "")
        if not key:
            print("bart: not connected; run `engelbart auth`",
                  file=sys.stderr)
            return 1
        print(key)
        return 0
    if args.action == "status":
        print("account   " + (str(record.get("email") or "connected")
                              if record.get("token") else "signed out"))
        if claude.get("apiKey"):
            spent = float(claude.get("spendUsd") or 0)
            budget = float(claude.get("budgetUsd") or 0)
            print(f"credits   ${spent:.2f} used of ${budget:.2f}")
            models = [str(item) for item in (claude.get("models") or [])
                      if isinstance(item, str)]
            if models:
                print("models    " + ", ".join(models))
        else:
            print("credits   not connected")
        return 0

    # ``start`` is handled above before account parsing.
    return 2


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
