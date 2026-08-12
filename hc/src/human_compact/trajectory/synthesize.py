"""Cross-conversation synthesis: infer the primary CURRENT goal (explicitly an
inference from recent history, never a claim of objective knowledge)."""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import discover as D
from .secure_io import atomic_write_json, secure_dir

SYNTH_VERSION = "6"

PROMPT = """You are synthesizing structured extractions from a user's Claude Code
conversations over the last {days} days into a model of what they appear to be
working toward. This is INFERENCE from recent history, not fact.

Inference rules — follow strictly:
- Weight evidence by recurrence across conversations, recency, depth of
  engagement, explicit statements of intent, and whether the user took actions
  (not merely discussed).
- Entries marked low_evidence carry less weight, EXCEPT an explicit statement
  of intent in a short conversation, which can matter a lot.
- Do not confuse: one-off curiosity with a goal; recurring interest with an
  active objective; something Claude suggested with something the user wants;
  a topic with progress.
- Be conservative. Use language like "Your conversations suggest…", "You
  appear to be working toward…". Never percentages, never certainty.
- Every claim's evidence_ids must be turn ids copied from the extractions'
  evidence lists. Discard any observation you cannot ground in ids.
- subgoal status must be exactly one of: "Active", "Emerging", "Blocked",
  "Underexplored", "Recently advanced".

Return ONLY minified JSON with exactly these keys:
{{"objectives":[{{"rank":1,"label":"","level":"broader_goal|objective|project","status":"primary|active|background","evidence_ids":[]}}],
 "scope":{{"label":"","evidence_ids":[]}},
 "current_objective":{{"label":"","evidence_ids":[]}},
 "context_lens":{{
   "preserve":{{"allocation":0,"items":[{{"label":"","reason":"","evidence_ids":[]}}]}},
   "active_context":{{"allocation":0,"items":[{{"label":"","reason":"","evidence_ids":[]}}]}},
   "safe_to_compress":{{"allocation":0,"items":[{{"label":"","reason":"","evidence_ids":[]}}]}}}}}}

Field rules:
- objectives: the user's ranked ACTIVE objective stack, at most 3. rank is
  ordinal (1 = most central) — never percentages or probabilities. level is
  one of broader_goal, objective, project. status: exactly one "primary"
  (normally rank 1); others "active" or "background". One sentence each.
- current_objective: the objective RELEVANT TO THE CURRENT SCOPE (usually
  the primary, or the project-level objective matching scope) — the lens is
  conditioned on this one. Other globally active objectives must NOT leak
  into preserve or active_context merely because they matter elsewhere;
  within this scope they belong in safe_to_compress.
- scope.label: the inferred CURRENT project/workstream this lens is
  conditioned on (a few words). The lens answers: given what the user is
  doing NOW, how should limited context capacity be allocated? Global
  importance does not imply local relevance — work unrelated to the current
  scope belongs in safe_to_compress regardless of its overall importance.
- current_objective.label: ONE sentence, at most 15 words.
- allocation: integer percentages that MUST sum to exactly 100. They are
  soft context-budget weights — the approximate share of post-compaction
  capacity each category deserves for the current scope. They are
  priorities/ceilings, not quotas (a compactor may borrow unused capacity).
  NEVER derive them from conversation frequency alone: one explicit
  architectural constraint can outweigh twenty repetitive debugging
  messages. Weigh importance to the objective, unresolved status, cost of
  re-derivation if forgotten, recency, recurrence, active-vs-resolved, and
  explicit user corrections. These are NOT confidence scores.
- preserve.items: 3-5 — loss would impair continuation: unresolved product
  questions, decisions+rationale constraining future work, invariants.
- active_context.items: 3-5 — current implementation state, active
  experiments, relevant recent workstreams; may be summarized.
- safe_to_compress.items: 2-4 — resolved debugging, setup chatter,
  repetitive exploration, abandoned approaches, work outside the scope.
- Labels one line (max 12 words); reason one clause; evidence_ids copied
  from extractions (never invented).

Extractions (oldest first):
{extractions}"""


def synthesize(extractions, provider, days, outdir: Path, meta, corrections_text=None):
    compact = [{k: v for k, v in e.items() if k != "cwd"} for e in extractions]
    prompt = PROMPT.format(days=days, extractions=json.dumps(compact))
    if corrections_text:
        prompt += ("\n\nUSER CORRECTIONS — the user has explicitly stated these; "
                   "honor them over your own inference:\n" + corrections_text)
    data = provider.generate_json(prompt)
    lens = data.get("context_lens") or {}
    allocs = {k: max(0, int((lens.get(k) or {}).get("allocation", 0) or 0))
              for k in ("preserve", "active_context", "safe_to_compress")}
    total = sum(allocs.values())
    if total and total != 100:                    # normalize, largest remainder
        scaled = {k: v * 100.0 / total for k, v in allocs.items()}
        floored = {k: int(v) for k, v in scaled.items()}
        for k in sorted(scaled, key=lambda k: floored[k] - scaled[k])[:100 - sum(floored.values())]:
            floored[k] += 1
        for k, v in floored.items():
            if k in lens and isinstance(lens[k], dict):
                lens[k]["allocation"] = v
    analysis = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": days,
        "sessions_analyzed": len(extractions),
        "providers": meta,
        "synth_version": SYNTH_VERSION,
        "analysis": data,
    }
    secure_dir(outdir, D.VAULT)
    atomic_write_json(outdir / "analysis.json", analysis, root=D.VAULT)
    return analysis
