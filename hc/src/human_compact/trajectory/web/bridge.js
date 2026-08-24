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
  // The document a goal opens as when nobody has written one yet: nothing.
  // Kept byte-identical to human_compact.trajectory.goals.default_doc() --
  // no spine of empty headings; a heading arrives with the first thing
  // written under it. A test in tests/test_goal_ui_bridge.py greps this line
  // and compares the two.
  var DEFAULT_DOC = "";
  var serverState = { goals: [], prompts: [], runs: {}, claim: null,
                      scope: "global", sessionId: "", buildSession: null,
                      buildCost: null };
  var stateFingerprint = null;
  var lastObservedGoals = null;
  var refreshPending = false;
  var syncBusy = false;
  // Polls in a row that answered nothing, and how many of those mean the
  // server has gone rather than that one request was unlucky.
  var pollsMissed = 0;
  var POLLS_GONE = 3;
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

  function layBuildState(local, remote) {
    // The TODO rows as this page has them -- text, depth, order, what was
    // added or taken away: the edit -- with the build state the server has
    // for each id laid back over them: the run. The list is one field, but
    // it is those two things, and the server's own import splits it the
    // same way. Taken whole, one typed row would carry the page's stale
    // "not sent" over a row the server had just marked queued or asking.
    var held = Object.create(null);
    array(remote).forEach(function (row) {
      if (row && typeof row.id === "string") held[row.id] = row;
    });
    return array(local).map(function (row) {
      var out = clone(row);
      var was = row && held[row.id];
      if (was) { out.status = str(was.status); out.question = str(was.question); }
      return out;
    });
  }

  function mergeTrees(baseRoots, localRoots, remoteRoots, deletedIds) {
    var base = flattenTree(baseRoots), local = flattenTree(localRoots);
    var remote = flattenTree(remoteRoots), selected = Object.create(null);
    var deleted = deletedIds || Object.create(null);
    var order = remote.order.slice();
    local.order.forEach(function (id) {
      if (!remote.map[id]) order.push(id);
    });

    order.forEach(function (id) {
      var b = base.map[id], l = local.map[id], r = remote.map[id];
      if (r && b && !l) return; // an explicit local deletion
      if (r && !l && deleted[id]) return; // deleted here; a stale writer's copy
      if (!r) {
        if (!l) return;
        // Absent from the remote tree with a local copy in hand. The server
        // never erases a deleted goal -- it keeps it, marked abandoned, and
        // the payload carries that marker -- so a goal the payload does not
        // even mention was LOST by a stale writer, not deleted by anyone.
        // Deleted: let it go. Lost (or locally created): keep ours; the
        // next import puts it back.
        if (deleted[id]) return;
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
          value[key] = key === "todo_items"
            ? layBuildState(l.value[key], r.value[key])
            : clone(l.value[key]);
        }
      });
      var parent = r.parent;
      if (!b || l.parent !== b.parent) parent = l.parent;
      else if (!r.parent && l.parent) {
        // The remote lost the parent (a stale writer dropped it and the
        // server re-rooted the child); we never moved it. Keep our link --
        // the parent itself is being kept or re-imported by the same merge.
        parent = l.parent;
      }
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
        // A refusal is an answer, not a broken request: a shared save that
        // would not take some rows says which, and the caller needs that
        // rather than only the fact that something went wrong.
        error.body = body;
        throw error;
      }
      return body;
    });
  }

  // What a shared workspace said about a save: which rows would not take
  // the edit, and why. Shown rather than swallowed -- the reader typed
  // something and the tree is about to snap back to what the server has.
  // Returns the words it said, or "" when there was nothing to say -- so
  // what it decided can be checked without reaching into the page.
  function reportSharedSave(result) {
    if (!result || !sharedWorkspace()) return "";
    var refused = array(result.refused);
    var clashes = array(result.conflicts);
    if (!refused.length && !clashes.length) return "";
    var words;
    if (clashes.length) {
      var one = clashes[0];
      words = (clashes.length === 1
        ? "\u201c" + str(one.title) + "\u201d changed while you were "
          + "editing \u2014 your copy was not saved; this is theirs"
        : clashes.length + " goals changed while you were editing \u2014 "
          + "none of those edits were saved");
    } else {
      var first = refused[0];
      words = (refused.length === 1
        ? (str(first.author) || "someone") + "\u2019s \u201c"
          + str(first.title) + "\u201d \u2014 not yours to change"
        : refused.length + " goals belong to other people and were not "
          + "changed");
    }
    flashNote(words);
    // The server sent what it now holds; show that rather than the edit
    // that did not land.
    if (result.state && result.state.goals) {
      try { acceptState(result.state); } catch (e) {}
    }
    return words;
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

  function installGoals(goals, revision) {
    // The tree the sync settled on, into the store and into the artifact's
    // own state -- and the page stays where the reader is, caret and all.
    // The artifact publishes a setter from its constructor for exactly
    // this; only a page whose artifact has none (booted before the setter
    // existed) still reloads to learn the tree. A build marking rows queued
    // and its goal in progress is the common case: that used to reload the
    // page on every Build.
    var setter = (typeof window !== "undefined") ? window.__hcSetGoals : null;
    if (typeof setter !== "function") {
      installGoalsAndReload(goals, revision);
      return false;
    }
    var saved;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); }
    catch (e) { saved = {}; }
    var ids = flattenTree(goals).map;
    var selId = (typeof saved.selId === "string" && ids[saved.selId])
      ? saved.selId : (goals.length ? goals[0].id : null);
    saved.goals = goals;
    saved.selId = selId;
    saved.updatedAt = Date.now();
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
    writeSync(revision, goals);
    lastObservedGoals = JSON.stringify(goals);
    try {
      setter(clone(goals), selId);
    } catch (e) {
      installGoalsAndReload(goals, revision);
      return false;
    }
    return true;
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

  // What the reader deleted, remembered on this side. The server's
  // tombstones cover most of it, but a stale writer can flip one back to
  // active -- and to the merge that then looks exactly like a goal somebody
  // else just made. Only this memory can tell those apart.
  var TOMB_KEY = "hc-deleted-goals-v1";
  var TOMB_MAX = 200;

  function readTombs() {
    try {
      var value = JSON.parse(localStorage.getItem(TOMB_KEY) || "{}");
      return (value && typeof value === "object") ? value : {};
    } catch (e) { return {}; }
  }

  function noteTombs(ids) {
    if (!ids.length) return;
    var tombs = readTombs();
    ids.forEach(function (id) { tombs[id] = Date.now(); });
    var keys = Object.keys(tombs);
    if (keys.length > TOMB_MAX) {
      keys.sort(function (a, b) { return tombs[a] - tombs[b]; });
      keys.slice(0, keys.length - TOMB_MAX).forEach(function (id) {
        delete tombs[id];
      });
    }
    try { localStorage.setItem(TOMB_KEY, JSON.stringify(tombs)); } catch (e) {}
  }

  function deletedIdsOf(st) {
    // The tombstones: goals the server remembers as deleted. These are the
    // only absences mergeTrees may honour.
    var gone = Object.create(null);
    array(st && st.goals).forEach(function (goal) {
      if (goal && goal.status === "abandoned" && typeof goal.id === "string") {
        gone[goal.id] = true;
      }
    });
    var tombs = readTombs();
    Object.keys(tombs).forEach(function (id) { gone[id] = true; });
    return gone;
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
        installGoals(mergeTrees(synced.goals, stale, remote, deletedIdsOf(st)),
                     st.revision);
      }
      return;
    }
    var local = readLocalGoals();
    // Ids in the synced base that the local tree no longer holds are the
    // reader's deletions; remember them before they leave the base too.
    var localFlat = flattenTree(local);
    noteTombs(flattenTree(synced.goals).order.filter(function (id) {
      return !localFlat.map[id];
    }));
    var merged = mergeTrees(synced.goals, local, remote, deletedIdsOf(st));
    if (same(merged, remote)) {
      writeSync(st.revision, remote);
      if (!same(local, remote)) installGoals(remote, st.revision);
      return;
    }
    syncBusy = true;
    postImport(merged, st.revision).then(function (result) {
      syncBusy = false;
      installGoals(merged, result.revision);
    }).catch(function (error) {
      syncBusy = false;
      lastObservedGoals = null;
      // A shared save that refused rows is reported; anything else is a
      // failure and the tree simply reloads.
      if (reportSharedSave(error && error.body)) return;
      setTimeout(refreshState, 50);
    });
  }

  function refreshState() {
    // An import we started is a change this page already shows. Reconciling
    // against a half-applied revision is what turned a delete into a reload.
    // Answers with the fetch when one starts, so a caller that has just
    // changed something can wait for the state that follows it -- and with
    // nothing when it does not.
    if (syncBusy) return null;
    if (refreshPending) return null;
    refreshPending = true;
    // Whether anything answered at all, which is a different question from
    // whether the answer was any good: a 500 is a server that is still there.
    var answered = false;
    return fetch("/api/state", { cache: "no-store" })
      .then(function (r) {
        answered = true;
        if (!r.ok) throw new Error("state request failed (" + r.status + ")");
        return r.json();
      })
      .then(function (st) {
        acceptState(st);
        injectionState = (st && st.injection && typeof st.injection === "object")
          ? st.injection : null;
        showNotices(st && st.notices);
        reconcileState(st);
      })
      .catch(function () {})
      .then(function () {
        refreshPending = false;
        if (answered) {
          if (pollsMissed) { pollsMissed = 0; clearServerGone(); }
          return;
        }
        // Several misses in a row is not a slow disk: the server this page
        // came from has stopped -- restarted from the chat, most often, which
        // leaves this window on screen and answering nothing. Silence reads
        // as a page that broke rather than a process that ended, so it is
        // said, and taken back the moment a poll lands again.
        pollsMissed += 1;
        if (pollsMissed >= POLLS_GONE) noteServerGone();
      });
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
    // Which links inference made rather than the reader. The server clears
    // this id the moment they attach or detach it by hand ("an auto link the
    // user keeps becomes theirs"), so it reports who is standing behind the
    // link, not who first proposed it.
    var automatic = Object.create(null);
    array(goal && goal.auto_prompt_ids).forEach(function (id) {
      automatic[id] = true;
    });
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
               auto: !!automatic[id],
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

  // Which branches the reader has folded. A fold is a view preference the
  // server never hears of, so a tree rebuilt from the payload -- at boot,
  // and again after any server-side change -- opened every branch. The
  // store this page keeps is the only record of it; a rebuilt tree reads
  // it back, and so the base and the remote agree with the local copy on
  // a key the merge would otherwise take for an edit.
  function foldedIds() {
    var folded = Object.create(null);
    (function walk(list) {
      array(list).forEach(function (node) {
        if (!node || typeof node.id !== "string") return;
        if (node.open === false) folded[node.id] = true;
        walk(node.children);
      });
    })(readLocalGoals());
    return folded;
  }

  // Left and right on the keyboard move across the hierarchy, where up and
  // down walk the drawn rows. Right opens a folded branch, or steps into
  // its first drawn child; left folds an open branch with something drawn
  // under it, or steps out to the parent. A pure function of the tree, the
  // rows on screen and the selection: it returns what to do, or null when
  // the key means nothing here.
  function treeStep(goals, rowIds, selId, back) {
    if (typeof selId !== "string") return null;
    var trail = null;
    (function find(list, above) {
      array(list).some(function (node) {
        if (!node || trail) return !!trail;
        var here = above.concat([node]);
        if (node.id === selId) { trail = here; return true; }
        find(node.children, here);
        return !!trail;
      });
    })(goals, []);
    if (!trail) return null;
    var node = trail[trail.length - 1];
    var kids = array(node.children);
    var ids = array(rowIds);
    var next = ids[ids.indexOf(selId) + 1];
    var childDrawn = !!next && kids.some(function (k) {
      return k && k.id === next;
    });
    if (!back) {
      if (kids.length && node.open === false) {
        return { fold: { id: node.id, open: true } };
      }
      return childDrawn ? { selId: next } : null;
    }
    if (childDrawn && node.open !== false) {
      return { fold: { id: node.id, open: false } };
    }
    return trail.length > 1 ? { selId: trail[trail.length - 2].id } : null;
  }

  function toNode(goal, byParent, byId, runs, claim, folded) {
    return {
      id: goal.id,
      title: str(goal.title),
      prio: goal.priority || "normal",
      done: goal.status === "completed" || goal.status === "abandoned",
      open: !(folded && folded[goal.id]),
      status: goal.status === "in_progress" ? "inprog" : "todo",
      notes: str(goal.notes),
      todos_md: str(goal.todos_md),
      todo_items: array(goal.todo_items),
      prompt_md: str(goal.prompt_md),
      desc: str(goal.description),
      // Whose goal this is, in a shared workspace. Empty everywhere else,
      // and the chip does not draw when it is empty: a personal tree has
      // one author, and saying so on every row would be noise.
      who: str(goal.shared_author || (goal.shared_mine ? "you" : "")),
      labels: [],
      prompts: promptRows(goal, byId),
      ctx: contextOf(goal, details[goal.id]),
      agent: agentOf(goal, runs, claim),
      artifact: artifactOf(goal, runs, details[goal.id]),
      children: array(byParent[goal.id]).map(function (child) {
        return toNode(child, byParent, byId, runs, claim, folded);
      })
    };
  }

  function rootsFromState(st) {
    var byParent = {}, byId = {};
    array(st && st.prompts).forEach(function (p) {
      if (p && typeof p.id === "string") byId[p.id] = p;
    });
    // A goal the reader deleted is kept on disk as "abandoned" rather than
    // erased, so nothing they wrote is lost -- but it is deleted to them, and
    // drawing it struck through under every filter makes the delete look like
    // it failed. Leave it out of the tree; the record stays in goals.json.
    array(st && st.goals).forEach(function (goal) {
      if (goal && goal.status === "abandoned") return;
      var parent = goal.parent_goal_id || null;
      (byParent[parent] = byParent[parent] || []).push(goal);
    });
    // Read the claim from the state being rendered, not from module state:
    // the mapping is a pure function of one payload.
    var claim = (st && st.agent_claim && typeof st.agent_claim === "object")
      ? st.agent_claim : null;
    var folded = foldedIds();
    return array(byParent[null]).map(function (goal) {
      return toNode(goal, byParent, byId, (st && st.agent_runs) || {}, claim,
                    folded);
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
    if (chat && paneTab !== "context") {
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
      // A chat workspace opens dark; the toggle in its header still
      // decides, and what it decides is what comes back on the next load.
      themeMode: saved.themeMode === "light" || saved.themeMode === "dark" ?
        saved.themeMode : (chat ? "dark" : null),
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
    try { markReadonly(st); noteRowRights(st); } catch (e) {}
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
      scope: st.scope === "chat" ? "chat" : "global",
      // Which Claude conversation this window is a second view of. Only the
      // server knows; it is what names the tab.
      sessionId: str(st.session_id),
      // Whether that conversation is still there to build in, and how many
      // builds wait for its next turn.
      buildSession: (st.build_session && typeof st.build_session === "object")
        ? st.build_session : null,
      // What a build costs here: the context every one of them opens on, and
      // what a row of work has spent in this chat so far. The TODO rail
      // prices its rows from it.
      buildCost: (st.build_cost && typeof st.build_cost === "object")
        ? st.build_cost : null,
      // What each goal's build is doing right now, by goal id: how far in it
      // is, roughly how much longer it has, and the last thing it did. The
      // rail draws it under the rows, so "building" is not all the reader
      // gets to know.
      buildRuns: (st.build_runs && typeof st.build_runs === "object")
        ? st.build_runs : {},
      // The directory this chat works in -- the Claude Code project -- as
      // the manifest recorded it: name, branch, origin, and the objective
      // written for it. Empty when the manifest never said.
      project: (st.project && typeof st.project === "object") ? st.project : null,
      // Present only in a shared workspace: whose goals these are, what
      // this reader may change, and who else is here. Absent everywhere
      // else, which is how the page tells the two apart.
      shared: (st.shared && typeof st.shared === "object") ? st.shared : null,
      // How far the chat has been read. Reported all along and drawn by
      // nobody, which is what made a slow catch-up indistinguishable from a
      // broken one.
      analyzer: (st.analyzer && typeof st.analyzer === "object")
        ? st.analyzer : null
    };
    var fingerprint = JSON.stringify([
      serverState.goals.map(function (g) {
        return [g.id, g.prompt_ids, g.sources, g.status, g.title];
      }),
      serverState.runs
    ]);
    // Row status is not in the fingerprint -- the tree does not redraw for
    // it -- so the builder's transitions are read before the early return.
    trackTodoAlerts(st);
    if (fingerprint === stateFingerprint) return true;
    stateFingerprint = fingerprint;
    return true;
  }

  function askHealthForScope() {
    try {
      var health = new XMLHttpRequest();
      health.open("GET", "/api/health", false);
      health.send();
      var answered = JSON.parse(health.responseText);
      if (answered.scope === "chat") {
        serverState.scope = "chat";
        // This is the path where /api/state did not answer, so the poll has
        // not named the tab yet and this is the only place that can.
        serverState.sessionId = str(answered.session_id);
      }
    } catch (e) { /* nothing left to ask */ }
  }

  function seed() {
    // Which scope this is decides most of what follows, and only one route
    // is never gated. Asking it first costs one cheap call and saves two
    // blocking ones: /api/setup and /api/briefings speak for the global
    // vault, so in a chat they are two synchronous round trips on the path
    // to first paint whose only possible answer is "not here".
    askHealthForScope();
    if (serverState.scope !== "chat") {
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
        // artifact's saved state at boot, and anything fetched afterwards
        // has nowhere to land until the page reloads.
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
    }
    try {
      var request = new XMLHttpRequest();
      request.open("GET", "/api/state", false);   // sync: must beat app boot
      request.send();
      var st = JSON.parse(request.responseText);
      // Not a return: a reply that is not a state payload is a failed fetch,
      // and the fallback below has to see it as one.
      if (!acceptState(st)) throw new Error("not a state payload");
      var roots = rootsFromState(st);
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
      localStorage.setItem(KEY, JSON.stringify(seedPayload(st, roots, saved)));
      lastObservedGoals = JSON.stringify(roots);
      if (typeof st.revision === "string") writeSync(st.revision, roots);
    } catch (e) {
      // Server unreachable, or answering something that is not state: let
      // the artifact boot on whatever it already has. Which scope this is
      // was settled before the fetch, on the one route that is never gated
      // -- the controls a chat must not offer do not depend on the tree
      // loading, and nothing here has to ask a second time.
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
    // A read-only workspace refuses at the server too; stopping here means
    // no optimistic redraw runs first and then has to be taken back.
    if (document.documentElement
        && document.documentElement.getAttribute("data-hc-readonly") !== null
        && !(body && READ_OPS[body.op])) {
      return Promise.resolve({ ok: false, readonly: true,
                               error: "this is a shared workspace: it is read-only" });
    }
    return fetch("/api/op", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); }).catch(function () { return null; });
  }

  // The few operations that only read. Everything else is a write and is
  // stopped while a workspace is read-only.
  var READ_OPS = { list_shares: true, supabase_logout: true,
                   prompt_preview: true };

  // Notes and the document pane are real editors; a caret that types and
  // saves nothing is worse than no caret. Turned off while read-only, and
  // turned back on if the same page ever stops being so.
  function sealEditors() {
    var on = document.documentElement
      && document.documentElement.getAttribute("data-hc-readonly") !== null;
    var fields = document.querySelectorAll(
      "textarea,input[type=text],[contenteditable=true],[contenteditable='']");
    for (var i = 0; i < fields.length; i += 1) {
      var node = fields[i];
      if (closestByClass(node, "hc-settings-panel")) continue;
      if (node.getAttribute("contenteditable") !== null) {
        node.setAttribute("contenteditable", on ? "false" : "true");
      } else if (on) {
        node.setAttribute("readonly", "readonly");
      } else {
        node.removeAttribute("readonly");
      }
    }
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
    // the sources they just replaced. Answers when the tree has reached the
    // server: a caller about to send work ABOUT these goals -- a build -- has
    // to know the server is holding what it is about to be asked to build.
    return syncNodeFields(goals).then(function () { return importTree(goals); });
  }

  function importTree(goals) {
    var synced = readSync();
    if (!synced) {
      lastObservedGoals = null;
      return Promise.resolve(refreshState());
    }
    // Ids the last synced base holds that this import no longer does are
    // the reader's deletions, recorded HERE -- the import is about to
    // rewrite that base, after which nothing else can tell a deletion from
    // a goal that never existed. reconcileState's own recording only sees
    // deletions made while a refresh was already in flight.
    var posted = flattenTree(goals);
    noteTombs(flattenTree(synced.goals).order.filter(function (id) {
      return !posted.map[id];
    }));
    syncBusy = true;
    return postImport(goals, synced.revision).then(function (result) {
      syncBusy = false;
      writeSync(result.revision, goals);
      refreshState();
    }).catch(function (error) {
      syncBusy = false;
      lastObservedGoals = null;
      reportSharedSave(error && error.body);
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

  // The goal and every goal above it, as a set of ids. A chat linked on a
  // goal is offered to that goal and the goals under it: a prompt tagged
  // with chat_goals belongs in the picker for goal S when one of those
  // goals is S or an ancestor of S -- never when it is a descendant.
  function goalLine(goalId) {
    var byId = {};
    array(serverState.goals).forEach(function (g) {
      if (g && typeof g.id === "string") byId[g.id] = g;
    });
    var line = {};
    var at = goalId, hops = 0;
    while (typeof at === "string" && at && !line[at] && hops++ < 64) {
      line[at] = true;
      at = byId[at] ? byId[at].parent_goal_id : null;
    }
    return line;
  }

  function promptOfferedTo(prompt, line) {
    var scope = prompt && prompt.chat_goals;
    if (!Array.isArray(scope)) return true;
    for (var i = 0; i < scope.length; i++) {
      if (line[scope[i]]) return true;
    }
    return false;
  }

  function pickPrompt(goalId, trigger) {
    var goal = array(serverState.goals).filter(function (g) {
      return g && g.id === goalId;
    })[0];
    var linked = {};
    array(goal && goal.prompt_ids).forEach(function (id) { linked[id] = true; });
    var line = goalLine(goalId);
    var pool = array(serverState.prompts).filter(function (prompt) {
      return !linked[prompt.id] && promptOfferedTo(prompt, line);
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
      // A way out in the corner the eye goes to first. Cancel is at the
      // bottom of a box that can be 84vh tall, which is a long way from
      // where a reader who opened this by mistake is looking.
      var shut = document.createElement("button");
      shut.className = "hc-pick-close";
      shut.type = "button";
      shut.setAttribute("aria-label", "Close");
      shut.textContent = "×";

      function onKey(event) {
        // Bound on the document, not on the filter input: Escape has to work
        // wherever focus has landed inside the box -- a picked row, the close
        // button, the scrolled list.
        if (!overlay.parentNode) return;
        if (!event || event.key !== "Escape") return;
        if (event.preventDefault) event.preventDefault();
        close(null);
      }

      function close(value) {
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
        if (document.removeEventListener) {
          document.removeEventListener("keydown", onKey, true);
        }
        // Back where they were. Closing a modal onto <body> loses a keyboard
        // reader's place on the page entirely.
        if (trigger && trigger.focus) trigger.focus();
        resolve(value || null);
      }

      shut.onclick = function () { close(null); };

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
          when.textContent = promptWhen(prompt)
            + (prompt.chat ? "  \u00b7  " + str(prompt.chat) : "");
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
      box.appendChild(shut);
      box.appendChild(title);
      box.appendChild(filter);
      box.appendChild(count);
      box.appendChild(list);
      box.appendChild(row);
      overlay.appendChild(box);
      // Inside the workspace's root, not on <body>: the theme's variables
      // (--panel, --ink, --bd …) are declared on `.hc`, so a picker mounted
      // outside it fell back to the light defaults on a dark page.
      var root = document.querySelector(".hc");
      if (root && document.documentElement
          && document.documentElement.contains
          && !document.documentElement.contains(root)) root = null;
      (root || document.body || document.documentElement).appendChild(overlay);
      if (document.addEventListener) {
        document.addEventListener("keydown", onKey, true);
      }
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
    pickPrompt(goalId, button).catch(function (error) {
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
        if (name.indexOf("hc-chat-addbtn") >= 0
            || name.indexOf("hc-chat-linkbtn") >= 0) {
          if (event.preventDefault) event.preventDefault();
          if (event.stopPropagation) event.stopPropagation();
          // The header's button links for the whole workspace; the one in
          // a goal's pane links for that goal and the goals under it.
          openChatPicker(node, name.indexOf("hc-chat-linkbtn") >= 0
                                 ? null : selectedGoalId());
          return;
        }
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

  // How one linked chat stands in relation to a picker opened for `goalId`
  // (null: the header, i.e. the whole workspace). `on` is the link this
  // picker can undo; `via` names a link made elsewhere that already covers
  // this scope, which the picker reports and leaves alone.
  function chatStanding(entries, goalId, line, titles) {
    var on = false, via = "";
    entries.forEach(function (entry) {
      var scope = entry.goal_id || null;
      if (scope === goalId) on = true;
      else if (goalId && !scope) via = via || "linked for every goal";
      else if (goalId && line[scope]) {
        via = via || ("linked on " + (titles[scope] || scope));
      } else if (!goalId && scope) {
        via = via || "linked on " + (titles[scope] || scope);
      }
    });
    return { on: on, via: via };
  }

  function openChatPicker(button, goalId) {
    if (document.querySelector(".hc-ask")) return;
    goalId = typeof goalId === "string" && goalId ? goalId : null;
    var goal = goalId ? array(serverState.goals).filter(function (g) {
      return g && g.id === goalId;
    })[0] : null;
    if (goalId && !goal) {
      button.textContent = "select a goal first";
      return;
    }
    fetch("/api/chats").then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.ok !== true) {
          button.textContent = (data && data.error) || "could not list chats";
          return;
        }
        ensureDialogStyles();
        var titles = {};
        array(serverState.goals).forEach(function (g) {
          if (g && typeof g.id === "string") titles[g.id] = str(g.title);
        });
        var line = goalId ? goalLine(goalId) : {};
        var overlay = document.createElement("div");
        overlay.className = "hc-ask";
        var box = document.createElement("div");
        box.className = "hc-ask-box hc-pick-box";
        var title = document.createElement("div");
        title.className = "hc-ask-title";
        title.textContent = goalId
          ? "Chats this goal draws prompts from"
          : "Chats this workspace draws prompts from";
        var note = document.createElement("div");
        note.className = "hc-pick-count";
        note.textContent = (goalId
          ? "Linked here, a chat offers its prompts to \u201c"
            + (str(goal.title) || "Untitled") + "\u201d and the goals "
            + "under it, not to the goals above. "
          : "Linked here, a chat offers its prompts to every goal. ")
          + "Its goals are never read; it stays in sync.";
        var list = document.createElement("div");
        list.className = "hc-pick-list";
        function close() {
          if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
          document.removeEventListener("keydown", onKey, true);
          if (button && button.focus) button.focus();
        }
        function onKey(event) {
          if (event && event.key === "Escape") { event.preventDefault(); close(); }
        }
        function draw() {
          while (list.firstChild) list.removeChild(list.firstChild);
          var shown = {}, order = [];
          array(data.linked).forEach(function (chat) {
            if (!shown[chat.session_id]) {
              shown[chat.session_id] = { id: chat.session_id, label: chat.label,
                                         project: "", entries: [] };
              order.push(chat.session_id);
            }
            shown[chat.session_id].entries.push(chat);
          });
          array(data.available).forEach(function (chat) {
            if (shown[chat.session_id]) {
              shown[chat.session_id].project = str(chat.project);
              return;
            }
            shown[chat.session_id] = { id: chat.session_id,
                                       label: str(chat.project) || chat.session_id.slice(0, 8),
                                       project: str(chat.project), entries: [] };
            order.push(chat.session_id);
          });
          var rows = order.map(function (id) { return shown[id]; });
          if (!rows.length) {
            var none = document.createElement("div");
            none.className = "hc-pick-none";
            none.textContent = "No other chats with transcripts were found.";
            list.appendChild(none);
            return;
          }
          rows.forEach(function (chat) {
            var standing = chatStanding(chat.entries, goalId, line, titles);
            var row = document.createElement("button");
            row.className = "hc-pick-row";
            var when = document.createElement("span");
            when.className = "hc-pick-when";
            when.textContent = (standing.on ? "LINKED \u00b7 " : "")
              + chat.id.slice(0, 8) + (chat.project ? " \u00b7 " + chat.project : "")
              + (standing.via ? " \u00b7 " + standing.via : "");
            var text = document.createElement("span");
            text.className = "hc-pick-text";
            text.textContent = (chat.label || chat.id)
              + (standing.on ? " \u2014 click to unlink"
                 : (standing.via && !goalId
                    ? " \u2014 click to link for every goal"
                    : " \u2014 click to link"));
            row.appendChild(when);
            row.appendChild(text);
            row.onclick = function () {
              row.disabled = true;
              var body = { op: standing.on ? "unlink_chat" : "link_chat",
                           session_id: chat.id, label: chat.label };
              if (goalId) body.goal_id = goalId;
              post(body).then(function (result) {
                if (result && result.ok === true) {
                  if (standing.on) {
                    data.linked = data.linked.filter(function (c) {
                      return !(c.session_id === chat.id
                               && (c.goal_id || null) === goalId);
                    });
                  } else {
                    var entry = { session_id: chat.id, label: chat.label };
                    if (goalId) entry.goal_id = goalId;
                    data.linked.push(entry);
                  }
                  refreshState();
                }
                row.disabled = false;
                draw();
              });
            };
            list.appendChild(row);
          });
        }
        var rowBar = document.createElement("div");
        rowBar.className = "hc-ask-row";
        var done = document.createElement("button");
        done.className = "hc-ask-btn";
        done.textContent = "Done";
        done.onclick = close;
        rowBar.appendChild(done);
        overlay.onclick = function (e) { if (e.target === overlay) close(); };
        box.appendChild(title);
        box.appendChild(note);
        box.appendChild(list);
        box.appendChild(rowBar);
        overlay.appendChild(box);
        var root = document.querySelector(".hc");
        if (root && document.documentElement
            && document.documentElement.contains
            && !document.documentElement.contains(root)) root = null;
        (root || document.body || document.documentElement).appendChild(overlay);
        document.addEventListener("keydown", onKey, true);
        draw();
      });
  }

  function renderPromptAdd() {
    // Chat scope used to be turned away here. Both ops behind this button --
    // attach_prompt and detach_prompt -- answer in this scope, and the
    // prompts it offers are the ones this chat recorded, so a chat that
    // could not correct a wrong inference was the only thing missing.
    bindPromptAdd();
    renderChatLink();
    var slot = promptAddSlot();
    if (!slot) return false;
    if (slot.querySelector && slot.querySelector(".hc-prompt-addbtn")) {
      return true;
    }
    ensurePaneStyles();
    var button = document.createElement("button");
    button.className = "hc-prompt-addbtn";
    button.type = "button";
    button.textContent = "+ add a prompt";
    // This goal's own link: the chat is offered here and below, not above.
    var chats = document.createElement("button");
    chats.className = "hc-chat-addbtn";
    chats.type = "button";
    chats.title = "Link a chat to this goal and the goals under it";
    chats.textContent = "+ add a chat";
    slot.appendChild(chats);
    slot.appendChild(button);
    return true;
  }

  // The workspace-wide link lives in the header, beside the session chip:
  // a chat linked there is offered to every goal. The slot is left by the
  // header patch; only chat scope has one, which is the scope the op
  // answers in. Attributes survive the artifact's re-render, listeners do
  // not -- the click is delegated in bindPromptAdd.
  function renderChatLink() {
    var slot = document.querySelector(".hc-chats");
    if (!slot) return false;
    if (slot.querySelector && slot.querySelector(".hc-chat-linkbtn")) {
      return true;
    }
    // "+ chats" is gone from the header. Linking a chat is still reachable
    // per goal, where the question "whose prompts should this draw from?"
    // is actually being asked.
    return true;
  }

  function watchPromptAdd() {
    renderPromptAdd();
    setInterval(renderPromptAdd, 700);
  }

  // --- controls a chat workspace has no backend for ------------------------
  // The artifact was drawn for the global vault. Three of its controls lead
  // somewhere this scope cannot go, and a fourth -- the PROMPT tab -- leads
  // somewhere this workspace now keeps permanently on screen: the assembled
  // prompt has its own rail, so a tab that swaps the document out for it is
  // a second route to something already visible. The Conversations page lists a vault's
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

  function rowHolding(name, companions) {
    // A row is identified by two of its labels, not one. The artifact
    // re-renders these rows from its own state, so text it has to draw is
    // the only handle that cannot be re-rendered away -- but a goal the
    // reader titled "CONTEXT" or "Goals" draws that same text, and hiding
    // something inside their tree row would erase their own goal. Requiring
    // a sibling that only the real row has is what tells them apart.
    var found = leafSpansNamed(name);
    for (var i = 0; i < found.length; i++) {
      var row = found[i].parentNode;
      var kids = (row && row.children) || [];
      for (var k = 0; k < kids.length; k++) {
        if (kids[k] === found[i]) continue;
        if (companions.indexOf(str(kids[k].textContent).trim()) >= 0) return row;
      }
    }
    return null;
  }

  function paneTabBar() {
    return rowHolding("CONTEXT", ["PROMPT", "AGENT", "REVIEW"]);
  }

  function headerNav() {
    return rowHolding("Goals", ["Conversations"]);
  }

  function hideNode(node) {
    if (!node || !node.style || node.style.display === "none") return false;
    node.style.display = "none";
    return true;
  }

  function hideLabelsIn(row, labels) {
    var kids = (row && row.children) || [], hidden = 0;
    for (var i = 0; i < kids.length; i++) {
      if (labels.indexOf(str(kids[i].textContent).trim()) >= 0
          && hideNode(kids[i])) hidden += 1;
    }
    return hidden;
  }

  function renderChatSurface() {
    if (serverState.scope !== "chat") return false;
    // REVIEW arrives late -- it is behind an sc-if that only turns on once
    // a run exists -- so this is a standing sweep, not a one-shot.
    //
    // The tab's name rides along here for the same reason the sweep exists
    // at all: the artifact unpacks its template by replacing the whole
    // documentElement, which takes the document's <title> with it. Anything
    // written before that is gone, so the name has to be re-asserted by
    // something that keeps running. applyPageTitle is idempotent and lives
    // with the banner that prefixes it.
    return (hideLabelsIn(headerNav(), ["Conversations"])
            + hideLabelsIn(paneTabBar(), ["AGENT", "REVIEW", "PROMPT"])
            + (renderTodoRail(false) ? 1 : 0)
            + (applyPageTitle() ? 1 : 0)) > 0;
  }

  function watchChatSurface() {
    renderChatSurface();
    setInterval(renderChatSurface, 700);
  }

  // --- what the session behind this workspace just did ---------------------
  // This page is a second window on a conversation happening in a terminal,
  // usually on another screen. The one thing it can say that the terminal
  // cannot is that the terminal is finished. Hooks record a notice when the
  // session stops, when a subagent returns and when the session ends.
  //
  // What arrives here is drawn where a finished TODO is drawn: a card in the
  // top-right corner, an entry behind the bell, one unread count. A synced
  // chat answering is news of the same kind as a build coming back, and a
  // reader watching one corner should not have to watch two -- so this
  // module keeps only the part the alert stack cannot know: which rows are
  // new, and which of them this page has already had its chance to show.
  // Chat scope only: a global vault stands behind no one session, so it has
  // nothing to report.

  // Three is what fits above the fold without covering the page it reports on.
  var NOTICE_MAX = 3;
  var NOTICE_MARK = "● ";
  // Exactly what one hook payload proves, and no further. A Stop means the
  // turn ended -- not that goals moved, that tasks closed, or that anything
  // succeeded. A map with no prototype so a kind named "constructor" reads
  // as unknown rather than as a function. The sentence each one says lives
  // in ALERT_SAYS with the builder's, because one node draws them both.
  var NOTICE_KINDS = Object.create(null);
  NOTICE_KINDS.session_stopped = true;
  NOTICE_KINDS.subagent_returned = true;
  NOTICE_KINDS.session_ended = true;
  // And the two things this page can learn about its own server: that it
  // is running older code than the plugin on disk, and that it is gone.
  NOTICE_KINDS.server_stale = true;
  NOTICE_KINDS.server_gone = true;

  // Everything this page has already had its chance to show. The store keeps
  // its last twenty rows and state is polled every 1.5s, so without this the
  // same notice arrives again on every poll for the rest of the session.
  var noticeSeen = Object.create(null);
  // Anything older than this window belongs to the part of the conversation
  // it was not open for. Replaying that would report old news as new.
  var noticeSince = Date.now();

  function pageTitle() {
    // Named after the session rather than the goal tree: a day with several
    // of these open needs the tab strip to tell them apart, and the tree is
    // what they all have in common. The same 8-character prefix the prompt
    // rows already use for a conversation, so the two line up.
    var sid = str(serverState.sessionId).slice(0, 8);
    return sid ? "Engelbart \u00b7 " + sid : "Engelbart";
  }

  function applyPageTitle() {
    // Chat scope only: a global vault's tab is the artifact's own business,
    // and there is no one session it could be named after.
    if (serverState.scope !== "chat" || typeof document.title !== "string") {
      return false;
    }
    // Derived, never remembered. Restoring a title by putting back the
    // string that was there when the mark appeared restores whatever the
    // artifact had most recently wiped it to; recomputing cannot. The mark
    // follows the unread count rather than a card standing on screen: a
    // reader who was looking elsewhere for the six seconds a card is up is
    // exactly the reader the tab strip is for.
    var want = (alertUnread() ? NOTICE_MARK : "") + pageTitle();
    if (document.title === want) return false;
    document.title = want;
    return true;
  }

  function noticesToShow(rows, since, seen) {
    var fresh = [];
    array(rows).forEach(function (row) {
      if (!row || typeof row !== "object") return;
      var id = str(row.id);
      var kind = str(row.kind);
      var at = Date.parse(str(row.at));
      if (!id || !NOTICE_KINDS[kind] || seen[id]) return;
      if (!isFinite(at) || at <= since) return;
      // Marked here rather than at draw time: one this page decided not to
      // draw is not one it should draw 1.5s later, when it is older still.
      seen[id] = true;
      fresh.push({ id: id, kind: kind, detail: str(row.detail) });
    });
    return fresh.slice(-NOTICE_MAX);
  }

  // Into the same log the builder writes to, and out through the same card.
  // No goal and no row: a turn ending is about the conversation, not about
  // one line of work, so clicking it has nowhere to go and says so by
  // moving nothing.
  function showNotices(rows) {
    if (serverState.scope !== "chat") return 0;
    var fresh = noticesToShow(rows, noticeSince, noticeSeen);
    if (!fresh.length) return 0;
    var log = loadAlertLog();
    var made = fresh.map(function (row) {
      alertSeq += 1;
      var entry = { id: "a" + Date.now().toString(36) + "-" + alertSeq,
                    kind: row.kind, goalId: "", goalTitle: "", rowId: "",
                    text: row.detail, question: "",
                    at: Date.now(), read: false };
      log.unshift(entry);
      return entry;
    });
    saveAlertLog();
    if (alertSettings().banners) {
      made.forEach(function (entry) { showAlertBanner(entry); });
    }
    renderBell();
    renderAlertCenter();
    // The bell's badge and the tab's mark are the same fact seen twice.
    applyPageTitle();
    return fresh.length;
  }

  // The server stamps this page with whether its own code has been edited
  // since it started. If it has, every control added by that edit is about
  // to fail against it, so the page says so once, on the way in, rather than
  // leaving it to be discovered one broken button at a time. No timer: this
  // is not news that stops being true after eight seconds.
  function showStaleServer() {
    if (typeof window === "undefined" || window.__hcServerStale !== true) {
      return false;
    }
    return noteStaleServer();
  }

  // What this page learns about its own server is said the way everything
  // else is said now: a card in the corner, an entry behind the bell, the
  // mark on the tab. No goal and no row -- it is about the workspace.
  function serverNotice(kind, detail) {
    var log = loadAlertLog();
    alertSeq += 1;
    var entry = { id: "a" + Date.now().toString(36) + "-" + alertSeq,
                  kind: kind, goalId: "", goalTitle: "", rowId: "",
                  text: detail, question: "",
                  at: Date.now(), read: false };
    log.unshift(entry);
    saveAlertLog();
    if (alertSettings().banners) showAlertBanner(entry);
    renderBell();
    renderAlertCenter();
    applyPageTitle();
    return entry.id;
  }

  // The same card, raised by the other way this is ever learned: a server
  // old enough not to stamp the page at all still answers a control it has
  // never heard of, and that answer is the stamp. Once per page: the fact
  // does not change until the server does.
  var staleSaid = false;
  function noteStaleServer() {
    if (staleSaid) return false;
    staleSaid = true;
    serverNotice("server_stale",
                 "Restart it (/goals-ui) — anything added since it started"
                 + " will not work here.");
    return true;
  }

  // The other end of the same trouble: not an old server but no server. The
  // remedy for a stale one is to restart it, and a restart is exactly what
  // leaves this window pointed at a process that has ended -- with a newer
  // one already open in another tab. Saying so is the difference between a
  // workspace that closed and a workspace that looks broken.
  var goneSaid = null;
  function noteServerGone() {
    if (goneSaid) return false;
    goneSaid = serverNotice(
      "server_gone",
      "It was stopped or restarted — the newer one opens in its own tab."
      + " Nothing typed here is being saved.");
    return true;
  }

  // A server answering again takes the card back: read, and off the screen.
  function clearServerGone() {
    if (!goneSaid) return false;
    var id = goneSaid;
    goneSaid = null;
    markAlertRead(id, true);
    dropAlertBanner(alertBannerFor(id));
    renderBell();
    renderAlertCenter();
    applyPageTitle();
    return true;
  }

  // --- what the builder just did to a TODO row ------------------------------
  // A row handed to the builder comes back from the server as done, failed,
  // or asking. The rail shows that for the goal on screen; this says it for
  // every goal, once, in the top-right corner, and keeps the list behind a
  // bell in the header. Chat scope only, like the notices above: a global
  // vault has no builder behind it.
  //
  // Detection is a diff of row status between two accepted states, so it
  // needs no new server field and fires exactly once per transition. The
  // first state seen is the baseline: a page opening on a finished build
  // does not report old news. The race a 1.5s poll can lose -- a row that
  // goes from handed-off to done between two polls -- is covered for rows
  // this page handed off itself: todoBuild and todoAnswer mark them out
  // before the server is asked, so the next "done" still reads as a finish.

  var ALERT_SETTINGS_KEY = "hc-alerts-settings-v1";
  var ALERT_LOG_KEY = "hc-alerts-log-v1";
  var ALERT_LOG_MAX = 50;
  var ALERT_SECONDS_MIN = 1;
  var ALERT_SECONDS_MAX = 120;
  var ALERT_DEFAULTS = { banners: true, seconds: 6 };
  var ALERT_OUT = { building: true, queued: true, asking: true };
  var ALERT_SAYS = Object.create(null);
  ALERT_SAYS.done = "TODO finished";
  ALERT_SAYS.failed = "TODO failed";
  ALERT_SAYS.asking = "Claude has a question";
  // What a synced chat did, said in the same three lines a build gets.
  ALERT_SAYS.session_stopped = "Claude finished responding";
  ALERT_SAYS.subagent_returned = "A subagent returned";
  ALERT_SAYS.session_ended = "Session ended";
  ALERT_SAYS.server_stale = "This workspace is running older code than the plugin on disk";
  ALERT_SAYS.server_gone = "This workspace is no longer running";

  var ALERT_CSS = [
      ".hc-alert-stack{position:fixed;top:var(--hc-alerts-top,calc(var(--hc-top,37px) + 10px));right:16px;z-index:100002;display:flex;flex-direction:column;align-items:flex-end;gap:8px;pointer-events:none}",
      ".hc-alert{pointer-events:auto;position:relative;box-sizing:border-box;width:320px;max-width:calc(100vw - 32px);padding:9px 24px 9px 11px;border:1px solid var(--bd2,#d5d5d5);border-left:2px solid var(--acc,#a5492a);border-radius:2px;background:var(--panel,#fff);color:var(--ink,#111);box-shadow:0 10px 30px rgba(0,0,0,.16);font:11px/1.5 'Source Code Pro',ui-monospace,monospace;cursor:pointer}",
      ".hc-alert[data-hc-alert-kind=\"done\"]{border-left-color:var(--hc-ok,#1a7f37)}",
      ".hc-alert[data-hc-alert-kind=\"failed\"]{border-left-color:var(--del,#b42318)}",
      ".hc-alert[data-hc-alert-kind=\"asking\"]{border-left-color:var(--hc-warn,#9a6700)}",
      ".hc-alert-title{font-weight:600;color:var(--ink,#111)}",
      ".hc-alert-detail{margin-top:3px;color:var(--mut,#575757);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-alert-goal{margin-top:2px;color:var(--fnt,#9b9b9b);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-alert-close{position:absolute;top:4px;right:5px;width:15px;height:15px;display:flex;align-items:center;justify-content:center;border-radius:2px;color:var(--mut,#575757);cursor:pointer;user-select:none;font:12px/1 'Source Code Pro',monospace}",
      ".hc-alert-close:hover{color:var(--ink,#111);background:var(--hov,#f4f4f4)}",
      // The bell, in the header slot the template leaves for it.
      ".hc-alerts{display:inline-flex;align-items:center;align-self:center}",
      ".hc-bell{position:relative;display:inline-flex;align-items:center;cursor:pointer;color:var(--fnt,#9b9b9b);user-select:none;padding:2px}",
      ".hc-bell:hover,.hc-bell[data-hc-bell-open]{color:var(--ink,#111)}",
      ".hc-bell-count{display:none;position:absolute;top:-4px;right:-6px;min-width:14px;height:14px;padding:0 3px;box-sizing:border-box;border-radius:7px;background:var(--acc,#a5492a);color:var(--onacc,#fff);font:9px/14px 'Source Code Pro',monospace;text-align:center}",
      ".hc-bell[data-hc-unread] .hc-bell-count{display:block}",
      // The center: a list under the bell, newest first, with the settings
      // that govern the banners at its foot.
      ".hc-alert-center{position:fixed;top:var(--hc-alerts-top,calc(var(--hc-top,37px) + 6px));right:var(--hc-alerts-right,16px);z-index:100003;width:360px;max-width:calc(100vw - 32px);max-height:calc(100vh - var(--hc-alerts-top,calc(var(--hc-top,37px) + 6px)) - 18px);display:flex;flex-direction:column;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;background:var(--panel,#fff);color:var(--ink,#111);box-shadow:0 10px 30px rgba(0,0,0,.16);font:11px/1.5 'Source Code Pro',ui-monospace,monospace}",
      ".hc-alert-center-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 11px;border-bottom:1px solid var(--bd,#e3e3e3);font-weight:600}",
      ".hc-alert-center-act{font-weight:400;color:var(--mut,#575757);cursor:pointer;user-select:none;margin-left:10px}",
      ".hc-alert-center-act:hover{color:var(--ink,#111)}",
      ".hc-alert-center-list{flex:1 1 auto;overflow-y:auto;min-height:0}",
      ".hc-alert-center-empty{padding:14px 11px;color:var(--mut,#575757)}",
      ".hc-alert-row{position:relative;display:block;padding:8px 11px 8px 22px;border-bottom:1px solid var(--bd,#e3e3e3);cursor:pointer}",
      ".hc-alert-row:hover{background:var(--hov,#f4f4f4)}",
      ".hc-alert-row::before{content:'';position:absolute;left:9px;top:14px;width:6px;height:6px;border-radius:3px;background:transparent}",
      ".hc-alert-row[data-hc-alert-unread]::before{background:var(--acc,#a5492a)}",
      ".hc-alert-row[data-hc-alert-unread] .hc-alert-title{color:var(--ink,#111)}",
      ".hc-alert-row .hc-alert-title{font-weight:500;color:var(--mut,#575757)}",
      ".hc-alert-when{float:right;color:var(--fnt,#9b9b9b);font-weight:400;margin-left:8px}",
      // Banners and the center live on the body, outside .hc, where the
      // artifact's theme variables do not reach; the theme is mirrored onto
      // the root, so the dark palette is spelled out here in the launch
      // skin's own greys.
      "[data-hc-theme=\"dark\"] .hc-alert,[data-hc-theme=\"dark\"] .hc-alert-center{background:#161b22;color:#e6edf3;border-color:#30363d;box-shadow:0 10px 30px rgba(0,0,0,.5)}",
      "[data-hc-theme=\"dark\"] .hc-alert[data-hc-alert-kind=\"done\"]{border-left-color:#3fb950}",
      "[data-hc-theme=\"dark\"] .hc-alert[data-hc-alert-kind=\"failed\"]{border-left-color:#f85149}",
      "[data-hc-theme=\"dark\"] .hc-alert[data-hc-alert-kind=\"asking\"]{border-left-color:#d29922}",
      "[data-hc-theme=\"dark\"] .hc-alert-title,[data-hc-theme=\"dark\"] .hc-alert-center-head,[data-hc-theme=\"dark\"] .hc-alert-row[data-hc-alert-unread] .hc-alert-title{color:#e6edf3}",
      "[data-hc-theme=\"dark\"] .hc-alert-detail,[data-hc-theme=\"dark\"] .hc-alert-close,[data-hc-theme=\"dark\"] .hc-alert-center-act,[data-hc-theme=\"dark\"] .hc-alert-center-empty,[data-hc-theme=\"dark\"] .hc-alert-row .hc-alert-title{color:#8b949e}",
      "[data-hc-theme=\"dark\"] .hc-alert-goal,[data-hc-theme=\"dark\"] .hc-alert-when{color:#6e7681}",
      "[data-hc-theme=\"dark\"] .hc-alert-center-head,[data-hc-theme=\"dark\"] .hc-alert-row{border-color:#21262d}",
      "[data-hc-theme=\"dark\"] .hc-alert-close:hover,[data-hc-theme=\"dark\"] .hc-alert-center-act:hover{color:#e6edf3;background:#21262d}",
      "[data-hc-theme=\"dark\"] .hc-alert-row:hover{background:#1c2128}",
      // The gear, in the header slot after the bell, and the settings panel
      // it opens. What governs the banners lives here, not in the center:
      // the center lists what happened; the gear is where the page is set.
      ".hc-settings{display:inline-flex;align-items:center;align-self:center}",
      ".hc-gear{display:inline-flex;align-items:center;cursor:pointer;color:var(--fnt,#9b9b9b);user-select:none;padding:2px}",
      ".hc-gear:hover,.hc-gear[data-hc-gear-open]{color:var(--ink,#111)}",
      ".hc-settings-panel{position:fixed;top:var(--hc-settings-top,44px);right:var(--hc-settings-right,16px);z-index:100003;width:348px;max-width:calc(100vw - 32px);max-height:calc(100vh - var(--hc-settings-top,44px) - 16px);text-align:left;display:flex;flex-direction:column;overflow-y:auto;border:1px solid var(--bd,#e3e3e3);border-radius:10px;background:var(--panel,#fff);color:var(--ink,#111);box-shadow:0 12px 34px rgba(0,0,0,.18);font:12px/1.6 'Source Code Pro',ui-monospace,monospace}",
      ".hc-settings-tabs{display:flex;gap:20px;align-items:stretch;padding:0 18px;border-bottom:1px solid var(--bd,#e3e3e3)}",
      ".hc-settings-tab{cursor:pointer;user-select:none;font:600 10px 'Source Code Pro',monospace;letter-spacing:1.4px;text-transform:uppercase;color:var(--fnt,#9b9b9b);padding:10px 0 9px;border-bottom:2px solid transparent;margin-bottom:-1px}",
      ".hc-settings-tab:hover{color:var(--ink,#111)}",
      ".hc-settings-tab[data-hc-on]{color:var(--acc,#a5492a);border-bottom-color:var(--acc,#a5492a)}",
      ".hc-settings-sec[data-hc-tab]{display:none}",
      ".hc-settings-sec[data-hc-tab][data-hc-on]{display:flex}",
      ".hc-settings-head{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;padding:15px 18px 11px;font:800 14px 'Source Code Pro',monospace;letter-spacing:.2px;color:var(--ink,#111)}",
      ".hc-settings-act{flex:none;font:400 15px/1 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b);cursor:pointer;user-select:none;padding:2px 4px}",
      ".hc-settings-act:hover{color:var(--ink,#111)}",
      ".hc-settings-sec{padding:14px 18px 16px;display:flex;flex-direction:column;gap:9px;color:var(--mut,#575757)}",
      ".hc-settings-sec[data-hc-on]+.hc-settings-sec[data-hc-on]{border-top:1px solid var(--bd,#e3e3e3)}",
      ".hc-settings-sec-head{font:600 10.5px 'Source Code Pro',monospace;letter-spacing:2px;text-transform:uppercase;color:var(--mut,#575757)}",
      ".hc-settings-sec label{display:flex;align-items:center;gap:9px;cursor:pointer;color:var(--dtxt,#333)}",
      ".hc-settings-sec input[type=number]{width:56px;box-sizing:border-box;border:1px solid var(--bd,#e3e3e3);border-radius:6px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11.5px 'Source Code Pro',monospace;padding:4px 6px}",
      ".hc-settings-sec input[type=text],.hc-settings-sec input[type=password]{width:100%;box-sizing:border-box;border:1px solid var(--bd,#e3e3e3);border-radius:6px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11.5px 'Source Code Pro',monospace;padding:7px 9px}",
      ".hc-settings-sec input:focus{outline:none;border-color:var(--acc,#a5492a)}",
      ".hc-settings-field{display:flex;flex-direction:column;gap:4px;align-items:flex-start;text-align:left;cursor:default;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1.2px;text-transform:uppercase;color:var(--fnt,#9b9b9b)}",
      ".hc-settings-field>input{align-self:stretch;box-sizing:border-box}",
      ".hc-settings-hint{color:var(--fnt,#9b9b9b);font-size:10.5px;line-height:1.6;overflow-wrap:anywhere;margin-top:-3px}",
      ".hc-settings-row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}",
      ".hc-settings-btn{cursor:pointer;user-select:none;border:1px solid var(--acc,#a5492a);border-radius:3px;background:var(--acc,#a5492a);color:var(--onacc,#fff);font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;text-transform:uppercase;padding:7px 13px}",
      ".hc-settings-btn[data-hc-quiet]{background:transparent;color:var(--mut,#575757);border-color:var(--bd2,#d5d5d5)}",
      ".hc-settings-btn[data-hc-quiet]:hover{color:var(--ink,#111);border-color:var(--ink,#111)}",
      ".hc-settings-btn[data-hc-busy]{opacity:.55;cursor:default}",
      ".hc-settings-say{font-size:10.5px;line-height:1.6;color:var(--mut,#575757);overflow-wrap:anywhere}",
      ".hc-settings-say[data-hc-bad]{color:var(--bad,#a12d2d)}",
      ".hc-settings-record{margin:0;white-space:pre;overflow:auto;max-height:46vh;border:1px solid var(--bd,#e3e3e3);border-radius:6px;background:var(--panel2,#f6f6f6);color:var(--dtxt,#333);font:11px/1.6 'Source Code Pro',monospace;padding:9px 11px}",
      ".hc-settings-role{cursor:pointer;user-select:none;font:600 10px 'Source Code Pro',monospace;letter-spacing:1px;text-transform:uppercase;color:var(--mut,#575757);border-bottom:1px dashed var(--bd2,#d5d5d5);padding-bottom:1px}",
      ".hc-settings-role:hover{color:var(--ink,#111)}",
      ".hc-settings-code{display:none;flex-direction:column;gap:6px}",
      ".hc-settings-code[data-hc-on]{display:flex}",
      ".hc-settings-code-box{display:block;width:100%;box-sizing:border-box;resize:none;border:1px solid var(--bd,#e3e3e3);border-radius:6px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11px/1.6 'Source Code Pro',monospace;padding:8px 10px;word-break:break-all}",
      ".hc-settings-shares{display:flex;flex-direction:column;gap:5px;padding-top:2px}",
      ".hc-settings-shares:empty{display:none}",
      ".hc-settings-share-row{display:flex;gap:9px;align-items:baseline;font-size:10.5px;color:var(--mut,#575757);flex-wrap:wrap}",
      ".hc-settings-share-x{cursor:pointer;user-select:none;color:var(--fnt,#9b9b9b)}",
      ".hc-settings-share-x:hover{color:var(--bad,#a12d2d)}",
      "[data-hc-readonly] .hc-settings-sync{display:none}",
      // The hand-off, in the header slot before the bell: one click puts
      // the whole workspace -- goals, notes, TODO states, git -- on the
      // clipboard as markdown for a teammate's agent, and says so briefly.
      ".hc-handoff{display:inline-flex;align-items:center;align-self:center}",
      ".hc-handoff-btn{position:relative;display:inline-flex;align-items:center;gap:4px;cursor:pointer;color:var(--fnt,#9b9b9b);user-select:none;padding:2px;font:10px/1 'Source Code Pro',ui-monospace,monospace}",
      ".hc-handoff-btn:hover{color:var(--ink,#111)}",
      ".hc-handoff-btn[data-hc-handoff=\"busy\"]{color:var(--mut,#575757);cursor:progress}",
      ".hc-handoff-btn[data-hc-handoff=\"copied\"]{color:var(--ok,#2f7d4f)}",
      ".hc-handoff-btn[data-hc-handoff=\"failed\"]{color:var(--acc,#a5492a)}",
      ".hc-handoff-said{display:none;white-space:nowrap}",
      ".hc-handoff-btn[data-hc-handoff] .hc-handoff-said{display:inline}"
  ].join("");

  var HANDOFF_ICON = "<svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7\"></path><polyline points=\"16 6 12 2 8 6\"></polyline><line x1=\"12\" y1=\"2\" x2=\"12\" y2=\"15\"></line></svg>";
  var HANDOFF_SAYS = { busy: "assembling…", copied: "copied ✓",
                       failed: "copy failed" };
  var HANDOFF_TITLE = "Hand off: copy the whole workspace as markdown for a teammate’s agent";

  var BELL_ICON = "<svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9\"></path><path d=\"M13.7 21a2 2 0 0 1-3.4 0\"></path></svg>";
  var GEAR_ICON = "<svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"12\" cy=\"12\" r=\"3\"></circle><path d=\"M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z\"></path></svg>";

  var alertPrev = null;            // row id -> status, from the last state
  var alertLog = null;             // newest first; loaded on first use
  var alertSettingsCache = null;
  var alertStackBox = null;
  var alertCenterBox = null;
  var settingsPanelBox = null;
  var alertTimers = Object.create(null);
  var alertBound = false;
  var alertSeq = 0;

  function ensureAlertStyles() {
    if (document.getElementById("hc-alert-style")) return;
    var style = document.createElement("style");
    style.id = "hc-alert-style";
    style.textContent = ALERT_CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  function alertSettings() {
    if (alertSettingsCache) return alertSettingsCache;
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(ALERT_SETTINGS_KEY) || "null"); }
    catch (e) { saved = null; }
    var out = { banners: ALERT_DEFAULTS.banners, seconds: ALERT_DEFAULTS.seconds };
    if (saved && typeof saved === "object") {
      if (typeof saved.banners === "boolean") out.banners = saved.banners;
      if (typeof saved.seconds === "number" && isFinite(saved.seconds)) {
        out.seconds = alertClampSeconds(saved.seconds);
      }
    }
    alertSettingsCache = out;
    return out;
  }

  function alertClampSeconds(value) {
    var n = Number(value);
    if (!isFinite(n)) return ALERT_DEFAULTS.seconds;
    n = Math.round(n);
    if (n < ALERT_SECONDS_MIN) return ALERT_SECONDS_MIN;
    if (n > ALERT_SECONDS_MAX) return ALERT_SECONDS_MAX;
    return n;
  }

  function setAlertSettings(patch) {
    var cur = alertSettings();
    var next = { banners: cur.banners, seconds: cur.seconds };
    if (patch && typeof patch === "object") {
      if (typeof patch.banners === "boolean") next.banners = patch.banners;
      if (patch.seconds !== undefined && patch.seconds !== null && patch.seconds !== "") {
        next.seconds = alertClampSeconds(patch.seconds);
      }
    }
    alertSettingsCache = next;
    try { localStorage.setItem(ALERT_SETTINGS_KEY, JSON.stringify(next)); } catch (e) {}
    // A banner already up keeps the clock it was armed with; the next one
    // gets the new one. Turning banners off takes the ones up down.
    if (!next.banners) dropAllAlerts();
    renderSettingsPanel();
    return next;
  }

  function loadAlertLog() {
    if (alertLog) return alertLog;
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(ALERT_LOG_KEY) || "null"); }
    catch (e) { saved = null; }
    alertLog = array(saved).filter(function (row) {
      return row && typeof row === "object" && typeof row.id === "string"
        && ALERT_SAYS[str(row.kind)];
    }).map(function (row) {
      return { id: row.id, kind: str(row.kind), goalId: str(row.goalId),
               goalTitle: str(row.goalTitle), rowId: str(row.rowId),
               text: str(row.text), question: str(row.question),
               at: (typeof row.at === "number") ? row.at : Date.parse(str(row.at)) || 0,
               read: !!row.read };
    }).slice(0, ALERT_LOG_MAX);
    return alertLog;
  }

  function saveAlertLog() {
    if (!alertLog) return;
    if (alertLog.length > ALERT_LOG_MAX) alertLog.length = ALERT_LOG_MAX;
    try { localStorage.setItem(ALERT_LOG_KEY, JSON.stringify(alertLog)); } catch (e) {}
  }

  // The store is the log; the array above is only what this page last read
  // of it. More than one workspace page can be open on the same store -- a
  // second window, a tab left over from an earlier run -- so anything about
  // to write reads first. Without this, clearing the log in one page left
  // the others holding the entries it cleared, and the next alert was added
  // to that remembered list: a bell that had just been emptied came back
  // reading total + 1.
  function reloadAlertLog() {
    alertLog = null;
    return loadAlertLog();
  }

  function alertUnread() {
    return loadAlertLog().filter(function (row) { return !row.read; }).length;
  }

  function alertById(id) {
    var log = loadAlertLog();
    for (var i = 0; i < log.length; i++) if (log[i].id === id) return log[i];
    return null;
  }

  function markAlertRead(id, read) {
    reloadAlertLog();
    var row = alertById(id);
    if (!row || row.read === !!read) return false;
    row.read = !!read;
    saveAlertLog();
    renderBell();
    renderAlertCenter();
    applyPageTitle();
    return true;
  }

  function markAllAlertsRead() {
    var changed = false;
    reloadAlertLog().forEach(function (row) {
      if (!row.read) { row.read = true; changed = true; }
    });
    if (changed) {
      saveAlertLog(); renderBell(); renderAlertCenter(); applyPageTitle();
    }
    return changed;
  }

  function clearAlertLog() {
    alertLog = [];
    saveAlertLog();
    dropAllAlerts();
    renderBell();
    renderAlertCenter();
    applyPageTitle();
  }

  // Another page writing the log -- clearing it, or reading something in it
  // -- arrives as a storage event here. What this one remembered is dropped,
  // banners for entries that have gone or have been read come down, and the
  // bell counts what is now stored.
  function alertLogChangedElsewhere() {
    reloadAlertLog();
    var host = alertStack();
    var up = host ? Array.prototype.slice.call(host.children || []) : [];
    up.forEach(function (box) {
      var entry = alertById(alertIdOf(box));
      if (!entry || entry.read) dropAlertBanner(box);
    });
    renderBell();
    renderAlertCenter();
  }

  // The transitions worth a word, between two states of the goals.
  function todoAlertsFrom(goals, prev) {
    var next = Object.create(null), fresh = [];
    array(goals).forEach(function (goal) {
      if (!goal || typeof goal !== "object") return;
      array(goal.todo_items).forEach(function (row) {
        if (!row || typeof row.id !== "string") return;
        var status = str(row.status);
        next[row.id] = status;
        if (!prev) return;
        var was = str(prev[row.id]);
        if (was === status) return;
        var kind = "";
        if (status === "done" && ALERT_OUT[was]) kind = "done";
        else if (status === "failed") kind = "failed";
        else if (status === "asking") kind = "asking";
        if (!kind) return;
        fresh.push({ kind: kind, goalId: str(goal.id),
                     goalTitle: str(goal.title).trim() || "Untitled",
                     rowId: row.id, text: str(row.text).trim(),
                     question: str(row.question).trim() });
      });
    });
    return { next: next, fresh: fresh };
  }

  // Rows this page just handed to the builder: remembered as out so a
  // finish that lands before the next poll still counts as one.
  function alertNoteOut(ids) {
    if (!alertPrev) alertPrev = Object.create(null);
    array(ids).forEach(function (id) {
      if (typeof id === "string") alertPrev[id] = "building";
    });
  }

  // A banner that was up when the page reloaded -- and a state change from
  // the builder is exactly what makes reconcileState reload it -- comes back
  // for the time it had left. Read, dismissed, or expired ones do not.
  function resumeAlertBanners() {
    if (!alertSettings().banners) return 0;
    var window_ms = alertSettings().seconds * 1000;
    var now = Date.now();
    var back = loadAlertLog().filter(function (entry) {
      return !entry.read && now - entry.at < window_ms && !alertBannerFor(entry.id);
    }).slice(0, NOTICE_MAX).reverse();
    back.forEach(function (entry) {
      showAlertBanner(entry, window_ms - (now - entry.at));
    });
    return back.length;
  }

  function trackTodoAlerts(st) {
    if (!st || serverState.scope !== "chat" || !Array.isArray(st.goals)) return [];
    var diff = todoAlertsFrom(st.goals, alertPrev);
    alertPrev = diff.next;
    if (!diff.fresh.length) return [];
    var log = reloadAlertLog();
    var made = diff.fresh.map(function (row) {
      alertSeq += 1;
      var entry = { id: "a" + Date.now().toString(36) + "-" + alertSeq,
                    kind: row.kind, goalId: row.goalId, goalTitle: row.goalTitle,
                    rowId: row.rowId, text: row.text, question: row.question,
                    at: Date.now(), read: false };
      log.unshift(entry);
      return entry;
    });
    saveAlertLog();
    if (alertSettings().banners) {
      made.forEach(function (entry) { showAlertBanner(entry); });
    }
    renderBell();
    renderAlertCenter();
    applyPageTitle();
    return made;
  }

  // In the live document, not merely parented: the artifact unpacks its
  // template by replacing the whole documentElement, so a node appended to
  // the body before that keeps a parent and is on no screen.
  function inLiveDocument(node) {
    var at = node;
    while (at && at.parentNode) at = at.parentNode;
    return !!at && (at === document || at === document.documentElement);
  }

  function alertStack() {
    return (alertStackBox && inLiveDocument(alertStackBox)) ? alertStackBox : null;
  }

  function alertHost() {
    if (alertStack()) return alertStackBox;
    ensureAlertStyles();
    placeAlerts();
    alertStackBox = document.createElement("div");
    alertStackBox.className = "hc-alert-stack";
    // On the body, outside the artifact's subtree, for the same reason the
    // notices are: the artifact rebuilds its subtree on every state change.
    (document.body || document.documentElement).appendChild(alertStackBox);
    return alertStackBox;
  }

  function alertDetailOf(entry) {
    if (entry.kind === "asking" && entry.question) return entry.question;
    // A hook that carried nothing to quote gets a headline and no blank
    // line pretending there was something to say. A TODO always has a row
    // to name, so an empty one there is a bug worth seeing.
    if (NOTICE_KINDS[entry.kind]) return entry.text;
    return entry.text || "(untitled TODO)";
  }

  function alertBannerNode(entry) {
    var box = document.createElement("div");
    box.className = "hc-alert";
    box.setAttribute("data-hc-alert", entry.id);
    box.setAttribute("data-hc-alert-kind", entry.kind);
    box.setAttribute("role", "status");
    var close = document.createElement("span");
    close.className = "hc-alert-close";
    close.textContent = "×";
    close.setAttribute("role", "button");
    close.setAttribute("aria-label", "Dismiss");
    box.appendChild(close);
    var title = document.createElement("div");
    title.className = "hc-alert-title";
    title.textContent = ALERT_SAYS[entry.kind];
    box.appendChild(title);
    var said = alertDetailOf(entry);
    if (said) {
      var detail = document.createElement("div");
      detail.className = "hc-alert-detail";
      detail.textContent = said;
      box.appendChild(detail);
    }
    // A session card names no goal: there is no line under it to carry.
    if (entry.goalTitle) {
      var goal = document.createElement("div");
      goal.className = "hc-alert-goal";
      goal.textContent = entry.goalTitle;
      box.appendChild(goal);
    }
    return box;
  }

  function showAlertBanner(entry, ms) {
    var up = alertBannerFor(entry.id);
    if (up) return up;
    var host = alertHost();
    bindAlerts();
    var box = host.appendChild(alertBannerNode(entry));
    // Three at once is what fits without covering the page it reports on.
    while (host.children.length > NOTICE_MAX) dropAlertBanner(host.children[0]);
    armAlert(box, ms);
    return box;
  }

  function alertIdOf(box) {
    return (box && box.getAttribute) ? str(box.getAttribute("data-hc-alert")) : "";
  }

  function holdAlert(box) {
    var id = alertIdOf(box);
    if (!id || !alertTimers[id]) return;
    clearTimeout(alertTimers[id]);
    delete alertTimers[id];
  }

  function armAlert(box, ms) {
    var id = alertIdOf(box);
    if (!id) return;
    holdAlert(box);
    var wait = (typeof ms === "number" && isFinite(ms) && ms > 0)
      ? ms : alertSettings().seconds * 1000;
    alertTimers[id] = setTimeout(function () { dropAlertBanner(box); }, wait);
  }

  function dropAlertBanner(box) {
    if (!box) return;
    holdAlert(box);
    if (box.parentNode) box.parentNode.removeChild(box);
  }

  function dropAllAlerts() {
    var host = alertStack();
    if (!host) return;
    while (host.children && host.children.length) dropAlertBanner(host.children[0]);
  }

  function alertBannerFor(id) {
    var host = alertStack();
    var kids = (host && host.children) || [];
    for (var i = 0; i < kids.length; i++) if (alertIdOf(kids[i]) === id) return kids[i];
    return null;
  }

  // Going to the row: the goal is selected in the tree, the rail opens on
  // its TODOs, and the entry reads as seen.
  function alertGo(id) {
    var entry = alertById(id);
    if (!entry) return false;
    markAlertRead(id, true);
    dropAlertBanner(alertBannerFor(id));
    closeAlertCenter();
    // A session card reports on the conversation, not on a row. Reading it
    // is the whole of it: moving the rail to a goal it never named would
    // take the reader off whatever they were working on.
    if (NOTICE_KINDS[entry.kind]) return true;
    railTab = "todos";
    if (entry.goalId) {
      if (typeof window !== "undefined" && typeof window.__hcSelectGoal === "function") {
        try { window.__hcSelectGoal(entry.goalId); } catch (e) {}
      } else {
        try {
          var saved = JSON.parse(localStorage.getItem(KEY) || "{}");
          saved.selId = entry.goalId;
          localStorage.setItem(KEY, JSON.stringify(saved));
        } catch (e) {}
      }
    }
    renderTodoRail(true);
    return true;
  }

  function closestByClass(node, name) {
    while (node && node !== document) {
      var own = node.className ? String(node.className).split(" ") : [];
      if (own.indexOf(name) >= 0) return node;
      node = node.parentNode;
    }
    return null;
  }

  function bindAlerts() {
    if (alertBound || !document.addEventListener) return;
    alertBound = true;
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("storage", function (event) {
        if (event && str(event.key) === ALERT_LOG_KEY) alertLogChangedElsewhere();
      });
    }
    document.addEventListener("click", function (event) {
      var target = event && event.target;
      if (!target) return;
      var stop = function () {
        if (event.preventDefault) event.preventDefault();
        if (event.stopPropagation) event.stopPropagation();
      };
      // The bell toggles the center; the gear toggles the settings panel.
      if (closestByClass(target, "hc-handoff-btn")) {
        stop();
        copyHandoff();
        return;
      }
      // One of the two is up at a time.
      if (closestByClass(target, "hc-bell")) {
        stop();
        closeSettingsPanel();
        toggleAlertCenter();
        return;
      }
      if (closestByClass(target, "hc-gear")) {
        stop();
        closeAlertCenter();
        toggleSettingsPanel();
        return;
      }
      var panel = closestByClass(target, "hc-settings-panel");
      if (panel) {
        if (closestByClass(target, "hc-settings-act")) {
          stop();
          closeSettingsPanel();
          return;
        }
        var openId = target.getAttribute
          && target.getAttribute("data-hc-open-shared");
        if (openId) { stop(); openShared(openId); return; }
        var tab = closestByClass(target, "hc-settings-tab");
        if (tab) {
          stop();
          setSettingsTab(tab.getAttribute("data-hc-settings-tab"));
          return;
        }
        var sbBtn = closestByClass(target, "hc-settings-btn");
        // The sharing controls wear the same button class and are handled
        // where the sharing is; only the ones naming an account action
        // belong here.
        if (sbBtn && sbBtn.getAttribute("data-hc-sb-do") !== null) {
          stop();
          var what = sbBtn.getAttribute("data-hc-sb-do");
          if (what === "join") joinShared();
          else settingsSupabaseDo(what);
          return;
        }
        // Clicks on the controls fall through to the inputs.
        return;
      }
      // A banner: × dismisses it as read; anywhere else goes to the row.
      var banner = closestByClass(target, "hc-alert");
      if (banner) {
        stop();
        var id = alertIdOf(banner);
        if (closestByClass(target, "hc-alert-close")) {
          markAlertRead(id, true);
          dropAlertBanner(banner);
        } else {
          alertGo(id);
        }
        return;
      }
      var center = closestByClass(target, "hc-alert-center");
      if (center) {
        var act = closestByClass(target, "hc-alert-center-act");
        if (act) {
          stop();
          var what = str(act.getAttribute("data-hc-alert-act"));
          if (what === "read-all") markAllAlertsRead();
          else if (what === "clear") clearAlertLog();
          else if (what === "close") closeAlertCenter();
          return;
        }
        var row = closestByClass(target, "hc-alert-row");
        if (row) { stop(); alertGo(str(row.getAttribute("data-hc-alert"))); }
        // Clicks on the settings controls fall through to the inputs.
        return;
      }
      // Anywhere else closes whichever is up.
      if (alertCenterShown()) closeAlertCenter();
      if (settingsPanelShown()) closeSettingsPanel();
    }, true);
    document.addEventListener("change", function (event) {
      var target = event && event.target;
      var key = (target && target.getAttribute) ? str(target.getAttribute("data-hc-alert-set")) : "";
      if (!key) return;
      if (key === "banners") setAlertSettings({ banners: !!target.checked });
      else if (key === "seconds") setAlertSettings({ seconds: target.value });
    }, true);
    // The clock stops while the pointer is on a banner, as for the notices.
    document.addEventListener("mouseover", function (event) {
      var box = closestByClass(event && event.target, "hc-alert");
      if (box) holdAlert(box);
    }, true);
    document.addEventListener("mouseout", function (event) {
      var box = closestByClass(event && event.target, "hc-alert");
      if (!box) return;
      var to = event.relatedTarget;
      if (to && box.contains && box.contains(to)) return;
      armAlert(box);
    }, true);
  }

  // The bell sits in the header; its badge is the unread count.
  var alertResumed = false;

  function renderBell() {
    if (serverState.scope !== "chat") return false;
    var slot = document.querySelector(".hc-alerts");
    if (!slot) return false;
    ensureAlertStyles();
    bindAlerts();
    // The slot exists only once the artifact has unpacked the patched
    // template, which is the first moment a banner appended to the body
    // stays on screen. Whatever was up before the reload comes back here.
    if (!alertResumed) { alertResumed = true; resumeAlertBanners(); }
    var bell = slot.querySelector(".hc-bell");
    if (!bell) {
      bell = document.createElement("span");
      bell.className = "hc-bell";
      bell.setAttribute("role", "button");
      bell.setAttribute("aria-label", "Notifications");
      bell.title = "Notifications";
      bell.innerHTML = BELL_ICON;
      var count = document.createElement("span");
      count.className = "hc-bell-count";
      bell.appendChild(count);
      slot.appendChild(bell);
    }
    placeAlerts();
    var unread = alertUnread();
    var badge = bell.querySelector(".hc-bell-count");
    var label = unread > 99 ? "99+" : String(unread);
    var changed = false;
    if (badge && badge.textContent !== label) { badge.textContent = label; changed = true; }
    var has = bell.getAttribute("data-hc-unread") !== null;
    if (unread && !has) { bell.setAttribute("data-hc-unread", String(unread)); changed = true; }
    else if (unread && bell.getAttribute("data-hc-unread") !== String(unread)) {
      bell.setAttribute("data-hc-unread", String(unread)); changed = true;
    } else if (!unread && has) { bell.removeAttribute("data-hc-unread"); changed = true; }
    var open = alertCenterShown();
    if (open && bell.getAttribute("data-hc-bell-open") === null) {
      bell.setAttribute("data-hc-bell-open", ""); changed = true;
    } else if (!open && bell.getAttribute("data-hc-bell-open") !== null) {
      bell.removeAttribute("data-hc-bell-open"); changed = true;
    }
    return changed;
  }

  function alertCenterShown() {
    return !!(alertCenterBox && inLiveDocument(alertCenterBox));
  }

  // Under the bell, measured from the bell. Both the banners and the center
  // used to hang from --hc-top -- the bottom of the whole bar stack, which
  // grows with the notice row and the view tabs -- so with those on screen
  // they opened two rows below the icon they belong to. Re-measured on every
  // bell render, which is the same sweep that adds and removes those bars.
  function placeAlerts() {
    var root = document.documentElement;
    var bell = document.querySelector(".hc-bell");
    if (!root || !root.style || typeof root.style.setProperty !== "function"
        || !bell || !bell.getBoundingClientRect) return false;
    var box = bell.getBoundingClientRect();
    if (!box || typeof box.bottom !== "number") return false;
    var under = Math.round(Number(box.bottom) || 0);
    if (under <= 0) return false;
    root.style.setProperty("--hc-alerts-top", (under + 6) + "px");
    var width = Number((document.documentElement || {}).clientWidth)
      || Number(window.innerWidth) || 0;
    if (width > 0 && typeof box.right === "number") {
      root.style.setProperty("--hc-alerts-right",
        Math.max(8, Math.round(width - box.right)) + "px");
    }
    return true;
  }

  function alertWhen(at) {
    var d = new Date(at);
    if (!isFinite(d.getTime())) return "";
    var hh = d.getHours(), mm = d.getMinutes();
    return (hh < 10 ? "0" : "") + hh + ":" + (mm < 10 ? "0" : "") + mm;
  }

  function alertCenterNode() {
    var box = document.createElement("div");
    box.className = "hc-alert-center";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Notifications");
    var head = document.createElement("div");
    head.className = "hc-alert-center-head";
    var name = document.createElement("span");
    name.textContent = "Notifications";
    head.appendChild(name);
    var acts = document.createElement("span");
    [["read-all", "Mark all read"], ["clear", "Clear"], ["close", "×"]].forEach(function (spec) {
      var a = document.createElement("span");
      a.className = "hc-alert-center-act";
      a.setAttribute("data-hc-alert-act", spec[0]);
      a.setAttribute("role", "button");
      a.textContent = spec[1];
      acts.appendChild(a);
    });
    head.appendChild(acts);
    box.appendChild(head);
    var list = document.createElement("div");
    list.className = "hc-alert-center-list";
    box.appendChild(list);
    return box;
  }

  function alertInputs(node, out) {
    out = out || [];
    var kids = (node && node.children) || [];
    for (var i = 0; i < kids.length; i++) {
      if (kids[i].getAttribute && kids[i].getAttribute("data-hc-alert-set") !== null) {
        out.push(kids[i]);
      }
      alertInputs(kids[i], out);
    }
    return out;
  }

  function renderAlertCenter() {
    if (!alertCenterShown()) return false;
    var box = alertCenterBox;
    var list = box.querySelector(".hc-alert-center-list");
    if (list) {
      while (list.firstChild) list.removeChild(list.firstChild);
      var log = loadAlertLog();
      if (!log.length) {
        var empty = document.createElement("div");
        empty.className = "hc-alert-center-empty";
        empty.textContent = "Nothing yet. Builds that finish, fail, or ask "
          + "land here, and so does a synced chat answering.";
        list.appendChild(empty);
      }
      log.forEach(function (entry) {
        var row = document.createElement("div");
        row.className = "hc-alert-row";
        row.setAttribute("data-hc-alert", entry.id);
        row.setAttribute("data-hc-alert-kind", entry.kind);
        if (!entry.read) row.setAttribute("data-hc-alert-unread", "");
        var title = document.createElement("div");
        title.className = "hc-alert-title";
        var when = document.createElement("span");
        when.className = "hc-alert-when";
        when.textContent = alertWhen(entry.at);
        title.appendChild(when);
        var says = document.createElement("span");
        says.textContent = ALERT_SAYS[entry.kind];
        title.appendChild(says);
        row.appendChild(title);
        var said = alertDetailOf(entry);
        if (said) {
          var detail = document.createElement("div");
          detail.className = "hc-alert-detail";
          detail.textContent = said;
          row.appendChild(detail);
        }
        if (entry.goalTitle) {
          var goal = document.createElement("div");
          goal.className = "hc-alert-goal";
          goal.textContent = entry.goalTitle;
          row.appendChild(goal);
        }
        list.appendChild(row);
      });
    }
    return true;
  }

  function openAlertCenter() {
    if (alertCenterShown()) return alertCenterBox;
    ensureAlertStyles();
    bindAlerts();
    placeAlerts();
    alertCenterBox = alertCenterNode();
    (document.body || document.documentElement).appendChild(alertCenterBox);
    renderAlertCenter();
    renderBell();
    return alertCenterBox;
  }

  function closeAlertCenter() {
    if (!alertCenterShown()) return false;
    alertCenterBox.parentNode.removeChild(alertCenterBox);
    renderBell();
    return true;
  }

  function toggleAlertCenter() {
    return alertCenterShown() ? closeAlertCenter() : !!openAlertCenter();
  }

  // --- the project: where this chat works --------------------------------
  // A Claude Code project is a directory: the one the chat was started in,
  // which its manifest recorded and /api/state reports as `project`. The
  // header names it after the brand -- "Engelbart / myrepo ▾ / ● session" --
  // and the name opens a menu: the project's facts (directory, branch,
  // origin), a way into its overview, and the other chats started in the
  // same directory, each linkable as a prompt source for every goal.
  //
  // The overview is the project's own screen, drawn over the document
  // column and nothing else: the name and objective (the reader's words,
  // kept once per directory), and the repository as context -- its README,
  // read from the server under its containment rule, with an Ask tab beside
  // it that answers a question from that one document and nothing else.
  // Nothing here is narrated: a pane the vault cannot fill says so.
  // Chat scope only; a global vault has no manifest to name a project from.

  var PROJECT_CSS = [
      // The header spreads its children apart; the chip takes the slack on
      // its right so it stays against the brand, and the pills follow it.
      ".hc-project{display:inline-flex;align-items:center;gap:8px;margin-left:10px;margin-right:auto;font:11px 'Source Code Pro',monospace;color:var(--mut,#575757)}",
      ".hc-project:empty{display:none}",
      ".hc-project-sep{color:var(--fnt,#9b9b9b)}",
      ".hc-project-name{display:inline-flex;align-items:center;gap:5px;cursor:pointer;user-select:none;color:var(--ink,#111);font-weight:600;padding:2px 4px;border-radius:4px}",
      ".hc-project-name:hover,.hc-project-name[data-hc-project-open]{background:var(--hov,#f2f2f2)}",
      ".hc-project-caret{font-size:8px;color:var(--fnt,#9b9b9b)}",
      // The menu under the name: facts, the overview, the project's chats.
      ".hc-project-menu{position:fixed;top:var(--hc-project-top,42px);left:var(--hc-project-left,120px);z-index:100003;width:340px;max-width:calc(100vw - 32px);max-height:calc(100vh - var(--hc-project-top,42px) - 16px);overflow-y:auto;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;background:var(--panel,#fff);color:var(--ink,#111);box-shadow:0 10px 30px rgba(0,0,0,.16);font:11px/1.5 'Source Code Pro',ui-monospace,monospace}",
      ".hc-project-menu-head{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:8px 11px;border-bottom:1px solid var(--bd,#e3e3e3);font-weight:600}",
      ".hc-project-menu-sub{font-weight:400;color:var(--mut,#575757);font-size:10px}",
      ".hc-project-act{padding:7px 11px;cursor:pointer;user-select:none;color:var(--ink,#111);border-bottom:1px solid var(--bd,#e3e3e3)}",
      ".hc-project-act:hover{background:var(--hov,#f2f2f2)}",
      ".hc-project-facts{padding:6px 11px 8px;border-bottom:1px solid var(--bd,#e3e3e3)}",
      ".hc-project-fact{display:flex;gap:10px;align-items:baseline;padding:2px 0}",
      ".hc-project-fact-k{flex:none;width:62px;font:600 9px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--fnt,#9b9b9b);text-transform:uppercase}",
      ".hc-project-fact-v{flex:1 1 auto;min-width:0;overflow-wrap:anywhere;color:var(--mut,#575757)}",
      ".hc-project-fact-v a{color:inherit}",
      ".hc-project-list{padding:6px 0;max-height:46vh;overflow-y:auto}",
      ".hc-project-row{display:flex;align-items:baseline;justify-content:space-between;gap:14px;padding:7px 14px;cursor:pointer;user-select:none}",
      ".hc-project-row:hover{background:var(--hov,#f2f2f2)}",
      ".hc-project-row-name{font:600 12.5px 'Source Code Pro',monospace;color:var(--ink,#111);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-project-row[data-hc-active] .hc-project-row-name{color:var(--acc,#a5492a)}",
      ".hc-project-row-note{flex:none;font-size:10.5px;color:var(--fnt,#9b9b9b)}",
      ".hc-project-new{padding:9px 14px;cursor:pointer;user-select:none;color:var(--fnt,#9b9b9b);border-top:1px solid var(--bd,#e3e3e3);font-size:12px}",
      ".hc-project-new:hover{color:var(--ink,#111)}",
      ".hc-project-newform{display:none;flex-wrap:wrap;gap:8px;align-items:center;padding:0 14px 10px}",
      ".hc-project-newform[data-hc-on]{display:flex}",
      ".hc-project-parent{font-size:10px;color:var(--fnt,#9b9b9b);overflow-wrap:anywhere}",
      ".hc-project-parent:empty{display:none}",
      ".hc-project-browse{flex:1 0 100%;cursor:pointer;user-select:none;color:var(--fnt,#9b9b9b);font-size:10.5px}",
      ".hc-project-browse:hover{color:var(--ink,#111)}",
      ".hc-project-newname{flex:1 1 auto;min-width:0;box-sizing:border-box;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11px 'Source Code Pro',monospace;padding:5px 7px}",
      ".hc-project-newrepo{flex:1 0 100%;min-width:0;box-sizing:border-box;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;background:var(--panel2,#f6f6f6);color:var(--mut,#575757);font:10.5px 'Source Code Pro',monospace;padding:5px 7px}",
      // The two questions a project nobody has worked in yet is asked, in
      // the menu that just made it.
      ".hc-project-setup{padding:2px 14px 10px;border-top:1px solid var(--bd,#e3e3e3);margin-top:2px}",
      ".hc-project-setup-head{font:600 12px 'Source Code Pro',monospace;color:var(--ink,#111);padding:9px 0 3px}",
      ".hc-project-setup-note{font-size:10px;color:var(--fnt,#9b9b9b);padding-bottom:8px;line-height:1.5}",
      ".hc-project-setup-field{display:block;width:100%;box-sizing:border-box;margin-bottom:7px;resize:vertical;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11px/1.5 'Source Code Pro',monospace;padding:5px 7px}",
      ".hc-project-setup-field::placeholder{color:var(--fnt,#9b9b9b)}",
      ".hc-project-setup-row{display:flex;align-items:center;gap:12px}",
      ".hc-project-setup-later{cursor:pointer;user-select:none;color:var(--fnt,#9b9b9b);font-size:10.5px}",
      ".hc-project-setup-later:hover{color:var(--ink,#111)}",
      ".hc-project-addbtn{flex:none;cursor:pointer;user-select:none;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;color:var(--mut,#575757);font:600 10px 'Source Code Pro',monospace;letter-spacing:1px;text-transform:uppercase;padding:5px 9px}",
      ".hc-project-addbtn:hover{color:var(--ink,#111);border-color:var(--ink,#111)}",
      ".hc-project-say{padding:0 14px 8px;font-size:10px;color:var(--fnt,#9b9b9b);overflow-wrap:anywhere}",
      ".hc-project-say:empty{display:none}",
      ".hc-project-say[data-hc-bad]{color:var(--bad,#a12d2d)}",
      // The overview: the whole window under the header. It is the
      // project's screen, not a pane of the goal's -- both rails are
      // covered, and GOALS (or Esc) brings the three columns back.
      ".hc-overview{display:none;position:fixed;top:var(--hc-top,37px);left:0;right:0;bottom:0;z-index:18;overflow-y:auto;background:var(--bg,#fff);color:var(--ink,#111);font:12px/1.6 'Source Code Pro',ui-monospace,monospace;padding:0 24px 24px;box-sizing:border-box}",
      "[data-hc-overview] .hc-overview{display:block}",
      ".hc-overview-tabs{display:flex;gap:22px;align-items:baseline;padding:12px 0 0;border-bottom:1px solid var(--bd,#e3e3e3);margin:0 -24px 16px;padding-left:24px;padding-right:24px}",
      ".hc-overview-tab{font:600 10px 'Source Code Pro',monospace;letter-spacing:1.4px;color:var(--fnt,#9b9b9b);cursor:pointer;user-select:none;padding:0 0 9px;border-bottom:2px solid transparent;margin-bottom:-1px}",
      ".hc-overview-tab:hover{color:var(--ink,#111)}",
      ".hc-overview-tab-on{color:var(--acc,#a5492a);border-bottom-color:var(--acc,#a5492a)}",
      ".hc-overview-card{border:1px solid var(--bd,#e3e3e3);border-radius:10px;background:var(--panel,#fff);padding:20px 24px;margin-bottom:18px}",
      ".hc-overview-head{display:flex;justify-content:space-between;align-items:flex-start;gap:14px}",
      ".hc-overview-ctxhead{display:flex;justify-content:space-between;align-items:baseline;gap:14px;margin:0 2px 10px}",
      ".hc-overview-refreshed{font:10.5px 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-about{display:block;width:100%;box-sizing:border-box;margin-top:6px;min-height:0;overflow:hidden;resize:none;border:0;outline:none;background:transparent;font:12.5px/1.7 'Source Code Pro',monospace;color:var(--mut,#575757);caret-color:var(--ink,#111)}",
      ".hc-overview-about::placeholder{color:var(--fnt,#9b9b9b)}",
      ".hc-overview-sec{margin-top:18px;border-top:1px solid var(--bd,#e3e3e3);padding-top:14px}",
      ".hc-overview-facts{margin-top:14px;display:flex;flex-direction:column;gap:7px}",
      ".hc-overview-fact{display:flex;gap:14px;font-size:13px}",
      ".hc-overview-fact-k{flex:none;width:108px;color:var(--fnt,#9b9b9b);letter-spacing:1px;font-size:10.5px;padding-top:3px}",
      ".hc-overview-fact-v{color:var(--mut,#575757);overflow-wrap:anywhere;font-size:12px}",
      "a.hc-overview-fact-v{color:var(--acc,#a5492a)}",
      ".hc-overview-name{display:block;width:100%;box-sizing:border-box;border:0;outline:none;background:transparent;padding:0;font:800 16px 'Source Code Pro',monospace;letter-spacing:.2px;color:var(--ink,#111);caret-color:var(--ink,#111)}",
      ".hc-overview-name::placeholder{color:var(--fnt,#9b9b9b);font-weight:600}",
      ".hc-overview-label{font:600 11.5px 'Source Code Pro',monospace;letter-spacing:2px;color:var(--mut,#575757);text-transform:uppercase}",
      ".hc-overview-label small{font-weight:400;letter-spacing:.2px;text-transform:none;color:var(--fnt,#9b9b9b);margin-left:6px}",
      ".hc-overview-objective{display:block;width:100%;box-sizing:border-box;margin-top:6px;min-height:0;overflow:hidden;font-size:15.5px!important;font-weight:600;resize:none;border:0;outline:none;background:transparent;color:var(--ink,#111);font:12.5px/1.6 'Source Code Pro',monospace;caret-color:var(--ink,#111)}",
      ".hc-overview-objective::placeholder{color:var(--fnt,#9b9b9b)}",
      ".hc-overview-context{display:flex;box-sizing:border-box;max-width:100%;border:1px solid var(--bd,#e3e3e3);border-radius:10px;background:var(--panel,#fff);min-height:320px}",
      ".hc-overview-srcs{flex:0 0 220px;border-right:1px solid var(--bd,#e3e3e3);padding:14px 12px}",
      ".hc-overview-src{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:6px;cursor:pointer;user-select:none;margin-bottom:2px}",
      ".hc-overview-src:hover{background:var(--hov,#f2f2f2)}",
      ".hc-overview-src[data-hc-on]{background:var(--accbg,#f5e2d9)}",
      ".hc-overview-src[data-hc-on]:hover{background:var(--accbg,#f5e2d9)}",
      ".hc-overview-src-text{flex:1 1 auto;min-width:0}",
      ".hc-overview-src-x{flex:none;color:var(--fnt,#9b9b9b);opacity:0;padding:0 2px}",
      ".hc-overview-src:hover .hc-overview-src-x{opacity:1}",
      ".hc-overview-src-x:hover{color:var(--bad,#a12d2d)}",
      ".hc-overview-addform{display:none;flex-direction:column;gap:8px;padding:8px 10px 4px}",
      ".hc-overview-addform[data-hc-on]{display:flex}",
      // The picker is a dialog over the page. It used to unfold inside the
      // 220px source column, which is not where anybody picks a
      // conversation out of sixty.
      ".hc-overview-modal{display:none;position:fixed;top:0;left:0;right:0;bottom:0;z-index:24;background:rgba(0,0,0,.45);align-items:flex-start;justify-content:center;padding:9vh 20px 20px;box-sizing:border-box}",
      ".hc-overview-modal[data-hc-on]{display:flex}",
      ".hc-overview-modal-card{width:100%;max-width:520px;box-sizing:border-box;border:1px solid var(--bd2,#d5d5d5);border-radius:10px;background:var(--panel,#fff);box-shadow:0 18px 48px rgba(0,0,0,.28);padding:18px 20px 20px;max-height:78vh;overflow-y:auto}",
      ".hc-overview-modal-head{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:14px}",
      ".hc-overview-modal-x{cursor:pointer;user-select:none;color:var(--fnt,#9b9b9b);font-size:16px;line-height:1;padding:0 2px}",
      ".hc-overview-modal-x:hover{color:var(--ink,#111)}",
      ".hc-overview-modal .hc-overview-addform{padding:0;gap:12px}",
      ".hc-overview-modal .hc-overview-srcchats{max-height:44vh}",
      ".hc-overview-kinds{display:flex;gap:6px;flex-wrap:wrap}",
      ".hc-overview-kind{cursor:pointer;user-select:none;border:1px solid var(--bd2,#d5d5d5);border-radius:999px;padding:3px 9px;font-size:10px;color:var(--mut,#575757)}",
      ".hc-overview-kind:hover{color:var(--ink,#111);border-color:var(--ink,#111)}",
      ".hc-overview-kind-on{background:var(--acc,#a5492a);border-color:var(--acc,#a5492a);color:var(--onacc,#fff)}",
      ".hc-overview-srcpath{box-sizing:border-box;width:100%;border:1px solid var(--bd2,#d5d5d5);border-radius:3px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11px 'Source Code Pro',monospace;padding:5px 7px}",
      ".hc-overview-srcchats{display:none;flex-direction:column;max-height:230px;overflow-y:auto;border:1px solid var(--bd,#e3e3e3);border-radius:6px}",
      ".hc-overview-srcchats[data-hc-on]{display:flex}",
      ".hc-overview-srcchat{display:flex;gap:9px;align-items:baseline;cursor:pointer;user-select:none;padding:6px 9px;font-size:10.5px;color:var(--mut,#575757)}",
      ".hc-overview-srcchat+.hc-overview-srcchat{border-top:1px solid var(--bd,#e3e3e3)}",
      ".hc-overview-srcchat:hover{background:var(--hov,#f2f2f2);color:var(--ink,#111)}",
      ".hc-overview-srcchat[data-hc-had]{cursor:default;opacity:.55}",
      ".hc-overview-srcchat[data-hc-had]:hover{background:transparent}",
      ".hc-overview-srcchat-id{flex:none;font-weight:600;color:var(--ink,#111)}",
      ".hc-overview-srcchat-where{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-srcchat-where[data-hc-here]{color:var(--acc,#a5492a)}",
      ".hc-overview-srcchat-when{flex:none;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-srcbtn[data-hc-off]{display:none}",
      ".hc-overview-srcrow{display:flex;gap:9px;align-items:center}",
      ".hc-overview-srcbtn{flex:none;cursor:pointer;user-select:none;border:1px solid var(--bd2,#d5d5d5);border-radius:3px;color:var(--mut,#575757);font:600 10px 'Source Code Pro',monospace;letter-spacing:1px;text-transform:uppercase;padding:4px 9px}",
      ".hc-overview-srcbtn:hover{color:var(--ink,#111);border-color:var(--ink,#111)}",
      ".hc-overview-srcsay{font-size:10px;color:var(--fnt,#9b9b9b);overflow-wrap:anywhere}",
      ".hc-overview-srcsay[data-hc-bad]{color:var(--bad,#a12d2d)}",
      ".hc-overview-reading{display:none;flex:1 1 auto;min-width:0;padding:16px 20px 20px}",
      ".hc-overview-reading[data-hc-on]{display:block}",
      ".hc-overview-repo[data-hc-off]{display:none}",
      ".hc-overview-srcbody{max-height:60vh;overflow-y:auto}",
      ".hc-overview-turn{display:flex;gap:12px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--bd,#e3e3e3)}",
      ".hc-overview-turn-who{flex:none;width:64px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-turn[data-hc-role=\"user\"] .hc-overview-turn-who{color:var(--acc,#a5492a)}",
      ".hc-overview-turn-text{min-width:0;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--dtxt,#333)}",
      ".hc-overview-addsrc{margin-top:8px;padding:7px 10px;cursor:pointer;user-select:none;font:12px 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-addsrc:hover{color:var(--ink,#111)}",
      ".hc-overview-src-glyph{flex:none;width:18px;height:18px;border-radius:3px;background:var(--acc,#a5492a);color:var(--onacc,#fff);font:600 9px/18px 'Source Code Pro',monospace;text-align:center}",
      ".hc-overview-src-name{font-weight:600;color:var(--ink,#111);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-overview-src-kind{font-size:9.5px;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-repo{flex:1 1 auto;min-width:0;padding:16px 20px 20px}",
      ".hc-overview-repo-name{font:600 13px 'Source Code Pro',monospace;color:var(--ink,#111)}",
      ".hc-overview-repo-meta{font-size:10.5px;color:var(--mut,#575757);margin-top:2px;overflow-wrap:anywhere}",
      ".hc-overview-repo-meta a{color:inherit}",
      ".hc-overview-panes{display:flex;gap:18px;border-bottom:1px solid var(--bd,#e3e3e3);margin:14px 0 14px}",
      ".hc-overview-pane-tab{font:600 10px 'Source Code Pro',monospace;letter-spacing:.6px;color:var(--fnt,#9b9b9b);cursor:pointer;user-select:none;padding:0 0 7px;border-bottom:2px solid transparent;margin-bottom:-1px}",
      ".hc-overview-pane-tab:hover{color:var(--ink,#111)}",
      ".hc-overview-pane-tab-on{color:var(--acc,#a5492a);border-bottom-color:var(--acc,#a5492a)}",
      ".hc-overview-panebody{min-width:0}",
      ".hc-overview-ask{display:flex;flex-direction:column;gap:9px}",
      ".hc-overview-askfield{box-sizing:border-box;width:100%;resize:vertical;border:1px solid var(--bd2,#d5d5d5);border-radius:6px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:12px/1.6 'Source Code Pro',monospace;padding:8px 10px}",
      ".hc-overview-askout:empty{display:none}",
      ".hc-overview-askout{border-top:1px solid var(--bd,#e3e3e3);padding-top:12px}",
      ".hc-md{font:12.5px/1.7 'Source Code Pro',monospace;color:var(--dtxt,#333);max-height:60vh;overflow-y:auto;overflow-wrap:anywhere}",
      ".hc-md-h{font-weight:700;color:var(--ink,#111);margin:18px 0 8px;line-height:1.35}",
      ".hc-md-h:first-child{margin-top:0}",
      ".hc-md-h1{font-size:19px;letter-spacing:-.2px}",
      ".hc-md-h2{font-size:16px;padding-bottom:6px;border-bottom:1px solid var(--bd,#e3e3e3)}",
      ".hc-md-h3{font-size:14px}",
      ".hc-md-h4,.hc-md-h5,.hc-md-h6{font-size:12.5px;color:var(--mut,#575757);letter-spacing:.6px;text-transform:uppercase}",
      ".hc-md-p{margin:0 0 10px}",
      ".hc-md-rule{height:1px;background:var(--bd,#e3e3e3);margin:16px 0}",
      ".hc-md-quote{border-left:2px solid var(--bd2,#d5d5d5);padding-left:11px;color:var(--mut,#575757);margin:0 0 10px}",
      ".hc-md-list{margin:0 0 10px}",
      ".hc-md-item{display:flex;gap:9px;align-items:baseline}",
      ".hc-md-bullet{flex:none;color:var(--fnt,#9b9b9b);min-width:14px}",
      ".hc-md-itemtext{min-width:0}",
      ".hc-md-row{margin:0 0 4px;color:var(--mut,#575757)}",
      ".hc-md-pre{margin:0 0 12px;padding:11px 13px;border:1px solid var(--bd,#e3e3e3);border-radius:6px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11.5px/1.6 'Source Code Pro',monospace;white-space:pre;overflow-x:auto}",
      ".hc-md-code{padding:1px 5px;border-radius:3px;background:var(--panel2,#f6f6f6);color:var(--acc,#a5492a);font-size:11.5px}",
      ".hc-md-link{color:var(--acc,#a5492a)}",
      ".hc-md-img{color:var(--fnt,#9b9b9b);font-style:italic}",
      ".hc-overview-sync{display:flex;align-items:center;gap:12px;margin-top:14px;padding-top:12px;border-top:1px solid var(--bd,#e3e3e3)}",
      ".hc-overview-sync-btn{flex:none;cursor:pointer;user-select:none;border:1px solid var(--acc,#a5492a);border-radius:3px;background:var(--acc,#a5492a);color:var(--onacc,#fff);font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;text-transform:uppercase;padding:7px 13px}",
      ".hc-overview-sync-btn[data-hc-busy]{opacity:.55;cursor:default}",
      ".hc-overview-sync-btn[data-hc-off]{background:transparent;color:var(--fnt,#9b9b9b);border-color:var(--bd2,#d5d5d5);cursor:default}",
      ".hc-overview-sync-say{font:11px/1.5 'Source Code Pro',monospace;color:var(--mut,#575757);overflow-wrap:anywhere}",
      ".hc-overview-sync-bad{color:var(--bad,#a12d2d)}",
      ".hc-overview-share-btn{flex:none;cursor:pointer;user-select:none;border:1px solid var(--bd2,#d5d5d5);border-radius:3px;background:transparent;color:var(--mut,#575757);font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;text-transform:uppercase;padding:7px 13px}",
      ".hc-overview-share-btn:hover{color:var(--ink,#111);border-color:var(--ink,#111)}",
      ".hc-overview-share-btn[data-hc-busy],.hc-overview-share-btn[data-hc-off]{opacity:.55;cursor:default}",
      ".hc-overview-share-role{flex:none;cursor:pointer;user-select:none;font:600 10px 'Source Code Pro',monospace;letter-spacing:1px;text-transform:uppercase;color:var(--mut,#575757);border-bottom:1px dashed var(--bd2,#d5d5d5);padding-bottom:1px}",
      ".hc-overview-share-role:hover{color:var(--ink,#111)}",
      ".hc-overview-code{margin-top:12px;display:none;flex-direction:column;gap:7px}",
      ".hc-overview-code[data-hc-on]{display:flex}",
      ".hc-overview-code-box{display:block;width:100%;box-sizing:border-box;resize:none;border:1px solid var(--bd2,#d5d5d5);border-radius:3px;background:var(--panel2,#f6f6f6);color:var(--ink,#111);font:11px/1.5 'Source Code Pro',monospace;padding:8px 10px;word-break:break-all}",
      ".hc-overview-code-row{display:flex;gap:10px;align-items:center}",
      ".hc-overview-code-warn{font-size:10.5px;color:var(--mut,#575757)}",
      ".hc-overview-shares{margin-top:10px;display:flex;flex-direction:column;gap:4px}",
      ".hc-overview-share-row{display:flex;gap:9px;align-items:baseline;font-size:10.5px;color:var(--mut,#575757)}",
      ".hc-overview-share-x{cursor:pointer;user-select:none;color:var(--fnt,#9b9b9b)}",
      ".hc-overview-share-x:hover{color:var(--bad,#a12d2d)}",
      ".hc-overview-empty{color:var(--fnt,#9b9b9b)}",
      ".hc-overview-more{color:var(--fnt,#9b9b9b);font-size:10.5px;margin-top:6px}"
  ].join("\n");

  var projectBound = false;
  var projectMenuBox = null;
  var overviewBox = null;
  var projectJsonPending = false;

  function ensureProjectStyles() {
    if (document.getElementById("hc-project-style")) return;
    var style = document.createElement("style");
    style.id = "hc-project-style";
    style.textContent = PROJECT_CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  // The palette (--bg, --ink, ...) is declared on the artifact's .hc root,
  // and the menu and overview are parented on <body>, outside it, where
  // those names resolve to nothing. Copy the live values across, so both
  // read exactly as the page does in either theme, and again whenever the
  // theme moves.
  var THEME_VARS = ["--bg", "--panel", "--panel2", "--ink", "--mut", "--fnt",
                    "--bd", "--bd2", "--line", "--hov", "--acc", "--accbg",
                    "--onacc", "--del", "--dtxt"];

  function projectTheme() {
    var app = document.querySelector(".hc");
    if (!app || typeof getComputedStyle !== "function") return null;
    var computed;
    try { computed = getComputedStyle(app); } catch (error) { return null; }
    if (!computed || typeof computed.getPropertyValue !== "function") return null;
    var out = {};
    THEME_VARS.forEach(function (name) {
      var value = String(computed.getPropertyValue(name) || "").trim();
      if (value) out[name] = value;
    });
    return out;
  }

  function syncProjectTheme(node) {
    if (!node || !node.style || typeof node.style.setProperty !== "function") return false;
    var theme = projectTheme();
    if (!theme) return false;
    var changed = false;
    Object.keys(theme).forEach(function (name) {
      if (node.style.getPropertyValue(name) !== theme[name]) {
        node.style.setProperty(name, theme[name]);
        changed = true;
      }
    });
    return changed;
  }

  function projectInfo() {
    var who = serverState.project;
    if (!who || typeof who !== "object" || !who.name) return null;
    return who;
  }

  function fetchJSON(path) {
    if (typeof fetch !== "function") return Promise.resolve(null);
    try {
      return fetch(path, { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .catch(function () { return null; });
    } catch (error) {
      return Promise.resolve(null);
    }
  }

  // A node's children as an array: a browser hands back an HTMLCollection,
  // which Array.isArray refuses, so array() would read an element as empty.
  function kids(node) {
    return node && node.children ? Array.prototype.slice.call(node.children) : [];
  }

  function wipe(node) {
    if (!node) return;
    while (node.children && node.children.length) {
      node.removeChild(node.children[node.children.length - 1]);
    }
    if (!node.children || !node.children.length) node.textContent = "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // The chip after the brand. Built once into the slot the header patch
  // leaves; only the name changes after that. The click is handled at the
  // document: the artifact re-materializes the header on render, which
  // keeps attributes and drops listeners.
  function renderProjectChip() {
    if (serverState.scope !== "chat") return false;
    var slot = document.querySelector(".hc-project");
    if (!slot) return false;
    ensureProjectStyles();
    bindProject();
    var who = projectInfo();
    var name = slot.querySelector(".hc-project-name");
    if (!who) {
      if (name) { wipe(slot); return true; }
      return false;
    }
    if (!name) {
      slot.appendChild(el("span", "hc-project-sep", "/"));
      name = el("span", "hc-project-name");
      name.setAttribute("role", "button");
      name.setAttribute("aria-label", "Project menu");
      name.title = "This chat's project: " + who.cwd;
      name.appendChild(el("span", "hc-project-name-text", who.name));
      name.appendChild(el("span", "hc-project-caret", "▾"));
      slot.appendChild(name);
      return true;
    }
    var text = name.querySelector(".hc-project-name-text");
    if (text && text.textContent !== who.name) {
      text.textContent = who.name;
      name.title = "This chat's project: " + who.cwd;
      return true;
    }
    return false;
  }

  function projectMenuShown() {
    return !!(projectMenuBox && inLiveDocument(projectMenuBox));
  }

  function closeProjectMenu() {
    if (!projectMenuShown()) return false;
    if (projectMenuBox.parentNode) projectMenuBox.parentNode.removeChild(projectMenuBox);
    projectMenuBox = null;
    var name = document.querySelector(".hc-project-name");
    if (name && name.removeAttribute) name.removeAttribute("data-hc-project-open");
    return true;
  }

  function placeProjectMenu() {
    var root = document.documentElement;
    var name = document.querySelector(".hc-project-name");
    if (!root || !root.style || typeof root.style.setProperty !== "function"
        || !name || !name.getBoundingClientRect) return;
    var box = name.getBoundingClientRect();
    if (!box || typeof box.left !== "number") return;
    root.style.setProperty("--hc-project-left",
                           Math.max(8, Math.round(box.left)) + "px");
    // And from its underside. --hc-top is the bottom of every fixed bar
    // there is; hanging the menu off that dropped it a whole tab row and a
    // pill row below the name that opened it.
    var under = Math.round(Number(box.bottom) || 0);
    if (under > 0) root.style.setProperty("--hc-project-top", (under + 5) + "px");
  }

  function relativeAge(epochSeconds) {
    if (typeof epochSeconds !== "number" || !isFinite(epochSeconds)) return "";
    var ago = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (ago < 90) return "just now";
    if (ago < 3600) return Math.round(ago / 60) + "m ago";
    if (ago < 86400) return Math.round(ago / 3600) + "h ago";
    return Math.round(ago / 86400) + "d ago";
  }

  function projectFact(facts, key, value, href) {
    var row = el("div", "hc-project-fact");
    row.appendChild(el("span", "hc-project-fact-k", key));
    var v = el("span", "hc-project-fact-v");
    if (value && href) {
      var a = el("a", "", value);
      a.setAttribute("href", href);
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener");
      v.appendChild(a);
    } else {
      v.textContent = value || "—";
    }
    row.appendChild(v);
    facts.appendChild(row);
  }

  // The origin as something a browser can open, when it is a GitHub-style
  // remote; otherwise no link at all -- a made-up URL is worse than none.
  function remoteHref(remote) {
    var text = str(remote).trim();
    if (!text) return "";
    var ssh = text.match(/^git@([^:]+):(.+?)(?:\.git)?$/);
    if (ssh) return "https://" + ssh[1] + "/" + ssh[2];
    if (/^https?:\/\//.test(text)) return text.replace(/\.git$/, "");
    return "";
  }

  // The chats of this project: every discovered chat whose manifest names
  // the same directory, this one first. Each other one carries a link
  // toggle -- a workspace-wide link, the same op the header's "+ chats"
  // makes -- so the project's own conversations are one click from being
  // prompt sources here.
  function projectChatsOf(data, who) {
    var linked = {};
    array(data && data.linked).forEach(function (row) {
      if (row && row.session_id && !row.goal_id) linked[row.session_id] = true;
    });
    var rows = array(data && data.available).filter(function (row) {
      return row && typeof row.session_id === "string"
        && str(row.cwd) && str(row.cwd) === str(who.cwd);
    }).map(function (row) {
      return { session_id: row.session_id, mtime: row.mtime,
               linked: !!linked[row.session_id] };
    });
    return rows;
  }

  // The name in the header opens the switcher: every project this machine
  // has worked in, the one being looked at marked, and a way to name a
  // directory that has not been worked in yet. The chats of this project
  // keep their own section below -- linking one as a prompt source is a
  // different job from moving between projects.
  function projectMenuNode(who) {
    var box = el("div", "hc-project-menu");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Projects");
    var list = el("div", "hc-project-list");
    list.setAttribute("data-hc-project-list", "");
    // The current one is drawn before the fetch answers, so the menu is
    // never momentarily empty under the name that opened it.
    list.appendChild(projectRow({ name: who.name, cwd: who.cwd }, true));
    box.appendChild(list);
    var add = el("div", "hc-project-new", "+ New project");
    add.setAttribute("role", "button");
    add.setAttribute("data-hc-project-new", "");
    box.appendChild(add);
    var form = el("div", "hc-project-newform");
    form.setAttribute("data-hc-project-newform", "");
    var field = el("input", "hc-project-newname");
    field.setAttribute("type", "text");
    field.setAttribute("data-hc-project-name", "");
    // A name, not a path: a project is somewhere to keep an objective and
    // its goals, and asking for a directory turns making one into an
    // errand. A path still works for a repository already on disk.
    field.setAttribute("placeholder", "Name your project");
    field.setAttribute("spellcheck", "false");
    field.setAttribute("autocomplete", "off");
    form.appendChild(field);
    var addBtn = el("span", "hc-project-addbtn", "Add");
    addBtn.setAttribute("role", "button");
    addBtn.setAttribute("data-hc-project-add", "");
    form.appendChild(addBtn);
    // A repository is a project that already exists somewhere else. Given
    // one here it is cloned into the home the project is given, so the code
    // is there from the first moment rather than after an errand in a
    // terminal. Optional: most projects are named before there is any.
    var repo = el("input", "hc-project-newrepo");
    repo.setAttribute("type", "text");
    repo.setAttribute("data-hc-project-repo", "");
    repo.setAttribute("placeholder", "Git repo to clone (optional)");
    repo.setAttribute("spellcheck", "false");
    repo.setAttribute("autocomplete", "off");
    form.appendChild(repo);
    // A path is something to point at rather than to spell. The machine's
    // own folder chooser opens under this line, and what comes back is
    // dropped in the box above as ordinary text -- still readable, still
    // editable, and nothing is made of it until "Add".
    var browse = el("div", "hc-project-browse", "Choose where it goes…");
    browse.setAttribute("role", "button");
    browse.setAttribute("data-hc-project-browse", "");
    form.appendChild(browse);
    // Where the folder will be made. Empty means the vault, which is where
    // a project goes when nobody has said -- fine for somewhere to keep an
    // objective, wrong for anything anybody will open an editor on.
    var seat = el("div", "hc-project-parent", "");
    seat.setAttribute("data-hc-project-parent", "");
    form.appendChild(seat);
    box.appendChild(form);
    var say = el("div", "hc-project-say", "");
    say.setAttribute("data-hc-project-say", "");
    box.appendChild(say);
    return box;
  }

  function projectRow(row, active) {
    var line = el("div", "hc-project-row");
    line.setAttribute("role", "button");
    line.setAttribute("data-hc-goto", str(row.cwd));
    line.setAttribute("title", str(row.cwd));
    if (active) line.setAttribute("data-hc-active", "");
    line.appendChild(el("span", "hc-project-row-name", str(row.name)));
    // A project nobody has worked in and that says nothing about itself has
    // no workspace to open -- one serves a chat's goals, and it has none.
    // The row asks its two questions instead, so onboarding left for later
    // is still reachable rather than a dead end saying "no chat yet".
    var unset = !active && !Number(row.chats) && !str(row.objective).trim();
    if (unset) line.setAttribute("data-hc-project-setup-row", "");
    var right = active ? "active"
      : (Number(row.chats) > 0
         ? Number(row.chats) + (Number(row.chats) === 1 ? " chat" : " chats")
         : (unset ? "set up" : ""));
    line.appendChild(el("span", "hc-project-row-note", right));
    return line;
  }

  function renderProjects(result) {
    if (!projectMenuBox || !inLiveDocument(projectMenuBox)) return false;
    var list = projectMenuBox.querySelector("[data-hc-project-list]");
    if (!list) return false;
    var rows = array(result && result.projects);
    if (!rows.length) return false;
    var here = str(result && result.active);
    wipe(list);
    // The one being looked at first, whatever the server's order: it is the
    // answer to "where am I", which is why the menu was opened.
    rows.filter(function (r) { return str(r.cwd) === here; })
      .concat(rows.filter(function (r) { return str(r.cwd) !== here; }))
      .forEach(function (r) { list.appendChild(projectRow(r, str(r.cwd) === here)); });
    return true;
  }

  function loadProjects() {
    return fetchJSON("/api/projects").then(function (result) {
      renderProjects(result);
      return result;
    });
  }

  function projectSay(words, bad) {
    if (!projectMenuBox) return false;
    var node = projectMenuBox.querySelector("[data-hc-project-say]");
    if (!node) return false;
    node.textContent = str(words);
    if (bad) node.setAttribute("data-hc-bad", "");
    else node.removeAttribute("data-hc-bad");
    return true;
  }

  // A control the server has never heard of is not a control that failed:
  // this page is re-read from disk on every load and the process answering
  // it is not, so a workspace left open across an edit reaches a server
  // older than itself. Restarting it is the whole fix, and saying that is
  // worth more than repeating the server's own word for the operation.
  var STALE_SAY = "this workspace's server is older than this page — "
    + "restart it and try again";

  // Which words those are depends on how old the server is: builds since the
  // project menu name the operation they did not recognise, and the ones
  // before it called every operation they did not know invalid. Both are the
  // same sentence -- a process that started before this control existed.
  var STALE_ERRORS = ["unknown operation", "unknown or invalid op"];

  function staleSay(result) {
    var says = str(result && result.error);
    for (var i = 0; i < STALE_ERRORS.length; i++) {
      if (says.indexOf(STALE_ERRORS[i]) === 0) {
        // Said twice on purpose: the line answers the button that was just
        // pressed, and the banner outlives the menu, since every other
        // control added by the same edit is about to fail the same way.
        noteStaleServer();
        return STALE_SAY;
      }
    }
    return "";
  }

  // Another project's workspace is another server: this one serves one
  // chat's goals and cannot serve a second directory's. The reply carries
  // the URL it came up on, and the page is opened beside this one.
  function switchProject(cwd) {
    if (!cwd) return false;
    if (str(cwd) === str((projectInfo() || {}).cwd)) {
      closeProjectMenu();
      return true;
    }
    projectSay("opening…");
    post({ op: "open_project", cwd: str(cwd) }).then(function (result) {
      if (!result || !result.ok || !result.url) {
        projectSay((result && result.error) || "could not open it", true);
        return;
      }
      projectSay(result.already ? "already open · " + result.url : result.url);
      try { window.open(result.url, "_blank"); } catch (e) {}
    }).catch(function () { projectSay("could not open it", true); });
    return true;
  }

  // The chooser belongs to the machine, not to the page: the server opens
  // the one this desktop already has -- Finder's, the file manager's -- and
  // answers with the directory that was picked. Closing it is not a failure,
  // so a cancelled choice just clears the line and leaves the box alone.
  function chooseProjectFolder() {
    if (!projectMenuBox) return false;
    var field = projectMenuBox.querySelector("[data-hc-project-name]");
    projectSay("choosing…");
    post({ op: "pick_directory", start: str((projectInfo() || {}).cwd) })
      .then(function (result) {
        if (!result || !result.ok) {
          projectSay(staleSay(result)
                     || (result && result.error)
                     || "could not open the chooser", true);
          return;
        }
        if (result.cancelled) { projectSay(""); return; }
        var seat = projectMenuBox
          && projectMenuBox.querySelector("[data-hc-project-parent]");
        if (seat) {
          seat.setAttribute("data-hc-cwd", str(result.cwd));
          seat.textContent = "in " + str(result.cwd);
        }
        if (field && field.focus) field.focus();
        projectSay("");
      })
      .catch(function () { projectSay("could not open the chooser", true); });
    return true;
  }

  // What the reader typed is a repository when it is written as one. Only
  // for the word the line reports while it waits -- cloning takes long
  // enough that "adding…" would read as a hang. The server decides.
  var REPO_TEXT = /^(?:https?:\/\/|ssh:\/\/|git:\/\/|[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:)/;

  // The two questions a project nobody has worked in yet is asked: what it
  // is for, and what is worth knowing before anything starts. A project made
  // from this menu has no chat behind it and so no workspace of its own to
  // open, so they are asked here, at the moment it is made, and written to
  // its record. Answering is not compulsory -- "Later" leaves the project
  // made and the questions unanswered.
  function projectSetupNode(who) {
    var box = el("div", "hc-project-setup");
    box.setAttribute("data-hc-project-setup", str(who.cwd));
    box.appendChild(el("div", "hc-project-setup-head",
                       "Set up " + str(who.name)));
    box.appendChild(el("div", "hc-project-setup-note",
                       "Nothing has been worked on here yet. Say what it is "
                       + "for and every goal is read against that."));
    var objective = el("textarea", "hc-project-setup-field");
    objective.setAttribute("data-hc-project-objective", "");
    objective.setAttribute("placeholder", "What are you trying to accomplish?");
    objective.setAttribute("rows", "2");
    objective.setAttribute("spellcheck", "false");
    box.appendChild(objective);
    var about = el("textarea", "hc-project-setup-field");
    about.setAttribute("data-hc-project-about", "");
    about.setAttribute("placeholder", "Context: what should be known first?");
    about.setAttribute("rows", "3");
    about.setAttribute("spellcheck", "false");
    box.appendChild(about);
    var row = el("div", "hc-project-setup-row");
    var save = el("span", "hc-project-addbtn", "Save");
    save.setAttribute("role", "button");
    save.setAttribute("data-hc-project-setup-save", "");
    row.appendChild(save);
    var skip = el("span", "hc-project-setup-later", "Later");
    skip.setAttribute("role", "button");
    skip.setAttribute("data-hc-project-setup-skip", "");
    row.appendChild(skip);
    box.appendChild(row);
    return box;
  }

  function projectSetupBox() {
    return projectMenuBox
      && projectMenuBox.querySelector("[data-hc-project-setup]");
  }

  function askProjectSetup(who) {
    if (!projectMenuBox || !who || !who.cwd) return false;
    closeProjectSetup();
    // The box that made it has done its job; the questions take its place
    // rather than sitting under a form still asking for another name.
    var form = projectMenuBox.querySelector("[data-hc-project-newform]");
    if (form) form.removeAttribute("data-hc-on");
    var box = projectSetupNode(who);
    var say = projectMenuBox.querySelector("[data-hc-project-say]");
    if (say && say.parentNode === projectMenuBox) {
      projectMenuBox.insertBefore(box, say);
    } else {
      projectMenuBox.appendChild(box);
    }
    var field = box.querySelector("[data-hc-project-objective]");
    if (field && field.focus) field.focus();
    return true;
  }

  function closeProjectSetup() {
    var box = projectSetupBox();
    if (box && box.parentNode) box.parentNode.removeChild(box);
    return !!box;
  }

  function saveProjectSetup() {
    var box = projectSetupBox();
    if (!box) return false;
    var objective = box.querySelector("[data-hc-project-objective]");
    var about = box.querySelector("[data-hc-project-about]");
    projectSay("saving…");
    post({ op: "project_setup",
           cwd: str(box.getAttribute("data-hc-project-setup")),
           objective: objective ? str(objective.value) : "",
           description: about ? str(about.value) : "" })
      .then(function (result) {
        if (!result || !result.ok) {
          projectSay(staleSay(result) || (result && result.error)
                     || "could not save that", true);
          return;
        }
        closeProjectSetup();
        projectSay("saved");
        loadProjects();
      })
      .catch(function () { projectSay("could not save that", true); });
    return true;
  }

  function addProject() {
    if (!projectMenuBox) return false;
    var field = projectMenuBox.querySelector("[data-hc-project-name]");
    var repoField = projectMenuBox.querySelector("[data-hc-project-repo]");
    var typed = field ? str(field.value).trim() : "";
    var repo = repoField ? str(repoField.value).trim() : "";
    if (!typed && !repo) { projectSay("type a name first", true); return false; }
    var cloning = !!repo || REPO_TEXT.test(typed);
    projectSay(cloning ? "cloning…" : "adding…");
    var seat = projectMenuBox.querySelector("[data-hc-project-parent]");
    var parent = seat ? str(seat.getAttribute("data-hc-cwd")) : "";
    post({ op: "new_project", name: typed, repo: repo,
           parent: parent }).then(function (result) {
      if (!result || !result.ok) {
        projectSay(staleSay(result) || (result && result.error)
                   || "could not add it", true);
        // A name somebody already used is not a failure to report and leave:
        // the fix is another name, so what was typed stays in the box and
        // the cursor goes back to it, selected, ready to be typed over.
        if (result && result.duplicate && field) {
          if (field.focus) field.focus();
          if (field.select) field.select();
        }
        return;
      }
      if (field) field.value = "";
      if (repoField) repoField.value = "";
      if (seat) { seat.removeAttribute("data-hc-cwd"); seat.textContent = ""; }
      projectSay((result.cloned ? "cloned " : "added ") + str(result.name));
      loadProjects();
      // A project with nothing in it is asked what it is for, here, while
      // the reader is still looking at the thing they just made.
      if (result.setup) askProjectSetup(result);
    }).catch(function () { projectSay("could not add it", true); });
    return true;
  }

  function openProjectMenu() {
    var who = projectInfo();
    if (!who) return false;
    ensureProjectStyles();
    closeProjectMenu();
    projectMenuBox = projectMenuNode(who);
    syncProjectTheme(projectMenuBox);
    placeProjectMenu();
    (document.body || document.documentElement).appendChild(projectMenuBox);
    var name = document.querySelector(".hc-project-name");
    if (name) name.setAttribute("data-hc-project-open", "");
    loadProjects();
    return true;
  }

  function toggleProjectMenu() {
    return projectMenuShown() ? !closeProjectMenu() : openProjectMenu();
  }

  // --- the overview screen -------------------------------------------------

  function overviewShown() {
    var root = document.documentElement;
    return !!(root && root.getAttribute && root.getAttribute("data-hc-overview") !== null);
  }

  // --- the project's sources ----------------------------------------------
  //
  // The repository is always the first one and is not stored: it is where
  // the chat was started, which no reader has to attach. Everything after
  // it was attached by hand and lives in the project's own record.

  var SOURCE_KINDS = {
    github: { glyph: "R", kind: "Repository" },
    chat: { glyph: "C", kind: "Conversation" },
    doc: { glyph: "D", kind: "Document" },
    local: { glyph: "D", kind: "Document" }
  };

  // The same kinds again, as the one word a chip has room for. "repo" is
  // the project's own directory, which is context without being attached.
  var SOURCE_TAGS = {
    github: "GITHUB", chat: "CHAT", doc: "DOC", local: "LOCAL", repo: "REPO"
  };

  // Which source the right-hand side is showing. "" is the repository.
  var overviewSource = "";

  function sourceRows(who) {
    return array(who && who.sources).filter(function (row) {
      return row && str(row.id) && str(row.label);
    });
  }

  function sourceName(row) {
    var label = str(row.label);
    if (str(row.type) === "chat") {
      return "Claude session: " + label.slice(0, 8);
    }
    if (str(row.type) === "github") return label;
    // A path reads better as its last segment, with the rest in the title.
    var cut = label.replace(/\/+$/, "").split("/");
    return cut[cut.length - 1] || label;
  }

  function renderSourceList(host, who) {
    wipe(host);
    var mine = el("div", "hc-overview-src");
    mine.setAttribute("data-hc-source", "");
    mine.setAttribute("role", "button");
    if (!overviewSource) mine.setAttribute("data-hc-on", "");
    mine.appendChild(el("span", "hc-overview-src-glyph", "R"));
    var mineText = el("div", "hc-overview-src-text");
    mineText.appendChild(el("div", "hc-overview-src-name", who.name));
    mineText.appendChild(el("div", "hc-overview-src-kind", "Repository"));
    mine.appendChild(mineText);
    host.appendChild(mine);
    sourceRows(who).forEach(function (row) {
      var spec = SOURCE_KINDS[str(row.type)] || SOURCE_KINDS.doc;
      var line = el("div", "hc-overview-src");
      line.setAttribute("data-hc-source", str(row.id));
      line.setAttribute("role", "button");
      line.setAttribute("title", str(row.label));
      if (overviewSource === str(row.id)) line.setAttribute("data-hc-on", "");
      line.appendChild(el("span", "hc-overview-src-glyph", spec.glyph));
      var text = el("div", "hc-overview-src-text");
      text.appendChild(el("div", "hc-overview-src-name", sourceName(row)));
      text.appendChild(el("div", "hc-overview-src-kind", spec.kind));
      line.appendChild(text);
      var drop = el("span", "hc-overview-src-x", "×");
      drop.setAttribute("role", "button");
      drop.setAttribute("title", "Remove this source");
      drop.setAttribute("data-hc-drop-source", str(row.id));
      line.appendChild(drop);
      host.appendChild(line);
    });
    // The foot of the list: what a project carries beyond its own
    // directory is attached by hand, so there has to be a way in. The form
    // itself is not here -- it opens as a dialog over the page, because a
    // 220px column is not where anybody picks a conversation out of sixty.
    var add = el("div", "hc-overview-addsrc", "+ Add context");
    add.setAttribute("role", "button");
    add.setAttribute("data-hc-add-source", "");
    host.appendChild(add);
    return host;
  }

  // The picker, over the page rather than inside the list it adds to: the
  // conversation list is long, and the column it used to unfold in pushed
  // everything below it off the screen. Built once with the overview and
  // hidden, so the source list can be redrawn without taking it away
  // mid-choice.
  function addSourceModal() {
    var back = el("div", "hc-overview-modal");
    back.setAttribute("data-hc-addmodal", "");
    var card = el("div", "hc-overview-modal-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-label", "Add context");
    card.setAttribute("data-hc-modal-card", "");
    var head = el("div", "hc-overview-modal-head");
    head.appendChild(el("div", "hc-overview-label", "add context"));
    var shut = el("span", "hc-overview-modal-x", "×");
    shut.setAttribute("role", "button");
    shut.setAttribute("title", "Close");
    shut.setAttribute("data-hc-addclose", "");
    head.appendChild(shut);
    card.appendChild(head);
    card.appendChild(addSourceForm());
    back.appendChild(card);
    return back;
  }

  function addSourceShown() {
    var back = overviewBox && overviewBox.querySelector("[data-hc-addmodal]");
    return !!(back && back.getAttribute("data-hc-on") !== null);
  }

  // The dialog and the form it holds carry the same marker: the form's is
  // what every control inside it already reads, and the backdrop's is what
  // draws it.
  function openAddSource() {
    if (!overviewBox) return false;
    var back = overviewBox.querySelector("[data-hc-addmodal]");
    var form = overviewBox.querySelector("[data-hc-addform]");
    if (!back || !form) return false;
    back.setAttribute("data-hc-on", "");
    form.setAttribute("data-hc-on", "");
    setSourceKind(str(form.getAttribute("data-hc-kind-on")) || "github");
    var field = form.querySelector("[data-hc-src-label]");
    if (field && field.focus) field.focus();
    return true;
  }

  function closeAddSource() {
    if (!overviewBox) return false;
    var back = overviewBox.querySelector("[data-hc-addmodal]");
    var form = overviewBox.querySelector("[data-hc-addform]");
    if (!back || back.getAttribute("data-hc-on") === null) return false;
    back.removeAttribute("data-hc-on");
    if (form) form.removeAttribute("data-hc-on");
    sourceSay("");
    return true;
  }

  function toggleAddSource() {
    return addSourceShown() ? !closeAddSource() : openAddSource();
  }

  // Three kinds, because there are three things a project has context in:
  // a repository, something written down, and a conversation that happened.
  function addSourceForm() {
    var form = el("div", "hc-overview-addform");
    form.setAttribute("data-hc-addform", "");
    var kinds = el("div", "hc-overview-kinds");
    [["github", "Repository"], ["doc", "Document"],
     ["chat", "Conversation"]].forEach(function (spec) {
      var pick = el("span", "hc-overview-kind" + (spec[0] === "github" ? " hc-overview-kind-on" : ""),
                    spec[1]);
      pick.setAttribute("data-hc-kind", spec[0]);
      pick.setAttribute("role", "button");
      kinds.appendChild(pick);
    });
    form.appendChild(kinds);
    var field = el("input", "hc-overview-srcpath");
    field.setAttribute("type", "text");
    field.setAttribute("data-hc-src-label", "");
    field.setAttribute("placeholder", "owner/repo, or a link");
    field.setAttribute("spellcheck", "false");
    field.setAttribute("autocomplete", "off");
    form.appendChild(field);
    // Conversations are picked, not typed: a session id is sixteen bytes of
    // hex and nobody knows one by heart. The field above narrows the list
    // rather than naming the chat.
    var picks = el("div", "hc-overview-srcchats");
    picks.setAttribute("data-hc-src-chats", "");
    form.appendChild(picks);
    var row = el("div", "hc-overview-srcrow");
    var add = el("span", "hc-overview-srcbtn", "Add");
    add.setAttribute("role", "button");
    add.setAttribute("data-hc-src-add", "");
    row.appendChild(add);
    var say = el("span", "hc-overview-srcsay", "");
    say.setAttribute("data-hc-src-say", "");
    row.appendChild(say);
    form.appendChild(row);
    return form;
  }

  var SOURCE_PLACEHOLDERS = {
    github: "owner/repo, or a link",
    doc: "a file in this project, or a link",
    chat: "narrow the list \u2014 an id, a project"
  };

  function setSourceKind(kind) {
    if (!overviewBox) return false;
    var form = overviewBox.querySelector("[data-hc-addform]");
    if (!form) return false;
    kids(form).forEach(function (child) {
      if (String(child.className).indexOf("hc-overview-kinds") < 0) return;
      kids(child).forEach(function (pick) {
        var on = pick.getAttribute("data-hc-kind") === kind;
        pick.setAttribute("class",
          "hc-overview-kind" + (on ? " hc-overview-kind-on" : ""));
      });
    });
    form.setAttribute("data-hc-kind-on", str(kind));
    var field = form.querySelector("[data-hc-src-label]");
    if (field) {
      field.setAttribute("placeholder",
        SOURCE_PLACEHOLDERS[kind] || SOURCE_PLACEHOLDERS.doc);
      field.value = "";
    }
    var picks = form.querySelector("[data-hc-src-chats]");
    if (picks) {
      if (kind === "chat") picks.setAttribute("data-hc-on", "");
      else { picks.removeAttribute("data-hc-on"); wipe(picks); }
    }
    // Add takes a typed label; for a conversation the row is the choice.
    var add = form.querySelector("[data-hc-src-add]");
    if (add) {
      if (kind === "chat") add.setAttribute("data-hc-off", "");
      else add.removeAttribute("data-hc-off");
    }
    if (kind === "chat") loadSourceChats();
    return true;
  }

  // Every chat the workspace can see, not only this project's: the reason
  // to attach a conversation is often that it happened somewhere else --
  // the design argument had in another repository is exactly the context
  // this project is missing. This project's own come first, and each row
  // says which project it was, the way the prompt picker's chat list does.
  var sourceChatRows = [];

  function loadSourceChats() {
    if (!overviewBox) return Promise.resolve(null);
    var host = overviewBox.querySelector("[data-hc-src-chats]");
    if (!host) return Promise.resolve(null);
    wipe(host);
    host.appendChild(el("div", "hc-overview-srcsay", "looking…"));
    return fetchJSON("/api/chats").then(function (result) {
      sourceChatRows = allChatsFor(result, projectInfo() || {});
      renderSourceChats("");
      return result;
    });
  }

  function allChatsFor(data, who) {
    var here = str(who && who.cwd);
    var rows = array(data && data.available).filter(function (row) {
      return row && typeof row.session_id === "string" && row.session_id;
    }).map(function (row) {
      return { session_id: str(row.session_id),
               project: str(row.project),
               cwd: str(row.cwd),
               mine: !!here && str(row.cwd) === here,
               mtime: row.mtime };
    });
    // This project's first, then everything else, newest first within each.
    var byAge = function (a, b) { return (Number(b.mtime) || 0) - (Number(a.mtime) || 0); };
    return rows.filter(function (r) { return r.mine; }).sort(byAge)
      .concat(rows.filter(function (r) { return !r.mine; }).sort(byAge));
  }

  function renderSourceChats(filter) {
    if (!overviewBox) return false;
    var host = overviewBox.querySelector("[data-hc-src-chats]");
    if (!host) return false;
    wipe(host);
    var want = str(filter).trim().toLowerCase();
    var rows = sourceChatRows.filter(function (row) {
      if (!want) return true;
      return (row.session_id + " " + row.project + " " + row.cwd)
        .toLowerCase().indexOf(want) >= 0;
    });
    if (!sourceChatRows.length) {
      host.appendChild(el("div", "hc-overview-srcsay",
                          "no other chats to attach"));
      return true;
    }
    if (!rows.length) {
      host.appendChild(el("div", "hc-overview-srcsay",
                          "no chat matches that"));
      return true;
    }
    var already = {};
    sourceRows(projectInfo() || {}).forEach(function (row) {
      if (str(row.type) === "chat") already[str(row.label)] = true;
    });
    rows.slice(0, 60).forEach(function (row) {
      var pick = el("div", "hc-overview-srcchat");
      pick.setAttribute("role", "button");
      pick.setAttribute("title", row.cwd || row.session_id);
      if (already[row.session_id]) pick.setAttribute("data-hc-had", "");
      else pick.setAttribute("data-hc-pick-chat", row.session_id);
      pick.appendChild(el("span", "hc-overview-srcchat-id",
                          row.session_id.slice(0, 8)));
      var where = el("span", "hc-overview-srcchat-where", row.project
        || (row.cwd ? row.cwd.split("/").pop() : "elsewhere"));
      if (row.mine) where.setAttribute("data-hc-here", "");
      pick.appendChild(where);
      pick.appendChild(el("span", "hc-overview-srcchat-when",
        already[row.session_id] ? "attached" : relativeAge(row.mtime)));
      host.appendChild(pick);
    });
    if (rows.length > 60) {
      host.appendChild(el("div", "hc-overview-srcsay",
                          (rows.length - 60) + " more; type to narrow"));
    }
    return true;
  }

  // Saved by posting the whole list back, the way the objective and the
  // description are: the record holds one array and the server normalises
  // it, so there is no second place for a source to half-exist.
  function saveSources(rows, note) {
    return post({ op: "set_project_meta", sources: rows }).then(function (result) {
      if (!result || !result.ok) {
        sourceSay((result && result.error) || "could not save it", true);
        return false;
      }
      if (serverState.project) serverState.project.sources = array(result.sources);
      var host = overviewBox && overviewBox.querySelector("[data-hc-srcs]");
      if (host) renderSourceList(host, projectInfo() || {});
      if (note) sourceSay(note);
      return true;
    });
  }

  function sourceSay(words, bad) {
    if (!overviewBox) return false;
    var node = overviewBox.querySelector("[data-hc-src-say]");
    if (!node) return false;
    node.textContent = str(words);
    if (bad) node.setAttribute("data-hc-bad", "");
    else node.removeAttribute("data-hc-bad");
    return true;
  }

  function addSource(kind, label) {
    var who = projectInfo();
    if (!who) return false;
    var text = str(label).trim();
    if (!text) { sourceSay("say what to attach first", true); return false; }
    var rows = sourceRows(who).map(function (row) {
      return { id: str(row.id), type: str(row.type), label: str(row.label) };
    });
    var taken = rows.filter(function (row) { return row.label === text; });
    if (taken.length) { sourceSay("already attached", true); return false; }
    rows.push({ id: "s" + (rows.length + 1) + "-" + text.length,
                type: str(kind), label: text });
    sourceSay("saving…");
    // The dialog is over the list it just changed; leaving it up would hide
    // the row that proves the attach worked.
    saveSources(rows, "attached").then(function (ok) {
      if (ok) closeAddSource();
    });
    return true;
  }

  function dropSource(id) {
    var who = projectInfo();
    if (!who) return false;
    if (overviewSource === str(id)) showSource("");
    saveSources(sourceRows(who)
      .filter(function (row) { return str(row.id) !== str(id); })
      .map(function (row) {
        return { id: str(row.id), type: str(row.type), label: str(row.label) };
      }), "removed");
    return true;
  }

  // --- reading one attached source ----------------------------------------

  function showSource(id) {
    overviewSource = str(id);
    if (!overviewBox) return false;
    var repo = overviewBox.querySelector("[data-hc-repo-pane]");
    var reading = overviewBox.querySelector("[data-hc-reading]");
    var list = overviewBox.querySelector("[data-hc-srcs]");
    if (list) renderSourceList(list, projectInfo() || {});
    if (repo) {
      if (overviewSource) repo.setAttribute("data-hc-off", "");
      else repo.removeAttribute("data-hc-off");
    }
    if (!reading) return false;
    if (!overviewSource) {
      reading.removeAttribute("data-hc-on");
      wipe(reading);
      return true;
    }
    reading.setAttribute("data-hc-on", "");
    wipe(reading);
    var row = sourceRows(projectInfo() || {}).filter(function (r) {
      return str(r.id) === overviewSource; })[0];
    if (!row) return false;
    var spec = SOURCE_KINDS[str(row.type)] || SOURCE_KINDS.doc;
    reading.appendChild(el("div", "hc-overview-repo-name", sourceName(row)));
    reading.appendChild(el("div", "hc-overview-src-kind", spec.kind));
    reading.appendChild(paneTabs("src", [["source", "Source"], ["ask", "Ask"]]));
    var body = el("div", "hc-overview-panebody");
    body.setAttribute("data-hc-pane-body", "src");
    reading.appendChild(body);
    renderPane("src");
    return true;
  }

  // --- the two reading panes ----------------------------------------------
  //
  // The repository's and the open source's are the same thing twice: a tab
  // row over one body. Each keeps its own tab, so opening a source does not
  // answer a question asked about the repository, and going back to the
  // repository does not lose the pane it was on.

  var overviewPane = { repo: "readme", src: "source" };

  function paneTabs(where, specs) {
    var panes = el("div", "hc-overview-panes");
    specs.forEach(function (spec) {
      var on = overviewPane[where] === spec[0];
      var tab = el("span", "hc-overview-pane-tab"
                   + (on ? " hc-overview-pane-tab-on" : ""), spec[1]);
      tab.setAttribute("role", "button");
      tab.setAttribute("data-hc-pane", where + ":" + spec[0]);
      panes.appendChild(tab);
    });
    return panes;
  }

  function showPane(where, which) {
    if (!overviewBox || !overviewBox.querySelectorAll) return false;
    overviewPane[where] = str(which);
    Array.prototype.slice.call(
      overviewBox.querySelectorAll("[data-hc-pane]")).forEach(function (tab) {
        var name = str(tab.getAttribute("data-hc-pane")).split(":");
        if (name[0] !== where) return;
        // className rather than setAttribute("class"): el() writes the
        // class the same way, and the two must stay the one property.
        tab.className = "hc-overview-pane-tab"
          + (name[1] === str(which) ? " hc-overview-pane-tab-on" : "");
      });
    return renderPane(where);
  }

  function renderPane(where) {
    if (!overviewBox) return false;
    var host = overviewBox.querySelector(
      '[data-hc-pane-body="' + where + '"]');
    if (!host) return false;
    wipe(host);
    if (overviewPane[where] === "ask") {
      host.appendChild(askNode(where));
      return renderAsk(where);
    }
    if (where === "repo") return renderReadme(host);
    var row = sourceRows(projectInfo() || {}).filter(function (r) {
      return str(r.id) === overviewSource; })[0];
    if (!row) return false;
    var body = el("div", "hc-overview-srcbody");
    body.setAttribute("data-hc-src-body", "");
    body.appendChild(el("div", "hc-overview-empty", "reading…"));
    host.appendChild(body);
    loadSourceBody(row);
    return true;
  }

  // The README, read once per project and kept: a reader who goes to Ask
  // and back must not watch the front page load a second time.
  var readmeKept = { cwd: "", result: null };

  function renderReadme(host) {
    var here = str((projectInfo() || {}).cwd);
    if (readmeKept.cwd !== here) readmeKept = { cwd: here, result: null };
    if (readmeKept.result) return paintReadme(host, readmeKept.result);
    host.appendChild(el("div", "hc-overview-empty", "reading…"));
    fetchJSON("/api/readme").then(function (result) {
      readmeKept = { cwd: here, result: result || { ok: false } };
      if (!overviewBox || overviewPane.repo !== "readme") return result;
      var live = overviewBox.querySelector('[data-hc-pane-body="repo"]');
      if (live) { wipe(live); paintReadme(live, readmeKept.result); }
      return result;
    });
    return true;
  }

  function paintReadme(host, result) {
    if (!result || !result.ok || typeof result.text !== "string") {
      host.appendChild(el("div", "hc-overview-empty",
        (result && result.error) || "could not read the README"));
      return true;
    }
    host.appendChild(markdownNode(str(result.text)));
    if (result.truncated) {
      host.appendChild(el("div", "hc-overview-more",
        "… truncated; the file is longer than this pane reads"));
    }
    return true;
  }

  // --- asking about what is in the pane -----------------------------------
  //
  // One question about the one document in front of it, answered by the
  // server from that document alone. The answer is held for as long as the
  // page is up and no longer: this is a reading aid, not a transcript, and
  // nothing about it belongs in the project's record.

  var askKept = {};

  function askKey(where) {
    return where === "repo" ? "repo" : "src:" + overviewSource;
  }

  function askNode(where) {
    var box = el("div", "hc-overview-ask");
    var field = el("textarea", "hc-overview-askfield");
    field.setAttribute("data-hc-ask-field", where);
    field.setAttribute("rows", "2");
    field.setAttribute("spellcheck", "false");
    field.setAttribute("placeholder", where === "repo"
      ? "Ask about this repository — ⌘↩ to send"
      : "Ask about this document — ⌘↩ to send");
    var kept = askKept[askKey(where)];
    if (kept) field.value = str(kept.question);
    box.appendChild(field);
    var row = el("div", "hc-overview-srcrow");
    var go = el("span", "hc-overview-srcbtn", "Ask");
    go.setAttribute("role", "button");
    go.setAttribute("data-hc-ask", where);
    row.appendChild(go);
    var say = el("span", "hc-overview-srcsay", "");
    say.setAttribute("data-hc-ask-say", where);
    row.appendChild(say);
    box.appendChild(row);
    var out = el("div", "hc-overview-askout");
    out.setAttribute("data-hc-ask-out", where);
    box.appendChild(out);
    return box;
  }

  function askSay(where, words, bad) {
    if (!overviewBox) return false;
    var node = overviewBox.querySelector(
      '[data-hc-ask-say="' + where + '"]');
    if (!node) return false;
    node.textContent = str(words);
    if (bad) node.setAttribute("data-hc-bad", "");
    else node.removeAttribute("data-hc-bad");
    return true;
  }

  function renderAsk(where) {
    if (!overviewBox) return false;
    var out = overviewBox.querySelector(
      '[data-hc-ask-out="' + where + '"]');
    if (!out) return false;
    wipe(out);
    var kept = askKept[askKey(where)];
    if (!kept) return askSay(where, "");
    if (kept.error) return askSay(where, kept.error, true);
    if (!kept.answer) return askSay(where, "asking…");
    askSay(where, "");
    out.appendChild(markdownNode(kept.answer));
    return true;
  }

  function runAsk(where) {
    if (!overviewBox) return false;
    var field = overviewBox.querySelector(
      '[data-hc-ask-field="' + where + '"]');
    var words = str(field && field.value).trim();
    if (!words) { askSay(where, "ask something first", true); return false; }
    var key = askKey(where);
    askKept[key] = { question: words, answer: "", error: "" };
    renderAsk(where);
    if (typeof fetch !== "function") return false;
    fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: where === "repo" ? "" : overviewSource,
                             question: words })
    }).then(function (r) { return r.json(); })
      .catch(function () { return null; })
      .then(function (result) {
        var kept = askKept[key];
        // A second question, or another source, while this one was out:
        // the answer that came back is to a question nobody is looking at.
        if (!kept || kept.question !== words) return result;
        if (result && result.ok) kept.answer = str(result.answer);
        else kept.error = str((result && result.error)
                              || "the question could not be asked");
        if (overviewPane[where] === "ask") renderAsk(where);
        return result;
      });
    return true;
  }

  function loadSourceBody(row) {
    var want = str(row.id);
    return fetchJSON("/api/source?id=" + encodeURIComponent(want))
      .then(function (result) {
        if (overviewSource !== want || !overviewBox) return result;
        var body = overviewBox.querySelector("[data-hc-src-body]");
        if (!body) return result;
        wipe(body);
        renderSourceBody(body, row, result);
        return result;
      });
  }

  function renderSourceBody(body, row, result) {
    if (!result || !result.ok) {
      body.appendChild(el("div", "hc-overview-empty",
        (result && result.error) || "could not read it"));
      return body;
    }
    if (str(result.kind) === "chat") {
      var turns = array(result.turns);
      turns.forEach(function (turn) {
        var line = el("div", "hc-overview-turn");
        line.setAttribute("data-hc-role", str(turn.role));
        line.appendChild(el("div", "hc-overview-turn-who", str(turn.role)));
        line.appendChild(el("div", "hc-overview-turn-text", str(turn.text)));
        body.appendChild(line);
      });
      if (Number(result.total) > turns.length) {
        body.appendChild(el("div", "hc-overview-more",
          "… " + (Number(result.total) - turns.length)
          + " earlier turns not shown"));
      }
      return body;
    }
    // A link is all a repository or a remote document can be from here:
    // this pane reads the disk, and fetching over the network is not what
    // a project overview should quietly start doing.
    if (typeof result.text !== "string") {
      var label = str(result.label || row.label);
      body.appendChild(mdLink(label, label));
      body.appendChild(el("div", "hc-overview-more",
        str(result.kind) === "github"
          ? "A repository, not a file: opened rather than read here."
          : "A link, not a file of this project."));
      return body;
    }
    body.appendChild(markdownNode(str(result.text)));
    if (result.truncated) {
      body.appendChild(el("div", "hc-overview-more",
        "… truncated; the file is longer than this pane reads"));
    }
    return body;
  }

  function overviewNode(who) {
    var box = el("div", "hc-overview");
    box.setAttribute("role", "region");
    box.setAttribute("aria-label", "Project overview");
    var tabs = el("div", "hc-overview-tabs");
    var over = el("span", "hc-overview-tab hc-overview-tab-on", "OVERVIEW");
    over.setAttribute("data-hc-overview-tab", "overview");
    var goals = el("span", "hc-overview-tab", "GOALS");
    goals.setAttribute("data-hc-overview-tab", "goals");
    goals.setAttribute("role", "button");
    tabs.appendChild(over);
    tabs.appendChild(goals);
    box.appendChild(tabs);

    // The project card, in the order a reader needs it: what this project
    // is, what it is for, and then the facts that place it. The objective
    // sits under a rule of its own because it is the one line here anybody
    // writes by hand.
    //
    // Every written line on this card is a field, not a label with a way in
    // behind a menu: the name, what the project is, and what it is for are
    // all typed where they are read. The \u22ef that used to sit beside the name
    // is gone -- it opened the switcher, which the header chip already does.
    var card = el("div", "hc-overview-card");
    var head = el("div", "hc-overview-head");
    var name = el("input", "hc-overview-name");
    name.setAttribute("type", "text");
    name.setAttribute("data-hc-name", "");
    name.setAttribute("placeholder", "Name this project");
    name.setAttribute("spellcheck", "false");
    name.setAttribute("autocomplete", "off");
    name.setAttribute("title", "What this project is called; the directory's"
                      + " own name when this is empty");
    name.value = str(who.name);
    head.appendChild(name);
    card.appendChild(head);
    var about = el("textarea", "hc-overview-about");
    about.setAttribute("placeholder", "What is this project?");
    about.setAttribute("spellcheck", "false");
    about.setAttribute("rows", "1");
    about.setAttribute("data-hc-about", "");
    about.value = str(who.description) === str(who.objective)
      ? "" : str(who.description);
    card.appendChild(about);
    fitObjective(about);

    // Where it is, what is checked out, and where it came from. Read from
    // git rather than written, so they sit apart from the two fields above
    // as facts rather than answers.
    var facts = el("div", "hc-overview-facts");
    [["directory", str(who.cwd), ""],
     ["branch", str(who.branch), ""],
     ["origin", str(who.remote), remoteHref(who.remote)]].forEach(
      function (spec) {
        if (!spec[1]) return;
        var row = el("div", "hc-overview-fact");
        row.appendChild(el("span", "hc-overview-fact-k", spec[0]));
        if (spec[2]) {
          var a = el("a", "hc-overview-fact-v", spec[1]);
          a.setAttribute("href", spec[2]);
          a.setAttribute("target", "_blank");
          a.setAttribute("rel", "noreferrer noopener");
          row.appendChild(a);
        } else {
          row.appendChild(el("span", "hc-overview-fact-v", spec[1]));
        }
        facts.appendChild(row);
      });
    if (facts.children && facts.children.length) card.appendChild(facts);

    var objSec = el("div", "hc-overview-sec");
    var label = el("div", "hc-overview-label", "main objective");
    label.appendChild(el("small", "", "· optional"));
    objSec.appendChild(label);
    card.appendChild(objSec);
    var objective = el("textarea", "hc-overview-objective");
    objective.setAttribute("placeholder", "What are you trying to accomplish?");
    objective.setAttribute("spellcheck", "false");
    objective.setAttribute("rows", "1");
    objective.value = str(who.objective);
    objSec.appendChild(objective);
    fitObjective(objective);
    box.appendChild(card);

    var ctxHead = el("div", "hc-overview-ctxhead");
    ctxHead.appendChild(el("div", "hc-overview-label", "context"));
    // When this was drawn. A source is fetched when it is opened, not
    // followed, so what is on screen is as old as this line says.
    var when = el("span", "hc-overview-refreshed", "");
    when.setAttribute("data-hc-refreshed", "");
    try {
      when.textContent = "refreshed " + new Date().toLocaleTimeString(
        [], { hour: "numeric", minute: "2-digit" });
    } catch (e) { when.textContent = ""; }
    ctxHead.appendChild(when);
    box.appendChild(ctxHead);
    var context = el("div", "hc-overview-context");
    var srcs = el("div", "hc-overview-srcs");
    srcs.setAttribute("data-hc-srcs", "");
    renderSourceList(srcs, who);
    context.appendChild(srcs);
    // Where a source that is not the repository is read. Hidden while the
    // repository is the one selected, which is the state the page opens in.
    var reading = el("div", "hc-overview-reading");
    reading.setAttribute("data-hc-reading", "");
    context.appendChild(reading);
    var repo = el("div", "hc-overview-repo");
    repo.setAttribute("data-hc-repo-pane", "");
    repo.appendChild(el("div", "hc-overview-repo-name", who.name));
    var meta = el("div", "hc-overview-repo-meta");
    var href = remoteHref(who.remote);
    if (who.remote && href) {
      var a = el("a", "", who.remote);
      a.setAttribute("href", href);
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener");
      meta.appendChild(a);
    } else if (who.remote) {
      meta.appendChild(el("span", "", who.remote));
    }
    if (who.branch) {
      meta.appendChild(el("span", "", (who.remote ? " · " : "") + who.branch));
    }
    if (!who.remote && !who.branch) meta.textContent = who.cwd;
    repo.appendChild(meta);
    // The README is back, and only the README. It is the one piece of a
    // project's context nobody has to attach and everybody reads first, so
    // the pane that names the repository may as well show what it says.
    // The file tree that used to sit beside it is not: that was a file
    // browser bolted onto a project page, in front of the sources somebody
    // actually attached.
    repo.appendChild(paneTabs("repo", [["readme", "Readme"], ["ask", "Ask"]]));
    var repoBody = el("div", "hc-overview-panebody");
    repoBody.setAttribute("data-hc-pane-body", "repo");
    repo.appendChild(repoBody);
    context.appendChild(repo);
    box.appendChild(context);
    box.appendChild(addSourceModal());
    return box;
  }

  // Markdown, read rather than transcribed. This is a reader's markdown,
  // not a conformant one: headings, code, lists, quotes, rules, tables of
  // links, and the inline run of code/bold/italic/link -- which is all a
  // project's front page uses. Everything is built as DOM nodes, so no
  // markup in the file can become markup on the page.
  var MD_INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(!?\[[^\]]*\]\([^)\s]+\))|(<https?:\/\/[^>\s]+>)|(https?:\/\/[^\s<>()]+)/;

  function mdInline(host, text) {
    var rest = str(text);
    var guard = 0;
    while (rest && guard < 400) {
      guard += 1;
      var hit = MD_INLINE.exec(rest);
      if (!hit) break;
      if (hit.index > 0) host.appendChild(el("span", "", rest.slice(0, hit.index)));
      var token = hit[0];
      rest = rest.slice(hit.index + token.length);
      if (token.charAt(0) === "`") {
        host.appendChild(el("code", "hc-md-code", token.slice(1, -1)));
      } else if (token.slice(0, 2) === "**" || token.slice(0, 2) === "__") {
        host.appendChild(el("strong", "", token.slice(2, -2)));
      } else if (token.charAt(0) === "*") {
        host.appendChild(el("em", "", token.slice(1, -1)));
      } else if (token.charAt(0) === "[" || token.slice(0, 2) === "![") {
        var image = token.charAt(0) === "!";
        var cut = token.indexOf("](");
        var label = token.slice(image ? 2 : 1, cut);
        var href = token.slice(cut + 2, -1);
        // An image is named, not fetched: the pane reads a file from disk
        // and has no business pulling anything over the network.
        if (image) {
          host.appendChild(el("span", "hc-md-img", label || "image"));
        } else {
          host.appendChild(mdLink(href, label || href));
        }
      } else if (token.charAt(0) === "<") {
        host.appendChild(mdLink(token.slice(1, -1), token.slice(1, -1)));
      } else {
        host.appendChild(mdLink(token, token));
      }
    }
    if (rest) host.appendChild(el("span", "", rest));
    return host;
  }

  function mdLink(href, label) {
    var safe = /^(https?:|mailto:|#|\/|\.)/.test(str(href));
    if (!safe) return el("span", "", str(label));
    var a = el("a", "hc-md-link", str(label));
    a.setAttribute("href", str(href));
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noreferrer noopener");
    return a;
  }

  function markdownNode(text) {
    var box = el("div", "hc-md");
    var lines = str(text).split("\n");
    var at = 0;
    var para = null;
    var list = null;
    var flush = function () { para = null; list = null; };
    while (at < lines.length) {
      var line = lines[at];
      var bare = line.replace(/\s+$/, "");
      var trimmed = bare.replace(/^\s+/, "");
      // A fenced block is copied out whole, fences and all, until it closes
      // or the file ends -- an unterminated fence must not eat the page.
      var fence = /^(```|~~~)(.*)$/.exec(trimmed);
      if (fence) {
        flush();
        var body = [];
        at += 1;
        while (at < lines.length && !new RegExp("^\\s*" + fence[1]).test(lines[at])) {
          body.push(lines[at]);
          at += 1;
        }
        at += 1;
        var block = el("pre", "hc-md-pre", body.join("\n"));
        if (str(fence[2]).trim()) block.setAttribute("data-hc-lang", str(fence[2]).trim());
        box.appendChild(block);
        continue;
      }
      at += 1;
      // Raw HTML: a README's <div align="center"> and its closing tag are
      // layout for GitHub and noise here. Dropped, not shown, and never
      // interpreted.
      if (/^<\/?[a-zA-Z][^>]*>$/.test(trimmed)) { flush(); continue; }
      if (!trimmed) { flush(); continue; }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        flush();
        box.appendChild(el("div", "hc-md-rule", ""));
        continue;
      }
      var head = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (head) {
        flush();
        var level = Math.min(head[1].length, 6);
        box.appendChild(mdInline(el("div", "hc-md-h hc-md-h" + level, ""), head[2]));
        continue;
      }
      var quote = /^>\s?(.*)$/.exec(trimmed);
      if (quote) {
        flush();
        box.appendChild(mdInline(el("div", "hc-md-quote", ""), quote[1]));
        continue;
      }
      var bullet = /^([-*+]|\d+[.)])\s+(.*)$/.exec(trimmed);
      if (bullet) {
        para = null;
        if (!list) { list = el("div", "hc-md-list"); box.appendChild(list); }
        var item = el("div", "hc-md-item");
        item.appendChild(el("span", "hc-md-bullet",
          /^\d/.test(bullet[1]) ? bullet[1] : "•"));
        item.appendChild(mdInline(el("span", "hc-md-itemtext", ""), bullet[2]));
        list.appendChild(item);
        continue;
      }
      // A table is read as its rows: the pane is narrow, and a grid of
      // pipes is harder to read here than the cells one after another.
      if (/^\|.*\|$/.test(trimmed)) {
        if (/^\|[\s:|-]+\|$/.test(trimmed)) continue;
        flush();
        var cells = trimmed.slice(1, -1).split("|").map(function (c) {
          return c.replace(/^\s+|\s+$/g, ""); }).filter(Boolean);
        box.appendChild(mdInline(el("div", "hc-md-row", ""), cells.join("  ·  ")));
        continue;
      }
      list = null;
      if (!para) { para = el("div", "hc-md-p"); box.appendChild(para); }
      else para.appendChild(el("span", "", " "));
      mdInline(para, trimmed);
    }
    return box;
  }

  // The project's own file: every chat's goals of this directory, with
  // where each sits in its tree, its notes, its TODO rows and their
  // statuses, its prompt and the prompts marked related to it. Shown as the
  // file reads -- this is the record a collaborator would be handed, and a
  // reader checking what is kept should see exactly what is in it.
  // The project's own record, under the gear: one file per directory,
  // holding every goal of every chat started there. It is what this
  // machine keeps, which is a settings question rather than a project one.
  function renderProjectJson(result) {
    if (!settingsPanelBox) return;
    var pane = settingsPanelBox.querySelector("[data-hc-record]");
    if (!pane) return;
    wipe(pane);
    if (!result || !result.ok) {
      pane.appendChild(el("div", "hc-settings-hint",
                          "This chat has no project directory, so there is"
                          + " no record to read."));
      return;
    }
    var where = el("div", "hc-settings-hint", str(result.path));
    if (!result.written) {
      where.appendChild(el("span", "",
        " · not written yet; this is what it would hold"));
    }
    pane.appendChild(where);
    pane.appendChild(el("pre", "hc-settings-record", str(result.text)));
    if (result.truncated) {
      pane.appendChild(el("div", "hc-settings-hint",
        "… truncated; the record is longer than this pane reads"));
    }
  }

  // The record is rewritten on every goal save, so an overview being opened
  // reads it again rather than showing what the project held the last time
  // it was looked at.
  function loadProjectJson() {
    if (projectJsonPending) return Promise.resolve(null);
    projectJsonPending = true;
    return fetchJSON("/api/project.json").then(function (result) {
      projectJsonPending = false;
      renderProjectJson(result || { ok: false });
      return result;
    });
  }

  // What the button reads before it is pressed. Kept out of /api/state:
  // it reaches the vault and, when a token has lapsed, the network -- and
  // the goal tree should not wait on either.
  function loadSyncStatus() {
    return fetchJSON("/api/supabase").then(function (result) {
      renderSyncStatus(result);
      return result;
    });
  }

  // The sending and sharing controls moved into the settings panel, where
  // the account they need is signed into. They exist only while it is open.
  function syncHost() {
    return settingsPanelShown() ? settingsPanelBox : null;
  }

  function syncNodes() {
    var host = syncHost();
    if (!host) return null;
    var button = host.querySelector("[data-hc-sync]");
    var say = host.querySelector("[data-hc-sync-say]");
    return button && say ? { button: button, say: say } : null;
  }

  function sayOn(node, text, bad) {
    if (!node) return;
    node.textContent = text;
    if (bad) node.setAttribute("data-hc-bad", "");
    else node.removeAttribute("data-hc-bad");
  }

  // A shared workspace refuses every write at the server. Saying so only
  // in the reply is not enough: the controls are still there to press, and
  // a button that does nothing reads as a broken page rather than a rule.
  // Mark the document and let the styles below take them off it.
  function markReadonly(state) {
    var root = document.documentElement;
    if (!root || !root.setAttribute) return false;
    var shared = state && state.shared;
    if (shared && shared.readonly) {
      root.setAttribute("data-hc-readonly", "");
      showReadonlyNote(shared);
      // The tree redraws constantly, so this runs on every state rather
      // than once: a row drawn after the seal must be sealed too.
      try { sealEditors(); } catch (e) {}
      return true;
    }
    root.removeAttribute("data-hc-readonly");
    return false;
  }

  // One line at the top saying what this is and why nothing can be typed.
  function showReadonlyNote(shared) {
    if (document.querySelector("[data-hc-note]")) return;
    var note = el("div", "hc-ro-note");
    note.setAttribute("data-hc-note", "shared");
    var who = array(shared && shared.contributors).map(function (c) {
      return str(c.name);
    });
    note.appendChild(el("span", "hc-ro-dot", "●"));
    note.appendChild(el("span", "", "Shared workspace · read-only"
      + (who.length ? " · with " + who.join(", ") : "")));
    var host = document.body || document.documentElement;
    if (host) host.appendChild(note);
  }

  function renderSyncStatus(state) {
    renderShareState(state);
    var nodes = syncNodes();
    if (!nodes) return;
    nodes.button.removeAttribute("data-hc-busy");
    if (!state || !state.ok) {
      nodes.button.setAttribute("data-hc-off", "");
      sayOn(nodes.say, "the workspace could not read its Supabase settings", true);
      return;
    }
    if (!state.configured) {
      nodes.button.setAttribute("data-hc-off", "");
      sayOn(nodes.say, "not connected · run `hc supabase setup`, then put your"
                       + " project URL and anon key in " + str(state.config_path));
      return;
    }
    if (!state.signed_in) {
      nodes.button.setAttribute("data-hc-off", "");
      sayOn(nodes.say, "connected, not signed in · run `hc supabase login`");
      return;
    }
    nodes.button.removeAttribute("data-hc-off");
    sayOn(nodes.say, "signed in as " + str(state.email || "you")
                     + " · sends this project's goals, TODO rows and notes");
  }

  // Sharing needs the same sign-in the send does, so the two buttons turn
  // on together.
  function renderShareState(state) {
    var nodes = shareNodes();
    if (!nodes || !nodes.button) return;
    if (state && state.ok && state.configured && state.signed_in) {
      nodes.button.removeAttribute("data-hc-off");
      loadShares();
    } else {
      nodes.button.setAttribute("data-hc-off", "");
    }
  }

  // Which goals this reader may change. Empty in a personal workspace,
  // where the question does not arise and every row is theirs.
  var lockedRows = {};
  var rowAuthors = {};

  function noteRowRights(st) {
    lockedRows = {};
    rowAuthors = {};
    if (!st || !st.shared) return;
    array(st.goals).forEach(function (g) {
      if (!g || typeof g.id !== "string") return;
      if (g.shared_readonly) lockedRows[g.id] = true;
      rowAuthors[g.id] = str(g.shared_author);
    });
  }

  // The artifact draws rows without ids on them, so the id is read back
  // from the selection the same way the rest of the bridge does.
  function rowGoalId(node) {
    var row = closestByClass(node, "hc-row");
    if (!row) return "";
    var id = row.getAttribute && row.getAttribute("data-hc-goal");
    return id || "";
  }

  function editableRow(node) {
    if (!sharedWorkspace()) return true;
    var id = rowGoalId(node);
    if (!id) return true;
    return !lockedRows[id];
  }

  function sayNotYours(node) {
    var id = rowGoalId(node);
    var who = (id && rowAuthors[id]) || "someone else";
    flashNote(who + "'s goal — you can read it here, not change it");
  }

  // A line at the foot, briefly. Reuses the read-only note's place so a
  // shared workspace has one voice rather than two.
  var noteTimer = null;
  function flashNote(words) {
    var note = document.querySelector("[data-hc-note]");
    if (!note) {
      note = el("div", "hc-ro-note");
      note.setAttribute("data-hc-note", "shared");
      (document.querySelector(".hc") || document.body
       || document.documentElement).appendChild(note);
    }
    wipe(note);
    note.appendChild(el("span", "hc-ro-dot", "●"));
    note.appendChild(el("span", "", words));
    if (noteTimer) clearTimeout(noteTimer);
    noteTimer = setTimeout(function () {
      var live = document.querySelector("[data-hc-note]");
      if (live && live.parentNode) live.parentNode.removeChild(live);
      noteTimer = null;
    }, 4000);
  }

  function sharedWorkspace() {
    return !!(serverState && serverState.shared);
  }

  // closestByClass's twin. The moved controls are identified by attribute
  // so their styling can be the panel's without the handlers caring.
  function closestAttr(node, name) {
    for (var at = node; at; at = at.parentNode) {
      if (at.getAttribute && at.getAttribute(name) !== null) return at;
    }
    return null;
  }

  function shareNodes() {
    var host = syncHost();
    if (!host) return null;
    return {
      button: host.querySelector("[data-hc-share]"),
      wrap: host.querySelector("[data-hc-code]"),
      box: host.querySelector("[data-hc-code-box]"),
      list: host.querySelector("[data-hc-shares]"),
      say: host.querySelector("[data-hc-sync-say]")
    };
  }

  // The open shares, without their tokens -- those are gone. Shown so a
  // reader can see how many doors are open and shut one.
  function renderShares(rows) {
    var nodes = shareNodes();
    if (!nodes || !nodes.list) return;
    wipe(nodes.list);
    array(rows).filter(function (r) { return r && !r.revoked_at; })
      .forEach(function (row) {
        var line = el("div", "hc-settings-share-row");
        var x = el("span", "hc-settings-share-x", "×");
        x.setAttribute("role", "button");
        x.setAttribute("data-hc-share-revoke", str(row.id));
        line.appendChild(x);
        // What the code grants, which is the part worth seeing at a
        // glance: an editor invite is the one to be careful with.
        line.appendChild(el("span", "", "joins as " + (str(row.role) || "reader")));
        // `uses` counts redemptions -- someone joining with it. Opening the
        // shared workspace from here is not one, and saying "opened" made
        // an unredeemed code look like it had failed.
        line.appendChild(el("span", "", Number(row.uses) > 0
          ? "redeemed " + row.uses + (Number(row.uses) === 1 ? " time" : " times")
          : "not redeemed yet"));
        if (row.expires_at) {
          line.appendChild(el("span", "", "expires " + String(row.expires_at).slice(0, 10)));
        }
        nodes.list.appendChild(line);
      });
  }

  function loadShares() {
    return post({ op: "list_shares" }).then(function (result) {
      if (result && result.ok) renderShares(result.shares);
      return result;
    });
  }

  function makeShare() {
    var nodes = shareNodes();
    if (!nodes || !nodes.button || nodes.button.hasAttribute("data-hc-off")
        || nodes.button.hasAttribute("data-hc-busy")) return false;
    nodes.button.setAttribute("data-hc-busy", "");
    sayOn(nodes.say, "minting an invite…");
    var host = syncHost();
    var chip = host && host.querySelector("[data-hc-share-role]");
    var wanted = chip ? chip.getAttribute("data-hc-share-role") : "reader";
    post({ op: "create_share", label: "shared from the workspace",
           expires_in_days: 30, role: wanted }).then(function (result) {
      var live = shareNodes();
      if (!live) return;
      if (live.button) live.button.removeAttribute("data-hc-busy");
      if (!result || !result.ok || !result.code) {
        sayOn(live.say, (result && result.error) || "could not make a share",
              true);
        return;
      }
      if (live.wrap) live.wrap.setAttribute("data-hc-on", "");
      var handOver = result.code;
      if (live.box) { live.box.value = handOver; live.box.focus(); live.box.select(); }
      sayOn(live.say, "invite ready · joins as "
        + str(result.role || "reader") + " · expires in 30 days");
      // The same clipboard path the hand-off uses: async first,
      // hidden-textarea when that is missing or refuses.
      handoffToClipboard(handOver);
      loadShares();
    });
    return true;
  }

  function revokeShare(id) {
    post({ op: "revoke_share", id: id }).then(function () { loadShares(); });
    return true;
  }

  function runSync() {
    var nodes = syncNodes();
    if (!nodes || nodes.button.hasAttribute("data-hc-off")
        || nodes.button.hasAttribute("data-hc-busy")) return false;
    nodes.button.setAttribute("data-hc-busy", "");
    sayOn(nodes.say, "sending…");
    post({ op: "sync_supabase" }).then(function (result) {
      var live = syncNodes();
      if (!live) return;
      live.button.removeAttribute("data-hc-busy");
      if (!result || !result.ok) {
        sayOn(live.say, (result && result.error) || "the send did not go through",
              true);
        return;
      }
      var sent = result.sent || {};
      sayOn(live.say, "sent · " + (sent.goals || 0) + " goals, "
                      + (sent.todos || 0) + " TODO rows, "
                      + (sent.chats || 0) + " chats");
    });
    return true;
  }

  function renderOverview() {
    var who = projectInfo();
    var root = document.documentElement;
    if (!who || serverState.scope !== "chat") {
      if (overviewShown()) closeOverview();
      return false;
    }
    if (!overviewShown()) return false;
    ensureProjectStyles();
    if (!overviewBox || !inLiveDocument(overviewBox)
        || overviewBox.getAttribute("data-hc-cwd") !== who.cwd) {
      if (overviewBox && overviewBox.parentNode) overviewBox.parentNode.removeChild(overviewBox);
      overviewBox = overviewNode(who);
      overviewBox.setAttribute("data-hc-cwd", who.cwd);
      syncProjectTheme(overviewBox);
      (document.body || document.documentElement).appendChild(overviewBox);
      // The repository pane's body is filled after the box is on screen and
      // named: what fills it goes looking for it through overviewBox.
      renderPane("repo");
      return true;
    }
    // The theme can move while the overview is up.
    var themed = syncProjectTheme(overviewBox);
    // The objective the server now holds, unless the reader is mid-edit.
    var objective = overviewBox.querySelector(".hc-overview-objective");
    if (objective && objective.getAttribute("data-hc-editing") === null
        && objective.value !== str(who.objective)) {
      objective.value = str(who.objective);
      fitObjective(objective);
      return true;
    }
    var about = overviewBox.querySelector("[data-hc-about]");
    if (about && about.getAttribute("data-hc-editing") === null
        && about.value !== str(who.description)) {
      about.value = str(who.description);
      fitObjective(about);
      return true;
    }
    // Same rule for the name: what the server holds, unless it is being
    // typed right now.
    var name = overviewBox.querySelector("[data-hc-name]");
    if (name && name.getAttribute("data-hc-editing") === null
        && name.value !== str(who.name)) {
      name.value = str(who.name);
      return true;
    }
    return themed;
  }

  // The box is one row and grows to its text: a fixed two rows left a
  // blank line under every short objective, and a fixed one clipped a long
  // one. Measured after it is on the page, so a freshly drawn box is
  // fitted once the layout exists.
  function fitObjective(node) {
    if (!node || !node.style) return false;
    try {
      node.style.height = "auto";
      var want = Number(node.scrollHeight) || 0;
      node.style.height = (want > 0 ? want : 28) + "px";
    } catch (e) { return false; }
    return true;
  }

  function openOverview() {
    var who = projectInfo();
    var root = document.documentElement;
    if (!who || !root || !root.setAttribute) return false;
    closeProjectMenu();
    root.setAttribute("data-hc-overview", "");
    renderOverview();
    // A reopened overview keeps the box it drew before, whose record is as
    // old as that drawing; the fetch is a no-op when renderOverview just
    // made a new one and started the same read.
    return true;
  }

  function closeOverview() {
    var root = document.documentElement;
    if (!root || !root.removeAttribute) return false;
    var was = overviewShown();
    root.removeAttribute("data-hc-overview");
    return was;
  }

  function toggleOverview() {
    return overviewShown() ? !closeOverview() : openOverview();
  }

  function saveObjective(textarea) {
    var who = projectInfo();
    if (!who || !textarea) return Promise.resolve(false);
    var text = str(textarea.value);
    if (textarea.removeAttribute) textarea.removeAttribute("data-hc-editing");
    if (text.trim() === str(who.objective).trim()) return Promise.resolve(false);
    return post({ op: "set_project_objective", objective: text }).then(function (result) {
      if (!result || !result.ok) return false;
      if (serverState.project) serverState.project.objective = str(result.objective);
      return true;
    });
  }

  function saveAbout(textarea) {
    var who = projectInfo();
    if (!who || !textarea) return Promise.resolve(false);
    var text = str(textarea.value);
    if (textarea.removeAttribute) textarea.removeAttribute("data-hc-editing");
    if (text.trim() === str(who.description).trim()) return Promise.resolve(false);
    return post({ op: "set_project_meta", description: text }).then(function (result) {
      if (!result || !result.ok) return false;
      if (serverState.project) serverState.project.description = str(result.description);
      return true;
    });
  }

  // Renaming the project, typed where the name is read. An empty field is
  // not an error: the server falls back to the directory's own name, and
  // the field is filled with whatever comes back.
  function saveName(field) {
    var who = projectInfo();
    if (!who || !field) return Promise.resolve(false);
    var text = str(field.value);
    if (field.removeAttribute) field.removeAttribute("data-hc-editing");
    if (text.trim() === str(who.name).trim()) return Promise.resolve(false);
    return post({ op: "set_project_meta", name: text }).then(function (result) {
      if (!result || !result.ok) {
        field.value = str(who.name);
        return false;
      }
      if (serverState.project) serverState.project.name = str(result.name);
      field.value = str(result.name);
      // The header chip is the same name in another place.
      renderProjectChip();
      return true;
    });
  }

  function bindProject() {
    if (projectBound || !document.addEventListener) return;
    projectBound = true;
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("resize", function () {
        if (projectMenuShown()) placeProjectMenu();
        if (settingsPanelShown()) placeSettingsPanel();
      });
    }
    document.addEventListener("click", function (event) {
      var target = event && event.target;
      if (!target) return;
      var stop = function () {
        if (event.preventDefault) event.preventDefault();
        if (event.stopPropagation) event.stopPropagation();
      };
      if (closestByClass(target, "hc-project-name")) {
        stop();
        toggleProjectMenu();
        return;
      }
      var act = closestByClass(target, "hc-project-act");
      if (act) {
        stop();
        if (act.getAttribute("data-hc-project-act") === "overview") openOverview();
        else closeProjectMenu();
        return;
      }
      var row = closestAttr(target, "data-hc-goto");
      if (row) {
        stop();
        if (row.getAttribute("data-hc-project-setup-row") !== null) {
          var named = row.querySelector(".hc-project-row-name");
          askProjectSetup({ cwd: row.getAttribute("data-hc-goto"),
                            name: named ? str(named.textContent) : "" });
          return;
        }
        switchProject(row.getAttribute("data-hc-goto"));
        return;
      }
      if (closestAttr(target, "data-hc-project-new")) {
        stop();
        var form = projectMenuBox
          && projectMenuBox.querySelector("[data-hc-project-newform]");
        if (form) {
          if (form.getAttribute("data-hc-on") !== null) form.removeAttribute("data-hc-on");
          else {
            form.setAttribute("data-hc-on", "");
            var field = form.querySelector("[data-hc-project-name]");
            if (field && field.focus) field.focus();
          }
        }
        return;
      }
      if (closestAttr(target, "data-hc-project-browse")) {
        stop();
        chooseProjectFolder();
        return;
      }
      if (closestAttr(target, "data-hc-project-add")) {
        stop();
        addProject();
        return;
      }
      if (closestAttr(target, "data-hc-project-setup-save")) {
        stop();
        saveProjectSetup();
        return;
      }
      if (closestAttr(target, "data-hc-project-setup-skip")) {
        stop();
        closeProjectSetup();
        projectSay("");
        return;
      }
      if (closestByClass(target, "hc-project-menu")) return;
      if (projectMenuShown()) closeProjectMenu();
      var tab = closestByClass(target, "hc-overview-tab");
      if (tab) {
        stop();
        if (tab.getAttribute("data-hc-overview-tab") === "goals") closeOverview();
        return;
      }
      var revoke = target.getAttribute
        && target.getAttribute("data-hc-share-revoke");
      if (revoke) { stop(); revokeShare(revoke); return; }
      var roleChip = closestAttr(target, "data-hc-share-role");
      if (roleChip) {
        stop();
        var next = roleChip.getAttribute("data-hc-share-role") === "editor"
          ? "reader" : "editor";
        roleChip.setAttribute("data-hc-share-role", next);
        roleChip.textContent = "as " + next;
        return;
      }
      var shareBtn = closestAttr(target, "data-hc-share")
        || closestAttr(target, "data-hc-code-copy");
      if (shareBtn) {
        stop();
        if (shareBtn.getAttribute("data-hc-code-copy") !== null) {
          var host = syncHost();
          var boxNode = host && host.querySelector("[data-hc-code-box]");
          if (boxNode) { boxNode.focus(); boxNode.select();
                         handoffToClipboard(boxNode.value); }
        } else {
          makeShare();
        }
        return;
      }
      var viewTab = closestByClass(target, "hc-viewtab");
      if (viewTab) {
        stop();
        if (viewTab.getAttribute("data-hc-viewtab") === "overview") openOverview();
        else closeOverview();
        renderViewTabs();
        return;
      }
      var sendUp = closestAttr(target, "data-hc-sync");
      if (sendUp) {
        stop();
        runSync();
        return;
      }
      var drop = closestAttr(target, "data-hc-drop-source");
      if (drop) {
        stop();
        dropSource(drop.getAttribute("data-hc-drop-source"));
        return;
      }
      var paneTab = closestAttr(target, "data-hc-pane");
      if (paneTab) {
        stop();
        var which = str(paneTab.getAttribute("data-hc-pane")).split(":");
        showPane(which[0], which[1]);
        return;
      }
      var askGo = closestAttr(target, "data-hc-ask");
      if (askGo) {
        stop();
        runAsk(askGo.getAttribute("data-hc-ask"));
        return;
      }
      var pickSrc = closestAttr(target, "data-hc-source");
      if (pickSrc) {
        stop();
        showSource(pickSrc.getAttribute("data-hc-source"));
        return;
      }
      // An inherited chip is not this goal's to edit, but it is readable:
      // the click goes where it is attached, opened on that source.
      var ctxSrc = closestAttr(target, "data-hc-ctxid");
      if (ctxSrc) {
        stop();
        openOverview();
        showSource(ctxSrc.getAttribute("data-hc-ctxid"));
        renderViewTabs();
        return;
      }
      if (closestAttr(target, "data-hc-add-source")) {
        stop();
        toggleAddSource();
        return;
      }
      if (closestAttr(target, "data-hc-addclose")) {
        stop();
        closeAddSource();
        return;
      }
      // The backdrop, and only the backdrop: a click that landed on the
      // card is a click on the form, not a way out of it.
      if (target.getAttribute
          && target.getAttribute("data-hc-addmodal") !== null) {
        stop();
        closeAddSource();
        return;
      }
      var kindPick = closestAttr(target, "data-hc-kind");
      if (kindPick) {
        stop();
        setSourceKind(kindPick.getAttribute("data-hc-kind"));
        return;
      }
      var chatPick = closestAttr(target, "data-hc-pick-chat");
      if (chatPick) {
        stop();
        addSource("chat", chatPick.getAttribute("data-hc-pick-chat"));
        return;
      }
      var srcAdd = closestAttr(target, "data-hc-src-add");
      if (srcAdd) {
        stop();
        if (srcAdd.getAttribute("data-hc-off") !== null) return;
        var live = overviewBox && overviewBox.querySelector("[data-hc-addform]");
        var field = live && live.querySelector("[data-hc-src-label]");
        addSource(str(live && live.getAttribute("data-hc-kind-on")) || "github",
                  field ? field.value : "");
        return;
      }
    }, true);
    document.addEventListener("keydown", function (event) {
      if (!event || event.key !== "Enter") return;
      var target = event.target;
      if (target && target.getAttribute
          && (target.getAttribute("data-hc-project-name") !== null
              || target.getAttribute("data-hc-project-repo") !== null)) {
        if (event.preventDefault) event.preventDefault();
        addProject();
      }
      if (target && target.getAttribute
          && target.getAttribute("data-hc-src-label") !== null) {
        if (event.preventDefault) event.preventDefault();
        var form = overviewBox && overviewBox.querySelector("[data-hc-addform]");
        var kind = str(form && form.getAttribute("data-hc-kind-on")) || "github";
        // In the conversation list the field is a filter, not a label.
        if (kind !== "chat") addSource(kind, target.value);
      }
      // A question can run to several lines, so plain Enter stays a newline
      // and the modifier sends it -- the same bargain the objective makes.
      if (target && target.getAttribute
          && target.getAttribute("data-hc-ask-field") !== null
          && (event.metaKey || event.ctrlKey)) {
        if (event.preventDefault) event.preventDefault();
        runAsk(target.getAttribute("data-hc-ask-field"));
      }
      // The name is one line: Enter is the end of it, not a newline.
      if (target && closestByClass(target, "hc-overview-name")) {
        if (event.preventDefault) event.preventDefault();
        saveName(target);
        if (typeof target.blur === "function") target.blur();
      }
    }, true);
    document.addEventListener("focusin", function (event) {
      var target = event && event.target;
      if (target && (closestByClass(target, "hc-overview-objective")
                     || closestByClass(target, "hc-overview-about")
                     || closestByClass(target, "hc-overview-name"))) {
        target.setAttribute("data-hc-editing", "");
      }
    }, true);
    document.addEventListener("focusout", function (event) {
      var target = event && event.target;
      if (target && closestByClass(target, "hc-overview-objective")) saveObjective(target);
      else if (target && closestByClass(target, "hc-overview-about")) saveAbout(target);
      else if (target && closestByClass(target, "hc-overview-name")) saveName(target);
    }, true);
    document.addEventListener("input", function (event) {
      var target = event && event.target;
      if (target && (closestByClass(target, "hc-overview-objective")
                     || closestByClass(target, "hc-overview-about"))) {
        fitObjective(target);
      }
      if (target && target.getAttribute
          && target.getAttribute("data-hc-src-label") !== null) {
        var form = overviewBox && overviewBox.querySelector("[data-hc-addform]");
        if (form && form.getAttribute("data-hc-kind-on") === "chat") {
          renderSourceChats(target.value);
        }
      }
    }, true);
    document.addEventListener("keydown", function (event) {
      if (!event) return;
      var target = event.target;
      if (event.key === "Escape") {
        if (projectMenuShown()) { closeProjectMenu(); return; }
        // The dialog is the innermost thing open, so it is the first thing
        // Escape takes -- not the page under it.
        if (addSourceShown()) { closeAddSource(); return; }
        if (target && closestByClass(target, "hc-overview-objective")) {
          saveObjective(target);
          if (typeof target.blur === "function") target.blur();
          return;
        }
        if (target && closestByClass(target, "hc-overview-name")) {
          saveName(target);
          if (typeof target.blur === "function") target.blur();
          return;
        }
        if (overviewShown()) closeOverview();
        return;
      }
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)
          && target && closestByClass(target, "hc-overview-objective")) {
        if (event.preventDefault) event.preventDefault();
        saveObjective(target);
        if (typeof target.blur === "function") target.blur();
      }
    }, true);
  }

  // --- the settings panel, behind the header gear -------------------------
  // The controls that govern the banners live here. The center is a list of
  // what the builder did; a setting is not an event, so it does not sit at
  // the foot of that list. Sections are added here as the page grows
  // settings; notifications is the first.

  function settingsPanelShown() {
    return !!(settingsPanelBox && inLiveDocument(settingsPanelBox));
  }

  // Which tab the panel opens on. Kept across openings within a session:
  // somebody who came here for the invite code is likely coming back for it.
  var settingsTab = "account";

  function setSettingsTab(which) {
    settingsTab = str(which) || "account";
    if (!settingsPanelBox) return false;
    kids(settingsPanelBox).forEach(function (child) {
      if (String(child.className).indexOf("hc-settings-tabs") >= 0) {
        kids(child).forEach(function (tab) {
          if (tab.getAttribute("data-hc-settings-tab") === settingsTab) {
            tab.setAttribute("data-hc-on", "");
          } else {
            tab.removeAttribute("data-hc-on");
          }
        });
        return;
      }
      var mine = child.getAttribute && child.getAttribute("data-hc-tab");
      if (!mine) return;
      if (mine === settingsTab) child.setAttribute("data-hc-on", "");
      else child.removeAttribute("data-hc-on");
    });
    return true;
  }

  function settingsPanelNode() {
    var box = document.createElement("div");
    box.className = "hc-settings-panel";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-label", "Settings");
    var head = document.createElement("div");
    head.className = "hc-settings-head";
    var name = document.createElement("span");
    name.textContent = "Settings";
    head.appendChild(name);
    var close = document.createElement("span");
    close.className = "hc-settings-act";
    close.setAttribute("role", "button");
    close.setAttribute("aria-label", "Close settings");
    close.textContent = "×";
    head.appendChild(close);
    box.appendChild(head);
    // Four sections in one column was a scroll, and the two that matter --
    // the account and what is shared from it -- were below the fold under
    // a banner timeout. One tab at a time, the account first because
    // nothing else in here works until it is signed in.
    var tabs = document.createElement("div");
    tabs.className = "hc-settings-tabs";
    [["account", "Account"], ["alerts", "Alerts"], ["sharing", "Sharing"],
     ["cloud", "Cloud"], ["data", "Data"]].forEach(function (spec) {
      var tab = document.createElement("span");
      tab.className = "hc-settings-tab";
      tab.setAttribute("data-hc-settings-tab", spec[0]);
      tab.setAttribute("role", "button");
      if (spec[0] === settingsTab) tab.setAttribute("data-hc-on", "");
      tab.textContent = spec[1];
      tabs.appendChild(tab);
    });
    box.appendChild(tabs);
    var text = function (words) {
      var s = document.createElement("span");
      s.textContent = words;
      return s;
    };
    var sec = document.createElement("div");
    sec.className = "hc-settings-sec";
    sec.setAttribute("data-hc-settings-sec", "notifications");
    sec.setAttribute("data-hc-tab", "alerts");
    var sh = document.createElement("div");
    sh.className = "hc-settings-sec-head";
    sh.textContent = "Notifications";
    sec.appendChild(sh);
    var l1 = document.createElement("label");
    var c1 = document.createElement("input");
    c1.type = "checkbox";
    c1.setAttribute("type", "checkbox");
    c1.setAttribute("data-hc-alert-set", "banners");
    l1.appendChild(c1);
    l1.appendChild(text("Show a banner when a TODO finishes, fails, or asks"));
    sec.appendChild(l1);
    var l2 = document.createElement("label");
    l2.appendChild(text("Banner stays for"));
    var n2 = document.createElement("input");
    n2.type = "number";
    n2.setAttribute("type", "number");
    n2.setAttribute("min", String(ALERT_SECONDS_MIN));
    n2.setAttribute("max", String(ALERT_SECONDS_MAX));
    n2.setAttribute("step", "1");
    n2.setAttribute("data-hc-alert-set", "seconds");
    l2.appendChild(n2);
    l2.appendChild(text("seconds"));
    sec.appendChild(l2);
    box.appendChild(sec);

    // Supabase: where this workspace's projects are sent. The URL and the
    // anon key are typed and kept; the password is typed and is not -- it
    // buys a token on its way past and the field is emptied behind it.
    // Where this workspace's projects go, and who they go as: two
    // questions with two answers, and the first has to be settled before
    // the second can be asked. A tab each.
    var cloud = document.createElement("div");
    cloud.className = "hc-settings-sec";
    cloud.setAttribute("data-hc-settings-sec", "cloud");
    cloud.setAttribute("data-hc-tab", "cloud");
    var ch = document.createElement("div");
    ch.className = "hc-settings-sec-head";
    ch.textContent = "Supabase";
    cloud.appendChild(ch);
    var sb = document.createElement("div");
    sb.className = "hc-settings-sec";
    sb.setAttribute("data-hc-settings-sec", "supabase");
    sb.setAttribute("data-hc-tab", "account");
    var sbh = document.createElement("div");
    sbh.className = "hc-settings-sec-head";
    sbh.textContent = "Account";
    sb.appendChild(sbh);
    var field = function (label, name, type, placeholder, host) {
      var wrap = document.createElement("label");
      wrap.className = "hc-settings-field";
      wrap.appendChild(text(label));
      var input = document.createElement("input");
      input.type = type;
      input.setAttribute("type", type);
      input.setAttribute("data-hc-sb", name);
      input.setAttribute("placeholder", placeholder);
      input.setAttribute("spellcheck", "false");
      input.setAttribute("autocomplete", "off");
      input.setAttribute("autocapitalize", "off");
      wrap.appendChild(input);
      (host || sb).appendChild(wrap);
      return input;
    };
    field("Project URL", "url", "text", "https://your-ref.supabase.co", cloud);
    field("Anon (public) key", "anon_key", "text", "eyJ…", cloud);
    var keyHint = document.createElement("div");
    keyHint.className = "hc-settings-hint";
    keyHint.textContent = "Project Settings → API. Use the anon key, never"
      + " the service key.";
    cloud.appendChild(keyHint);
    var saveRow = document.createElement("div");
    saveRow.className = "hc-settings-row";
    var save = document.createElement("span");
    save.className = "hc-settings-btn";
    save.setAttribute("role", "button");
    save.setAttribute("data-hc-sb-do", "save");
    save.textContent = "Connect";
    saveRow.appendChild(save);
    cloud.appendChild(saveRow);
    // The connection's own line, so the Cloud tab says whether it holds
    // keys without sending the reader to another tab to find out.
    var cloudSay = document.createElement("div");
    cloudSay.className = "hc-settings-say";
    cloudSay.setAttribute("data-hc-cloud-say", "");
    cloudSay.textContent = "checking…";
    cloud.appendChild(cloudSay);
    box.appendChild(cloud);
    field("Display name", "display_name", "text", "David");
    var nameHint = document.createElement("div");
    nameHint.className = "hc-settings-hint";
    nameHint.textContent = "What your goals are signed with in a shared"
      + " workspace.";
    sb.appendChild(nameHint);
    field("Email", "email", "text", "you@example.com");
    var pw = field("Password", "password", "password", "not stored");
    pw.setAttribute("autocomplete", "new-password");
    var pwHint = document.createElement("div");
    pwHint.className = "hc-settings-hint";
    pwHint.textContent = "Exchanged once for a token. Never written to disk"
      + " and never sent back to this page.";
    sb.appendChild(pwHint);
    var authRow = document.createElement("div");
    authRow.className = "hc-settings-row";
    var signIn = document.createElement("span");
    signIn.className = "hc-settings-btn";
    signIn.setAttribute("role", "button");
    signIn.setAttribute("data-hc-sb-do", "login");
    signIn.textContent = "Sign in";
    authRow.appendChild(signIn);
    var signOut = document.createElement("span");
    signOut.className = "hc-settings-btn";
    signOut.setAttribute("data-hc-quiet", "");
    signOut.setAttribute("role", "button");
    signOut.setAttribute("data-hc-sb-do", "logout");
    signOut.textContent = "Sign out";
    authRow.appendChild(signOut);
    sb.appendChild(authRow);
    var say = document.createElement("div");
    say.className = "hc-settings-say";
    say.setAttribute("data-hc-sb-say", "");
    say.textContent = "checking…";
    sb.appendChild(say);
    box.appendChild(sb);

    // This project, and who else may see it. Sending it up and minting an
    // invitation are both account work -- they need the sign-in typed
    // directly above, and neither belongs on a page about the goals.
    var proj = document.createElement("div");
    proj.className = "hc-settings-sec hc-settings-sync";
    proj.setAttribute("data-hc-settings-sec", "project");
    proj.setAttribute("data-hc-tab", "sharing");
    var ph = document.createElement("div");
    ph.className = "hc-settings-sec-head";
    ph.textContent = "This project";
    proj.appendChild(ph);
    var sendRow = document.createElement("div");
    sendRow.className = "hc-settings-row";
    var sendBtn = document.createElement("span");
    sendBtn.className = "hc-settings-btn";
    sendBtn.setAttribute("role", "button");
    sendBtn.setAttribute("data-hc-sync", "");
    sendBtn.setAttribute("data-hc-off", "");
    sendBtn.textContent = "Save to Supabase";
    sendRow.appendChild(sendBtn);
    proj.appendChild(sendRow);
    var syncSay = document.createElement("div");
    syncSay.className = "hc-settings-say";
    syncSay.setAttribute("data-hc-sync-say", "");
    syncSay.textContent = "checking\u2026";
    proj.appendChild(syncSay);
    // Reader or editor, chosen before the code is minted: the role is
    // baked into the invitation, so it cannot be decided afterwards.
    var inviteRow = document.createElement("div");
    inviteRow.className = "hc-settings-row";
    var share = document.createElement("span");
    share.className = "hc-settings-btn";
    share.setAttribute("data-hc-quiet", "");
    share.setAttribute("role", "button");
    share.setAttribute("data-hc-share", "");
    share.setAttribute("data-hc-off", "");
    share.textContent = "Get invite code";
    inviteRow.appendChild(share);
    var role = document.createElement("span");
    role.className = "hc-settings-role";
    role.setAttribute("data-hc-share-role", "reader");
    role.setAttribute("role", "button");
    role.setAttribute("title", "Click to change what this invite grants");
    role.textContent = "as reader";
    inviteRow.appendChild(role);
    proj.appendChild(inviteRow);
    // Where a freshly minted code is shown. It is shown once and nowhere
    // else: only its hash is kept, here or in Postgres, so a reader who
    // loses it mints another rather than looking it up.
    var codeWrap = document.createElement("div");
    codeWrap.className = "hc-settings-code";
    codeWrap.setAttribute("data-hc-code", "");
    var codeBox = document.createElement("textarea");
    codeBox.className = "hc-settings-code-box";
    codeBox.setAttribute("readonly", "readonly");
    codeBox.setAttribute("rows", "3");
    codeBox.setAttribute("spellcheck", "false");
    codeBox.setAttribute("data-hc-code-box", "");
    codeWrap.appendChild(codeBox);
    var codeRow = document.createElement("div");
    codeRow.className = "hc-settings-row";
    var copy = document.createElement("span");
    copy.className = "hc-settings-btn";
    copy.setAttribute("data-hc-quiet", "");
    copy.setAttribute("role", "button");
    copy.setAttribute("data-hc-code-copy", "");
    copy.textContent = "Copy";
    codeRow.appendChild(copy);
    codeWrap.appendChild(codeRow);
    var warn = document.createElement("div");
    warn.className = "hc-settings-hint";
    warn.textContent = "Whoever redeems it signs in first, and joins under"
      + " their own account. Shown once \u2014 copy it now.";
    codeWrap.appendChild(warn);
    proj.appendChild(codeWrap);
    var shares = document.createElement("div");
    shares.className = "hc-settings-shares";
    shares.setAttribute("data-hc-shares", "");
    proj.appendChild(shares);
    box.appendChild(proj);

    // What this machine keeps: one JSON file per project directory,
    // holding every goal of every chat started there. Read rather than
    // written -- the workspace rewrites it on every goal save.
    var data = document.createElement("div");
    data.className = "hc-settings-sec";
    data.setAttribute("data-hc-settings-sec", "data");
    data.setAttribute("data-hc-tab", "data");
    var dh = document.createElement("div");
    dh.className = "hc-settings-sec-head";
    dh.textContent = "Project record";
    data.appendChild(dh);
    var record = document.createElement("div");
    record.setAttribute("data-hc-record", "");
    record.appendChild(el("div", "hc-settings-hint", "reading\u2026"));
    data.appendChild(record);
    box.appendChild(data);

    // Joining someone else's project. A code carries where to go as well
    // as the token, so this works before Supabase is configured at all --
    // the only thing it cannot do without is an account to join to.
    var join = document.createElement("div");
    join.className = "hc-settings-sec";
    join.setAttribute("data-hc-settings-sec", "shared");
    join.setAttribute("data-hc-tab", "sharing");
    var jh = document.createElement("div");
    jh.className = "hc-settings-sec-head";
    jh.textContent = "Shared workspaces";
    join.appendChild(jh);
    var jwrap = document.createElement("label");
    jwrap.className = "hc-settings-field";
    jwrap.appendChild(text("Invite code"));
    var jin = document.createElement("input");
    jin.type = "text";
    jin.setAttribute("type", "text");
    jin.setAttribute("data-hc-join", "");
    jin.setAttribute("placeholder", "hcjoin1_…");
    jin.setAttribute("spellcheck", "false");
    jin.setAttribute("autocomplete", "off");
    jwrap.appendChild(jin);
    join.appendChild(jwrap);
    var jrow = document.createElement("div");
    jrow.className = "hc-settings-row";
    var jbtn = document.createElement("span");
    jbtn.className = "hc-settings-btn";
    jbtn.setAttribute("role", "button");
    jbtn.setAttribute("data-hc-sb-do", "join");
    jbtn.textContent = "Join";
    jrow.appendChild(jbtn);
    join.appendChild(jrow);
    var jlist = document.createElement("div");
    jlist.className = "hc-settings-sec";
    jlist.setAttribute("data-hc-shared-list", "");
    jlist.style.padding = "0";
    join.appendChild(jlist);
    var jsay = document.createElement("div");
    jsay.className = "hc-settings-say";
    jsay.setAttribute("data-hc-join-say", "");
    join.appendChild(jsay);
    box.appendChild(join);
    return box;
  }

  // The panel is drawn once and filled from the server: the key is shown
  // back so a reader can see which project they are pointed at, the
  // password field is left empty always.
  function settingsSupabaseFill(state) {
    if (!settingsPanelBox) return;
    var at = function (name) {
      return settingsPanelBox.querySelector('[data-hc-sb="' + name + '"]');
    };
    var say = settingsPanelBox.querySelector("[data-hc-sb-say]");
    var cloudSay = settingsPanelBox.querySelector("[data-hc-cloud-say]");
    var onCloud = function (words, bad) {
      if (!cloudSay) return;
      cloudSay.textContent = words;
      if (bad) cloudSay.setAttribute("data-hc-bad", "");
      else cloudSay.removeAttribute("data-hc-bad");
    };
    if (!say) return;
    if (!state || !state.ok) {
      say.setAttribute("data-hc-bad", "");
      say.textContent = "could not read the Supabase settings";
      onCloud("could not read the Supabase settings", true);
      return;
    }
    say.removeAttribute("data-hc-bad");
    // The keys are shown back so a reader can see which project this points
    // at; the URL and the key are what this tab is about.
    var url = at("url");
    if (url && document.activeElement !== url) url.value = str(state.url);
    var key = at("anon_key");
    if (key && document.activeElement !== key) key.value = str(state.anon_key);
    onCloud(state.configured
      ? "connected to " + (str(state.url) || "your project")
      : "not connected · paste the project URL and anon key, then Connect");
    var email = at("email");
    if (email && !email.value) email.value = str(state.email);
    var named = at("display_name");
    if (named && document.activeElement !== named) {
      named.value = str(state.display_name);
    }
    var pw = at("password");
    if (pw) pw.value = "";
    if (!state.configured) {
      say.textContent = "not connected · paste the project URL and anon key,"
        + " then Connect";
    } else if (!state.signed_in) {
      say.textContent = "connected · sign in to send projects";
    } else {
      say.textContent = "signed in as " + str(state.email || "you")
        + " · the project overview can send this project";
    }
  }

  // The projects this account has been let into. Opening one is a second
  // window on a second port -- this one stays the reader's own.
  function renderSharedList(rows) {
    if (!settingsPanelBox) return;
    var host = settingsPanelBox.querySelector("[data-hc-shared-list]");
    if (!host) return;
    wipe(host);
    array(rows).forEach(function (row) {
      if (!row || !row.id) return;
      var line = el("div", "hc-settings-row");
      var open = el("span", "hc-settings-btn", "Open");
      open.setAttribute("data-hc-quiet", "");
      open.setAttribute("role", "button");
      open.setAttribute("data-hc-open-shared", str(row.id));
      line.appendChild(open);
      line.appendChild(el("span", "hc-settings-say",
                          str(row.name) || "untitled project"));
      host.appendChild(line);
    });
  }

  function loadSharedList() {
    return post({ op: "shared_projects" }).then(function (result) {
      if (result && result.ok) renderSharedList(result.projects);
      return result;
    });
  }

  function joinSay(words, bad) {
    if (!settingsPanelBox) return;
    var node = settingsPanelBox.querySelector("[data-hc-join-say]");
    if (!node) return;
    node.textContent = words || "";
    if (bad) node.setAttribute("data-hc-bad", "");
    else node.removeAttribute("data-hc-bad");
  }

  function openShared(projectId) {
    joinSay("opening…");
    return post({ op: "open_shared", project_id: projectId })
      .then(function (result) {
        if (!result || !result.ok || !result.url) {
          joinSay((result && result.error) || "could not open it", true);
          return null;
        }
        joinSay("opened at " + result.url);
        try { window.open(result.url, "_blank"); } catch (e) {}
        return result.url;
      });
  }

  function joinShared() {
    if (!settingsPanelBox) return false;
    var field = settingsPanelBox.querySelector("[data-hc-join]");
    var code = field ? String(field.value || "").trim() : "";
    if (!code) { joinSay("paste the invite code first", true); return false; }
    joinSay("joining…");
    post({ op: "redeem_invite", code: code }).then(function (result) {
      if (!result || !result.ok) {
        joinSay((result && result.error) || "that invitation is not open", true);
        return;
      }
      if (field) field.value = "";
      if (result.already) {
        // Redeeming your own project's code joins nobody -- the counter on
        // that invite stays at nothing, and saying "joined" here made the
        // list underneath look wrong.
        joinSay("that is your own project — nothing to join"
                + (result.url ? " · opening it" : ""));
      } else {
        joinSay("joined " + (str(result.name) || "the project")
                + " as " + (str(result.role) || "reader")
                + (result.url ? " · opening…" : ""));
      }
      loadSharedList();
      if (result.url) { try { window.open(result.url, "_blank"); } catch (e) {} }
    });
    return true;
  }

  function settingsSupabaseLoad() {
    return fetchJSON("/api/supabase").then(function (result) {
      settingsSupabaseFill(result);
      // The sending and sharing controls sit in this panel now, and read
      // the same state the fields above do.
      renderSyncStatus(result);
      if (result && result.ok && result.signed_in) loadSharedList();
      return result;
    });
  }

  function settingsSupabaseDo(what) {
    if (!settingsPanelBox) return false;
    var at = function (name) {
      var node = settingsPanelBox.querySelector('[data-hc-sb="' + name + '"]');
      return node ? String(node.value || "").trim() : "";
    };
    var say = settingsPanelBox.querySelector("[data-hc-sb-say]");
    var button = settingsPanelBox.querySelector(
      '[data-hc-sb-do="' + what + '"]');
    if (button && button.hasAttribute("data-hc-busy")) return false;
    if (button) button.setAttribute("data-hc-busy", "");
    if (say) { say.removeAttribute("data-hc-bad"); say.textContent = "working…"; }
    var body = { op: "supabase_logout" };
    if (what === "save") {
      body = { op: "set_supabase_config", url: at("url"),
               anon_key: at("anon_key"), email: at("email"),
               display_name: at("display_name") };
    } else if (what === "login") {
      var node = settingsPanelBox.querySelector('[data-hc-sb="password"]');
      body = { op: "supabase_login", email: at("email"),
               password: node ? String(node.value || "") : "",
               display_name: at("display_name") };
      // Out of the page the moment it is on its way: the reply never
      // carries it back, and nothing redraws it.
      if (node) node.value = "";
    }
    post(body).then(function (result) {
      var live = settingsPanelBox
        && settingsPanelBox.querySelector('[data-hc-sb-do="' + what + '"]');
      if (live) live.removeAttribute("data-hc-busy");
      var line = settingsPanelBox
        && settingsPanelBox.querySelector("[data-hc-sb-say]");
      if (!result || !result.ok) {
        if (line) {
          line.setAttribute("data-hc-bad", "");
          line.textContent = (result && result.error) || "that did not work";
        }
        return;
      }
      settingsSupabaseFill(result);
      renderSyncStatus(result);
    });
    return true;
  }

  function renderSettingsPanel() {
    if (!settingsPanelShown()) return false;
    var cur = alertSettings();
    var inputs = alertInputs(settingsPanelBox);
    for (var i = 0; i < inputs.length; i++) {
      var key = str(inputs[i].getAttribute("data-hc-alert-set"));
      if (key === "banners") inputs[i].checked = !!cur.banners;
      else if (key === "seconds") inputs[i].value = String(cur.seconds);
    }
    return true;
  }

  // Under the gear that opened it, measured when it opens. The panel used
  // to hang from --hc-top -- the bottom of the whole bar stack -- which put
  // it two rows below the icon it belongs to.
  function placeSettingsPanel() {
    var root = document.documentElement;
    var gear = document.querySelector(".hc-gear");
    if (!root || !root.style || typeof root.style.setProperty !== "function"
        || !gear || !gear.getBoundingClientRect) return false;
    var box = gear.getBoundingClientRect();
    if (!box || typeof box.bottom !== "number") return false;
    var under = Math.round(Number(box.bottom) || 0);
    if (under > 0) root.style.setProperty("--hc-settings-top", (under + 6) + "px");
    var width = Number((document.documentElement || {}).clientWidth)
      || Number(window.innerWidth) || 0;
    if (width > 0 && typeof box.right === "number") {
      root.style.setProperty("--hc-settings-right",
        Math.max(8, Math.round(width - box.right)) + "px");
    }
    return true;
  }

  function openSettingsPanel() {
    if (settingsPanelShown()) return settingsPanelBox;
    ensureAlertStyles();
    bindAlerts();
    placeSettingsPanel();
    settingsPanelBox = settingsPanelNode();
    (document.body || document.documentElement).appendChild(settingsPanelBox);
    ensureProjectStyles();
    syncProjectTheme(settingsPanelBox);
    setSettingsTab(settingsTab);
    renderSettingsPanel();
    loadProjectJson();
    // Whether it is connected, and as whom: read when the panel opens, not
    // held from the last time it did.
    settingsSupabaseLoad();
    renderGear();
    return settingsPanelBox;
  }

  function closeSettingsPanel() {
    if (!settingsPanelShown()) return false;
    settingsPanelBox.parentNode.removeChild(settingsPanelBox);
    renderGear();
    return true;
  }

  function toggleSettingsPanel() {
    return settingsPanelShown() ? closeSettingsPanel() : !!openSettingsPanel();
  }

  // The gear sits in the header after the bell, in the slot the template
  // leaves for it. Drawn by the same sweep as the bell, so a re-render of
  // the header that drops the node gets it back within the tick.
  function renderGear() {
    if (serverState.scope !== "chat") return false;
    var slot = document.querySelector(".hc-settings");
    if (!slot) return false;
    ensureAlertStyles();
    bindAlerts();
    var gear = slot.querySelector(".hc-gear");
    if (!gear) {
      gear = document.createElement("span");
      gear.className = "hc-gear";
      gear.setAttribute("role", "button");
      gear.setAttribute("aria-label", "Settings");
      gear.title = "Settings";
      gear.innerHTML = GEAR_ICON;
      slot.appendChild(gear);
    }
    var open = settingsPanelShown();
    // The theme button sits beside this gear and does not close the panel.
    if (open) syncProjectTheme(settingsPanelBox);
    var changed = false;
    if (open && gear.getAttribute("data-hc-gear-open") === null) {
      gear.setAttribute("data-hc-gear-open", ""); changed = true;
    } else if (!open && gear.getAttribute("data-hc-gear-open") !== null) {
      gear.removeAttribute("data-hc-gear-open"); changed = true;
    }
    return changed;
  }

  // --- the hand-off, before the bell ----------------------------------------
  // One click asks the server for the workspace as markdown -- every goal's
  // notes, prompt and TODO rows with their build states, the repository's
  // git and GitHub metadata, under a prompt for a teammate's agent -- and
  // puts it on the clipboard. The server keeps a copy as handoff.md beside
  // the goals. A clipboard that refuses (no focus, no permission) gets the
  // file downloaded instead, so the click always yields the document.

  var handoffState = "";           // "", busy, copied, failed
  var handoffTimer = null;
  var handoffLast = null;          // the last document fetched, for tests

  function handoffSay(state) {
    handoffState = state;
    clearTimeout(handoffTimer);
    if (state === "copied" || state === "failed") {
      handoffTimer = setTimeout(function () {
        handoffState = "";
        renderHandoff();
      }, 1800);
    }
    renderHandoff();
  }

  function handoffDownload(text, name) {
    try {
      var blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var a = document.createElement("a");
      a.href = url;
      a.download = name || "hc-handoff.md";
      a.style.display = "none";
      (document.body || document.documentElement).appendChild(a);
      a.click();
      setTimeout(function () {
        if (a.parentNode) a.parentNode.removeChild(a);
        try { URL.revokeObjectURL(url); } catch (e) {}
      }, 0);
      return true;
    } catch (e) {
      return false;
    }
  }

  function handoffToClipboard(text) {
    // Answers with a promise of whether the text is on the clipboard. The
    // async clipboard first; the hidden-textarea copy when that is missing
    // or refuses. Safari wants the write inside the click: ClipboardItem
    // with a promise of the text lets the write start in the gesture and
    // the fetch resolve it, and is tried first when it exists.
    var fallback = function () {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      (document.body || document.documentElement).appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy") === true; } catch (e) { ok = false; }
      if (ta.parentNode) ta.parentNode.removeChild(ta);
      return ok;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; },
                                                      function () { return fallback(); });
    }
    return Promise.resolve(fallback());
  }

  function fetchHandoff() {
    return fetch("/api/handoff", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (body) {
        if (!(body && body.ok && typeof body.markdown === "string")) {
          throw new Error((body && body.error) || "no hand-off");
        }
        handoffLast = body;
        return body;
      });
  }

  function copyHandoff() {
    if (handoffState === "busy") return null;
    handoffSay("busy");
    var onClipboard;
    var fetched = fetchHandoff();
    if (typeof ClipboardItem === "function" && navigator.clipboard
        && navigator.clipboard.write) {
      // Started inside the click, resolved by the fetch.
      var textPromise = fetched.then(function (body) {
        return new Blob([body.markdown], { type: "text/plain" });
      });
      var item;
      try { item = new ClipboardItem({ "text/plain": textPromise }); } catch (e) { item = null; }
      onClipboard = item
        ? navigator.clipboard.write([item]).then(function () { return true; },
                                                function () { return false; })
        : Promise.resolve(false);
      onClipboard = onClipboard.then(function (ok) {
        return ok ? fetched.then(function () { return true; })
                  : fetched.then(function (body) { return handoffToClipboard(body.markdown); });
      });
    } else {
      onClipboard = fetched.then(function (body) { return handoffToClipboard(body.markdown); });
    }
    return onClipboard.then(function (ok) {
      if (ok) { handoffSay("copied"); return true; }
      var body = handoffLast;
      if (body) handoffDownload(body.markdown, body.filename);
      handoffSay("failed");
      return false;
    }, function () {
      handoffSay("failed");
      return false;
    });
  }

  // The hand-off button copied the whole workspace to the clipboard from
  // the header. Taken off: it sat beside controls that do small things and
  // did a very large one, with no way to tell before pressing it. The
  // /api/handoff route and handoff.md are untouched.
  function renderHandoff() {
    var slot = document.querySelector(".hc-handoff");
    if (slot) wipe(slot);
    return false;
  }

  function renderHandoffDisabled() {
    if (serverState.scope !== "chat") return false;
    var slot = document.querySelector(".hc-handoff");
    if (!slot) return false;
    ensureAlertStyles();
    bindAlerts();
    var btn = slot.querySelector(".hc-handoff-btn");
    if (!btn) {
      btn = document.createElement("span");
      btn.className = "hc-handoff-btn";
      btn.setAttribute("role", "button");
      btn.setAttribute("aria-label", "Hand off to a teammate");
      btn.title = HANDOFF_TITLE;
      btn.innerHTML = HANDOFF_ICON;
      var said = document.createElement("span");
      said.className = "hc-handoff-said";
      btn.appendChild(said);
      slot.appendChild(btn);
    }
    var changed = false;
    var was = btn.getAttribute("data-hc-handoff");
    if (handoffState && was !== handoffState) {
      btn.setAttribute("data-hc-handoff", handoffState); changed = true;
    } else if (!handoffState && was !== null) {
      btn.removeAttribute("data-hc-handoff"); changed = true;
    }
    var label = btn.querySelector(".hc-handoff-said");
    var words = handoffState ? (HANDOFF_SAYS[handoffState] || "") : "";
    if (label && label.textContent !== words) { label.textContent = words; changed = true; }
    return changed;
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



  // --- the launch skin: one chat workspace, three columns ------------------
  // Everything below is gated on chat scope and on a single root attribute,
  // so a global vault renders exactly as it did. The artifact keeps owning
  // state and rendering; this only names its containers (through the same
  // template patch the rest of the bridge uses) and dresses them.

  var LAUNCH_CSS = [
      // A darker, flatter palette than the artifact's own dark theme. Only
      // the greys move: the accent stays the artifact's, so every control
      // that was accented still is, in both themes.
      "[data-hc-launch] .hc[data-dark=\"true\"]{--bg:#0d1117;--panel:#0d1117;--panel2:#161b22;--ink:#e6edf3;--mut:#8b949e;--fnt:#6e7681;--bd:#21262d;--bd2:#30363d;--line:#21262d;--hov:#161b22;--dtxt:#c9d1d9}",
      // On the root, not on .hc: the banner is parented on <body>, outside
      // the artifact's subtree, so anything declared inside .hc never
      // reaches it. The theme is mirrored onto the root for the same reason.
      // --hc-top is the header's height: the columns take every pixel under
      // it. --hc-left/--hc-right are the rail widths; the bridge writes them
      // on the root when the reader drags a divider, and the defaults here
      // are the widths the shell shipped with.
      "[data-hc-launch]{--hc-ok:#1a7f37;--hc-okbg:#eaf6ec;--hc-okbd:#b7dfc2;--hc-warn:#9a6700;--hc-noticetxt:#3d5c46;--hc-top:37px;--hc-left:300px;--hc-right:330px}",
      "[data-hc-launch][data-hc-theme=\"dark\"]{--hc-ok:#3fb950;--hc-okbg:#0f2417;--hc-okbd:#1c5030;--hc-warn:#d29922;--hc-noticetxt:#8aa495}",
      // A banner is not an overlay: it takes its own line, and the columns
      // give it back when it goes.
      "[data-hc-launch][data-hc-notice]{--hc-top:71px}",
      "[data-hc-launch][data-hc-notice] .hc>div:nth-child(2){padding-top:34px!important}",
      // The page is the workspace: it fills the window and does not scroll
      // as a whole -- each column scrolls in its own right, the way the
      // screenshots read.
      // Full bleed: no outer padding, so the columns meet the window on
      // every side and each other on a shared 1px line.
      "[data-hc-launch] .hc>div:nth-child(2){max-width:none!important;padding:0!important}",
      // Header bar: brand, status pills, panel toggles, session. A fixed
      // height, so the columns can be sized against it exactly.
      // Sticky, and above the columns: the pills are pinned to the viewport
      // top, so the bar they sit in has to stay there too when the page
      // scrolls -- or they float off it.
      "[data-hc-launch] .hc>div:first-child{position:sticky;top:0;z-index:19;background:var(--bg);height:var(--hc-top);box-sizing:border-box;padding:0 16px!important;align-items:center!important;border-bottom:1px solid var(--bd)}",
      "[data-hc-launch][data-hc-notice] .hc>div:first-child{height:37px}",
      // The product name is the one serif on the page; the marker before
      // it and everything after it stay in the workspace's monospace.
      "[data-hc-launch] .hc-brand{font:600 15px Georgia,'Iowan Old Style','Times New Roman',serif!important;letter-spacing:.1px;line-height:1}",
      "[data-hc-launch] .hc-session{font:11px 'Source Code Pro',monospace;color:var(--mut)}",
      // A slot with nothing in it is still a slot: inline-flex leaves its
      // gap behind, so the header reads as if something failed to load
      // rather than as if nothing belongs there. Collapse the empties and
      // what remains closes up.
      "[data-hc-launch] .hc-session:empty,[data-hc-launch] .hc-chats:empty,[data-hc-launch] .hc-handoff:empty{display:none!important}",
      "[data-hc-launch] [data-hc-hasnote]{position:relative}",
      "[data-hc-launch] .hc-analyzing{position:absolute;right:10px;bottom:7px;z-index:4;font:10px 'Source Code Pro',monospace;color:var(--fnt);white-space:nowrap;pointer-events:none;background:var(--bg);padding:2px 4px;border-radius:2px}",
      "[data-hc-launch] .hc-analyzing:empty{display:none}",
      "[data-hc-launch] .hc-analyzing:not(:empty)::before{content:'\\25cc';margin-right:5px;font-size:8px;vertical-align:1px}",
      // Three states, three marks: reading, settled, broken. A backlog with
      // nobody reading draws nothing at all, so it needs no mark.
      "[data-hc-launch] .hc-analyzing[data-hc-mood=\"done\"]::before{content:'\\25cf';color:var(--hc-ok)}",
      "[data-hc-launch] .hc-analyzing[data-hc-mood=\"bad\"]{color:var(--del,#a12d2d)}",
      "[data-hc-launch] .hc-session:not(:empty)::before{content:'\\25cf';color:var(--hc-ok);margin-right:6px;font-size:9px;vertical-align:1px}",
      // "saved" and a tick, ahead of the rail toggles: the stamp is the
      // header's first word on the right, and the time it stands for is
      // its title. A clock in the header changed every minute and said
      // nothing the tick does not.
      "[data-hc-launch] .hc-updated{order:-2;color:var(--fnt);margin-right:4px}",
      // The title row loses the page heading -- a chat workspace has one
      // page, already named in the header -- and its status pills move up
      // INTO the header, so the row itself takes no height. The row is not
      // a child of the header (the artifact renders it in the body), and
      // moving the node would be undone by the next render, so it is lifted
      // by position instead: fixed at the top, just after the brand. The
      // bridge measures the brand and writes --hc-pills-left.
      "[data-hc-launch] .hc-titlerow{position:fixed;top:0;left:var(--hc-pills-left,120px);height:37px;margin:0;padding:0!important;align-items:center!important;z-index:20}",
      "[data-hc-launch] .hc-titlerow>div:first-child{display:none}",
      // Read-only: the controls that write are taken off the page rather
      // than left to fail. Anything that only reads -- folding, selecting,
      // the search, the panes -- is untouched.
      "[data-hc-launch][data-hc-readonly] [title=\"Delete goal\"]{display:none!important}",
      "[data-hc-launch][data-hc-readonly] .hc-todo-build,[data-hc-launch][data-hc-readonly] .hc-todo-cancel,[data-hc-launch][data-hc-readonly] .hc-todo-copy,[data-hc-launch][data-hc-readonly] .hc-todo-reopen,[data-hc-launch][data-hc-readonly] .hc-todo-reply,[data-hc-launch][data-hc-readonly] .hc-rail-select{display:none!important}",
      "[data-hc-launch][data-hc-readonly] .hc-src-rm,[data-hc-launch][data-hc-readonly] .hc-overview-objective{display:none!important}",
      // A guest may watch the build -- the log is the owner's account of what
      // their machine is doing -- but not open a window on that machine.
      "[data-hc-launch][data-hc-readonly] [data-hc-todo-term]{display:none!important}",
      // A caret in a field nothing will save is the cruellest part of it.
      "[data-hc-launch][data-hc-readonly] textarea,[data-hc-launch][data-hc-readonly] [contenteditable]{caret-color:transparent!important;cursor:default!important}",
      "[data-hc-launch] .hc-ro-note{position:fixed;left:0;right:0;bottom:0;z-index:100004;display:flex;align-items:center;justify-content:center;gap:8px;padding:5px 12px;background:var(--panel2,#f6f6f6);border-top:1px solid var(--bd,#e3e3e3);color:var(--mut,#575757);font:11px/1.5 'Source Code Pro',ui-monospace,monospace}",
      "[data-hc-launch] .hc-ro-dot{color:var(--acc,#a5492a);font-size:8px}",
      "[data-hc-launch] .hc-who{flex:none;font:600 12.5px 'Source Code Pro',monospace;color:var(--mut);white-space:nowrap}",
      // The row sits where the overview's own tabs used to: directly under
      // the header, full width. It no longer comes and goes with the
      // overview -- that was the only way back, and it was visible only
      // once you had already arrived.
      "[data-hc-launch] .hc-viewtabs{position:fixed;top:37px;left:0;right:0;z-index:19;display:flex;gap:22px;align-items:stretch;height:32px;padding:0 24px;box-sizing:border-box;background:var(--bg);border-bottom:1px solid var(--bd)}",
      "[data-hc-launch][data-hc-notice] .hc-viewtabs{top:71px}",
      // One strip, not two: the tabs say which view on the left and the
      // counts say how much of it on the right of the same 32px row. The
      // counts used to take a 42px row of bordered pills under the tabs;
      // as words in the tab row they cost no height, and the layout below
      // is measured from --hc-top, so it climbs by that row.
      "[data-hc-launch] .hc-pillbar{position:fixed;top:37px;right:24px;z-index:20;display:flex;gap:12px;align-items:center;height:32px;padding:0;box-sizing:border-box}",
      "[data-hc-launch][data-hc-notice] .hc-pillbar{top:71px}",
      "[data-hc-launch] .hc-pillbar[data-hc-hidden]{display:none}",
      "[data-hc-launch] .hc-pillbar .hc-chiprow{margin:0!important;gap:22px!important;flex-wrap:nowrap!important}",
      // Words, not pills: no box around a count, and the one in force is
      // the bold one -- the artifact's own mark for it.
      "[data-hc-launch] .hc-pillbar .hc-chip{padding:0!important;border:0!important;border-radius:0!important;background:transparent!important;letter-spacing:.4px}",
      // The count stands beside the name in a quieter colour, so the row
      // reads as four names with four numbers rather than four sentences.
      "[data-hc-launch] .hc-chip-n{font-weight:400;color:var(--fnt)}",
      "[data-hc-launch] .hc-chip[style*=\"700 11px\"] .hc-chip-n{color:var(--mut)}",
      "[data-hc-launch][data-hc-viewtabs]{--hc-top:69px}",
      "[data-hc-launch][data-hc-viewtabs] .hc>div:first-child{height:37px}",
      "[data-hc-launch][data-hc-viewtabs] .hc>div:nth-child(2){padding-top:32px!important}",
      "[data-hc-launch][data-hc-viewtabs][data-hc-overview]{--hc-top:69px}",
      "[data-hc-launch][data-hc-notice][data-hc-viewtabs]{--hc-top:103px}",
      "[data-hc-launch][data-hc-notice][data-hc-viewtabs][data-hc-overview]{--hc-top:103px}",
      "[data-hc-launch][data-hc-notice][data-hc-viewtabs] .hc>div:nth-child(2){padding-top:66px!important}",
      // One tab row, not two: the overview's own is redundant now.
      "[data-hc-launch] .hc-overview-tabs{display:none!important}",
      // With the duplicate gone the card would sit against the pillbar's
      // rule; the page needs the top margin the tab row used to give it.
      "[data-hc-launch] .hc-overview{padding-top:22px}",
      "[data-hc-launch] .hc-viewtab{cursor:pointer;user-select:none;font:600 11px 'Source Code Pro',monospace;letter-spacing:1.3px;text-transform:uppercase;color:var(--fnt);height:32px;display:flex;align-items:center;border-bottom:2px solid transparent;margin-bottom:-1px}",
      "[data-hc-launch] .hc-viewtab:hover{color:var(--ink)}",
      "[data-hc-launch] .hc-viewtab[data-hc-on]{color:var(--ink);border-bottom-color:var(--ink)}",
      "[data-hc-launch] .hc-chiprow{gap:6px!important}",
      "[data-hc-launch] .hc-chip{padding:3px 10px;border:1px solid var(--bd);border-radius:99px;background:transparent;letter-spacing:.1px}",
      "[data-hc-launch] .hc-chip:hover{border-color:var(--bd2);text-decoration:none!important}",
      // The selected chip is the one the artifact draws bold; reading its
      // own inline style is what keeps this in step with its state.
      "[data-hc-launch] .hc-chip[style*=\"700 11px\"]{background:var(--panel2);border-color:var(--bd2);color:var(--ink)!important}",
      // Three columns. The artifact's own flex row becomes the shell; the
      // prompt rail is emitted before the inspector and ordered after it,
      // so nothing has to be re-parented after a render.
      // No gap between columns and no border radius: each rail keeps one
      // border, the one it shares with the document, and the document
      // itself has none -- so between any two columns there is exactly one
      // 1px line, and none against the window.
      "[data-hc-launch] .hc-shell{gap:0!important;align-items:stretch!important;margin-top:0!important}",
      "[data-hc-launch] .hc-rail-left{position:relative;flex:0 0 var(--hc-left)!important;height:calc(100vh - var(--hc-top))!important;padding:0 0 6px!important;border-width:0 1px 0 0!important;border-radius:0!important}",
      "[data-hc-launch] .hc-main{flex:1 1 auto!important;order:2;height:calc(100vh - var(--hc-top))!important;top:0!important;border:0!important;border-radius:0!important;padding:14px 20px 18px!important}",
      "[data-hc-launch] .hc-rail-right{position:relative;order:3;flex:0 0 var(--hc-right);display:flex;flex-direction:column;min-width:0;height:calc(100vh - var(--hc-top));box-sizing:border-box;border:solid var(--bd);border-width:0 0 0 1px;border-radius:0;background:transparent;padding:0 0 12px}",
      // Either rail can be hidden -- from the header toggles, or by
      // double-clicking its divider -- and the document takes the space.
      "[data-hc-launch][data-hc-hide-left] .hc-rail-left{display:none!important}",
      "[data-hc-launch][data-hc-hide-right] .hc-rail-right{display:none!important}",
      // The dividers are drag handles: an 8px strip astride each shared
      // line. Pseudo-elements, so no node is added for a render to drop;
      // the bridge hit-tests the pointer against the rail's edge.
      "[data-hc-launch] .hc-rail-left::after{content:'';position:absolute;top:0;right:-4px;width:8px;height:100%;cursor:col-resize;z-index:6}",
      "[data-hc-launch] .hc-rail-right::before{content:'';position:absolute;top:0;left:-4px;width:8px;height:100%;cursor:col-resize;z-index:6}",
      "[data-hc-launch][data-hc-dragging]{cursor:col-resize;user-select:none}",
      // The two panel toggles in the header, before the theme switch.
      "[data-hc-launch] .hc-panels{order:-1;display:inline-flex;align-items:center;gap:8px;padding-right:8px;border-right:1px solid var(--bd);align-self:center}",
      "[data-hc-launch][data-hc-overview] .hc-panels{display:none}",
      "[data-hc-launch] .hc-panel{display:inline-flex;cursor:pointer;color:var(--fnt);user-select:none}",
      "[data-hc-launch] .hc-panel:hover,[data-hc-launch] .hc-panel-on{color:var(--ink)}",
      // Rail headings, shared by both rails.
      "[data-hc-launch] .hc-rail-head{flex:none;display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 13px 10px;border-bottom:1px solid var(--bd)}",
      "[data-hc-launch] .hc-rail-name{font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1.2px;color:var(--mut)}",
      // Two tabs and a save stamp, in place of the old single label.
      "[data-hc-launch] .hc-rail-tabs{display:inline-flex;gap:14px;align-items:baseline}",
      "[data-hc-launch] .hc-rail-tab{font:600 10px 'Source Code Pro',monospace;letter-spacing:1.1px;color:var(--fnt);cursor:pointer;user-select:none}",
      "[data-hc-launch] .hc-rail-tab:hover{color:var(--ink)}",
      "[data-hc-launch] .hc-rail-tab-on{color:var(--ink)}",
      "[data-hc-launch] .hc-rail-saved{font:10px 'Source Code Pro',monospace;letter-spacing:.4px;color:var(--fnt);margin-left:10px}",
      // The list: rows of editable text in one column, so a selection can
      // run across them and Cmd+A takes them all; a dash gutter that picks a
      // row for a build; a state badge at the right; a question thread under
      // a row Claude asked about; Copy at the lower left, Build at the right.
      "[data-hc-launch] .hc-todos{flex:1 1 auto;min-height:0;display:flex;flex-direction:column}",
      "[data-hc-launch] .hc-todos-list{flex:1 1 auto;min-height:0;overflow-y:auto;padding:10px 10px 4px;outline:none;caret-color:var(--ink)}",
      "[data-hc-launch] .hc-todo{position:relative}",
      "[data-hc-launch] .hc-todo[data-hc-todo-head] .hc-todo-row{padding-right:24px}",
      // The x sits on the head's first line, level with the state badge --
      // the tile's top, not its bottom, which for an asking row is under the
      // question thread. 5px = the row's 2px top padding + half the gap
      // between a 22.8px line box and a 16px control.
      "[data-hc-launch] .hc-todo-cancel{position:absolute;top:5px;right:4px;width:16px;height:16px;line-height:15px;text-align:center;border-radius:4px;font:500 13px/15px 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none;opacity:.55}",
      "[data-hc-launch] .hc-todo:hover .hc-todo-cancel{opacity:1}",
      "[data-hc-launch] .hc-todo-cancel:hover{color:var(--del);background:var(--hov)}",
      "[data-hc-launch] .hc-todo-row{display:flex;align-items:baseline;gap:9px;padding:2px 6px;border-radius:5px}",
      "[data-hc-launch] .hc-todo-row:hover{background:var(--hov)}",
      "[data-hc-launch] .hc-todo-row[data-hc-todo-picked]{background:var(--panel2)}",
      "[data-hc-launch] .hc-todo-dash{flex:none;font:12px/1.9 'Source Code Pro',monospace;color:var(--fnt);user-select:none;cursor:pointer;width:8px;text-align:center}",
      "[data-hc-launch] .hc-todo-row[data-hc-todo-picked] .hc-todo-dash{color:var(--ink)}",
      "[data-hc-launch] .hc-todo-line{flex:1 1 auto;min-width:0;min-height:1.9em;outline:none;font:12px/1.9 'Source Code Pro',monospace;color:var(--dtxt);caret-color:var(--ink);white-space:pre-wrap;word-break:break-word}",
      // A row with no words yet has no line box, so the row's baseline
      // alignment falls back to the empty box's bottom edge: the dash drops
      // half a line and the caret sits above it. A zero-width space, drawn
      // and never in the DOM -- textContent, and so the row's text, cannot
      // see it -- gives the empty line a real baseline to hold the dash to.
      "[data-hc-launch] .hc-todo-line::before{content:\"\\200b\"}",
      "[data-hc-launch] .hc-todo-row[data-hc-todo-picked] .hc-todo-line{color:var(--ink);font-weight:600}",
      "[data-hc-launch] .hc-todo-row[data-hc-todo-state=\"done\"] .hc-todo-line{color:var(--fnt);text-decoration:line-through}",
      // A done row is a control: clicking it opens the pane that sends it
      // back out. "reopening" is the moment between the click and the note
      // -- struck through no longer, not yet building.
      "[data-hc-launch] .hc-todo-row[data-hc-todo-state=\"done\"]{cursor:pointer}",
      "[data-hc-launch] .hc-todo-row[data-hc-todo-state=\"reopening\"] .hc-todo-line{color:var(--ink);font-weight:600}",
      // The runs a row has already had, under it: quieter than the row, and
      // quieter still for what the reader said about each.
      "[data-hc-launch] .hc-todo-runs{margin:1px 0 7px;border-left:2px solid var(--bd);padding:1px 0 1px 10px;user-select:text}",
      "[data-hc-launch] .hc-todo-run{font:11px/1.5 'Source Code Pro',monospace;color:var(--fnt)}",
      "[data-hc-launch] .hc-todo-run-note{font:11px/1.5 'Source Code Pro',monospace;color:var(--dtxt);padding-left:12px;white-space:pre-wrap;overflow-wrap:anywhere}",
      // A rule between the list's bands: rows not yet sent, rows out with
      // the builder, rows that came back done.
      "[data-hc-launch] .hc-todo-sep{border-top:1px solid var(--bd);margin:7px 6px;user-select:none}",
      "[data-hc-launch] .hc-todo-status{flex:none;font:500 10px/1.9 'Source Code Pro',monospace;letter-spacing:.3px;user-select:none}",
      // What the row will cost to build, in its lower right. `flex-end`, not
      // the row's baseline: a row whose text wraps is two or three lines
      // tall, and the number belongs at the bottom of it, beside the last
      // line -- which is what "in the corner" means on a row that wrapped.
      // Faint, because it is an estimate the reader is free to ignore; the
      // measured number a finished build leaves is not faint.
      "[data-hc-launch] .hc-todo-cost{flex:none;align-self:flex-end;font:500 10px/1.9 'Source Code Pro',monospace;letter-spacing:.2px;color:var(--fnt);opacity:.7;user-select:none;white-space:nowrap;cursor:default}",
      "[data-hc-launch] .hc-todo-row:hover .hc-todo-cost{opacity:1}",
      "[data-hc-launch] .hc-todo-cost[data-hc-todo-spent]{color:var(--mut);opacity:1}",
      "[data-hc-launch] .hc-todo-ask{user-select:text}",
      "[data-hc-launch] .hc-todo-ask{margin:2px 0 8px;border-left:2px solid var(--hc-warn);padding:2px 0 2px 10px}",
      // The question and the answer both wrap: a long question runs onto
      // more lines, and the answer box grows as it is typed into, rather
      // than either scrolling its text out of the rail's width.
      "[data-hc-launch] .hc-todo-question{font:12px/1.5 'Source Code Pro',monospace;color:var(--dtxt);margin-bottom:5px;white-space:pre-wrap;overflow-wrap:anywhere}",
      "[data-hc-launch] .hc-todo-reply{display:flex;align-items:baseline;gap:6px}",
      "[data-hc-launch] .hc-todo-arrow{color:var(--hc-warn);font-size:11px}",
      "[data-hc-launch] .hc-todo-answer{flex:1;min-width:0;border:none;outline:none;background:transparent;padding:0;margin:0;font:12px/1.6 'Source Code Pro',monospace;color:var(--dtxt);caret-color:var(--ink);display:block;resize:none;overflow:hidden;height:auto;white-space:pre-wrap;overflow-wrap:anywhere}",
      "[data-hc-launch] .hc-todo-answer::placeholder{color:var(--fnt)}",
      // What the build is doing, under the rows and above the actions: a
      // state line with the run's own clock, the last thing it did, and --
      // unfolded -- the log itself, which is bounded and scrolls.
      "[data-hc-launch] .hc-todo-watch{flex:none;margin:8px 12px 0;padding:8px 10px;border:1px solid var(--bd);border-radius:6px;background:var(--panel2);user-select:none}",
      "[data-hc-launch] .hc-todo-watch-head{display:flex;align-items:center;gap:8px}",
      "[data-hc-launch] .hc-todo-watch-dot{flex:none;width:6px;height:6px;border-radius:50%;background:var(--fnt)}",
      "[data-hc-launch] .hc-todo-watch[data-hc-watch-state=\"on\"] .hc-todo-watch-dot{background:var(--hc-blue, #58a6ff);animation:hc-todo-pulse 1.6s ease-in-out infinite}",
      "@keyframes hc-todo-pulse{0%,100%{opacity:1}50%{opacity:.25}}",
      "[data-hc-launch] .hc-todo-watch-meta{flex:1;min-width:0;font:500 10.5px/1.5 'Source Code Pro',monospace;letter-spacing:.2px;color:var(--dtxt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:default}",
      "[data-hc-launch] .hc-todo-watch-btn{flex:none;padding:2px 7px;border:1px solid var(--bd2);border-radius:4px;font:600 10px 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer}",
      "[data-hc-launch] .hc-todo-watch-btn:hover{color:var(--ink);border-color:var(--ink)}",
      "[data-hc-launch] .hc-todo-watch-last{margin-top:5px;font:10.5px/1.5 'Source Code Pro',monospace;color:var(--fnt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      "[data-hc-launch] .hc-todo-watch-log{margin-top:6px;max-height:132px;overflow-y:auto;border-top:1px solid var(--bd);padding-top:6px;user-select:text}",
      "[data-hc-launch] .hc-todo-watch-row{display:flex;gap:8px;font:10.5px/1.7 'Source Code Pro',monospace;color:var(--fnt);overflow-wrap:anywhere}",
      "[data-hc-launch] .hc-todo-watch-at{flex:none;color:var(--bd2)}",
      "[data-hc-launch] .hc-todos-actions{flex:none;display:flex;align-items:center;gap:10px;padding:10px 12px 0}",
      "[data-hc-launch] .hc-todo-copy{padding:5px 10px;border:1px solid var(--bd2);border-radius:4px;font:600 11px 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none}",
      "[data-hc-launch] .hc-todo-copy:hover{color:var(--ink);border-color:var(--ink)}",
      "[data-hc-launch] .hc-todo-error{flex:1;min-width:0;font:10.5px/1.4 'Source Code Pro',monospace;color:var(--fnt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      "[data-hc-launch] .hc-todo-note-bad{color:var(--del)}",
      "[data-hc-launch] .hc-todo-reopen{color:var(--ink);cursor:pointer;text-decoration:underline;text-underline-offset:2px}",
      "[data-hc-launch] .hc-todo-build{margin-left:auto;padding:5px 12px;border-radius:4px;font:600 11px 'Source Code Pro',monospace;color:var(--fnt);border:1px solid var(--bd2);cursor:default;user-select:none}",
      "[data-hc-launch] .hc-todo-build[data-hc-todo-build=\"on\"]{color:#fff;background:#1f6feb;border-color:#1f6feb;cursor:pointer}",
      "[data-hc-launch] .hc-rail-select{margin-left:auto;font:500 10px 'Source Code Pro',monospace;letter-spacing:.3px;color:var(--fnt);cursor:pointer;user-select:none}",
      "[data-hc-launch] .hc-rail-select:hover{color:var(--ink)}",
      "[data-hc-launch] .hc-rail-prompt{flex:1 1 auto;min-height:0;overflow-y:auto;display:flex;flex-direction:column}",
      "[data-hc-launch] .hc-rail-count{font:10px 'Source Code Pro',monospace;color:var(--fnt)}",
      // The search bar sits directly under GOALS, with no rule between the
      // two: the heading's line moves down to under the input. The rail's
      // tree is the third child now, after the heading and the search.
      "[data-hc-launch] .hc-rail-left>.hc-rail-head{border-bottom:0;padding-bottom:6px}",
      "[data-hc-launch] .hc-search{flex:none;display:flex;flex-direction:column;min-height:0;padding:4px 11px 10px;border-bottom:1px solid var(--bd)}",
      "[data-hc-launch] .hc-search-field{flex:none;display:flex;align-items:center;gap:8px;box-sizing:border-box;border:1px solid var(--bd2);border-radius:8px;background:var(--panel2);padding:0 9px}",
      "[data-hc-launch] .hc-search-field:focus-within{border-color:var(--acc)}",
      "[data-hc-launch] .hc-search-glyph{flex:none;display:flex;align-items:center;color:var(--fnt)}",
      "[data-hc-launch] .hc-search-field:focus-within .hc-search-glyph{color:var(--acc)}",
      "[data-hc-launch] .hc-search-clear{flex:none;display:none;cursor:pointer;user-select:none;color:var(--fnt);font:14px/1 'Source Code Pro',monospace;padding:2px}",
      "[data-hc-launch] .hc-search-clear:hover{color:var(--ink)}",
      "[data-hc-launch] .hc-search-field[data-hc-typed] .hc-search-clear{display:block}",
      "[data-hc-launch] .hc-relbar{display:none;flex-wrap:wrap;gap:5px;padding-top:8px}",
      "[data-hc-launch] .hc-relbar[data-hc-on]{display:flex}",
      "[data-hc-launch] .hc-relchip{display:inline-flex;align-items:baseline;gap:5px;cursor:pointer;user-select:none;border:1px solid var(--bd2);border-radius:999px;padding:3px 9px;font:10px 'Source Code Pro',monospace;color:var(--mut);white-space:nowrap}",
      "[data-hc-launch] .hc-relchip:hover{color:var(--ink);border-color:var(--ink)}",
      "[data-hc-launch] .hc-relchip[data-hc-on]{background:var(--acc);border-color:var(--acc);color:var(--onacc)}",
      "[data-hc-launch] .hc-relchip-n{opacity:.7}",
      "[data-hc-launch] .hc-row[data-hc-rel-off]{display:none!important}",
      // While searching, the hits replace the tree and there is nothing
      // for the filter to act on.
      "[data-hc-launch] .hc-rail-left[data-hc-searching] .hc-relbar{display:none}",
      "[data-hc-launch] .hc-search-input{flex:1 1 auto;display:block;width:100%;min-width:0;box-sizing:border-box;border:none;outline:none;background:transparent;margin:0;padding:7px 0;font:11.5px 'Source Code Pro',monospace;color:var(--ink);caret-color:var(--ink);-webkit-appearance:none;appearance:none}",
      "[data-hc-launch] .hc-search-input::placeholder{color:var(--fnt)}",
      "[data-hc-launch] .hc-search-input::-webkit-search-cancel-button{-webkit-appearance:none;appearance:none}",
      // While a query is typed the rail shows hits in place of the tree:
      // the search box grows to the rail, and every sibling but the
      // heading is hidden. The tree comes back when the box is cleared.
      "[data-hc-launch] .hc-search-hits{display:none}",
      "[data-hc-launch] .hc-rail-left[data-hc-searching] .hc-search{flex:1 1 auto;border-bottom:0}",
      "[data-hc-launch] .hc-rail-left[data-hc-searching] .hc-search-hits{border-top:1px solid var(--bd);margin-top:9px}",
      "[data-hc-launch] .hc-rail-left[data-hc-searching] .hc-search-hits{display:block;flex:1 1 auto;min-height:0;overflow-y:auto;padding:8px 0 0}",
      "[data-hc-launch] .hc-rail-left[data-hc-searching]>:not(.hc-rail-head):not(.hc-search){display:none!important}",
      "[data-hc-launch] .hc-search-hit{padding:5px 8px;border-radius:2px;cursor:pointer}",
      "[data-hc-launch] .hc-search-hit:hover,[data-hc-launch] .hc-search-hit[data-hc-hit-active]{background:var(--acchov)}",
      "[data-hc-launch] .hc-search-hit-title{font-size:12.5px;line-height:1.4;color:var(--ink)}",
      "[data-hc-launch] .hc-search-hit-trail{font:10px 'Source Code Pro',monospace;color:var(--fnt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      "[data-hc-launch] .hc-search-hit-where{font:10.5px/1.5 'Source Code Pro',monospace;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
      "[data-hc-launch] .hc-search-hit-where b{font-weight:600;color:var(--fnt)}",
      "[data-hc-launch] .hc-search-none{padding:10px 8px;font:11px 'Source Code Pro',monospace;color:var(--fnt)}",
      "[data-hc-launch] .hc-rail-left>div:nth-child(3){padding:6px 6px 0}",
      // A tree row is one line high, so its title has to be one line: a
      // wrapped one overlapped the row under it at this width.
      "[data-hc-launch] .hc-row{white-space:nowrap}",
      "[data-hc-launch] .hc-rowtitle{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis}",
      "[data-hc-launch] .hc-rail-none{margin:12px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--fnt)}",
      // The prompt the build opens on: a block to read, not a box to type in.
      // It is the whole of the tab now, so it takes the whole of the column --
      // it used to be capped at 38vh to leave room for a field under it, and
      // that field is gone. Only Copy sits below it.
      "[data-hc-launch] .hc-rail-ctx{flex:1 1 auto;display:flex;flex-direction:column;min-height:0;border-bottom:1px solid var(--bd)}",
      "[data-hc-launch] .hc-rail-ctx-head{flex:none;display:flex;align-items:baseline;gap:7px;padding:9px 16px 7px;cursor:pointer;user-select:none}",
      "[data-hc-launch] .hc-rail-ctx-title{font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)}",
      "[data-hc-launch] .hc-rail-ctx-note{flex:1;min-width:0;font:10px 'Source Code Pro',monospace;color:var(--fnt);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      "[data-hc-launch] .hc-rail-ctx-fold{flex:none;font:10px 'Source Code Pro',monospace;color:var(--fnt)}",
      "[data-hc-launch] .hc-rail-ctx-head:hover .hc-rail-ctx-fold{color:var(--ink)}",
      "[data-hc-launch] .hc-rail-ctx-body{flex:1 1 auto;min-height:0;overflow:auto;margin:0;padding:0 16px 11px;font:11.5px/1.62 'Source Code Pro',ui-monospace,monospace;color:var(--dtxt);white-space:pre-wrap;word-break:break-word;user-select:text}",
      "[data-hc-launch] .hc-rail-ctx[data-hc-fold] .hc-rail-ctx-body{display:none}",
      "[data-hc-launch] .hc-rail-actions{flex:none;display:flex;align-items:center;gap:10px;padding:10px 12px 0}",
      "[data-hc-launch] .hc-rail-copy{display:block;flex:1;text-align:center;padding:7px 12px;border-radius:4px;background:var(--hc-ok);color:#fff;font:600 11.5px 'Source Code Pro',monospace;cursor:pointer;user-select:none}",
      // The light fill is dark enough that only white clears AA on it; the
      // dark theme's fill is bright enough that only near-black does.
      "[data-hc-launch][data-hc-theme=\"dark\"] .hc-rail-copy{color:#08130c}",
      "[data-hc-launch] .hc-rail-copy:hover{filter:brightness(1.08)}",
      // What the chat is actually being told, from /api/state.injection.
      // Sources, as a chip rail above the document. The tabs under it are
      // sticky and paint a 16px band of --bg upward to hide what scrolls
      // behind them, so the rail keeps a bottom margin clear of that band --
      // at 14px it cut the round bottom off every chip.
      "[data-hc-launch] .hc-sources{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin:12px 0 22px}",
      "[data-hc-launch] .hc-sources-label{font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut);margin-right:2px}",
      "[data-hc-launch] .hc-src{display:inline-flex;align-items:center;gap:6px;max-width:280px;padding:3px 8px;border:1px solid var(--bd);border-radius:99px;background:var(--panel2);font:10.5px 'Source Code Pro',monospace;color:var(--dtxt)}",
      "[data-hc-launch] .hc-src-tag{font:600 8px 'Source Code Pro',monospace;letter-spacing:.6px;color:var(--fnt)}",
      // "GITHUB claude-plugins" read as two halves of one name. The colon
      // says which half is the kind and which is the thing, and it is drawn
      // rather than written so the tag's own text stays the word alone.
      "[data-hc-launch] .hc-src-tag::after{content:':';margin-left:-.6px}",
      "[data-hc-launch] .hc-src-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      "[data-hc-launch] .hc-src-rm{color:var(--fnt);cursor:pointer;font-size:11px}",
      "[data-hc-launch] .hc-src-rm:hover{color:var(--del)}",
      // Inherited from the overview: the same chip, unfilled and muted, so
      // the goal's own read as the ones this goal owns.
      "[data-hc-launch] .hc-src-ctx{background:transparent;color:var(--mut);cursor:pointer}",
      "[data-hc-launch] .hc-src-ctx .hc-src-tag{color:var(--mut)}",
      "[data-hc-launch] .hc-src-ctx:hover{border-color:var(--acc);color:var(--acc)}",
      "[data-hc-launch] .hc-src-ctx:hover .hc-src-tag{color:var(--acc)}",
      "[data-hc-launch] .hc-src-add{padding:3px 9px;border:1px dashed var(--bd2);border-radius:99px;font:10.5px 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none}",
      "[data-hc-launch] .hc-src-add:hover{color:var(--acc);border-color:var(--acc)}",
      "[data-hc-launch] .hc-tabs{margin-top:14px!important}",
      // The session banner. Same nodes, same timers, same close button as
      // the toast it replaces -- a bar under the header rather than a card
      // in the corner, because it reports on the whole workspace.
      "[data-hc-launch] .hc-notice-stack{position:fixed;top:37px;left:0;right:0;bottom:auto;z-index:60;align-items:stretch;gap:0}",
      "[data-hc-launch] .hc-notice{width:auto;max-width:none;border:none;border-bottom:1px solid var(--hc-okbd);border-left:none;border-radius:0;background:var(--hc-okbg);box-shadow:none;display:flex;align-items:baseline;gap:10px;padding:7px 34px 7px 16px}",
      "[data-hc-launch] .hc-notice-title{color:var(--hc-ok);flex:none}",
      "[data-hc-launch] .hc-notice-title::before{content:'\\25cf';margin-right:7px;font-size:9px;vertical-align:1px}",
      "[data-hc-launch] .hc-notice-detail{margin-top:0;color:var(--hc-noticetxt);flex:1;min-width:0}",
      "[data-hc-launch] .hc-notice-close{top:6px;right:12px;color:var(--hc-noticetxt)}",
  ].join("");

  var launchApplied = false;

  function launchDressed() {
    // Dressed is not the same as skinned: the artifact paints a frame with
    // its bindings still written out ("saved {{ updatedLabel }}") before it
    // resolves them, and showing that is showing the machinery. textContent,
    // not innerText: innerText is computed from layout, and a document held
    // hidden has none, so it would read as empty forever.
    var root = document.documentElement;
    if (!root || !root.getAttribute) return false;
    if (root.getAttribute("data-hc-launch") !== "chat") return false;
    if (!document.getElementById("hc-launch-style")) return false;
    var text = "";
    try { text = (document.body && document.body.textContent) || ""; }
    catch (e) { return false; }
    return text.length > 0 && text.indexOf("{{") < 0;
  }

  // Section surgery on a markdown document: find one heading, hand every
  // other line back untouched -- including a "# comment" inside a fenced
  // block, which is not a heading however much it looks like one. This is
  // goals._scan_doc's rule. The rail's TODO list no longer lives in the
  // document at all (it is its own store, todos.json on the server); the
  // machinery stays for the reader's-prompt section and the tests.
  var TODO_SECTION = "TODOs";
  // The reader's own edit of the assembled prompt. Deliberately NOT one of
  // goals.DOC_SECTIONS: the spine is what inference may append to, and this
  // section is the reader's alone. ensure_doc_sections leaves headings it
  // does not know about exactly where they are.
  var PROMPT_SECTION = "Prompt";

  function docScan(document_) {
    var lines = str(document_).split("\n");
    var spans = [], open = null, fence = null;
    lines.forEach(function (line, at) {
      var mark = /^(`{3,}|~{3,})/.exec(line);
      if (mark) {
        if (fence === null) fence = mark[1][0];
        else if (mark[1][0] === fence) fence = null;
        return;
      }
      if (fence !== null) return;
      var head = /^# (.*)$/.exec(line);
      if (!head) return;
      if (open) { open.end = at; spans.push(open); }
      open = { title: head[1].trim(), start: at + 1, end: lines.length };
    });
    if (open) spans.push(open);
    return { lines: lines, spans: spans };
  }

  function docSectionRead(document_, title) {
    var scan = docScan(document_);
    for (var i = 0; i < scan.spans.length; i++) {
      var span = scan.spans[i];
      if (span.title !== title) continue;
      var body = scan.lines.slice(span.start, span.end).join("\n");
      body = body.replace(/^\n+/, "").replace(/\n+$/, "");
      return body ? body + "\n" : "";
    }
    return "";
  }

  function todoDocRead(document_) {
    return docSectionRead(document_, TODO_SECTION);
  }

  function docSectionWrite(document_, title, body) {
    var scan = docScan(document_);
    var text = str(body).replace(/\n+$/, "");
    for (var i = 0; i < scan.spans.length; i++) {
      var span = scan.spans[i];
      if (span.title !== title) continue;
      var head = scan.lines.slice(0, span.start);
      var tail = scan.lines.slice(span.end);
      var middle = text ? text.split("\n") : [];
      // One blank line before the next heading, and none if this section
      // ends the document: the same shape join_doc writes on the server.
      if (tail.length) middle = middle.concat([""]);
      return head.concat(middle, tail).join("\n");
    }
    var out = str(document_).replace(/\n+$/, "");
    return (out ? out + "\n\n" : "") + "# " + title + "\n"
      + (text ? text + "\n" : "");
  }

  function todoDocWrite(document_, body) {
    return docSectionWrite(document_, TODO_SECTION, body);
  }

  // --- the rail's list: rows the reader edits and picks -----------------------
  //
  // The list is the goal's `todo_items`: one row per line, each with an id
  // the reader never sees, its text, its depth, and the state a build run
  // gives it. It is held apart from the notes so an edit to one never reaches
  // the other. Rows are drawn as spans inside ONE editable column, not as
  // inputs and not as one editable each: a browser will not let a selection
  // cross from one editing host into another, so one host is what makes a
  // selection dragged across rows a real selection, and lets Copy of that
  // selection be the rows as markdown. The keys the list is about -- Enter,
  // Tab, Shift-Tab, Backspace at a bullet, Cmd+/ to pick, Cmd+A to pick
  // every row for the build, Cmd+Backspace to delete -- are operations on
  // the row list, each returning the rows and where the caret should land,
  // so all of them are testable without a DOM.

  var TODO_INDENT = "    ";
  var railTab = "todos";
  var todoItems = null;
  var todoGoalId = null;
  var todoPicked = {};
  var todoSavedLabel = "";
  var todoSaveTimer = null;
  var todoFocusAt = null;
  var todoCopied = false;
  var todoCopiedTimer = null;
  var todoBuilding = false;
  var todoBuildError = "";
  // The done row whose "what went wrong?" pane is open, if any. One at a
  // time: reopening is an argument about one row, not a mode the list is in.
  var todoReopening = null;

  function repeat(unit, times) {
    var out = "";
    for (var i = 0; i < times; i++) out += unit;
    return out;
  }

  function todoNewId() {
    var rand = Math.floor(Math.random() * 0xffffffff).toString(16);
    return "t" + ("00000000" + rand).slice(-8);
  }

  function todoRow(text, depth, id) {
    return { id: id || todoNewId(), text: str(text), depth: depth | 0,
             status: "", question: "" };
  }

  function todoAttachmentsOn(row) {
    // The row's own pasted files, well-formed: a positive number, a path.
    var out = [], seen = {};
    array(row && row.attachments).forEach(function (att) {
      var n = att && +att.n;
      var path = str(att && att.path);
      if (!(n > 0) || seen[n] || !path) return;
      seen[n] = true;
      out.push({ n: n, path: path, name: str(att.name) });
    });
    return out;
  }

  function todoCopyRows(items) {
    return array(items).map(function (row) {
      var copy = { id: str(row.id) || todoNewId(), text: str(row.text),
                   depth: row.depth | 0, status: str(row.status),
                   question: str(row.question) };
      // Only when there is something to hold, on both sides of the wire:
      // the server leaves the field off a row without one, and the rail's
      // "has anything changed" is a field-for-field comparison.
      var shots = todoAttachmentsOn(row);
      if (shots.length) copy.attachments = shots;
      // What building this row actually spent, when the server has measured
      // it. Same rule as the screenshots, and in the same order the server
      // writes its rows: the comparison is JSON against JSON.
      var spent = +row.tokens;
      if (spent > 0) copy.tokens = Math.round(spent);
      var past = todoHistoryOn(row);
      if (past.length) copy.history = past;
      return copy;
    });
  }

  function todoHistoryOn(row) {
    // The runs this row has already had: the verdict each ended on and what
    // the reader said was wrong with it. The server owns this list -- it is
    // carried here only so the rows on screen and the rows the server holds
    // compare equal, and so the rail can stack the runs under the row.
    return array(row && row.history).filter(function (past) {
      return past && str(past.state);
    }).map(function (past) {
      return { state: str(past.state), note: str(past.note) };
    });
  }

  // --- screenshots pasted into a row ---------------------------------------
  //
  // A Cmd+V whose clipboard holds an image lands as "[attachment #N]" in the
  // row's text, at the caret, the way Claude Code's own composer says
  // "[Image #1]" -- and the row remembers which file the marker names. N
  // counts up across the whole list and is never reused, so a marker means
  // the same file wherever the reader later moves it. Deleting the marker
  // from the text un-cites the file: what leaves the rail (Copy TODOs, the
  // copied prompt, a Build) resolves only the markers still present in some
  // row, marker to path, so the session reading it can open them.

  var TODO_MARKER = /\[attachment #(\d+)\]/g;

  function todoAttach(items, index, caret, attachment) {
    var rows = todoCopyRows(items);
    var row = rows[index];
    if (!row || !attachment || !str(attachment.path)) return null;
    var next = 0;
    rows.forEach(function (r) {
      todoAttachmentsOn(r).forEach(function (a) { next = Math.max(next, a.n); });
      // A marker typed or pasted in as text counts too: the number must
      // never collide with one already on screen.
      var m;
      TODO_MARKER.lastIndex = 0;
      while ((m = TODO_MARKER.exec(str(r.text)))) next = Math.max(next, +m[1]);
    });
    next += 1;
    var marker = "[attachment #" + next + "]";
    var text = row.text;
    var at = (typeof caret === "number")
      ? Math.max(0, Math.min(caret, text.length)) : text.length;
    var head = text.slice(0, at), tail = text.slice(at);
    if (head && !/\s$/.test(head)) head += " ";
    if (tail && !/^\s/.test(tail)) tail = " " + tail;
    row.text = head + marker + tail;
    row.attachments = todoAttachmentsOn(row).concat([{
      n: next, path: str(attachment.path), name: str(attachment.name) }]);
    return { items: rows, index: index, caret: head.length + marker.length };
  }

  function todoAttachments(items) {
    // The files the list still cites, ordered by number.
    var cited = {};
    array(items).forEach(function (row) {
      var m;
      TODO_MARKER.lastIndex = 0;
      while ((m = TODO_MARKER.exec(str(row && row.text)))) cited[+m[1]] = true;
    });
    var out = [], seen = {};
    array(items).forEach(function (row) {
      todoAttachmentsOn(row).forEach(function (att) {
        if (cited[att.n] && !seen[att.n]) { seen[att.n] = true; out.push(att); }
      });
    });
    return out.sort(function (a, b) { return a.n - b.n; });
  }

  function todoAttachmentLines(items) {
    return todoAttachments(items).map(function (att) {
      return "[attachment #" + att.n + "]: " + att.path;
    });
  }

  var TODO_ATTACH_HEAD = "Attachments (files the rows cite; open them for"
    + " the rows that name them):\n";

  function todoNormalize(items) {
    var rows = todoCopyRows(items);
    var ceiling = 0;
    rows.forEach(function (row) {
      row.depth = Math.max(0, Math.min(row.depth, ceiling));
      ceiling = row.depth + 1;
    });
    return rows;
  }

  function todoSerialize(items) {
    var rows = array(items).filter(function (row) {
      return str(row && row.text).trim() !== "";
    });
    if (!rows.length) return "";
    return rows.map(function (row) {
      return repeat(TODO_INDENT, row.depth | 0) + "- " + row.text;
    }).join("\n") + "\n";
  }

  function todoSerializeStates(items) {
    // The same bullets, each carrying its state: what a session receiving
    // the list needs to know before touching a row. A row with no status
    // yet is named "active" rather than left bare, so the reader never has
    // to guess what an unmarked bullet means.
    var rows = array(items).filter(function (row) {
      return str(row && row.text).trim() !== "";
    });
    if (!rows.length) return "";
    return rows.map(function (row) {
      return repeat(TODO_INDENT, row.depth | 0)
        + "- [" + (str(row.status) || "active") + "] " + row.text;
    }).join("\n") + "\n";
  }

  function todoCopyText(items, notes) {
    // The body the Copy TODOs control puts on the clipboard: the rows with
    // their states, and the goal's notes underneath as CONTEXT only -- the
    // notes describe the goal, and a session pasted this body must act on
    // the TODOs alone, never on changes the notes happen to mention.
    var text = todoSerializeStates(items);
    var doc = str(notes).trim();
    if (!text) return text;
    // The screenshots the rows cite, resolved to their files, right under
    // the rows: part of the work, not of the context.
    var shots = todoAttachmentLines(items);
    if (shots.length) text += "\n" + TODO_ATTACH_HEAD + shots.join("\n") + "\n";
    if (!doc) return text;
    return "TODOs (each with its current state):\n" + text
      + "\nCONTEXT — the goal's notes, for background only. Do NOT make"
      + " any changes specified in these notes; act only on the TODOs"
      + " above:\n" + doc + "\n";
  }

  function todoSpan(items, index) {
    // A row and everything nested under it move together.
    var end = index + 1;
    while (end < items.length && items[end].depth > items[index].depth) end++;
    return end;
  }

  function todoBandOf(items) {
    // Which band of the list each row sits in: 0 for rows not yet sent
    // ("active" -- the absence of a status), 1 for rows out with the
    // builder (queued, building, asking -- and failed, which came back
    // needing another go), 2 for rows that came back done. A family is
    // banded whole: it is done only when every row in it is, and it is
    // out only when any row in it is.
    var rows = array(items), bands = [], i = 0;
    while (i < rows.length) {
      var end = todoSpan(rows, i);
      var out = false, done = true;
      for (var j = i; j < end; j++) {
        if (rows[j].status) out = true;
        if (rows[j].status !== "done") done = false;
      }
      var band = done ? 2 : out ? 1 : 0;
      for (; i < end; i++) bands.push(band);
    }
    return bands;
  }

  function todoSectioned(items) {
    // The same rows, banded: active families first, then families out
    // with the builder, then finished ones -- each band keeping the
    // order the rows were already in. The rows themselves are the rows
    // given, not copies: a caller holding one keeps holding it.
    var rows = array(items);
    var bands = todoBandOf(rows);
    var out = [[], [], []];
    rows.forEach(function (row, i) { out[bands[i]].push(row); });
    return out[0].concat(out[1], out[2]);
  }

  function todoEnter(items, index, caret) {
    var rows = todoCopyRows(items);
    var row = rows[index];
    if (!row) return null;
    var text = row.text;
    var at = (typeof caret === "number") ? Math.min(caret, text.length) : text.length;
    if (!text.trim()) {
      // An empty row never makes another empty row. Nested, the key spends
      // itself outdenting; at the margin it does nothing at all.
      if (row.depth > 0) row.depth -= 1;
      return { items: rows, index: index, caret: 0 };
    }
    row.text = text.slice(0, at);
    // The tail goes down without the space that separated it from the caret.
    var tail = text.slice(at).replace(/^ +(?=\S)/, "");
    rows.splice(index + 1, 0, todoRow(tail, row.depth));
    return { items: rows, index: index + 1, caret: 0 };
  }

  function todoParsePaste(text) {
    // What a pasted body means in rows. Bullet lines land one row each, at
    // their indent (four spaces or a tab to the level, "- ", "* " or "• "
    // all bullets, a "[state]" from a Copy TODOs body dropped) -- and so
    // does a bulleted list whose newlines were lost ("- a- b- c"), where a
    // dash glued to the word before it opens the next bullet. A spaced
    // dash (" - ") is prose and stays. Plain lines land one row each too.
    var body = str(text).replace(/\r\n?/g, "\n");
    if (/^\s*[-*•]\s/.test(body)) body = body.replace(/(\S)- /g, "$1\n- ");
    var parts = [];
    body.split("\n").forEach(function (line) {
      if (!line.trim()) return;
      var lead = /^[ \t]*/.exec(line)[0];
      var depth = Math.floor(lead.replace(/\t/g, TODO_INDENT).length
                             / TODO_INDENT.length);
      var rest = line.slice(lead.length);
      if (/^[-*•]\s/.test(rest)) {
        rest = rest.replace(/^[-*•]\s+/, "").replace(/^\[[a-z]+\]\s+/, "");
      }
      parts.push({ text: rest.replace(/\s+$/, ""), depth: depth });
    });
    if (!parts.length) return parts;
    // Depths are relative to the shallowest line: a subtree copied from the
    // middle of a list nests under the caret's row, not at its old depth.
    var floor = parts[0].depth;
    parts.forEach(function (part) { floor = Math.min(floor, part.depth); });
    parts.forEach(function (part) { part.depth -= floor; });
    return parts;
  }

  function todoPaste(items, index, caret, text) {
    // A paste lands like typing the parsed rows in: the first fragment joins
    // the caret's row, every further fragment becomes a row of its own under
    // it (nested as the body nested it, from the row's own depth), and what
    // stood after the caret ends up after the last fragment, caret between
    // the two.
    var parts = todoParsePaste(text);
    if (!parts.length) return null;
    var rows = todoCopyRows(items);
    var row = rows[index];
    if (!row) return null;
    var at = (typeof caret === "number")
      ? Math.max(0, Math.min(caret, row.text.length)) : row.text.length;
    var tail = row.text.slice(at);
    row.text = row.text.slice(0, at) + parts[0].text;
    var base = row.depth | 0;
    for (var i = 1; i < parts.length; i++) {
      rows.splice(index + i, 0, todoRow(parts[i].text, base + parts[i].depth));
    }
    var last = rows[index + parts.length - 1];
    var caretAt = last.text.length;
    last.text += tail;
    return { items: rows, index: index + parts.length - 1, caret: caretAt };
  }

  function todoIndent(items, index) {
    var rows = todoCopyRows(items);
    if (index <= 0 || !rows[index]) return { items: rows, index: index };
    // Only ever one level below the row above: the first child of a parent
    // cannot be a grandchild of it.
    var ceiling = rows[index - 1].depth + 1;
    if (rows[index].depth >= ceiling) return { items: rows, index: index };
    var end = todoSpan(rows, index);
    for (var i = index; i < end; i++) rows[i].depth += 1;
    return { items: rows, index: index };
  }

  function todoOutdent(items, index) {
    var rows = todoCopyRows(items);
    if (!rows[index] || rows[index].depth === 0) return { items: rows, index: index };
    var end = todoSpan(rows, index);
    for (var i = index; i < end; i++) rows[i].depth -= 1;
    return { items: rows, index: index };
  }

  function todoBackspace(items, index, caret) {
    // Only the start of a row is ours; anywhere else is ordinary typing.
    if (caret !== 0) return null;
    var rows = todoCopyRows(items);
    var row = rows[index];
    if (!row) return null;
    if (!row.text) {
      if (row.depth > 0) return todoOutdent(rows, index);
      if (rows.length === 1) return null;
      rows.splice(index, 1);
      var back = Math.max(0, index - 1);
      return { items: rows, index: back, caret: rows[back].text.length };
    }
    if (index === 0) return null;
    var previous = rows[index - 1];
    var caretAt = previous.text.length;
    previous.text += row.text;
    rows.splice(index, 1);
    return { items: rows, index: index - 1, caret: caretAt };
  }

  function todoRemove(items, index) {
    var rows = todoCopyRows(items);
    if (!rows[index]) return null;
    rows.splice(index, 1);
    if (!rows.length) rows.push(todoRow("", 0));
    var next = Math.min(index, rows.length - 1);
    return { items: rows, index: next, caret: rows[next].text.length };
  }

  function todoCut(items, a, aCaret, b, bCaret, insert) {
    // A selection that runs across rows: what is between the two carets goes,
    // the two rows become one, and `insert` (typed or pasted) lands there.
    var rows = todoCopyRows(items);
    if (!rows[a] || !rows[b]) return null;
    if (a > b || (a === b && aCaret > bCaret)) {
      var t = a; a = b; b = t; t = aCaret; aCaret = bCaret; bCaret = t;
    }
    var head = rows[a].text.slice(0, aCaret);
    var tail = rows[b].text.slice(bCaret);
    var put = str(insert);
    rows[a].text = head + put + tail;
    rows.splice(a + 1, b - a);
    return { items: rows, index: a, caret: head.length + put.length };
  }

  function todoSelectionText(items, a, aCaret, b, bCaret) {
    var rows = todoCopyRows(items);
    if (!rows[a] || !rows[b]) return "";
    if (a > b || (a === b && aCaret > bCaret)) {
      var t = a; a = b; b = t; t = aCaret; aCaret = bCaret; bCaret = t;
    }
    var out = [];
    for (var i = a; i <= b; i++) {
      var text = rows[i].text;
      var from = i === a ? aCaret : 0;
      var to = i === b ? bCaret : text.length;
      out.push(repeat(TODO_INDENT, rows[i].depth) + "- " + text.slice(from, to));
    }
    return out.join("\n");
  }

  // --- what a row will cost to build ---------------------------------------
  //
  // Every row that has not been built yet prints a number in its lower right.
  // What its build will spend is two parts, and the tooltip says which is
  // which, because only one of them is knowable in advance:
  //
  // * the context the build opens on -- the project, the goal tree this plugin
  //   keeps for the chat, this goal's own prompt, the protocol, plus the row
  //   itself. That is the string the Prompt tab prints, and it is counted
  //   here the way the tab counts it: prompt_preview composes it and says how
  //   many tokens it is with no rows in it, so the number beside a row and
  //   the number in the tab can never disagree. (The state's
  //   own context_tokens measures the goal-context file alone, which is a
  //   part of that string and reads low beside it; it stands in only until
  //   the preview has answered for this goal.) Characters over four, the
  //   usual rule of thumb, since no browser holds the model's tokenizer;
  // * the work -- what Claude reads, writes and re-reads to do the row. Not
  //   knowable, so it is measured instead: the server keeps what this chat's
  //   own finished builds spent, per row, and the median of those is what a
  //   row is priced at. Before this chat has built anything there is nothing
  //   to measure and the server's default stands in, which the tooltip says.
  //
  // A row longer than the ones already built is scaled up from that median,
  // and a shorter one down, within a factor either way -- length is a weak
  // signal about work, so it is allowed to nudge the number, not set it.
  //
  // The corner prints the context, since that is the half a number can be
  // told about honestly; the work is in the tooltip, where it can be labelled
  // as the guess it is.
  //
  // Once a row HAS been built, and built on its own, the run's own usage is
  // on the row and the estimate is not printed: a measured number replaces a
  // guess about the same thing.

  var TODO_COST_SPAN = [0.5, 2.5];

  function todoCostContext() {
    // The context the row on screen opens on. The Prompt tab's measurement
    // for this goal where it has one -- see promptCtxCost -- and the state's
    // coarser figure until then.
    if (todoGoalId && promptCtxCost && promptCtxCost.goal === todoGoalId
        && promptCtxCost.context > 0) {
      return promptCtxCost.context;
    }
    var cost = serverState.buildCost;
    return (cost && typeof cost === "object")
      ? Math.max(0, +cost.context_tokens || 0) : 0;
  }

  function todoCostBasis() {
    var cost = serverState.buildCost;
    var work = (cost && typeof cost === "object")
      ? Math.max(0, +cost.row_tokens || 0) : 0;
    if (!work) return null;
    return { context: todoCostContext(), work: work,
             chars: Math.max(1, +cost.row_chars || 1),
             samples: Math.max(0, +cost.samples || 0) };
  }

  function todoCostOf(row) {
    // The estimate for one row, or null when there is nothing to say: an
    // empty row is no work, a row already out with the builder is past being
    // estimated, and a chat the server has measured nothing for would rather
    // print no number than one nothing stands behind.
    var text = str(row && row.text).trim();
    if (!text) return null;
    var basis = todoCostBasis();
    if (!basis) return null;
    var own = Math.ceil(text.length / 4);
    var scale = Math.min(TODO_COST_SPAN[1],
                         Math.max(TODO_COST_SPAN[0], text.length / basis.chars));
    var work = Math.round(basis.work * scale);
    return { total: basis.context + own + work, context: basis.context + own,
             work: work, samples: basis.samples };
  }

  function todoTokenLabel(n) {
    // Three significant figures at most: an estimate that reads "31.7k" is
    // claiming a precision it does not have.
    if (!(n > 0)) return "";
    if (n < 1000) return String(Math.round(n));
    if (n < 10000) return (Math.round(n / 100) / 10) + "k";
    return Math.round(n / 1000) + "k";
  }

  function todoCostText(row) {
    // What the corner of one row says, and what it says when hovered. A
    // measured number where there is one; an estimate where there is not;
    // nothing at all for a row that is neither.
    var spent = +(row && row.tokens);
    if (spent > 0) {
      return { label: todoTokenLabel(spent) + " tok",
               title: "this row's build spent " + spent.toLocaleString()
                 + " tokens", measured: true };
    }
    if (row && row.status && row.status !== "failed") return null;
    var cost = todoCostOf(row);
    if (!cost) return null;
    var source = cost.samples
      ? "a typical build here, over your last " + cost.samples
        + (cost.samples === 1 ? " build" : " builds")
      : "a typical build, until this chat has built something to measure";
    return {
      label: "~" + todoTokenLabel(cost.context) + " tok",
      title: "about " + todoTokenLabel(cost.context)
        + " of context this row's build opens on — the string the Prompt tab"
        + " prints, plus this row. Doing the work then costs roughly "
        + todoTokenLabel(cost.work) + " more: " + source
        + ". Rows built together share the context.",
      measured: false
    };
  }

  function todoCostPaint(row, line) {
    // The corner of a row already on screen, brought up to date without
    // redrawing it: the reader may be typing in that very row, and replacing
    // it would take the caret with it.
    if (!line || !line.parentNode) return;
    var slot = line.parentNode.querySelector(".hc-todo-cost");
    var cost = todoCostText(row);
    if (!slot) return;
    slot.textContent = cost ? cost.label : "";
    slot.title = cost ? cost.title : "";
    slot.style.display = cost ? "" : "none";
    if (cost && cost.measured) slot.setAttribute("data-hc-todo-spent", "");
    else slot.removeAttribute("data-hc-todo-spent");
  }

  function todoCostSweep(list) {
    // Every drawn row's corner, repriced. Nothing is replaced -- see
    // todoCostPaint -- so this is safe while the reader is typing.
    if (!list) return;
    array(todoItems).forEach(function (row) {
      todoCostPaint(row, list.querySelector(
        "[data-hc-todo-line=\"" + row.id + "\"]"));
    });
  }

  // --- what the build is doing ----------------------------------------------
  //
  // A row handed to the builder used to say "building" and stop there, for as
  // long as it took. What the rail says now is the run's own account of
  // itself: how long it has been at it, roughly how much longer it has -- from
  // what a row has taken in this chat before, so the number gets truer as the
  // chat builds -- and the last thing the agent actually did. The whole log is
  // one click away, and so is a terminal window on the run: following its log
  // while it runs, and resuming its session once it has stopped.

  var WATCH_HEAD = {
    running: "building", retrying: "retrying after an API error",
    waiting: "waiting on your answer", cancelled: "stopped",
    failed: "failed", idle: "finished"
  };

  function buildDuration(seconds) {
    var n = Math.max(0, Math.round(+seconds || 0));
    if (n < 60) return n + "s";
    if (n < 3600) return Math.max(1, Math.round(n / 60)) + "m";
    var hours = Math.floor(n / 3600);
    var mins = Math.round((n - hours * 3600) / 60);
    return mins ? (hours + "h " + mins + "m") : (hours + "h");
  }

  function todoWatchLine(run) {
    // The panel's one line, or null when this goal has no build to watch.
    if (!run || typeof run !== "object" || !str(run.status)) return null;
    var head = WATCH_HEAD[str(run.status)] || str(run.status);
    var parts = [head];
    if (typeof run.elapsed_s === "number") {
      parts.push(buildDuration(run.elapsed_s) + " in");
    }
    if (run.running && typeof run.eta_s === "number") {
      // An estimate that has run out says so rather than sliding forward: a
      // number that keeps moving away is worse than none.
      parts.push(run.eta_s > 0 ? "about " + buildDuration(run.eta_s) + " left"
                 : "longer than usual");
    }
    var per = buildDuration(run.per_row_s);
    return {
      head: head,
      running: !!run.running,
      meta: parts.join(" · "),
      last: (run.last && typeof run.last === "object") ? str(run.last.text) : "",
      lines: +run.lines || 0,
      canOpen: !!run.can_open,
      error: str(run.error),
      title: run.measured
        ? "about " + per + " a row, from this chat's own finished builds"
        : "about " + per + " a row — a default, until this chat has built"
          + " something to measure"
    };
  }

  var watchOpen = {};        // goal id -> the log is unfolded
  var watchLines = {};       // goal id -> the lines it was last sent
  var watchFetched = {};     // goal id -> when that was
  var WATCH_REFETCH_MS = 2500;

  function todoWatchRun() {
    var runs = serverState.buildRuns || {};
    return (todoGoalId && runs[todoGoalId]) ? runs[todoGoalId] : null;
  }

  function todoLoadLog(goalId) {
    if (!goalId) return;
    watchFetched[goalId] = Date.now();
    post({ op: "build_log", goal_id: goalId }).then(function (res) {
      watchLines[goalId] = (res && res.ok) ? array(res.lines) : [];
      if (todoGoalId === goalId) renderTodoRail(true);
    });
  }

  function todoWatchSlot(host) {
    // The panel sits between the rows and the actions. Made here rather than
    // in the shell: a workspace served by an older page still gets it.
    var box = host.querySelector(".hc-todo-watch");
    if (box) return box;
    box = document.createElement("div");
    box.className = "hc-todo-watch";
    box.setAttribute("contenteditable", "false");
    var actions = host.querySelector(".hc-todos-actions");
    if (actions && host.insertBefore) host.insertBefore(box, actions);
    else host.appendChild(box);
    return box;
  }

  function renderTodoWatch(host) {
    if (!host) return false;
    var box = todoWatchSlot(host);
    var line = todoWatchLine(todoWatchRun());
    var open = !!watchOpen[todoGoalId];
    var logged = open ? array(watchLines[todoGoalId]) : [];
    // Redrawn only when it would say something different. A poll lands every
    // second and a half; a log rebuilt under the reader would take their
    // scroll position and any line they were selecting with it.
    var key = JSON.stringify([todoGoalId, open, line && line.meta,
                              line && line.last, logged.length]);
    if (box.getAttribute("data-hc-watch-key") !== key) {
      box.setAttribute("data-hc-watch-key", key);
      todoWatchPaint(box, line, open, logged);
    }
    // While the build runs and the log stands open, it is re-read with the
    // state -- but no faster than it can usefully change.
    if (line && line.running && open
        && Date.now() - (watchFetched[todoGoalId] || 0) > WATCH_REFETCH_MS) {
      todoLoadLog(todoGoalId);
    }
    return !!line;
  }

  function todoWatchPaint(box, line, open, logged) {
    while (box.firstChild) box.removeChild(box.firstChild);
    if (!line) {
      box.style.display = "none";
      return false;
    }
    box.style.display = "";
    box.setAttribute("data-hc-watch-state", line.running ? "on" : "off");
    var head = document.createElement("div");
    head.className = "hc-todo-watch-head";
    var dot = document.createElement("span");
    dot.className = "hc-todo-watch-dot";
    head.appendChild(dot);
    var meta = document.createElement("span");
    meta.className = "hc-todo-watch-meta";
    meta.textContent = line.meta;
    meta.title = line.title;
    head.appendChild(meta);
    var log = document.createElement("span");
    log.className = "hc-todo-watch-btn";
    log.textContent = open ? "Hide log" : "Log";
    log.title = "everything this build has done so far";
    log.setAttribute("data-hc-todo-log", str(todoGoalId));
    head.appendChild(log);
    if (line.running || line.canOpen) {
      var term = document.createElement("span");
      term.className = "hc-todo-watch-btn";
      term.textContent = "Terminal";
      term.title = line.running
        ? "open a terminal following this build as it works"
        : "open a terminal on this build's session, where you can carry on";
      term.setAttribute("data-hc-todo-term", str(todoGoalId));
      head.appendChild(term);
    }
    box.appendChild(head);
    var last = document.createElement("div");
    last.className = "hc-todo-watch-last";
    last.textContent = line.last || line.error
      || (line.running ? "starting…" : "");
    last.style.display = last.textContent ? "" : "none";
    box.appendChild(last);
    if (!open) return true;
    var body = document.createElement("div");
    body.className = "hc-todo-watch-log";
    var lines = array(logged);
    if (!lines.length) {
      var empty = document.createElement("div");
      empty.className = "hc-todo-watch-row";
      empty.textContent = "nothing logged yet";
      body.appendChild(empty);
    }
    lines.forEach(function (entry) {
      if (!entry || typeof entry !== "object") return;
      var row = document.createElement("div");
      row.className = "hc-todo-watch-row";
      var when = document.createElement("span");
      when.className = "hc-todo-watch-at";
      when.textContent = str(entry.at).slice(11, 16);
      row.appendChild(when);
      var what = document.createElement("span");
      what.textContent = str(entry.text);
      row.appendChild(what);
      body.appendChild(row);
    });
    box.appendChild(body);
    // Scrolled to the newest line, which is where the reader is looking.
    if (typeof body.scrollHeight === "number") body.scrollTop = body.scrollHeight;
    return true;
  }

  // --- the rows on screen ---------------------------------------------------

  var TODO_STATUS = {
    queued: ["queued", "var(--fnt)"],
    building: ["building", "var(--hc-blue, #58a6ff)"],
    asking: ["needs you", "var(--hc-warn)"],
    done: ["done", "var(--hc-ok)"],
    failed: ["failed", "var(--del)"]
  };

  function todoHost() { return document.querySelector(".hc-todos-list"); }

  function todoLineOf(node) {
    while (node && node !== document) {
      if (node.getAttribute && node.getAttribute("data-hc-todo-line") !== null) return node;
      node = node.parentNode;
    }
    return null;
  }

  function todoIndexOfId(id) {
    if (!todoItems) return -1;
    for (var i = 0; i < todoItems.length; i++) if (todoItems[i].id === id) return i;
    return -1;
  }

  function todoIndexOfLine(node) {
    var line = todoLineOf(node);
    return line ? todoIndexOfId(line.getAttribute("data-hc-todo-line")) : -1;
  }

  function todoCaretIn(line, container, offset) {
    // How many characters from the start of the row's text the point is.
    try {
      var range = document.createRange();
      range.setStart(line, 0);
      range.setEnd(container, offset);
      return range.toString().length;
    } catch (e) { return 0; }
  }

  function todoSelection() {
    // Where the selection is, in rows: null when it is not in the list.
    var sel = window.getSelection ? window.getSelection() : null;
    if (!sel || !sel.rangeCount) return null;
    var anchor = todoLineOf(sel.anchorNode), focus = todoLineOf(sel.focusNode);
    if (!anchor || !focus) return null;
    var a = todoIndexOfId(anchor.getAttribute("data-hc-todo-line"));
    var b = todoIndexOfId(focus.getAttribute("data-hc-todo-line"));
    if (a < 0 || b < 0) return null;
    return { a: a, aCaret: todoCaretIn(anchor, sel.anchorNode, sel.anchorOffset),
             b: b, bCaret: todoCaretIn(focus, sel.focusNode, sel.focusOffset),
             collapsed: sel.isCollapsed };
  }

  function todoPlaceCaret(line, caret) {
    if (!line) return;
    var text = line.firstChild;
    if (!text || text.nodeType !== 3) {
      text = document.createTextNode("");
      line.appendChild(text);
    }
    var at = Math.max(0, Math.min(caret, text.length));
    var host = todoHost();
    if (host && document.activeElement !== host) host.focus();
    try {
      var range = document.createRange();
      range.setStart(text, at);
      range.collapse(true);
      var sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (e) {}
  }

  function todoTyping() {
    return document.activeElement === todoHost()
      || !!(document.activeElement
            && document.activeElement.getAttribute
            && document.activeElement.getAttribute("data-hc-todo-answer") !== null);
  }

  function todoFind(nodes, id) {
    // The live node, not flattenTree's copy: this is the object the rail
    // writes into, and a clone would swallow every save.
    var found = null;
    array(nodes).some(function (node) {
      if (!node || typeof node.id !== "string") return false;
      if (node.id === id) { found = node; return true; }
      found = todoFind(node.children, id);
      return !!found;
    });
    return found;
  }

  function todoSelectedGoal() {
    var id = selectedGoalId();
    if (!id) return null;
    var node = todoFind(readLocalGoals(), id);
    return node ? { id: id, notes: str(node.notes),
                    items: todoNormalize(node.todo_items),
                    prompt: str(node.prompt_md) }
                : null;
  }

  function todoLayState(items, incoming) {
    // The build state the store holds for each row, laid over the rows on
    // screen by id -- status, question, what the build spent and the run
    // history; text, depth and order are the reader's. Answers whether
    // anything changed.
    var held = Object.create(null);
    array(incoming).forEach(function (row) {
      if (row && typeof row.id === "string") held[row.id] = row;
    });
    var changed = false;
    array(items).forEach(function (row) {
      var was = row && held[row.id];
      if (!was) return;
      var status = str(was.status), question = str(was.question);
      var spent = +was.tokens > 0 ? Math.round(+was.tokens) : undefined;
      var past = todoHistoryOn(was);
      if (str(row.status) !== status || str(row.question) !== question
          || row.tokens !== spent || !same(todoHistoryOn(row), past)) {
        row.status = status;
        row.question = question;
        if (spent === undefined) delete row.tokens;
        else row.tokens = spent;
        if (past.length) row.history = past;
        else delete row.history;
        changed = true;
      }
    });
    return changed;
  }

  function todoLoad(goal) {
    todoGoalId = goal.id;
    todoItems = goal.items.length ? todoSectioned(goal.items) : [todoRow("", 0)];
    todoPicked = {};
    todoBuildError = "";
    todoReopening = null;
  }

  function todoStamp(now) {
    var when = now || new Date();
    var hour = when.getHours();
    var suffix = hour < 12 ? "AM" : "PM";
    hour = hour % 12;
    if (!hour) hour = 12;
    var minute = when.getMinutes();
    return "saved " + hour + ":" + (minute < 10 ? "0" : "") + minute
      + " " + suffix;
  }

  // The fields of a goal the rail owns: written here, read by the artifact
  // only at boot. When the artifact saves its own tree, these come from the
  // store -- the last thing the rail (or the server, through the sync)
  // wrote -- never from the artifact's memory of them.
  var RAIL_FIELDS = ["todo_items", "todos_md", "prompt_md"];

  function railFields(goals, stored) {
    var held = flattenTree(stored === undefined ? readLocalGoals() : stored).map;
    // The store is the artifact's source for these fields, and the store is
    // behind the screen by up to one save window: the rail follows every
    // keystroke into todoItems and writes 600ms after the last one. An
    // artifact save landing inside that window -- a filter chip, a selection,
    // a title -- would carry the store's older copy of the row being typed,
    // which for a row opened a moment ago has no words in it at all. That
    // copy wins the next merge (it differs from the synced base) and reaches
    // the server, and the row the reader typed reads back blank on the next
    // load. So the rail's own rows, as they are on screen, come first --
    // while there is a write outstanding, and only then: with nothing
    // pending the store is the newer of the two, since that is where a
    // build, another tab or the other person in a shared workspace lands
    // first and the rail picks them up from it on its next sweep.
    var live = (todoGoalId && todoItems && todoSaveTimer)
      ? { todo_items: todoCopyRows(todoItems),
          todos_md: todoSerialize(todoItems) }
      : null;
    var lay = function (nodes) {
      return array(nodes).map(function (node) {
        if (!node || typeof node.id !== "string") return node;
        var was = held[node.id];
        var out = {};
        Object.keys(node).forEach(function (key) { out[key] = node[key]; });
        if (was) {
          RAIL_FIELDS.forEach(function (key) {
            if (key in was.value) out[key] = was.value[key];
          });
        }
        if (live && node.id === todoGoalId) {
          out.todo_items = live.todo_items;
          out.todos_md = live.todos_md;
        }
        if (Array.isArray(node.children)) out.children = lay(node.children);
        return out;
      });
    };
    return lay(goals);
  }

  function writeGoalsLocal(goals) {
    var saved;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); }
    catch (e) { saved = {}; }
    saved.goals = goals;
    saved.updatedAt = Date.now();
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) {}
    lastObservedGoals = JSON.stringify(goals);
    todoSavedLabel = todoStamp();
    return importGoals(goals);
  }

  // What todoSaveNow answered, as something to wait on: the import's own
  // promise where there was a write, and an already-settled one where there
  // was nothing to write because the store holds the rows already.
  function todoSaved(saved) {
    return saved && typeof saved.then === "function"
      ? saved : Promise.resolve(saved);
  }

  function todoSaveNow() {
    if (todoSaveTimer) { clearTimeout(todoSaveTimer); todoSaveTimer = null; }
    if (!todoGoalId || !todoItems) return false;
    var goals = readLocalGoals();
    var node = todoFind(goals, todoGoalId);
    if (!node) return false;
    var next = todoCopyRows(todoItems);
    if (same(todoNormalize(node.todo_items), next)) return false;
    // Its own field, written through the same tree import the notes editor
    // uses -- and never the notes: the list and the document are two things.
    // The server lays its own build state back over these rows by id.
    node.todo_items = next;
    node.todos_md = todoSerialize(next);
    // The import's own promise, not merely "yes": a build pressed on a row
    // typed a moment ago must wait for that row's text to reach the server.
    return writeGoalsLocal(goals);
  }

  function todoSaveSoon() {
    if (todoSaveTimer) clearTimeout(todoSaveTimer);
    todoSaveTimer = setTimeout(todoSaveNow, 600);
  }

  // The rail writes 600ms after the last keystroke, and until then the row is
  // on screen and nowhere else. Blur flushes it; leaving the page did not --
  // so a Cmd+R a moment after typing, or the tab closed on it, took the words
  // with it and the row came back blank. Writing the store on the way out is
  // not enough either: the next load rebuilds the store from /api/state, so
  // anything the server never heard is overwritten by what it holds. The way
  // out has to reach the server.
  //
  // sendBeacon, not the fetch every other write goes through and not a
  // synchronous XHR either: a page being dismissed is exactly the case fetch
  // is cancelled in, and browsers have refused synchronous XHR from an unload
  // handler since 2020. A beacon is handed to the browser to deliver after
  // the page is gone, which is the one guarantee that helps here.
  //
  // Revision-guarded like every other import, so a page on its way out cannot
  // overwrite work that landed while it sat there -- the server refuses it and
  // nothing here is left to hear that, which is the price of leaving late.
  // Answers whether it handed anything over, which is what a test can check
  // without unloading a page.
  function todoFlushOnExit() {
    // Only the rows: the Prompt tab is read, not typed into, since the
    // prompt became the assembled string itself -- so there is no second
    // field with a timer of its own to flush. (There was; a call to its
    // old flush from here threw on every unload, and the rows it was
    // meant to carry out went with the page.)
    if (!todoSaveTimer) return false;
    // A read-only workspace refuses every write, and leaving one is not the
    // exception -- post() stops the ordinary path here and this is the same
    // path by another route.
    if (document.documentElement
        && document.documentElement.getAttribute("data-hc-readonly") !== null) {
      return false;
    }
    todoSaveNow();
    var synced = readSync();
    if (!synced) return false;
    var body = JSON.stringify({ goals: readLocalGoals(),
                                base_revision: synced.revision });
    try {
      if (navigator && typeof navigator.sendBeacon === "function") {
        // The type is the request's Content-Type, and /api/import refuses
        // anything that is not JSON.
        return navigator.sendBeacon(
          "/api/import", new Blob([body], { type: "application/json" }));
      }
    } catch (e) { /* fall through to the request below */ }
    try {
      var request = new XMLHttpRequest();
      request.open("POST", "/api/import", true);
      request.setRequestHeader("Content-Type", "application/json");
      request.send(body);
      return true;
    } catch (e) {
      // A page that is leaving has nowhere to report this. The store still
      // holds the rows for a load that happens to read it first.
      return false;
    }
  }

  function todoApply(result) {
    if (!result) return false;
    todoItems = result.items;
    todoFocusAt = { index: result.index, caret: result.caret };
    todoSaveSoon();
    return true;
  }

  function todoSyncLine(line) {
    // Text alone never redraws: the row the reader is typing in is the one
    // thing on screen that must not be replaced underneath them.
    var index = todoIndexOfLine(line);
    if (index < 0 || !todoItems) return;
    var text = str(line.textContent).replace(/\n/g, "");
    if (todoItems[index].text !== text) {
      todoItems[index].text = text;
      // The row's price follows what it now says -- the corner alone, never
      // the row: see todoCostPaint.
      todoCostPaint(todoItems[index], line);
      todoSaveSoon();
    }
  }

  function todoKey(event) {
    // The row a key is about is wherever the selection's focus is: the
    // whole list is one editing host, so the event's target is the host.
    var where = todoSelection();
    if (!todoItems) return;
    if (!where) {
      // Cmd+Enter is the build wherever the caret is -- and after a pick
      // from the gutter it is in no row at all. Once: not once to land the
      // caret and once more to build.
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        todoBuild();
        return;
      }
      // The caret is in the host but outside every row's text (between
      // rows, beside a gutter). Nothing may be typed there: put it at the
      // end of the last row still being written -- not a row out with the
      // builder or done, which sit below -- and take the key from the
      // browser.
      if ((event.key.length === 1 && !event.metaKey && !event.ctrlKey)
          || event.key === "Backspace" || event.key === "Delete"
          || event.key === "Enter") {
        event.preventDefault();
        var last = todoItems.length - 1;
        for (var k = todoItems.length - 1; k >= 0; k--) {
          if (!todoItems[k].status) { last = k; break; }
        }
        todoFocusAt = { index: last, caret: null };
        renderTodoRail(true);
      }
      return;
    }
    var index = where.b;
    var line = todoLineOf(window.getSelection().focusNode);
    if (line) todoSyncLine(line);
    var caret = where.collapsed ? where.bCaret : null;
    var mod = event.metaKey || event.ctrlKey;
    var handled = false;
    if (event.key === "Escape") {
      // The row under the caret, or the family it is out with, comes back
      // from the build. A row that is not out is not Escape's business.
      if (todoCancel(index)) {
        event.preventDefault();
        event.stopPropagation();
        renderTodoRail(true);
      }
      return;
    } else if (mod && event.key === "a") {
      // Every pickable row, picked for the build -- the same toggle as the
      // Select all control, not a text selection. Cmd+A again releases them.
      // The caret stays where it was, so the next Cmd+A (or any key) still
      // lands on the list.
      event.preventDefault();
      event.stopPropagation();
      todoToggleAll();
      todoFocusAt = { index: index, caret: caret };
      renderTodoRail(true);
      return;
    } else if (mod && event.key === "/") {
      todoTogglePick(todoItems[index].id);
      // The caret stays on the row it picked, so the next key -- Cmd+Enter
      // -- still finds the list.
      todoFocusAt = { index: index, caret: caret };
      renderTodoRail(true);
      event.preventDefault();
      event.stopPropagation();
      return;
    } else if (mod && event.key === "Enter") {
      // The build draws the list itself, caret on the fresh empty row; a
      // second redraw here would take that caret away again.
      event.preventDefault();
      event.stopPropagation();
      todoBuild();
      return;
    } else if (mod && (event.key === "Backspace" || event.key === "Delete")) {
      handled = todoApply(todoRemove(todoItems, index));
    } else if (mod || event.altKey) {
      // Cmd+C, Cmd+V, Cmd+Z and the rest are the browser's (copy is
      // answered on the copy event, where the rows leave as markdown).
      return;
    } else if (!where.collapsed && where.a !== where.b) {
      // A selection across rows: the browser will not edit across editing
      // hosts, so what a key does to it is ours.
      if (event.key === "Backspace" || event.key === "Delete") {
        handled = todoApply(todoCut(todoItems, where.a, where.aCaret,
                                    where.b, where.bCaret, ""));
      } else if (event.key === "Enter") {
        var cut = todoCut(todoItems, where.a, where.aCaret, where.b, where.bCaret, "");
        handled = !!cut && todoApply(todoEnter(cut.items, cut.index, cut.caret));
      } else if (event.key.length === 1) {
        handled = todoApply(todoCut(todoItems, where.a, where.aCaret,
                                    where.b, where.bCaret, event.key));
      }
    } else if (event.key === "Enter" && !event.shiftKey) {
      handled = todoApply(todoEnter(todoItems, index, caret));
    } else if (event.key === "Tab") {
      if (!todoApply(event.shiftKey ? todoOutdent(todoItems, index)
                                    : todoIndent(todoItems, index))) {
        todoFocusAt = { index: index, caret: caret };
      }
      handled = true;
    } else if (event.key === "Backspace" && where.collapsed && caret === 0) {
      // Ours whether or not it changes anything: left to the browser, a
      // backspace at the head of a row would eat the row's gutter.
      todoApply(todoBackspace(todoItems, index, caret));
      handled = true;
    } else if (event.key === "Delete" && where.collapsed
               && caret === todoItems[index].text.length) {
      if (index < todoItems.length - 1) {
        todoApply(todoBackspace(todoItems, index + 1, 0));
      }
      handled = true;
    }
    if (!handled) return;
    event.preventDefault();
    // Ours, and nobody else's: the artifact listens on the document too, and
    // reads Tab and the arrows as "move the goal selection".
    event.stopPropagation();
    renderTodoRail(true);
  }

  function todoBeforeInput(event) {
    // Every paste, typing over a selection that runs across rows -- and any
    // edit the browser would make outside a row's text (between rows, over
    // a gutter), which is nowhere the model has a place for.
    var where = todoSelection();
    if (!where) { event.preventDefault(); return; }
    if (event.inputType === "insertFromPaste") {
      // Every paste is ours, wherever the selection sits: a pasted list --
      // bullet lines, or bullets whose newlines were lost -- must land as
      // one row per bullet, where the browser's own insertion would mash
      // the whole body into the row holding the caret.
      event.preventDefault();
      // Chromium hands the body in `data` when the host is plain text;
      // the dataTransfer form is the spec's, kept for the browsers on it.
      var body = event.dataTransfer
        ? str(event.dataTransfer.getData("text/plain")) : str(event.data);
      var ground = where.collapsed
        ? { items: todoItems, index: where.b, caret: where.bCaret }
        : todoCut(todoItems, where.a, where.aCaret, where.b, where.bCaret, "");
      if (ground
          && todoApply(todoPaste(ground.items, ground.index, ground.caret, body))) {
        renderTodoRail(true);
      }
      return;
    }
    if (where.collapsed || where.a === where.b) return;
    var put = "";
    if (event.inputType === "insertText") put = str(event.data);
    else if (!/^delete/.test(str(event.inputType))) return;
    event.preventDefault();
    if (todoApply(todoCut(todoItems, where.a, where.aCaret, where.b, where.bCaret, put))) {
      renderTodoRail(true);
    }
  }

  function todoCopyEvent(event) {
    // A selection across rows leaves as the rows it covers, as markdown.
    var where = todoSelection();
    if (!where || where.collapsed || !event.clipboardData) return;
    var text = todoSelectionText(todoItems, where.a, where.aCaret, where.b, where.bCaret);
    if (!text) return;
    event.clipboardData.setData("text/plain", text);
    event.preventDefault();
  }

  function todoFamily(items, index) {
    // The rows one pick covers: the row itself and everything nested under
    // it, ending at the next row back at its own depth or above.
    var rows = array(items);
    if (!rows[index]) return [];
    var head = rows[index].depth || 0;
    var out = [index];
    for (var i = index + 1; i < rows.length; i++) {
      if ((rows[i].depth || 0) <= head) break;
      out.push(i);
    }
    return out;
  }

  // --- taking a row back from the build ------------------------------------
  //
  // A row out with the builder -- queued, building, asking, or failed -- can
  // be pulled back to the active band. The unit is the family, as it was for
  // the pick: the control sits on the family's head (an out row with no out
  // row above it), never on the rows nested under it, and cancelling the
  // head cancels every out row in its family. Escape from a caret inside any
  // row of the family, or from its answer box, is the same act.

  var TODO_OUT = { queued: true, building: true, asking: true, failed: true };
  // Out states a child under an out head does not badge: the head's badge
  // already says the family is with the builder.
  var TODO_CHILD_QUIET = { queued: true, building: true };

  function todoOut(row) {
    return !!(row && TODO_OUT[str(row.status)]);
  }

  function todoCancelHead(items, index) {
    var rows = array(items);
    if (!todoOut(rows[index])) return -1;
    var head = index, depth = rows[index].depth | 0;
    for (var i = index - 1; i >= 0 && depth > 0; i--) {
      if ((rows[i].depth | 0) < depth) {
        depth = rows[i].depth | 0;
        if (todoOut(rows[i])) head = i;
      }
    }
    return head;
  }

  function todoCancelHeads(items) {
    var rows = array(items), heads = [];
    rows.forEach(function (row, i) {
      if (todoOut(row) && todoCancelHead(rows, i) === i) heads.push(i);
    });
    return heads;
  }

  function todoCancelIds(items, index) {
    var rows = array(items);
    if (!todoOut(rows[index])) return [];
    var ids = [];
    // The family counts the head itself.
    todoFamily(rows, index).forEach(function (i) {
      if (todoOut(rows[i])) ids.push(rows[i].id);
    });
    return ids;
  }

  function todoCancel(index) {
    var head = todoCancelHead(todoItems, index);
    if (head < 0 || !todoGoalId) return false;
    var ids = todoCancelIds(todoItems, head);
    var headId = todoItems[head].id;
    todoItems.forEach(function (row) {
      if (ids.indexOf(row.id) >= 0) { row.status = ""; row.question = ""; }
    });
    todoBuildError = "";
    // Back into the active band at once; the server's word follows.
    todoItems = todoSectioned(todoItems);
    todoFocusAt = { index: todoIndexOfId(headId), caret: null };
    var goalId = todoGoalId;
    todoHold();
    post({ op: "cancel_todos", goal_id: goalId, ids: ids }).then(function (res) {
      if ((!res || !res.ok) && todoGoalId === goalId) {
        todoBuildError = (res && res.error) || "the build could not be cancelled";
      }
      todoSettle();
    });
    return true;
  }

  function todoTogglePick(id) {
    var at = todoIndexOfId(id);
    var row = todoItems && todoItems[at];
    if (!row || (row.status && row.status !== "failed")) return;
    // A row with nothing on it is nothing to build: the gutter of the empty
    // row the list keeps for typing into does not pick.
    if (!str(row.text).trim()) return;
    var on = !todoPicked[id];
    // A parent stands for the rows under it: picking it picks its children,
    // and unpicking releases them. Rows already building keep their state.
    todoFamily(todoItems, at).forEach(function (i) {
      var member = todoItems[i];
      if (member.status && member.status !== "failed") return;
      if (on) todoPicked[member.id] = true; else delete todoPicked[member.id];
    });
  }

  function todoPickable() {
    return array(todoItems).filter(function (row) {
      return row.text.trim() && (!row.status || row.status === "failed");
    });
  }

  function todoToggleAll() {
    var rows = todoPickable();
    if (!rows.length) return;
    var all = rows.every(function (row) { return todoPicked[row.id]; });
    todoPicked = {};
    if (!all) rows.forEach(function (row) { todoPicked[row.id] = true; });
  }

  function todoPickedIds() {
    return array(todoItems).filter(function (row) {
      return todoPicked[row.id];
    }).map(function (row) { return row.id; });
  }

  function todoTypingTarget(node) {
    // Whether a key aimed at this node is typing: a field, or an editing
    // host. The list itself is one, and has its own handler.
    var tag = (node && node.tagName) ? String(node.tagName).toUpperCase() : "";
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT"
      || !!(node && node.isContentEditable);
  }

  function todoSole(items) {
    // The rows to build when nothing is picked and there is only one thing
    // to pick: the list's one unsent family (a row, or a row and what is
    // nested under it). Rows with no text are nothing to build and do not
    // count; two unsent families are a choice, and nothing is chosen.
    var rows = array(items), live = [];
    rows.forEach(function (row, i) {
      if (str(row.text).trim() && (!row.status || row.status === "failed")) live.push(i);
    });
    if (!live.length) return [];
    var family = todoFamily(rows, live[0]);
    return live.every(function (i) { return family.indexOf(i) >= 0; }) ? live : [];
  }

  function todoBlankAfter(items, sent) {
    // The rows with a fresh empty row at the foot of the active band -- the
    // place the next TODO gets typed once `sent` have left for the build --
    // or null when an empty row is already there to type into.
    var rows = array(items), bands = todoBandOf(rows);
    var end = 0, blank = false;
    rows.forEach(function (row, i) {
      if (bands[i] !== 0) return;
      end = i + 1;
      if (sent.indexOf(row.id) < 0 && !str(row.text).trim()) blank = true;
    });
    if (blank) return null;
    var fresh = todoRow("", 0);
    var out = rows.slice();
    out.splice(end, 0, fresh);
    return { items: out, id: fresh.id };
  }

  // --- holding the rail's own word until the server's arrives --------------
  //
  // A row just handed off (answered, taken back) shows its new state at
  // once, while the store still says what it said before -- and the sweep
  // reads the store. Until a state fetched AFTER the op has landed, the
  // store is not laid back over the rail; a bound on the wait keeps a lost
  // reply from holding it forever.

  var todoHeld = false, todoHoldSeq = 0, todoHoldTimer = null;

  function todoHold() {
    todoHeld = true;
    todoHoldSeq += 1;
    if (todoHoldTimer) clearTimeout(todoHoldTimer);
    todoHoldTimer = setTimeout(function () {
      todoHoldTimer = null;
      todoHeld = false;
      renderTodoRail(true);
    }, 6000);
  }

  function todoSettle() {
    // A refresh that starts after now; the hold lifts when it has landed.
    var mine = todoHoldSeq, tries = 0;
    var done = function () {
      if (mine === todoHoldSeq) {
        todoHeld = false;
        if (todoHoldTimer) { clearTimeout(todoHoldTimer); todoHoldTimer = null; }
      }
      renderTodoRail(true);
    };
    var tick = function () {
      var going = refreshState();
      if (going) going.then(done, done);
      else if (++tries < 40) setTimeout(tick, 100);
      else done();
    };
    tick();
  }

  function todoBuild() {
    // Build starts the build. Nothing is asked first: the reader picked the
    // rows and pressed the button, and what each row is priced at is already
    // printed in its corner, where they read it before pressing.
    if (todoBuilding || !todoGoalId || !todoItems) return;
    var ids = todoPickedIds();
    if (!ids.length) {
      // Nothing picked and one thing to pick: that is the build.
      ids = todoSole(todoItems).map(function (i) { return todoItems[i].id; });
    }
    if (!ids.length) return;
    todoBuildNow(ids);
  }

  function todoBuildNow(ids) {
    // What comes next is typed into a fresh row, not into one just sent:
    // the row is there before the reader has to ask for it with Enter.
    var blank = todoBlankAfter(todoItems, ids);
    if (blank) todoItems = blank.items;
    // The rows reach the server BEFORE the build is asked for. The two used
    // to leave together and the build, with no import to wait for, regularly
    // won: a row typed inside the save's own window was still blank on the
    // server, which marked that blank row building and then refused the
    // import carrying the text -- the build had moved the revision under it.
    // What came back was a row that said "building" with nothing written on
    // it, and the next merge laid that blank over the reader's own copy.
    // A save that fails still lets the build go: the server has the rows as
    // it last saw them, and a build that never starts is the worse answer.
    var written = todoSaved(todoSaveNow()).catch(function () {});
    todoBuilding = true;
    todoBuildError = "";
    // Building from the moment it is submitted: the server says so too, on
    // its next state, but the rail should not wait for it.
    todoItems.forEach(function (row) {
      if (ids.indexOf(row.id) >= 0) { row.status = "building"; row.question = ""; }
    });
    alertNoteOut(ids);
    todoPicked = {};
    // The rows just handed off drop into the middle band right away.
    todoItems = todoSectioned(todoItems);
    // And the caret lands on the empty active row, ready for the next.
    var at = -1;
    todoItems.some(function (row, i) {
      if (!row.status && !str(row.text).trim()) { at = i; return true; }
      return false;
    });
    if (at >= 0) todoFocusAt = { index: at, caret: 0 };
    // Held before the redraw: the redraw is where the store would be read.
    todoHold();
    renderTodoRail(true);
    var goalId = todoGoalId;
    written.then(function () {
      return post({ op: "build_todos", goal_id: goalId, ids: ids });
    }).then(function (res) {
      todoBuilding = false;
      if (res && res.ok && res.queued && todoGoalId === goalId && todoItems) {
        // Handed to the connected session: it is queued until that
        // session's next turn boundary takes it.
        todoItems.forEach(function (row) {
          if (ids.indexOf(row.id) >= 0 && row.status === "building") row.status = "queued";
        });
      }
      if (!res || !res.ok) {
        todoBuildError = (res && res.error) || "the build could not start";
        if (todoGoalId === goalId && todoItems) {
          todoItems.forEach(function (row) {
            if (ids.indexOf(row.id) >= 0 && row.status === "building") row.status = "";
          });
          // Released rows climb back into the active band.
          todoItems = todoSectioned(todoItems);
        }
      }
      todoSettle();
    });
  }

  function todoAnswer(id, text) {
    if (!todoGoalId || !str(text).trim()) return;
    var goalId = todoGoalId;
    var row = todoItems && todoItems[todoIndexOfId(id)];
    if (row) { row.status = "building"; row.question = ""; }
    alertNoteOut([id]);
    todoHold();
    renderTodoRail(true);
    post({ op: "answer_todo", goal_id: goalId, id: id, answer: text })
      .then(function (res) {
        if (res && res.ok && res.queued && todoGoalId === goalId && row
            && row.status === "building") {
          row.status = "queued";
        }
        if ((!res || !res.ok) && todoGoalId === goalId) {
          todoBuildError = (res && res.error) || "the answer could not be sent";
        }
        todoSettle();
      });
  }

  function todoReopen(id, text) {
    // "No, you did a bad job": the row goes back out on its next run, told
    // what was wrong with the last one. The run that ended is kept, note and
    // all -- so the row's argument survives, and so a failure here can put
    // the row back exactly as it was.
    var note = str(text).trim();
    if (!todoGoalId || !note) return;
    var goalId = todoGoalId;
    var row = todoItems && todoItems[todoIndexOfId(id)];
    if (!row || row.status !== "done") return;
    var held = todoHistoryOn(row);
    row.history = held.concat([{ state: "done", note: note }]);
    row.status = "building";
    row.question = "";
    todoReopening = null;
    todoBuildError = "";
    alertNoteOut([id]);
    // Out of the done band and back into the middle one.
    todoItems = todoSectioned(todoItems);
    todoHold();
    renderTodoRail(true);
    post({ op: "reopen_todo", goal_id: goalId, id: id, note: note })
      .then(function (res) {
        if (todoGoalId === goalId && row) {
          if (res && res.ok && res.queued && row.status === "building") {
            row.status = "queued";
          } else if (!res || !res.ok) {
            todoBuildError = (res && res.error)
              || "the TODO could not be reopened";
            row.status = "done";
            if (held.length) row.history = held;
            else delete row.history;
            todoItems = todoSectioned(todoItems);
          }
        }
        todoSettle();
      });
  }

  function todoCopyAll() {
    var goal = todoSelectedGoal();
    var text = todoCopyText(todoItems, goal ? goal.notes : "");
    var done = function () {
      todoCopied = true;
      clearTimeout(todoCopiedTimer);
      todoCopiedTimer = setTimeout(function () {
        todoCopied = false;
        renderTodoRail(true);
      }, 1600);
      renderTodoRail(true);
    };
    var fallback = function () {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = document.execCommand("copy") === true; } catch (e) { ok = false; }
      ta.remove();
      return ok;
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        if (fallback()) done();
      });
    } else if (fallback()) {
      done();
    }
  }

  function todoClipboardImages(data) {
    // The image files on a clipboard, if any: a screenshot is one file with
    // an image type, a copied image from a page the same. Text-only pastes
    // have none, and stay on the beforeinput path.
    var out = [];
    if (!data) return out;
    // FileList and DataTransferItemList are array-likes, not arrays.
    var list = function (v) {
      try { return Array.prototype.slice.call(v || []); } catch (e) { return []; }
    };
    var files = list(data.files);
    if (!files.length && data.items) {
      list(data.items).forEach(function (item) {
        if (item && item.kind === "file" && /^image\//.test(str(item.type))) {
          var file = item.getAsFile && item.getAsFile();
          if (file) files.push(file);
        }
      });
    }
    files.forEach(function (file) {
      if (file && /^image\//.test(str(file.type))) out.push(file);
    });
    return out;
  }

  function todoUpload(file) {
    // The bytes as they are, to the workspace's own store; the answer is the
    // path the row will remember.
    return fetch("/api/attachment", {
      method: "POST",
      headers: { "Content-Type": str(file.type) || "image/png",
                 "X-HC-Name": str(file.name) || "pasted image" },
      body: file
    }).then(function (r) { return r.json(); }).catch(function () { return null; });
  }

  var todoAttaching = 0;

  function todoPasteImages(files, where) {
    // Each image in turn: uploaded, then marked into the row at the caret.
    // In turn rather than at once, so two screenshots pasted together get
    // consecutive numbers in the order they were on the clipboard.
    if (!todoItems || !files.length) return;
    var ground = where
      ? (where.collapsed
         ? { items: todoItems, index: where.b, caret: where.bCaret }
         : todoCut(todoItems, where.a, where.aCaret, where.b, where.bCaret, ""))
      : { items: todoItems, index: todoItems.length - 1, caret: null };
    if (!ground) return;
    if (ground.items !== todoItems) todoApply(ground);
    var index = ground.index, caret = ground.caret;
    var goalId = todoGoalId;
    todoAttaching += 1;
    todoSavedLabel = "attaching…";
    renderTodoRail(true);
    var step = function (i) {
      if (i >= files.length || todoGoalId !== goalId || !todoItems) {
        todoAttaching -= 1;
        if (!todoAttaching) todoSaveNow();
        renderTodoRail(true);
        return;
      }
      todoUpload(files[i]).then(function (res) {
        if (res && res.ok && res.path && todoGoalId === goalId && todoItems) {
          var row = todoItems[index];
          var result = row && todoAttach(todoItems, index, caret,
                                         { path: res.path, name: res.name });
          if (result) {
            todoApply(result);
            caret = result.caret;
          }
        } else if (todoGoalId === goalId) {
          todoBuildError = (res && res.error) || "the image could not be attached";
        }
        step(i + 1);
      });
    };
    step(0);
  }

  var todoDelegated = false;

  function todoDelegate() {
    // Listeners live on the document, which nothing re-creates. Binding them
    // to the rows themselves worked exactly until the artifact next redrew
    // its own rail, after which every key went to a node with no handlers.
    if (todoDelegated || !document.addEventListener) return;
    todoDelegated = true;
    document.addEventListener("keydown", function (event) {
      var node = event.target;
      if (node === todoHost()) { todoKey(event); return; }
      if (node && node.getAttribute
          && node.getAttribute("data-hc-todo-answer") !== null) {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          todoAnswer(node.getAttribute("data-hc-todo-answer"), node.value);
        } else if (event.key === "Escape") {
          // Withdrawing the question rather than answering it.
          var asked = todoIndexOfId(node.getAttribute("data-hc-todo-answer"));
          if (asked >= 0 && todoCancel(asked)) {
            event.preventDefault();
            event.stopPropagation();
            renderTodoRail(true);
          }
        }
        return;
      }
      if (node && node.getAttribute
          && node.getAttribute("data-hc-todo-reopen") !== null) {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          todoReopen(node.getAttribute("data-hc-todo-reopen"), node.value);
        } else if (event.key === "Escape") {
          // Second thoughts: the row stays done, nothing was said.
          event.preventDefault();
          event.stopPropagation();
          todoReopening = null;
          renderTodoRail(true);
        }
        return;
      }
      // Cmd+Enter from anywhere that is not a place to type -- the Build
      // control just clicked, Select all, the tree -- is the build, the
      // same as from the list. Once: the reader should not have to click
      // back into the list first.
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter"
          && railTab === "todos" && todoItems && !todoTypingTarget(node)) {
        event.preventDefault();
        event.stopPropagation();
        todoBuild();
      }
    }, true);
    document.addEventListener("beforeinput", function (event) {
      // The browser aims the event at the editing host of the range it is
      // about to edit: the list for a selection across rows, the row itself
      // for a caret sitting inside one. Both are ours.
      if (event.target === todoHost() || todoLineOf(event.target)) {
        todoBeforeInput(event);
      }
    }, true);
    document.addEventListener("input", function (event) {
      var typed = event.target;
      if (typed && typed.getAttribute
          && (typed.getAttribute("data-hc-todo-answer") !== null
              || typed.getAttribute("data-hc-todo-reopen") !== null)) {
        // The box grows with its text: one line until it needs two.
        typed.style.height = "auto";
        typed.style.height = typed.scrollHeight + "px";
        return;
      }
      if (typed !== todoHost()) return;
      var sel = window.getSelection();
      var line = sel && todoLineOf(sel.focusNode);
      if (line) todoSyncLine(line);
    }, true);
    document.addEventListener("copy", function (event) {
      if (todoSelection()) todoCopyEvent(event);
    }, true);
    document.addEventListener("paste", function (event) {
      // An image on the clipboard, pasted into the list: ours, before the
      // browser gets to turn it into nothing (or, in a rich host, an <img>
      // the model has no place for). Text pastes pass through untouched to
      // the beforeinput path, which lands them as rows.
      if (!todoTyping() || document.activeElement !== todoHost()) return;
      var files = todoClipboardImages(event.clipboardData);
      if (!files.length) return;
      event.preventDefault();
      event.stopPropagation();
      todoPasteImages(files, todoSelection());
    }, true);
    document.addEventListener("blur", function (event) {
      if (event.target === todoHost()) todoSaveNow();
    }, true);
    // Both, because they fail differently: beforeunload is what a reload and
    // a closed tab fire, and pagehide is what a page put away for the back
    // button fires instead. todoFlushOnExit does nothing when there is
    // nothing outstanding, so the second one is free.
    if (typeof window !== "undefined" && window.addEventListener) {
      window.addEventListener("beforeunload", todoFlushOnExit);
      window.addEventListener("pagehide", todoFlushOnExit);
    }
    document.addEventListener("mousedown", function (event) {
      // The gutter is not a place to type, and the browser knows it: pressing
      // on it collapses the caret to whatever position it can reach -- the top
      // of the list, most often -- which reads as a caret that jumped away
      // from the dash just clicked. We take the press and place the caret
      // ourselves, on the click, at the head of that row's text.
      var node = event.target;
      if (node && node.getAttribute
          && node.getAttribute("data-hc-todo-dash") !== null) {
        event.preventDefault();
      }
    }, true);
    document.addEventListener("click", function (event) {
      var node = event.target;
      if (!node || !node.getAttribute) return;
      // A finished row, clicked: the reader wants another go at it. No
      // hover-only affordance -- the row itself is the control, and the
      // pane it opens is the same thread Claude's own questions use.
      var line = todoLineOf(node);
      var at = line ? todoIndexOfLine(line) : -1;
      var clicked = at >= 0 && todoItems ? todoItems[at] : null;
      if (clicked && clicked.status === "done") {
        todoReopening = clicked.id;
        renderTodoRail(true);
        var box = document.querySelector("[data-hc-todo-reopen]");
        if (box) box.focus();
        return;
      }
      // Anywhere else -- another row, the tree, the buttons -- puts an open
      // pane away. Not inside it: the note is being typed there.
      if (todoReopening && !todoInReopenPane(node)) {
        todoReopening = null;
        renderTodoRail(true);
      }
      var dash = node.getAttribute("data-hc-todo-dash");
      if (dash !== null) {
        todoTogglePick(dash);
        var picked = todoIndexOfId(dash);
        if (picked >= 0) todoFocusAt = { index: picked, caret: 0 };
        renderTodoRail(true);
        return;
      }
      var cancelId = node.getAttribute("data-hc-todo-cancel");
      if (cancelId !== null) {
        event.preventDefault();
        if (todoCancel(todoIndexOfId(cancelId))) renderTodoRail(true);
        return;
      }
      var logFor = node.getAttribute("data-hc-todo-log");
      if (logFor !== null) {
        watchOpen[logFor] = !watchOpen[logFor];
        if (watchOpen[logFor] && !watchLines[logFor]) todoLoadLog(logFor);
        renderTodoRail(true);
        return;
      }
      var termFor = node.getAttribute("data-hc-todo-term");
      if (termFor !== null) {
        node.textContent = "opening…";
        post({ op: "watch_build", goal_id: termFor }).then(function (res) {
          todoBuildError = (res && res.ok) ? ""
            : ((res && res.error) || "could not open a terminal on the build");
          renderTodoRail(true);
        });
        return;
      }
      if (node.className === "hc-todo-copy") { todoCopyAll(); return; }
      if (node.className === "hc-todo-build" || node.getAttribute("data-hc-todo-build") !== null) {
        todoBuild(); return;
      }
      if (node.className === "hc-rail-select") {
        todoToggleAll();
        renderTodoRail(true);
        return;
      }
      if (node.className === "hc-todo-reopen") {
        node.textContent = "opening…";
        post({ op: "reopen_session" }).then(function (res) {
          todoBuildError = (res && res.ok) ? ""
            : ((res && res.error) || "could not reopen the session");
          refreshState();
          renderTodoRail(true);
        });
        return;
      }
      var name = node.getAttribute("data-hc-rail-tab");
      if (name !== "todos" && name !== "prompt") return;
      railTab = name;
      renderTodoRail(true);
    }, true);
  }

  function todoInReopenPane(node) {
    while (node && node !== document) {
      if (node.className === "hc-todo-ask hc-todo-reopen-pane") return true;
      node = node.parentNode;
    }
    return false;
  }

  function todoTabSpan(name, label) {
    var tab = document.createElement("span");
    tab.className = "hc-rail-tab" + (railTab === name ? " hc-rail-tab-on" : "");
    tab.textContent = label;
    // Named by attribute and handled on the document, for the same reason
    // the rows are: a cloned tab keeps its label and loses its listener,
    // which reads as a tab that simply does not switch.
    tab.setAttribute("data-hc-rail-tab", name);
    return tab;
  }

  var TODO_EDITABLE = (function () {
    try {
      var probe = document.createElement("div");
      probe.contentEditable = "plaintext-only";
      return probe.contentEditable === "plaintext-only" ? "plaintext-only" : "true";
    } catch (e) { return "true"; }
  })();

  function todoRowNode(row, head) {
    var wrap = document.createElement("div");
    wrap.className = "hc-todo";
    if (head) {
      // The way back from the build, in the tile's own lower-right corner:
      // on the family's head only, since the family comes back whole.
      wrap.setAttribute("data-hc-todo-head", "");
      var cancel = document.createElement("span");
      cancel.className = "hc-todo-cancel";
      cancel.textContent = "×";
      cancel.title = "cancel the build of this TODO (esc)";
      cancel.setAttribute("data-hc-todo-cancel", row.id);
      cancel.setAttribute("contenteditable", "false");
      wrap.appendChild(cancel);
    }
    var line = document.createElement("div");
    line.className = "hc-todo-row";
    line.style.paddingLeft = (row.depth * 20) + "px";
    // A done row with its pane open is already out of the done band in the
    // reader's head: the strikethrough lifts before the note is sent, so
    // what they are typing about does not read as settled.
    var reopening = row.status === "done" && todoReopening === row.id;
    if (row.status) {
      line.setAttribute("data-hc-todo-state",
                        reopening ? "reopening" : row.status);
    }
    if (todoPicked[row.id]) line.setAttribute("data-hc-todo-picked", "");
    var dash = document.createElement("span");
    dash.className = "hc-todo-dash";
    dash.textContent = "-";
    dash.title = "pick for build (⌘/)";
    dash.setAttribute("data-hc-todo-dash", row.id);
    // The gutter and the badge are islands the caret cannot enter; the
    // row's text is editable by inheritance from the list, which is the
    // one editing host -- never its own, or the selection could not cross.
    dash.setAttribute("contenteditable", "false");
    line.appendChild(dash);
    var text = document.createElement("span");
    text.className = "hc-todo-line";
    // Which row this is, as an attribute rather than a closure: the artifact
    // re-creates the subtree it owns, and a clone keeps attributes while
    // dropping every listener and property bound to the original node.
    text.setAttribute("data-hc-todo-line", row.id);
    text.textContent = row.text;
    line.appendChild(text);
    // The badge names the family's state on its head. A child under an out
    // head that is merely along for the build -- building, queued -- says
    // nothing the head has not; one that needs the user, or failed, still
    // does. Done rows are their own band and always say so.
    var state = TODO_STATUS[row.status];
    if (state && !head && TODO_CHILD_QUIET[row.status]) state = null;
    if (reopening) state = ["reopened", "var(--hc-warn)"];
    var past = todoHistoryOn(row);
    if (state) {
      var badge = document.createElement("span");
      badge.className = "hc-todo-status";
      badge.setAttribute("contenteditable", "false");
      // A row on its second or later go says which: "building · run 2".
      badge.textContent = past.length
        ? state[0] + " · run " + (past.length + 1) : state[0];
      badge.style.color = state[1];
      line.appendChild(badge);
    }
    // What this row will cost to build, in its lower right -- or what it did
    // cost, once it has been built. An island like the gutter and the badge:
    // the caret cannot enter it, and it is kept in step as the reader types
    // without the row being redrawn under them.
    var cost = document.createElement("span");
    cost.className = "hc-todo-cost";
    cost.setAttribute("contenteditable", "false");
    line.appendChild(cost);
    todoCostPaint(row, text);
    wrap.appendChild(line);
    if (past.length) {
      // Every run this row has already had, oldest first, each with what the
      // reader said was wrong with it. Kept under the row rather than
      // replaced by the newest: the row's argument with the builder is the
      // record, and the reader is the only one who can read it.
      var log = document.createElement("div");
      log.className = "hc-todo-runs";
      log.setAttribute("contenteditable", "false");
      log.style.marginLeft = (row.depth * 20 + 22) + "px";
      past.forEach(function (entry, i) {
        var when = document.createElement("div");
        when.className = "hc-todo-run";
        when.textContent = "run " + (i + 1) + " · " + entry.state
          + " · reopened";
        log.appendChild(when);
        if (!entry.note) return;
        var said = document.createElement("div");
        said.className = "hc-todo-run-note";
        said.textContent = "↳ “" + entry.note + "”";
        log.appendChild(said);
      });
      wrap.appendChild(log);
    }
    if (row.status === "asking") {
      var ask = document.createElement("div");
      ask.className = "hc-todo-ask";
      ask.setAttribute("contenteditable", "false");
      ask.style.marginLeft = (row.depth * 20 + 22) + "px";
      var q = document.createElement("div");
      q.className = "hc-todo-question";
      q.textContent = row.question || "Claude has a question about this one.";
      ask.appendChild(q);
      var reply = document.createElement("div");
      reply.className = "hc-todo-reply";
      var arrow = document.createElement("span");
      arrow.className = "hc-todo-arrow";
      arrow.textContent = "↳";
      reply.appendChild(arrow);
      // A textarea, not an input: an answer longer than the rail is wide
      // wraps onto another line and the box grows with it, where an input
      // would scroll the start of it out of sight. Enter still sends.
      var answer = document.createElement("textarea");
      answer.className = "hc-todo-answer";
      answer.rows = 1;
      answer.setAttribute("rows", "1");
      answer.placeholder = "answer, then enter";
      answer.spellcheck = false;
      answer.setAttribute("data-hc-todo-answer", row.id);
      reply.appendChild(answer);
      ask.appendChild(reply);
      wrap.appendChild(ask);
    }
    if (row.status === "done" && todoReopening === row.id) {
      // The reader's side of the same thread: not Claude asking what to do,
      // the reader saying what was wrong. Same shape on purpose -- it is the
      // one place in the rail where the two of them talk about a row.
      wrap.appendChild(todoReopenNode(row));
    }
    return wrap;
  }

  function todoReopenNode(row) {
    var pane = document.createElement("div");
    pane.className = "hc-todo-ask hc-todo-reopen-pane";
    pane.setAttribute("contenteditable", "false");
    pane.style.marginLeft = (row.depth * 20 + 22) + "px";
    var q = document.createElement("div");
    q.className = "hc-todo-question";
    q.textContent = "What went wrong?";
    pane.appendChild(q);
    var reply = document.createElement("div");
    reply.className = "hc-todo-reply";
    var arrow = document.createElement("span");
    arrow.className = "hc-todo-arrow";
    arrow.textContent = "↳";
    reply.appendChild(arrow);
    var note = document.createElement("textarea");
    note.className = "hc-todo-answer";
    note.rows = 1;
    note.setAttribute("rows", "1");
    note.placeholder = "say what to fix, then enter";
    note.spellcheck = false;
    note.setAttribute("data-hc-todo-reopen", row.id);
    reply.appendChild(note);
    pane.appendChild(reply);
    return pane;
  }

  function renderTodoRail(force) {
    if (serverState.scope !== "chat") return false;
    todoDelegate();
    var host = document.querySelector(".hc-todos");
    var list = todoHost();
    var tabs = document.querySelector(".hc-rail-tabs");
    var stamp = document.querySelector(".hc-rail-saved");
    var select = document.querySelector(".hc-rail-select");
    var promptBox = document.querySelector(".hc-rail-prompt");
    if (!host || !list || !tabs) return false;

    var goal = todoSelectedGoal();
    if (!goal || goal.id !== todoGoalId) {
      // Whatever the reader typed into the goal the rail is leaving is
      // written before the rail forgets which goal that was: a save still
      // in its window would otherwise fire with nothing to write into.
      todoSaveNow();
    }
    if (!goal) {
      todoGoalId = null;
      todoItems = null;
    } else if (goal.id !== todoGoalId) {
      todoLoad(goal);
      force = true;
    } else if (!todoHeld) {
      // The server's build state -- and inference's additions -- reach the
      // rail here, but never while the rail is ahead of the store on an op
      // of its own. An empty list is drawn as one empty row to type into;
      // that row is the rail's own and is never "incoming".
      var blank = todoItems && todoItems.length === 1 && !todoItems[0].text;
      var incoming = goal.items.length ? todoSectioned(goal.items) : null;
      if (!todoTyping() && !todoSaveTimer) {
        if (incoming ? !same(incoming, todoItems) : !blank) {
          todoItems = incoming || [todoRow("", 0)];
          force = true;
        }
      } else if (incoming && todoItems && document.activeElement === todoHost()) {
        // Mid-edit, the rows are the reader's: nothing replaces the one
        // they are typing in. The server's word on a row's STATE still
        // lands, though -- laid over the rows by id, text untouched -- or a
        // build finishing while they type the next TODO would never show.
        // The caret is put back where it was, by row id, since the rows
        // may re-band.
        if (todoLayState(todoItems, incoming)) {
          todoItems = todoSectioned(todoItems);
          force = true;
        }
      }
    }

    if (tabs.children.length !== 2 || force) {
      while (tabs.firstChild) tabs.removeChild(tabs.firstChild);
      tabs.appendChild(todoTabSpan("todos", "TODOs"));
      tabs.appendChild(todoTabSpan("prompt", "Prompt"));
    }
    if (stamp) stamp.textContent = todoSavedLabel;
    host.style.display = railTab === "todos" ? "flex" : "none";
    if (promptBox) {
      // "flex", not "block": this is the column the prompt stretches inside,
      // and an inline display beats the stylesheet that made it one -- which
      // left the field exactly one line tall.
      promptBox.style.display = railTab === "prompt" ? "flex" : "none";
      // With no goal selected there is no prompt and so no context to it;
      // the invitation to select one is the whole of the tab.
      if (!goal) promptCtxDrop(promptBox);
    }
    if (select) {
      var pickable = goal && railTab === "todos" ? todoPickable() : [];
      select.style.display = pickable.length ? "" : "none";
      var every = pickable.length && pickable.every(function (row) {
        return todoPicked[row.id];
      });
      select.textContent = every ? "Deselect all" : "Select all";
    }
    if (goal && railTab === "prompt") renderPromptTab(goal);
    // The rows are priced off the same preview the Prompt tab prints, so the
    // TODO tab asks for it too -- once per goal; it wants the size, not the
    // text. Read-only workspaces have no chat to compose one from.
    else if (goal && railTab === "todos"
             && !(document.documentElement
                  && document.documentElement
                      .getAttribute("data-hc-readonly") !== null)) {
      promptCtxLoad(goal, true);
    }
    if (railTab !== "todos") return true;

    var actions = host.querySelector(".hc-todos-actions");
    if (actions) {
      actions.style.display = goal ? "" : "none";
      var copy = actions.querySelector(".hc-todo-copy");
      if (copy) copy.textContent = todoCopied ? "copied ✓" : "Copy TODOs";
      var build = actions.querySelector(".hc-todo-build");
      if (build) {
        var picked = todoPickedIds().length;
        build.textContent = todoBuilding ? "Building…"
          : picked ? "Build " + picked : "Build";
        build.setAttribute("data-hc-todo-build", picked ? "on" : "off");
      }
      var note = actions.querySelector(".hc-todo-error");
      if (note) {
        while (note.firstChild) note.removeChild(note.firstChild);
        var session = serverState.buildSession;
        var queued = array(todoItems).some(function (row) {
          return row.status === "queued";
        });
        if (todoBuildError) {
          note.textContent = todoBuildError;
          note.className = "hc-todo-error hc-todo-note-bad";
        } else if (session && session.ended_at) {
          // The session this workspace belongs to has ended: nothing will
          // take a build until it is back. Offer to bring it back.
          note.className = "hc-todo-error hc-todo-note";
          note.appendChild(document.createTextNode("session closed · "));
          var reopen = document.createElement("span");
          reopen.className = "hc-todo-reopen";
          reopen.textContent = "Reopen";
          note.appendChild(reopen);
        } else if (queued) {
          note.className = "hc-todo-error hc-todo-note";
          note.textContent = (session && session.mode === "headless")
            ? "queued — starts when the running build finishes"
            : "queued — Claude picks it up when its turn ends"
              + " or on your next message";
        }
        note.style.display = note.firstChild ? "" : "none";
      }
    }
    renderTodoWatch(host);
    if (!goal || !todoItems) {
      while (list.firstChild) list.removeChild(list.firstChild);
      return true;
    }
    var shape = todoItems.map(function (row) {
      return [row.id, row.depth, row.status, row.question, !!todoPicked[row.id],
              +row.tokens || 0];
    });
    var drawn = list.getAttribute("data-hc-todo-shape");
    if (!force && drawn === JSON.stringify(shape) && list.children.length) {
      // The rows stand, but what a build costs here may have moved under
      // them -- a finished build is a new sample, and the goal tree every
      // build carries grows as the reader writes. The corners follow it.
      todoCostSweep(list);
      return true;
    }
    // A redraw replaces every row on screen. It must not take the caret
    // with it: where the reader was typing is kept by row id (the rows may
    // have re-banded under them) and put back once the rows are drawn. An
    // answer half-typed into a question's box is kept the same way.
    var active = document.activeElement;
    if (!todoFocusAt && active === list) {
      var kept = todoSelection();
      if (kept && todoItems[kept.b]) {
        todoFocusAt = { index: kept.b, caret: kept.collapsed ? kept.bCaret : null };
      }
    }
    var reply = null;
    if (active && active.getAttribute
        && active.getAttribute("data-hc-todo-answer") !== null) {
      reply = { id: active.getAttribute("data-hc-todo-answer"),
                value: str(active.value),
                at: typeof active.selectionStart === "number" ? active.selectionStart : null };
    }
    list.setAttribute("data-hc-todo-shape", JSON.stringify(shape));
    list.setAttribute("contenteditable", TODO_EDITABLE);
    list.setAttribute("spellcheck", "false");
    while (list.firstChild) list.removeChild(list.firstChild);
    // A rule between the bands: active rows, then rows out with the
    // builder, then done ones. The caret cannot land on it, like the
    // gutters -- it is a line, not a row.
    var bands = todoBandOf(todoItems);
    var heads = todoCancelHeads(todoItems);
    todoItems.forEach(function (row, i) {
      if (i && bands[i] !== bands[i - 1]) {
        var sep = document.createElement("div");
        sep.className = "hc-todo-sep";
        sep.setAttribute("contenteditable", "false");
        list.appendChild(sep);
      }
      list.appendChild(todoRowNode(row, heads.indexOf(i) >= 0));
    });
    var focus = todoFocusAt;
    todoFocusAt = null;
    if (focus) {
      var at = Math.max(0, Math.min(focus.index, todoItems.length - 1));
      var line = list.querySelector("[data-hc-todo-line=\"" + todoItems[at].id + "\"]");
      if (line) {
        var caret = (focus.caret === null || focus.caret === undefined)
          ? todoItems[at].text.length : focus.caret;
        todoPlaceCaret(line, caret);
      }
    }
    if (reply) {
      var box = list.querySelector("[data-hc-todo-answer=\"" + reply.id + "\"]");
      if (box) {
        box.value = reply.value;
        try {
          box.focus();
          if (reply.at !== null && box.setSelectionRange) {
            box.setSelectionRange(reply.at, reply.at);
          }
          box.style.height = "auto";
          box.style.height = box.scrollHeight + "px";
        } catch (e) {}
      }
    }
    return true;
  }

  // --- the prompt the build opens on ----------------------------------------
  //
  // The rail's Prompt tab is the prompt itself and nothing else. It used to be
  // a box to type in -- the goal's `prompt_md`, one paragraph appended to a
  // context assembled out of sight -- with that context printed above it. Two
  // halves of one string, on screen apart, and the half that mattered was the
  // one nobody could edit. The box is gone.
  //
  // What is left is the whole string: the project, the goal tree, this goal's
  // TODO rows and the build protocol. The server composes it on request --
  // prompt_preview is build.compose_prompt over the rows a Build would send --
  // so what the tab prints and what the build opens on are one text, read
  // before it goes rather than inferred from a sentence promising it exists.

  var promptGoalId = null;

  var promptCtxKey = "";        // the tree and picks the held context is for
  var promptCtxText = "";
  var promptCtxBusy = false;
  var promptCtxFold = false;
  // What that same string costs with no rows in it, as the server counted it,
  // and the goal it was counted for. This is what a TODO row's corner prices
  // itself against -- see todoCostBasis -- so the number beside a row and the
  // number above the field are one measurement of one string.
  var promptCtxCost = { goal: "", context: 0 };

  function promptCtxSignature(goal) {
    // Anything in the tree can move the context, so the tree is the key --
    // together with which rows are picked, since those are the rows the
    // previewed build would carry.
    var tree = "";
    try { tree = JSON.stringify(readLocalGoals()); } catch (e) { tree = ""; }
    return goal.id + "|" + todoPickedIds().join(",") + "|" + tree;
  }

  function promptCtxLoad(goal, once) {
    // One request per shape of the tree. A failure keeps the key too: the
    // rail asks again when something actually changes, rather than every
    // 700ms for as long as the tab is open.
    //
    // `once` is the TODO tab asking, which wants only the size of the context
    // and not the text of it: one request per goal, whatever came back, since
    // the rows there are being typed in and re-previewing every keystroke
    // would buy a number that rounds to the same thing. Opening the Prompt
    // tab, or moving to another goal, asks again.
    var key = promptCtxSignature(goal);
    var goalId = goal.id;
    if (promptCtxKey.indexOf(goalId + "|") !== 0) {
      // Another goal's context is not this one's, and showing it while the
      // new one is fetched would say the build carries what it does not.
      promptCtxText = "";
      window.__hcPromptContext = "";
    }
    if (once && promptCtxKey.indexOf(goalId + "|") === 0) return;
    if (promptCtxBusy || key === promptCtxKey) return;
    promptCtxBusy = true;
    post({ op: "prompt_preview", goal_id: goalId, ids: todoPickedIds() })
      .then(function (res) {
        promptCtxBusy = false;
        // The reader moved on. Either name for the rail's goal will do: the
        // TODO tab asks without ever having set the prompt tab's.
        if (promptGoalId !== goalId && todoGoalId !== goalId) return;
        promptCtxKey = key;
        promptCtxText = (res && res.ok && typeof res.prompt === "string")
          ? res.prompt : "";
        window.__hcPromptContext = promptCtxText;
        promptCtxCost = (res && res.ok && +res.context_tokens > 0)
          ? { goal: goalId, context: Math.round(+res.context_tokens) }
          : { goal: "", context: 0 };
        // The corners are repriced in place rather than redrawn: the reader
        // is very likely typing in one of those rows.
        if (railTab === "prompt") renderTodoRail(true);
        else todoCostSweep(todoHost());
      });
  }

  function promptCtxNode(box) {
    // Last child of the rail's prompt column and drawn first (`order:-1`):
    // the artifact owns the children its own template declares, and an
    // injected node that sat between them would be a node in one of their
    // places the next time it re-renders.
    if (!box || !box.querySelector) return null;
    var node = box.querySelector(".hc-rail-ctx");
    if (node) return node;
    node = document.createElement("div");
    node.className = "hc-rail-ctx";
    node.style.order = "-1";
    var head = document.createElement("div");
    head.className = "hc-rail-ctx-head";
    var title = document.createElement("span");
    title.className = "hc-rail-ctx-title";
    title.textContent = "PROMPT";
    var note = document.createElement("span");
    note.className = "hc-rail-ctx-note";
    var fold = document.createElement("span");
    fold.className = "hc-rail-ctx-fold";
    head.appendChild(title);
    head.appendChild(note);
    head.appendChild(fold);
    head.onclick = function () {
      promptCtxFold = !promptCtxFold;
      renderTodoRail(true);
    };
    var body = document.createElement("pre");
    body.className = "hc-rail-ctx-body";
    node.appendChild(head);
    node.appendChild(body);
    box.appendChild(node);
    return node;
  }

  function promptCtxDrop(box) {
    var node = box && box.querySelector && box.querySelector(".hc-rail-ctx");
    if (node && node.parentNode) node.parentNode.removeChild(node);
  }

  function renderPromptCtx(goal) {
    var box = document.querySelector(".hc-rail-prompt");
    if (!box) return;
    // A shared workspace stands on no vault: there is no chat here to compose
    // a build's prompt out of. The tab is the whole of what a guest is shown
    // now, so it says where the prompt lives rather than going blank.
    var guest = !!(document.documentElement
      && document.documentElement.getAttribute("data-hc-readonly") !== null);
    if (!guest) promptCtxLoad(goal);
    var node = promptCtxNode(box);
    if (!node) return;
    var text = guest ? str(goal.prompt) : promptCtxText;
    var body = node.querySelector(".hc-rail-ctx-body");
    var note = node.querySelector(".hc-rail-ctx-note");
    var fold = node.querySelector(".hc-rail-ctx-fold");
    if (body && body.textContent !== text) {
      body.textContent = text;
    }
    if (note) {
      note.textContent = guest
        ? "assembled on the owner's machine"
        : (text
           ? "~" + todoTokenLabel(Math.ceil(text.length / 4)) + " tok"
           : (promptCtxBusy ? "assembling…" : "could not be assembled"));
      note.title = (!guest && text)
        ? "The project, this chat's goal tree, this goal's TODO rows and "
          + "how to work — the whole of what a build of these rows sends."
        : "";
    }
    if (fold) fold.textContent = promptCtxFold ? "show" : "hide";
    if (promptCtxFold) node.setAttribute("data-hc-fold", "");
    else node.removeAttribute("data-hc-fold");
  }

  function renderPromptTab(goal) {
    // Which goal the held prompt is for, set before it is asked for: the
    // reply from prompt_preview is dropped unless this still names its goal.
    promptGoalId = goal.id;
    renderPromptCtx(goal);
  }

  // Kept in step with ui.CHAT_GROUND, which paints the same colour into the
  // mask the server serves. Two writers, one ground: the server owns the
  // frames before the unpack, this owns every frame after it.
  var CHAT_GROUND = "#0d1117";

  function groundColor() {
    // What the workspace will land on, decided the way the served mask decides
    // it: the reader's own choice if they have made one, dark otherwise. A
    // fresh port is a fresh origin, so most opens have nothing saved.
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); }
    catch (e) { saved = null; }
    return (saved && saved.themeMode === "light") ? "#fff" : CHAT_GROUND;
  }

  function holdRoot(root) {
    // `visibility:hidden` hides the element, not the viewport canvas: the
    // canvas keeps painting the background propagated from the root, and
    // where the root has none it falls through to the body's -- which in this
    // artifact is white. So holding the page is two things, not one. Hiding
    // it alone is what turned the hold into the flash it was added to remove.
    if (!root || !root.style) return;
    if (root.style.visibility !== "hidden") root.style.visibility = "hidden";
    var ground = groundColor();
    if (root.style.background !== ground) root.style.background = ground;
  }

  function releaseRoot(root) {
    // Both, and in this order: a root that keeps the ground after the reveal
    // would fight the reader's own theme at the edges the workspace does not
    // cover.
    if (!root || !root.style) return;
    root.style.visibility = "";
    root.style.background = "";
  }

  function revealWhenDressed() {
    // The server hides the document until the artifact unpacks its template,
    // because what it paints before that is not this product. The unpack
    // takes that mask away with the rest of the original head -- and for one
    // to several frames after it, the page is the artifact's own two-column
    // light layout, which then rearranges when the skin lands. That is the
    // same flash one step later. Hold the page for those frames instead:
    // re-hide on whatever documentElement now is, dress it, then show it.
    if (serverState.scope !== "chat") return;
    var clock = function () {
      return (window.performance && performance.now)
        ? performance.now() : Date.now();
    };
    var started = clock();
    var elapsed = function () { return clock() - started; };
    var frames = 0;
    var show = releaseRoot;
    var step = function () {
      var root = document.documentElement;
      if (!root) return;
      holdRoot(root);
      var dressed;
      try {
        applyLaunchSkin();
        mirrorRootState();
        renderChatSurface();
        dressed = launchDressed();
      } catch (e) {
        // Nothing here is worth a page the reader cannot see.
        show(root);
        return;
      }
      // Two seconds is the failsafe, not the plan: a page nobody can see is
      // worse than a page that arrives badly dressed. Counted in time rather
      // than frames, because the machine that needs the failsafe is the one
      // whose frames are slow.
      // Two bounds, because they fail differently: a slow machine runs out
      // of time, and a host whose animation frames are synchronous runs out
      // of stack long before any clock notices.
      if (dressed || elapsed() > 2000 || ++frames > 240) {
        show(root);
        return;
      }
      (window.requestAnimationFrame || function (fn) { setTimeout(fn, 16); })(step);
    };
    step();
  }

  function applyLaunchSkin() {
    // Chat scope only, and only once the artifact has unpacked its template
    // over documentElement -- which is why this is re-asserted by the same
    // standing sweep that keeps the tab named.
    if (serverState.scope !== "chat") return false;
    var root = document.documentElement;
    var changed = false;
    if (root && root.setAttribute
        && root.getAttribute("data-hc-launch") !== "chat") {
      root.setAttribute("data-hc-launch", "chat");
      changed = true;
    }
    if (!document.getElementById("hc-launch-style")) {
      var style = document.createElement("style");
      style.id = "hc-launch-style";
      style.textContent = LAUNCH_CSS;
      (document.head || document.documentElement).appendChild(style);
      changed = true;
      launchApplied = true;
    }
    return changed;
  }

  // The launch stylesheet lives on the root so it can reach the cards, which
  // are parented on <body>. The one fact it cannot read from there is which
  // theme the artifact is drawing.
  function mirrorRootState() {
    var root = document.documentElement;
    if (!root || !root.setAttribute) return false;
    var app = document.querySelector(".hc");
    var theme = (app && app.getAttribute && app.getAttribute("data-dark") === "true")
      ? "dark" : "light";
    var changed = false;
    if (root.getAttribute("data-hc-theme") !== theme) {
      root.setAttribute("data-hc-theme", theme);
      changed = true;
    }
    return changed;
  }

  // Whether this chat is still being read, and how far.
  //
  // The server has always reported this; nothing drew it, so a workspace
  // catching up on a long chat looked identical to one where inference was
  // broken -- an empty tree and no way to tell which. That ambiguity is the
  // whole reason this exists: it is not a control, only a sentence.
  function analyzerLine(state) {
    var a = state && state.analyzer;
    if (!a) return "";
    var done = Number(a.last_analyzed_ordinal || 0);
    var want = Number(a.requested_ordinal || 0);
    if (a.status === "error") {
      return "could not read this chat" + (a.error ? ": " + str(a.error) : "");
    }
    // Caught up still says so. Silence here is what made a working
    // workspace and a broken one look the same; an empty tree with nothing
    // in the corner gives a reader no way to tell "nothing to infer" from
    // "nothing is running".
    if (want <= done) {
      return done ? "up to date · " + done.toLocaleString() + " read"
                  : "nothing read from this chat yet";
    }
    // "running" is the only status that means a worker holds the lease and
    // is actually reading. "pending" means the work is queued and nobody
    // has picked it up -- which is what a dead hook leaves behind, and
    // saying "reading" for it would claim progress that is not happening.
    if (a.status === "running") {
      return "reading this chat · " + done.toLocaleString() + " of "
             + want.toLocaleString();
    }
    // Behind with nobody reading: the corner says nothing. A backlog count
    // is not something the reader can act on -- the queue drains or it does
    // not -- and it sat there as a permanent number in the corner of the
    // tree. Breakage still speaks: "error" above says so in words.
    return "";
  }

  // Overview and Goals, always on screen. They used to live inside the
  // overview panel, which meant the way back was visible only once you
  // were already there -- and nothing on the goals page said the overview
  // existed at all. They sit above the filter chips: what you are looking
  // at, then how much of it.
  function renderViewTabs() {
    if (serverState.scope !== "chat") return false;
    // Inside .hc, not on the body: the palette -- --ink, --bg, --fnt, and
    // the dark override -- is defined on .hc, so anything parked outside it
    // inherits none of them and renders as black text on nothing.
    var host = document.querySelector(".hc") || document.body
               || document.documentElement;
    if (!host || !host.appendChild) return false;
    var tabs = document.querySelector(".hc-viewtabs");
    if (!tabs) {
      tabs = el("div", "hc-viewtabs");
      [["overview", "Overview"], ["goals", "Goals"]].forEach(function (pair) {
        var tab = el("span", "hc-viewtab", pair[1]);
        tab.setAttribute("data-hc-viewtab", pair[0]);
        tab.setAttribute("role", "button");
        tabs.appendChild(tab);
      });
      host.appendChild(tabs);
    }
    // Everything below is measured from --hc-top; the bar takes its own
    // strip, so the root says so and the rails, the main column and the
    // overview all start under it. Same trick the notice bar uses.
    var root = document.documentElement;
    if (root && root.setAttribute) root.setAttribute("data-hc-viewtabs", "");

    // The filter chips belong under the tabs, not in the goals column: the
    // tabs say which view, the counts say how much of it. The artifact's
    // own element is moved rather than rebuilt, so its click handlers --
    // which own the filter state -- come with it.
    var bar = document.querySelector(".hc-pillbar");
    if (!bar) {
      bar = el("div", "hc-pillbar");
      host.appendChild(bar);
    }
    var chips = document.querySelector(".hc-chiprow");
    if (chips && chips.parentNode !== bar) bar.appendChild(chips);
    // Counts describe the tree, and the overview is not the tree.
    if (overviewShown()) bar.setAttribute("data-hc-hidden", "");
    else bar.removeAttribute("data-hc-hidden");
    var on = overviewShown() ? "overview" : "goals";
    var kids = tabs.children || [];
    var moved = false;
    for (var i = 0; i < kids.length; i += 1) {
      var want = kids[i].getAttribute("data-hc-viewtab") === on;
      var has = kids[i].getAttribute("data-hc-on") !== null;
      if (want === has) continue;
      if (want) kids[i].setAttribute("data-hc-on", "");
      else kids[i].removeAttribute("data-hc-on");
      moved = true;
    }
    return moved;
  }

  // --- the project's context, on every goal --------------------------------
  //
  // What the overview attaches is the project's, and every goal is inside
  // the project: the repository and each source on it are context the goal
  // was written against whether or not anybody attached them here again.
  // The rail under the title drew only the goal's own, so a workspace with
  // a repository and three documents in its overview showed "+ Add source"
  // and nothing else. These are inherited, so they carry no remove -- the
  // overview is where they came on and the only place they come off.
  // Clicking one opens it there.
  function inheritedSources() {
    var who = projectInfo();
    if (!who) return [];
    var mine = [{ id: "", type: "repo", label: str(who.name) }];
    return mine.concat(sourceRows(who).map(function (row) {
      return { id: str(row.id), type: str(row.type), label: str(row.label) };
    }));
  }

  function renderInheritedSources() {
    var rail = document.querySelector(".hc-sources");
    if (!rail) return false;
    var rows = inheritedSources();
    var keys = rows.map(function (row) {
      return str(row.type) + " " + str(row.label);
    });
    // The artifact owns this rail and re-renders it whenever the selected
    // goal changes; the sweep redraws what that took away. Comparing what
    // is on screen against what belongs there keeps that from touching the
    // DOM twice a second for no reason.
    var had = kids(rail).filter(function (node) {
      return node.getAttribute && node.getAttribute("data-hc-ctxsrc") !== null;
    });
    var same = had.length === keys.length;
    had.forEach(function (node, i) {
      if (node.getAttribute("data-hc-ctxsrc") !== keys[i]) same = false;
    });
    if (same) return false;
    had.forEach(function (node) {
      if (node.parentNode) node.parentNode.removeChild(node);
    });
    // After the label and before the goal's own: this is the context all of
    // them sit in, so it reads first.
    var label = rail.querySelector(".hc-sources-label");
    var at = label ? label.nextSibling : rail.firstChild;
    rows.forEach(function (row, i) {
      var chip = el("span", "hc-src hc-src-ctx");
      chip.setAttribute("data-hc-ctxsrc", keys[i]);
      chip.setAttribute("data-hc-ctxid", str(row.id));
      chip.setAttribute("role", "button");
      chip.setAttribute("title", str(row.label) + " — from the overview");
      chip.appendChild(el("span", "hc-src-tag",
                          SOURCE_TAGS[str(row.type)] || SOURCE_TAGS.doc));
      chip.appendChild(el("span", "hc-src-label", sourceName(row)));
      rail.insertBefore(chip, at);
    });
    return true;
  }

  function renderAnalyzer() {
    // In the corner of the goals column, under the tree it is filling: the
    // header is for what this workspace IS, and this is what it is doing.
    // The search box is the one stable handle on that column.
    var slot = document.querySelector(".hc-analyzing");
    if (!slot) {
      var box = document.querySelector(".hc-search");
      var rail = box && box.parentNode;
      if (!rail || !rail.appendChild) return false;
      slot = el("span", "hc-analyzing");
      rail.appendChild(slot);
      if (rail.setAttribute) rail.setAttribute("data-hc-hasnote", "");
    }
    var words = analyzerLine(serverState);
    var a = serverState && serverState.analyzer;
    var mood = !a ? "none"
      : a.status === "error" ? "bad"
      : a.status === "running" ? "working"
      : (Number(a.requested_ordinal || 0) > Number(a.last_analyzed_ordinal || 0))
        ? "waiting" : "done";
    if (slot.textContent === words && slot.getAttribute("data-hc-mood") === mood) {
      return false;
    }
    slot.textContent = words;
    slot.setAttribute("data-hc-mood", mood);
    return true;
  }

  // The session id was in the header and told a reader nothing they could
  // act on -- eight hex characters naming a conversation they are already
  // in. The slot stays (the header lays out around it); it is left empty.
  function renderSessionChip() {
    var slot = document.querySelector(".hc-session");
    if (!slot) return false;
    if (slot.textContent === "") return false;
    slot.textContent = "";
    return true;
  }

  // What Claude has been sent, and whether it is still being sent it. Every
  // line here is a fact /api/state.injection reports; none of it is a
  // control, because none of it has one -- turning it off is a slash command
  // in the terminal, which is what the last line says.
  //
  // "sent", never "read": the snapshot behind these numbers records what the
  // hook *rendered* into the turn (see save_context_snapshot). Claude Code
  // may still drop or compact that injection, so the page cannot claim the
  // model read it -- only that this side handed it over.
  var injectionShown = "";

  function injectionLines(state) {
    var rows = [];
    if (!state || typeof state !== "object") return rows;
    rows.push(["head", "context injection"]);
    rows.push([state.cached ? "on" : "off",
               state.cached ? "goal document sent ✓"
                            : "not sent to Claude yet"]);
    if (typeof state.last_delta_chars === "number") {
      rows.push(["", state.last_delta_chars
        ? "~" + Math.ceil(state.last_delta_chars / 4)
          + " tok changed since it was last sent"
        : "unchanged since it was last sent"]);
    }
    var at = str(state.last_at);
    if (at) {
      var when = new Date(Date.parse(at));
      if (!isNaN(when.getTime())) {
        rows.push(["", "last sent " + when.toLocaleTimeString("en-US",
          { hour: "2-digit", minute: "2-digit", hour12: false })]);
      }
    }
    var reads = array(state.reads).map(str).filter(Boolean);
    if (reads.length) rows.push(["", "reads: " + reads.join(" · ")]);
    rows.push([state.active ? "on" : "off",
               state.active ? "on · /goals-ui disable turns it off"
                            : "off · /goals-ui turns it back on"]);
    return rows;
  }

  function renderInjection(state) {
    var host = document.querySelector(".hc-inject");
    if (!host) return false;
    var rows = injectionLines(state);
    var stamp = JSON.stringify(rows);
    if (stamp === injectionShown && host.children && host.children.length) {
      return true;
    }
    injectionShown = stamp;
    while (host.firstChild) host.removeChild(host.firstChild);
    rows.forEach(function (row) {
      var line = document.createElement("div");
      if (row[0]) line.className = "hc-inject-" + row[0];
      line.textContent = row[1];
      host.appendChild(line);
    });
    return true;
  }

  var injectionState = null;

  // --- the two rails: how wide, and whether shown ---------------------------
  // The reader's own layout, kept per origin like the theme is. Widths are
  // CSS variables on the root and hidden-ness is a root attribute, so the
  // stylesheet above does the drawing and a re-render of the artifact's
  // tree cannot lose either. Nothing here touches the artifact's nodes.
  // v2: the rails open at a quarter of the window each (a 1:2:1 split), so
  // a layout saved by the shell that shipped narrower ones is not reused.
  var LAYOUT_KEY = "hc-launch-layout-v2";
  var RAIL_MIN = { left: 200, right: 240 };
  var RAIL_MAX = { left: 720, right: 720 };
  var layout = null;

  function railDefault(side) {
    var width = (typeof window !== "undefined" && window.innerWidth) || 1440;
    var quarter = Math.round(width / 4);
    return Math.min(RAIL_MAX[side], Math.max(RAIL_MIN[side], quarter));
  }
  var RAIL_DEFAULT = { left: railDefault("left"), right: railDefault("right") };

  function clampWidth(side, px) {
    var n = Number(px);
    if (!isFinite(n)) return RAIL_DEFAULT[side];
    return Math.round(Math.min(RAIL_MAX[side], Math.max(RAIL_MIN[side], n)));
  }

  function loadLayout() {
    if (layout) return layout;
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}") || {}; }
    catch (e) { saved = {}; }
    layout = {
      left: clampWidth("left", saved.left != null ? saved.left : RAIL_DEFAULT.left),
      right: clampWidth("right", saved.right != null ? saved.right : RAIL_DEFAULT.right),
      hideLeft: saved.hideLeft === true,
      hideRight: saved.hideRight === true,
    };
    return layout;
  }

  function saveLayout() {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(loadLayout())); }
    catch (e) { /* private mode: the layout lasts for the page */ }
  }

  function applyLayout() {
    var root = document.documentElement;
    if (!root || !root.style || typeof root.style.setProperty !== "function"
        || !root.setAttribute) return false;
    var l = loadLayout();
    root.style.setProperty("--hc-left", l.left + "px");
    root.style.setProperty("--hc-right", l.right + "px");
    if (l.hideLeft) root.setAttribute("data-hc-hide-left", "");
    else root.removeAttribute("data-hc-hide-left");
    if (l.hideRight) root.setAttribute("data-hc-hide-right", "");
    else root.removeAttribute("data-hc-hide-right");
    return true;
  }

  function setRailWidth(side, px) {
    var l = loadLayout();
    l[side] = clampWidth(side, px);
    saveLayout();
    applyLayout();
    return l[side];
  }

  function setRailHidden(side, hidden) {
    var l = loadLayout();
    l[side === "left" ? "hideLeft" : "hideRight"] = !!hidden;
    saveLayout();
    applyLayout();
    renderPanelToggles();
    return !!hidden;
  }

  function toggleRail(side) {
    var l = loadLayout();
    return setRailHidden(side, !(side === "left" ? l.hideLeft : l.hideRight));
  }

  var PANEL_ICON = "<svg width=\"14\" height=\"14\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3\" y=\"4\" width=\"18\" height=\"16\" rx=\"2\"></rect><path d=\"M{X} 4v16\"></path></svg>";

  // Two toggles in the header, one per rail. Built into the slot the
  // header patch leaves; only their on/off class changes after that. The
  // click is handled at the document, not on the node: the artifact
  // re-materializes its subtree on render, which drops listeners but keeps
  // attributes -- the same reason the drag below is document-level.
  function renderPanelToggles() {
    var slot = document.querySelector(".hc-panels");
    if (!slot) return false;
    var l = loadLayout();
    if (!slot.children || !slot.children.length) {
      [["left", "9", "goals rail"], ["right", "15", "prompt rail"]].forEach(function (spec) {
        var btn = document.createElement("span");
        btn.className = "hc-panel";
        btn.setAttribute("data-hc-panel", spec[0]);
        btn.title = "Show or hide the " + spec[2];
        btn.innerHTML = PANEL_ICON.replace("{X}", spec[1]);
        slot.appendChild(btn);
      });
    }
    var kids = slot.children || [];
    for (var i = 0; i < kids.length; i++) {
      var side = kids[i].getAttribute("data-hc-panel");
      var on = side === "left" ? !l.hideLeft : !l.hideRight;
      kids[i].className = "hc-panel" + (on ? " hc-panel-on" : "");
    }
    return true;
  }

  // The status pills sit in the header just after the brand. The row they
  // live in is fixed-positioned by the stylesheet; this measures where the
  // brand ends so the offset follows the font rather than a guess.
  function placePills() {
    var root = document.documentElement;
    // After the project chip when there is one: the pills follow the last
    // thing the header names before them.
    var chip = document.querySelector(".hc-project");
    var brand = (chip && chip.children && chip.children.length) ? chip
              : document.querySelector(".hc-brand");
    if (!root || !root.style || typeof root.style.setProperty !== "function"
        || !brand || !brand.getBoundingClientRect) return false;
    var box = brand.getBoundingClientRect();
    if (!box || !box.width) return false;
    var left = Math.round(box.right + 18);
    var want = left + "px";
    if (root.style.getPropertyValue("--hc-pills-left") === want) return false;
    root.style.setProperty("--hc-pills-left", want);
    return true;
  }

  // Which divider, if any, the pointer is on: within 4px of the goals
  // rail's right edge or the prompt rail's left edge. The handles are the
  // rails' own pseudo-elements, so the event target is the rail itself.
  function dividerAt(x, y) {
    var pairs = [[".hc-rail-left", "left"], [".hc-rail-right", "right"]];
    for (var i = 0; i < pairs.length; i++) {
      var el = document.querySelector(pairs[i][0]);
      if (!el || !el.getBoundingClientRect) continue;
      var r = el.getBoundingClientRect();
      if (!r.width || y < r.top || y > r.bottom) continue;
      var edge = pairs[i][1] === "left" ? r.right : r.left;
      if (Math.abs(x - edge) <= 4) return { side: pairs[i][1], rect: r };
    }
    return null;
  }

  var dragInstalled = false;

  function installRailDrag() {
    if (dragInstalled || !document.addEventListener) return false;
    dragInstalled = true;
    var drag = null;
    document.addEventListener("click", function (e) {
      var node = e.target;
      while (node && node !== document && !(node.getAttribute && node.getAttribute("data-hc-panel"))) {
        node = node.parentNode;
      }
      if (!node || node === document) return;
      e.preventDefault();
      toggleRail(node.getAttribute("data-hc-panel"));
    });
    document.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      var hit = dividerAt(e.clientX, e.clientY);
      if (!hit) return;
      drag = { side: hit.side, rect: hit.rect, moved: false };
      document.documentElement.setAttribute("data-hc-dragging", "");
      e.preventDefault();
    });
    document.addEventListener("mousemove", function (e) {
      if (!drag) return;
      drag.moved = true;
      var px = drag.side === "left" ? e.clientX - drag.rect.left
                                    : drag.rect.right - e.clientX;
      setRailWidth(drag.side, px);
    });
    document.addEventListener("mouseup", function () {
      if (!drag) return;
      drag = null;
      document.documentElement.removeAttribute("data-hc-dragging");
    });
    // Rename-on-double-click is bound inside the artifact's own row, so a
    // row that may not be edited has to stop the event before it gets
    // there. Whole-workspace when the reader may write nothing; per row
    // when they may write some of it.
    document.addEventListener("dblclick", function (e) {
      var whole = document.documentElement
        && document.documentElement.getAttribute("data-hc-readonly") !== null;
      if (whole || !editableRow(e.target)) {
        if (!whole) sayNotYours(e.target);
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
    document.addEventListener("dblclick", function (e) {
      var hit = dividerAt(e.clientX, e.clientY);
      if (!hit) return;
      e.preventDefault();
      setRailHidden(hit.side, true);
    });
    return true;
  }

  // --- finding a goal: the search bar under GOALS ---------------------------
  // The tree is where goals live, and the reader forgets which branch a
  // thing went under. The box directly under GOALS takes a few words and
  // ranks every goal by them -- its title first, then its notes, its TODO
  // rows and its prompt -- forgiving a slip or two of spelling. Hits stand
  // in for the tree while there is a query; picking one opens its branch,
  // selects it, and clears the box.

  var SEARCH_FIELDS = ["title", "notes", "todos", "prompt"];
  var SEARCH_WEIGHT = { title: 3, notes: 1, todos: 1, prompt: 1 };
  var SEARCH_LABEL = { title: "", notes: "notes:", todos: "TODO:",
                       prompt: "prompt:" };

  function searchWords(text) {
    return str(text).toLowerCase().split(/[^0-9a-zÀ-ɏ_#]+/)
      .filter(Boolean);
  }

  // Optimal string alignment distance: Levenshtein plus a swapped pair,
  // abandoned the moment no row can come in under the cap.
  function editDistance(a, b, cap) {
    a = str(a); b = str(b);
    cap = cap >= 0 ? cap : Math.max(a.length, b.length);
    if (Math.abs(a.length - b.length) > cap) return cap + 1;
    var prev2 = null, prev = [], cur, i, j;
    for (j = 0; j <= b.length; j += 1) prev[j] = j;
    for (i = 1; i <= a.length; i += 1) {
      cur = [i];
      var best = i;
      for (j = 1; j <= b.length; j += 1) {
        var cost = a.charAt(i - 1) === b.charAt(j - 1) ? 0 : 1;
        var v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
        if (prev2 && i > 1 && j > 1 && a.charAt(i - 1) === b.charAt(j - 2)
            && a.charAt(i - 2) === b.charAt(j - 1)) {
          v = Math.min(v, prev2[j - 2] + 1);
        }
        cur[j] = v;
        if (v < best) best = v;
      }
      if (best > cap) return cap + 1;
      prev2 = prev; prev = cur;
    }
    return prev[b.length];
  }

  // How well one typed word matches one word of a field, 0..1. Exact,
  // then a prefix (the reader is still typing), then inside the word; then
  // a slip or two of spelling, scaled to how much was typed -- one from
  // four letters, two from seven, none under four ("the" is one edit from
  // most of English). A slip is measured against the whole word and
  // against its prefix, so a typo in a half-typed word still finds it.
  function wordScore(token, word) {
    token = str(token); word = str(word);
    if (!token || !word) return 0;
    if (word === token) return 1;
    var at = word.indexOf(token);
    if (at === 0) return 0.9;
    if (at > 0) return 0.7;
    var slips = token.length >= 7 ? 2 : token.length >= 4 ? 1 : 0;
    if (!slips) return 0;
    var d = Math.min(editDistance(token, word, slips),
                     editDistance(token, word.slice(0, token.length), slips));
    if (d > slips) return 0;
    return 0.6 * (1 - d / token.length);
  }

  // Every field the reader may remember a goal by. TODOs are the rows;
  // the prompt is the one written for the goal and the ones linked to it.
  function searchFields(goal, promptsById) {
    var todos = array(goal.todo_items).map(function (row) {
      return str(row && row.text);
    }).filter(Boolean);
    var said = array(goal.prompt_ids).map(function (id) {
      var p = promptsById[id];
      return p ? str(p.text) : "";
    }).filter(Boolean);
    return {
      title: str(goal.title),
      notes: [str(goal.notes), str(goal.description)].filter(Boolean).join("\n"),
      todos: todos.length ? todos.join("\n") : str(goal.todos_md),
      prompt: [str(goal.prompt_md)].concat(said).filter(Boolean).join("\n")
    };
  }

  // The line of a field the matched word is on, cut to fit a rail, with
  // the word kept inside what is shown.
  function searchExcerpt(text, word) {
    var lines = str(text).split("\n"), line = "";
    for (var i = 0; i < lines.length; i += 1) {
      if (lines[i].toLowerCase().indexOf(word) >= 0) { line = lines[i]; break; }
    }
    line = line.replace(/^\s*(?:[-*+]|\d+\.)?\s*(?:\[[^\]]*\]\s*)?#*\s*/, "")
      .replace(/\s+/g, " ").trim();
    if (!line) return "";
    var at = line.toLowerCase().indexOf(word);
    if (at > 28) line = "…" + line.slice(at - 20);
    if (line.length > 90) line = line.slice(0, 88) + "…";
    return line;
  }

  // Every goal in the tree against the query, best first. A goal scores
  // only if every word of the query lands somewhere in it; its score is the
  // sum of each word's best field. Equal scores fall to whichever was
  // edited last, then to tree order.
  function searchGoals(goals, prompts, query) {
    var tokens = searchWords(query);
    if (!tokens.length) return [];
    var byId = Object.create(null), byParent = Object.create(null);
    array(prompts).forEach(function (p) {
      if (p && typeof p.id === "string") byId[p.id] = p;
    });
    array(goals).forEach(function (g) {
      if (!g || typeof g.id !== "string" || g.status === "abandoned") return;
      var parent = g.parent_goal_id || null;
      (byParent[parent] = byParent[parent] || []).push(g);
    });
    var out = [], order = 0;
    (function walk(list, trail) {
      array(list).forEach(function (g) {
        var fields = searchFields(g, byId), words = {};
        SEARCH_FIELDS.forEach(function (f) { words[f] = searchWords(fields[f]); });
        var total = 0, best = null, every = true;
        tokens.forEach(function (token) {
          var top = 0, topField = null, topWord = "";
          SEARCH_FIELDS.forEach(function (f) {
            words[f].forEach(function (w) {
              var s = wordScore(token, w) * SEARCH_WEIGHT[f];
              if (s > top) { top = s; topField = f; topWord = w; }
            });
          });
          if (!top) { every = false; return; }
          total += top;
          if (!best || top > best.score) {
            best = { score: top, field: topField, word: topWord };
          }
        });
        if (every) {
          out.push({
            id: g.id, title: str(g.title) || "Untitled",
            trail: trail.map(function (t) { return str(t.title) || "Untitled"; }),
            score: Math.round(total * 1000) / 1000,
            updated: Date.parse(str(g.updated_at)) || 0,
            where: best.field,
            excerpt: best.field === "title"
              ? "" : searchExcerpt(fields[best.field], best.word),
            order: order
          });
          order += 1;
        }
        walk(byParent[g.id], trail.concat([g]));
      });
    })(byParent[null], []);
    out.sort(function (a, b) {
      return (b.score - a.score) || (b.updated - a.updated) || (a.order - b.order);
    });
    return out.map(function (hit) { delete hit.order; return hit; });
  }

  var searchDrawn = null, searchActive = 0, searchBound = false;

  // --- filtering by how a goal stands to the objective ---------------------
  //
  // Inference tags every goal core, supporting or unrelated against the
  // project's stated objective. The tags were only ever readable one goal
  // at a time; this is the other half -- a way to see the tree through one
  // of them. It sits under the search field because it answers the same
  // question the search does: show me less than everything.

  var relevanceFilter = "";
  var RELEVANCE_ORDER = ["core", "supporting", "unrelated"];
  var RELEVANCE_WORDS = { core: "On objective", supporting: "Supporting",
                          unrelated: "Off objective" };

  function relevanceOf(goal) {
    var tag = str(goal && goal.relevance);
    return RELEVANCE_ORDER.indexOf(tag) >= 0 ? tag : "core";
  }

  function relevanceCounts() {
    var out = { core: 0, supporting: 0, unrelated: 0, all: 0 };
    array(serverState.goals).forEach(function (g) {
      if (!g || typeof g.id !== "string") return;
      if (str(g.status) === "abandoned") return;
      out[relevanceOf(g)] += 1;
      out.all += 1;
    });
    return out;
  }

  function setRelevanceFilter(tag) {
    var want = RELEVANCE_ORDER.indexOf(str(tag)) >= 0 ? str(tag) : "";
    if (want === relevanceFilter) want = "";
    relevanceFilter = want;
    renderRelevance();
    applyRelevanceFilter();
    return relevanceFilter;
  }

  // A goal survives the filter if it carries the tag, and so does every
  // goal above it: hiding a parent whose child matched would put the child
  // at the root of a tree it does not belong to.
  function relevanceKeep() {
    if (!relevanceFilter) return null;
    var parents = {}, keep = {};
    var rows = array(serverState.goals).filter(function (g) {
      return g && typeof g.id === "string";
    });
    rows.forEach(function (g) { parents[g.id] = str(g.parent_goal_id); });
    rows.forEach(function (g) {
      // Counted out above, so kept out here: a tombstone that answered a
      // filter would make the chip's number disagree with the tree.
      if (str(g.status) === "abandoned") return;
      if (relevanceOf(g) !== relevanceFilter) return;
      for (var at = g.id, hops = 0; at && hops < 64; hops += 1) {
        keep[at] = true;
        at = parents[at];
      }
    });
    return keep;
  }

  function applyRelevanceFilter() {
    var tree = document.querySelector(".hc-tree") || document;
    if (!tree || !tree.querySelectorAll) return false;
    var keep = relevanceKeep();
    var rows = tree.querySelectorAll(".hc-row");
    var moved = false;
    for (var i = 0; i < rows.length; i += 1) {
      var id = rows[i].getAttribute && rows[i].getAttribute("data-hc-goal");
      var off = !!(keep && id && !keep[id]);
      var had = rows[i].getAttribute("data-hc-rel-off") !== null;
      if (off === had) continue;
      if (off) rows[i].setAttribute("data-hc-rel-off", "");
      else rows[i].removeAttribute("data-hc-rel-off");
      moved = true;
    }
    return moved;
  }

  function renderRelevance() {
    if (serverState.scope !== "chat") return false;
    var box = searchBox();
    if (!box) return false;
    // The chips are handled beside the search's own controls, so the bar
    // must not depend on the search having drawn to be clickable.
    bindSearch();
    var bar = box.querySelector(".hc-relbar");
    if (!bar) {
      bar = el("div", "hc-relbar");
      bar.setAttribute("data-hc-relbar", "");
      var field = box.querySelector(".hc-search-field");
      if (field && field.nextSibling && box.insertBefore) {
        box.insertBefore(bar, field.nextSibling);
      } else {
        box.appendChild(bar);
      }
    }
    var counts = relevanceCounts();
    // Nothing to choose between when the whole tree is one tag -- which is
    // every project until inference has read enough to disagree.
    var kinds = RELEVANCE_ORDER.filter(function (tag) { return counts[tag] > 0; });
    var want = JSON.stringify([relevanceFilter, counts, kinds]);
    if (bar.getAttribute("data-hc-drawn") === want) return false;
    bar.setAttribute("data-hc-drawn", want);
    wipe(bar);
    if (kinds.length < 2) {
      bar.removeAttribute("data-hc-on");
      return true;
    }
    bar.setAttribute("data-hc-on", "");
    var chip = function (tag, words, n, on) {
      var node = el("span", "hc-relchip", "");
      node.setAttribute("role", "button");
      node.setAttribute("data-hc-rel", tag);
      if (on) node.setAttribute("data-hc-on", "");
      node.appendChild(el("span", "", words));
      node.appendChild(el("span", "hc-relchip-n", String(n)));
      bar.appendChild(node);
    };
    chip("", "All", counts.all, !relevanceFilter);
    kinds.forEach(function (tag) {
      chip(tag, RELEVANCE_WORDS[tag], counts[tag], relevanceFilter === tag);
    });
    return true;
  }

  function searchBox() { return document.querySelector(".hc-search"); }
  function searchInputEl() {
    var box = searchBox();
    return box ? box.querySelector(".hc-search-input") : null;
  }
  function searchQuery() {
    var input = searchInputEl();
    return input ? str(input.value).trim() : "";
  }
  function searchHitNodes() {
    var box = searchBox();
    var list = box && box.querySelector(".hc-search-hits");
    // children is a live collection in a browser and an array in the
    // harness; slice reads both.
    return list ? Array.prototype.slice.call(list.children || []).filter(
      function (n) { return n && n.className === "hc-search-hit"; }) : [];
  }
  function markSearchActive(hits) {
    hits.forEach(function (n, i) {
      if (i === searchActive) n.setAttribute("data-hc-hit-active", "");
      else n.removeAttribute("data-hc-hit-active");
    });
  }

  function clearSearch(keepFocus) {
    var input = searchInputEl();
    if (input) input.value = "";
    searchActive = 0;
    renderSearch();
    if (input && !keepFocus && typeof input.blur === "function") input.blur();
  }

  // Picking a hit: open the branch it is on and select it (the artifact's
  // own reveal, patched in beside its select), then put the tree back.
  function searchPick(id) {
    if (!id) return false;
    var went = false;
    if (typeof window.__hcRevealGoal === "function") {
      try { went = !!window.__hcRevealGoal(id); } catch (e) { went = false; }
    }
    if (!went && typeof window.__hcSelectGoal === "function") {
      try { window.__hcSelectGoal(id); went = true; } catch (e) { went = false; }
    }
    clearSearch();
    return went;
  }

  function bindSearch() {
    if (searchBound || !document.addEventListener) return;
    searchBound = true;
    var stop = function (event) {
      if (event.preventDefault) event.preventDefault();
      if (event.stopPropagation) event.stopPropagation();
    };
    document.addEventListener("input", function (event) {
      if (!closestByClass(event && event.target, "hc-search-input")) return;
      searchActive = 0;
      renderSearch();
    }, true);
    // Up and down walk the hits, Enter takes the one that is lit, Escape
    // puts the tree back. Captured, so the artifact's own tree keys (which
    // already stand aside for typing) never see them.
    document.addEventListener("keydown", function (event) {
      if (!closestByClass(event && event.target, "hc-search-input")) return;
      var key = str(event.key), hits = searchHitNodes();
      if (key === "Escape") { stop(event); clearSearch(); return; }
      if (key === "ArrowDown" || key === "ArrowUp") {
        if (!hits.length) return;
        stop(event);
        searchActive = (searchActive + (key === "ArrowDown" ? 1 : hits.length - 1))
          % hits.length;
        markSearchActive(hits);
        return;
      }
      if (key === "Enter") {
        if (!hits.length) return;
        stop(event);
        var lit = hits[searchActive] || hits[0];
        searchPick(str(lit.getAttribute("data-hc-goal")));
      }
    }, true);
    document.addEventListener("click", function (event) {
      var target = event && event.target;
      var chip = closestByClass(target, "hc-relchip");
      if (chip) {
        stop(event);
        setRelevanceFilter(chip.getAttribute("data-hc-rel"));
        return;
      }
      if (closestByClass(target, "hc-search-clear")) {
        stop(event);
        clearSearch(true);
        var input = searchInputEl();
        if (input && input.focus) input.focus();
        return;
      }
      var hit = closestByClass(target, "hc-search-hit");
      if (!hit) return;
      stop(event);
      searchPick(str(hit.getAttribute("data-hc-goal")));
    }, true);
  }

  function renderSearch() {
    if (serverState.scope !== "chat") return false;
    var box = searchBox();
    if (!box) return false;
    var input = box.querySelector(".hc-search-input");
    var list = box.querySelector(".hc-search-hits");
    if (!input || !list) return false;
    bindSearch();
    var rail = box.parentNode;
    var q = str(input.value).trim();
    var field = box.querySelector(".hc-search-field");
    if (field && field.setAttribute) {
      if (str(input.value)) field.setAttribute("data-hc-typed", "");
      else field.removeAttribute("data-hc-typed");
    }
    var searching = !!(rail && rail.getAttribute
                       && rail.getAttribute("data-hc-searching") !== null);
    if (!q) {
      if (searching) rail.removeAttribute("data-hc-searching");
      if (searchDrawn === "") return false;
      searchDrawn = "";
      while (list.firstChild) list.removeChild(list.firstChild);
      return true;
    }
    if (rail && rail.setAttribute && !searching) {
      rail.setAttribute("data-hc-searching", "");
    }
    var ranked = searchGoals(serverState.goals, serverState.prompts, q);
    var key = JSON.stringify([q, ranked]);
    if (key === searchDrawn) return false;
    searchDrawn = key;
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!ranked.length) {
      var none = document.createElement("div");
      none.className = "hc-search-none";
      none.textContent = "Nothing matches “" + q + "”.";
      list.appendChild(none);
      return true;
    }
    if (searchActive >= ranked.length) searchActive = 0;
    ranked.forEach(function (hit, i) {
      var row = document.createElement("div");
      row.className = "hc-search-hit";
      row.setAttribute("data-hc-goal", hit.id);
      row.setAttribute("data-hc-where", hit.where);
      if (i === searchActive) row.setAttribute("data-hc-hit-active", "");
      if (hit.trail.length) {
        var trail = document.createElement("div");
        trail.className = "hc-search-hit-trail";
        trail.textContent = hit.trail.join(" › ");
        row.appendChild(trail);
      }
      var title = document.createElement("div");
      title.className = "hc-search-hit-title";
      title.textContent = hit.title;
      row.appendChild(title);
      if (hit.excerpt) {
        var where = document.createElement("div");
        where.className = "hc-search-hit-where";
        var tag = document.createElement("b");
        tag.textContent = SEARCH_LABEL[hit.where] + " ";
        where.appendChild(tag);
        var text = document.createElement("span");
        text.textContent = hit.excerpt;
        where.appendChild(text);
        row.appendChild(where);
      }
      list.appendChild(row);
    });
    return true;
  }

  // The theme is the artifact's own data-dark, and everything the launch
  // skin dresses reads it from data-hc-theme on the root -- which the sweep
  // copied across every 700ms. So the header flipped at once and the rest
  // of the page a beat later. Watch the attribute instead of polling it.
  var themeWatched = false;

  function watchTheme() {
    if (themeWatched || typeof MutationObserver !== "function") return false;
    var root = document.documentElement;
    if (!root) return false;
    themeWatched = true;
    // Watched from the root, not from .hc itself: this runtime re-renders
    // nodes away rather than mutating them, so an observer bound to the
    // element that happens to carry data-dark today is watching a detached
    // node tomorrow -- and the copies fall back to the 700ms sweep, which
    // is the beat this was meant to remove.
    new MutationObserver(themeMoved).observe(root, {
      attributes: true, subtree: true, attributeFilter: ["data-dark"] });
    return true;
  }

  function themeMoved() {
    repaintCopies();
    // And again once the frame the change lands in has been laid out: the
    // palette is a stylesheet rule keyed on the attribute, and a reader
    // whose browser has not recalculated yet would copy the old colours.
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(repaintCopies);
    }
    return true;
  }

  function repaintCopies() {
    mirrorRootState();
    // The overview, the switcher and the settings panel are parented on
    // <body>, outside .hc, and carry copies of the palette rather than
    // inheriting it. Copies go stale the moment the original moves.
    [overviewBox, projectMenuBox, settingsPanelBox,
     askSelBox, askSelPill].forEach(function (node) {
      if (node && inLiveDocument(node)) syncProjectTheme(node);
    });
    return true;
  }

  function watchLaunchSurface() {
    function sweep() {
      watchTheme();
      if (serverState.scope !== "chat") return;
      applyLaunchSkin();
      mirrorRootState();
      applyLayout();
      placePills();
      renderPanelToggles();
      installRailDrag();
      renderSessionChip();
      renderViewTabs();
      renderInheritedSources();
      renderAnalyzer();
      renderProjectChip();
      renderOverview();
      renderHandoff();
      renderBell();
      renderGear();
      renderSearch();
      renderRelevance();
      applyRelevanceFilter();
      renderInjection(injectionState);
    }
    sweep();
    setInterval(sweep, 700);
  }

  // Asking which kind of source, and then for the value. The three kinds are
  // the three the store keeps (github, local, doc); the value goes back
  // through the artifact's own ctx lists, so it lands on set_sources by the
  // path every other source edit already takes.
  function askSource() {
    return new Promise(function (resolve) {
      ensureDialogStyles();
      var overlay = document.createElement("div");
      overlay.className = "hc-ask";
      var box = document.createElement("div");
      box.className = "hc-ask-box";
      var title = document.createElement("div");
      title.className = "hc-ask-title";
      title.textContent = "Attach a source to this goal";
      var kinds = document.createElement("div");
      kinds.className = "hc-ask-kinds";
      var input = document.createElement("input");
      input.type = "text";
      input.className = "hc-ask-input";
      var chosen = "github";
      var buttons = [];

      function pick(kind) {
        chosen = kind;
        input.placeholder = ASK[kind].placeholder;
        buttons.forEach(function (b) {
          b.className = "hc-ask-btn"
            + (b.getAttribute("data-kind") === kind ? " hc-ask-ok" : "");
        });
      }

      [["github", "GitHub repo"], ["local", "Local folder"],
       ["doc", "Document"]].forEach(function (row) {
        var button = document.createElement("button");
        button.type = "button";
        button.setAttribute("data-kind", row[0]);
        button.textContent = row[1];
        button.onclick = function () { pick(row[0]); input.focus(); };
        buttons.push(button);
        kinds.appendChild(button);
      });

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
      function take() {
        var label = str(input.value).trim();
        close(label ? { type: chosen, label: label } : null);
      }
      cancel.onclick = function () { close(null); };
      confirm.onclick = take;
      input.onkeydown = function (e) {
        if (e.key === "Enter") { e.preventDefault(); take(); }
        if (e.key === "Escape") { e.preventDefault(); close(null); }
      };
      overlay.onclick = function (e) { if (e.target === overlay) close(null); };
      row.appendChild(cancel);
      row.appendChild(confirm);
      box.appendChild(title);
      box.appendChild(kinds);
      box.appendChild(input);
      box.appendChild(row);
      overlay.appendChild(box);
      (document.querySelector(".hc") || document.body).appendChild(overlay);
      pick("github");
      setTimeout(function () { input.focus(); }, 0);
    });
  }

  // --- asking Claude about what is highlighted -----------------------------
  //
  // Highlight anything in the workspace -- a goal's title in the tree, a TODO
  // row in the rail, a line of somebody's notes -- and a small "Ask Claude"
  // appears beside it. The question is answered from that passage and the
  // goal it sits in, and the answer is held only while the panel is open:
  // this is a way to think beside the tree, not a way to write into it.

  var ASKSEL_MAX = 4000;
  var ASKSEL_MIN = 2;

  var ASKSEL_CSS = [
      ".hc-sel-pill{position:fixed;z-index:100002;display:flex;align-items:center;gap:5px;border:1px solid var(--bd2,#d5d5d5);background:var(--panel,#fff);color:var(--ink,#111);border-radius:3px;box-shadow:0 6px 18px rgba(0,0,0,.18);padding:4px 9px;cursor:pointer;font:11px 'Source Code Pro',monospace;user-select:none;white-space:nowrap}",
      ".hc-sel-pill:hover{border-color:var(--acc,#c15f3c)}",
      ".hc-sel-spark{color:var(--acc,#c15f3c)}",
      ".hc-sel-box{position:fixed;z-index:100002;width:min(380px,calc(100vw - 16px));display:flex;flex-direction:column;background:var(--panel,#fff);color:var(--ink,#111);border:1px solid var(--bd2,#d5d5d5);border-radius:3px;box-shadow:0 18px 60px rgba(0,0,0,.22);font-family:'Source Code Pro',monospace}",
      ".hc-sel-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 8px 6px 11px;border-bottom:1px solid var(--bd,#e6e6e6)}",
      ".hc-sel-title{font:600 10px 'Source Code Pro',monospace;letter-spacing:.6px;text-transform:uppercase;color:var(--fnt,#9b9b9b)}",
      ".hc-sel-close{width:20px;height:20px;display:flex;align-items:center;justify-content:center;border:none;background:transparent;color:var(--fnt,#9b9b9b);border-radius:2px;cursor:pointer;font:14px/1 'Source Code Pro',monospace}",
      ".hc-sel-close:hover{background:var(--hov,#f4f4f4);color:var(--ink,#111)}",
      ".hc-sel-quote{margin:9px 11px 0;padding-left:8px;border-left:2px solid var(--acc,#c15f3c);max-height:66px;overflow:hidden;white-space:pre-wrap;word-break:break-word;font:11px/1.55 'Source Code Pro',monospace;color:var(--dtxt,#333)}",
      ".hc-sel-field{margin:9px 11px 0;box-sizing:border-box;width:calc(100% - 22px);resize:vertical;min-height:44px;border:1px solid var(--bd2,#d5d5d5);border-radius:2px;background:var(--panel2,#fafafa);color:var(--ink,#111);outline:none;padding:7px 9px;font:11.5px/1.6 'Source Code Pro',monospace}",
      ".hc-sel-field:focus{border-color:var(--acc,#c15f3c)}",
      ".hc-sel-row{display:flex;align-items:center;gap:8px;padding:8px 11px 10px}",
      ".hc-sel-go{border:1px solid var(--acc,#c15f3c);background:var(--acc,#c15f3c);color:var(--onacc,#fff);border-radius:2px;padding:4px 12px;cursor:pointer;font:11px 'Source Code Pro',monospace}",
      ".hc-sel-say{font:10.5px 'Source Code Pro',monospace;color:var(--fnt,#9b9b9b);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
      ".hc-sel-say[data-hc-bad]{color:var(--del,#b23c3c)}",
      ".hc-sel-out{margin:0 11px 11px;max-height:min(46vh,300px);overflow-y:auto;overscroll-behavior:contain}",
      ".hc-sel-out:empty{display:none}",
      // A conversation, not one answer: every turn keeps its question above
      // the answer it got, so a panel five questions deep still reads.
      ".hc-sel-turn{padding-top:9px}",
      ".hc-sel-turn+.hc-sel-turn{margin-top:9px;border-top:1px solid var(--bd,#e6e6e6)}",
      ".hc-sel-q{font:600 11px/1.5 'Source Code Pro',monospace;color:var(--mut,#666);white-space:pre-wrap;word-break:break-word}",
      ".hc-sel-a{margin-top:5px}",
      // The answer is drawn by the same markdown reader the panes use; only
      // its own scroller is taken away, since this box already has one.
      ".hc-sel-out .hc-md{max-height:none;overflow:visible;font:11.5px/1.65 'Source Code Pro',monospace}",
  ].join("");

  function ensureAskSelStyles() {
    if (document.getElementById("hc-sel-style")) return;
    var style = document.createElement("style");
    style.id = "hc-sel-style";
    style.textContent = ASKSEL_CSS;
    (document.head || document.documentElement).appendChild(style);
  }

  var askSelPill = null;       // the offer, beside a live highlight
  var askSelBox = null;        // the panel, once the offer is taken
  var askSelWhat = null;       // {text, goal, rect} the panel is asking about
  var askSelTurns = [];        // the conversation in it: {question, answer}
  var askSelAsking = "";       // the question waiting on an answer, if any
  var askSelSaid = null;       // {words, bad} under the field, or nothing
  var askSelSeq = 0;           // which ask the panel is showing
  var askSelBound = false;

  // On <body>, not inside .hc: the artifact owns that subtree and redraws
  // it whenever the state moves, and a panel halfway through a conversation
  // is not something to lose to a poll. Everything parented out here carries
  // a copy of the palette rather than inheriting it.
  function askSelHost() {
    return document.body || document.querySelector(".hc")
      || document.documentElement;
  }

  function askSelAdopt(node) {
    askSelHost().appendChild(node);
    syncProjectTheme(node);
    return node;
  }

  // Whether a highlight in *node* is one this offer belongs beside. The
  // workspace, yes; its own panel and the modal dialogs over it, no -- a
  // question asked about the box asking the question is a loop, not a
  // reading aid.
  function askSelReach(node) {
    var host = document.querySelector(".hc");
    var at = node && node.nodeType === 3 ? node.parentNode : node;
    if (!at || !at.getAttribute) return false;
    if (host && !(host === at || host.contains(at))) return false;
    return !closestByClass(at, "hc-sel-box") && !closestByClass(at, "hc-sel-pill")
      && !closestByClass(at, "hc-ask");
  }

  // Which goal a highlight belongs to: the tree row it landed in, or -- in
  // the rail, the notes, the panes, none of which draw a row -- whichever
  // goal is open, since that is the one they are all showing.
  function askSelGoalOf(node) {
    var at = node && node.nodeType === 3 ? node.parentNode : node;
    return (at && rowGoalId(at)) || str(selectedGoalId() || "");
  }

  // What is highlighted right now, or null. A field keeps its own selection
  // -- the notes are a textarea, and a document selection reads nothing
  // inside one -- so it is asked separately, and anchored to its own edge
  // because no browser will hand back a rectangle for a caret in a textarea.
  function askSelLive() {
    var active = document.activeElement;
    if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")
        && typeof active.selectionStart === "number"
        && typeof active.selectionEnd === "number"
        && active.selectionEnd > active.selectionStart) {
      var words = str(active.value)
        .slice(active.selectionStart, active.selectionEnd);
      if (str(words).trim().length < ASKSEL_MIN || !askSelReach(active)) {
        return null;
      }
      var edge = active.getBoundingClientRect();
      return { text: words, goal: askSelGoalOf(active),
               rect: { left: edge.right - 1, right: edge.right,
                       top: edge.top, bottom: edge.top } };
    }
    if (typeof window.getSelection !== "function") return null;
    var picked = window.getSelection();
    if (!picked || picked.isCollapsed || !picked.rangeCount) return null;
    var text = str(picked.toString());
    if (text.trim().length < ASKSEL_MIN) return null;
    if (!askSelReach(picked.focusNode) || !askSelReach(picked.anchorNode)) {
      return null;
    }
    var range = picked.getRangeAt(picked.rangeCount - 1);
    var box = range.getBoundingClientRect && range.getBoundingClientRect();
    if (!box || (!box.width && !box.height && !box.top && !box.left)) return null;
    return { text: text, goal: askSelGoalOf(picked.focusNode), rect: box };
  }

  // Fixed coordinates, kept on screen: below the highlight when there is
  // room under it, above it when there is not.
  function askSelPlace(node, rect, gap) {
    var wide = (window.innerWidth || 1024), tall = (window.innerHeight || 768);
    var size = node.getBoundingClientRect();
    var left = Math.max(8, Math.min(rect.left, wide - size.width - 8));
    var top = rect.bottom + gap;
    if (top + size.height > tall - 8) {
      top = Math.max(8, rect.top - size.height - gap);
    }
    node.style.left = Math.round(left) + "px";
    node.style.top = Math.round(top) + "px";
    return node;
  }

  function hideAskSelPill() {
    if (askSelPill && askSelPill.parentNode) {
      askSelPill.parentNode.removeChild(askSelPill);
    }
    askSelPill = null;
    return true;
  }

  function renderAskSelPill() {
    // While the panel is up it holds the passage it was opened on; a
    // highlight that moves under it -- clicking into the question field
    // collapses one -- must not take the offer away and reopen it.
    if (askSelBox) return false;
    var live = askSelLive();
    if (!live) return hideAskSelPill();
    ensureAskSelStyles();
    // The artifact re-draws the panels it owns, and a pill parked in one
    // goes with them: a node off the document is one nobody can see, so it
    // is dropped rather than positioned where it no longer is.
    if (askSelPill && !inLiveDocument(askSelPill)) askSelPill = null;
    if (!askSelPill) {
      askSelPill = el("div", "hc-sel-pill");
      askSelPill.setAttribute("data-hc-ask-sel", "");
      askSelPill.setAttribute("role", "button");
      askSelPill.title = "ask Claude about the highlighted text";
      askSelPill.appendChild(el("span", "hc-sel-spark", "✦"));
      askSelPill.appendChild(el("span", "", "Ask Claude"));
      askSelAdopt(askSelPill);
    }
    askSelPill.__hcWhat = { text: live.text, goal: live.goal, rect: live.rect };
    askSelPlace(askSelPill, live.rect, 6);
    return true;
  }

  function askSelSay(words, bad) {
    if (!askSelBox) return false;
    var node = askSelBox.querySelector("[data-hc-sel-say]");
    if (!node) return false;
    node.textContent = str(words);
    if (bad) node.setAttribute("data-hc-bad", "");
    else node.removeAttribute("data-hc-bad");
    return true;
  }

  // The panel's own field, when it has one on the page.
  function askSelField() {
    return askSelBox && askSelBox.querySelector("[data-hc-sel-field]");
  }

  function renderAskSelOut() {
    if (!askSelBox) return false;
    // Nothing here re-parents the panel -- it is on <body>, outside what the
    // artifact redraws -- but the box grows a turn at a time, and a panel
    // that started near the bottom of the window has to be put back on
    // screen as it does.
    var out = askSelBox.querySelector("[data-hc-sel-out]");
    if (!out) return false;
    wipe(out);
    askSelTurns.forEach(function (turn) {
      var block = el("div", "hc-sel-turn");
      block.appendChild(el("div", "hc-sel-q", turn.question));
      if (turn.answer) {
        var said = el("div", "hc-sel-a");
        said.appendChild(markdownNode(turn.answer));
        block.appendChild(said);
      }
      out.appendChild(block);
    });
    // The newest turn is the one being read: an answer that arrives above
    // the fold of a panel already full of them is one nobody sees.
    if (typeof out.scrollHeight === "number") out.scrollTop = out.scrollHeight;
    var field = askSelField();
    if (field && askSelTurns.length) {
      field.setAttribute("placeholder", "Ask a follow-up — ⌘↩ to send");
    }
    if (askSelWhat && askSelWhat.rect) {
      askSelPlace(askSelBox, askSelWhat.rect, 8);
    }
    if (askSelAsking) return askSelSay("asking…");
    if (askSelSaid) return askSelSay(askSelSaid.words, askSelSaid.bad);
    return askSelSay("");
  }

  function runAskSel() {
    if (!askSelBox || !askSelWhat) return false;
    var field = askSelField();
    var words = str(field && field.value).trim();
    if (!words) {
      askSelSaid = { words: "ask something first", bad: true };
      askSelSay("ask something first", true);
      return false;
    }
    // One question at a time: a panel with two answers in flight cannot say
    // which of them the next one follows from.
    if (askSelAsking) return false;
    // Asked before the turn is added: a panel that cannot send is not a
    // panel with a question outstanding in it.
    if (typeof fetch !== "function") return false;
    var mine = ++askSelSeq;
    var turn = { question: words, answer: "" };
    var before = askSelTurns.slice();
    askSelTurns.push(turn);
    askSelAsking = words;
    askSelSaid = null;
    // The field empties for the next question, which is what continuing a
    // conversation looks like. It fills back in if the ask comes to nothing.
    if (field) field.value = "";
    renderAskSelOut();
    fetch("/api/ask_selection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // What this panel has already been told travels with the follow-up,
      // so "and the second one?" is a question rather than a fragment.
      body: JSON.stringify({ text: askSelWhat.text.slice(0, ASKSEL_MAX),
                             goal: askSelWhat.goal, question: words,
                             turns: before })
    }).then(function (r) { return r.json(); })
      .catch(function () { return null; })
      .then(function (result) {
        // A closed panel, or one asking something else by now: the answer
        // that came back is to a question nobody is looking at.
        if (mine !== askSelSeq) return result;
        askSelAsking = "";
        if (result && result.ok) {
          turn.answer = str(result.answer);
        } else {
          // A turn with no answer is not part of the conversation: it would
          // be quoted back at the model as one it had already had. The
          // question goes back where it was typed, to be tried again.
          var at = askSelTurns.indexOf(turn);
          if (at >= 0) askSelTurns.splice(at, 1);
          askSelSaid = { words: str((result && result.error)
                                    || "the question could not be asked"),
                         bad: true };
          var back = askSelField();
          if (back && !str(back.value).trim()) back.value = turn.question;
        }
        renderAskSelOut();
        askSelFocus();
        return result;
      });
    return true;
  }

  function askSelFocus() {
    var field = askSelField();
    if (field && field.parentNode && typeof field.focus === "function") {
      field.focus();
    }
    return true;
  }

  function closeAskSel() {
    if (askSelBox && askSelBox.parentNode) {
      askSelBox.parentNode.removeChild(askSelBox);
    }
    askSelBox = null;
    askSelWhat = null;
    askSelTurns = [];
    askSelAsking = "";
    askSelSaid = null;
    askSelSeq += 1;
    return true;
  }

  function openAskSel(what) {
    if (!what || !str(what.text).trim()) return false;
    ensureAskSelStyles();
    // The answer is markdown drawn the way the panes draw it, so that sheet
    // has to be up as well -- a workspace where nobody has opened a pane yet
    // has never needed it.
    ensureProjectStyles();
    closeAskSel();
    hideAskSelPill();
    askSelWhat = { text: str(what.text), goal: str(what.goal),
                   rect: what.rect || null };
    askSelBox = el("div", "hc-sel-box");
    askSelBox.setAttribute("data-hc-sel-box", "");
    var head = el("div", "hc-sel-head");
    head.appendChild(el("div", "hc-sel-title", "Ask Claude"));
    var shut = el("button", "hc-sel-close", "×");
    shut.type = "button";
    shut.setAttribute("data-hc-sel-close", "");
    shut.title = "close (esc)";
    head.appendChild(shut);
    askSelBox.appendChild(head);
    askSelBox.appendChild(el("div", "hc-sel-quote", askSelWhat.text));
    var field = el("textarea", "hc-sel-field");
    field.setAttribute("data-hc-sel-field", "");
    field.setAttribute("rows", "2");
    field.setAttribute("spellcheck", "false");
    field.setAttribute("placeholder", "Ask about this — ⌘↩ to send");
    askSelBox.appendChild(field);
    var row = el("div", "hc-sel-row");
    var go = el("button", "hc-sel-go", "Ask");
    go.type = "button";
    go.setAttribute("data-hc-sel-go", "");
    row.appendChild(go);
    var say = el("span", "hc-sel-say", "");
    say.setAttribute("data-hc-sel-say", "");
    row.appendChild(say);
    askSelBox.appendChild(row);
    var out = el("div", "hc-sel-out");
    out.setAttribute("data-hc-sel-out", "");
    askSelBox.appendChild(out);
    askSelAdopt(askSelBox);
    if (!askSelWhat.rect) {
      askSelWhat.rect = { left: 40, top: 80, bottom: 80 };
    }
    askSelPlace(askSelBox, askSelWhat.rect, 8);
    setTimeout(function () { if (field.parentNode) field.focus(); }, 0);
    return true;
  }

  function bindAskSel() {
    if (askSelBound || !document.addEventListener) return false;
    askSelBound = true;
    // Held down rather than clicked: a mousedown that is not defaulted
    // takes the highlight away before the handler can read it, and the
    // panel would open on nothing.
    document.addEventListener("mousedown", function (event) {
      var pill = closestByClass(event.target, "hc-sel-pill");
      if (pill) {
        event.preventDefault();
        openAskSel(pill.__hcWhat);
        return;
      }
      // A click in the workspace dismisses a panel nobody has asked
      // anything in yet -- it is one highlight away from coming back. Once
      // there is a conversation in it, it stays until it is closed: reading
      // the tree an answer is about is part of having the answer, and
      // losing the thread to that click is not.
      if (askSelBox && !closestByClass(event.target, "hc-sel-box")
          && !askSelTurns.length && !askSelAsking) {
        closeAskSel();
      }
    }, true);
    document.addEventListener("click", function (event) {
      if (closestAttr(event.target, "data-hc-sel-close")) closeAskSel();
      else if (closestAttr(event.target, "data-hc-sel-go")) runAskSel();
    });
    // Both keys are taken before the rail sees them: escape cancels a build
    // there, and a panel closing must not also stop one.
    document.addEventListener("keydown", function (event) {
      if (!askSelBox) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeAskSel();
        return;
      }
      if (event.key !== "Enter") return;
      if (!(event.metaKey || event.ctrlKey)) return;
      if (!closestAttr(event.target, "data-hc-sel-field")) return;
      event.preventDefault();
      event.stopPropagation();
      runAskSel();
    }, true);
    // Three ways a highlight is made or unmade. selectionchange alone
    // misses a field's own selection in the browsers that do not report
    // one, so the field's `select` is listened for beside it.
    ["selectionchange", "mouseup", "keyup", "select"].forEach(function (name) {
      document.addEventListener(name, function () {
        setTimeout(renderAskSelPill, 0);
      }, true);
    });
    // The offer sits at fixed coordinates over a page that scrolls under
    // it. Re-read the highlight rather than move it blind.
    if (typeof window !== "undefined" && window.addEventListener) {
      ["scroll", "resize"].forEach(function (name) {
        window.addEventListener(name, function () { renderAskSelPill(); }, true);
      });
    }
    return true;
  }

  // Only the listeners at boot. The sheet goes up with the first offer, so a
  // workspace nobody highlights anything in pays for nothing.
  function watchAskSelection() {
    return bindAskSel();
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
      ".hc-ask-kinds{display:flex;gap:6px;margin-bottom:10px}",
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
      ".hc-pick-box{position:relative}",
      ".hc-pick-close{position:absolute;top:8px;right:8px;width:22px;height:22px;display:flex;align-items:center;justify-content:center;border:none;background:transparent;color:var(--fnt,#9b9b9b);border-radius:2px;cursor:pointer;font:15px/1 'Source Code Pro',monospace}",
      ".hc-pick-close:hover{background:var(--hov,#f4f4f4);color:var(--ink,#111)}",
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

  // Which anchors the last patch run failed to find. Reset on every call.
  var patchMisses = [];

  // Its three add controls append a placeholder row. Make them ask for the
  // real value first; nothing else about them changes.
  function patchBundleSource(source) {
    // Four of the pairs below differ by workspace, and markup cannot ask at
    // render time the way stepTab's patched expression can. The scope is
    // published from the state fetch -- a synchronous XHR -- before this
    // runs, so it is already settled here; a standalone artifact with no
    // bridge never reaches this function at all.
    var chat = (typeof window !== "undefined" && window.__hcScope === "chat");
    var parts = [
      // Whose goal each row is, in a shared workspace. The row
      // model carries it and the line draws it; both are inert
      // when nothing sets it, which is every personal workspace.
      ["id: n.id, isReal: true, isAdd: false, title: n.title || 'Untitled', rawTitle: n.title,",
       "id: n.id, isReal: true, isAdd: false, title: n.title || 'Untitled', rawTitle: n.title, who: n.who || '',"],
      ["<sc-if value=\"{{ row.showTitle }}\" hint-placeholder-val=\"{{ true }}\"><span style=\"font-size:12.5px;color:{{ row.tcol }};font-weight:{{ row.fw }};text-decoration:{{ row.deco }}\">{{ row.title }}</span></sc-if>",
       "<sc-if value=\"{{ row.who }}\" hint-placeholder-val=\"{{ '' }}\"><span class=\"hc-who\">{{ row.who }}:</span></sc-if>\n<sc-if value=\"{{ row.showTitle }}\" hint-placeholder-val=\"{{ true }}\"><span style=\"font-size:12.5px;color:{{ row.tcol }};font-weight:{{ row.fw }};text-decoration:{{ row.deco }}\">{{ row.title }}</span></sc-if>"],
      // One line per goal. At rail width a wrapped title overlapped
      // the row under it, so the row says which span is the title.
      ["<div sc-camel-on-click=\"{{ row.sel }}\" sc-camel-on-double-click=\"{{ row.edit }}\" sc-camel-on-mouse-down=\"{{ row.dragStart }}\" ref=\"{{ row.rowRef }}\" style=\"display:flex;align-items:center;gap:7px;height:29px;padding:0 8px;border-radius:2px;cursor:pointer;background:{{ row.bg }};opacity:{{ row.dragOp }};box-shadow:{{ row.dropShadow }}\" style-hover=\"background:{{ row.hovBg }}\">",
       chat ? "<div class=\"hc-row\" data-hc-goal=\"{{ row.id }}\" sc-camel-on-click=\"{{ row.sel }}\" sc-camel-on-double-click=\"{{ row.edit }}\" sc-camel-on-mouse-down=\"{{ row.dragStart }}\" ref=\"{{ row.rowRef }}\" style=\"display:flex;align-items:center;gap:7px;height:29px;padding:0 8px;border-radius:2px;cursor:pointer;background:{{ row.bg }};opacity:{{ row.dragOp }};box-shadow:{{ row.dropShadow }}\" style-hover=\"background:{{ row.hovBg }}\">"
            : "<div sc-camel-on-click=\"{{ row.sel }}\" sc-camel-on-double-click=\"{{ row.edit }}\" sc-camel-on-mouse-down=\"{{ row.dragStart }}\" ref=\"{{ row.rowRef }}\" style=\"display:flex;align-items:center;gap:7px;height:29px;padding:0 8px;border-radius:2px;cursor:pointer;background:{{ row.bg }};opacity:{{ row.dragOp }};box-shadow:{{ row.dropShadow }}\" style-hover=\"background:{{ row.hovBg }}\">"],
      ["<sc-if value=\"{{ row.showTitle }}\" hint-placeholder-val=\"{{ true }}\"><span style=\"font-size:12.5px;color:{{ row.tcol }};font-weight:{{ row.fw }};text-decoration:{{ row.deco }}\">{{ row.title }}</span></sc-if>",
       chat ? "<sc-if value=\"{{ row.showTitle }}\" hint-placeholder-val=\"{{ true }}\"><span class=\"hc-rowtitle\" style=\"font-size:12.5px;color:{{ row.tcol }};font-weight:{{ row.fw }};text-decoration:{{ row.deco }}\">{{ row.title }}</span></sc-if>"
            : "<sc-if value=\"{{ row.showTitle }}\" hint-placeholder-val=\"{{ true }}\"><span style=\"font-size:12.5px;color:{{ row.tcol }};font-weight:{{ row.fw }};text-decoration:{{ row.deco }}\">{{ row.title }}</span></sc-if>"],
      // --- the launch layout, chat scope only ---------------------------
      // Names for the containers the skin dresses, and the one column the
      // artifact does not have: a rail for the prompt it assembles. The
      // rail is emitted before the inspector and ordered after it in CSS,
      // so no node is re-parented and a re-render cannot un-place it.
      ["<div style=\"display:{{ mainDisp }};gap:16px;align-items:flex-start;margin-top:14px\">",
       chat ? "<div class=\"hc-shell\" style=\"display:{{ mainDisp }};gap:16px;align-items:flex-start;margin-top:14px\">"
            : "<div style=\"display:{{ mainDisp }};gap:16px;align-items:flex-start;margin-top:14px\">"],
      ["<div style=\"display:{{ leftDisp }};flex-direction:column;height:calc(100vh - 185px);min-height:300px;box-sizing:border-box;flex:{{ leftFlex }};min-width:0;background:transparent;border:1px solid var(--bd);border-radius:2px;padding:16px 10px 6px\">",
       chat ? "<div class=\"hc-rail-left\" style=\"display:{{ leftDisp }};flex-direction:column;height:calc(100vh - 185px);min-height:300px;box-sizing:border-box;flex:{{ leftFlex }};min-width:0;background:transparent;border:1px solid var(--bd);border-radius:2px;padding:16px 10px 6px\">\n<div class=\"hc-rail-head\"><span class=\"hc-rail-name\">GOALS</span><span class=\"hc-rail-count\">{{ goalCount }}</span></div><div class=\"hc-search\"><div class=\"hc-search-field\"><span class=\"hc-search-glyph\"><svg width=\"12\" height=\"12\" viewBox=\"0 0 16 16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.7\"><circle cx=\"6.8\" cy=\"6.8\" r=\"4.4\"></circle><path d=\"M10.2 10.2 L14 14\" stroke-linecap=\"round\"></path></svg></span><input class=\"hc-search-input\" type=\"search\" placeholder=\"Search goals, notes, TODOs, prompts\" spellcheck=\"false\" autocomplete=\"off\" aria-label=\"Search goals\"><span class=\"hc-search-clear\" role=\"button\" title=\"Clear\" aria-label=\"Clear search\">\u00d7</span></div><div class=\"hc-search-hits\"></div></div>"
            : "<div style=\"display:{{ leftDisp }};flex-direction:column;height:calc(100vh - 185px);min-height:300px;box-sizing:border-box;flex:{{ leftFlex }};min-width:0;background:transparent;border:1px solid var(--bd);border-radius:2px;padding:16px 10px 6px\">"],
      ["<div style=\"display:{{ rightDisp }};flex:{{ rightFlex }};min-width:300px;position:sticky;top:16px;height:calc(100vh - 185px);min-height:300px;box-sizing:border-box;overflow-y:auto;background:transparent;border:1px solid var(--bd);border-radius:2px;padding:16px 18px 18px\">",
       chat ? "<div class=\"hc-rail-right\"><div class=\"hc-rail-head\"><span class=\"hc-rail-tabs\"></span><span class=\"hc-rail-select\">Select all</span><span class=\"hc-rail-saved\"></span></div><div class=\"hc-todos\"><div class=\"hc-todos-list\"></div><div class=\"hc-todos-actions\"><span class=\"hc-todo-copy\">Copy TODOs</span><span class=\"hc-todo-error\"></span><span class=\"hc-todo-build\" data-hc-todo-build=\"off\">Build</span></div></div><div class=\"hc-rail-prompt\"><sc-if value=\"{{ hasSel }}\" hint-placeholder-val=\"{{ true }}\"><div class=\"hc-rail-actions\"><span sc-camel-on-click=\"{{ copyPrompt }}\" class=\"hc-rail-copy\">{{ copyPromptLabel }}</span></div></sc-if><sc-if value=\"{{ noSel }}\" hint-placeholder-val=\"{{ false }}\"><div class=\"hc-rail-none\">Select a goal to see the prompt for it.</div></sc-if></div></div>\n<div class=\"hc-main\" style=\"display:{{ rightDisp }};flex:{{ rightFlex }};min-width:300px;position:sticky;top:16px;height:calc(100vh - 185px);min-height:300px;box-sizing:border-box;overflow-y:auto;background:transparent;border:1px solid var(--bd);border-radius:2px;padding:16px 18px 18px\">"
            : "<div style=\"display:{{ rightDisp }};flex:{{ rightFlex }};min-width:300px;position:sticky;top:16px;height:calc(100vh - 185px);min-height:300px;box-sizing:border-box;overflow-y:auto;background:transparent;border:1px solid var(--bd);border-radius:2px;padding:16px 18px 18px\">"],
      // The sources this goal was written against, as a rail over the
      // document. Both lists and both remove handlers are the artifact's
      // own, so every edit lands on set_sources through the path that was
      // already there -- this is the source control the textbox pane had.
      ["<div style=\"display:flex;gap:16px;margin-top:20px;border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5;background:var(--bg);box-shadow:0 -16px 0 0 var(--bg)\">",
       chat ? "<div class=\"hc-sources\"><span class=\"hc-sources-label\">SOURCES</span><sc-for list=\"{{ codeRows }}\" as=\"cr\" hint-placeholder-count=\"1\"><span class=\"hc-src\"><span class=\"hc-src-tag\">{{ cr.tag }}</span><span class=\"hc-src-label\">{{ cr.label }}</span><span sc-camel-on-click=\"{{ cr.rm }}\" title=\"Remove this source\" class=\"hc-src-rm\">\u00d7</span></span></sc-for><sc-for list=\"{{ docRows }}\" as=\"dr\" hint-placeholder-count=\"1\"><span class=\"hc-src\"><span class=\"hc-src-tag\">DOC</span><span class=\"hc-src-label\">{{ dr.label }}</span><span sc-camel-on-click=\"{{ dr.rm }}\" title=\"Remove this source\" class=\"hc-src-rm\">\u00d7</span></span></sc-for><span sc-camel-on-click=\"{{ srcAdd }}\" class=\"hc-src-add\">+ Add source</span></div>\n<div class=\"hc-tabs\" style=\"display:flex;gap:16px;margin-top:20px;border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5;background:var(--bg);box-shadow:0 -16px 0 0 var(--bg)\">"
            : "<div style=\"display:flex;gap:16px;margin-top:20px;border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5;background:var(--bg);box-shadow:0 -16px 0 0 var(--bg)\">"],
      // + Add source asks which of the three kinds the store keeps and
      // then for the value, rather than appending a placeholder row.
      ["      codeEmpty: codeList.length === 0,",
       chat ? "      srcAdd: () => window.__hcAskSource().then(function (v) { if (!v) return; if (v.type === 'doc') { setDocs(docList.concat([{ id: 'd' + Date.now().toString(36), type: 'doc', label: v.label }])); } else { setCode(codeList.concat([{ id: 'c' + Date.now().toString(36), type: v.type, label: v.label }])); } }),\n      codeEmpty: codeList.length === 0,"
            : "      codeEmpty: codeList.length === 0,"],
      // Two numbers the rails print: how many goals this chat has, and
      // how big the assembled prompt is. Characters over four, labelled
      // "~", because a token count is not something a browser can know.
      ["      hasCrumb: !!(trail && trail.length > 1),",
       chat ? "      goalCount: total,\n      draftTok: '~' + Math.ceil(String(draft || '').length / 4) + ' tok',\n      hasCrumb: !!(trail && trail.length > 1),"
            : "      hasCrumb: !!(trail && trail.length > 1),"],
      // All first, because a chat opens on All and its tree is small.
      // The count moves to the end so a chip reads as a name with a
      // number rather than a parenthetical aside.
      ["const filters = [['active', 'active', activeN], ['inprog', 'in progress', ipN], ['done', 'done', doneN], ['all', 'all', total]].map(([k, lab, n], i) => ({\n      lab: lab + ' (' + n + ')',",
       chat ? "const filters = [['all', 'All', total], ['active', 'Active', activeN], ['inprog', 'In progress', ipN], ['done', 'Done', doneN]].map(([k, lab, n], i) => ({\n      num: String(n),\n      lab: lab,"
            : "const filters = [['active', 'active', activeN], ['inprog', 'in progress', ipN], ['done', 'done', doneN], ['all', 'all', total]].map(([k, lab, n], i) => ({\n      lab: lab + ' (' + n + ')',"],
      ["<sc-for list=\"{{ filters }}\" as=\"f\" hint-placeholder-count=\"4\"><span sc-camel-on-click=\"{{ f.click }}\" style=\"font:{{ f.fw }} 11px 'Source Code Pro',monospace;cursor:pointer;color:{{ f.c }}\" style-hover=\"text-decoration:underline\">{{ f.lab }}</span></sc-for>",
       chat ? "<sc-for list=\"{{ filters }}\" as=\"f\" hint-placeholder-count=\"4\"><span class=\"hc-chip\" sc-camel-on-click=\"{{ f.click }}\" style=\"font:{{ f.fw }} 11px 'Source Code Pro',monospace;cursor:pointer;color:{{ f.c }}\" style-hover=\"text-decoration:underline\">{{ f.lab }} <b class=\"hc-chip-n\">{{ f.num }}</b></span></sc-for>"
            : "<sc-for list=\"{{ filters }}\" as=\"f\" hint-placeholder-count=\"4\"><span sc-camel-on-click=\"{{ f.click }}\" style=\"font:{{ f.fw }} 11px 'Source Code Pro',monospace;cursor:pointer;color:{{ f.c }}\" style-hover=\"text-decoration:underline\">{{ f.lab }}</span></sc-for>"],
      ["<div style=\"display:{{ headDisp }};align-items:flex-end;justify-content:space-between;gap:16px;padding:0 4px;flex-wrap:wrap\">",
       chat ? "<div class=\"hc-titlerow\" style=\"display:{{ headDisp }};align-items:flex-end;justify-content:space-between;gap:16px;padding:0 4px;flex-wrap:wrap\">"
            : "<div style=\"display:{{ headDisp }};align-items:flex-end;justify-content:space-between;gap:16px;padding:0 4px;flex-wrap:wrap\">"],
      ["<sc-if value=\"{{ pageGoals }}\" hint-placeholder-val=\"{{ true }}\"><div style=\"display:flex;gap:16px;align-items:baseline;flex-wrap:wrap\">",
       chat ? "<sc-if value=\"{{ pageGoals }}\" hint-placeholder-val=\"{{ true }}\"><div class=\"hc-chiprow\" style=\"display:flex;gap:16px;align-items:baseline;flex-wrap:wrap\">"
            : "<sc-if value=\"{{ pageGoals }}\" hint-placeholder-val=\"{{ true }}\"><div style=\"display:flex;gap:16px;align-items:baseline;flex-wrap:wrap\">"],
      // The window is named for what it is: one chat's goals. "Vault"
      // is the global product this scope is not.
      ["<span style=\"font-size:13.5px;font-weight:700;color:var(--ink)\">Vault</span>",
       chat ? "<span class=\"hc-brand\" style=\"font-size:13.5px;font-weight:700;color:var(--ink)\">Engelbart</span><span class=\"hc-project\"></span>"
            : "<span style=\"font-size:13.5px;font-weight:700;color:var(--ink)\">Vault</span>"],
      // A notification names a goal; going to it means selecting it in the
      // tree. The artifact owns selection, so it publishes one setter from
      // its own set(): the bridge calls that rather than reaching into state
      // it does not hold. Both scopes, so the anchor is found in both.
      ["  set(fn, touch) { this.setState(",
       "  set(fn, touch) { if (typeof window !== 'undefined') window.__hcSelectGoal = (id) => this.set(() => ({ page: 'goals', selId: id, editId: null, paneTab: 'context' })); if (typeof window !== 'undefined') window.__hcRevealGoal = (id) => { const tr = this.path(this.state.goals, id) || []; if (!tr.length) return false; this.set(s => { let gs = s.goals; tr.slice(0, -1).forEach(n => { gs = this.up(gs, n.id, x => ({ ...x, open: true })); }); return { page: 'goals', selId: id, editId: null, paneTab: 'context', goals: gs }; }); setTimeout(() => { const ids = this._rowIds || []; if (ids.indexOf(id) < 0) { this.set(() => ({ filter: 'all' })); } setTimeout(() => { const ids2 = this._rowIds || [], nx = ids2.indexOf(id), el = this._treeEl; if (el && nx >= 0) { const top = nx * 29, bot = top + 29; if (top < el.scrollTop) el.scrollTop = top; else if (bot > el.scrollTop + el.clientHeight) el.scrollTop = bot - el.clientHeight; } }, 0); }, 0); return true; }; this.setState("],
      // Room for the session this window is a second view of. The bridge
      // fills it in: only the server knows which conversation this is.
      ["</span><span style=\"font:11px 'Source Code Pro',monospace;color:var(--fnt)\">updated {{ updatedLabel }}</span></div>",
       chat ? "</span><span class=\"hc-panels\"></span><span class=\"hc-session\"></span><span class=\"hc-chats\"></span><span class=\"hc-handoff\"></span><span class=\"hc-alerts\"></span><span class=\"hc-settings\"></span><span class=\"hc-updated\" style=\"font:11px 'Source Code Pro',monospace;color:var(--fnt)\" title=\"saved {{ updatedLabel }}\">saved \u2713</span></div>"
            : "</span><span style=\"font:11px 'Source Code Pro',monospace;color:var(--fnt)\">updated {{ updatedLabel }}</span></div>"],
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
      // The seeded filter never reached the page: the constructor read
      // saved.selId, saved.labels and saved.paneTab, but hardcoded the
      // filter. A chat opening on 'active' hides its finished goals, and
      // the chip row said 'active' while the store said otherwise.
      ["      filter: 'active',",
       "      filter: (saved && ['active', 'inprog', 'done', 'all'].indexOf(saved.filter) >= 0) ? saved.filter : 'active',"],
      // The tree, handed in from outside. The bridge's sync gives the
      // server's tree to the artifact's own state, so a change made on the
      // server -- a build marking rows queued and the goal in progress, a
      // row coming back done -- lands on the page that is open, with no
      // reload. Published from the constructor, where norm is in scope, so
      // a tree pushed in takes the shape a loaded one does.
      ["    if (!g0) { g0 = this.seed(); if (saved) this._resetSave = true; }\n",
       "    if (!g0) { g0 = this.seed(); if (saved) this._resetSave = true; }\n    if (typeof window !== 'undefined') window.__hcSetGoals = (goals, selId) => this.set(() => (typeof selId === 'string' ? { goals: norm(goals), selId } : { goals: norm(goals) }));\n"],
      // And the store it writes back has to declare the version the seed
      // trusts, or the reader's own choice is discarded on the next load
      // as if it were the artifact's default. v7 means "this origin has
      // been seeded by the bridge", which is true the moment it saves.
      // And the goals it writes back carry the rail's fields -- the TODO
      // rows, their markdown, the reader's prompt -- from the store, not
      // from the artifact's own memory. The artifact read those fields once
      // at boot and never again; the rail writes them to the store as they
      // change. Left as they were, the artifact's next save (a filter chip,
      // a selection) would put its boot-time copy back over the rows the
      // reader had just typed, and the watcher would then import that copy.
      ["localStorage.setItem('hc-vault-ui-v1', JSON.stringify({ v: 6, goals,",
       "localStorage.setItem('hc-vault-ui-v1', JSON.stringify({ v: 7, goals: (typeof window !== 'undefined' && window.__hcRailFields) ? window.__hcRailFields(goals) : goals,"],
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
      // And the same demo copy reaches a real tree by a second door: the
      // constructor backfilled any empty desc from a map keyed by the sample
      // tree's own ids -- g1, g2, g3, g4 among them -- which are exactly the
      // ids the vault mints. One filter chip was enough to write four
      // sentences nobody wrote into goals.json, and from there into the
      // prompt the reader copied. The map stays; nothing applies it.
      ["    try { ad(g0); } catch (e) {}",
       "    /* the demo backfill above is not applied: its keys are the ids\n"
       + "       the vault mints, so it would overwrite real goals */"],
      // Its other demo door is a fallback rather than a backfill: a goal
      // the reader adds in the tree is minted with ctx: null, and every
      // context read then answered from the artifact's own sample -- so
      // the prompt for a goal named a second ago claimed an objective, a
      // GitHub repo and a document belonging to somebody else's demo, and
      // said so until the page was next reloaded. contextOf already sets
      // every field for a goal the server knows about; these three are
      // what the gap was filled with.
      ["    const CTXDEF = { objective: 'Get the drawable frame populating in Chrome and validate the boundary feature end-to-end.', said: '\"why is the drawable boundary not showing up?\"\\n\"ok i put the frame in google chrome but it doesnt seem to be getting populated\u2026\"', decided: '- Build as LSUIElement menu bar app (no dock icon, no window)\\n- Split responsibilities: extension handles Chrome, native app handles other apps', built: '- Menu bar record icon (33\u00d724 points, positioned at x=1079)\\n- Captured events in Supabase database (41 events from test session)', hit: '- Accessibility permissions invalidated after rebuilds (code hash mismatch)\\n- Chrome doesn\\'t expose page content to OS Accessibility layer\\n- Full validation that drawable frame feature works as intended', open: '- Full validation that drawable frame feature works as intended' };",
       "    const CTXDEF = {};"],
      ["    const CODEDEF = [{ id: 'c1', type: 'github', label: 'divadbaroon/claude-plugins' }];",
       "    const CODEDEF = [];"],
      ["    const DOCDEF = [{ id: 'd1', type: 'doc', label: 'design-notes.md' }];",
       "    const DOCDEF = [];"],
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
      // In a chat the draft is not sent anywhere: it is assembled from the
      // goal's document and read out through Copy prompt. Nothing kept an
      // edit to it -- not a reload, not a CONTEXT round trip -- so the box
      // is read-only and the line above it says which of the two it is.
      // The global pane keeps its editable box: there the draft is what
      // runAgent actually sends.
      ["showPrompt: !!sel && paneTab === 'prompt'",
       chat ? "showPrompt: !!sel && paneTab === 'prompt' /* its own tab here */"
            : "showPrompt: !!sel && paneTab === 'agent'"],
      ["<span sc-camel-on-click=\"{{ tabPrompt }}\" style=\"padding:0 2px 7px;font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;cursor:pointer;color:{{ tpC }};border-bottom:2px solid {{ tpBd }};margin-bottom:-1px\">PROMPT</span>\n",
       chat ? "<!--prompt tab kept for chat scope--><span sc-camel-on-click=\"{{ tabPrompt }}\" style=\"padding:0 2px 7px;font:600 10px 'Source Code Pro',monospace;letter-spacing:1.2px;cursor:pointer;color:{{ tpC }};border-bottom:2px solid {{ tpBd }};margin-bottom:-1px\">PROMPT</span>\n"
            : "<!--prompt folded into agent-->\n"],
      ["<sc-if value=\"{{ showPrompt }}\" hint-placeholder-val=\"{{ true }}\">\n<div style=\"margin-top:16px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">RECOMMENDED PROMPT</div>\n<div style=\"position:relative\"><textarea key=\"{{ selKey }}\" sc-camel-default-value=\"{{ draft }}\" ref=\"{{ draftRef }}\" sc-camel-on-input=\"{{ promptInput }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;min-height:96px;max-height:300px;overflow-y:auto;resize:none;margin-top:8px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 11px;font:12px/1.6 'Source Code Pro',monospace;color:var(--dtxt);outline:none\"></textarea>\n<span sc-camel-on-click=\"{{ gen }}\" title=\"Regenerate prompt\" style=\"position:absolute;right:8px;bottom:8px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:2px;font:13px/1 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none\" style-hover=\"color:var(--acc);background:var(--hov)\">\u21bb</span>\n</div>\n</sc-if>",
       chat ? "<sc-if value=\"{{ showPrompt }}\" hint-placeholder-val=\"{{ true }}\"><div style=\"margin-top:16px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">RECOMMENDED PROMPT</div><div style=\"margin-top:5px;font:italic 11.5px/1.6 'Source Code Pro',monospace;color:var(--mut);max-width:62ch\">assembled from your goal document \u00b7 read-only</div>\n<div style=\"position:relative\"><textarea key=\"{{ selKey }}\" sc-camel-default-value=\"{{ draft }}\" ref=\"{{ draftRef }}\" readonly=\"readonly\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;min-height:96px;max-height:300px;overflow-y:auto;resize:none;margin-top:8px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 11px;font:12px/1.6 'Source Code Pro',monospace;color:var(--dtxt);outline:none\"></textarea>\n<span sc-camel-on-click=\"{{ gen }}\" title=\"Regenerate prompt\" style=\"position:absolute;right:8px;bottom:8px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:2px;font:13px/1 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none\" style-hover=\"color:var(--acc);background:var(--hov)\">\u21bb</span>\n</div>\n<div style=\"margin-top:10px;display:flex;justify-content:flex-end\"><span sc-camel-on-click=\"{{ copyPrompt }}\" style=\"padding:4px 11px;border:1px solid var(--bd2);border-radius:2px;font:600 11px 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none\" style-hover=\"color:var(--acc);border-color:var(--acc)\">{{ copyPromptLabel }}</span></div>\n</sc-if>"
            : "<sc-if value=\"{{ showPrompt }}\" hint-placeholder-val=\"{{ true }}\"><div style=\"margin-top:16px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">AGENT</div><div style=\"margin-top:5px;font:italic 11.5px/1.6 'Source Code Pro',monospace;color:var(--mut);max-width:62ch\">Run Claude Code on this goal with the self-contained context Vault has assembled. Progress appears in REVIEW.</div><details class=\"hc-promptbox\"><summary class=\"hc-promptsum\">RECOMMENDED PROMPT</summary>\n<div style=\"position:relative\"><textarea key=\"{{ selKey }}\" sc-camel-default-value=\"{{ draft }}\" ref=\"{{ draftRef }}\" sc-camel-on-input=\"{{ promptInput }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;min-height:96px;max-height:300px;overflow-y:auto;resize:none;margin-top:8px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:9px 11px;font:12px/1.6 'Source Code Pro',monospace;color:var(--dtxt);outline:none\"></textarea>\n<span sc-camel-on-click=\"{{ gen }}\" title=\"Regenerate prompt\" style=\"position:absolute;right:8px;bottom:8px;width:20px;height:20px;display:flex;align-items:center;justify-content:center;border-radius:2px;font:13px/1 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer;user-select:none\" style-hover=\"color:var(--acc);background:var(--hov)\">\u21bb</span>\n</div>\n</details></sc-if>"],
      // The notes box is the Context pane now: one markdown document per
      // goal, rendered as it is typed, with the prompts that fed it below.
      // The textbox pane it replaces -- objective, code, documents,
      // decisions, blockers, built -- goes dormant in BOTH scopes. Nothing
      // is deleted: every field, handler and getter behind it stays, so the
      // markup can be re-gated in one line if the document does not hold.
      ["showNotes: !!sel && paneTab === 'prompt'",
       "showNotes: !!sel && paneTab === 'context'"],
      ["showCtx: !!sel && paneTab === 'context',",
       "showCtx: false,"],
      // The goal is already named at the top of the inspector; the draft
      // restating it just pushed the actual content down.
      ["blocks.push(isSub ? 'Within the main goal \"' + (trail[0].title || 'Untitled') + '\", I am working on: ' + (sel.title || 'Untitled') + '.' : 'I am working on the goal: ' + (sel.title || 'Untitled') + '.');\n",
       "void 0;\n"],
      // The pane, top to bottom: the goal's own document, then the prompts
      // it was written from. The heading rule above it went with the
      // "additional" framing -- this is not an addendum to anything.
      ["<div style=\"margin-top:20px;padding-top:14px;border-top:1px solid var(--bd);font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">ADDITIONAL NOTES</div>\n<div style=\"position:relative;margin-top:7px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\">\n<div style=\"padding:10px 12px;font:12px/1.7 'Source Code Pro',monospace;white-space:pre-wrap;word-break:break-word;min-height:96px;color:var(--dtxt)\">{{ notesOverlay }}</div>\n<textarea value=\"{{ notesVal }}\" sc-camel-on-change=\"{{ notesChange }}\" spellcheck=\"false\" placeholder=\"Plan in markdown \u2014 # heading, - list, - [ ] task, **bold**, `code`\" style=\"position:absolute;inset:0;width:100%;height:100%;box-sizing:border-box;padding:10px 12px;font:12px/1.7 'Source Code Pro',monospace;background:transparent;border:none;outline:none;resize:none;overflow:hidden;color:transparent;caret-color:var(--ink);white-space:pre-wrap;word-break:break-word\"></textarea>\n</div>",
       "<div style=\"margin-top:16px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">NOTES</div>\n<div style=\"position:relative;margin-top:7px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\">\n<div style=\"padding:10px 12px;font:12px/1.7 'Source Code Pro',monospace;white-space:pre-wrap;word-break:break-word;min-height:360px;color:var(--dtxt)\">{{ notesOverlay }}</div>\n<textarea value=\"{{ notesVal }}\" sc-camel-on-change=\"{{ notesChange }}\" spellcheck=\"false\" placeholder=\"Write in markdown \u2014 # heading, - list, - [ ] task, **bold**, `code`\" style=\"position:absolute;inset:0;width:100%;height:100%;box-sizing:border-box;padding:10px 12px;font:12px/1.7 'Source Code Pro',monospace;background:transparent;border:none;outline:none;resize:none;overflow:hidden;color:transparent;caret-color:var(--ink);white-space:pre-wrap;word-break:break-word\"></textarea>\n</div>\n<div style=\"margin-top:15px;display:flex;align-items:baseline;justify-content:space-between;gap:12px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">RELATED PROMPTS</span><span class=\"hc-prompt-add\"></span></div>\n<div style=\"margin-top:6px;max-height:420px;overflow-y:auto;overscroll-behavior:contain;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\">\n<sc-for list=\"{{ histRows }}\" as=\"hr\" hint-placeholder-count=\"2\">\n<div style=\"padding:8px 11px;border-bottom:{{ hr.bd }}\"><div style=\"display:flex;align-items:baseline;gap:10px\"><span style=\"flex:1;min-width:0;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">{{ hr.when }}</span><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">{{ hr.origin }}</span><span sc-camel-on-click=\"{{ hr.del }}\" title=\"Unlink this prompt\" style=\"flex:none;font:12px 'Source Code Pro',monospace;color:var(--fnt);cursor:pointer\" style-hover=\"color:var(--del)\">\u00d7</span></div><div style=\"margin-top:3px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);white-space:pre-wrap;word-break:break-word\">{{ hr.text }}</div></div>\n</sc-for>\n<sc-if value=\"{{ histEmpty }}\" hint-placeholder-val=\"{{ false }}\"><div style=\"padding:12px 11px;font-size:11.5px;color:var(--fnt)\">No prompts of yours are tied to this goal yet.</div></sc-if>\n</div>"],
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
      // The status badge beside the goal's title wore the accent -- a filled
      // outline in the one colour the page saves for what it wants read --
      // and it sat in the corner the eye lands on first, restating a word
      // the tree already carries on the row. Muted text on a hairline says
      // the same thing without competing with the title; the accent comes
      // back on hover, where it still has to say "this is a control".
      ["<span sc-camel-on-click=\"{{ stCycle }}\" title=\"Click to change status\" style=\"flex:none;margin-top:2px;padding:2px 9px;border:1px solid var(--acc);border-radius:3px;background:var(--panel);font:600 9.5px 'Source Code Pro',monospace;letter-spacing:.6px;color:var(--acc);cursor:pointer;user-select:none;white-space:nowrap\" style-hover=\"background:var(--acchov)\">{{ stBadge }}</span>",
       "<span sc-camel-on-click=\"{{ stCycle }}\" title=\"Click to change status\" style=\"flex:none;margin-top:3px;padding:1.5px 8px;border:1px solid var(--bd);border-radius:3px;background:transparent;font:500 9.5px 'Source Code Pro',monospace;letter-spacing:.6px;color:var(--fnt);cursor:pointer;user-select:none;white-space:nowrap\" style-hover=\"color:var(--acc);border-color:var(--acc)\">{{ stBadge }}</span>"],
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
       "<!--related prompts moved under the document-->\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">BLOCKERS &amp; OPEN QUESTIONS</span><sc-if value=\"{{ inhHit }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxHit }}\" sc-camel-on-change=\"{{ ctxHitCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">ALREADY BUILT</span><sc-if value=\"{{ inhBuilt }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxBuilt }}\" sc-camel-on-change=\"{{ ctxBuiltCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>\n<div style=\"margin-top:15px;display:flex;align-items:center;gap:7px\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">DECISIONS</span><sc-if value=\"{{ inhDecided }}\" hint-placeholder-val=\"{{ false }}\"><span style=\"flex:none;padding:0.5px 6px;border:1px solid var(--bd);border-radius:2px;font:600 8px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">INHERITED</span></sc-if></div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2)\"><textarea key=\"{{ selCtxKey }}\" value=\"{{ ctxDecided }}\" sc-camel-on-change=\"{{ ctxDecidedCh }}\" sc-camel-on-input=\"{{ ctxSize }}\" ref=\"{{ ctxRef }}\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;border:none;outline:none;resize:none;background:transparent;padding:8px 11px;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--dtxt);overflow:hidden\"></textarea></div>"],
      // The tree position, in the pane and in the prompt. The crumb under
      // the title said the same thing in a place that had no room for it.
      ["\n<div style=\"margin-top:15px;display:flex;align-items:baseline;justify-content:space-between;gap:12px\"><span style=\"display:inline-flex;gap:7px;align-items:center\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">CODE CONTEXT</span>",
       "\n<div style=\"margin-top:15px;font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">WHERE THIS SITS</div>\n<div style=\"margin-top:6px;border:1px solid var(--bd);border-radius:2px;background:var(--panel2);padding:8px 11px\">\n<sc-for list=\"{{ ctxTrail }}\" as=\"tr\" hint-placeholder-count=\"2\">\n<div style=\"padding:1px 0 1px {{ tr.pad }};font:11.5px/1.6 'Source Code Pro',monospace;color:{{ tr.c }}\">{{ tr.mark }}{{ tr.title }}</div>\n</sc-for>\n</div>\n<div style=\"margin-top:15px;display:flex;align-items:baseline;justify-content:space-between;gap:12px\"><span style=\"display:inline-flex;gap:7px;align-items:center\"><span style=\"font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--mut)\">CODE CONTEXT</span>"],
      ["      hasCrumb: !!(trail && trail.length > 1),\n",
       "      hasCrumb: !!(trail && trail.length > 1),\n      ctxTrail: (trail || []).map((n, i) => ({\n        title: n.title || 'Untitled',\n        pad: (i * 14) + 'px',\n        mark: i ? '\\u2514 ' : '',\n        c: n === sel ? 'var(--ink)' : 'var(--fnt)'\n      })),\n"],
      // The recommended prompt follows the same order as the pane it is
      // built from, so what the reader checked is what the agent is told.
      ["      const parts = [];\n      const obj = ctxGet('objective'); if (obj && obj.trim()) parts.push('Objective:\\n' + obj.trim());\n      const dec = ctxGet('decided'); if (dec && dec.trim()) parts.push('Established decisions:\\n' + dec.trim());\n      const blt = ctxGet('built'); if (blt && blt.trim()) parts.push('Already built:\\n' + blt.trim());\n      const blk = ctxGet('hit'); if (blk && blk.trim()) parts.push('Blockers & open questions:\\n' + blk.trim());\n      if (codeList.length) parts.push('Code context:\\n' + codeList.map(c => '- ' + c.label + ' (' + c.type + ')').join('\\n'));\n      if (docList.length) parts.push('Document context:\\n' + docList.map(d => '- ' + d.label).join('\\n'));\n",
       "      const parts = [];\n      const secOf = (t) => { const lines = String((sel && sel.notes) || '').split('\\n'); const body = []; let on = (t === ''), fence = null; for (let i = 0; i < lines.length; i += 1) { const line = lines[i], bare = line.replace(/^ */, ''); if (line.length - bare.length <= 3) { const m = /^(`{3,}|~{3,})(.*)$/.exec(bare); if (m) { const ch = m[1].charAt(0), run = m[1].length, rest = m[2]; if (fence === null) { if (!(ch === '`' && rest.indexOf('`') >= 0)) fence = [ch, run]; } else if (ch === fence[0] && run >= fence[1] && !rest.trim()) fence = null; if (on) body.push(line); continue; } } if (fence === null && line.indexOf('# ') === 0) { on = line.slice(2).trim() === t; continue; } if (on) body.push(line); } return body.join('\\n').trim(); };\n      const section = (t) => { const body = secOf(t); if (body) parts.push(t + ':\\n' + body); };\n      const obj = secOf('Objective') || String(ctxGet('objective') || '').trim(); if (obj) parts.push('Objective:\\n' + obj);\n      const pre = secOf(''); if (pre) parts.push('Notes:\\n' + pre);\n      const titems = (Array.isArray(sel && sel.todo_items) ? sel.todo_items : []).filter(r => r && String(r.text || '').trim());\n      const todos = titems.length ? titems.map(r => '    '.repeat(r.depth | 0) + '- [' + (String(r.status || '') || 'active') + '] ' + r.text).join('\\n') : String((sel && sel.todos_md) || '').trim();\n      if (todos) parts.push('TODOs (each with its current state):\\n' + todos);\n      const shots = (window.__hcPromptUI && window.__hcPromptUI.todoList) ? window.__hcPromptUI.todoList.attachmentLines(titems) : [];\n      if (shots.length) parts.push('Attachments (files the rows cite; open them for the rows that name them):\\n' + shots.join('\\n'));\n      if (trail && trail.length) parts.push('Where this sits:\\n' + trail.map((n, i) => '  '.repeat(i) + (i ? '\\u2514 ' : '') + (n.title || 'Untitled')).join('\\n'));\n      if (codeList.length) parts.push('Code context:\\n' + codeList.map(c => '- ' + c.label + ' (' + c.type + ')').join('\\n'));\n      if (docList.length) parts.push('Document context:\\n' + docList.map(d => '- ' + d.label).join('\\n'));\n      const said = (sel.prompts || []).slice().reverse();\n      if (said.length) parts.push('Related prompts, in my own words:\\n' + said.map(q => '- \"' + String(q.text || '').replace(/\\s+/g, ' ').trim() + '\"').join('\\n'));\n      section('In my words');\n      section('Decisions');\n      section('Built');\n      section('Blockers');\n      section('Open questions');\n"],
      // A prompt without its conversation is a quote without a source. The
      // separator belongs to the source, not to the line: chat prompt
      // records carry no session_id (chat_state writes id, ordinal, role,
      // text and created_at), so a middot drawn beside the value rendered
      // on every row with nothing after it.
      ["        text: p.text,\n",
       "        text: p.text,\n        conv: p.conv ? ' \u00b7 conversation ' + p.conv : '',\n"
       + "        origin: p.auto ? 'automatic' : 'yours',\n"],
      ["<span style=\"flex:1;min-width:0;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt)\">{{ hr.when }}</span>",
       "<span style=\"flex:1;min-width:0;font:600 9px 'Source Code Pro',monospace;letter-spacing:.5px;color:var(--fnt);white-space:nowrap;overflow:hidden;text-overflow:ellipsis\">{{ hr.when }}{{ hr.conv }}</span>"],
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
      // A goal nobody has written to yet shows the six headings rather
      // than an empty box: the shape of the document is the prompt to fill
      // it in. Nothing is stored until the first keystroke, which persists
      // the whole document through the existing notesChange -> import path.
      ["notesVal: sel ? (sel.notes || '') : '',",
       "notesVal: sel ? (sel.notes || window.__hcDefaultDoc || '') : '',"],
      ["notesOverlay: sel ? this.md(sel.notes || '') : null,",
       "notesOverlay: sel ? this.md(sel.notes || window.__hcDefaultDoc || '') : null,"],
      // Copying the recommended prompt, without doCopy's two side effects:
      // it appends a metadata footer nobody asked for, and it records the
      // draft as a prompt the user is then shown as one of their own words.
      ["      copy: () => this.doCopy(),\n",
       !chat ? "      copy: () => this.doCopy(),\n" :
       "      copy: () => this.doCopy(),\n"
       // What the rail is printing is what leaves: one string, the same one a
       // Build of these rows would open on. It used to be two -- the server's
       // context plus whatever the reader had typed into the rail's field --
       // joined here, because the field held the second half. There is no
       // field now. With no context to be had, the artifact's own draft
       // stands in, so Copy is never a no-op.
       + "      copyPrompt: () => { "
       + "const t = String(window.__hcPromptContext || '') || String(draft || ''); "
       + "const done = () => { this.setState({ copied: true }); clearTimeout(this._ct); "
       + "this._ct = setTimeout(() => this.setState({ copied: false }), 1600); }; "
       + "const fb = () => { const ta = document.createElement('textarea'); ta.value = t; "
       + "ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); "
       + "ta.select(); let ok = false; try { ok = document.execCommand('copy') === true; } catch (e) { ok = false; } ta.remove(); return ok; }; "
       + "if (navigator.clipboard && navigator.clipboard.writeText) "
       + "{ navigator.clipboard.writeText(t).then(done, () => { if (fb()) done(); }); } "
       + "else { if (fb()) done(); } },\n"
       + "      copyPromptLabel: copied ? 'copied \u2713' : 'Copy prompt',\n"],
      ["docAdd: () => setDocs(docList.concat([{ id: 'd' + Date.now().toString(36), type: 'doc', label: 'notes.md' }]))",
       "docAdd: () => window.__hcAsk('doc').then(function (v) { if (v) setDocs(docList.concat([{ id: 'd' + Date.now().toString(36), type: 'doc', label: v }])); })"],
      // The title is edited where it is largest: the inspector header. The
      // heading div becomes an input in the same clothes; Enter or blur
      // commits, Escape puts back what was there. The sidebar's double-click
      // edit stays, but is no longer the only door.
      ["<div style=\"flex:1;min-width:0;font-size:13.5px;font-weight:700;line-height:1.4;color:var(--ink)\">{{ selTitle }}</div>",
       "<input key=\"{{ selKey }}\" sc-camel-default-value=\"{{ titleRaw }}\" ref=\"{{ titleRef }}\" sc-camel-on-key-down=\"{{ titleKey }}\" sc-camel-on-blur=\"{{ titleBlur }}\" placeholder=\"Untitled\" spellcheck=\"false\" style=\"flex:1;min-width:0;font-size:13.5px;font-weight:700;line-height:1.4;color:var(--ink);font-family:inherit;border:none;outline:none;background:transparent;padding:0;margin:0\">"],
      ["      selTitle: sel ? (sel.title || 'Untitled') : '',",
       "      selTitle: sel ? (sel.title || 'Untitled') : '',\n"
       + "      titleRaw: sel ? (sel.title || '') : '',\n"
       + "      titleKey: (e) => { if (e.key === 'Enter') { e.preventDefault(); e.target.blur(); } else if (e.key === 'Escape') { e.target.value = sel ? (sel.title || '') : ''; e.target.blur(); } },\n"
       + "      titleBlur: (e) => { const v = (e.target.value || '').trim(); this._new = null; if (sel && v !== (sel.title || '')) this.set(s => ({ goals: this.up(s.goals, sel.id, x => ({ ...x, title: v })) }), true); },\n"
       + "      titleRef: (el) => { if (el && sel && this._focusTitle === sel.id) { this._focusTitle = null; el.focus(); el.select(); } },"],
      // The description box under the title is gone: the notes document is
      // the description. descChange and its siblings stay dormant behind
      // this one line, like the textbox pane before them.
      ["<textarea value=\"{{ descVal }}\" sc-camel-on-change=\"{{ descChange }}\" sc-camel-on-input=\"{{ descInput }}\" ref=\"{{ descRef }}\" rows=\"1\" placeholder=\"Add a description…\" spellcheck=\"false\" style=\"display:block;width:100%;box-sizing:border-box;margin-top:6px;border:none;outline:none;resize:none;overflow:hidden;background:transparent;padding:0;font:11.5px/1.6 'Source Code Pro',monospace;color:var(--mut)\"></textarea>",
       "<!--description removed: the notes document is the description-->"],
      // A new top-level goal lands at the top of the list, where the eye
      // already is, and the cursor lands in the header input, where the
      // title is largest -- so editId stays null and no row input opens.
      // A subgoal keeps appending: its add control sits under the children
      // it joins.
      ["addUnder(pid) {\n    const n = this.node(); this._new = n.id;\n    this.set(s => ({\n      goals: pid ? this.up(s.goals, pid, x => ({ ...x, open: true, children: (x.children || []).concat([n]) })) : s.goals.concat([n]),\n      selId: n.id, editId: n.id\n    }), true);\n  }",
       "addUnder(pid) {\n    const n = this.node(); this._new = n.id; this._focusTitle = n.id;\n    this.set(s => ({\n      goals: pid ? this.up(s.goals, pid, x => ({ ...x, open: true, children: (x.children || []).concat([n]) })) : [n].concat(s.goals),\n      selId: n.id, editId: null\n    }), true);\n  }"],
      // The way in to a subgoal existed only under goals that already had
      // one: the add row was drawn beneath the children it joins, so a
      // childless goal offered no door. The selected row always offers it
      // now -- the eye is already there, and adding under anything else
      // starts with selecting it.
      ["if (open && kids.length) rows.push({",
       "if (open && (kids.length || isSel)) rows.push({"],
      // A held arrow walks the tree, which already wraps at both ends.
      // Key repeat was swallowed for every key, so holding moved one row
      // and stopped. It stays swallowed for everything else: a held
      // cmd+enter or cmd+backspace should not pour goals in or out.
      ["this._kd = (e) => {\n      if (e.repeat) return;\n",
       "this._kd = (e) => {\n      if (e.repeat && e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;\n"],
      // Left and right move across the hierarchy: right opens a folded
      // branch or steps into its first drawn child, left folds an open
      // branch or steps out to the parent. The bridge's treeStep decides;
      // a move scrolls the row into view the way up and down do.
      ["if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { if (typing) return; nav(e.key === 'ArrowUp'); return; }\n",
       "if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { if (typing) return; nav(e.key === 'ArrowUp'); return; }\n"
       + "      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { if (typing) return; const step = window.__hcPromptUI && window.__hcPromptUI.treeStep(this.state.goals, this._rowIds || [], this.state.selId, e.key === 'ArrowLeft'); if (!step) return; e.preventDefault(); if (step.fold) this.set(s => ({ goals: this.up(s.goals, step.fold.id, x => ({ ...x, open: step.fold.open })) })); else this.set(() => ({ selId: step.selId, editId: null })); const ids = this._rowIds || [], nx = ids.indexOf(step.selId), el = this._treeEl; if (el && nx >= 0) { const top = nx * 29, bot = top + 29; if (top < el.scrollTop) el.scrollTop = top; else if (bot > el.scrollTop + el.clientHeight) el.scrollTop = bot - el.clientHeight; } return; }\n"],
      // Done means the whole branch is done: the row's check marks every
      // child too and folds the branch shut. Unchecking reopens only the
      // goal itself -- what each child was before is not something to
      // guess at, and a struck-through child is one click from back.
      ["done: (e) => { e.stopPropagation(); this.set(s => ({ goals: this.up(s.goals, n.id, x => ({ ...x, done: !x.done })), editId: null }), true); },",
       "done: (e) => { e.stopPropagation(); const dn = (g) => ({ ...g, done: true, children: (g.children || []).map(dn) }); this.set(s => ({ goals: this.up(s.goals, n.id, x => x.done ? { ...x, done: false } : { ...dn(x), open: false }), editId: null }), true); },"],
      // The inspector's status control is the same door with a different
      // handle, so 'done' cascades and folds there as well.
      ["const setSt = (k) => sel && this.set(s => ({ goals: this.up(s.goals, sel.id, x => k === 'done' ? { ...x, done: true } : { ...x, done: false, status: k === 'inprog' ? 'inprog' : 'todo' }) }), true);",
       "const setSt = (k) => { if (!sel) return; const dn = (g) => ({ ...g, done: true, children: (g.children || []).map(dn) }); this.set(s => ({ goals: this.up(s.goals, sel.id, x => k === 'done' ? { ...dn(x), open: false } : { ...x, done: false, status: k === 'inprog' ? 'inprog' : 'todo' }) }), true); };"]
    ];
    // Every pair is a string match against a checked-in artifact, so a
    // re-vendored bundle degrades to "the layout silently did not apply".
    // The indexes that matched nothing are kept rather than only warned
    // about, so a test can assert the whole set landed.
    patchMisses = [];
    return parts.reduce(function (patched, part, index) {
      if (patched.indexOf(part[1]) >= 0) return patched;
      var at = patched.indexOf(part[0]);
      if (at < 0) {
        patchMisses.push(index);
        console.warn("[hc ui] template anchor " + index
                     + " was not found; left as-is: "
                     + String(part[0]).slice(0, 60));
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
  window.__hcAskSource = askSource;
  window.__hcRailFields = railFields;

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
      ".hc-chat-addbtn{flex:none;margin-right:7px;border:1px solid var(--bd2,#d5d5d5);background:var(--hov,#f4f4f4);color:var(--mut,#575757);border-radius:2px;padding:3px 10px;cursor:pointer;font:600 10px 'Source Code Pro',monospace}",
      ".hc-chat-addbtn:hover{background:var(--bd,#e6e6e6);color:var(--ink,#111)}",
      // The header's workspace-wide link: sized to the session chip it sits
      // beside, not to the pane buttons.
      ".hc-chats{display:inline-flex;align-items:center;align-self:center}",
      ".hc-chat-linkbtn{border:1px solid var(--bd2,#d5d5d5);background:transparent;color:var(--mut,#575757);border-radius:2px;padding:2px 8px;cursor:pointer;font:600 10px 'Source Code Pro',monospace;line-height:1.4}",
      ".hc-chat-linkbtn:hover{background:var(--hov,#f4f4f4);color:var(--ink,#111)}",
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
    // The analysis this reports is the global vault's, and the route it
    // reads answers "global scope only" in a chat -- so polling it there is
    // a request every few seconds for the life of the page whose answer
    // cannot change anything on screen. Same early return as loadPlan and
    // watchRunFeed, and it still resolves so callers can await first paint.
    if (serverState.scope === "chat") return Promise.resolve();
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
    search: { rank: searchGoals, render: renderSearch, wordScore: wordScore,
              distance: editDistance, query: searchQuery,
              clear: clearSearch },
    paneShape: paneShape,
    reconcileState: reconcileState,
    clearKeepPane: clearKeepPane,
    patchBundleSource: patchBundleSource,
    revealWhenDressed: revealWhenDressed,
    launchDressed: launchDressed,
    groundColor: groundColor,
    todoDoc: { read: todoDocRead, write: todoDocWrite,
               readSection: docSectionRead,
               writeSection: docSectionWrite },
    renderTodoRail: renderTodoRail,
    railFields: railFields,
    todoFlushOnExit: todoFlushOnExit,
    installGoals: installGoals,
    todoState: function () {
      return { goalId: todoGoalId, tab: railTab,
               items: todoItems && todoItems.slice(),
               picked: Object.keys(todoPicked),
               saving: !!todoSaveTimer, held: todoHeld };
    },
    todoList: {
      serialize: todoSerialize,
      serializeStates: todoSerializeStates,
      copyText: todoCopyText,
      normalize: todoNormalize,
      enter: todoEnter,
      indent: todoIndent,
      outdent: todoOutdent,
      backspace: todoBackspace,
      remove: todoRemove,
      cut: todoCut,
      paste: todoPaste,
      parsePaste: todoParsePaste,
      attach: todoAttach,
      attachments: todoAttachments,
      attachmentLines: todoAttachmentLines,
      selectionText: todoSelectionText,
      family: todoFamily,
      bands: todoBandOf,
      sectioned: todoSectioned,
      sole: todoSole,
      blankAfter: todoBlankAfter,
      cancelHead: todoCancelHead,
      cancelHeads: todoCancelHeads,
      cancelIds: todoCancelIds,
      rowNode: todoRowNode,
      cost: todoCostText,
      costLabel: todoTokenLabel,
      duration: buildDuration,
      watchLine: todoWatchLine,
      renderWatch: renderTodoWatch,
      openLog: function (goalId) {
        watchOpen[goalId] = true;
        todoLoadLog(goalId);
      },
      logShown: function (goalId) { return !!watchOpen[goalId]; },
    },
    holdRoot: holdRoot,
    releaseRoot: releaseRoot,
    patchMisses: function () { return patchMisses.slice(); },
    seedPayload: seedPayload,
    mergeTrees: mergeTrees,
    acceptState: acceptState,
    refreshState: refreshState,
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
    renderChatLink: renderChatLink,
    openChatPicker: openChatPicker,
    chatStanding: chatStanding,
    goalLine: goalLine,
    renderChatSurface: renderChatSurface,
    showNotices: showNotices,
    noticesToShow: noticesToShow,
    pageTitle: pageTitle,
    applyPageTitle: applyPageTitle,
    paneTabBar: paneTabBar,
    headerNav: headerNav,
    promptAddSlot: promptAddSlot,
    openPromptPicker: openPromptPicker,
    pickPrompt: pickPrompt,
    dialogCss: function () { return DIALOG_CSS; },
    briefingSections: briefingSections,
    analysisPending: function () { return window.__hcAnalysisPending(); },
    setSetupForTest: function (value) { setupState = value; },
    setDetailForTest: function (id, value) { details[id] = value; },
    seedForTest: seed,
    loadDetailForTest: loadDetail,
    applyLaunchSkin: applyLaunchSkin,
    themeMoved: themeMoved,
    repaintCopies: repaintCopies,
    watchTheme: watchTheme,
    launchCss: function () { return LAUNCH_CSS; },
    railLayout: function () { return loadLayout(); },
    renderProjectChip: renderProjectChip,
    openProjectMenu: openProjectMenu,
    closeProjectMenu: closeProjectMenu,
    projectMenuShown: projectMenuShown,
    projectChatsOf: projectChatsOf,
    remoteHref: remoteHref,
    openOverview: openOverview,
    closeOverview: closeOverview,
    renderOverview: renderOverview,
    overviewShown: overviewShown,
    loadProjectJson: loadProjectJson,
    markdownNode: markdownNode,
    loadProjects: loadProjects,
    renderProjects: renderProjects,
    switchProject: switchProject,
    addProject: addProject,
    askProjectSetup: askProjectSetup,
    saveProjectSetup: saveProjectSetup,
    closeProjectSetup: closeProjectSetup,
    saveAbout: saveAbout,
    saveName: saveName,
    showSource: showSource,
    showPane: showPane,
    renderPane: renderPane,
    overviewPane: function () { return overviewPane; },
    runAsk: runAsk,
    askKept: function () { return askKept; },
    askSelection: {
      watch: watchAskSelection,
      renderPill: renderAskSelPill,
      pill: function () { return askSelPill; },
      open: openAskSel,
      close: closeAskSel,
      run: runAskSel,
      box: function () { return askSelBox; },
      held: function () {
        return askSelTurns.length
          ? askSelTurns[askSelTurns.length - 1] : null;
      },
      turns: function () { return askSelTurns.slice(); },
      css: function () { return ASKSEL_CSS; },
    },
    addSource: addSource,
    dropSource: dropSource,
    openAddSource: openAddSource,
    closeAddSource: closeAddSource,
    addSourceShown: addSourceShown,
    setSourceKind: setSourceKind,
    renderSourceChats: renderSourceChats,
    allChatsFor: allChatsFor,
    renderSourceBody: renderSourceBody,
    loadSyncStatus: loadSyncStatus,
    relevance: {
      render: renderRelevance,
      apply: applyRelevanceFilter,
      set: setRelevanceFilter,
      counts: relevanceCounts,
      filter: function () { return relevanceFilter; }
    },
    settingsSupabaseLoad: settingsSupabaseLoad,
    settingsSupabaseFill: settingsSupabaseFill,
    settingsSupabaseDo: settingsSupabaseDo,
    joinShared: joinShared,
    openShared: openShared,
    loadSharedList: loadSharedList,
    renderSharedList: renderSharedList,
    renderSyncStatus: renderSyncStatus,
    runSync: runSync,
    reportSharedSave: reportSharedSave,
    makeShare: makeShare,
    loadShares: loadShares,
    renderShares: renderShares,
    revokeShare: revokeShare,
    saveObjective: saveObjective,
    projectCss: function () { return PROJECT_CSS; },
    projectTheme: projectTheme,
    syncProjectTheme: syncProjectTheme,
    setRailWidth: setRailWidth,
    setRailHidden: setRailHidden,
    toggleRail: toggleRail,
    applyLayout: applyLayout,
    renderPanelToggles: renderPanelToggles,
    injectionLines: injectionLines,
    renderInjection: renderInjection,
    renderSessionChip: renderSessionChip,
    renderViewTabs: renderViewTabs,
    inheritedSources: inheritedSources,
    renderInheritedSources: renderInheritedSources,
    renderAnalyzer: renderAnalyzer,
    analyzerLine: analyzerLine,
    askSource: askSource,
    treeStep: treeStep,
    foldedIds: foldedIds,
    // TODO build alerts: the banner stack, the bell, the center, settings.
    handoff: {
      render: renderHandoff,
      copy: copyHandoff,
      fetch: fetchHandoff,
      state: function () { return handoffState; },
      last: function () { return handoffLast; },
    },
    alerts: {
      track: trackTodoAlerts,
      diff: todoAlertsFrom,
      noteOut: alertNoteOut,
      stack: alertStack,
      log: function () { return loadAlertLog().slice(); },
      unread: alertUnread,
      markRead: markAlertRead,
      markAllRead: markAllAlertsRead,
      clear: clearAlertLog,
      changedElsewhere: alertLogChangedElsewhere,
      go: alertGo,
      settings: alertSettings,
      setSettings: setAlertSettings,
      renderBell: renderBell,
      open: openAlertCenter,
      close: closeAlertCenter,
      center: function () { return alertCenterShown() ? alertCenterBox : null; },
      css: function () { return ALERT_CSS; }
    },
    // The header gear and the settings panel behind it.
    gear: {
      tab: setSettingsTab,
      render: renderGear,
      open: openSettingsPanel,
      close: closeSettingsPanel,
      toggle: toggleSettingsPanel,
      panel: function () { return settingsPanelShown() ? settingsPanelBox : null; }
    }
  };

  seed();
  // Published before the template is patched and before the artifact boots,
  // because both read it: the patched source asks it which tabs exist, and
  // the watcher below asks it which controls to take off the page.
  window.__hcScope = serverState.scope;
  // The empty document's spine, read by the patched notesVal/notesOverlay
  // getters. A goal with no notes shows the six headings rather than an
  // empty box, and the first keystroke persists the whole document.
  window.__hcDefaultDoc = DEFAULT_DOC;
  // Placed after the template island and before the closing body tag: the
  // artifact's DOMContentLoaded listener is registered but has not unpacked
  // the template yet, so patching here is safe.
  patchBundleTemplate();
  function boot() {
    // First: hold the page until it is dressed. Everything below can run
    // while it is hidden.
    revealWhenDressed();
    ensurePaneStyles();
    showStaleServer();
    // Read once. Leaving it set would make every later reload land on
    // whatever pane happened to be open when a run last finished.
    clearKeepPane();
    watchPromptAdd();
    watchChatSurface();
    watchLaunchSurface();
    watchGoals();
    watchAnalysis();
    watchSelection();
    watchAskSelection();
    watchPane();
    watchRunFeed();
    // A personal workspace reads files on the same disk; a shared one asks
    // Postgres across a network. Same poll for both would be a query every
    // second and a half for an answer that rarely changes.
    setInterval(refreshState, sharedWorkspace() ? 5000 : 1500);
    setTimeout(refreshState, 0);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
