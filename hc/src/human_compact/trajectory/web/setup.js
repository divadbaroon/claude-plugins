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
    shown: [],           // the cards drawn so far: the step order is read
                         // from this, and the server will not draw one out
                         // of turn
    thinking: false,
    draft: "",
    error: "",
    plan: null,          // the plan they approved
    goals: null,         // the goals they were offered
    chosen: "",          // the one they picked
    other: "",           // ...or the one they typed
    goalNote: "",        // what else the rows should know about it
    todos: [],           // rows, editable -- flat, when it did not break down
    pieces: [],          // ...or the pieces of the chosen goal, with rows
    newTodo: "",
    name: "",            // the project's name, typed while the rest arrives
    made: null           // what commit gave back
  };

  var OPEN = "Tell me what you're working on in your own words."
    + " I'll ask a few questions, then write up a plan for you to approve.";

  function dark() {
    // Light unless the reader has asked for dark on this page. The first
    // thing anybody sees of this tool should not depend on a system setting
    // they made for something else -- and a dark install screen reads as a
    // terminal, which is the thing they were trying to get out of.
    try {
      return window.localStorage
        && window.localStorage.getItem("hc-setup-theme") === "dark";
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
    var b = el("button", "btn " + (cls || ""));
    b.appendChild(el("span", "", label));
    // The chevron is on the button that moves the reader forward and on no
    // other: it is what tells them which of two buttons is the way on.
    if ((cls || "").indexOf("btn-on") >= 0) b.appendChild(el("span", "go", "›"));
    if (disabled) b.setAttribute("disabled", "disabled");
    else on(b, "click", fn);
    return b;
  }

  function str(value) { return value == null ? "" : String(value); }

  function fresh() { return "x" + Math.random().toString(36).slice(2, 8); }

  function row(text) { return { id: fresh(), text: str(text) }; }

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
    post({ op: "setup_say", transcript: st.msgs.concat(extra || []),
           shown: st.shown })
      .then(function (out) {
        st.thinking = false;
        if (!out || !out.ok) {
          st.error = (out && out.error) || "setup could not reach Claude";
          draw();
          return;
        }
        say("engelbart", out.say);
        st.card = out;
        if (out.card && out.card !== "none") st.shown.push(out.card);
        if (out.card === "plan") st.plan = out.plan;
        if (out.card === "goals") st.goals = out.goals;
        if (out.card === "todos") {
          st.todos = (out.todos || []).map(row);
          st.pieces = (out.subgoals || []).map(function (g) {
            return { id: fresh(), label: g.label,
                     todos: (g.todos || []).map(row) };
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
    hero(col, "");
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

    step(body, "1", "Open the chat you were working in.", "claude -r", true);
    var pick = el("div", "step");
    pick.appendChild(el("div", "step-n", "2"));
    var pb = el("div", "step-b");
    pb.appendChild(el("div", "step-t",
      "Pick that chat from the list Claude Code shows you."));
    pick.appendChild(pb);
    body.appendChild(pick);
    step(body, "3", "Open its workspace.", "/bart");

    var acts = el("div", "acts");
    acts.appendChild(btn("Back", "btn-quiet", function () {
      st.screen = "fork";
      draw();
    }));
    body.appendChild(acts);
    card.appendChild(body);
    col.appendChild(card);
  }

  function step(parent, n, text, command, openable) {
    var row = el("div", "step");
    row.appendChild(el("div", "step-n", n));
    var body = el("div", "step-b");
    body.appendChild(el("div", "step-t", text));
    if (command) body.appendChild(commandRow(command, openable));
    row.appendChild(body);
    parent.appendChild(row);
  }

  function openTerminal(command, said) {
    // The machine opens a terminal with the command already in it and the
    // reader presses Return. Where it cannot -- no terminal it knows how to
    // drive, a permission not granted -- the copy row beside this is still
    // the answer, so a refusal says so quietly rather than failing.
    said.textContent = "opening…";
    post({ op: "setup_open_terminal", command: command }).then(function (out) {
      said.textContent = (out && out.ok)
        ? "opened — press Return in it"
        : "copy it instead";
      setTimeout(function () { said.textContent = ""; }, 6000);
    });
  }

  function commandRow(command, openable) {
    var row = el("div", "cmd");
    row.appendChild(el("span", "cmd-text", command));
    var said = el("span", "cmd-said", "");
    if (openable) {
      var open = el("button", "cmd-copy", "open terminal");
      on(open, "click", function () { openTerminal(command, said); });
      row.appendChild(open);
    }
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
    var wrap = el("div", "");
    wrap.appendChild(row);
    wrap.appendChild(said);
    return wrap;
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

    if (st.thinking) col.appendChild(generating());

    if (st.error) {
      // A round that came back with nothing is a dead end unless there is a
      // way out of it: the model is occasionally unparseable, and the
      // reader should not have to reload the page to find that out.
      var bad = el("div", "");
      bad.appendChild(el("div", "err", st.error));
      var again = el("div", "acts");
      again.appendChild(btn("Try again", "btn-on", function () { round(); }));
      bad.appendChild(again);
      col.appendChild(bad);
    }

    var kind = st.card && st.card.card;
    if (!st.thinking && kind === "questions") drawQuestions(col);
    if (!st.thinking && kind === "plan") drawPlan(col);
    if (!st.thinking && kind === "goals") drawGoals(col);
    if (!st.thinking && kind === "todos") drawTodos(col);

    drawComposer(app);
  }

  function generating() {
    // Nine dots in a square, lit in turn. A ring says "waiting"; this says
    // something is being made, which is what is actually happening.
    var box = el("div", "think rise");
    var grid = el("span", "dots");
    for (var i = 0; i < 9; i++) {
      var dot = el("span", "dot");
      dot.style.animationDelay = (i % 3 + Math.floor(i / 3)) * 90 + "ms";
      grid.appendChild(dot);
    }
    box.appendChild(grid);
    box.appendChild(el("span", "", "generating"));
    return box;
  }

  function cardBox(col, eyebrow, right) {
    var card = el("div", "card rise");
    var head = el("div", "card-head");
    var line = el("div", "");
    line.style.display = "flex";
    line.style.alignItems = "baseline";
    line.appendChild(el("span", "lbl", eyebrow));
    if (right) line.appendChild(el("span", "lbl right", right));
    head.appendChild(line);
    head.appendChild(el("div", "rule"));
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
                       items.length === 1 ? "1 question"
                                          : items.length + " questions");
    items.forEach(function (q) { body.appendChild(questionNode(q)); });

    var acts = el("div", "acts");
    var ready = items.some(function (q) {
      var got = st.answers[q.id];
      return Array.isArray(got) ? got.length : str(got).trim();
    });
    acts.appendChild(btn("Send answers", ready ? "btn-on" : "",
                         submitAnswers, !ready));
    acts.appendChild(btn("Skip", "", skipAnswers));
    body.appendChild(acts);
  }

  function questionNode(q) {
    var box = el("div", "q");
    box.appendChild(el("div", "card-title", q.title));
    if (q.subtitle) box.appendChild(el("div", "card-sub", q.subtitle));
    else if (q.type === "select_all") {
      box.appendChild(el("div", "card-sub", "select all that apply"));
    }
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
    body.appendChild(el("div", "card-title",
                        "Here's what I think you're working on"));
    str(plan.description).split("\n\n").forEach(function (para) {
      if (!para.trim()) return;
      body.appendChild(el("div", "prose", para.trim()));
    });
    // What it could not settle. The part that tells them whether it
    // understood them or guessed, so it is shown rather than buried.
    if ((plan.unsure || []).length) {
      var box = el("div", "inset");
      box.appendChild(el("div", "lbl", "still unsure about"));
      plan.unsure.forEach(function (line) {
        var row = el("div", "bullet");
        row.appendChild(el("span", "bullet-dot", "·"));
        row.appendChild(el("span", "", line));
        box.appendChild(row);
      });
      body.appendChild(box);
    }
    var ask = el("div", "card-title", "Is that basically right?");
    ask.style.marginTop = "16px";
    body.appendChild(ask);
    var acts = el("div", "acts");
    acts.appendChild(btn("Continue", "btn-on", function () {
      st.plan = plan;
      say("you", "Approved.");
      round();
    }));
    acts.appendChild(btn("Add something", "", function () {
      var field = document.querySelector(".composer .f");
      if (field) field.focus();
    }));
    body.appendChild(acts);
  }

  // --- goals ----------------------------------------------------------------

  function drawGoals(col) {
    var goals = st.card.goals || [];
    var typing = st.chosen === " other";
    var label = typing ? st.other.trim() : st.chosen;
    var body = cardBox(col, "goal");
    body.appendChild(el("div", "card-title", "What should we focus on?"));

    // Chosen, it opens up: the one they picked is lifted out of the list and
    // shown as the thing being started, with the rest still there to change
    // their mind with. Reading a decision back is what makes it feel made.
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
    mine.setAttribute("data-on", typing ? "1" : "0");
    mine.appendChild(el("span", "mark mark-one", ""));
    var mineText = el("span", "opt-text");
    mineText.appendChild(el("span", "opt-label", "Something else"));
    mineText.appendChild(el("span", "opt-why",
      "tell it what to start on instead and it will use that"));
    mine.appendChild(mineText);
    on(mine, "click", function () {
      st.chosen = typing ? "" : " other";
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
      on(input, "blur", draw);
      wrap.appendChild(input);
      body.appendChild(wrap);
    }

    // Chosen, it opens a box rather than reading the goal back at them:
    // what they have to add is what the rows should know, and the goal is
    // still on screen two lines above.
    if (label) {
      var more = el("div", "rise");
      more.style.marginTop = "14px";
      more.appendChild(el("div", "lbl", "anything the rows should know"));
      var wrap = el("div", "field");
      var note = el("textarea", "f");
      note.setAttribute("rows", "3");
      note.setAttribute("spellcheck", "false");
      note.setAttribute("placeholder",
                        "constraints, what to leave alone, where to start…");
      note.value = st.goalNote;
      on(note, "input", function () { st.goalNote = note.value; });
      wrap.appendChild(note);
      more.appendChild(wrap);
      body.appendChild(more);
    }

    var acts = el("div", "acts");
    acts.appendChild(btn("Generate TODOs", label ? "btn-on" : "", function () {
      st.goals = goals;
      say("you", st.goalNote.trim()
          ? label + "\n\n" + st.goalNote.trim() : label);
      round();
    }, !label));
    body.appendChild(acts);
  }

  // --- todos, and the name ---------------------------------------------------

  function drawTodos(col) {
    var pieces = st.pieces;
    var count = pieces.length
      ? pieces.reduce(function (n, g) { return n + g.todos.length; }, 0)
      : st.todos.length;
    var body = cardBox(col, "todos",
                       count === 1 ? "1 row" : count + " rows");

    if (pieces.length) {
      // The pieces of the goal, with their rows under them. Editable in
      // place: what the model proposed is a first draft of the reader's
      // own tree, not a thing to accept whole.
      pieces.forEach(function (piece) {
        var head = el("div", "piece");
        var name = el("input", "f piece-name");
        name.setAttribute("type", "text");
        name.setAttribute("spellcheck", "false");
        name.value = piece.label;
        on(name, "input", function () { piece.label = name.value; });
        head.appendChild(name);
        var drop = el("button", "x", "×");
        on(drop, "click", function () {
          st.pieces = st.pieces.filter(function (g) { return g !== piece; });
          draw();
        });
        head.appendChild(drop);
        body.appendChild(head);
        var kids = el("div", "kids");
        piece.todos.forEach(function (t) {
          kids.appendChild(todoRow(t, piece.todos, function () {
            piece.todos = piece.todos.filter(function (r) { return r !== t; });
          }));
        });
        kids.appendChild(adder(function (text) {
          piece.todos.push(row(text));
        }));
        body.appendChild(kids);
      });
    } else {
      st.todos.forEach(function (t) {
        body.appendChild(todoRow(t, st.todos, function () {
          st.todos = st.todos.filter(function (r) { return r !== t; });
        }));
      });
      body.appendChild(adder(function (text) { st.todos.push(row(text)); }));
    }

    // The name, and the button that lives in it. Asked for here rather than
    // at the start: by now they have seen what the project is, so naming it
    // is recognition rather than invention -- and it is the one thing left
    // to do while they read the rows. The button sits inside the field
    // because naming and making are one act, not two.
    var nameWrap = el("div", "");
    nameWrap.style.marginTop = "20px";
    nameWrap.appendChild(el("div", "lbl", "name your project"));
    var pill = el("div", "name-row");
    var name = el("input", "f");
    name.setAttribute("type", "text");
    name.setAttribute("spellcheck", "false");
    name.setAttribute("placeholder", "a short name");
    name.value = st.name;
    var go = btn("Create project", st.name.trim() ? "btn-on" : "",
                 complete, !st.name.trim());
    on(name, "input", function () {
      st.name = name.value;
      // Toggled in place rather than by redrawing: the reader is typing in
      // the field that decides it, and a sweep would take the caret away.
      if (st.name.trim()) {
        go.removeAttribute("disabled");
        go.className = "btn btn-on";
        if (!go.querySelector(".go")) go.appendChild(el("span", "go", "\u203a"));
        go.onclick = complete;
      } else {
        go.setAttribute("disabled", "disabled");
        go.className = "btn";
        var chev = go.querySelector(".go");
        if (chev) chev.remove();
      }
    });
    pill.appendChild(name);
    pill.appendChild(go);
    nameWrap.appendChild(pill);
    body.appendChild(nameWrap);
  }

  function todoRow(t, list, drop) {
    var line = el("div", "row");
    line.appendChild(el("span", "bullet-dot", "\u00b7"));
    var input = el("input", "f");
    input.setAttribute("type", "text");
    input.setAttribute("spellcheck", "false");
    input.value = t.text;
    on(input, "input", function () { t.text = input.value; });
    line.appendChild(input);
    var x = el("button", "x", "\u00d7");
    on(x, "click", function () { drop(); draw(); });
    line.appendChild(x);
    return line;
  }

  function adder(add) {
    var line = el("div", "row");
    line.appendChild(el("span", "bullet-dot", "\u00b7"));
    var input = el("input", "f");
    input.setAttribute("type", "text");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("placeholder", "add a row\u2026");
    on(input, "keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      var text = input.value.trim();
      if (!text) return;
      add(text);
      draw();
    });
    line.appendChild(input);
    return line;
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
      st.pieces = [];
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
    step(nbody, "1", "Start a chat in your terminal.", "claude", true);
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
