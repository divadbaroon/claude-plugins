"""LLM providers. Explicit selection only — NEVER silently fall back between
local and remote. `claude` sends conversation-derived digests to Anthropic's
API via the user's own CLI; `ollama` stays entirely on-device."""
import json
import os
import re
import subprocess
import urllib.request

OLLAMA_URL = os.environ.get("HC_OLLAMA_URL", "http://localhost:11434")
CLAUDE_TIMEOUT_SECONDS = 180


class ProviderError(RuntimeError):
    pass


def _last_json_object(raw):
    """Return the last complete JSON object in a model response.

    Models occasionally abandon a malformed draft, then emit a corrected
    object. Taking the text from the first ``{`` through the last ``}`` joins
    those two drafts and forces a second expensive model call.
    """
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r"\{", raw):
        try:
            value, end = decoder.raw_decode(raw[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append((match.start() + end, -match.start(), value))
    if not candidates:
        raise json.JSONDecodeError("no complete JSON object", raw, 0)
    return max(candidates, key=lambda candidate: candidate[:2])[2]


class Base:
    kind = "base"
    def __init__(self, model):
        self.model = model
    def identity(self):
        return f"{self.kind}:{self.model}"
    def generate(self, prompt: str) -> str:
        raise NotImplementedError
    def generate_json(self, prompt: str):
        for attempt in (1, 2):
            raw = self.generate(prompt if attempt == 1 else
                                prompt + "\n\nReply with ONLY valid minified JSON. No prose, no code fences.")
            txt = raw.strip()
            if txt.startswith("```"):
                txt = "\n".join(l for l in txt.splitlines() if not l.startswith("```"))
            try:
                return _last_json_object(txt)
            except json.JSONDecodeError:
                continue
        raise ProviderError(f"{self.identity()} did not return parseable JSON")


class ClaudeCLI(Base):
    kind = "claude"
    def _run(self, prompt, *, structured=False):
        command = ["claude", "-p", "--safe-mode", "--model", self.model,
                   "--no-session-persistence"]
        if structured:
            # These are extraction/classification calls, not coding-agent
            # turns. Pinning effort prevents a user's interactive preference
            # (for example xhigh) from exhausting the subprocess deadline.
            command += ["--effort", "low", "--tools", ""]
        try:
            # Provider subprocesses are implementation details, not user chats.
            # Mark them so the always-on chat hook cannot recursively launch
            # another analyzer, and suppress the opt-in global Vault hook too.
            child_env = os.environ.copy()
            child_env["HC_CHAT_INFERENCE"] = "1"
            child_env.pop("CLAUDE_VAULT", None)
            r = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                timeout=CLAUDE_TIMEOUT_SECONDS, env=child_env)
        except FileNotFoundError:
            raise ProviderError("claude CLI not found on PATH")
        except subprocess.TimeoutExpired:
            raise ProviderError(
                f"claude CLI timed out after {CLAUDE_TIMEOUT_SECONDS}s")
        if r.returncode != 0:
            raise ProviderError(f"claude CLI failed: {r.stderr.strip()[:200]}")
        return r.stdout

    def generate(self, prompt):
        return self._run(prompt)

    def generate_json(self, prompt):
        # Avoid a second full model call when a large rebuild response includes
        # prose or a discarded draft before its corrected final object.
        raw = self._run(prompt, structured=True)
        try:
            return _last_json_object(raw)
        except json.JSONDecodeError as e:
            raise ProviderError(
                f"{self.identity()} did not return parseable JSON") from e


class Ollama(Base):
    kind = "ollama"
    def _post(self, path, payload):
        req = urllib.request.Request(
            OLLAMA_URL + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())
    def available(self):
        try:
            urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3)
            return True
        except OSError:
            return False
    def generate(self, prompt):
        if not self.available():
            raise ProviderError(
                "Ollama is not running at " + OLLAMA_URL +
                " — start it (`ollama serve`) or install: brew install ollama; "
                "ollama pull " + self.model + ". Refusing to fall back off-device.")
        try:
            return self._post("/api/generate",
                              {"model": self.model, "prompt": prompt,
                               "stream": False})["response"]
        except OSError as e:
            raise ProviderError(f"ollama request failed: {e}")


class Mock(Base):
    """Deterministic provider for tests/offline demos (HC_MOCK_DIR canned files).
    HC_MOCK_DELAY (s), HC_MOCK_FAIL_SUBSTR (fail calls whose prompt matches),
    and calls.log (dispatch order) support the concurrency tests."""
    kind = "mock"
    def generate_json(self, prompt):
        import time
        d = os.environ.get("HC_MOCK_DIR", "")
        which = ("goal_nl" if "goal correction operations" in prompt else
                 "goal_synth" if "Infer the current goal tree for ONE Claude Code chat" in prompt else
                 "goal_classify" if "Update the current goal state for ONE Claude Code chat" in prompt else
                 "goal_classify" if "Classify this ONE newly analyzed" in prompt else
                 "goal_synth" if "construct the FULL GOAL TREE" in prompt else
                 "nl_ops" if "correction operations" in prompt else
                 "synthesize" if ("current_objective" in prompt or "primary_current_goal" in prompt)
                 else "extract")
        if which == "extract":
            cid = ""
            for line in prompt.splitlines():
                if ", id " in line:
                    cid = line.split(", id ")[1].split(",")[0]; break
            with open(os.path.join(d, "calls.log"), "a") as f:
                f.write(cid + "\n")
            fail = os.environ.get("HC_MOCK_FAIL_SUBSTR")
            if fail and fail in prompt:
                raise ProviderError("simulated extraction failure")
            delay = float(os.environ.get("HC_MOCK_DELAY", "0") or 0)
            if delay:
                time.sleep(delay)
        with open(os.path.join(d, which + ".json")) as f:
            return json.load(f)
    def generate(self, prompt):
        if "Summarize this conversation" in prompt:
            d = os.environ.get("HC_MOCK_DIR", "")
            name = "summary_lens.txt" if "POLICY:" in prompt else "summary_default.txt"
            try:
                with open(os.path.join(d, name)) as f:
                    return f.read()
            except OSError:
                return "mock summary"
        return json.dumps(self.generate_json(prompt))


DEFAULTS = {"claude": {"extract": "haiku", "synthesize": "sonnet"},
            "ollama": {"extract": "llama3.1", "synthesize": "llama3.1"},
            "mock": {"extract": "mock", "synthesize": "mock"}}


def make(kind, stage, model=None):
    model = model or DEFAULTS[kind][stage]
    return {"claude": ClaudeCLI, "ollama": Ollama, "mock": Mock}[kind](model)
