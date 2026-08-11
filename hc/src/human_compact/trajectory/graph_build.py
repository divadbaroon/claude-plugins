"""Build the evidence-cloud graph from cached extractions + synthesis.

Nodes are meaningful evidence units (conversations, decisions, actions,
recurring questions, statements of intent, artifacts) — never raw messages.
Recurring items across conversations MERGE into one node whose weight is its
recurrence, which is what makes clusters emerge under ForceAtlas2.
Derived layer: regenerable, raw Vault untouched.
"""
import json
import re
from pathlib import Path

PALETTE = ["#8b5cf6", "#14b8a6", "#d97706", "#e11d48", "#64748b", "#2563eb"]
ITEM_FIELDS = [("decisions", "decision"), ("actions_taken", "action"),
               ("unresolved_questions", "question"),
               ("apparent_objectives", "intent"),
               ("artifacts_or_outputs", "artifact")]
TARGET_MAX = 80
STOP = set("the a an of to for and in on with that this is are was were be been "
           "it its into from as at by or not no do does did user claude".split())


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def build(extractions, analysis, outdir: Path):
    convs, items = [], []
    for e in extractions:
        sid8 = e["conversation_id"][:8]
        ev_ids = [x.get("id") for x in e.get("evidence", []) if x.get("id")]
        convs.append({"id": f"c:{sid8}", "type": "conversation",
                      "label": (e.get("apparent_objectives") or
                                e.get("projects_or_topics") or ["conversation"])[0][:60],
                      "date": e["date"], "weight": max(1, e.get("user_turn_count", 1)),
                      "evidence_ids": ev_ids, "sid8": sid8})
        for field, typ in ITEM_FIELDS:
            for text in e.get(field, []) or []:
                if not isinstance(text, str) or len(text) < 6:
                    continue
                toks = _tokens(text)
                for it in items:
                    if it["type"] == typ and _jaccard(toks, it["toks"]) >= 0.45:
                        it["occ"].append({"conv": f"c:{sid8}", "date": e["date"]})
                        it["evidence_ids"].extend(ev_ids[:2])
                        if len(text) < len(it["label"]):
                            it["label"] = text
                        break
                else:
                    items.append({"type": typ, "label": text, "toks": toks,
                                  "occ": [{"conv": f"c:{sid8}", "date": e["date"]}],
                                  "evidence_ids": ev_ids[:2]})

    # prune to target size: recurring items and questions/decisions/intents first
    def keep_score(it):
        pri = {"question": 3, "decision": 2, "intent": 2, "action": 1, "artifact": 1}
        return (len(it["occ"]), pri.get(it["type"], 0))
    items.sort(key=keep_score, reverse=True)
    items = items[:max(0, TARGET_MAX - len(convs))]

    nodes, edges = [], []
    for c in convs:
        nodes.append({k: c[k] for k in ("id", "type", "label", "date", "weight", "evidence_ids")})
    for i, it in enumerate(items):
        nid = f"i:{i}"
        dates = sorted(o["date"] for o in it["occ"])
        nodes.append({"id": nid, "type": it["type"], "label": it["label"][:70],
                      "date": dates[-1], "date_first": dates[0],
                      "weight": len(it["occ"]),
                      "evidence_ids": list(dict.fromkeys(it["evidence_ids"]))[:6]})
        for o in it["occ"]:
            edges.append({"source": nid, "target": o["conv"]})

    # clusters: map nodes to subgoals via evidence-id overlap; majority vote for items
    a = analysis.get("analysis", analysis)
    subs = a.get("subgoals", [])[:len(PALETTE)]
    sub_ev = [set(s.get("evidence_ids", [])) for s in subs]

    def cluster_of(ev_ids):
        best, bi = 0, -1
        for j, se in enumerate(sub_ev):
            n = len(se & set(ev_ids))
            if n > best:
                best, bi = n, j
        return bi
    conv_cluster = {}
    for n in nodes:
        if n["type"] == "conversation":
            conv_cluster[n["id"]] = cluster_of(n["evidence_ids"])
            n["cluster"] = conv_cluster[n["id"]]
    for n in nodes:
        if n["type"] != "conversation":
            votes = [conv_cluster.get(e["target"], -1) for e in edges if e["source"] == n["id"]]
            votes = [v for v in votes if v >= 0]
            n["cluster"] = max(set(votes), key=votes.count) if votes else cluster_of(n["evidence_ids"])

    # label only the heavyweights
    for n in sorted(nodes, key=lambda x: -x["weight"])[:9]:
        n["show_label"] = True

    clusters = [{"id": j, "label": s.get("name", f"cluster {j}"),
                 "color": PALETTE[j % len(PALETTE)]} for j, s in enumerate(subs)]
    goals = [{"rank": 1, "label": a.get("primary_current_goal", {}).get("label", "")
              or a.get("primary_current_goal", {}).get("description", "")[:60],
              "cluster": cluster_of(a.get("primary_current_goal", {}).get("evidence_ids", [])),
              "evidence_ids": a.get("primary_current_goal", {}).get("evidence_ids", [])}]
    ranked = sorted(range(len(subs)),
                    key=lambda j: -sum(n["weight"] for n in nodes if n.get("cluster") == j))
    for r, j in enumerate(ranked[:2], start=2):
        goals.append({"rank": r, "label": subs[j].get("name", ""), "cluster": j,
                      "evidence_ids": subs[j].get("evidence_ids", [])})

    dates = sorted(n["date"] for n in nodes if n.get("date"))
    graph = {"nodes": nodes, "edges": edges, "clusters": clusters, "goals": goals,
             "date_min": dates[0] if dates else "", "date_max": dates[-1] if dates else "",
             "state": {"current": a.get("current_position", {}).get("description", ""),
                       "question": (a.get("unresolved_questions") or [{}])[0].get("question", ""),
                       "blocker": (a.get("blockers") or [{}])[0].get("description", ""),
                       "progress": (a.get("recent_progress") or [{}])[0].get("description", "")}}
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "graph.json").write_text(json.dumps(graph, indent=1))
    return graph
