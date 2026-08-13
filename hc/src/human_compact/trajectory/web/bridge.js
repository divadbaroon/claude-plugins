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
    // Absent fields fall back to the artifact's own copy, so only set what the
    // Vault actually knows about this goal.
    if (str(goal && goal.description)) ctx.objective = goal.description;
    var sections = (detail && detail.sections) || {};
    ["said", "decided", "built", "hit"].forEach(function (key) {
      if (sections[key]) ctx[key] = sections[key];
    });
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
      v: 6,
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
      if (answered && answered.ok) setupState = answered;
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
      var sections = {};
      array(brief.sections).forEach(function (section) {
        var body = array(section.lines).map(function (line) {
          return line.replace(/^ {2}/, "").replace(/^- /, "");
        }).join("\n").trim();
        var title = str(section.title);
        if (title.indexOf("IN THEIR WORDS") >= 0) sections.said = body;
        else if (title.indexOf("ALREADY DECIDED") === 0) sections.decided = body;
        else if (title.indexOf("ALREADY BUILT") === 0) sections.built = body;
        else if (title.indexOf("PROBLEMS HIT") === 0) sections.hit = body;
      });
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

  // --- onboarding: the artifact asks, the vault answers -------------------

  function refreshSetup() {
    return fetch("/api/setup", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (body && body.ok) setupState = body;
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
    ask: ask
  };

  seed();
  // Placed after the template island and before the closing body tag: the
  // artifact's DOMContentLoaded listener is registered but has not unpacked
  // the template yet, so patching here is safe.
  patchBundleTemplate();
  function boot() {
    watchGoals();
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
