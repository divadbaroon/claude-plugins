"""Bounded product prose; identifiers and full diagnostics stay internal."""
import re
IDS = re.compile(r"\b(g\d+(?:\.\d+)*[a-z0-9]*|t[a-f0-9]{6,}|[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12})\b", re.I)
def text(value, limit=1200, allow_ids=False):
    value=re.sub(r"[^\S\n]+", " ", str(value or "").replace("\r\n", "\n")).strip()
    if not allow_ids:
        value=IDS.sub(lambda m: "this subgoal" if m[0].lower().startswith("g") else "this todo" if m[0].lower().startswith("t") else "this run",value)
    if len(value)<=limit: return value
    bounded=value[:max(0,limit-1)]
    ends=list(re.finditer(r'[.!?](?:["”\']?)(?=\s)',bounded))
    if ends: return bounded[:ends[-1].end()]
    return bounded.rsplit(" ",1)[0].rstrip(" ,;:")+"…" if " " in bounded else "…"
def smalltalk(value):
    return bool(re.fullmatch(r"(?:hello|hi|hey|thanks|thank you|cool|okay|ok|great|nice)(?: bart| there| very much| a lot)?[.!?,\s]*",str(value or "").strip(),re.I))
def debug_requested(value):
    return bool(re.search(r"(?:debug|internal|developer).*(?:id|identifier)|(?:goal|todo|run) (?:id|identifier)",str(value),re.I))
def equivalent(value):
    return re.sub(r"[^\w]+"," ",str(value).lower()).strip()
