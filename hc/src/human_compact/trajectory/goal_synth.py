"""Goal inference: full-tree rebuild over cached extractions, plus incremental
classification of each newly analyzed conversation into the existing tree."""
import json

REBUILD_PROMPT = """You will construct the FULL GOAL TREE for a user from
structured extractions of their recent conversations. This is inference from
behavior; be conservative.

Rules — follow strictly:
- Distinguish GOALS from tasks. A multi-hour debugging effort ("fix telemetry
  mismatch") is a task or todo under a goal, never a top-level goal, no matter
  how much time it consumed. Raw frequency never determines importance.
- Prefer FEW top-level goals (1-4). Create subgoals only where evidence
  supports real decomposition; do not manufacture deep trees. Max depth 3.
- Evidence that counts: explicitly stated objectives, TODO-like statements,
  repeated work toward the same outcome, decisions, unresolved questions,
  project/repository identity (cwd), and the USER CORRECTIONS below, which
  override your inference.
- todos: concrete next actions stated or clearly implied; mark done=true only
  when completion is evidenced. Unresolved questions may become todos.
- Every goal needs evidence_ids copied from the extractions (never invented).
- Titles: short imperative phrases. status is active|completed|abandoned.
- description: one sentence saying what finishing this goal would mean, in the
  user's own framing. Subgoals need one as much as top-level goals do — a
  title alone rarely says why the work exists. Leave it empty only when the
  evidence genuinely does not say.

Return ONLY minified JSON:
{"goals":[{"id":"g1","title":"","status":"active","parent_goal_id":null,
 "description":"","evidence_ids":[],
 "todos":[{"text":"","done":false,"evidence_ids":[]}]}]}
Assign ids g1,g2,… and reference parents by those ids.

<<CORRECTIONS>>
Extractions (oldest first):
<<EXTRACTIONS>>"""

CLASSIFY_PROMPT = """Classify this ONE newly analyzed conversation into the
user's existing goal tree. Prefer attaching to existing goals. Return ONLY
minified JSON: {"operations":[...],"note":""} where each operation is one of:
 {"op":"attach_evidence","goal_id":"","evidence_ids":[]}
 {"op":"add_todo","goal_id":"","text":"","evidence_ids":[]}
 {"op":"complete_todo","goal_id":"","text_match":""}
 {"op":"set_status","goal_id":"","status":"active|completed|abandoned"}
 {"op":"new_goal","parent_goal_id":"<id or null>","title":"","description":"",
  "evidence_ids":[],"todos":[],"distinct_because":""}

Rules:
- A new TOP-LEVEL goal (parent null) requires an explicitly stated distinct
  objective — fill distinct_because with the stated evidence. Time spent or
  message volume is NEVER sufficient. A bug hunt, refactor, tooling errand, or
  experiment belongs under an existing goal as evidence or a todo/subgoal.
- If nothing fits and nothing is distinct, attach_evidence to the closest
  goal with a cautious note. Empty operations is valid.

CURRENT GOAL TREE:
<<TREE>>
NEW CONVERSATION EXTRACTION:
<<EXTRACTION>>"""

DESCRIBE_PROMPT = """Write the missing one-sentence description for each goal
below, using that goal's own evidence — the user's own messages.

Rules:
- One sentence. Say what finishing this goal would mean, in the user's framing.
- Describe only what the evidence shows. Never invent scope, deadlines, or
  motives the messages do not state.
- A description must not restate the title. It says why the work exists.
- Omit any goal whose evidence does not support a description. A missing entry
  is always better than a plausible guess.

Return ONLY minified JSON: {"descriptions":{"<goal id>":"<one sentence>"}}

GOALS NEEDING DESCRIPTIONS:
<<GOALS>>"""

