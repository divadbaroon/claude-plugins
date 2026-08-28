"""LLM providers. Explicit selection only — NEVER silently fall back between
local and remote. `claude` sends conversation-derived digests to Anthropic's
API via the user's own CLI; `ollama` stays entirely on-device."""
import json
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from .secure_io import open_private_append

OLLAMA_URL = os.environ.get("HC_OLLAMA_URL", "http://localhost:11434")
CLAUDE_TIMEOUT_SECONDS = 180

# A call that has to find its answer in the project gets longer than one that
# has the answer quoted to it: grepping a repository, opening what the grep
# found and then writing the answer is three rounds where the others are one.
CLAUDE_SEARCH_TIMEOUT_SECONDS = 420

# What such a call may do: find files, find lines in them, read them. Nothing
# that writes, and nothing that runs -- the question was "what does the code
# do", and answering it never needs the code to be changed or executed.
SEARCH_TOOLS = "Read,Grep,Glob"


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
    def __init__(self, model, timeout=None):
        self.model = model
        # Callers with someone watching a spinner get to say how long that
        # someone should be made to watch it. None means the module default.
        self.timeout = timeout
    def identity(self):
        return f"{self.kind}:{self.model}"
    def generate(self, prompt: str) -> str:
        raise NotImplementedError
    def generate_plain(self, prompt: str) -> str:
        """One question in, one answer out -- no tools, no agent turn.

        The prompt already carries everything the answer is allowed to come
        from. A provider that would otherwise go looking through the project
        for it is spending a deadline on work nobody asked for.
        """
        return self.generate(prompt)
    def generate_reading(self, prompt: str, read_dirs=()) -> str:
        """One answer that may open the files the prompt names.

        The opposite of ``generate_plain``: a screenshot is not text, and the
        only way one reaches an answer is for the provider to open it. A
        provider that cannot open anything answers from the prompt's words
        alone, which is the same answer with the pictures missing.
        """
        return self.generate(prompt)
    def generate_searching(self, prompt: str, where="") -> str:
        """One answer that may go looking through the project for itself.

        For the question whose answer is in the code and nowhere else: the
        provider is pointed at a directory and left to find it. A provider
        with nothing to look with answers from the prompt alone, which is the
        same answer with the code missing.
        """
        return self.generate(prompt)
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


# Which credentials a claude subprocess runs on. The server inherits whatever
# shell started it, and an ANTHROPIC_API_KEY there makes the CLI bill the key
# and drop the claude.ai login (and its connectors) -- for work the reader
# pressed a button for in their own workspace, that is the wrong account.
# Stripped by default; HC_USE_API_KEY=1 keeps it for anyone who means it.
API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def subscription_env(base=None):
    """A copy of the environment that lets `claude` use the claude.ai login."""
    env = dict(os.environ if base is None else base)
    if os.environ.get("HC_USE_API_KEY") != "1":
        for name in API_KEY_VARS:
            env.pop(name, None)
    return env


class ClaudeCLI(Base):
    kind = "claude"
    def _run(self, prompt, *, structured=False, plain=False, read=None,
             search=""):
        command = ["claude", "-p", "--safe-mode", "--model", self.model,
                   "--no-session-persistence"]
        deadline = self.timeout or CLAUDE_TIMEOUT_SECONDS
        if search:
            # The answer is somewhere in that directory and the call has to
            # find it, so the tools are the ones that find things and read
            # them. The subprocess is started in the directory as well as
            # given it: a search rooted anywhere else is a search of the
            # wrong project.
            command += ["--tools", SEARCH_TOOLS,
                        "--allowed-tools", SEARCH_TOOLS,
                        "--add-dir", str(search)]
            deadline = self.timeout or CLAUDE_SEARCH_TIMEOUT_SECONDS
        elif read is not None:
            # Opening the named files is the whole of this call, so the tool
            # set is Read and nothing else -- allowed as well as available,
            # since a prompt nobody is sitting in front of cannot answer for
            # one. The directories holding the files are named too, because
            # the workspace keeps its screenshots outside whatever directory
            # this server happened to be started in.
            command += ["--tools", "Read", "--allowed-tools", "Read"]
            for folder in read:
                command += ["--add-dir", str(folder)]
        elif structured or plain:
            # Neither of these is a coding-agent turn: the prompt carries the
            # text the answer comes from, so a subprocess that starts reading
            # the project instead spends the deadline and comes back with
            # nothing to show for it.
            command += ["--tools", ""]
        if structured:
            # These are extraction/classification calls. Pinning effort
            # prevents a user's interactive preference (for example xhigh)
            # from exhausting the subprocess deadline.
            command += ["--effort", "low"]
        try:
            # Provider subprocesses are implementation details, not user chats.
            # Mark them so the always-on chat hook cannot recursively launch
            # another analyzer, and suppress the opt-in global Vault hook too.
            child_env = subscription_env()
            child_env["HC_CHAT_INFERENCE"] = "1"
            child_env.pop("CLAUDE_VAULT", None)
            r = subprocess.run(
                command, input=prompt, capture_output=True, text=True,
                timeout=deadline, env=child_env,
                cwd=str(search) if search else None)
        except FileNotFoundError:
            # Two things can be missing here once a call names a directory to
            # run in, and "install the CLI" is the wrong thing to say about
            # the other one.
            if search and not Path(search).is_dir():
                raise ProviderError(f"{search} is not a directory to look in")
            raise ProviderError("claude CLI not found on PATH")
        except NotADirectoryError:
            raise ProviderError(f"{search} is not a directory to look in")
        except subprocess.TimeoutExpired:
            raise ProviderError(f"claude CLI timed out after {deadline}s")
        if r.returncode != 0:
            raise ProviderError(f"claude CLI failed: {r.stderr.strip()[:200]}")
        return r.stdout

    def generate(self, prompt):
        return self._run(prompt)

    def generate_plain(self, prompt):
        return self._run(prompt, plain=True)

    def generate_reading(self, prompt, read_dirs=()):
        return self._run(prompt, read=list(read_dirs or ()))

    def generate_searching(self, prompt, where=""):
        return self._run(prompt, search=str(where or ""))

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
            with open_private_append(
                    Path(d) / "calls.log", secure_parent=False) as f:
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


def make(kind, stage, model=None, timeout=None):
    model = model or DEFAULTS[kind][stage]
    return {"claude": ClaudeCLI, "ollama": Ollama,
            "mock": Mock}[kind](model, timeout=timeout)
