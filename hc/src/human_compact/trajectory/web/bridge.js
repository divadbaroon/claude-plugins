/* hc ui bridge: seeds the checked-in goal artifact from the Vault's own state
   before boot, maps server records onto the fields that artifact renders
   (prompts, ctx, agent, artifact), mirrors edits back through /api/import, and
   makes its add-source controls ask for a real value instead of appending a
   placeholder. The artifact is never forked: it is patched through its
   template island before the runtime unpacks it. */
(function () {
  "use strict";

  var KEY = "hc-vault-ui-v1";
  var SYNC_KEY = "hc-vault-ui-sync-v1";
  var serverState = { goals: [], prompts: [], runs: {}, scope: "global" };
  var stateFingerprint = null;
  var lastObservedGoals = null;
  var refreshPending = false;
  var syncBusy = false;
  var setupState = null;
  var details = Object.create(null);
  var detailPending = Object.create(null);

  function array(value) {
    return Array.isArray(value) ? value : [];
  }

  function str(value) {
    return typeof value === "string" ? value : "";
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function same(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
  }

  function readSync() {
    try {
      var value = JSON.parse(localStorage.getItem(SYNC_KEY) || "null");
      return value && typeof value.revision === "string" &&
        Array.isArray(value.goals) ? value : null;
    } catch (e) {
      return null;
    }
  }

  function writeSync(revision, goals) {
    try {
      localStorage.setItem(SYNC_KEY, JSON.stringify({
        revision: revision,
        goals: clone(goals)
      }));
    } catch (e) {}
  }

  function readLocalGoals() {
    try {
      var value = JSON.parse(localStorage.getItem(KEY) || "{}");
      return Array.isArray(value.goals) ? value.goals : [];
    } catch (e) {
      return [];
    }
  }

  function flattenTree(nodes) {
    var map = Object.create(null), order = [];
    function walk(list, parentId) {
      array(list).forEach(function (node) {
        if (!node || typeof node.id !== "string" || map[node.id]) return;
        var value = clone(node);
        delete value.children;
        map[node.id] = { value: value, parent: parentId };
        order.push(node.id);
        walk(node.children, node.id);
      });
    }
    walk(nodes, null);
    return { map: map, order: order };
  }

  function mergeTrees(baseRoots, localRoots, remoteRoots) {
    var base = flattenTree(baseRoots), local = flattenTree(localRoots);
    var remote = flattenTree(remoteRoots), selected = Object.create(null);
    var order = remote.order.slice();
    local.order.forEach(function (id) {
      if (!remote.map[id] && !base.map[id]) order.push(id);
    });

    order.forEach(function (id) {
      var b = base.map[id], l = local.map[id], r = remote.map[id];
      if (r && b && !l) return; // an explicit local deletion
      if (!r && (!l || b)) return; // remote deletion, unless locally created
      if (!r && l && !b) {
        selected[id] = { value: clone(l.value), parent: l.parent };
        return;
      }
      if (r && !l) {
        selected[id] = { value: clone(r.value), parent: r.parent };
        return;
      }
      var value = clone(r.value);
      var keys = Object.keys(l.value);
      keys.forEach(function (key) {
        if (key === "id") return;
        if (!b || !same(l.value[key], b.value[key])) {
          value[key] = clone(l.value[key]);
        }
      });
      var parent = r.parent;
      if (!b || l.parent !== b.parent) parent = l.parent;
      selected[id] = { value: value, parent: parent };
    });

    var children = Object.create(null), roots = [];
    order.forEach(function (id) {
      var row = selected[id];
      if (!row) return;
      row.value.children = [];
      var parent = row.parent;
      if (!parent || !selected[parent] || parent === id) {
        roots.push(row.value);
      } else {
        (children[parent] = children[parent] || []).push(row.value);
      }
    });
    order.forEach(function (id) {
      if (selected[id]) selected[id].value.children = children[id] || [];
    });
    return roots;
  }

  function responseJson(response) {
    return response.json().then(function (body) {
      if (!response.ok || !body || body.ok !== true) {
        var error = new Error(body && body.error ? body.error :
          "request failed (" + response.status + ")");
        error.status = response.status;
        throw error;
      }
      return body;
    });
  }

  function postImport(goals, baseRevision) {
    return fetch("/api/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goals: goals, base_revision: baseRevision })
    }).then(responseJson);
  }

  function installGoalsAndReload(goals, revision) {
    var saved;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); }
    catch (e) { saved = {}; }
    saved.goals = goals;
    var ids = flattenTree(goals).map;
    if (typeof saved.selId !== "string" || !ids[saved.selId]) {
      saved.selId = goals.length ? goals[0].id : null;
    }
    saved.updatedAt = Date.now();
    localStorage.setItem(KEY, JSON.stringify(saved));
    writeSync(revision, goals);
    lastObservedGoals = JSON.stringify(goals);
    syncBusy = true;
    window.location.reload();
  }

  function reconcileState(st) {
    if (!st || typeof st.revision !== "string") return;
    var remote = rootsFromState(st);
    var synced = readSync();
    if (!synced) {
      writeSync(st.revision, remote);
      return;
    }
    if (synced.revision === st.revision) return;
    var local = readLocalGoals();
    var merged = mergeTrees(synced.goals, local, remote);
    if (same(merged, remote)) {
      writeSync(st.revision, remote);
      if (!same(local, remote)) installGoalsAndReload(remote, st.revision);
      return;
    }
    syncBusy = true;
    postImport(merged, st.revision).then(function (result) {
      installGoalsAndReload(merged, result.revision);
    }).catch(function () {
      syncBusy = false;
      lastObservedGoals = null;
      setTimeout(refreshState, 50);
    });
  }

  function refreshState() {
    if (refreshPending) return;
    refreshPending = true;
    fetch("/api/state", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("state request failed (" + r.status + ")");
        return r.json();
      })
      .then(function (st) {
        acceptState(st);
        reconcileState(st);
      })
      .catch(function () {})
      .then(function () { refreshPending = false; });
  }

  function watchGoals() {
    setInterval(function () {
      if (syncBusy) return;
      var raw;
      try { raw = localStorage.getItem(KEY); } catch (e) { return; }
      if (!raw) return;
      var goals;
      try { goals = JSON.stringify(JSON.parse(raw).goals); } catch (e) { return; }
      if (goals === lastObservedGoals) return;
      lastObservedGoals = goals;
      importGoals(JSON.parse(goals));
    }, 800);
  }


  // --- server records -> the fields this artifact renders ------------------

  function promptRows(goal, byId) {
    return array(goal && goal.prompt_ids).map(function (id) {
      var prompt = byId[id];
      if (!prompt) return null;
      var when = Date.parse(prompt.created_at || "");
      return { id: id, text: str(prompt.text),
               ts: isFinite(when) ? when : Date.now() };
    }).filter(Boolean);
  }

  function contextOf(goal, detail) {
    var sources = array(goal && goal.sources);
    var ctx = {
      code: sources.filter(function (s) {
        return s && (s.type === "github" || s.type === "local");
      }).map(function (s) {
        return { id: s.id, type: s.type, label: str(s.label) };
      }),
      docs: sources.filter(function (s) { return s && s.type === "doc"; })
        .map(function (s) { return { id: s.id, type: "doc", label: str(s.label) }; })
    };
    // Every text field is set, including to "", because the artifact falls
    // back to its own sample copy for anything left null — which would show a
    // demo goal's decisions and blockers as if they were this goal's.
    var sections = (detail && detail.sections) || {};
    ctx.objective = str(goal && goal.description);
    ctx.said = str(sections.said);
    ctx.decided = str(sections.decided);
    ctx.built = str(sections.built);
    ctx.hit = str(sections.hit);
    return ctx;
  }

  var TASK_STATE = { pending: "todo", in_progress: "doing", completed: "done" };

  function agentOf(goal, runs) {
    var rows = array(runs && runs[goal.id]);
    if (!rows.length) return null;
    var run = rows[0];
    return {
      status: run.status === "finished" ? "done" : "running",
      branch: str(run.git_branch),
      prompt: str(run.user_prompt),
      lastFile: "",
      todos: array(run.tasks).map(function (task) {
        return { t: str(task.subject) || "(untitled task)",
                 s: TASK_STATE[task.status] || "todo",
                 active: str(task.activeForm) };
      }),
      log: []
    };
  }

  function artifactOf(goal, runs, detail) {
    var rows = array(runs && runs[goal.id]);
    var reviewed = (detail && array(detail.review)) || [];
    if (!rows.length && !reviewed.length) return null;
    var run = rows[0] || {};
    var latest = reviewed[0] || {};
    var files = array(latest.files);
    return {
      ts: Date.parse(run.finished_at || run.started_at || "") || Date.now(),
      branch: str(run.git_branch),
      status: run.status === "finished" ? "ready" : "pending",
      note: "",
      summary: str(run.summary) || str(latest.summary),
      files: files.map(function (f) {
        return { path: str(f.path), edits: f.edits || 0 };
      }),
      how: array(latest.how)
    };
  }

  function toNode(goal, byParent, byId, runs) {
    return {
      id: goal.id,
      title: str(goal.title),
      prio: goal.priority || "normal",
      done: goal.status === "completed" || goal.status === "abandoned",
      open: true,
      status: goal.status === "in_progress" ? "inprog" : "todo",
      notes: str(goal.notes),
      desc: str(goal.description),
      labels: [],
      prompts: promptRows(goal, byId),
      ctx: contextOf(goal, details[goal.id]),
      agent: agentOf(goal, runs),
      artifact: artifactOf(goal, runs, details[goal.id]),
      children: array(byParent[goal.id]).map(function (child) {
        return toNode(child, byParent, byId, runs);
      })
    };
  }

  function rootsFromState(st) {
    var byParent = {}, byId = {};
    array(st && st.prompts).forEach(function (p) {
      if (p && typeof p.id === "string") byId[p.id] = p;
    });
    array(st && st.goals).forEach(function (goal) {
      var parent = goal.parent_goal_id || null;
      (byParent[parent] = byParent[parent] || []).push(goal);
    });
    return array(byParent[null]).map(function (goal) {
      return toNode(goal, byParent, byId, (st && st.agent_runs) || {});
    });
  }

  function seedPayload(st, roots, saved) {
    saved = saved && typeof saved === "object" ? saved : {};
    var flat = flattenTree(roots).map;
    var filters = { active: true, inprog: true, done: true, all: true };
    var tabs = { context: true, prompt: true, agent: true, artifact: true };
    var selection = typeof saved.selId === "string" && flat[saved.selId] ?
      saved.selId : (roots.length ? roots[0].id : null);
    return {
      v: 7,
      goals: roots,
      selId: selection,
      filter: filters[saved.filter] ? saved.filter : "active",
      updatedAt: st.generated_at ? Date.parse(st.generated_at) : Date.now(),
      labels: array(saved.labels),
      paneTab: tabs[saved.paneTab] ? saved.paneTab : "context",
      themeMode: saved.themeMode === "light" || saved.themeMode === "dark" ?
        saved.themeMode : null,
      view: saved.view === "tree" || saved.view === "inspect" ? saved.view : "split",
      page: saved.page === "convos" ? "convos" : "goals",
      // Onboarding is the artifact's, and these are the real answers: what the
      // vault has actually been told, not an assumption that it is set up.
      setup: setupState || { sv: 9, storage: false, analysis: null, done: false }
    };
  }

  function acceptState(st) {
    if (!st || !Array.isArray(st.goals)) return false;
    serverState = {
      goals: st.goals,
      // Human turns only: keep the role check here so a future change upstream
      // cannot leak assistant or tool text into the prompt history.
      prompts: array(st.prompts).filter(function (p) {
        return p && p.role === "user" && typeof p.id === "string" &&
          typeof p.text === "string";
      }),
      runs: (st.agent_runs && typeof st.agent_runs === "object") ? st.agent_runs : {},
      scope: st.scope === "chat" ? "chat" : "global"
    };
    var fingerprint = JSON.stringify([
      serverState.goals.map(function (g) {
        return [g.id, g.prompt_ids, g.sources, g.status, g.title];
      }),
      serverState.runs
    ]);
    if (fingerprint === stateFingerprint) return true;
    stateFingerprint = fingerprint;
    return true;
  }

  function seed() {
    try {
      var setup = new XMLHttpRequest();
      setup.open("GET", "/api/setup", false);
      setup.send();
      var answered = JSON.parse(setup.responseText);
      if (answered && answered.ok) {
        setupState = answered;
        if (answered.convos && answered.convos.length) {
          window.__hcConvos = answered.convos;
        }
      }
    } catch (e) {
      setupState = null;
    }
    try {
      var request = new XMLHttpRequest();
      request.open("GET", "/api/state", false);   // sync: must beat app boot
      request.send();
      var st = JSON.parse(request.responseText);
      if (!acceptState(st)) return;
      var roots = rootsFromState(st);
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
      localStorage.setItem(KEY, JSON.stringify(seedPayload(st, roots, saved)));
      lastObservedGoals = JSON.stringify(roots);
      if (typeof st.revision === "string") writeSync(st.revision, roots);
    } catch (e) {
      // Server unreachable: let the artifact boot on whatever it already has.
    }
  }

  // --- edits the artifact makes to goal fields, not tree shape -------------

  function sourcesOfNode(node) {
    var ctx = node && node.ctx;
    return array(ctx && ctx.code).concat(array(ctx && ctx.docs))
      .filter(function (row) { return row && str(row.label).trim(); })
      .map(function (row) {
        return { id: str(row.id), label: str(row.label).trim(),
                 type: row.type === "github" ? "github" :
                       row.type === "doc" ? "doc" : "local" };
      });
  }

  function sameSources(a, b) {
    var key = function (rows) {
      return array(rows).map(function (r) { return [r.type, r.label]; });
    };
    return same(key(a), key(b));
  }

  function post(body) {
    return fetch("/api/op", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).catch(function () { return null; });
  }

  function syncNodeFields(roots) {
    var pending = [];
    var goals = {};
    array(serverState.goals).forEach(function (g) { goals[g.id] = g; });
    var flat = flattenTree(roots);
    flat.order.forEach(function (id) {
      var node = flat.map[id], goal = goals[id];
      if (!node || !goal) return;
      var next = sourcesOfNode(node.value);
      if (!sameSources(next, array(goal.sources))) {
        goal.sources = next;
        pending.push(post({ op: "set_sources", goal_id: id, sources: next }));
      }
      var kept = array(node.value.prompts).map(function (p) { return p.id; });
      array(goal.prompt_ids).forEach(function (pid) {
        if (kept.indexOf(pid) < 0) {
          pending.push(post({ op: "detach_prompt", goal_id: id, prompt_id: pid }));
        }
      });
    });
    return Promise.all(pending);
  }

  function importGoals(goals) {
    // Field ops must land before the tree import, or the import reads back
    // the sources they just replaced.
    syncNodeFields(goals).then(function () { importTree(goals); });
  }

  function importTree(goals) {
    var synced = readSync();
    if (!synced) {
      lastObservedGoals = null;
      refreshState();
      return;
    }
    syncBusy = true;
    postImport(goals, synced.revision).then(function (result) {
      writeSync(result.revision, goals);
      syncBusy = false;
      refreshState();
    }).catch(function () {
      syncBusy = false;
      lastObservedGoals = null;
      refreshState();
    });
  }

  // --- per-goal detail, fetched only for the goal on screen ----------------

  function selectedGoalId() {
    try {
      var saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      return typeof saved.selId === "string" ? saved.selId : null;
    } catch (e) {
      return null;
    }
  }

  var threads = Object.create(null);

  function loadThread(id) {
    // The list carries a short preview so polling stays cheap. Opening one
    // conversation is when the whole transcript is worth fetching.
    if (!id || threads[id]) return Promise.resolve(false);
    threads[id] = true;
    return fetch("/api/conversation?id=" + encodeURIComponent(id),
                 { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (!body || body.ok !== true || !array(body.thread).length) return false;
        var rows = array(window.__hcConvos);
        for (var i = 0; i < rows.length; i++) {
          if (rows[i] && rows[i].id === id) {
            rows[i].thread = body.thread;
            return true;
          }
        }
        return false;
      })
      .catch(function () { threads[id] = false; return false; });
  }

  function briefingSections(brief) {
    // The briefing is written to be read as a prompt; the inspector shows the
    // same material as panels. One section can feed one panel, and one panel
    // can need two sections: BLOCKERS & OPEN QUESTIONS is blockers *and*
    // whatever is still open.
    var sections = {};
    array((brief || {}).sections).forEach(function (section) {
      var body = array(section.lines).map(function (line) {
        return line.replace(/^ {2}/, "").replace(/^- /, "");
      }).join("\n").trim();
      var title = str(section.title);
      if (!body) return;
      if (title.indexOf("IN THEIR WORDS") >= 0) sections.said = body;
      else if (title.indexOf("ALREADY DECIDED") === 0) sections.decided = body;
      else if (title.indexOf("ALREADY BUILT") === 0) sections.built = body;
      else if (title.indexOf("PROBLEMS HIT") === 0 ||
               title.indexOf("STILL OPEN") === 0) {
        sections.hit = sections.hit ? sections.hit + "\n" + body : body;
      }
    });
    return sections;
  }

  function loadDetail(goalId) {
    if (!goalId || details[goalId] || detailPending[goalId]) return;
    if (serverState.scope === "chat") return;
    detailPending[goalId] = true;
    var query = "?goal=" + encodeURIComponent(goalId);
    var get = function (path) {
      return fetch(path + query, { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .catch(function () { return null; });
    };
    Promise.all([get("/api/briefing"), get("/api/review")]).then(function (both) {
      var brief = both[0] || {}, review = both[1] || {};
      var sections = briefingSections(brief);
      details[goalId] = { sections: sections, opening: str(brief.opening),
                          cwd: str(brief.cwd), review: array(review.runs) };
      delete detailPending[goalId];
      lastObservedGoals = null;
      refreshState();
    });
  }

  function watchSelection() {
    setInterval(function () { loadDetail(selectedGoalId()); }, 500);
  }


  // --- asking for a value the artifact would otherwise invent --------------

  var ASK = {
    github: { title: "Attach a GitHub repository",
              placeholder: "owner/repo  or  https://github.com/owner/repo" },
    local: { title: "Attach a local folder",
             placeholder: "~/path/to/project" },
    doc: { title: "Attach a document",
           placeholder: "~/notes/design.md  or  https://…" }
  };

  function ensureDialogStyles() {
    if (document.getElementById("hc-ask-style")) return;
    var style = document.createElement("style");
    style.id = "hc-ask-style";
    style.textContent = [
      ".hc-ask{position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.28);display:flex;align-items:center;justify-content:center;padding:20px}",
      ".hc-ask-box{width:min(460px,100%);background:var(--panel,#fff);color:var(--ink,#111);border:1px solid var(--bd2,#d5d5d5);border-radius:3px;box-shadow:0 18px 60px rgba(0,0,0,.2);padding:16px;font-family:'Source Code Pro',monospace}",
      ".hc-ask-title{font:600 12px 'Source Code Pro',monospace;margin-bottom:10px;color:var(--ink)}",
      ".hc-ask-input{width:100%;box-sizing:border-box;border:1px solid var(--bd2);border-radius:2px;background:var(--panel2);color:var(--ink);outline:none;padding:8px 10px;font:12px 'Source Code Pro',monospace}",
      ".hc-ask-input:focus{border-color:var(--acc)}",
      ".hc-ask-row{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}",
      ".hc-ask-btn{border:1px solid var(--bd2);background:transparent;color:var(--fnt);border-radius:2px;padding:5px 12px;cursor:pointer;font:11px 'Source Code Pro',monospace}",
      ".hc-ask-btn:hover{color:var(--ink)}",
      ".hc-ask-ok{background:var(--acc);border-color:var(--acc);color:var(--onacc)}"
    ].join("");
    document.head.appendChild(style);
  }

  function ask(kind) {
    var spec = ASK[kind] || ASK.doc;
    return new Promise(function (resolve) {
      ensureDialogStyles();
      var overlay = document.createElement("div");
      overlay.className = "hc-ask";
      var box = document.createElement("div");
      box.className = "hc-ask-box";
      var title = document.createElement("div");
      title.className = "hc-ask-title";
      title.textContent = spec.title;
      var input = document.createElement("input");
      input.type = "text";
      input.className = "hc-ask-input";
      input.placeholder = spec.placeholder;
      var row = document.createElement("div");
      row.className = "hc-ask-row";
      var cancel = document.createElement("button");
      cancel.className = "hc-ask-btn";
      cancel.textContent = "Cancel";
      var confirm = document.createElement("button");
      confirm.className = "hc-ask-btn hc-ask-ok";
      confirm.textContent = "Attach";

      function close(value) {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        resolve(value || null);
      }
      cancel.onclick = function () { close(null); };
      confirm.onclick = function () { close(input.value.trim()); };
      input.onkeydown = function (e) {
        if (e.key === "Enter") { e.preventDefault(); close(input.value.trim()); }
        if (e.key === "Escape") { e.preventDefault(); close(null); }
      };
      overlay.onclick = function (e) { if (e.target === overlay) close(null); };
      row.appendChild(cancel);
      row.appendChild(confirm);
      box.appendChild(title);
      box.appendChild(input);
      box.appendChild(row);
      overlay.appendChild(box);
      (document.querySelector(".hc") || document.body).appendChild(overlay);
      setTimeout(function () { input.focus(); }, 0);
    });
  }

  // Its three add controls append a placeholder row. Make them ask for the
  // real value first; nothing else about them changes.
  function patchBundleSource(source) {
    var parts = [
      ["Goals, subgoals, and suggested tasks inferred from your Claude Code history.", "A holistic view of your goals, subgoals, and suggested tasks \u2014 inferred from your Claude Code\u00a0conversation\u00a0history."],
      ["The source conversations your goals and state are derived from.", "Your Claude Code conversations, preserved beyond Claude\u2019s default 30-day history and used to derive your goals."],
      // Both subtitles were sized for the shorter copy they replaced, so the
      // longer sentences wrapped mid-clause. Widening them also tags them:
      // the analysis banner sits directly under whichever one is on screen,
      // and needs a stable handle on it.
      ['<div style="margin-top:6px;font-size:11.5px;line-height:1.6;color:var(--mut);max-width:560px;text-wrap:pretty">A holistic view',
       '<div class="hc-sub" style="margin-top:6px;font-size:11.5px;line-height:1.6;color:var(--mut);max-width:740px;text-wrap:pretty">A holistic view'],
      ['<div style="margin-top:6px;font-size:11.5px;line-height:1.6;color:var(--mut);max-width:560px;text-wrap:pretty">Your Claude Code conversations',
       '<div class="hc-sub" style="margin-top:6px;font-size:11.5px;line-height:1.6;color:var(--mut);max-width:740px;text-wrap:pretty">Your Claude Code conversations'],
      // The inspector always opens on Context. Restoring the last pane meant
      // landing on Agent or Artifact for a goal that has neither.
      ["paneTab: (saved && saved.v >= 6 && ['prompt', 'agent', 'artifact'].indexOf(saved.paneTab) >= 0) ? saved.paneTab : 'context',",
       "paneTab: 'context',"],
      // A bare "Goal:" with nothing after it reads as missing data. The line
      // now states the link or its absence, and is computed from which goals
      // actually cite this conversation.
      ["Goal: {{ cv.goal }}", "{{ cv.goalLine }}"],
      // An empty tree is not a dead end during onboarding: goals arrive when
      // the analysis finishes. Say so, rather than only offering a manual add.
      ["emptyLabel: filter === 'done' ? 'Nothing completed yet.' : filter === 'inprog' ? 'Nothing in progress \u2014 set a goal\\u2019s status in the inspector.' : 'No goals yet \u2014 add one below.',", "emptyLabel: filter === 'done' ? 'Nothing completed yet.' : filter === 'inprog' ? 'Nothing in progress \u2014 set a goal\\u2019s status in the inspector.' : (window.__hcAnalysisPending && window.__hcAnalysisPending() ? 'No goals yet \\u2014 they are inferred once your conversations have all been analyzed. You can add one yourself below in the meantime.' : 'No goals yet \\u2014 they are inferred from your analyzed conversations, or add one below.'),"],
      // An empty vault is a real state, not a missing one. Without this the
      // artifact falls back to its built-in sample tree, and the sync then
      // writes those sample goals into the user's vault as if they authored
      // them. The seed marks itself v7 so only a seeded store qualifies.
      ["if (saved && saved.v >= 4 && Array.isArray(saved.goals) && saved.goals.length && cnt(saved.goals) <= 2000) g0 = norm(saved.goals);",
       "if (saved && saved.v >= 4 && Array.isArray(saved.goals) && (saved.goals.length || saved.v >= 7) && cnt(saved.goals) <= 2000) g0 = norm(saved.goals);"],
      // Its agent panel simulated a session: templated todos, a hardcoded file
      // list, and diff stats computed from the length of each filename. Both
      // entry points now start a real goal-bound session instead.
      ["  genTodos() {\n    const id = this.state.selId; if (!id) return;\n    clearInterval(this._agT);\n    const todos = this.buildSteps(id).map(t => ({ t, s: 'todo' }));\n    this.set(s => ({ goals: this.up(s.goals, id, x => ({ ...x, agent: { status: 'planned', ts: Date.now(), todos } })) }), true);\n  }\n", "  genTodos() {\n    // A plan is what a real session produces; fabricating one from a template\n    // would put words in the agent's mouth. Launching is what produces it.\n    this.runAgent();\n  }\n"],
      ["  runAgent() {\n    const id = this.state.selId; if (!id) return;\n    const promptText = this._draftEl ? this._draftEl.value : '';\n    this.recordPrompt(promptText);\n    const tr = this.path(this.state.goals, id);\n    const node = tr ? tr[tr.length - 1] : null;\n    const planned = (node && node.agent && node.agent.status === 'planned' && node.agent.todos && node.agent.todos.length) ? node.agent.todos.map(o => o.t) : null;\n    const AF = { 'Scan repo for related code': 'Scanning repo for related code\u2026', 'Draft the changes': 'Drafting the changes\u2026', 'Run tests and fix failures': 'Running tests and fixing failures\u2026', 'Summarize results': 'Summarizing results\u2026' };\n    const steps = planned || this.buildSteps(id);\n    const todos = steps.map((t, i) => ({ t, s: i === 0 ? 'doing' : 'todo', af: AF[t] || ('Working on: ' + t + '\u2026') }));\n    const slug = ((node && node.title) || 'goal').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 28);\n    const branch = 'agent/' + (slug || 'run');\n    const now = () => new Date().toLocaleTimeString('en-US', { hour12: false });\n    const log0 = [{ ts: now(), m: 'session started \u2014 branch ' + branch }, { ts: now(), m: 'task started: ' + todos[0].t }];\n    this.set(s => ({ goals: this.up(s.goals, id, x => ({ ...x, done: false, status: 'inprog', agent: { status: 'running', ts: Date.now(), todos, branch, prompt: promptText, lastFile: null, log: log0 } })) }), true);\n    clearInterval(this._agT);\n    const FILES = ['compact_focus/cli.py', 'compact_focus/state.py', 'hooks/guard.sh', 'ui/server.py', 'CHANGELOG.md'];\n    let ticks = 0;\n    this._agT = setInterval(() => {\n      if (++ticks > steps.length * 2 + 5) { clearInterval(this._agT); return; }\n      this.set(s => ({ goals: this.up(s.goals, id, x => {\n        if (!x.agent || x.agent.status !== 'running') return x;\n        const a = { ...x.agent, todos: (x.agent.todos || []).map(o => ({ ...o })), log: (x.agent.log || []).slice() };\n        const t2 = new Date().toLocaleTimeString('en-US', { hour12: false });\n        const i = a.todos.findIndex(o => o.s === 'doing');\n        if (ticks % 2 === 1 && i >= 0) {\n          const f = FILES[(ticks >> 1) % FILES.length];\n          a.lastFile = f;\n          a.edited = a.edited || [];\n          if (a.edited.indexOf(f) < 0) a.edited = a.edited.concat([f]);\n          a.log.push({ ts: t2, m: 'edited ' + f });\n          return { ...x, agent: a };\n        }\n        if (i >= 0) { a.todos[i].s = 'done'; a.log.push({ ts: t2, m: 'task completed: ' + a.todos[i].t }); }\n        const nx = a.todos.findIndex(o => o.s === 'todo');\n        if (nx >= 0) { a.todos[nx].s = 'doing'; a.log.push({ ts: t2, m: 'task started: ' + a.todos[nx].t }); return { ...x, agent: a }; }\n        a.status = 'done';\n        a.log.push({ ts: t2, m: 'run finished \u2014 ' + a.todos.length + ' tasks completed' });\n        const fl = (a.edited && a.edited.length ? a.edited : ['compact_focus/cli.py']).map(p => ({ path: p, plus: (p.length * 7) % 60 + 8, minus: (p.length * 3) % 25 + 2 }));\n        return { ...x, agent: a, artifact: { ts: Date.now(), branch: a.branch, status: 'pending', note: '', summary: 'Implemented \"' + (x.title || 'Untitled') + '\" on ' + a.branch + '. ' + a.todos.length + ' tasks completed; changes staged for review.', files: fl } };\n      }) }));\n    }, 1600);\n  }\n", "  runAgent() {\n    const id = this.state.selId;\n    if (!id) return;\n    this.recordPrompt(this._draftEl ? this._draftEl.value : '');\n    // Opens a terminal in this goal's project with the prompt typed and\n    // unsent. Its tasks then arrive here as it creates them, for real.\n    if (window.__hcAgent) window.__hcAgent.launch(id);\n  }\n"],
      // Its analysis screen animated random progress over a sample list for a
      // fixed ~30s. Replaced with what the vault actually reports.
      ["  startAnalysis() {\n    clearInterval(this._anT);\n    const n = this.CONVOS.length;\n    this.set(() => ({ page: 'convos', convSel: null, an: { phase: 'convs', total: n, prog: Array(n).fill(0) } }));\n    this._anT = setInterval(() => {\n      const a = this.state.an;\n      if (!a) { clearInterval(this._anT); return; }\n      // up to 5 concurrent, ~15s each\n      let active = 0;\n      const prog = a.prog.map(p => {\n        if (p >= 100) return p;\n        if (active >= 5) return p;\n        active++;\n        return Math.min(100, p + 0.9 + Math.random() * 1.0);\n      });\n      this.setState({ an: { ...a, prog } });\n      if (prog.every(p => p >= 100)) {\n        clearInterval(this._anT);\n        setTimeout(() => {\n          this.set(() => ({ page: 'goals', an: { phase: 'goals', total: 63, done: 0 } }));\n          this._anT = setInterval(() => {   // ~30s for 63 conversations\n            const g = this.state.an;\n            if (!g) { clearInterval(this._anT); return; }\n            const gd = Math.min(g.total, g.done + 1);\n            this.setState({ an: { ...g, done: gd } });\n            if (gd >= g.total) { clearInterval(this._anT); setTimeout(() => this.setState({ an: null }), 1200); }\n          }, 470);\n        }, 1000);\n      }\n    }, 200);\n  }\n", "  startAnalysis() {\n    clearInterval(this._anT);\n    // Real progress: the vault reports how many conversations it has and how\n    // many it has finished. Nothing here invents a duration.\n    const tick = () => {\n      const setup = window.__hcSetup;\n      if (!setup) { clearInterval(this._anT); this.setState({ an: null }); return; }\n      setup.progress().then((s) => {\n        if (!s) return;\n        const counts = s.conversations || { total: 0, analyzed: 0 };\n        const total = counts.total || (window.__hcConvos || []).length;\n        const done = Math.min(counts.analyzed || 0, total);\n        const prog = Array.from({ length: total }, (_, k) =>\n          k < done ? 100 : (k === done && s.running ? 50 : 0));\n        this.setState({ an: { phase: 'convs', total, prog, done } });\n        if (total && done >= total && !s.running) {\n          clearInterval(this._anT);\n          this.set(() => ({ page: 'goals', an: { phase: 'goals', total, done } }));\n          setTimeout(() => this.setState({ an: null }), 1500);\n        }\n      });\n    };\n    this.set(() => ({ page: 'convos', convSel: null,\n      an: { phase: 'convs', total: (window.__hcConvos || []).length, prog: [], done: 0 } }));\n    tick();\n    this._anT = setInterval(tick, 2000);\n  }\n"],
      // Its sample conversation list becomes the user's own history when the
      // server has some; the sample survives for a standalone artifact.
      ["get CONVOS() {\n    return [",
       "get CONVOS() {\n    if (typeof window !== 'undefined' && window.__hcConvos) return window.__hcConvos;\n    return ["],
      // Step 1 of onboarding: turning capture on is a real act — it imports
      // existing transcripts — so it goes to the vault, not just to state.
      ["obNext: () => this.set(s => ({ setup: { ...s.setup, sv: 9, storage: true }, obStep: 2 }))",
       "obNext: () => { window.__hcSetup.capture(true); this.set(s => ({ setup: { ...s.setup, sv: 9, storage: true }, obStep: 2 })); }"],
      // Step 2: each choice starts the analysis it names, or declines it.
      ["obLocal: () => { this.set(s => ({ setup: { ...s.setup, analysis: 'local', done: true } })); this.startAnalysis(); }",
       "obLocal: () => { window.__hcSetup.analyze('local'); this.set(s => ({ setup: { ...s.setup, analysis: 'local', done: true } })); this.startAnalysis(); }"],
      ["obClaude: () => { this.set(s => ({ setup: { ...s.setup, analysis: 'claude', done: true } })); this.startAnalysis(); }",
       "obClaude: () => { window.__hcSetup.analyze('claude'); this.set(s => ({ setup: { ...s.setup, analysis: 'claude', done: true } })); this.startAnalysis(); }"],
      ["obSkip: () => this.set(s => ({ setup: { ...s.setup, analysis: 'none', done: true } }))",
       "obSkip: () => { window.__hcSetup.analyze('none'); this.set(s => ({ setup: { ...s.setup, analysis: 'none', done: true } })); }"],
      ["codeAddGh: () => setCode(codeList.concat([{ id: 'c' + Date.now().toString(36), type: 'github', label: 'owner/repo' }]))",
       "codeAddGh: () => window.__hcAsk('github').then(function (v) { if (v) setCode(codeList.concat([{ id: 'c' + Date.now().toString(36), type: 'github', label: v }])); })"],
      ["codeAddLocal: () => setCode(codeList.concat([{ id: 'c' + Date.now().toString(36), type: 'local', label: '~/path/to/project' }]))",
       "codeAddLocal: () => window.__hcAsk('local').then(function (v) { if (v) setCode(codeList.concat([{ id: 'c' + Date.now().toString(36), type: 'local', label: v }])); })"],
      // Opening a conversation shows a preview until the full transcript
      // arrives; the second setState re-renders it in place.
      ["open: () => this.setState({ convSel: c.id })",
       "open: () => { this.setState({ convSel: c.id }); if (window.__hcPromptUI) window.__hcPromptUI.loadThread(c.id).then((got) => { if (got) this.setState({ convSel: c.id }); }); }"],
      ["docAdd: () => setDocs(docList.concat([{ id: 'd' + Date.now().toString(36), type: 'doc', label: 'notes.md' }]))",
       "docAdd: () => window.__hcAsk('doc').then(function (v) { if (v) setDocs(docList.concat([{ id: 'd' + Date.now().toString(36), type: 'doc', label: v }])); })"]
    ];
    return parts.reduce(function (patched, part) {
      if (patched.indexOf(part[1]) >= 0) return patched;
      var at = patched.indexOf(part[0]);
      if (at < 0) {
        console.warn("[hc ui] an add-source control was not found; left as-is");
        return patched;
      }
      return patched.slice(0, at) + part[1] + patched.slice(at + part[0].length);
    }, source);
  }

  function patchBundleTemplate() {
    var template = document.querySelector('script[type="__bundler/template"]');
    if (!template) return false;
    try {
      template.textContent = JSON.stringify(patchBundleSource(
        JSON.parse(template.textContent)));
      return true;
    } catch (error) {
      console.error("[hc ui] could not patch add-source controls:", error);
      return false;
    }
  }

  window.__hcAsk = ask;

  // --- a banner for work happening outside the page ------------------------
  // Analysis runs in a detached worker over the user's whole history. Without
  // a standing statement of what is running and which conversation it is on,
  // an empty goal tree just looks broken.

  var banner = null;

  window.__hcAnalysisPending = function () {
    // Work is pending only while a worker holds it or the queue is non-empty.
    // Not every conversation yields an extraction, so analyzed < total is a
    // normal resting state, not a claim that something is running.
    if (!setupState) return false;
    var counts = setupState.conversations || {};
    return !!(setupState.running || (counts.pending || 0) > 0);
  };

  function ensureBannerStyles() {
    if (document.getElementById("hc-banner-style")) return;
    var style = document.createElement("style");
    style.id = "hc-banner-style";
    style.textContent = [
      ".hc-banner{position:relative;margin-top:10px;display:flex;align-items:center;gap:10px;padding:7px 11px;background:var(--panel2,#f6f6f6);border:1px solid var(--bd,#e3e3e3);border-radius:2px;font:11px/1.45 'Source Code Pro',monospace;color:var(--ink,#111)}",
      ".hc-banner-dot{flex:none;width:7px;height:7px;border-radius:50%;background:var(--acc,#a5492a);animation:hc-pulse 1.4s ease-in-out infinite}",
      "@keyframes hc-pulse{0%,100%{opacity:.35;transform:scale(.85)}50%{opacity:1;transform:scale(1.15)}}",
      ".hc-banner-what{flex:none;font-weight:600}",
      ".hc-banner-now{flex:1;min-width:0;color:var(--mut,#555);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-banner-count{flex:none;color:var(--fnt,#777)}",
      ".hc-banner-bar{position:absolute;left:0;bottom:-1px;height:2px;background:var(--acc,#a5492a);transition:width .4s ease}"
    ].join("");
    document.head.appendChild(style);
  }

  function renderBanner() {
    var pending = window.__hcAnalysisPending();
    if (!pending) {
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
      banner = null;
      return;
    }
    ensureBannerStyles();
    var sub = document.querySelector(".hc-sub");
    var host = document.querySelector(".hc") || document.body;
    if (!banner || !document.documentElement.contains(banner)) {
      banner = document.createElement("div");
      banner.className = "hc-banner";
      banner.setAttribute("role", "status");
      banner.setAttribute("aria-live", "polite");
      banner.style.position = "relative";
      ["hc-banner-dot", "hc-banner-what", "hc-banner-now", "hc-banner-count",
       "hc-banner-bar"].forEach(function (cls) {
        var part = document.createElement("div");
        part.className = cls;
        banner.appendChild(part);
      });
    }
    // Re-anchor every pass: switching pages swaps one subtitle for the other,
    // and the banner should follow the visible one rather than strand itself.
    if (sub && sub.parentNode && sub.nextSibling !== banner) {
      sub.parentNode.insertBefore(banner, sub.nextSibling);
    } else if (!sub && !banner.parentNode) {
      host.insertBefore(banner, host.firstChild || null);
    }
    var counts = (setupState && setupState.conversations) || { total: 0, analyzed: 0 };
    var current = setupState && setupState.current;
    var goalPhase = setupState && setupState.phase === "synthesizing";
    banner.querySelector(".hc-banner-what").textContent = goalPhase
      ? "Building your goal tree" : "Analyzing your conversations";
    banner.querySelector(".hc-banner-now").textContent = current && current.title
      ? "now: " + current.title
      : (current ? "now: " + String(current.id).slice(0, 8)
                 : "goals appear here as this finishes");
    banner.querySelector(".hc-banner-count").textContent =
      counts.analyzed + " of " + counts.total;
    banner.querySelector(".hc-banner-bar").style.width =
      (counts.total ? Math.round(counts.analyzed / counts.total * 100) : 0) + "%";
  }

  function watchAnalysis() {
    setInterval(function () {
      if (!window.__hcAnalysisPending() && !setupState) return;
      refreshSetup().then(renderBanner);
    }, 2000);
    renderBanner();
  }

  window.__hcAgent = {
    launch: function (goalId) {
      return post({ op: "launch_agent_run", goal_id: goalId })
        .then(function (body) {
          if (!body || body.ok !== true) {
            notify("Could not start a session: " +
              ((body && body.error) || "the launcher refused") +
              (body && body.command ? "\n\nRun it yourself:\n" + body.command : ""));
            return body;
          }
          notify("Opened " + body.terminal + " in " + body.cwd +
            "\n\nThe goal is typed into the composer there — press Enter to " +
            "start. Its tasks will appear here as it creates them.");
          return body;
        });
    }
  };

  function notify(message) {
    ensureDialogStyles();
    var overlay = document.createElement("div");
    overlay.className = "hc-ask";
    var box = document.createElement("div");
    box.className = "hc-ask-box";
    var body = document.createElement("div");
    body.className = "hc-ask-title";
    body.style.whiteSpace = "pre-wrap";
    body.style.fontWeight = "400";
    body.textContent = message;
    var row = document.createElement("div");
    row.className = "hc-ask-row";
    var ok = document.createElement("button");
    ok.className = "hc-ask-btn hc-ask-ok";
    ok.textContent = "OK";
    ok.onclick = function () {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    };
    overlay.onclick = function (e) { if (e.target === overlay) ok.onclick(); };
    row.appendChild(ok);
    box.appendChild(body);
    box.appendChild(row);
    overlay.appendChild(box);
    (document.querySelector(".hc") || document.body).appendChild(overlay);
    setTimeout(function () { ok.focus(); }, 0);
  }

  // --- onboarding: the artifact asks, the vault answers -------------------

  function refreshSetup() {
    return fetch("/api/setup", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (body && body.ok) {
          setupState = body;
          // The artifact reads this getter for its conversation list.
          if (body.convos && body.convos.length) window.__hcConvos = body.convos;
        }
        return setupState;
      })
      .catch(function () { return setupState; });
  }

  window.__hcSetup = {
    // Enabling capture imports existing transcripts, so it is only ever done
    // from an explicit click here.
    capture: function (enabled) {
      return post({ op: "enable_capture", enabled: enabled !== false })
        .then(function (body) {
          if (body && body.ok) setupState = body;
          return body;
        });
    },
    analyze: function (provider) {
      return post({ op: "start_analysis", provider: provider })
        .then(function (body) {
          if (body && body.ok) setupState = body;
          return body;
        });
    },
    progress: refreshSetup,
    convos: function () { return (setupState && setupState.convos) || null; },
    state: function () { return setupState; }
  };


  window.__hcPromptUI = {
    rootsFromState: rootsFromState,
    patchBundleSource: patchBundleSource,
    seedPayload: seedPayload,
    mergeTrees: mergeTrees,
    acceptState: acceptState,
    selectedGoalId: selectedGoalId,
    sourcesOfNode: sourcesOfNode,
    contextOf: contextOf,
    agentOf: agentOf,
    artifactOf: artifactOf,
    promptRows: promptRows,
    ask: ask,
    renderBanner: renderBanner,
    loadThread: loadThread,
    briefingSections: briefingSections,
    analysisPending: function () { return window.__hcAnalysisPending(); },
    setSetupForTest: function (value) { setupState = value; }
  };

  seed();
  // Placed after the template island and before the closing body tag: the
  // artifact's DOMContentLoaded listener is registered but has not unpacked
  // the template yet, so patching here is safe.
  patchBundleTemplate();
  function boot() {
    watchGoals();
    watchAnalysis();
    watchSelection();
    setInterval(refreshState, 1500);
    setTimeout(refreshState, 0);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