NL_PROMPT = """You translate a user's natural-language feedback about their
GOAL TREE into goal correction operations. Reply ONLY with minified JSON:
{"interpretation":["short bullet per distinct correction"],
 "operations":[
  {"op":"move_goal","goal_id":"","new_parent_id":"<id or null>"} |
  {"op":"merge_goals","from_id":"","into_id":""} |
  {"op":"set_status","goal_id":"","status":"active|completed|abandoned"} |
  {"op":"rename_goal","goal_id":"","title":""} |
  {"op":"demote_to_todo","goal_id":"","parent_goal_id":""} |
  {"op":"add_todo","goal_id":"","text":""} |
  {"op":"mark_important","text":"","goal_id":"<id or null>","why":""} |
  {"op":"attach_important","item_id":"","goal_id":""}]}

Use ONLY the goal/item ids shown. "I finished that" -> set_status completed or
complete-style todo. "not a separate goal, belongs under X" -> move_goal or
demote_to_todo. "these two are the same" -> merge_goals. "this decision is
important" -> mark_important. Faithful, concise interpretation bullets.

GOAL TREE:
<<TREE>>
IMPORTANT ITEMS:
<<ITEMS>>
USER FEEDBACK: <<RAW>>"""


def tree_digest(goals):
    out = []
    for g in goals["goals"]:
        todos = "; ".join(("[x] " if t["done"] else "[ ] ") + t["text"]
                          for t in g["todos"][:4])
        # Show the description: a goal the user has already framed in their own
        # words is the one classification should attach to, not duplicate.
        desc = " ".join(str(g.get("description") or "").split())[:160]
        out.append(f"{g['id']} (parent={g.get('parent_goal_id')}) "
                   f"[{g['status']}] {g['title']}"
                   + (f" — {desc}" if desc else "")
                   + (f" | todos: {todos}" if todos else ""))
    return "\n".join(out) or "(empty)"


def compact_extraction(e):
    return {k: v for k, v in e.items()
            if k in ("conversation_id", "date", "cwd", "apparent_objectives",
                     "projects_or_topics", "actions_taken", "decisions",
                     "blockers", "unresolved_questions", "evidence",
                     "low_evidence")}


def rebuild(provider, extractions, corrections_text=None):
    cor = ""
    if corrections_text:
        cor = ("USER CORRECTIONS (override inference):\n" + corrections_text + "\n\n")
    prompt = (REBUILD_PROMPT.replace("<<CORRECTIONS>>", cor)
              .replace("<<EXTRACTIONS>>",
                       json.dumps([compact_extraction(e) for e in extractions])))
    data = provider.generate_json(prompt)
    if not isinstance(data.get("goals"), list):
        raise ValueError("goal synthesis response is missing the goals array")
    return {"version": 1, "goals": data["goals"]}


def describe(provider, goals, evidence_index, max_excerpts=6):
    """Fill only empty descriptions; every other field is left untouched.

    A full rebuild would produce descriptions too, but at the cost of every
    hand-authored note, priority, and prompt link in the tree. Goals whose
    description the model cannot ground in evidence stay empty.
    """
    blanks = [g for g in goals["goals"]
              if not str(g.get("description") or "").strip()]
    if not blanks:
        return {}
    rows = []
    for g in blanks:
        excerpts = []
        for eid in g.get("evidence_ids", []):
            record = evidence_index.get(eid) if isinstance(evidence_index, dict) else None
            if isinstance(record, dict) and record.get("role") == "user":
                excerpts.append(" ".join(str(record.get("text") or "").split())[:200])
            if len(excerpts) >= max_excerpts:
                break
        rows.append({"id": g["id"], "title": g.get("title", ""),
                     "todos": [t.get("text", "") for t in g.get("todos", [])][:5],
                     "user_messages": excerpts})
    data = provider.generate_json(
        DESCRIBE_PROMPT.replace("<<GOALS>>", json.dumps(rows, ensure_ascii=False)))
    proposed = data.get("descriptions")
    if not isinstance(proposed, dict):
        raise ValueError("description response is missing the descriptions map")
    blank_ids = {g["id"] for g in blanks}
    return {gid: " ".join(str(text).split())[:600]
            for gid, text in proposed.items()
            if gid in blank_ids and isinstance(text, str) and text.strip()}


def classify(provider, goals, extraction):
    prompt = (CLASSIFY_PROMPT.replace("<<TREE>>", tree_digest(goals))
              .replace("<<EXTRACTION>>", json.dumps(compact_extraction(extraction))))
    return provider.generate_json(prompt)


def translate_nl(provider, goals, important, raw):
    items = "\n".join(f"{i['id']}: {i['text'][:70]}" for i in important["items"]) or "(none)"
    prompt = (NL_PROMPT.replace("<<TREE>>", tree_digest(goals))
              .replace("<<ITEMS>>", items).replace("<<RAW>>", raw))
    return provider.generate_json(prompt)
