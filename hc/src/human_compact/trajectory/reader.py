"""Who is at the keyboard, and how much jargon they want back.

Nearly every sentence Engelbart shows a reader was written by a model: the
questions setup asks, the plan it proposes, the answers the Understanding
tab gives, the prompt a build opens on and the notes it leaves behind. All
of it is pitched, by default, at whoever wrote the prompt -- which is to
say at somebody who already knows what a branch, a migration and a
subprocess are. A second-year who has never written code reads the same
sentence and learns nothing from it, and the tool reads as something built
for other people.

So the reader is asked four things, once, before anything else happens:
their name, their year, what they study, and how technical they want
explanations to be. The answers belong to the account rather than to any
one chat -- they are the same person in every project -- so they live in
one file at the top of the vault, beside the Supabase config, and are
appended to every prompt the tool sends.

Nothing here is required and nothing here is a gate. A profile nobody
filled in renders as no lines at all, and every surface behaves exactly as
it did before it existed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .secure_io import atomic_write_json

FILE_NAME = "reader.json"

MAX_NAME = 60
MAX_YEAR = 40
MAX_MAJOR = 80

# How technical to be, in three stops. Named rather than numbered because
# the number is the slider's business and would mean nothing in a prompt.
PLAIN = "plain"
SOME = "some"
FULL = "full"
LEVELS = (PLAIN, SOME, FULL)

# What each stop is called on the page. Kept here as well as in the browser
# so a profile read back from disk can be shown without the page having to
# remember which number meant what.
LEVEL_NAMES = {
    PLAIN: "Plain language",
    SOME: "Some technical detail",
    FULL: "Fully technical",
}

# The years, said as somebody would say them. A reader who typed their own
# answer keeps their own words -- "transferring", "fifth-year", "grad" are
# all things people are, and none of them is a number.
ORDINALS = {"1": "first-year", "2": "second-year",
            "3": "third-year", "4": "fourth-year"}

FOR = "# Who you are writing for"

# The instruction that actually does the work. Written as a rule about
# words rather than as a request to "be accessible", which every model
# already believes it is being.
LEVEL_RULES = {
    PLAIN: [
        "Write for somebody who has not programmed. Prefer the everyday",
        "word to the technical one; where a technical word is unavoidable,",
        "say what it means in the same sentence -- once, plainly, without",
        "turning the answer into a lesson. Name what a thing does rather",
        "than what it is called, and never use an acronym they did not use",
        "first.",
    ],
    SOME: [
        "Write for somebody who codes a little. Ordinary terms -- file,",
        "function, branch, server, test -- can stand on their own; anything",
        "narrower than that gets a few words saying what it is, the first",
        "time it appears.",
    ],
    FULL: [
        "Write for somebody fluent. Use the precise term and do not gloss",
        "it: an explanation they did not need is an explanation in their",
        "way.",
    ],
}


def blank() -> Dict[str, str]:
    return {"name": "", "year": "", "major": "", "level": ""}


def _one(value, cap: int) -> str:
    """One line of somebody's own words, or nothing.

    A list or a dict here is not a shorter answer, it is a different kind
    of thing, and ``str()`` would turn it into a Python repr -- which is
    exactly the sort of sentence these fields exist to keep out of a
    prompt. A number is allowed: a year arrives as one when the page sends
    JSON rather than a string.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).split())[:cap]


def normalize(value) -> Dict[str, str]:
    """Four strings, however they arrived.

    Everything here comes off a web page and goes into a prompt, so it is
    bounded and none of it is trusted. A level that is not one of the three
    is no level at all rather than a guess: the slider always has a
    position, so an unrecognised one means something else sent this.
    """
    value = value if isinstance(value, dict) else {}
    level = str(value.get("level") or "").strip().lower()
    return {
        "name": _one(value.get("name"), MAX_NAME),
        "year": _one(value.get("year"), MAX_YEAR),
        "major": _one(value.get("major"), MAX_MAJOR),
        "level": level if level in LEVELS else "",
    }


