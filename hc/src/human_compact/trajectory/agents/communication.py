"""Small, result-grounded Bart messages. No model calls or polling side effects."""
import hashlib
import json
import re

from .. import chat_state as CS, goals as GM, build as BUILD
from . import events as EV
from . import presentation as PUBLIC

# Mechanics belong in Terminal, including provider errors and browser locators.
TECHNICAL = re.compile(r'(?i)(https?://|HTTP\s*\d|playwright|locator|traceback|exit.code|'
                       r'\{\s*"|acceptance|verifier|overseer|agent|repair \d|[/\\][\w.-]+[/\\])')


def plain(value):
    text = ' '.join(str(value or '').split())
    return PUBLIC.text(text, 300).rstrip(' .') if text and not TECHNICAL.search(text) else ''


def subject(session, root, goal, rows, evidence=None):
    criteria = (evidence or {}).get('acceptance') or {}
    for rid in rows:
        criterion = plain((criteria.get(rid) or {}).get('criterion'))
        if criterion:
            return criterion
    goals, _ = CS.load_goals(session, root)
    piece = GM.by_id(goals, goal) or {}
    names = [plain(r.get('text')) for r in piece.get('todo_items', []) if r.get('id') in rows]
    return next((n for n in names if n), plain(piece.get('title')) or 'this todo')


def summary(kind, topic, reason=''):
    issue = plain(reason)
    if kind == 'repair':
        return (f'The check found: {issue}. I’m fixing it.' if issue else
                f'The check found a problem with “{topic}”. I’m fixing it.')
    if kind == 'done':
        return f'“{topic}” now passes its check.'
    if kind == 'failed':
        return f'I couldn’t finish “{topic}”. The details are in Terminal.'
    return (f'“{topic}” still doesn’t pass its check. '
            'Would you like me to try a different approach, or leave it for now?')


def publish(session, root, goal, kind, text, problem=''):
    """Stable identity per build and meaningful problem; repeated checks stay quiet."""
    # Routine run updates belong to TODO activity and Terminal, not conversation.
    if kind in {"done", "repair", "failed"}:
        return None
    starts = EV.read(session, root, types=['todo.build_requested'], subgoal_id=goal, limit=1)
    cycle = starts[-1]['id'] if starts else (BUILD.load_run(session, root, goal) or {}).get('claude_session_id', 'current')
    key = hashlib.sha256(json.dumps([goal, cycle, kind, problem], ensure_ascii=False).encode()).hexdigest()[:24]
    return CS.append_bart_message(session, goal, text, root, message_id='sys-life-' + key)


def check_label(check):
    kind=check.get("kind")
    if kind in ("control", "control_value"):
        target=f"{check.get('name', 'Requested')} {check.get('role', 'control')}"
        if kind=="control": return target + (" is visible" if check.get("visible",True) else " is hidden")
        suffix={"empty":" is empty", "nonempty":" has a value", "equals":" has the expected value", "contains":" contains the expected value"}
        if check.get("from_file"): return target + " matches " + check["from_file"]
        return target+suffix.get(check.get("match"), " has the expected value")
    if kind=="layout": return " and ".join(c.get("name", "control") for c in check.get("controls", []))+" are side by side"
    if kind=="text": return "Page shows “"+PUBLIC.text(check.get("text"),100)+"”"
    return str(check.get("path") or "File") + {"file_exists":" exists", "file_nonempty":" is not empty", "file":" contains the expected content"}.get(kind, " satisfies its check")


def evidence_to_terminal(session, root, goal, evidence):
    # Full observations remain in the existing debugger and event store.
    from ... import telemetry
    with telemetry.operation("verification.evidence", "processing") as op:
        op.snapshot("processing_output", evidence)
    artifact=evidence.get("artifact") or {}
    for result in (artifact.get("page") or {}).get("checks", []):
        BUILD.note_activity(session,root,goal,"verify",("✓ " if result.get("passed") else "✗ ")+check_label(result.get("expected") or {}))
    for result in artifact.get("files", []):
        BUILD.note_activity(session,root,goal,"verify",("✓ " if result.get("passed") else "✗ ")+check_label(result.get("expected") or {"path":result.get("path")}))
    startup=evidence.get("preview_startup") or {}
    if startup:
        BUILD.note_activity(session,root,goal,"verify", "Started preview" if startup.get("ok") else "Preview startup failed: "+PUBLIC.text(startup.get("error") or startup.get("reason"),160))


def terminal_lines(lines):
    """Product projection; historical raw evidence remains in developer storage."""
    out=[]
    for line in lines:
        text=str(line.get("text") or "")
        if text.startswith("check evidence:") or text.lstrip().startswith(("{", "}", "[", "]")): continue
        text=re.sub(r"repair \d+ of \d+:\s*", "Fixing: ", text, flags=re.I)
        text=re.sub(r"verification failed \d+ times; asking you", "Checks still fail; your input is needed", text)
        text=PUBLIC.text(text,300)
        if text: out.append(dict(line,text=text))
    return out


def pending(session, root, goal):
    events = EV.read(session, root, types=['chat.needs_human', 'human.answered',
                     'todo.build_requested'], subgoal_id=goal, limit=1)
    return events[-1] if events and events[-1]['type'] == 'chat.needs_human' else None
