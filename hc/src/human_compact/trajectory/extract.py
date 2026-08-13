"""Per-conversation structured extraction, cached reproducibly.

Cache key = sha256(input payload) + extractor/schema/prompt versions +
provider identity — so changing the inference method, model, or provider
automatically invalidates prior derived analyses.
"""
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import discover as D
from .secure_io import atomic_write_json, secure_dir

EXTRACTOR_VERSION = "1"
SCHEMA_VERSION = "1"

PROMPT = """You are analyzing one conversation between a user and Claude Code.
Each turn below has a stable id in [brackets]. Extract a compact structured
representation of what the USER appears to be doing. Do not confuse things
Claude suggested with things the user wants; weight the user's own words.

Return ONLY minified JSON with exactly these keys:
{"conversation_id":"", "date":"", "apparent_objectives":[], "projects_or_topics":[],
 "actions_taken":[], "decisions":[], "blockers":[], "unresolved_questions":[],
 "artifacts_or_outputs":[], "evidence":[{"id":"", "excerpt":""}]}

Rules: every non-empty claim must be supportable by the turns; "evidence" lists
the specific turn ids that ground your claims, each with a short verbatim-ish
excerpt (max 120 chars) from that turn. Use only ids that appear below. Empty
lists are fine and preferred over speculation.

Conversation (<<DATE>>, id <<CID>>, <<N>> user turns<<LOW>>):
<<TURNS>>"""


def prompt_sha():
    return hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


def _payload(sess):
    turns = "\n".join(f"[{t['id']}] {t['role']}: {t['text']}" for t in sess["turns"])
    return (PROMPT.replace("<<DATE>>", sess["date"])
                  .replace("<<CID>>", sess["session_id"][:8])
                  .replace("<<N>>", str(sess["user_turn_count"]))
                  .replace("<<LOW>>", " — LOW EVIDENCE, extract only what is explicit" if sess["low_evidence"] else "")
                  .replace("<<TURNS>>", turns))


def cache_key(sess, provider):
    h = hashlib.sha256()
    h.update(_payload(sess).encode())
    h.update(f"|x{EXTRACTOR_VERSION}|s{SCHEMA_VERSION}|p{prompt_sha()}|{provider.identity()}".encode())
    return h.hexdigest()


def _finish(sess, data, key, provider, cache_file: Path):
    data["conversation_id"] = sess["session_id"]
    data["date"] = sess["date"]
    data["user_turn_count"] = sess["user_turn_count"]
    data["low_evidence"] = sess["low_evidence"]
    data["cwd"] = sess["cwd"]
    atomic_write_json(cache_file,
        {"cache_key": key, "provider": provider.identity(),
         "versions": {"extractor": EXTRACTOR_VERSION, "schema": SCHEMA_VERSION,
                      "prompt": prompt_sha()},
         "extracted": data}, root=D.VAULT)
    return data


def extract_all(sessions, provider, outdir: Path, refresh=False, log=print, workers=4):
    """Map stage. Cache hits are resolved up front (no worker, no API call);
    the misses run under bounded concurrency, higher-evidence sessions first.
    Failures are collected, not fatal. Returns (results, failures)."""
    secure_dir(outdir, D.VAULT)
    total = len(sessions)
    results, failures, misses = [], [], []
    counter = {"n": 0}
    lock = threading.Lock()

    def tick(word, sess, extra=""):
        with lock:
            counter["n"] += 1
            log(f"  {word:<10s} [{counter['n']:>2d}/{total}] "
                f"{sess['session_id'][:8]} ({sess['date']}, {sess['user_turn_count']} turns){extra}")

    for sess in sessions:
        key = cache_key(sess, provider)
        cache_file = outdir / f"{sess['session_id']}.json"
        if not refresh and cache_file.is_file():
            try:
                cached = json.loads(cache_file.read_text())
                if cached.get("cache_key") == key:
                    results.append(cached["extracted"])
                    tick("cached", sess)
                    continue
            except (json.JSONDecodeError, KeyError):
                pass
        misses.append((sess, key, cache_file))

    # higher-evidence conversations first
    misses.sort(key=lambda m: (m[0]["low_evidence"], -m[0]["user_turn_count"]))

    def job(m):
        sess, key, cache_file = m
        tick("extracting", sess)
        # The UI polls this to say what is being analyzed right now. Without
        # it a long extraction is indistinguishable from a hung one.
        try:
            from . import state as _state
            _state.set_processing(sess["session_id"], phase="extracting")
        except Exception:                            # noqa: BLE001 - advisory
            pass
        data = provider.generate_json(_payload(sess))
        return _finish(sess, data, key, provider, cache_file)

    if misses:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futs = {pool.submit(job, m): m for m in misses}
            for fut in as_completed(futs):
                sess = futs[fut][0]
                try:
                    results.append(fut.result())
                except Exception as e:               # noqa: BLE001
                    failures.append((sess["session_id"], str(e)))
                    with lock:
                        log(f"  FAILED     {sess['session_id'][:8]}: {e}")
    if failures:
        log(f"  ⚠ {len(failures)} of {total} conversations failed extraction and are "
            f"excluded from synthesis")
    return results, failures
