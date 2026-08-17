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
  var serverState = { goals: [], prompts: [], runs: {}, claim: null,
                      scope: "global" };
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

  function clearKeepPane() {
    try {
      var saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      if (!saved.hcKeepPane) return;
      delete saved.hcKeepPane;
      localStorage.setItem(KEY, JSON.stringify(saved));
    } catch (e) { /* nothing to clear */ }
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
    // This reload is ours, not the reader's. A page they loaded themselves
    // should open on CONTEXT; one we forced on them because a run finished
    // should put them back where they were watching it.
    saved.hcKeepPane = true;
    localStorage.setItem(KEY, JSON.stringify(saved));
    writeSync(revision, goals);
    lastObservedGoals = JSON.stringify(goals);
    syncBusy = true;
    window.location.reload();
  }

  // What the inspector's shape depends on, per goal: whether a run is live,
  // and whether there is an artifact to review. Task-by-task progress is not
  // in here on purpose -- the feed draws that straight into the DOM, and
  // reloading the page for it would make the pane unusable.
  function paneShape(roots) {
    var flat = flattenTree(roots);
    return flat.order.map(function (id) {
      var value = (flat.map[id] || {}).value || {};
      var status = (value.agent || {}).status;
      var live = status === "running" || status === "waiting";
      return id + ":" + (live ? "1" : "0") + (value.artifact ? "1" : "0");
    }).join(",");
  }

  function reconcileState(st) {
    if (!st || typeof st.revision !== "string") return;
    var remote = rootsFromState(st);
    var synced = readSync();
    if (!synced) {
      writeSync(st.revision, remote);
      return;
    }
    if (synced.revision === st.revision) {
      // The revision covers the goal tree. A run starting changes what a node
      // says about itself without changing the tree, so the stored copy stays
      // right about the goals and stale about the work -- which is why REVIEW
      // only appeared after a reload. Only the shape of the pane is worth a
      // reload, not every task update: whether a goal has a live run, and
      // whether it has an artifact.
      var stale = readLocalGoals();
      if (stale && paneShape(stale) !== paneShape(remote)) {
        installGoalsAndReload(mergeTrees(synced.goals, stale, remote),
                              st.revision);
      }
      return;
    }
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
      // created_at is a date, not a datetime. Date.parse reads a bare
      // "2026-08-04" as midnight UTC, which west of Greenwich renders as
      // 5pm the day before -- a time nobody recorded, on the wrong day.
      var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str(prompt.created_at));
      var when = parts
        ? new Date(+parts[1], +parts[2] - 1, +parts[3]).getTime()
        : Date.parse(prompt.created_at || "");
      return { id: id, text: str(prompt.text),
               // Which conversation this was said in. The short form is the
               // same prefix the evidence ids use, so the two line up.
               conv: str(prompt.session_id).slice(0, 8),
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

  function agentOf(goal, runs, claim) {
    var rows = array(runs && runs[goal.id]);
    if (!rows.length) {
      var proposed = array(plans[goal.id]);
      if (proposed.length && !(claim && claim.goal_id === goal.id)) {
        // A proposal, plainly marked. The session's own tasks replace it.
        return { status: "proposed", branch: "", prompt: "", lastFile: "",
                 todos: proposed.map(function (step) {
                   return { t: str(step), s: "todo", active: "" };
                 }), log: [] };
      }
      // Launched but not yet started. Say that, rather than showing nothing
      // (which reads as "the button did nothing") or inventing steps.
      if (claim && claim.goal_id === goal.id) {
        return { status: "waiting", branch: "", prompt: str(claim.prompt),
                 lastFile: "",
                 todos: [], log: [] };
      }
      // Nothing has run and nothing is proposed.
      return { status: "idle", branch: "", prompt: "", lastFile: "",
               todos: [], log: [] };
    }
    var run = rows[0];
    return {
      status: run.status === "finished" ? "done" : "running",
      awaiting: !!run.awaiting_user,
      branch: str(run.git_branch),
      prompt: str(run.user_prompt),
      lastFile: "",
      todos: array(run.tasks).map(function (task) {
        return { t: str(task.subject) || "(untitled task)",
                 s: TASK_STATE[task.status] || "todo",
                 active: str(task.activeForm) };
      }),
      // Newest first on screen; the store appends, so reverse a copy.
      log: array(run.activity).slice(-24).reverse().map(function (entry) {
        return { ts: str(entry.at).slice(11, 19), m: str(entry.text) };
      })
    };
  }

  function artifactOf(goal, runs, detail) {
    var rows = array(runs && runs[goal.id]);
    var reviewed = (detail && array(detail.review)) || [];
    if (!rows.length && !reviewed.length) return null;
    // REVIEW is about what a completed run left behind, so it follows the
    // newest finished run. Reading rows[0] hid every past artifact the
    // moment a new run started.
    function done(row) {
      var state = str(row && (row.state || row.status));
      return state === "finished" || state === "failed";
    }
    var finished = rows.filter(done);
    // Prefer a finished run that actually changed something: a later run that
    // only read files would otherwise hide the one that did the work.
    var run = finished.filter(function (r) {
      return array(r.files).length;
    })[0] || finished[0] || rows[0] || {};
    var latest = reviewed.filter(done)[0] || reviewed[0] || {};
    var files = array(latest.files).length ? array(latest.files)
      : array(run.files);
    // The write-up belongs to whichever run produced one. Reading it off the
    // run picked for its files left the card blank whenever the run that
    // explained itself and the run that edited files were different ones —
    // which, at boot, is the only copy of the summary the page ever sees.
    var wrote = [latest].concat(finished, rows).filter(function (r) {
      return r && str(r.summary);
    })[0];
    // The question this pane answers: what has it done, and does it need me?
    var state = str(latest.state) || (run.status === "finished" ? "finished"
                                                               : "running");
    var head = [];
    if (state) {
      head.push(({ running: "Running", waiting: "Waiting on you",
                   finished: "Completed", failed: "Failed" })[state] || state);
    }
    if (latest.elapsed) head.push(str(latest.elapsed));
    var tasks = latest.tasks || {}, subs = latest.subgoals || {};
    return {
      state: state,
      headline: head.join(" \u00b7 "),
      did: array(latest.did).map(function (entry) {
        return { at: str(entry.at), kind: str(entry.kind),
                 text: str(entry.text) };
      }),
      attention: str(latest.attention),
      checked: array(latest.checked).map(str),
      progress: (tasks.total
        ? tasks.done + "/" + tasks.total + " steps"
        : "") + (subs.total
        ? (tasks.total ? "  \u00b7  " : "") + subs.done + "/" + subs.total
          + " subgoals complete" : ""),
      resume: str(latest.resume),
      ts: Date.parse(run.finished_at || run.started_at || "") || Date.now(),
      branch: str(run.git_branch),
      status: run.status === "finished" ? "ready" : "pending",
      // Any completed run is something to review, even while a newer one
      // is still going.
      finished: finished.length > 0 || reviewed.some(done),
      note: "",
      summary: wrote ? str(wrote.summary) : "",
      files: files.map(function (f) {
        return { path: str(f.path), edits: f.edits || 0 };
      }),
      how: array(latest.how)
    };
  }

  function toNode(goal, byParent, byId, runs, claim) {
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
      agent: agentOf(goal, runs, claim),
      artifact: artifactOf(goal, runs, details[goal.id]),
      children: array(byParent[goal.id]).map(function (child) {
        return toNode(child, byParent, byId, runs, claim);
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
    // Read the claim from the state being rendered, not from module state:
    // the mapping is a pure function of one payload.
    var claim = (st && st.agent_claim && typeof st.agent_claim === "object")
      ? st.agent_claim : null;
    return array(byParent[null]).map(function (goal) {
      return toNode(goal, byParent, byId, (st && st.agent_runs) || {}, claim);
    });
  }

  function seedPayload(st, roots, saved) {
    saved = saved && typeof saved === "object" ? saved : {};
    var flat = flattenTree(roots).map;
    var filters = { active: true, inprog: true, done: true, all: true };
    var tabs = { context: true, prompt: true, agent: true, artifact: true };
    var selection = typeof saved.selId === "string" && flat[saved.selId] ?
      saved.selId : (roots.length ? roots[0].id : null);
    var chat = !!(st && st.scope === "chat");
    // Only a store we wrote carries a filter the reader actually chose. A
    // page opened for the first time in a chat has no such history, and a
    // chat's tree is small enough that hiding most of it behind "active"
    // reads as an empty workspace.
    var mine = saved.v >= 7;
    var filter = filters[saved.filter] ? saved.filter : null;
    var paneTab = tabs[saved.paneTab] ? saved.paneTab : "context";
    // The panes those two tabs open are driven by ops this scope refuses,
    // so a saved value pointing at one would restore an empty inspector.
    if (chat && (paneTab === "agent" || paneTab === "artifact")) {
      paneTab = "context";
    }
    return {
      v: 7,
      goals: roots,
      selId: selection,
      filter: chat ? ((filter && mine) ? filter : "all")
                   : (filter || "active"),
      updatedAt: st.generated_at ? Date.parse(st.generated_at) : Date.now(),
      labels: array(saved.labels),
      paneTab: paneTab,
      themeMode: saved.themeMode === "light" || saved.themeMode === "dark" ?
        saved.themeMode : null,
      view: saved.view === "tree" || saved.view === "inspect" ? saved.view : "split",
      // A chat workspace has one conversation -- its own -- and no page to
      // list them on, so there is nowhere for 'convos' to land.
      page: (saved.page === "convos" && !chat) ? "convos" : "goals",
      // Onboarding is the artifact's, and these are the real answers: what the
      // vault has actually been told, not an assumption that it is set up.
      // A chat workspace was never asked any of it: its goals are inferred
      // from this chat by the Claude CLI, and the transcript it reads is the
      // one the chat is already keeping. Answering the wizard's questions
      // here is reporting that, not assuming it -- and /api/setup speaks for
      // the global vault only, so it is not consulted.
      setup: chat ? { sv: 9, storage: true, analysis: "claude", done: true }
                  : (setupState ||
                     { sv: 9, storage: false, analysis: null, done: false })
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
      // A launch that has not been started yet is real state: the terminal is
      // open with the prompt typed, waiting on a keypress the UI cannot make.
      claim: (st.agent_claim && typeof st.agent_claim === "object")
        ? st.agent_claim : null,
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
      // Same reason as the state fetch: the panels are baked into the
      // artifact's saved state at boot, and anything fetched afterwards has
      // nowhere to land until the page reloads.
      var briefs = new XMLHttpRequest();
      briefs.open("GET", "/api/briefings", false);
      briefs.send();
      var all = JSON.parse(briefs.responseText);
      if (all && all.ok && all.goals) {
        Object.keys(all.goals).forEach(function (id) {
          var one = all.goals[id] || {};
          details[id] = { sections: briefingSections(one), opening: "",
                          cwd: str(one.cwd), review: [],
                          brief: briefFacts(one) };
        });
      }
    } catch (e) {
      // No briefings: the panels stay empty rather than showing a guess.
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

  var plans = Object.create(null);
  var planPending = Object.create(null);

  // Whether the goals panel is currently drawing its own "Building Goals"
  // spinner. Asking the screen rather than inferring it from counts matters:
  // that spinner lives in state the artifact keeps in memory, so a reload
  // takes it away while the tree is still being built. Inferring would then
  // silence the banner too, and the page would report nothing at all.
  function treeSpinnerShown() {
    var node = document.querySelector && document.querySelector(".hc-anpanel");
    if (!node) return false;
    // Present is not the same as on screen. The goals panel stays in the
    // document with display:none while the conversations page is showing, so
    // testing for it alone suppressed the banner on both pages -- and the
    // conversations page has no panel of its own to report instead.
    if (typeof node.offsetParent !== "undefined") {
      return node.offsetParent !== null;
    }
    return true;
  }

  // What the vault is doing right now, read from the server on every poll
  // rather than from state the artifact keeps in memory. A reload must not
  // be able to make a running analysis invisible.
  window.__hcAnalysisNow = function () {
    var counts = (setupState && setupState.conversations) || {};
    var active = Object.create(null);
    array(setupState && setupState.active).forEach(function (sid) {
      active[str(sid)] = true;
    });
    var phase = str(setupState && setupState.phase);
    return {
      running: !!(setupState && setupState.running),
      phase: phase,
      // Synthesis reads every conversation at once, so no single row is the
      // one being worked on; extraction has a real handful.
      active: phase === "synthesizing" ? Object.create(null) : active,
      total: counts.total || 0,
      analyzed: counts.analyzed || 0
    };
  };

  function selectedPane() {
    try {
      var saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      return typeof saved.paneTab === "string" ? saved.paneTab : "context";
    } catch (e) {
      return "context";
    }
  }

  function loadPlan(goalId) {
    // One model call per goal, cached by the server. Asked for only when the
    // reader opens the pane that shows it, not on every render.
    if (!goalId || plans[goalId] || planPending[goalId]) return;
    if (serverState.scope === "chat") return;
    planPending[goalId] = true;
    fetch("/api/plan?goal=" + encodeURIComponent(goalId), { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        plans[goalId] = (body && body.ok) ? array(body.steps) : [];
        lastObservedGoals = null;
        refreshState();
      })
      .catch(function () { delete planPending[goalId]; });
  }

  var liveShown = "";

  function liveRows(run) {
    var rows = [];
    var head = ({ running: "Running", waiting: "Waiting on you",
                  finished: "Completed", failed: "Failed",
                  // Launched, but the session has not spoken yet. Saying
                  // nothing here reads as the run button having done nothing.
                  starting: "Starting \u2014 the terminal is open with your "
                            + "prompt; press Enter there"
                })[str(run.state)] || str(run.state);
    if (run.elapsed) head += " \u00b7 " + str(run.elapsed);
    rows.push(["head", head]);
    // No "ask" row: the artifact card already shows that message as its
    // summary, and the state line above it says it is waiting. Printing it
    // twice in one card is not emphasis.
    // Inferred from silence, not observed: Claude Code does not tell us it
    // asked something, so say "may be" and show what the guess rests on.
    if (run.quiet_for && str(run.state) === "running") {
      rows.push(["idle", "nothing for " + str(run.quiet_for)
                 + " \u2014 it may be waiting for you in the terminal"]);
    }
    var waiting = str(run.state) === "waiting";
    array(run.did).slice().reverse().forEach(function (entry, index) {
      var mark = entry.kind === "task" ? "\u2713"
        : (entry.kind === "turn" ? "\u23f8" : "\u00b7");
      // The newest turn is the one the reader is being kept waiting by; say
      // so on the line itself, not only in the heading.
      var kind = (waiting && index === 0 && entry.kind === "turn")
        ? "wait" : "did";
      var text = str(entry.at) + "  " + mark + "  " + str(entry.text);
      if (kind === "wait") text += "   \u2014 waiting for your decision";
      rows.push([kind, text]);
    });
    array(run.checked).forEach(function (command) {
      rows.push(["check", "verified by running: " + str(command)]);
    });
    var tasks = run.tasks || {}, subs = run.subgoals || {};
    var progress = [];
    if (tasks.total) progress.push(tasks.done + "/" + tasks.total + " steps");
    if (subs.total) {
      progress.push(subs.done + "/" + subs.total + " subgoals complete");
    }
    if (progress.length) rows.push(["foot", progress.join("  \u00b7  ")]);
    return rows;
  }

  function renderLive(goalId, runs) {
    var host = document.querySelector(".hc-live");
    if (!host) return false;
    // The decision about the run sits between the question and the log, so
    // the buttons stay under the thing they answer.
    var below = document.querySelector(".hc-live-rest") || host;
    var run = array(runs)[0];
    var rows = run ? liveRows(run) : [];
    var stamp = goalId + "|" + JSON.stringify(rows);
    if (stamp === liveShown && host.children && host.children.length) {
      return true;                       // nothing changed; leave the DOM be
    }
    liveShown = stamp;
    while (host.firstChild) host.removeChild(host.firstChild);
    if (!rows.length) return true;
    ensureLiveStyles();
    var head = document.createElement("div");
    head.className = "hc-live-top";
    host.appendChild(head);
    // The log scrolls in place: a run of any length otherwise pushes
    // everything below it off the page.
    while (below !== host && below.firstChild) {
      below.removeChild(below.firstChild);
    }
    var log = document.createElement("div");
    log.className = "hc-live-log";
    var logTitle = document.createElement("div");
    logTitle.className = "hc-live-title";
    logTitle.textContent = "ACTIVITY";
    var LOGGED = { did: true, wait: true, idle: true };
    rows.forEach(function (row) {
      var node = document.createElement("div");
      node.className = "hc-live-" + row[0];
      node.textContent = row[1];
      if (row[0] === "head") head.appendChild(node);
      else if (row[0] === "ask") host.appendChild(node);
      else if (LOGGED[row[0]]) {
        if (!log.parentNode) {
          below.appendChild(logTitle);
          below.appendChild(log);
        }
        log.appendChild(node);
      } else below.appendChild(node);
    });
    var session = run && str(run.session_id);
    if (session) {
      // Reading the command was never the point: the reader wants to be in
      // that conversation, especially when it is waiting on them.
      var open = document.createElement("button");
      open.className = "hc-live-open";
      open.textContent = "open the conversation";
      open.onclick = function () {
        open.disabled = true;
        open.textContent = "opening…";
        post({ op: "resume_agent_run", goal_id: goalId,
               session_id: session }).then(function (result) {
          open.disabled = false;
          open.textContent = (result && result.ok === true)
            ? "open the conversation"
            : ((result && result.error) || "could not open it");
        });
      };
      // The section corner, where the status badge used to sit. It is outside
      // .hc-live, so clearing the anchor does not clear it — empty it here or
      // every redraw stacks another button in it.
      var slot = document.querySelector(".hc-live-open-slot");
      while (slot && slot.firstChild) slot.removeChild(slot.firstChild);
      (slot || head).appendChild(open);
    }
    return true;
  }

  // Linking an existing prompt to a goal. The inference is a guess, and the
  // reader is the only one who knows which of their own words belong to
  // which goal -- so attaching is theirs to do. The op already exists
  // server-side; nothing here writes local-only state.
  function promptWhen(prompt) {
    var parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str(prompt.created_at));
    var date = parts ? new Date(+parts[1], +parts[2] - 1, +parts[3])
                     : new Date(Date.parse(prompt.created_at || ""));
    if (isNaN(date.getTime())) return "";
    return date.toLocaleDateString("en-US",
      { month: "short", day: "numeric", year: "numeric" });
  }

  // High enough that no real vault is truncated, low enough that a
  // pathological one cannot lock the tab building rows.
  var PICK_LIMIT = 2000;

  function pickPrompt(goalId) {
    var goal = array(serverState.goals).filter(function (g) {
      return g && g.id === goalId;
    })[0];
    var linked = {};
    array(goal && goal.prompt_ids).forEach(function (id) { linked[id] = true; });
    var pool = array(serverState.prompts).filter(function (prompt) {
      return !linked[prompt.id];
    }).slice().reverse();
    return new Promise(function (resolve) {
      ensureDialogStyles();
      ensurePaneStyles();
      var overlay = document.createElement("div");
      overlay.className = "hc-ask";
      var box = document.createElement("div");
      box.className = "hc-ask-box hc-pick-box";
      var title = document.createElement("div");
      title.className = "hc-ask-title";
      title.textContent = "Add one of your prompts to this goal";
      var filter = document.createElement("input");
      filter.type = "text";
      filter.className = "hc-ask-input";
      filter.placeholder = "Filter your prompts\u2026";
      var count = document.createElement("div");
      count.className = "hc-pick-count";
      var list = document.createElement("div");
      list.className = "hc-pick-list";

      function close(value) {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        resolve(value || null);
      }

      function draw() {
        // str(): a value that is not a string throws inside a promise
        // executor, which rejects it with nobody listening -- the modal
        // then sits there empty and says nothing at all.
        var needle = str(filter.value).trim().toLowerCase();
        var matched = pool.filter(function (prompt) {
          return !needle || str(prompt.text).toLowerCase().indexOf(needle) >= 0;
        });
        var shown = matched.slice(0, PICK_LIMIT);
        count.textContent = matched.length === pool.length
          ? pool.length + " prompts of yours are not on this goal yet"
          : matched.length + " of " + pool.length + " match";
        while (list.firstChild) list.removeChild(list.firstChild);
        if (!shown.length) {
          var none = document.createElement("div");
          none.className = "hc-pick-none";
          none.textContent = pool.length
            ? "No prompt of yours matches that."
            : "Every prompt on record is already on this goal.";
          list.appendChild(none);
          return;
        }
        shown.forEach(function (prompt) {
          var row = document.createElement("button");
          row.className = "hc-pick-row";
          var when = document.createElement("span");
          when.className = "hc-pick-when";
          when.textContent = promptWhen(prompt);
          var text = document.createElement("span");
          text.className = "hc-pick-text";
          text.textContent = str(prompt.text);
          row.appendChild(when);
          row.appendChild(text);
          row.onclick = function () { close(prompt.id); };
          list.appendChild(row);
        });
        // Say what was left out rather than letting a capped list read as
        // the whole record.
        if (matched.length > shown.length) {
          var more = document.createElement("div");
          more.className = "hc-pick-none";
          more.textContent = "Showing the newest " + shown.length + " of "
            + matched.length + ". Filter to narrow it down.";
          list.appendChild(more);
        }
      }

      var row = document.createElement("div");
      row.className = "hc-ask-row";
      var cancel = document.createElement("button");
      cancel.className = "hc-ask-btn";
      cancel.textContent = "Cancel";
      cancel.onclick = function () { close(null); };
      row.appendChild(cancel);
      filter.oninput = draw;
      filter.onkeydown = function (e) {
        if (e.key === "Escape") { e.preventDefault(); close(null); }
      };
      overlay.onclick = function (e) { if (e.target === overlay) close(null); };
      box.appendChild(title);
      box.appendChild(filter);
      box.appendChild(count);
      box.appendChild(list);
      box.appendChild(row);
      overlay.appendChild(box);
      (document.body || document.documentElement).appendChild(overlay);
      // Anything thrown from here rejects a promise the caller only
      // listens to for a chosen id, so a failure reads as "the button
      // does nothing". Say what went wrong, in the box being looked at.
      try {
        draw();
      } catch (error) {
        var broke = document.createElement("div");
        broke.className = "hc-pick-none";
        broke.textContent = "Could not list your prompts: " + error;
        list.appendChild(broke);
      }
      if (filter.focus) filter.focus();
    });
  }

  function promptAddSlot() {
    var slot = document.querySelector(".hc-prompt-add");
    if (slot) return slot;
    // The anchor is an empty span in a template the artifact re-renders from
    // its own state, and an empty element is exactly the kind of thing a
    // renderer is free to drop. The heading is text the pane has to draw, so
    // finding that and using its row is the version that cannot go missing.
    var heads = document.querySelectorAll("span, div");
    for (var i = 0; i < heads.length; i++) {
      var node = heads[i];
      if (node.children && node.children.length) continue;
      if (str(node.textContent).trim() !== "RELATED PROMPTS") continue;
      return node.parentNode || null;
    }
    return null;
  }

  function openPromptPicker(button) {
    // One at a time: a second overlay would sit on top of the first with no
    // way back to it.
    if (document.querySelector(".hc-ask")) return;
    var goalId = selectedGoalId();
    if (!goalId) {
      button.textContent = "select a goal first";
      return;
    }
    pickPrompt(goalId).catch(function (error) {
      // Never fail quietly: a click that does nothing is the one bug
      // the reader cannot report usefully.
      button.textContent = "could not open it: " + error;
      return null;
    }).then(function (promptId) {
      if (!promptId) return;
      button.disabled = true;
      button.textContent = "adding\u2026";
      post({ op: "attach_prompt", goal_id: goalId,
             prompt_id: promptId }).then(function (result) {
        button.disabled = false;
        button.textContent = (result && result.ok === true)
          ? "+ add a prompt"
          : ((result && result.error) || "could not add it");
        // The list is drawn from state, so the new link only shows once the
        // next state lands.
        refreshState();
      });
    });
  }

  var promptAddBound = false;

  function bindPromptAdd() {
    // Delegated, not bound to the node. The artifact re-renders this pane
    // from its own state every time a poll lands, which destroys whatever
    // button was there -- and a click that begins on a node replaced before
    // it completes is a click that goes nowhere. Listening on the document
    // and matching by class survives every redraw.
    if (promptAddBound || !document.addEventListener) return;
    promptAddBound = true;
    document.addEventListener("click", function (event) {
      var node = event && event.target;
      while (node && node !== document) {
        var name = node.className ? String(node.className) : "";
        if (name.indexOf("hc-prompt-addbtn") >= 0) {
          if (event.preventDefault) event.preventDefault();
          if (event.stopPropagation) event.stopPropagation();
          openPromptPicker(node);
          return;
        }
        node = node.parentNode;
      }
    }, true);
  }

  function renderPromptAdd() {
    if (serverState.scope === "chat") return false;
    var slot = promptAddSlot();
    if (!slot) return false;
    bindPromptAdd();
    if (slot.querySelector && slot.querySelector(".hc-prompt-addbtn")) {
      return true;
    }
    ensurePaneStyles();
    var button = document.createElement("button");
    button.className = "hc-prompt-addbtn";
    button.type = "button";
    button.textContent = "+ add a prompt";
    slot.appendChild(button);
    return true;
  }

  function watchPromptAdd() {
    renderPromptAdd();
    setInterval(renderPromptAdd, 700);
  }

  // --- controls a chat workspace has no backend for ------------------------
  // The artifact was drawn for the global vault. Three of its controls lead
  // somewhere this scope cannot go. The Conversations page lists a vault's
  // whole history, which arrives on /api/setup and /api/conversation -- both
  // refuse here, and the artifact answers a refusal by falling back to its
  // own sample list, so the page would read as a history nobody has. The
  // AGENT and REVIEW tabs open panes whose every op -- /api/plan,
  // launch_agent_run, resume_agent_run, /api/review -- refuses too. All
  // three are taken off the page rather than left to fail on click.

  function leafSpansNamed(name) {
    var out = [], nodes = document.querySelectorAll("span");
    for (var i = 0; i < nodes.length; i++) {
      // Leaf nodes only: an ancestor's textContent contains its children's,
      // so a wrapper would match the name its child carries.
      if (nodes[i].children && nodes[i].children.length) continue;
      if (str(nodes[i].textContent).trim() === name) out.push(nodes[i]);
    }
    return out;
  }

  function paneTabBar() {
    // Named by the one tab every scope keeps, for the same reason
    // promptAddSlot is anchored on a heading: the artifact re-renders this
    // row from its own state, so text it has to draw is the only handle
    // that cannot be re-rendered away.
    var anchor = leafSpansNamed("CONTEXT")[0];
    return anchor ? anchor.parentNode : null;
  }

  function hideNode(node) {
    if (!node || !node.style || node.style.display === "none") return false;
    node.style.display = "none";
    return true;
  }

  function renderChatSurface() {
    if (serverState.scope !== "chat") return false;
    var hidden = 0;
    leafSpansNamed("Conversations").forEach(function (span) {
      if (hideNode(span)) hidden += 1;
    });
    var bar = paneTabBar();
    var kids = (bar && bar.children) || [];
    for (var i = 0; i < kids.length; i++) {
      var label = str(kids[i].textContent).trim();
      // REVIEW arrives late -- it is behind an sc-if that only turns on
      // once a run exists -- so this is a standing sweep, not a one-shot.
      if (label === "AGENT" || label === "REVIEW") {
        if (hideNode(kids[i])) hidden += 1;
      }
    }
    return hidden > 0;
  }

  function watchChatSurface() {
    renderChatSurface();
    setInterval(renderChatSurface, 700);
  }

  function watchRunFeed() {
    // The artifact reads its state at boot, so a live feed cannot travel
    // through it. Poll and draw straight into the pane instead.
    function tick() {
      // Tied to the target, not to a pane name: the feed moved from REVIEW to
      // AGENT and a hardcoded pane left it fetching for a pane it no longer
      // draws into. If the anchor is on screen, it should be fed.
      if (!document.querySelector(".hc-live")) return Promise.resolve(false);
      var id = selectedGoalId();
      if (!id || serverState.scope === "chat") return Promise.resolve(false);
      return fetch("/api/review?goal=" + encodeURIComponent(id),
                   { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          if (!(body && body.ok)) return false;
          var rows = array(body.runs);
          // The run record only exists once a hook fires. Between pressing
          // run and that first hook there is a real state -- a terminal open
          // with the prompt typed -- and no row to carry it.
          if (!rows.length && serverState.claim
              && serverState.claim.goal_id === id) {
            rows = [{ state: "starting", did: [], checked: [] }];
          }
          return !!renderLive(id, rows);
        })
        .catch(function () { return false; });
    }
    var first = tick();          // opening the pane should not wait a tick
    setInterval(tick, 2000);
    return first;
  }

  function watchPane() {
    setInterval(function () {
      if (selectedPane() !== "agent") return;
      var id = selectedGoalId();
      // A goal that has really run shows its own tasks; never spend a call
      // proposing steps for work that already happened.
      if (id && !array(serverState.runs && serverState.runs[id]).length) {
        loadPlan(id);
      }
    }, 600);
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

  function briefFacts(brief) {
    // What a run is about to do, from the same payload the prompt is built
    // from. Counts rather than prose: the prompt itself is a click away, and
    // a reader deciding whether to press run wants the shape, not the text.
    var counts = [];
    array((brief || {}).sections).forEach(function (section) {
      var n = array(section.lines).filter(function (line) {
        return str(line).trim();
      }).length;
      var title = str(section.title);
      if (!n) return;
      if (title.indexOf("IN THEIR WORDS") >= 0) {
        counts.push(n + " of your own messages");
      } else if (title.indexOf("ALREADY DECIDED") === 0) {
        counts.push(n + " decision" + (n === 1 ? "" : "s"));
      } else if (title.indexOf("ALREADY BUILT") === 0) {
        counts.push(n + " thing" + (n === 1 ? "" : "s") + " already built");
      } else if (title.indexOf("PROBLEMS HIT") === 0) {
        counts.push(n + " problem" + (n === 1 ? "" : "s") + " hit before");
      } else if (title.indexOf("STILL OPEN") === 0) {
        counts.push(n + " still open");
      } else if (title.indexOf("EARLIER CLAUDE SESSIONS") === 0) {
        counts.push(n + " earlier session" + (n === 1 ? "" : "s"));
      }
    });
    var dirs = array((brief || {}).add_dirs);
    var refs = array((brief || {}).references);
    return {
      cwd: str((brief || {}).cwd),
      dirs: dirs.map(str),
      refs: refs.map(str),
      told: counts
    };
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
    // Seeding fills `details` with the briefing before boot, so "have an
    // entry" is not "have fetched the run history" — guarding on the entry
    // meant /api/review was never called and REVIEW could never fill.
    if (!goalId || detailPending[goalId]) return;
    if (details[goalId] && details[goalId].loaded) return;
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
                          cwd: str(brief.cwd), review: array(review.runs),
                          brief: briefFacts(brief), loaded: true };
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

  var DIALOG_CSS = [
      ".hc-ask{position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.28);display:flex;align-items:center;justify-content:center;padding:20px}",
      ".hc-ask-box{width:min(460px,100%);background:var(--panel,#fff);color:var(--ink,#111);border:1px solid var(--bd2,#d5d5d5);border-radius:3px;box-shadow:0 18px 60px rgba(0,0,0,.2);padding:16px;font-family:'Source Code Pro',monospace}",
      ".hc-ask-title{font:600 12px 'Source Code Pro',monospace;margin-bottom:10px;color:var(--ink)}",
      ".hc-ask-input{width:100%;box-sizing:border-box;border:1px solid var(--bd2);border-radius:2px;background:var(--panel2);color:var(--ink);outline:none;padding:8px 10px;font:12px 'Source Code Pro',monospace}",
      ".hc-ask-input:focus{border-color:var(--acc)}",
      ".hc-ask-row{display:flex;justify-content:flex-end;align-items:center;gap:4px;margin-top:14px}",
      ".hc-ask-btn{border:1px solid var(--bd2);background:transparent;color:var(--fnt);border-radius:2px;padding:5px 12px;cursor:pointer;font:11px 'Source Code Pro',monospace}",
      ".hc-ask-btn:hover{color:var(--ink)}",
      ".hc-ask-ok{background:var(--acc);border-color:var(--acc);color:var(--onacc)}",
      ".hc-pick-box{width:min(760px,100%);display:flex;flex-direction:column;max-height:min(84vh,760px)}",
      ".hc-pick-list{flex:1;min-height:0;margin-top:10px;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--bd,#e6e6e6);border-radius:2px;background:var(--panel2,#fafafa)}",
      ".hc-pick-count{margin-top:8px;font:10.5px 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b)}",
      ".hc-pick-row{width:100%;box-sizing:border-box;display:block;text-align:left;border:none;border-bottom:1px solid var(--bd,#e6e6e6);background:transparent;color:var(--dtxt,#333);padding:8px 11px;cursor:pointer;font:11.5px/1.6 'Source Code Pro',monospace}",
      ".hc-pick-row:last-child{border-bottom:none}",
      ".hc-pick-row:hover{background:var(--hov,#f4f4f4);color:var(--ink,#111)}",
      ".hc-pick-when{display:block;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt,#9b9b9b);margin-bottom:3px}",
      ".hc-pick-text{display:block;white-space:pre-wrap;word-break:break-word}",
      ".hc-pick-none{padding:12px 11px;font:11.5px 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b)}",
  ].join("");

  function ensureDialogStyles() {
    if (document.getElementById("hc-ask-style")) return;
    var style = document.createElement("style");
    style.id = "hc-ask-style";
    style.textContent = DIALOG_CSS;
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
       "paneTab: (saved && saved.hcKeepPane && ['prompt', 'agent', 'artifact', 'context'].indexOf(saved.paneTab) >= 0) ? saved.paneTab : 'context',"],
      // A chat workspace is not offered AGENT or REVIEW -- the ops behind
      // them answer "global scope only" here -- so the keyboard must not
      // step onto them either. It reads the scope at the moment of the
      // keypress rather than at patch time, which keeps this one string
      // true for whichever server the artifact is served from.
      ["const tabs = ['context', 'prompt', 'agent', 'artifact'];",
       "const tabs = (typeof window !== 'undefined' && window.__hcScope === 'chat') ? ['context'] : ['context', 'prompt', 'agent', 'artifact'];"],
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
      ["  startAnalysis() {\n    clearInterval(this._anT);\n    const n = this.CONVOS.length;\n    this.set(() => ({ page: 'convos', convSel: null, an: { phase: 'convs', total: n, prog: Array(n).fill(0) } }));\n    this._anT = setInterval(() => {\n      const a = this.state.an;\n      if (!a) { clearInterval(this._anT); return; }\n      // up to 5 concurrent, ~15s each\n      let active = 0;\n      const prog = a.prog.map(p => {\n        if (p >= 100) return p;\n        if (active >= 5) return p;\n        active++;\n        return Math.min(100, p + 0.9 + Math.random() * 1.0);\n      });\n      this.setState({ an: { ...a, prog } });\n      if (prog.every(p => p >= 100)) {\n        clearInterval(this._anT);\n        setTimeout(() => {\n          this.set(() => ({ page: 'goals', an: { phase: 'goals', total: 63, done: 0 } }));\n          this._anT = setInterval(() => {   // ~30s for 63 conversations\n            const g = this.state.an;\n            if (!g) { clearInterval(this._anT); return; }\n            const gd = Math.min(g.total, g.done + 1);\n            this.setState({ an: { ...g, done: gd } });\n            if (gd >= g.total) { clearInterval(this._anT); setTimeout(() => this.setState({ an: null }), 1200); }\n          }, 470);\n        }, 1000);\n      }\n    }, 200);\n  }\n", "  startAnalysis() {\n    clearInterval(this._anT);\n    // Real progress: the vault reports how many conversations it has and how\n    // many it has finished. Nothing here invents a duration.\n    this._anSwitched = false;\n    const tick = () => {\n      const setup = window.__hcSetup;\n      if (!setup) { clearInterval(this._anT); this.setState({ an: null }); return; }\n      setup.progress().then((s) => {\n        if (!s) return;\n        const counts = s.conversations || { total: 0, analyzed: 0 };\n        const total = counts.total || (window.__hcConvos || []).length;\n        const done = Math.min(counts.analyzed || 0, total);\n        if (!total || done < total) {\n          const prog = Array.from({ length: total }, (_, k) =>\n            k < done ? 100 : (k === done && s.running ? 50 : 0));\n          this.setState({ an: { phase: 'convs', total, prog, done } });\n          return;\n        }\n        // Every conversation has been read. The tree is built from all of\n        // them at once, so there is nothing per-row left to watch: move to\n        // the goals page, where the panel itself says what is happening.\n        // Switch once and not on every poll, or leaving the page is\n        // impossible while the tree is building.\n        if (!this._anSwitched) {\n          this._anSwitched = true;\n          this.set(() => ({ page: 'goals', convSel: null,\n            an: { phase: 'goals', total, done } }));\n        } else {\n          this.setState({ an: { phase: 'goals', total, done } });\n        }\n        if (!s.running) {\n          clearInterval(this._anT);\n          setTimeout(() => this.setState({ an: null }), 800);\n        }\n      });\n    };\n    this.set(() => ({ page: 'convos', convSel: null,\n      an: { phase: 'convs', total: (window.__hcConvos || []).length, prog: [], done: 0 } }));\n    tick();\n    this._anT = setInterval(tick, 2000);\n  }\n"],
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
      // A part-filled bar claims a conversation is a known fraction done.
      // It is not: one call either returns or it does not. Dots that travel
      // say "working" without inventing a percentage.
      // Only the conversation actually being read gets the bar; the
      // rest were showing the same animation while nothing was
      // happening to them, which said the machine was busy on all of
      // them at once. And every state now says what it is, in one
      // column, so the list can be read straight down.
      ["        const ph = anx && anx.phase === 'convs';\n        const p = ph ? (anx.prog[i] || 0) : 0;\n",
       "        const an = window.__hcAnalysisNow ? window.__hcAnalysisNow() : null;\n"],
      ["          stShow: !!(ph && p >= 100), st: '\u2713 analyzed', stC: 'var(--mut)',\n          qShow: false,\n          barShow: !!(ph && p < 100),\n          barW: Math.round(p) + '%',\n",
       "          stShow: !!c.done, st: '\u2713 analyzed', stC: 'var(--mut)',\n          qShow: !!(an && an.running && !c.done && !an.active[c.id]),\n          barShow: !!(an && an.running && !c.done && !!an.active[c.id]),\n          barW: '',\n"],
      ["<span style=\"display:inline-flex;width:78px;justify-content:flex-end;align-items:center\"><sc-if value=\"{{ cv.stShow }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;color:{{ cv.stC }}\">{{ cv.st }}</span></sc-if><sc-if value=\"{{ cv.barShow }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"display:inline-block;width:72px;height:3px;border-radius:2px;background:var(--accbg);overflow:hidden\"><span style=\"display:block;height:100%;width:{{ cv.barW }};background:var(--acc)\"></span></span></sc-if></span>",
       "<span style=\"display:inline-flex;width:118px;justify-content:flex-end;align-items:center\"><sc-if value=\"{{ cv.stShow }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;color:{{ cv.stC }}\">{{ cv.st }}</span></sc-if><sc-if value=\"{{ cv.qShow }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;color:var(--fnt)\">in queue</span></sc-if><sc-if value=\"{{ cv.barShow }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"display:inline-flex;flex-direction:column;align-items:flex-end;gap:3px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;color:var(--acc)\">analyzing\u2026</span><span class=\"hc-rowbar\"><span></span></span></span></sc-if></span>"],
      // Launching should land the reader where the work will appear, and
      // the pane must distinguish 'typed, not started' from 'running'.
      ["  runAgent() {\n    const id = this.state.selId;\n    if (!id) return;\n    this.recordPrompt(this._draftEl ? this._draftEl.value : '');\n    // Opens a terminal in this goal's project with the prompt typed and\n    // unsent. Its tasks then arrive here as it creates them, for real.\n    if (window.__hcAgent) window.__hcAgent.launch(id);\n  }\n",
       "  runAgent() {\n    const id = this.state.selId;\n    if (!id) return;\n    this.recordPrompt(this._draftEl ? this._draftEl.value : '');\n    // Opens a terminal in this goal's project with the prompt typed and\n    // unsent. Its tasks then arrive here as it creates them, for real.\n    // Opening the modal is not launching: the tab only changes once a\n    // run actually exists to review.\n    if (window.__hcAgent) {\n      window.__hcAgent.launch(id).then((started) => {\n        if (started) this.set(() => ({ paneTab: 'artifact' }));\n      }).catch(() => {});\n    }\n  }\n"],
      ["agentLabel: (() => {\n        if (!sel || !sel.agent) return '';\n        const td = sel.agent.todos || [], dn = td.filter(o => o.s === 'done').length;\n        if (sel.agent.status === 'running') return 'working on this goal \u2014 ' + dn + '/' + td.length + ' steps';\n        return 'finished ' + (td.length ? dn + '/' + td.length + ' steps' : '') + ' \u2014 output ready to review';\n      })(),",
       "agentLabel: (() => {\n        if (!sel || !sel.agent) return '';\n        const a = sel.agent, td = a.todos || [];\n        const dn = td.filter(o => o.s === 'done').length;\n        const steps = td.length ? ' \u00b7 ' + dn + ' of ' + td.length + ' steps done' : '';\n        if (a.status === 'idle') return 'nothing has run on this goal yet';\n        if (a.status === 'proposed') return 'has not run yet \u00b7 the steps below are a suggestion';\n        if (a.status === 'waiting') return 'the terminal is open with the prompt typed \u00b7 press Enter there to start';\n        if (a.awaiting) return 'waiting for your reply in the terminal' + steps;\n        if (a.status === 'running') return 'running now' + steps;\n        return 'finished' + steps + ' \u00b7 the result is in REVIEW';\n      })(),"],
      ["<span sc-camel-on-click=\"{{ tabArt }}\" style=\"padding:0 2px 7px;font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;cursor:pointer;color:{{ tarC }};border-bottom:2px solid {{ tarBd }};margin-bottom:-1px\">REVIEW</span>",
       "<sc-if value=\"{{ showReviewTab }}\" hint-placeholder-val=\"{{ false }}\"><span sc-camel-on-click=\"{{ tabArt }}\" style=\"padding:0 2px 7px;font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;cursor:pointer;color:{{ tarC }};border-bottom:2px solid {{ tarBd }};margin-bottom:-1px\">REVIEW</span></sc-if>"],
      ["artFiles: (art ? (art.files || []) : []).map(",
       "showReviewTab: !!(art || (sel && sel.agent && (sel.agent.status === 'running' || sel.agent.status === 'waiting'))),\n      artFiles: (art ? (art.files || []) : []).map("],
      // The decision belongs with the artifact it is about, at the top of
      // the pane, not at the far end of the changed-file list.
      ["<sc-if value=\"{{ revClosed }}\" hint-placeholder-val=\"{{ false }}\">\n<div style=\"display:flex;justify-content:flex-end;gap:16px;align-items:center;margin-top:14px\"><span sc-camel-on-click=\"{{ revOpenFn }}\" style=\"font:600 11px 'Source Code Pro',monospace;color:var(--acc);cursor:pointer;user-select:none\" style-hover=\"text-decoration:underline\">request revisions</span><span sc-camel-on-click=\"{{ artApprove }}\" style=\"padding:4px 11px;border-radius:2px;background:var(--acc);color:var(--onacc);font:600 11px 'Source Code Pro',monospace;cursor:pointer;user-select:none\" style-hover=\"filter:brightness(1.08)\">approve</span></div>\n</sc-if>",
       "<!--approve moved up-->"],
      // The prompt is what the run sends, so it belongs with the run —
      // behind a disclosure, because it is long and rarely the thing the
      // reader came for.
      ["showPrompt: !!sel && paneTab === 'prompt'",
       "showPrompt: !!sel && paneTab === 'agent'"],
      ["<span sc-camel-on-click=\"{{ tabPrompt }}\" style=\"padding:0 2px 7px;font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;cursor:pointer;color:{{ tpC }};border-bottom:2px solid {{ tpBd }};margin-bottom:-1px\">PROMPT</span>\n",
       "<!--prompt folded into agent-->\n"],
      ["<sc-if value=\"{{ showPrompt }}\" hint-placeholder-val=\"{{ true }}\">\n<div style=\"margin-top:16px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">RECOMMENDED PROMPT</div>\n<div style=\"position:relative\"><textarea key=\"{{ selKey }}\" sc-camel-default-value=\"{{ draft }}\" ref=\"{{ draftRef }}\" sc-camel-on-input=\"{{ promptInput }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;min-height:96px;max-height:300px;overflow-y:auto;resize:none;margin-top:8px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 11px;font:12px/1.6 'Source Code Pro',monospace;color:var(--dtxt);outline:none\"></textarea>\n<span sc-camel-on-click=\"{{ gen }}\" title=\"Regenerate prompt\" style=\"position:absolute;right:8px;bottom:8px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:2px;font:13px/1 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none\" style-hover=\"color:var(--acc);background:var(--hov)\">\u21bb</span>\n</div>\n</sc-if>",
       "<sc-if value=\"{{ showPrompt }}\" hint-placeholder-val=\"{{ true }}\"><div style=\"margin-top:16px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">AGENT</div><div style=\"margin-top:5px;font:italic 11.5px/1.6 'Source Code Pro',monospace;color:var(--mut);max-width:62ch\">Run Claude Code on this goal with the self-contained context Vault has assembled. Progress appears in REVIEW.</div><details class=\"hc-promptbox\"><summary class=\"hc-promptsum\">RECOMMENDED PROMPT</summary>\n<div style=\"position:relative\"><textarea key=\"{{ selKey }}\" sc-camel-default-value=\"{{ draft }}\" ref=\"{{ draftRef }}\" sc-camel-on-input=\"{{ promptInput }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;min-height:96px;max-height:300px;overflow-y:auto;resize:none;margin-top:8px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 11px;font:12px/1.6 'Source Code Pro',monospace;color:var(--dtxt);outline:none\"></textarea>\n<span sc-camel-on-click=\"{{ gen }}\" title=\"Regenerate prompt\" style=\"position:absolute;right:8px;bottom:8px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:2px;font:13px/1 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none\" style-hover=\"color:var(--acc);background:var(--hov)\">\u21bb</span>\n</div>\n</details></sc-if>"],
      // AGENT reads top to bottom: what it is, the prompt it will send,
      // the notes the user adds to it, then the button that runs it.
      ["showNotes: !!sel && paneTab === 'prompt'",
       "showNotes: !!sel && paneTab === 'agent'"],
      // The goal is already named at the top of the inspector; the draft
      // restating it just pushed the actual content down.
      ["blocks.push(isSub ? 'Within the main goal \"' + (trail[0].title || 'Untitled') + '\", I am working on: ' + (sel.title || 'Untitled') + '.' : 'I am working on the goal: ' + (sel.title || 'Untitled') + '.');\n",
       "void 0;\n"],
      ["placeholder=\"Plan in markdown \u2014 # heading, - list, - [ ] task, **bold**, `code`\"",
       "placeholder=\"Add any other thoughts you would like the agent to know...\""],
      // The section reports the run's state, not only a task list.
      [">AGENT TODOS</div>",
       ">AGENT STATUS</div>"],
      // The run belongs beside the artifact box, not inside it: ACTIVITY
      // is a section like CHANGES, and BRANCH said little the changed
      // files do not.
      ["<div style=\"margin-top:8px;display:grid;grid-template-columns:72px 1fr;gap:3px 10px;align-items:baseline\">\n<span style=\"font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">BRANCH</span><span style=\"font:11px 'Source Code Pro',monospace;color:var(--dtxt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis\">{{ artBranch }}</span>\n<span style=\"font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">CREATED</span><span style=\"font:11px 'Source Code Pro',monospace;color:var(--dtxt)\">{{ artWhen }}</span>\n</div>\n</div>\n",
       "<div style=\"margin-top:14px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap\">\n<div style=\"display:flex;gap:10px;align-items:baseline\"><span style=\"font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">CREATED</span><span style=\"font:11px 'Source Code Pro',monospace;color:var(--dtxt)\">{{ artWhen }}</span></div>\n<sc-if value=\"{{ revClosed }}\" hint-placeholder-val=\"{{ false }}\">\n<div style=\"display:flex;gap:16px;align-items:center\"><span sc-camel-on-click=\"{{ revOpenFn }}\" style=\"font:600 11px 'Source Code Pro',monospace;color:{{ artDecideC }};cursor:{{ artDecideCur }};user-select:none\">request revisions</span><span sc-camel-on-click=\"{{ artApprove }}\" style=\"padding:4px 11px;border-radius:2px;background:{{ artApproveBg }};color:{{ artApproveC }};font:600 11px 'Source Code Pro',monospace;cursor:{{ artDecideCur }};user-select:none\">approve</span></div>\n</sc-if>\n</div>\n</div>\n<div class=\"hc-live-rest\"></div>"],
      // Running the agent is what produces a plan; a separate button that
      // only starts the same session was a second door to one room.
      ["<span sc-camel-on-click=\"{{ genTodos }}\" style=\"font:600 11px 'Source Code Pro',monospace;color:var(--acc);cursor:pointer;user-select:none\" style-hover=\"text-decoration:underline\">generate todos</span>",
       "<!--generate todos removed-->"],
      // The parent trail under a subgoal title: the tree on the left
      // already shows where the goal sits.
      ["<sc-if value=\"{{ hasCrumb }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"margin-top:4px;font:10.5px 'Source Code Pro',monospace;color:var(--fnt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis\">{{ crumb }}</div></sc-if>",
       "<!--crumb removed-->"],
      // The badge restated the card. The reader standing here wants to be
      // in that conversation, so the corner holds the way in instead.
      ["<span style=\"padding:2px 8px;border:1px solid {{ artBd }};border-radius:2px;background:{{ artBg }};font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:{{ artC }}\">{{ artStatusLab }}</span>",
       "<div class=\"hc-live-open-slot\"></div>"],
      // A percentage over a step count the agent invents as it goes is not
      // progress, and on a finished run it drew an empty tan track under a
      // line that already said the run was done.
      ["\n<div style=\"margin-top:10px;height:3px;border-radius:2px;background:var(--accbg);overflow:hidden\"><div style=\"height:100%;width:{{ agentPct }};background:var(--acc)\"></div></div>",
       "\n<!--agent progress bar removed-->"],
      // The line only ever distinguished running from everything else, so a
      // goal that had never run announced "finished — output ready to
      // review". It now says which of the five states this actually is. The
      // control beside it went too: stop did not stop the session and clear
      // only blanked local state until the next poll refilled it.
      ["<div style=\"display:flex;align-items:baseline;gap:14px;margin-top:10px\"><span style=\"font-size:11px;color:{{ agentC }};min-width:0\">{{ agentLabel }}</span><span sc-camel-on-click=\"{{ agentAction }}\" style=\"margin-left:auto;font:600 10.5px 'Source Code Pro',monospace;color:var(--mut);cursor:pointer\" style-hover=\"text-decoration:underline\">{{ agentActionLabel }}</span></div>",
       "<div style=\"margin-top:10px;font:11.5px/1.6 'Source Code Pro',monospace;color:{{ agentC }}\">{{ agentLabel }}</div>"],
      // Nothing has run and nothing is proposed: an empty AGENT STATUS
      // heading is not information.
      ["agentShow: !!(sel && sel.agent),",
       "agentShow: !!(sel && sel.agent && (sel.agent.status !== 'idle' || (sel.agent.todos || []).length)),"],
      ["agentRowShow: !!(sel && sel.agent && sel.agent.status !== 'planned'),",
       "agentRowShow: !!(sel && sel.agent && sel.agent.status !== 'idle'),"],
      // A run blocked on the reader is as urgent as one in flight.
      ["agentC: (sel && sel.agent && sel.agent.status === 'running') ? 'var(--acc)' : 'var(--mut)',",
       "agentC: (sel && sel.agent && (sel.agent.status === 'running' || sel.agent.awaiting)) ? 'var(--acc)' : 'var(--mut)',"],
      // Every pane is about the selected goal, so the pane must follow the
      // selection. Landing on another goal's AGENT or REVIEW — a tab that
      // may not even be offered for this one — reads as an empty pane.
      ["sel: () => { if (this._justDragged) return; this.set(() => ({ selId: n.id })); },",
       "sel: () => { if (this._justDragged) return; this.set(() => ({ selId: n.id, paneTab: 'context' })); },"],
      ["this.set(() => ({ selId: ids[nx], editId: null }));",
       "this.set(() => ({ selId: ids[nx], editId: null, paneTab: 'context' }));"],
      ["this.set(() => ({ page: 'goals', selId: curConv.goalId, editId: null }));",
       "this.set(() => ({ page: 'goals', selId: curConv.goalId, editId: null, paneTab: 'context' }));"],
      // The stamp read 5:00 PM on every prompt ever recorded: created_at is
      // a date, and the clock time was the formatter's invention.
      ["when: (() => { const d2 = new Date(p.ts); return d2.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ', ' + d2.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }); })(),",
       "when: (() => { const d2 = new Date(p.ts); return d2.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }); })(),"],
      // The context pane, read top to bottom: what finishing this means,
      // where it sits, what it can read, what was asked for, what is in the
      // way, what is done. Decisions follow what was built -- both are
      // settled, and neither belongs between the goal and its blockers.
      ["<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">DECISIONS</span><sc-if value=\"{{ inhDecided }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxDecided }}\" sc-camel-on-change=\"{{ ctxDecidedCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">BLOCKERS &amp; OPEN QUESTIONS</span><sc-if value=\"{{ inhHit }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxHit }}\" sc-camel-on-change=\"{{ ctxHitCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">ALREADY BUILT</span><sc-if value=\"{{ inhBuilt }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxBuilt }}\" sc-camel-on-change=\"{{ ctxBuiltCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>",
       "<div style=\"margin-top:15px;display:flex;align-items:baseline;justify-content:space-between;gap:12px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">RELATED PROMPTS</span><span class=\"hc-prompt-add\"></span></div>\n<div style=\"margin-top:6px;max-height:420px;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\">\n<sc-for list=\"{{ histRows }}\" as=\"hr\" hint-placeholder-count=\"2\">\n<div style=\"padding:8px 11px;border-bottom:{{ hr.bd }}\"><div style=\"display:flex;align-items:baseline;gap:10px\"><span style=\"flex:1;min-width:0;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">{{ hr.when }}</span></div><div style=\"margin-top:3px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);white-space:pre-wrap;word-break:break-word\">{{ hr.text }}</div></div>\n</sc-for>\n<sc-if value=\"{{ histEmpty }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"padding:12px 11px;font-size:11.5px;color:var(--fnt)\">No prompts of yours are tied to this goal yet.</div></sc-if>\n</div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">BLOCKERS &amp; OPEN QUESTIONS</span><sc-if value=\"{{ inhHit }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxHit }}\" sc-camel-on-change=\"{{ ctxHitCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">ALREADY BUILT</span><sc-if value=\"{{ inhBuilt }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxBuilt }}\" sc-camel-on-change=\"{{ ctxBuiltCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">DECISIONS</span><sc-if value=\"{{ inhDecided }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxDecided }}\" sc-camel-on-change=\"{{ ctxDecidedCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>"],
      // The tree position, in the pane and in the prompt. The crumb under
      // the title said the same thing in a place that had no room for it.
      ["\n<div style=\"margin-top:15px;display:flex;align-items:baseline;justify-content:space-between;gap:12px\"><span style=\"display:inline-flex;gap:7px;align-items:center\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">CODE CONTEXT</span>",
       "\n<div style=\"margin-top:15px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">WHERE THIS SITS</div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:8px 11px\">\n<sc-for list=\"{{ ctxTrail }}\" as=\"tr\" hint-placeholder-count=\"2\">\n<div style=\"padding:1px 0 1px {{ tr.pad }};font:11.5px/1.6 'Source Code Pro',monospace;color:{{ tr.c }}\">{{ tr.mark }}{{ tr.title }}</div>\n</sc-for>\n</div>\n<div style=\"margin-top:15px;display:flex;align-items:baseline;justify-content:space-between;gap:12px\"><span style=\"display:inline-flex;gap:7px;align-items:center\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">CODE CONTEXT</span>"],
      ["      hasCrumb: !!(trail && trail.length > 1),\n",
       "      hasCrumb: !!(trail && trail.length > 1),\n      ctxTrail: (trail || []).map((n, i) => ({\n        title: n.title || 'Untitled',\n        pad: (i * 14) + 'px',\n        mark: i ? '\\u2514 ' : '',\n        c: n === sel ? 'var(--ink)' : 'var(--fnt)'\n      })),\n"],
      // The recommended prompt follows the same order as the pane it is
      // built from, so what the reader checked is what the agent is told.
      ["      const parts = [];\n      const obj = ctxGet('objective'); if (obj && obj.trim()) parts.push('Objective:\\n' + obj.trim());\n      const dec = ctxGet('decided'); if (dec && dec.trim()) parts.push('Established decisions:\\n' + dec.trim());\n      const blt = ctxGet('built'); if (blt && blt.trim()) parts.push('Already built:\\n' + blt.trim());\n      const blk = ctxGet('hit'); if (blk && blk.trim()) parts.push('Blockers & open questions:\\n' + blk.trim());\n      if (codeList.length) parts.push('Code context:\\n' + codeList.map(c => '- ' + c.label + ' (' + c.type + ')').join('\\n'));\n      if (docList.length) parts.push('Document context:\\n' + docList.map(d => '- ' + d.label).join('\\n'));\n",
       "      const parts = [];\n      const obj = ctxGet('objective'); if (obj && obj.trim()) parts.push('Objective:\\n' + obj.trim());\n      if (trail && trail.length) parts.push('Where this sits:\\n' + trail.map((n, i) => '  '.repeat(i) + (i ? '\\u2514 ' : '') + (n.title || 'Untitled')).join('\\n'));\n      if (codeList.length) parts.push('Code context:\\n' + codeList.map(c => '- ' + c.label + ' (' + c.type + ')').join('\\n'));\n      if (docList.length) parts.push('Document context:\\n' + docList.map(d => '- ' + d.label).join('\\n'));\n      const said = (sel.prompts || []).slice().reverse();\n      if (said.length) parts.push('Related prompts, in my own words:\\n' + said.map(q => '- \"' + String(q.text || '').replace(/\\s+/g, ' ').trim() + '\"').join('\\n'));\n      const blk = ctxGet('hit'); if (blk && blk.trim()) parts.push('Blockers & open questions:\\n' + blk.trim());\n      const blt = ctxGet('built'); if (blt && blt.trim()) parts.push('Already built:\\n' + blt.trim());\n      const dec = ctxGet('decided'); if (dec && dec.trim()) parts.push('Established decisions:\\n' + dec.trim());\n"],
      // A prompt without its conversation is a quote without a source.
      ["        text: p.text,\n",
       "        text: p.text,\n        conv: p.conv ? 'conversation ' + p.conv : '',\n"],
      ["<span style=\"flex:1;min-width:0;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">{{ hr.when }}</span>",
       "<span style=\"flex:1;min-width:0;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis\">{{ hr.when }}<span style=\"color:var(--bd2);padding:0 6px\">\u00b7</span>{{ hr.conv }}</span>"],
      // The goals panel reports any analysis, not only the tree build:
      // switching to this tab while conversations are still being read
      // used to show a banner over an empty tree. It is also read from
      // the server rather than from state held in memory, so a reload
      // cannot make a running analysis invisible.
      ["      anGoals: !!(anx && anx.phase === 'goals'),\n      treeListDisp: (anx && anx.phase === 'goals') ? 'none' : 'block',\n",
       "      anGoals: !!(window.__hcAnalysisNow && window.__hcAnalysisNow().running),\n      anTitle: (window.__hcAnalysisNow && window.__hcAnalysisNow().phase === 'synthesizing')\n        ? 'Building Goals' : 'Reading your conversations',\n      treeListDisp: (window.__hcAnalysisNow && window.__hcAnalysisNow().running) ? 'none' : 'block',\n"],
      ["<sc-if value=\"{{ anGoals }}\" hint-placeholder-val=\"{{ false }}\">\n<div style=\"flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px\">{{ anSpin }}<span style=\"font:600 13.5px 'Source Code Pro',monospace;color:var(--ink)\">Building Goals</span><span style=\"font:10.5px 'Source Code Pro',monospace;color:var(--fnt)\">{{ anCount }}</span></div>\n</sc-if>",
       "<sc-if value=\"{{ anGoals }}\" hint-placeholder-val=\"{{ false }}\">\n<div class=\"hc-anpanel\" style=\"flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px\">{{ anSpin }}<span style=\"font:600 13.5px 'Source Code Pro',monospace;color:var(--ink)\">{{ anTitle }}</span><span style=\"font:10.5px 'Source Code Pro',monospace;color:var(--fnt)\">{{ anCount }}</span></div>\n</sc-if>"],
      // A run has a state before it has an artifact. The feed used to
      // live only inside the artifact card, so pressing run opened a
      // pane that said to press run. The anchors now exist either way,
      // and the invitation only shows when nothing is happening.
      ["<sc-if value=\"{{ artEmpty }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"margin-top:16px;font-size:11.5px;color:var(--fnt)\">No artifact yet \u2014 run the agent from the AGENT tab.</div></sc-if>",
       "<sc-if value=\"{{ artEmpty }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"margin-top:16px\"><div class=\"hc-live\"></div><div class=\"hc-live-rest\"></div></div><sc-if value=\"{{ artIdle }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"margin-top:16px;font-size:11.5px;color:var(--fnt)\">No artifact yet \u2014 run the agent from the AGENT tab.</div></sc-if></sc-if>"],
      ["artEmpty: !art, hasArtifact: !!art,",
       "artEmpty: !art, hasArtifact: !!art,\n      artHasSummary: !!(art && String(art.summary || '').trim()),\n      artDecideC: (art && String(art.summary || '').trim()) ? 'var(--acc)' : 'var(--fnt)',\n      artApproveBg: (art && String(art.summary || '').trim()) ? 'var(--acc)' : 'var(--bd)',\n      artApproveC: (art && String(art.summary || '').trim()) ? 'var(--onacc)' : 'var(--fnt)',\n      artDecideCur: (art && String(art.summary || '').trim()) ? 'pointer' : 'default',\n      artIdle: !art && !(sel && sel.agent && (sel.agent.status === 'running' || sel.agent.status === 'waiting')),"],
      // It is the thing being reviewed, not a section of the pane.
      ["letter-spacing:1px;color:var(--mut)\">ARTIFACT</span>",
       "letter-spacing:1px;color:var(--mut)\">FINAL ARTIFACT</span>"],
      // Nothing to approve and nothing to send back until the run has
      // written something. The buttons stay in place, greyed, rather
      // than appearing when the work lands: a control that moves is
      // harder to find than one that waits.
      ["revOpenFn: () => this.setState({ revOpen: true }),",
       "revOpenFn: () => { if (art && String(art.summary || '').trim()) this.setState({ revOpen: true }); },"],
      ["artApprove: () => { if (sel)",
       "artApprove: () => { if (!(art && String(art.summary || '').trim())) return; if (sel)"],
      // The run's state opens the artifact card it describes.
      ["<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 12px\">\n<div style=\"font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt)\">{{ artSummary }}</div>",
       "<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 12px\">\n<div class=\"hc-live\"></div>\n<sc-if value=\"{{ artHasSummary }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"max-height:230px;overflow-y:auto;border:1px solid var(--acc);border-radius:2px;background:var(--accbg);padding:9px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);white-space:pre-wrap;word-break:break-word\">{{ artSummary }}</div></sc-if>"],
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
    // During onboarding the wizard is the thing to read; a banner behind it
    // would be talking over the questions it is still asking.
    if (!setupState.done) return false;
    var counts = setupState.conversations || {};
    var total = counts.total || 0;
    var analyzed = counts.analyzed || 0;
    // Nothing left to analyze: say nothing, even if a lock is still held.
    // The goal-tree build is the one thing worth reporting past that point.
    if (total && analyzed >= total && setupState.phase !== "synthesizing") {
      return false;
    }
    return !!(setupState.running || (counts.pending || 0) > 0);
  };

  // Full width of the panel it sits above: it shares that panel's container,
  // so 100% of the container is 100% of the panel.
  var BANNER_CSS = [
      ".hc-banner{position:relative;box-sizing:border-box;width:100%;margin:2px 0 0;display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--accbg,#f5e2d9);border:1px solid var(--acc,#a5492a);border-radius:2px;font:11.5px/1.5 'Source Code Pro',ui-monospace,monospace;color:var(--ink,#111)}",
      ".hc-banner-what{flex:none;font-weight:600}",
      ".hc-banner-now{flex:1;min-width:0;color:var(--mut,#575757);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-banner-count{flex:none;color:var(--mut,#575757)}",
      ".hc-banner-bar{position:absolute;left:0;bottom:0;height:2px;background:var(--acc,#a5492a);transition:width .4s ease}"
  ].join("");

  // Styles for parts of the pane that are always on screen. These lived in
  // BANNER_CSS, which is only injected when an analysis is running — so on
  // any settled vault the prompt section rendered as a bare <details>:
  // browser triangle, no divider, no section heading. It was never a
  // styling problem, it was a stylesheet that was never on the page.
  var PANE_CSS = [
      ".hc-prompt-addbtn{flex:none;border:1px solid var(--bd2,#d5d5d5);background:var(--hov,#f4f4f4);color:var(--mut,#575757);border-radius:2px;padding:3px 10px;cursor:pointer;font:600 10px 'Source Code Pro',monospace}",
      ".hc-prompt-addbtn:hover{background:var(--bd,#e6e6e6);color:var(--ink,#111)}",
      ".hc-prompt-addbtn:disabled{opacity:.6;cursor:default}",
      ".hc-promptbox{margin-top:14px;padding-top:14px;border-top:1px solid var(--bd,#e6e6e6)}",
      ".hc-promptsum{cursor:pointer;list-style:none;display:flex;align-items:center;gap:5px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut,#575757)}",
      ".hc-promptsum::-webkit-details-marker{display:none}",
      ".hc-promptsum::before{content:'\\25b8';display:inline-block;font-size:9px;transition:transform .15s ease}",
      ".hc-promptbox[open]>.hc-promptsum::before{transform:rotate(90deg)}",
      ".hc-promptsum:hover{color:var(--acc,#a5492a)}",
      // A conversation takes as long as it takes and reports no progress
      // of its own, so the bar sweeps rather than claiming a percentage.
      // Slow on purpose: a fast one reads as a thing about to finish.
      ".hc-rowbar{display:block;position:relative;width:64px;height:3px;border-radius:2px;background:var(--accbg,#f5e2d9);overflow:hidden}",
      ".hc-rowbar>span{position:absolute;top:0;bottom:0;left:0;width:45%;border-radius:2px;background:var(--acc,#a5492a);animation:hc-sweep 2.8s ease-in-out infinite}",
      "@keyframes hc-sweep{0%{left:-45%}100%{left:100%}}",
      "@media (prefers-reduced-motion: reduce){.hc-rowbar>span{animation:none;left:0;width:100%;opacity:.5}}",
  ].join("");

  function ensurePaneStyles() {
    if (document.getElementById("hc-pane-style")) return;
    var style = document.createElement("style");
    style.id = "hc-pane-style";
    style.textContent = PANE_CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  var LIVE_CSS = [
      ".hc-live{margin-top:0}",
      ".hc-live-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:9px}",
      ".hc-live-head{font:600 12.5px 'Source Code Pro',ui-monospace,monospace;color:var(--ink,#111)}",
      ".hc-live-wait{font:600 11px/1.7 'Source Code Pro',monospace;color:var(--acc,#a5492a);white-space:pre-wrap;word-break:break-word}",
      ".hc-live-ask{margin:0 0 8px;max-height:220px;overflow-y:auto;border:1px solid var(--acc,#a5492a);border-radius:2px;background:var(--accbg,#f5e2d9);padding:8px 11px;font:11px/1.6 'Source Code Pro',monospace;color:var(--dtxt,#333);white-space:pre-wrap}",
      ".hc-live-title{margin-top:14px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut,#575757)}",
      ".hc-live-log{margin-top:6px;max-height:320px;overflow-y:auto;border:1px solid var(--bd,#e6e6e6);border-radius:2px;background:var(--panel2,#fafafa);padding:7px 10px}",
      ".hc-live-idle{font:600 11px/1.7 'Source Code Pro',monospace;color:var(--acc,#a5492a);padding-bottom:3px}",
      ".hc-live-did{font:11px/1.7 'Source Code Pro',monospace;color:var(--dtxt,#333);white-space:pre-wrap;word-break:break-word}",
      ".hc-live-check{margin-top:6px;font:11px/1.6 'Source Code Pro',monospace;color:var(--mut,#575757)}",
      ".hc-live-foot{margin-top:8px;font:11px/1.6 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b)}",
      ".hc-live-open{flex:none;border:1px solid var(--bd2,#d5d5d5);background:var(--hov,#f4f4f4);color:var(--mut,#575757);border-radius:2px;padding:5px 12px;cursor:pointer;font:600 11px 'Source Code Pro',monospace}",
      ".hc-live-open:hover{background:var(--bd,#e6e6e6);color:var(--ink,#111)}",
      ".hc-live-open:disabled{opacity:.6;cursor:default}"
  ].join("");

  function ensureLiveStyles() {
    if (document.getElementById("hc-live-style")) return;
    var style = document.createElement("style");
    style.id = "hc-live-style";
    style.textContent = LIVE_CSS;
    document.head.appendChild(style);
  }

  function ensureBannerStyles() {
    if (document.getElementById("hc-banner-style")) return;
    var style = document.createElement("style");
    style.id = "hc-banner-style";
    style.textContent = BANNER_CSS;
    document.head.appendChild(style);
  }

  function anchorBanner() {
    // It belongs with the text that explains the page. The artifact rebuilds
    // that subtree whenever its state changes, which silently drops anything
    // parented inside it — so re-anchor on mutation, not on a timer.
    if (!banner) return;
    var sub = document.querySelector(".hc-sub");
    // Sit between the page description and the panel below it, as a sibling
    // of that panel rather than a child of the header — same container, so
    // it spans exactly the panel's width instead of the header's.
    var header = sub && sub.parentNode;
    var row = (header && header.parentNode) ? header : sub;
    if (row && row.parentNode) {
      if (row.nextSibling !== banner) {
        row.parentNode.insertBefore(banner, row.nextSibling);
      }
      return;
    }
    // No subtitle on screen (a modal, say): keep it in the document rather
    // than dropping it, so nothing flickers when the page comes back.
    var host = document.body || document.documentElement;
    if (banner.parentNode !== host) host.appendChild(banner);
  }

  function watchBannerAnchor() {
    if (typeof MutationObserver !== "function") return;
    var observer = new MutationObserver(function () {
      if (banner && document.documentElement.contains(banner)) anchorBanner();
      else if (banner) { banner = null; renderBanner(); }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
  }

  function renderBanner() {
    var pending = window.__hcAnalysisPending();
    // The goals panel draws its own spinner while the tree is being built.
    // Two reports of one thing, one above the other, is not twice the
    // information -- but only stand down while that spinner is really there.
    if (pending && treeSpinnerShown()) pending = false;
    if (!pending) {
      if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
      banner = null;
      return;
    }
    ensureBannerStyles();
    if (!banner || !document.documentElement.contains(banner)) {
      banner = document.createElement("div");
      banner.className = "hc-banner";
      banner.setAttribute("role", "status");
      banner.setAttribute("aria-live", "polite");
      banner.style.position = "relative";
      ["hc-banner-what", "hc-banner-now", "hc-banner-count",
       "hc-banner-bar"].forEach(function (cls) {
        var part = document.createElement("div");
        part.className = cls;
        banner.appendChild(part);
      });
    }
    anchorBanner();
    var counts = (setupState && setupState.conversations) || { total: 0, analyzed: 0 };
    var current = setupState && setupState.current;
    var goalPhase = setupState && setupState.phase === "synthesizing";
    var inflight = (setupState && setupState.inflight) || 0;
    // Say what is being done and what it is for. "Analyzing" alone tells the
    // reader that something is happening to their data without saying why.
    banner.querySelector(".hc-banner-what").textContent = goalPhase
      ? "Working out your goals from what it read"
      : "Reading your conversations to work out what you are building";
    banner.querySelector(".hc-banner-now").textContent = goalPhase
      ? "grouping related work into goals and subgoals"
      : (current && current.title ? "reading: " + current.title
         : (current ? "reading: " + String(current.id).slice(0, 8)
                    : "your goals appear here when this finishes"));
    banner.querySelector(".hc-banner-count").textContent = goalPhase
      ? String(counts.total) + " read"
      : (counts.analyzed + " of " + counts.total
         + (inflight > 1 ? "  ·  " + inflight + " at a time" : ""));
    banner.querySelector(".hc-banner-bar").style.width =
      (counts.total ? Math.round(counts.analyzed / counts.total * 100) : 0) + "%";
  }

  function watchAnalysis() {
    // Never decide from state that has not been fetched. Guarding the poll on
    // "is anything running" deadlocked: the answer is false until the first
    // fetch, and the first fetch was what the guard skipped.
    var idle = 0;
    function tick() { return refreshSetup().then(renderBanner); }
    var first = tick();
    watchBannerAnchor();
    setInterval(function () {
      if (window.__hcAnalysisPending()) {
        idle = 0;
        tick();
      } else if (++idle >= 5) {
        // Quiet: keep looking, just less often, so an analysis started from
        // another window or the CLI still shows up here.
        idle = 0;
        tick();
      }
    }, 2000);
    return first;                    // so a caller can await the first paint
  }

  window.__hcAgent = {
    launch: function (goalId) {
      // Pressing run agent is the confirmation. post() resolves the error
      // body rather than rejecting, and an error body is truthy, so the
      // check has to be explicit or a failure reads as a launch.
      return post({ op: "launch_agent_run", goal_id: goalId,
                    confirmed: true }).then(function (result) {
        if (!result || result.ok !== true) {
          throw new Error((result && result.error) || "launch failed");
        }
        return result;
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
    paneShape: paneShape,
    reconcileState: reconcileState,
    clearKeepPane: clearKeepPane,
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
    bannerCss: function () { return BANNER_CSS; },
    paneCss: function () { return PANE_CSS; },
    watchAnalysis: watchAnalysis,
    treeSpinnerShown: function () { return treeSpinnerShown(); },
    analysisNow: function () { return window.__hcAnalysisNow(); },
    loadThread: loadThread,
    loadPlan: loadPlan,
    renderLive: renderLive,
    liveCss: function () { return LIVE_CSS; },
    watchRunFeed: watchRunFeed,
    renderPromptAdd: renderPromptAdd,
    renderChatSurface: renderChatSurface,
    promptAddSlot: promptAddSlot,
    openPromptPicker: openPromptPicker,
    pickPrompt: pickPrompt,
    dialogCss: function () { return DIALOG_CSS; },
    briefingSections: briefingSections,
    analysisPending: function () { return window.__hcAnalysisPending(); },
    setSetupForTest: function (value) { setupState = value; },
    setDetailForTest: function (id, value) { details[id] = value; },
    seedForTest: seed,
    loadDetailForTest: loadDetail
  };

  seed();
  // Published before the template is patched and before the artifact boots,
  // because both read it: the patched source asks it which tabs exist, and
  // the watcher below asks it which controls to take off the page.
  window.__hcScope = serverState.scope;
  // Placed after the template island and before the closing body tag: the
  // artifact's DOMContentLoaded listener is registered but has not unpacked
  // the template yet, so patching here is safe.
  patchBundleTemplate();
  function boot() {
    ensurePaneStyles();
    // Read once. Leaving it set would make every later reload land on
    // whatever pane happened to be open when a run last finished.
    clearKeepPane();
    watchPromptAdd();
    watchChatSurface();
    watchGoals();
    watchAnalysis();
    watchSelection();
    watchPane();
    watchRunFeed();
    setInterval(refreshState, 1500);
    setTimeout(refreshState, 0);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
