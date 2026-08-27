/* Setup: what opens after `npx engelbart-cli`, before there is anything.
 *
 * A reader who has just installed has no chat, no project and nothing
 * written down. The workspace's own onboarding cannot help them -- it asks
 * which project a chat is for, and they have neither. So this page asks the
 * one question that comes first (is this new work, or work you already
 * have?) and then either takes them through setting a project up, or tells
 * them the two commands that bring an existing one in.
 *
 * The conversation lives here, in the browser, and is posted whole on every
 * round. Nothing is written into the vault until the reader presses the
 * button at the end: until then setup is a page they can close.
 *
 * The JSON the model replies with is never shown. What it names -- a set of
 * questions, a plan, the goals, the rows -- is what gets drawn.
 */
(function () {
  "use strict";

  var app = document.getElementById("app");
  var API = "/api/op";

  // --- state ---------------------------------------------------------------
  //
  // `screen` is the only thing that decides what is drawn: fork (the first
  // question), resume (the two commands), talk (the conversation), done.

  var st = {
    screen: "fork",
    msgs: [],
    card: null,          // the last card the model named
    answers: {},         // per question id, for the card on screen
    thinking: false,
    draft: "",
    error: "",
    plan: null,          // the plan they approved
    goals: null,         // the goals they were offered
    chosen: "",          // the one they picked
    other: "",           // ...or the one they typed
    todos: [],           // rows, editable
    newTodo: "",
    name: "",            // the project's name, typed while the rest arrives
    made: null           // what commit gave back
  };

  var OPEN = "Tell me what you're working on in your own words."
    + " I'll ask a few questions, then write up a plan for you to approve.";

  function dark() {
    try {
      return window.matchMedia
        && window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch (e) { return false; }
  }

  // --- little DOM helpers ---------------------------------------------------

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function on(node, event, fn) { node.addEventListener(event, fn); return node; }

  function btn(label, cls, fn, disabled) {
    var b = el("button", "btn " + (cls || ""), label);
    if (disabled) b.setAttribute("disabled", "disabled");
    else on(b, "click", fn);
    return b;
  }

  function str(value) { return value == null ? "" : String(value); }

  function post(body) {
    return fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json(); })
      .catch(function () {
        return { ok: false, error: "the workspace stopped answering" };
      });
  }

  // --- the conversation -----------------------------------------------------

  function say(role, text) {
    if (!str(text)) return;
    st.msgs.push({ role: role, text: str(text) });
  }

  function round(extra) {
    // One turn: the whole transcript out, one card back. The card the model
    // named replaces whatever was on screen -- a question set that has been
    // answered is not a thing to go back to.
    st.thinking = true;
    st.error = "";
    st.card = null;
    st.answers = {};
    draw();
    post({ op: "setup_say", transcript: st.msgs.concat(extra || []) })
      .then(function (out) {
        st.thinking = false;
        if (!out || !out.ok) {
          st.error = (out && out.error) || "setup could not reach Claude";
          draw();
          return;
        }
        say("engelbart", out.say);
        st.card = out;
        if (out.card === "plan") st.plan = out.plan;
        if (out.card === "goals") st.goals = out.goals;
        if (out.card === "todos") {
          st.todos = (out.todos || []).map(function (t, i) {
            return { id: "t" + i + "-" + Math.random().toString(36).slice(2, 7),
                     text: t };
          });
        }
        draw();
      });
  }

  function send() {
    var text = st.draft.trim();
    if (!text || st.thinking) return;
    say("you", text);
    st.draft = "";
    round();
  }

  // --- what the reader picked, as their turn --------------------------------

  function answersAsSaid() {
    var items = (st.card && st.card.questions && st.card.questions.items) || [];
    var said = [];
    items.forEach(function (q) {
      var got = st.answers[q.id];
      if (Array.isArray(got)) got = got.join(" · ");
      got = str(got).trim();
      if (got) said.push(q.title + ": " + got);
    });
    return said.join("\n");
  }

  function submitAnswers() {
    var said = answersAsSaid();
    if (!said) return;
    say("you", said);
    round();
  }

  function skipAnswers() {
    say("you", "skip -- decide for me");
    round();
  }

  // --- drawing --------------------------------------------------------------

  function draw() {
    app.setAttribute("data-dark", dark() ? "true" : "false");
    app.textContent = "";
    if (st.screen === "fork") return drawFork();
    if (st.screen === "resume") return drawResume();
    if (st.screen === "done") return drawDone();
    drawTalk();
  }

  function column(parent) {
    var wrap = el("div", "wrap");
    var col = el("div", "col");
    wrap.appendChild(col);
    parent.appendChild(wrap);
    return col;
  }

  function hero(col, note) {
    var box = el("div", "hero rise");
    box.appendChild(el("div", "hero-name", "Engelbart"));
    if (note) box.appendChild(el("div", "hero-note", note));
    col.appendChild(box);
  }

  // Screen 1: the question that comes before everything. No model call, no
  // spinner -- they have just installed and have not asked for anything yet.
  function drawFork() {
    var col = column(app);
    hero(col, "Installed. What are we opening?");
    var card = el("div", "card rise");
    var body = el("div", "card-body");
    body.appendChild(el("div", "card-title", "Is this new work, or work you already have?"));
    var acts = el("div", "acts");
    acts.appendChild(btn("Start a new project", "btn-on", function () {
      st.screen = "talk";
      say("engelbart", OPEN);
      draw();
    }));
    acts.appendChild(btn("Resume an existing one", "btn-quiet", function () {
      st.screen = "resume";
      draw();
    }));
    body.appendChild(acts);
    card.appendChild(body);
    col.appendChild(card);
  }

  // Screen 2: nothing to set up -- their project is already in a chat, and
  // the two commands are the whole of what they have to do. Copyable,
  // because a command someone retypes is a command someone mistypes.
  function drawResume() {
    var col = column(app);
    hero(col, "Then it is already yours -- two commands, in your terminal.");
    var card = el("div", "card rise");
    var head = el("div", "card-head");
    head.appendChild(el("span", "lbl", "resume a project"));
    card.appendChild(head);
    var body = el("div", "card-body");

    step(body, "1", "Open the chat you were working in.", "claude -r");
    var pick = el("div", "step");
    pick.appendChild(el("div", "step-n", "2"));
    var pb = el("div", "step-b");
    pb.appendChild(el("div", "step-t",
      "Pick that chat from the list Claude Code shows you."));
    pick.appendChild(pb);
    body.appendChild(pick);
    step(body, "3", "Open its workspace.", "/bart");

    body.appendChild(el("div", "hint",
      "Engelbart reads what is already in that chat, so its goals are"
      + " written from work you have already done."));
    var acts = el("div", "acts");
    acts.appendChild(btn("Back", "btn-quiet", function () {
      st.screen = "fork";
      draw();
    }));
    body.appendChild(acts);
    card.appendChild(body);
    col.appendChild(card);
  }

  function step(parent, n, text, command) {
    var row = el("div", "step");
    row.appendChild(el("div", "step-n", n));
    var body = el("div", "step-b");
    body.appendChild(el("div", "step-t", text));
    if (command) body.appendChild(commandRow(command));
    row.appendChild(body);
    parent.appendChild(row);
  }

  function commandRow(command) {
    var row = el("div", "cmd");
    row.appendChild(el("span", "cmd-text", command));
    var copy = el("button", "cmd-copy", "copy");
    on(copy, "click", function () {
      var done = function () {
        copy.textContent = "copied";
        setTimeout(function () { copy.textContent = "copy"; }, 1400);
      };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(command).then(done, fallback);
        } else fallback();
      } catch (e) { fallback(); }
      function fallback() {
        // A clipboard the browser will not hand over is not a dead end:
        // the text is selectable, and saying so beats a button that lies.
        var probe = document.createElement("textarea");
        probe.value = command;
        document.body.appendChild(probe);
        probe.select();
        try { document.execCommand("copy"); done(); }
        catch (e) { copy.textContent = "select it"; }
        document.body.removeChild(probe);
      }
    });
    row.appendChild(copy);
    return row;
  }

  // Screen 3: the conversation.
  function drawTalk() {
    var col = column(app);
    if (st.msgs.length <= 1 && !st.card) hero(col, "");

    st.msgs.forEach(function (m) {
      var box = el("div", "msg rise " + (m.role === "you" ? "msg-you" : "msg-them"));
      box.appendChild(el("div", "lbl", m.role === "you" ? "you" : "engelbart"));
      box.appendChild(el("div", "msg-body", m.text));
      col.appendChild(box);
    });

    if (st.thinking) {
      var think = el("div", "think rise");
      think.appendChild(el("span", "spin"));
      think.appendChild(el("span", "", "reading what you wrote…"));
      col.appendChild(think);
    }

    if (st.error) col.appendChild(el("div", "err", st.error));

    var kind = st.card && st.card.card;
    if (!st.thinking && kind === "questions") drawQuestions(col);
    if (!st.thinking && kind === "plan") drawPlan(col);
    if (!st.thinking && kind === "goals") drawGoals(col);
    if (!st.thinking && kind === "todos") drawTodos(col);

    drawComposer(app);
  }

  function cardBox(col, eyebrow, right) {
    var card = el("div", "card rise");
    var head = el("div", "card-head");
    head.appendChild(el("span", "lbl", eyebrow));
    if (right) {
      var r = el("span", "lbl", right);
      r.style.marginLeft = "auto";
      head.appendChild(r);
    }
    card.appendChild(head);
    var body = el("div", "card-body");
    card.appendChild(body);
    col.appendChild(card);
    return body;
  }

  // --- the question modal, in its four shapes -------------------------------

  function drawQuestions(col) {
    var set = st.card.questions;
    var items = set.items || [];
    var body = cardBox(col, set.eyebrow || "a few questions",
                       items.length + (items.length === 1 ? " question"
                                                          : " questions"));
    items.forEach(function (q) { body.appendChild(questionNode(q)); });

    var acts = el("div", "acts");
    var ready = items.some(function (q) {
      var got = st.answers[q.id];
      return Array.isArray(got) ? got.length : str(got).trim();
    });
    acts.appendChild(btn("Send answers", ready ? "btn-on" : "",
                         submitAnswers, !ready));
    acts.appendChild(btn("Skip", "btn-quiet", skipAnswers));
    body.appendChild(acts);
  }

  function questionNode(q) {
    var box = el("div", "q");
    box.appendChild(el("div", "card-title", q.title));
    if (q.subtitle) box.appendChild(el("div", "card-sub", q.subtitle));
    if (q.type === "mcq" || q.type === "select_all") {
      box.appendChild(optionList(q));
    } else {
      box.appendChild(writtenField(q));
    }
    return box;
  }

  function optionList(q) {
    var many = q.type === "select_all";
    var list = el("div", "");
    (q.options || []).forEach(function (o) {
      var picked = many
        ? (st.answers[q.id] || []).indexOf(o.label) >= 0
        : st.answers[q.id] === o.label;
      var row = el("div", "opt");
      row.setAttribute("data-on", picked ? "1" : "0");
      var mark = el("span", "mark " + (many ? "mark-many" : "mark-one"),
                    picked && many ? "✓" : "");
      row.appendChild(mark);
      var text = el("span", "opt-text");
      text.appendChild(el("span", "opt-label", o.label));
      // The argument for a choice, where the model is proposing rather than
      // asking. Absent on an ordinary question, and the row reads the same.
      if (o.why) text.appendChild(el("span", "opt-why", o.why));
      row.appendChild(text);
      on(row, "click", function () {
        if (many) {
          var held = (st.answers[q.id] || []).slice();
          var at = held.indexOf(o.label);
          if (at >= 0) held.splice(at, 1); else held.push(o.label);
          st.answers[q.id] = held;
        } else {
          st.answers[q.id] = st.answers[q.id] === o.label ? "" : o.label;
        }
        draw();
      });
      list.appendChild(row);
    });
    return list;
  }

  function writtenField(q) {
    var wrap = el("div", "field");
    var long = q.type === "open";
    var input = el(long ? "textarea" : "input", "f");
    if (long) input.setAttribute("rows", "3");
    else input.setAttribute("type", "text");
    input.setAttribute("spellcheck", "false");
    if (q.placeholder) input.setAttribute("placeholder", q.placeholder);
    input.value = str(st.answers[q.id]);
    // Held without redrawing: a sweep on every keystroke would take the
    // caret out of the box the reader is typing in.
    on(input, "input", function () { st.answers[q.id] = input.value; });
    on(input, "blur", draw);
    wrap.appendChild(input);
    return wrap;
  }

  // --- plan -----------------------------------------------------------------

  function drawPlan(col) {
    var plan = st.card.plan || {};
    var body = cardBox(col, "plan");
    if (plan.head) body.appendChild(el("div", "card-title", plan.head));
    var rows = el("div", "");
    rows.style.marginTop = "10px";
    (plan.lines || []).forEach(function (line) {
      var row = el("div", "plan-row");
      row.appendChild(el("span", "lbl plan-k", line.k));
      row.appendChild(el("span", "plan-v", line.v));
      rows.appendChild(row);
    });
    body.appendChild(rows);

    var acts = el("div", "acts");
    acts.appendChild(btn("Approve", "btn-on", function () {
      st.plan = plan;
      say("you", "Approved.");
      round();
    }));
    acts.appendChild(btn("Change something", "btn-quiet", function () {
      st.draft = "";
      var field = document.querySelector(".composer .f");
      if (field) field.focus();
    }));
    body.appendChild(acts);
    body.appendChild(el("div", "hint",
      "Nothing is saved yet. Say what to change in the box below and it"
      + " will write it again."));
  }

  // --- goals ----------------------------------------------------------------

  function drawGoals(col) {
    var goals = st.card.goals || [];
    var body = cardBox(col, "start with");
    goals.forEach(function (g) {
      var picked = st.chosen === g.label;
      var row = el("div", "opt");
      row.setAttribute("data-on", picked ? "1" : "0");
      row.appendChild(el("span", "mark mark-one", ""));
      var text = el("span", "opt-text");
      text.appendChild(el("span", "opt-label", g.label));
      if (g.why) text.appendChild(el("span", "opt-why", g.why));
      row.appendChild(text);
      on(row, "click", function () {
        st.chosen = picked ? "" : g.label;
        draw();
      });
      body.appendChild(row);
    });

    // Their own, always on offer: the model proposed, it did not decide.
    var mine = el("div", "opt");
    var typing = st.chosen === " other";
    mine.setAttribute("data-on", typing ? "1" : "0");
    mine.appendChild(el("span", "mark mark-one", ""));
    var mineText = el("span", "opt-text");
    mineText.appendChild(el("span", "opt-label", "Something else"));
    mineText.appendChild(el("span", "opt-why",
      "tell it what to start on instead and it will use that"));
    mine.appendChild(mineText);
    on(mine, "click", function () {
      st.chosen = typing ? "" : " other";
      draw();
    });
    body.appendChild(mine);

    if (typing) {
      var wrap = el("div", "field");
      var input = el("input", "f");
      input.setAttribute("type", "text");
      input.setAttribute("spellcheck", "false");
      input.setAttribute("placeholder", "what to start on");
      input.value = st.other;
      on(input, "input", function () { st.other = input.value; });
      wrap.appendChild(input);
      body.appendChild(wrap);
    }

    var label = typing ? st.other.trim() : st.chosen;
    var acts = el("div", "acts");
    acts.appendChild(btn("Break into TODOs", label ? "btn-on" : "", function () {
      st.goals = goals;
      say("you", label);
      round();
    }, !label));
    body.appendChild(acts);
  }

  // --- todos, and the name ---------------------------------------------------

  function drawTodos(col) {
    var body = cardBox(col, "todos",
                       st.todos.length + (st.todos.length === 1 ? " row"
                                                                : " rows"));
    st.todos.forEach(function (t) {
      var row = el("div", "row");
      row.appendChild(el("span", "dash", "—"));
      var input = el("input", "f");
      input.setAttribute("type", "text");
      input.setAttribute("spellcheck", "false");
      input.value = t.text;
      on(input, "input", function () { t.text = input.value; });
      row.appendChild(input);
      var x = el("button", "x", "×");
      on(x, "click", function () {
        st.todos = st.todos.filter(function (r) { return r !== t; });
        draw();
      });
      row.appendChild(x);
      body.appendChild(row);
    });

    var add = el("div", "row");
    add.appendChild(el("span", "dash", "+"));
    var fresh = el("input", "f");
    fresh.setAttribute("type", "text");
    fresh.setAttribute("spellcheck", "false");
    fresh.setAttribute("placeholder", "add a row");
    fresh.value = st.newTodo;
    on(fresh, "input", function () { st.newTodo = fresh.value; });
    on(fresh, "keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      var text = fresh.value.trim();
      if (!text) return;
      st.todos.push({ id: "t" + Math.random().toString(36).slice(2, 8),
                      text: text });
      st.newTodo = "";
      draw();
    });
    add.appendChild(fresh);
    body.appendChild(add);

    // The name, asked for here rather than at the start: by now they have
    // seen what the project is, so naming it is recognition rather than
    // invention -- and it is the one thing left to do while they read.
    var nameWrap = el("div", "name-field");
    nameWrap.appendChild(el("div", "lbl", "call it"));
    var field = el("div", "field");
    var name = el("input", "f");
    name.setAttribute("type", "text");
    name.setAttribute("spellcheck", "false");
    name.setAttribute("placeholder", "a name for this project");
    name.value = st.name;
    on(name, "input", function () {
      st.name = name.value;
      var go = document.querySelector("[data-hc-complete]");
      if (!go) return;
      // Toggled in place rather than by redrawing: the reader is typing in
      // the field that decides it.
      if (st.name.trim()) {
        go.removeAttribute("disabled");
        go.className = "btn btn-on";
      } else {
        go.setAttribute("disabled", "disabled");
        go.className = "btn";
      }
    });
    field.appendChild(name);
    nameWrap.appendChild(field);
    body.appendChild(nameWrap);

    var acts = el("div", "acts");
    var go = btn("Create project", st.name.trim() ? "btn-on" : "",
                 complete, !st.name.trim());
    go.setAttribute("data-hc-complete", "");
    if (!st.name.trim()) on(go, "click", complete);
    acts.appendChild(go);
    body.appendChild(acts);
    body.appendChild(el("div", "hint",
      "This makes the project and its goals. It is not attached to any"
      + " chat yet — the next screen says how to open it."));
  }

  function complete() {
    if (!st.name.trim() || st.thinking) return;
    st.thinking = true;
    st.error = "";
    draw();
    post({ op: "setup_commit", name: st.name,
           plan: st.plan, goals: st.goals,
           chosen: st.chosen === " other" ? st.other : st.chosen,
           todos: st.todos.map(function (t) { return t.text; }) })
      .then(function (out) {
        st.thinking = false;
        if (!out || !out.ok) {
          st.error = (out && out.error) || "the project could not be made";
          draw();
          return;
        }
        st.made = out;
        st.screen = "done";
        draw();
      });
  }

  // Screen 4: it exists, and is attached to nothing. What is left is the
  // same two commands the resume path gives -- because opening a project is
  // opening a project, however it came to be.
  function drawDone() {
    var col = column(app);
    hero(col, "“" + str(st.made.name) + "” is set up.");
    var card = el("div", "card rise");
    var head = el("div", "card-head");
    head.appendChild(el("span", "lbl", "what is there"));
    card.appendChild(head);
    var body = el("div", "card-body");
    body.appendChild(el("div", "card-title",
      st.made.goals + (st.made.goals === 1 ? " goal" : " goals")
      + ", with your rows under the one you chose."));
    body.appendChild(el("div", "card-sub",
      "It belongs to no chat yet. Open a chat for it and its goals are"
      + " there waiting."));
    body.appendChild(el("div", "step-t", str(st.made.cwd)));
    var acts = el("div", "acts");
    acts.appendChild(btn("Set up another", "btn-quiet", function () {
      st.screen = "fork";
      st.msgs = [];
      st.card = null;
      st.plan = null;
      st.goals = null;
      st.chosen = "";
      st.other = "";
      st.todos = [];
      st.name = "";
      st.made = null;
      draw();
    }));
    body.appendChild(acts);
    card.appendChild(body);
    col.appendChild(card);

    var next = el("div", "card rise");
    var nhead = el("div", "card-head");
    nhead.appendChild(el("span", "lbl", "open it"));
    next.appendChild(nhead);
    var nbody = el("div", "card-body");
    step(nbody, "1", "Start a chat in your terminal.", "claude");
    step(nbody, "2", "Open its workspace and pick this project.", "/bart");
    next.appendChild(nbody);
    col.appendChild(next);
  }

  // --- the composer ---------------------------------------------------------

  function drawComposer(parent) {
    var foot = el("div", "foot");
    var col = el("div", "col");
    var box = el("div", "composer");
    var field = el("textarea", "f");
    field.setAttribute("rows", "1");
    field.setAttribute("spellcheck", "false");
    field.setAttribute("placeholder", st.msgs.length <= 1
      ? "describe it however it comes out…"
      : "or just talk — the card above still works");
    field.value = st.draft;
    on(field, "input", function () {
      st.draft = field.value;
      field.style.height = "auto";
      field.style.height = Math.min(field.scrollHeight, 160) + "px";
      var button = box.querySelector(".send");
      if (!button) return;
      if (st.draft.trim() && !st.thinking) {
        button.removeAttribute("disabled");
        button.className = "send send-on";
      } else {
        button.setAttribute("disabled", "disabled");
        button.className = "send";
      }
    });
    on(field, "keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    box.appendChild(field);

    var ready = !!st.draft.trim() && !st.thinking;
    var button = el("button", "send" + (ready ? " send-on" : ""));
    if (!ready) button.setAttribute("disabled", "disabled");
    button.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24"'
      + ' fill="none" stroke="currentColor" stroke-width="1.6"'
      + ' stroke-linejoin="round"><path d="M3 20 L21 11 L4 4 L7 11 Z"></path>'
      + '<path d="M7 11 L21 11"></path></svg>';
    button.style.color = ready ? "var(--acc)" : "var(--fnt)";
    on(button, "click", send);
    box.appendChild(button);

    col.appendChild(box);
    foot.appendChild(col);
    parent.appendChild(foot);

    // The reader is mid-sentence far more often than not on a redraw.
    if (document.activeElement === document.body && st.msgs.length > 1) {
      try { field.focus(); } catch (e) { /* not focusable yet */ }
    }
  }

  try {
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)")
        .addEventListener("change", draw);
    }
  } catch (e) { /* an old browser keeps the theme it opened with */ }

  draw();
  window.__hcSetup = { state: function () { return st; }, draw: draw };
})();
