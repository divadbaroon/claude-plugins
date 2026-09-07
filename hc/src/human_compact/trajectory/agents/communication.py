"""Small, result-grounded Bart messages. No model calls or polling side effects."""
import hashlib
import json
import re

from .. import chat_state as CS, goals as GM, build as BUILD
from . import events as EV

# Mechanics belong in Terminal, including provider errors and browser locators.
TECHNICAL = re.compile(r'(?i)(https?://|HTTP\s*\d|playwright|locator|traceback|exit.code|'
                       r'\{\s*"|acceptance|verifier|overseer|agent|repair \d|[/\\][\w.-]+[/\\])')


def plain(value):
    text = ' '.join(str(value or '').split())
    return text[:300].rstrip(' .') if text and not TECHNICAL.search(text) else ''


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
    starts = EV.read(session, root, types=['todo.build_requested'], subgoal_id=goal, limit=1)
    cycle = starts[-1]['id'] if starts else (BUILD.load_run(session, root, goal) or {}).get('claude_session_id', 'current')
    key = hashlib.sha256(json.dumps([goal, cycle, kind, problem], ensure_ascii=False).encode()).hexdigest()[:24]
    return CS.append_bart_message(session, goal, text, root, message_id='sys-life-' + key)


def evidence_to_terminal(session, root, goal, evidence):
    text = json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
    for line in text[:20000].splitlines():
        for start in range(0, max(1, len(line)), 180):
            BUILD.note_activity(session, root, goal, 'verify', 'check evidence: ' + line[start:start+180])


def pending(session, root, goal):
    events = EV.read(session, root, types=['chat.needs_human', 'human.answered',
                     'todo.build_requested'], subgoal_id=goal, limit=1)
    return events[-1] if events and events[-1]['type'] == 'chat.needs_human' else None
