/* hc ui bridge: seeds the Claude Design app's localStorage from goals.json
   before boot, mirrors app edits through /api/import, and adds server-backed
   human-prompt assignment without modifying the bundled app. */
(function () {
  "use strict";

  var KEY = "hc-vault-ui-v1";
  var promptState = { goals: [], prompts: [] };
  var promptStateFingerprint = null;
  var stateRevision = 0;
  var refreshPending = false;
  var panel = null;
  var panelSignature = null;
  var picker = null;
  var pickerQuery = "";
  var pickerLimit = 80;
  var promptError = "";
  var pendingLinks = Object.create(null);

  function array(value) {
    return Array.isArray(value) ? value : [];
  }

  function toNode(g, byParent, todosOf) {
    var kids = (byParent[g.id] || []).map(function (c) {
      return toNode(c, byParent, todosOf);
    });
    var tds = (todosOf[g.id] || []).map(function (t, i) {
      return {
        id: "t:" + g.id + ":" + i,
        title: t.text,
        prio: "normal",
        done: !!t.done,
        open: true,
        status: "todo",
        notes: "",
        desc: "",
        labels: [],
        children: []
      };
    });
    return {
      id: g.id,
      title: g.title,
      prio: g.priority || "normal",
      done: g.status === "completed" || g.status === "abandoned",
      open: true,
      status: g.status === "in_progress" ? "inprog" : "todo",
      notes: g.notes || "",
      desc: g.description || "",
      labels: [],
      children: tds.concat(kids)
    };
  }

  function acceptState(st) {
    if (!st || !Array.isArray(st.goals)) return false;
    var next = {
      goals: st.goals,
      // The endpoint is intentionally human-only. Keep the role check here so
      // an accidental future expansion cannot expose assistant/tool turns.
      prompts: array(st.prompts).filter(function (p) {
        return p && p.role === "user" && typeof p.id === "string" &&
          typeof p.text === "string";
      })
    };
    var fingerprint = JSON.stringify([
      next.goals.map(function (g) { return [g.id, attachedIds(g)]; }),
      next.prompts
    ]);
    if (fingerprint === promptStateFingerprint) return true;
    promptStateFingerprint = fingerprint;
    promptState = next;
    stateRevision += 1;
    panelSignature = null;
    renderPanel();
    renderPickerList(true);
    return true;
  }

  function seed() {
    try {
      var x = new XMLHttpRequest();
      x.open("GET", "/api/state", false);   // sync on purpose: must beat app boot
      x.send();
      var st = JSON.parse(x.responseText);
      if (!acceptState(st)) return;
      var byParent = {}, todosOf = {};
      st.goals.forEach(function (g) {
        todosOf[g.id] = g.todos || [];
        var p = g.parent_goal_id || null;
        (byParent[p] = byParent[p] || []).push(g);
      });
      var roots = (byParent[null] || []).map(function (g) {
        return toNode(g, byParent, todosOf);
      });
      var sel = roots.length ? roots[0].id : null;
      localStorage.setItem(KEY, JSON.stringify({
        v: 6,
        goals: roots,
        selId: sel,
        filter: "active",
        updatedAt: st.generated_at ? Date.parse(st.generated_at) : Date.now(),
        labels: [],
        paneTab: "prompt",
        themeMode: null,
        view: "split"
      }));
      window.__hcSeed = JSON.stringify(roots);
    } catch (e) {
      // Offline model missing: let the app boot with whatever it already has.
    }
  }

  function watchGoals() {
    var last = window.__hcSeed || null;
    setInterval(function () {
      var raw;
      try { raw = localStorage.getItem(KEY); } catch (e) { return; }
      if (!raw) return;
      var goals;
      try { goals = JSON.stringify(JSON.parse(raw).goals); } catch (e) { return; }
      if (goals === last) return;
      last = goals;
      fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: goals
      }).then(function () {
        refreshState();
      }).catch(function () {});
    }, 800);
  }

  function refreshState() {
    if (refreshPending) return;
    refreshPending = true;
    fetch("/api/state", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("state request failed (" + r.status + ")");
        return r.json();
      })
      .then(acceptState)
      .catch(function () {})
      .then(function () { refreshPending = false; });
  }

  function readSelection() {
    try {
      var saved = JSON.parse(localStorage.getItem(KEY) || "{}");
      return typeof saved.selId === "string" ? saved.selId : null;
    } catch (e) {
      return null;
    }
  }

  function goalById(id) {
    return promptState.goals.find(function (g) { return g.id === id; }) || null;
  }

  function promptById(id) {
    return promptState.prompts.find(function (p) { return p.id === id; }) || null;
  }

  function attachedIds(goal) {
    return array(goal && goal.prompt_ids).filter(function (id) {
      return typeof id === "string";
    });
  }

  function normalize(text) {
    var out = String(text || "").toLowerCase();
    try { out = out.normalize("NFKD").replace(/[\u0300-\u036f]/g, ""); }
    catch (e) {}
    return out.replace(/[^a-z0-9]+/g, " ").trim();
  }

  function subsequenceScore(needle, haystack) {
    var at = 0, score = 0, run = 0;
    for (var i = 0; i < haystack.length && at < needle.length; i += 1) {
      if (haystack[i] === needle[at]) {
        run += 1;
        score += 4 + run * 2;
        at += 1;
      } else {
        run = 0;
        score -= 0.08;
      }
    }
    return at === needle.length ? score : null;
  }

  // Word-aware fuzzy matching: exact phrases lead, then exact/prefix/substring
  // word matches, then ordered character subsequences. Every query token must
  // match, preventing a strong first word from hiding a missing second word.
  function fuzzyScore(query, text) {
    var q = normalize(query), t = normalize(text);
    if (!q) return 0;
    if (!t) return null;
    var phraseAt = t.indexOf(q);
    var total = phraseAt >= 0 ? 2000 - phraseAt : 0;
    var words = t.split(" ");
    var tokens = q.split(" ");
    for (var i = 0; i < tokens.length; i += 1) {
      var token = tokens[i], best = null;
      for (var j = 0; j < words.length; j += 1) {
        var word = words[j], score = null;
        if (word === token) score = 500 - j;
        else if (word.indexOf(token) === 0) score = 400 - j - (word.length - token.length);
        else if (word.indexOf(token) >= 0) score = 300 - j - word.indexOf(token);
        else {
          var seq = subsequenceScore(token, word);
          if (seq !== null) score = 100 + seq - j;
        }
        if (score !== null && (best === null || score > best)) best = score;
      }
      if (best === null) return null;
      total += best;
    }
    return total;
  }

  function recency(prompt, index) {
    var ordinal = Number(prompt.ordinal);
    if (Number.isFinite(ordinal)) return ordinal;
    var stamp = Date.parse(prompt.created_at || "");
    return Number.isFinite(stamp) ? stamp : index;
  }

  function rankedPrompts(query) {
    return promptState.prompts.map(function (prompt, index) {
      return {
        prompt: prompt,
        score: fuzzyScore(query, prompt.text),
        recent: recency(prompt, index),
        index: index
      };
    }).filter(function (row) {
      return row.score !== null;
    }).sort(function (a, b) {
      return b.recent - a.recent || b.index - a.index;
    }).map(function (row) { return row.prompt; });
  }

  function findText(text) {
    var els = document.querySelectorAll("span");
    for (var i = 0; i < els.length; i += 1) {
      if (els[i].textContent.trim() === text) return els[i];
    }
    return null;
  }

  function ensureStyles() {
    if (document.getElementById("hc-prompt-style")) return;
    var style = document.createElement("style");
    style.id = "hc-prompt-style";
    style.textContent = [
      ".hc-pa{margin-top:13px;padding-top:12px;border-top:1px solid var(--bd)}",
      ".hc-pa-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px}",
      ".hc-pa-label{font:600 9.5px 'Source Code Pro',monospace;letter-spacing:1px;color:var(--fnt)}",
      ".hc-pa-add,.hc-pa-remove,.hc-pa-close{border:0;background:transparent;font-family:'Source Code Pro',monospace;color:var(--acc);cursor:pointer;padding:0}",
      ".hc-pa-add{font-size:11px}.hc-pa-add:hover,.hc-pa-remove:hover{text-decoration:underline}",
      ".hc-pa-list{display:flex;flex-direction:column;gap:6px;margin-top:8px}",
      ".hc-pa-card{display:flex;gap:8px;align-items:flex-start;border-left:2px solid var(--bd2);padding:5px 0 5px 8px;min-width:0}",
      ".hc-pa-text{flex:1;min-width:0;font:11px/1.45 'Source Code Pro',monospace;color:var(--mut);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;white-space:pre-wrap}",
      ".hc-pa-remove{flex:none;color:var(--fnt);font-size:13px;line-height:1.2}.hc-pa-remove:hover{color:var(--del)}",
      ".hc-pa-empty,.hc-pa-error{margin-top:7px;font:10.5px/1.45 'Source Code Pro',monospace;color:var(--fnt)}",
      ".hc-pa-error{color:var(--del)}",
      ".hc-pa-overlay{position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.28);display:flex;align-items:center;justify-content:center;padding:20px}",
      ".hc-pa-dialog{width:min(620px,100%);max-height:min(680px,calc(100vh - 40px));display:flex;flex-direction:column;background:var(--panel,#fff);color:var(--ink,#111);border:1px solid var(--bd2,#d5d5d5);border-radius:3px;box-shadow:0 18px 60px rgba(0,0,0,.2);font-family:'Source Code Pro',monospace}",
      ".hc-pa-dialog-head{display:flex;align-items:baseline;justify-content:space-between;padding:14px 16px 10px}",
      ".hc-pa-dialog-title{font-size:12.5px;font-weight:700}",
      ".hc-pa-close{font-size:16px;color:var(--fnt)}.hc-pa-close:hover{color:var(--ink)}",
      ".hc-pa-search{margin:0 16px 10px;width:calc(100% - 32px);box-sizing:border-box;border:1px solid var(--bd2);border-radius:2px;background:var(--panel2);color:var(--ink);outline:none;padding:8px 10px;font:11.5px 'Source Code Pro',monospace}",
      ".hc-pa-search:focus{border-color:var(--acc);box-shadow:0 0 0 2px var(--acchov)}",
      ".hc-pa-results{overflow-y:auto;overscroll-behavior:contain;border-top:1px solid var(--bd);border-bottom:1px solid var(--bd);max-height:52vh}",
      ".hc-pa-result{width:100%;display:flex;align-items:flex-start;gap:10px;text-align:left;border:0;border-bottom:1px solid var(--bd);background:transparent;color:var(--ink);padding:10px 16px;cursor:pointer;font-family:'Source Code Pro',monospace}",
      ".hc-pa-result:last-child{border-bottom:0}.hc-pa-result:hover{background:var(--acchov)}",
      ".hc-pa-check{width:13px;height:13px;flex:none;margin-top:2px;border:1.5px solid var(--fnt);border-radius:2px;color:var(--onacc);font:9px/11px 'Source Code Pro',monospace;text-align:center}",
      ".hc-pa-result[data-attached='true'] .hc-pa-check{background:var(--acc);border-color:var(--acc)}",
      ".hc-pa-result-body{display:block;min-width:0;flex:1}.hc-pa-result-meta{display:block;font-size:9.5px;color:var(--fnt);margin-bottom:4px}",
      ".hc-pa-result-text{display:block;font-size:11.5px;line-height:1.45;color:var(--mut);white-space:pre-wrap;word-break:break-word}",
      ".hc-pa-result[disabled]{cursor:wait;opacity:.6}",
      ".hc-pa-foot{padding:8px 16px;font-size:10px;color:var(--fnt)}",
      "@media(max-width:700px){.hc-pa-overlay{padding:8px}.hc-pa-dialog{max-height:calc(100vh - 16px)}}"
    ].join("");
    document.head.appendChild(style);
  }

  function makeButton(className, label, title) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = className;
    button.textContent = label;
    if (title) button.title = title;
    return button;
  }

  function ensurePanel() {
    var selected = readSelection();
    var goal = goalById(selected);
    var promptTab = findText("PROMPT");
    var tabs = promptTab && promptTab.parentElement;
    var inspector = tabs && tabs.parentElement;
    if (!goal || !tabs || !inspector || !inspector.contains(promptTab)) {
      if (panel) panel.style.display = "none";
      if (picker) closePicker();
      return null;
    }
    ensureStyles();
    // The bundle hydrates in phases and may clone an inspector after the
    // first bridge mount. Adopt one surviving panel and remove cloned copies.
    var existing = inspector.querySelectorAll("#hc-prompt-links");
    if (!panel || !document.documentElement.contains(panel) || !inspector.contains(panel)) {
      panel = existing.length ? existing[existing.length - 1] : null;
      panelSignature = null;
    }
    for (var i = 0; i < existing.length; i += 1) {
      if (existing[i] !== panel) existing[i].remove();
    }
    if (!panel || !document.documentElement.contains(panel)) {
      panel = document.createElement("section");
      panel.id = "hc-prompt-links";
      panel.className = "hc-pa";
      tabs.parentNode.insertBefore(panel, tabs);
      panelSignature = null;
    } else if (panel.parentNode !== tabs.parentNode || panel.nextSibling !== tabs) {
      tabs.parentNode.insertBefore(panel, tabs);
    }
    panel.style.display = "block";
    return goal;
  }

  function formatPromptMeta(prompt) {
    var parts = [];
    if (Number.isFinite(Number(prompt.ordinal))) parts.push("turn " + Number(prompt.ordinal));
    var date = new Date(prompt.created_at || "");
    if (!Number.isNaN(date.getTime())) {
      parts.push(date.toLocaleDateString("en-US", { month: "short", day: "numeric" }) +
        ", " + date.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }));
    }
    return parts.join(" · ") || "human prompt";
  }

  function renderPanel() {
    var goal = ensurePanel();
    if (!goal || !panel) return;
    var ids = attachedIds(goal);
    var sig = goal.id + "|" + stateRevision + "|" + ids.join(",") + "|" +
      promptError + "|" + Object.keys(pendingLinks).join(",");
    if (sig === panelSignature) return;
    panelSignature = sig;
    panel.textContent = "";

    var head = document.createElement("div");
    head.className = "hc-pa-head";
    var label = document.createElement("span");
    label.className = "hc-pa-label";
    label.textContent = "CHAT PROMPTS" + (ids.length ? " (" + ids.length + ")" : "");
    var add = makeButton("hc-pa-add", "+ attach from chat");
    add.onclick = openPicker;
    head.appendChild(label);
    head.appendChild(add);
    panel.appendChild(head);

    var list = document.createElement("div");
    list.className = "hc-pa-list";
    ids.forEach(function (id) {
      var prompt = promptById(id);
      if (!prompt) return;
      var card = document.createElement("div");
      card.className = "hc-pa-card";
      card.title = formatPromptMeta(prompt);
      var text = document.createElement("span");
      text.className = "hc-pa-text";
      text.textContent = prompt.text;
      var remove = makeButton("hc-pa-remove", "×", "Detach this prompt");
      remove.disabled = !!pendingLinks[goal.id + "\u0000" + id];
      remove.onclick = function () { setPromptLink(goal.id, id, false); };
      card.appendChild(text);
      card.appendChild(remove);
      list.appendChild(card);
    });
    if (!list.childNodes.length) {
      var empty = document.createElement("div");
      empty.className = "hc-pa-empty";
      empty.textContent = promptState.prompts.length ?
        "No chat prompts attached to this goal." : "No human prompts have streamed into this chat yet.";
      list.appendChild(empty);
    }
    panel.appendChild(list);
    if (promptError) {
      var error = document.createElement("div");
      error.className = "hc-pa-error";
      error.setAttribute("role", "alert");
      error.textContent = promptError;
      panel.appendChild(error);
    }
  }

  function createPicker() {
    if (picker && document.documentElement.contains(picker)) return picker;
    picker = document.createElement("div");
    picker.id = "hc-prompt-picker";
    picker.className = "hc-pa-overlay";
    picker.hidden = true;
    picker.onclick = function (e) { if (e.target === picker) closePicker(); };

    var dialog = document.createElement("div");
    dialog.className = "hc-pa-dialog";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-labelledby", "hc-pa-picker-title");
    var head = document.createElement("div");
    head.className = "hc-pa-dialog-head";
    var title = document.createElement("span");
    title.id = "hc-pa-picker-title";
    title.className = "hc-pa-dialog-title";
    title.textContent = "Attach chat prompts";
    var close = makeButton("hc-pa-close", "×", "Close prompt picker");
    close.onclick = closePicker;
    head.appendChild(title);
    head.appendChild(close);

    var search = document.createElement("input");
    search.type = "search";
    search.className = "hc-pa-search";
    search.placeholder = "Search human messages…";
    search.autocomplete = "off";
    search.setAttribute("aria-label", "Search human messages");
    search.oninput = function () {
      pickerQuery = search.value;
      pickerLimit = 80;
      renderPickerList();
    };
    var results = document.createElement("div");
    results.className = "hc-pa-results";
    results.setAttribute("role", "listbox");
    results.onscroll = function () {
      if (results.scrollTop + results.clientHeight >= results.scrollHeight - 80) {
        var total = rankedPrompts(pickerQuery).length;
        if (pickerLimit < total) {
          pickerLimit += 80;
          renderPickerList(true);
        }
      }
    };
    var foot = document.createElement("div");
    foot.className = "hc-pa-foot";
    dialog.appendChild(head);
    dialog.appendChild(search);
    dialog.appendChild(results);
    dialog.appendChild(foot);
    picker.appendChild(dialog);
    // Keep the modal inside the app root so its light/dark CSS variables are
    // inherited. Fixed positioning still makes it viewport-relative.
    (document.querySelector(".hc") || document.body).appendChild(picker);
    return picker;
  }

  function openPicker() {
    var goal = goalById(readSelection());
    if (!goal) return;
    ensureStyles();
    createPicker();
    promptError = "";
    pickerQuery = "";
    pickerLimit = 80;
    picker.hidden = false;
    var search = picker.querySelector(".hc-pa-search");
    search.value = "";
    renderPickerList();
    setTimeout(function () { search.focus(); }, 0);
  }

  function closePicker() {
    if (picker) picker.hidden = true;
  }

  function renderPickerList(keepScroll) {
    if (!picker || picker.hidden) return;
    var goal = goalById(readSelection());
    if (!goal) { closePicker(); return; }
    var results = picker.querySelector(".hc-pa-results");
    var foot = picker.querySelector(".hc-pa-foot");
    if (!results || !foot) return;
    var oldTop = keepScroll ? results.scrollTop : 0;
    var attached = new Set(attachedIds(goal));
    var ranked = rankedPrompts(pickerQuery);
    var shown = ranked.slice(0, pickerLimit);
    results.textContent = "";
    shown.forEach(function (prompt) {
      var linked = attached.has(prompt.id);
      var key = goal.id + "\u0000" + prompt.id;
      var row = makeButton("hc-pa-result", "");
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", linked ? "true" : "false");
      row.dataset.attached = linked ? "true" : "false";
      row.disabled = !!pendingLinks[key];
      var check = document.createElement("span");
      check.className = "hc-pa-check";
      check.textContent = linked ? "✓" : "";
      var body = document.createElement("span");
      body.className = "hc-pa-result-body";
      var meta = document.createElement("span");
      meta.className = "hc-pa-result-meta";
      meta.textContent = formatPromptMeta(prompt);
      var text = document.createElement("span");
      text.className = "hc-pa-result-text";
      text.textContent = prompt.text;
      body.appendChild(meta);
      body.appendChild(text);
      row.appendChild(check);
      row.appendChild(body);
      row.onclick = function () { setPromptLink(goal.id, prompt.id, !linked); };
      results.appendChild(row);
    });
    if (!shown.length) {
      var empty = document.createElement("div");
      empty.className = "hc-pa-empty";
      empty.style.padding = "18px 16px";
      empty.textContent = promptState.prompts.length ?
        "No human message matches that search." : "No human prompts have streamed into this chat yet.";
      results.appendChild(empty);
    }
    foot.textContent = shown.length < ranked.length ?
      "showing " + shown.length + " of " + ranked.length + " · scroll for older prompts" :
      ranked.length + (ranked.length === 1 ? " human prompt" : " human prompts") + " · newest first";
    if (keepScroll) results.scrollTop = oldTop;
  }

  function setPromptLink(goalId, promptId, attach) {
    var goal = goalById(goalId);
    if (!goal || !promptById(promptId)) return;
    var key = goalId + "\u0000" + promptId;
    if (pendingLinks[key]) return;
    var before = attachedIds(goal);
    var has = before.indexOf(promptId) >= 0;
    if (has === attach) return;
    pendingLinks[key] = true;
    promptError = "";
    goal.prompt_ids = attach ? before.concat([promptId]) : before.filter(function (id) {
      return id !== promptId;
    });
    stateRevision += 1;
    panelSignature = null;
    renderPanel();
    renderPickerList();

    fetch("/api/op", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        op: attach ? "attach_prompt" : "detach_prompt",
        goal_id: goalId,
        prompt_id: promptId
      })
    }).then(function (r) {
      if (!r.ok) throw new Error("request failed (" + r.status + ")");
      return r.json();
    }).then(function (result) {
      if (!result || result.ok !== true) {
        throw new Error(result && result.error ? result.error : "link was rejected");
      }
      delete pendingLinks[key];
      stateRevision += 1;
      panelSignature = null;
      renderPanel();
      renderPickerList();
      refreshState();
    }).catch(function (error) {
      goal.prompt_ids = before;
      delete pendingLinks[key];
      promptError = "Could not " + (attach ? "attach" : "detach") + " prompt: " + error.message;
      stateRevision += 1;
      panelSignature = null;
      renderPanel();
      renderPickerList();
    });
  }

  function mountPromptUI() {
    renderPanel();
    if (picker && !picker.hidden && !document.documentElement.contains(picker)) {
      picker = null;
      closePicker();
    }
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && picker && !picker.hidden) {
      e.preventDefault();
      closePicker();
    }
  }, true);

  // Test seam for the pure ordering/matching behavior; no backend state is
  // exposed, and production code does not depend on this object.
  window.__hcPromptUI = {
    fuzzyScore: fuzzyScore,
    normalize: normalize,
    rankedPrompts: rankedPrompts,
    acceptState: acceptState,
    selectedGoalId: readSelection
  };

  seed();
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      watchGoals();
      setInterval(refreshState, 1500);
      setInterval(mountPromptUI, 250);
      setTimeout(refreshState, 0);
    });
  } else {
    watchGoals();
    setInterval(refreshState, 1500);
    setInterval(mountPromptUI, 250);
    setTimeout(refreshState, 0);
  }
})();