def answered(profile) -> bool:
    """Whether there is anything here at all. A profile with nothing in it
    is not a profile the page should skip past."""
    return any(normalize(profile).values())


# --- where it lives ---------------------------------------------------------

def _vault(root: Optional[Path] = None) -> Path:
    """The vault root, not the sessions directory under it.

    The same fold ``supabase_client`` does, and for the same reason:
    callers arrive holding different halves of one layout. A server derives
    its root from the session directory it was handed, so it passes
    ``<vault>/chat-sessions``; a page served with no chat behind it passes
    nothing and means the vault itself. Both have to land on one file, or a
    reader answers the questions in one place and is asked them again in
    the other.
    """
    from . import chat_state as CS
    base = CS._state_location(root)[1] if root is None else Path(root)
    return base.parent if base.name == "chat-sessions" else base


def path(root: Optional[Path] = None) -> Path:
    return _vault(root) / FILE_NAME


def load(root: Optional[Path] = None) -> Dict[str, str]:
    """What they answered, or four empty strings. Never raises: a damaged
    file must not be able to stop a build, only to stop personalising it."""
    try:
        value = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return blank()
    return normalize(value)


def save(value, root: Optional[Path] = None) -> Dict[str, str]:
    """Write the profile where every surface reads it."""
    held = normalize(value)
    where = path(root)
    where.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(where, held, root=where.parent)
    return held


def remember(value, root: Optional[Path] = None) -> Dict[str, Any]:
    """Keep it here, and put it on the account where it can follow them.

    Local first and local always: the file is what every prompt reads, and
    a reader who has never signed in still gets prompts in their own
    register. The push to Supabase is the copy that survives a new laptop,
    so a failure there is reported alongside a profile that was saved --
    never instead of one.
    """
    held = normalize(value)
    kept = False
    error = ""
    try:
        save(held, root)
        kept = True
    except OSError as exc:
        # The one failure worth stopping the page for: every prompt reads
        # the file, so answers that did not reach it change nothing.
        error = "could not save that here: %s" % " ".join(str(exc).split())[:120]
    synced = False
    try:
        from . import supabase_client as SB
        SB.set_reader_profile(held, root)
        synced = True
    except Exception:                                        # noqa: BLE001
        # Not signed in, no config, no network, a schema without the
        # columns yet: none of them is a reason to lose what they typed, or
        # to say anything about it. The file is what the tool reads.
        synced = False
    return {"ok": kept, "profile": held, "synced": synced, "error": error}


# --- what goes into a prompt ------------------------------------------------

def who(profile) -> str:
    """One sentence naming the person the answer is for."""
    profile = normalize(profile)
    name = profile["name"]
    year = ORDINALS.get(profile["year"], profile["year"])
    major = profile["major"]
    if year and major:
        said = "a %s studying %s" % (year, major)
    elif year:
        said = "a %s" % year
    elif major:
        said = "studying %s" % major
    else:
        said = ""
    if name and said:
        return "%s, %s." % (name, said)
    if name:
        return "Their name is %s." % name
    if said:
        return "They are %s." % said
    return ""


def lines(profile) -> List[str]:
    """The block appended to every prompt, or nothing at all.

    Nothing at all is the point of the empty case: a reader who skipped the
    questions should get exactly the prompts the tool sent before this
    existed, not a paragraph apologising for knowing nothing about them.
    """
    profile = normalize(profile)
    said = who(profile)
    rule = LEVEL_RULES.get(profile["level"]) or []
    if not said and not rule:
        return []
    out = ["", FOR, ""]
    if said:
        out.append(said)
        if rule:
            out.append("")
    out += rule
    return out


def prompt_lines(root: Optional[Path] = None) -> List[str]:
    """The same block, read from disk. What the four surfaces call."""
    return lines(load(root))
