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

  // The four things asked before anything else, and how the slider holds
  // the last of them. `slide` is the raw track position so the thumb can go
  // wherever the finger is; `profile.level` is the stop it lands on, which
  // is the only part the server hears about.
  var YEARS = [
    { value: "1", label: "First year" },
    { value: "2", label: "Second year" },
    { value: "3", label: "Third year" },
    { value: "4", label: "Fourth year" }
  ];

  // Seeds, not a list to choose from: what people here actually study, so
  // most readers press one row instead of typing. Anything typed that is
  // not among them is their answer exactly as written.
  var MAJORS = [
    "Computer Science", "Electrical Engineering & Computer Sciences",
    "Data Science", "Cognitive Science", "Molecular & Cell Biology",
    "Bioengineering", "Mechanical Engineering", "Civil Engineering",
    "Chemical Engineering", "Applied Mathematics", "Mathematics",
    "Statistics", "Physics", "Chemistry", "Economics",
    "Business Administration", "Political Science", "Psychology",
    "Integrative Biology", "Public Health", "Environmental Science",
    "Media Studies", "English", "History", "Sociology",
    "Art Practice", "Architecture", "Undeclared"
  ];
  var SEEDS_SHOWN = 6;

  var LEVELS = [
    { key: "plain", label: "Plain language",
      why: "no jargon — everything said in ordinary words" },
    { key: "some", label: "Some technical detail",
      why: "ordinary terms stand on their own; anything narrower is explained" },
    { key: "full", label: "Fully technical",
      why: "the precise word, unglossed" }
  ];

  var st = {
    screen: "who",
    profile: { name: "", year: "", major: "", level: "some" },
    slide: 100,          // 0, 100 or 200 at rest; anything between mid-drag
    yearOther: false,    // they chose to write their own year
    savingWho: false,
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
    page: 0,             // which piece is on screen: the rows are walked one
                         // goal at a time, and the page after the last is
                         // where the project gets made
    newTodo: "",
    name: "",            // the project's name -- proposed, not asked for
    nameTouched: false,  // ...unless they have changed it, after which it is
                         // theirs and nothing overwrites it
    made: null,          // what commit gave back
    who: null,           // the chat this page was opened in, if any
    focus: null,         // the three read out of that chat, trees and all
    summary: "",         // their one sentence, asked for to fill the wait
    saidSummary: false,  // ...and whether they have finished with it
    adopting: false,     // this page opened in a chat, and will bind it
    newProject: false,   // ...and came here from a workspace asking for a new
                         // project, so the fork is already answered and the
                         // chat that asked is the one that gets bound
    opened: {}           // commands a terminal has already been opened for:
                         // draw() runs on every keystroke somewhere, and a
                         // window per redraw is not help
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
          st.page = 0;
          // Named rather than asked for. The model heard the whole
          // description and this is the last card, so what the reader gets
          // is a filled field to disagree with -- which is a smaller thing
          // to do than inventing a name at the end of a conversation.
          if (!st.nameTouched) st.name = str(out.name).trim() || fromGoal();
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
    if (st.screen === "who") drawWho();
    else if (st.screen === "fork") drawFork();
    else if (st.screen === "resume") drawResume();
    else if (st.screen === "adopt") drawAdopt();
    else if (st.screen === "done") drawDone();
    else drawTalk();
    drawBypass();
  }

  // The way out of the conversation, for somebody who came here from a
  // workspace and would rather just name the thing. Small, and in the corner:
  // describing the project is what this page is for and what makes the goals
  // worth having, so the bypass is available without being the offer.
  //
  // Only where there is something to go back to. A page opened after an
  // install has no workspace behind it, and a button that says "skip this"
  // to somebody with nowhere else to be is a dead end.
  function drawBypass() {
    if (!st.newProject || st.screen === "done") return;
    var out = el("button", "bypass", "Skip — just name it");
    on(out, "click", function () { window.location.href = "/?quick=1"; });
    app.appendChild(out);
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

  // Screen 0: who is reading this.
  //
  // Everything the tool says afterwards was written by a model, and by
  // default a model writes for whoever wrote the prompt -- somebody who
  // already knows what a branch and a migration are. Four answers change
  // that: they are appended to the setup conversation, to the Understanding
  // tab's answers, to a generated prompt and to every build, so the words
  // coming back are the reader's own rather than the repository's.
  //
  // Asked once and never again: the answers belong to the account, so a
  // returning reader is taken straight past this.
  function drawWho() {
    var col = column(app);
    hero(col, "So it talks to you, and not past you.");
    var body = cardBox(col, "about you", "4 questions");

    var nameQ = el("div", "q");
    nameQ.appendChild(el("div", "card-title", "What is your name?"));
    var nameWrap = el("div", "field");
    var nameInput = el("input", "f");
    nameInput.setAttribute("type", "text");
    nameInput.setAttribute("spellcheck", "false");
    nameInput.setAttribute("placeholder", "what people call you");
    nameInput.value = st.profile.name;
    // Held without a redraw, like every other field on this page: a sweep
    // on each keystroke would take the caret out of the box.
    on(nameInput, "input", function () { st.profile.name = nameInput.value; });
    nameWrap.appendChild(nameInput);
    nameQ.appendChild(nameWrap);
    body.appendChild(nameQ);

    body.appendChild(yearQuestion());

    var majorQ = el("div", "q");
    majorQ.appendChild(el("div", "card-title", "What is your major?"));
    majorQ.appendChild(el("div", "card-sub", "type it, or press one"));
    majorField(majorQ);
    body.appendChild(majorQ);

    var levelQ = el("div", "q");
    levelQ.appendChild(el("div", "card-title",
                          "How technical should explanations be?"));
    levelField(levelQ);
    body.appendChild(levelQ);

    if (st.error) body.appendChild(el("div", "err", st.error));

    var acts = el("div", "acts");
    acts.appendChild(btn(st.savingWho ? "Saving…" : "Continue", "btn-on",
                         function () { saveWho(); }, st.savingWho));
    // Never blockable. Somebody who does not want to answer should not be
    // held at the door of a tool they have not seen yet.
    acts.appendChild(btn("Skip", "", function () { leaveWho(); },
                         st.savingWho));
    body.appendChild(acts);
  }

  function yearQuestion() {
    var box = el("div", "q");
    box.appendChild(el("div", "card-title", "What year are you?"));
    var pick = function (value, other) {
      st.profile.year = value;
      st.yearOther = !!other;
      draw();
    };
    YEARS.forEach(function (y) {
      var picked = !st.yearOther && st.profile.year === y.value;
      box.appendChild(optionRow(y.label, "", picked, function () {
        pick(picked ? "" : y.value, false);
      }));
    });
    // Toggling it either way clears the answer: coming in, whichever
    // numbered year was picked is no longer what they mean; going out,
    // whatever they wrote is not one of the four.
    box.appendChild(optionRow("Something else", "", st.yearOther, function () {
      pick("", !st.yearOther);
    }));
    if (st.yearOther) {
      var wrap = el("div", "field");
      var input = el("input", "f");
      input.setAttribute("type", "text");
      input.setAttribute("spellcheck", "false");
      input.setAttribute("placeholder", "transferring, fifth-year, grad…");
      input.value = st.profile.year;
      on(input, "input", function () { st.profile.year = input.value; });
      wrap.appendChild(input);
      box.appendChild(wrap);
    }
    return box;
  }

  // The majors, seeded and filtered rather than listed whole: twenty-eight
  // rows is a form, and the point of seeding them is that most people press
  // one instead of typing. What is in the box is the answer either way, so
  // a major nobody thought of costs nothing to give.
  function majorField(parent) {
    var wrap = el("div", "field");
    var input = el("input", "f");
    input.setAttribute("type", "text");
    input.setAttribute("spellcheck", "false");
    input.setAttribute("placeholder", "start typing…");
    input.value = st.profile.major;
    wrap.appendChild(input);
    parent.appendChild(wrap);

    var seeds = el("div", "seeds");
    parent.appendChild(seeds);

    function fill() {
      seeds.textContent = "";
      var typed = str(st.profile.major).trim().toLowerCase();
      MAJORS.filter(function (m) {
        var low = m.toLowerCase();
        // An exact match is already in the box; a row that would change
        // nothing is a row in the way.
        return low !== typed && (!typed || low.indexOf(typed) >= 0);
      }).slice(0, SEEDS_SHOWN).forEach(function (m) {
        var seed = el("button", "seed", m);
        on(seed, "click", function () {
          st.profile.major = m;
          input.value = m;
          fill();
        });
        seeds.appendChild(seed);
      });
    }
    // Filtered in place, never by redrawing: they are typing in the box
    // that decides which rows show.
    on(input, "input", function () {
      st.profile.major = input.value;
      fill();
    });
    fill();
  }

  // Three stops, but a slider rather than three buttons: the thumb follows
  // the finger the whole way and settles on the nearest stop when it is let
  // go, so the answer reads as a position on a scale instead of a category.
  function levelField(parent) {
    var wrap = el("div", "slider");
    var track = el("input", "range");
    track.setAttribute("type", "range");
    track.setAttribute("min", "0");
    track.setAttribute("max", "200");
    track.setAttribute("step", "1");
    track.setAttribute("aria-label", "How technical should explanations be?");
    track.value = String(st.slide);
    wrap.appendChild(track);

    var stops = el("div", "stops");
    var marks = LEVELS.map(function (level, at) {
      var mark = el("button", "stop", level.label);
      on(mark, "click", function () {
        st.slide = at * 100;
        track.value = String(st.slide);
        settle();
      });
      stops.appendChild(mark);
      return mark;
    });
    wrap.appendChild(stops);

    var why = el("div", "hint");
    wrap.appendChild(why);

    function nearest() {
      return Math.max(0, Math.min(2, Math.round(Number(track.value) / 100)));
    }
    function settle() {
      var at = nearest();
      st.profile.level = LEVELS[at].key;
      marks.forEach(function (mark, i) {
        mark.setAttribute("data-on", i === at ? "1" : "0");
      });
      why.textContent = LEVELS[at].why;
    }
    // While it moves, the label follows the nearest stop; when it is let
    // go, the thumb goes there too.
    on(track, "input", settle);
    on(track, "change", function () {
      st.slide = nearest() * 100;
      track.value = String(st.slide);
      settle();
    });
    settle();
    parent.appendChild(wrap);
  }

  // The option row this page draws everywhere: a line that becomes a box
  // when it is the chosen one.
  function optionRow(label, why, picked, fn) {
    var row = el("div", "opt");
    row.setAttribute("data-on", picked ? "1" : "0");
    row.appendChild(el("span", "mark mark-one", ""));
    var text = el("span", "opt-text");
    text.appendChild(el("span", "opt-label", label));
    if (why) text.appendChild(el("span", "opt-why", why));
    row.appendChild(text);
    on(row, "click", fn);
    return row;
  }

  function answered(profile) {
    if (!profile) return false;
    return !!(str(profile.name).trim() || str(profile.year).trim()
              || str(profile.major).trim() || str(profile.level).trim());
  }

  function saveWho() {
    if (st.savingWho) return;
    st.savingWho = true;
    st.error = "";
    draw();
    post({ op: "setup_profile", profile: st.profile }).then(function (out) {
      st.savingWho = false;
      if (!out || !out.ok) {
        // Worth stopping for: these answers are read from the vault by
        // every prompt afterwards, so one that did not land is one that
        // changes nothing. Skip is still there for anyone who would rather
        // get on with it.
        st.error = (out && out.error) || "that could not be saved";
        draw();
        return;
      }
      leaveWho();
    });
  }

  // Where the first card hands off to: the chat this page was opened in, if
  // there is one worth reading, and the fork if there is not.
  function leaveWho() {
    st.error = "";
    // Already answered, over in the workspace: they pressed "create a new
    // project", which is the fork's own question. Asking it again here would
    // be the tool forgetting what it was told one screen ago.
    if (st.newProject) {
      st.screen = "talk";
      if (!st.msgs.length) say("engelbart", OPEN);
      draw();
      return;
    }
    if (st.adopting) {
      st.screen = "adopt";
      readChat();
      return;
    }
    st.screen = "fork";
    draw();
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
    hero(col, "");
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

  function openTerminal(command, said, asked) {
    // A terminal opens with the command already in it and the reader
    // presses Return. It happens by itself the first time a screen shows a
    // command, because "open a terminal" is not a decision anybody came
    // here to make -- the button beside it is for the second one.
    //
    // Where it cannot -- no terminal this knows how to drive, a permission
    // not granted -- the copy row is still the answer, so a refusal says so
    // quietly and stays out of the way. Nothing here is fatal.
    said.textContent = asked ? "opening…" : "";
    post({ op: "setup_open_terminal", command: command }).then(function (out) {
      if (out && out.ok) {
        // Which key depends on how it got the command in there, which the
        // server is the only one that knows: typed into the window, or put
        // in the new shell's history because it may not type.
        said.textContent = out.note === "up"
          ? "opened a terminal — press Up, then Return"
          : "opened a terminal — press Return in it";
        return;
      }
      // Only worth saying when they asked. Unasked, the copy button next to
      // it already says what to do, and an apology for something they did
      // not request is noise.
      said.textContent = asked ? "could not open one — copy it instead" : "";
      if (asked) setTimeout(function () { said.textContent = ""; }, 6000);
    });
  }

  function commandRow(command, openable) {
    var row = el("div", "cmd");
    row.appendChild(el("span", "cmd-text", command));
    var said = el("span", "cmd-said", "");
    if (openable) {
      var open = el("button", "cmd-copy", "open a terminal");
      on(open, "click", function () { openTerminal(command, said, true); });
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
    // Once per command, not once per drawing of it. Deferred a beat so the
    // row is on the page before the answer lands on it, and so a screen
    // that draws twice in a frame asks once.
    if (openable && !st.opened[command]) {
      st.opened[command] = true;
      setTimeout(function () { openTerminal(command, said, false); }, 250);
    }
    return wrap;
  }

  // Screen 2b: a chat with plenty in it and no project. Nothing is asked of
  // them about the work -- the transcript is the description, and asking
  // for it again would be asking them to repeat themselves. The reading
  // starts the moment this opens; the one sentence is what they do while it
  // happens, and it names the project rather than feeding the goals.
  function drawAdopt() {
    var col = column(app);
    if (!st.focus) hero(col, "Reading this chat.");

    if (!st.saidSummary) {
      var body = cardBox(col, "while that happens");
      body.appendChild(el("div", "card-title",
                          "In one sentence, what is this project?"));
      body.appendChild(el("div", "card-sub",
                          "for the project's own page — the goals come from"
                          + " the conversation, not from this"));
      var wrap = el("div", "field");
      var input = el("textarea", "f");
      input.setAttribute("rows", "2");
      input.setAttribute("spellcheck", "false");
      input.setAttribute("placeholder", "a tool for…, a rewrite of…, …");
      input.value = st.summary;
      wrap.appendChild(input);
      body.appendChild(wrap);
      var acts = el("div", "acts");
      var done = btn("Done", st.summary.trim() ? "btn-on" : "",
                     function () { st.saidSummary = true; draw(); },
                     !st.summary.trim());
      // Toggled in place, never by redrawing: they are typing in the field
      // that decides it, and a sweep would take the caret out of the box.
      on(input, "input", function () {
        st.summary = input.value;
        if (st.summary.trim()) {
          done.removeAttribute("disabled");
          done.className = "btn btn-on";
          if (!done.querySelector(".go")) {
            done.appendChild(el("span", "go", "\u203a"));
          }
          done.onclick = function () { st.saidSummary = true; draw(); };
        } else {
          done.setAttribute("disabled", "disabled");
          done.className = "btn";
          var chev = done.querySelector(".go");
          if (chev) chev.remove();
        }
      });
      acts.appendChild(done);
      acts.appendChild(btn("Skip", "", function () {
        st.saidSummary = true;
        draw();
      }));
      body.appendChild(acts);
    }

    // Still reading. The same nine dots the conversation uses, because it
    // is the same wait for the same reason.
    if (st.saidSummary && !st.focus && !st.error) col.appendChild(generating());
    if (st.error) {
      var bad = el("div", "");
      bad.appendChild(el("div", "err", st.error));
      var again = el("div", "acts");
      again.appendChild(btn("Try again", "btn-on", readChat));
      again.appendChild(btn("Start from scratch instead", "", function () {
        st.screen = "talk";
        st.error = "";
        say("engelbart", OPEN);
        draw();
      }));
      bad.appendChild(again);
      col.appendChild(bad);
    }
    if (st.focus && st.saidSummary) drawFocus(col);
  }

  function readChat() {
    // Started the moment the screen opens, not when they finish typing:
    // the sentence is there to cover this, and covering it only works if
    // it is already running.
    st.error = "";
    st.focus = null;
    draw();
    post({ op: "setup_from_chat", session: (st.who || {}).session })
      .then(function (out) {
        if (!out || !out.ok) {
          st.error = (out && out.error) || "this chat could not be read";
          draw();
          return;
        }
        st.focus = out.focus;
        draw();
      });
  }

  // The three, and the tree of whichever is chosen -- already written, so
  // choosing shows something at once instead of starting a third wait.
  function drawFocus(col) {
    var body = cardBox(col, "goal");
    body.appendChild(el("div", "card-title", "What should we focus on?"));
    var chosen = null;
    st.focus.forEach(function (f) {
      var picked = st.chosen === f.label;
      if (picked) chosen = f;
      var row = el("div", "opt");
      row.setAttribute("data-on", picked ? "1" : "0");
      row.appendChild(el("span", "mark mark-one", ""));
      var text = el("span", "opt-text");
      text.appendChild(el("span", "opt-label", f.label));
      if (f.why) text.appendChild(el("span", "opt-why", f.why));
      row.appendChild(text);
      on(row, "click", function () {
        st.chosen = picked ? "" : f.label;
        draw();
      });
      body.appendChild(row);
    });

    if (chosen && chosen.subgoals.length) {
      var open = el("div", "rise");
      open.style.marginTop = "16px";
      open.appendChild(el("div", "lbl", "which breaks into"));
      var kids = el("div", "kids");
      chosen.subgoals.forEach(function (kid) {
        var line = el("div", "row");
        line.appendChild(el("span", "bullet-dot", "\u00b7"));
        var name = el("input", "f");
        name.setAttribute("type", "text");
        name.setAttribute("spellcheck", "false");
        name.value = kid.label;
        on(name, "input", function () { kid.label = name.value; });
        line.appendChild(name);
        var x = el("button", "x", "\u00d7");
        on(x, "click", function () {
          chosen.subgoals = chosen.subgoals.filter(function (k) {
            return k !== kid;
          });
          draw();
        });
        line.appendChild(x);
        kids.appendChild(line);
      });
      open.appendChild(kids);
      body.appendChild(open);
    }

    var acts = el("div", "acts");
    acts.appendChild(btn("Generate TODOs", chosen ? "btn-on" : "", function () {
      // From here it is the conversation's own last step, so it joins it:
      // the transcript stands in for everything that was said, and the
      // pieces already chosen are what the rows go under.
      st.goals = st.focus.map(function (f) {
        return { label: f.label, why: f.why };
      });
      st.msgs = [{ role: "engelbart", text: "Read this chat." }];
      // Their one sentence goes in as a turn of theirs. On this path there
      // was no plan card and no description, so without it the only thing
      // the next call knows about the project is the goal they picked --
      // and a project named after where it starts is named wrong.
      if (st.summary.trim()) {
        st.msgs.push({ role: "you",
                       text: "The project, in a sentence: " + st.summary.trim() });
      }
      st.msgs.push({ role: "you", text: st.chosen
          + (chosen.subgoals.length
             ? "\n\nIn these pieces:\n"
               + chosen.subgoals.map(function (k) {
                   return "- " + k.label;
                 }).join("\n")
             : "") });
      st.shown = ["questions", "plan", "goals"];
      st.screen = "talk";
      round();
    }, !chosen));
    body.appendChild(acts);
  }

  // Screen 3: the conversation.
  function drawTalk() {
    var col = column(app);
    if (st.msgs.length <= 1 && !st.card) hero(col, "");

    st.msgs.forEach(function (m, index) {
      var cls = "msg rise " + (m.role === "you" ? "msg-you" : "msg-them");
      if (index === 0 && m.role !== "you" && m.text === OPEN) cls += " msg-opening";
      var box = el("div", cls);
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

  function fromGoal() {
    // Only where the model sent no name. A goal is what they are starting
    // on rather than what the project is, so this is the worse name of the
    // two -- but a filled field they can correct beats an empty one they
    // have to invent something for.
    var label = str(st.chosen === " other" ? st.other : st.chosen)
      .trim().replace(/[.·;:,\-–—\s]+$/, "");
    if (label.length <= 48) return label;
    // Cut at a word rather than mid-one: a folder called "Move uploads off
    // the app serv" reads as something that went wrong.
    var cut = label.slice(0, 48);
    var space = cut.lastIndexOf(" ");
    return (space > 0 ? cut.slice(0, space) : cut).replace(/[.·;:,]+$/, "");
  }

  function drawTodos(col) {
    // Broken into pieces, the rows are walked one goal at a time. A whole
    // twelve-row tree on one screen is the wall the breaking-up was for:
    // what somebody can actually read and correct is one goal's rows, and
    // the arrows are how they get to the next one.
    if (st.pieces.length) return drawPagedTodos(col);

    var body = cardBox(col, "todos",
                       st.todos.length === 1 ? "1 row"
                                             : st.todos.length + " rows");
    st.todos.forEach(function (t) {
      body.appendChild(todoRow(t, st.todos, function () {
        st.todos = st.todos.filter(function (r) { return r !== t; });
      }));
    });
    body.appendChild(adder(function (text) { st.todos.push(row(text)); }));
    nameBlock(body);
  }

  function drawPagedTodos(col) {
    var pieces = st.pieces;
    // The page after the last piece is where the project gets made, so the
    // walk ends somewhere rather than on a goal with a dead arrow beside it.
    var last = pieces.length;
    if (st.page > last) st.page = last;
    if (st.page < 0) st.page = 0;
    if (st.page === last) return drawMakeIt(col);

    var piece = pieces[st.page];
    var body = cardBox(col,
                       "goal " + (st.page + 1) + " of " + pieces.length,
                       piece.todos.length === 1
                         ? "1 row" : piece.todos.length + " rows");

    // The piece's own name, as the title of what is on screen and editable
    // there: what the model proposed is a first draft of the reader's own
    // tree, not a thing to accept whole.
    var head = el("div", "piece piece-head");
    var name = el("input", "f goal-title");
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

    piece.todos.forEach(function (t) {
      body.appendChild(todoRow(t, piece.todos, function () {
        piece.todos = piece.todos.filter(function (r) { return r !== t; });
      }, true));
    });
    body.appendChild(adder(function (text) { piece.todos.push(row(text)); },
                           true));
    pager(body, pieces.length + 1);
  }

  // The last page: the rows have all been read, and what is left is the
  // name and the button. Its own page rather than a tail on the last goal
  // -- "make it" is not a thing to come across while still reading rows.
  function drawMakeIt(col) {
    var pieces = st.pieces;
    var count = pieces.reduce(function (n, g) { return n + g.todos.length; }, 0);
    var body = cardBox(col, "ready", count === 1 ? "1 row" : count + " rows");
    body.appendChild(el("div", "card-title",
      count + (count === 1 ? " row" : " rows") + " across "
      + pieces.length + (pieces.length === 1 ? " goal." : " goals.")));
    nameBlock(body);
    pager(body, pieces.length + 1);
  }

  // Back, where you are, forward. The forward arrow is the filled one
  // because it is the way on; on the last page there is none, and the
  // button in the name field is the way on instead.
  function pager(body, pages) {
    var bar = el("div", "pager");
    var back = el("button", "arrow", "←");
    if (st.page <= 0) back.setAttribute("disabled", "disabled");
    else on(back, "click", function () { st.page -= 1; draw(); });
    bar.appendChild(back);

    var dots = el("div", "pager-dots");
    for (var i = 0; i < pages; i++) {
      var dot = el("span", "pdot");
      dot.setAttribute("data-on", i === st.page ? "1" : "0");
      // Clickable as well as walkable: somebody who has seen all three and
      // wants the second one back should not have to arrow past the first.
      (function (at) {
        on(dot, "click", function () { st.page = at; draw(); });
      })(i);
      dots.appendChild(dot);
    }
    bar.appendChild(dots);

    if (st.page < pages - 1) {
      var ahead = el("button", "arrow arrow-on", "→");
      on(ahead, "click", function () { st.page += 1; draw(); });
      bar.appendChild(ahead);
    } else {
      // Nothing to move to, but the row still has to balance: an empty slot
      // keeps the dots where they sat on every other page.
      bar.appendChild(el("span", "arrow arrow-gone", ""));
    }
    body.appendChild(bar);
  }

  // The name, and the button that lives in it. Filled in already -- by the
  // model, or failing that from the goal they chose -- because nobody
  // arrives at the end of a conversation wanting to invent a name, and the
  // field that stops them was the last thing between them and a project.
  // The button sits inside it because naming and making are one act.
  function nameBlock(body) {
    var nameWrap = el("div", "");
    nameWrap.style.marginTop = "20px";
    nameWrap.appendChild(el("div", "lbl", "name your project"));
    if (st.name.trim() && !st.nameTouched) {
      nameWrap.appendChild(el("div", "hint",
        "named from what you described — change it if it is wrong"));
    }
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
      // Theirs from here: a later card must not put its own name back over
      // one they have started correcting.
      st.nameTouched = true;
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

  // `boxed` is the paged view's row: a bordered line rather than one of a
  // ruled list, because on a screen holding one goal's four rows they are
  // the thing being read and not a table to run an eye down.
  function todoRow(t, list, drop, boxed) {
    var line = el("div", "row" + (boxed ? " row-boxed" : ""));
    if (!boxed) line.appendChild(el("span", "bullet-dot", "\u00b7"));
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

  function adder(add, boxed) {
    var line = el("div", "row" + (boxed ? " row-boxed row-add" : ""));
    if (!boxed) line.appendChild(el("span", "bullet-dot", "\u00b7"));
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
           // The chat that asked joins what it asked for -- whether it asked
           // by having been read (adopting) or by pressing the button in its
           // own workspace (newProject). A page opened after an install has
           // no chat behind it and binds nothing.
           bind: (st.adopting || st.newProject) && st.who ? st.who.session : "",
           plan: st.plan || { description: st.summary },
           goals: st.goals,
           chosen: st.chosen === " other" ? st.other : st.chosen,
           todos: st.todos.map(function (t) { return t.text; }),
           subgoals: st.pieces.map(function (g) {
             return { label: g.label,
                      todos: g.todos.map(function (t) { return t.text; }) };
           }) })
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
    body.appendChild(el("div", "card-sub", st.made.bound
      ? "This chat belongs to it. Its goals are waiting in the workspace."
      : "It belongs to no chat yet. Open a chat for it and its goals are"
        + " there waiting."));
    body.appendChild(el("div", "step-t", str(st.made.cwd)));
    var acts = el("div", "acts");
    // Bound, there is nothing to type in a terminal: the workspace this page
    // was opened from is now the workspace of this project.
    if (st.made.bound) {
      acts.appendChild(btn("Open the workspace", "btn-on", function () {
        window.location.href = "/";
      }));
    }
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
      st.page = 0;
      st.name = "";
      st.nameTouched = false;
      st.made = null;
      // The next one is not the one the workspace asked for, and there is no
      // second chat to bind it to: from here on this is the install page.
      st.newProject = false;
      draw();
    }));
    body.appendChild(acts);
    card.appendChild(body);
    col.appendChild(card);

    if (st.made.bound) return;
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
    var first = st.msgs.length <= 1;
    var box = el("div", "composer" + (first ? " composer-first" : ""));
    var field = el("textarea", "f");
    field.setAttribute("rows", "1");
    field.setAttribute("spellcheck", "false");
    field.setAttribute("aria-label", first
      ? "What are you working on?" : "Message Engelbart");
    field.setAttribute("placeholder", first
      ? "What are you working on? Describe it however it comes out…"
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
    if (document.activeElement === document.body) {
      try { field.focus(); } catch (e) { /* not focusable yet */ }
    }
  }

  try {
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)")
        .addEventListener("change", draw);
    }
  } catch (e) { /* an old browser keeps the theme it opened with */ }

  // Which of the two cold starts this is, and who is at it. A page served
  // after an install has no chat behind it and opens on the fork; one
  // opened by /bart in a conversation that has been going all afternoon
  // opens on that conversation, and starts reading it before anything is
  // asked. Either way the four questions come first -- once per account,
  // never again.
  function boot() {
    // Sent here by a workspace whose chat has no project: the fork has been
    // answered already, and the chat that asked is the one this commits
    // against.
    try {
      st.newProject = /[?&]new=1(&|$)/.test(String(window.location.search));
    } catch (e) { st.newProject = false; }
    draw();
    fetch("/setup.who").then(function (r) { return r.json(); })
      .then(function (who) {
        st.who = who || null;
        var kept = who && who.profile;
        if (answered(kept)) {
          st.profile = { name: str(kept.name), year: str(kept.year),
                         major: str(kept.major),
                         level: str(kept.level) || "some" };
          st.yearOther = !!st.profile.year
            && !YEARS.some(function (y) { return y.value === st.profile.year; });
          LEVELS.forEach(function (level, at) {
            if (level.key === st.profile.level) st.slide = at * 100;
          });
        }
        // Reading the chat is what to do when nobody said otherwise. They
        // did: "a new project" is not "the one this conversation has been
        // about", so the transcript is left alone.
        st.adopting = !st.newProject
          && !!(who && who.session && who.events > 0 && !who.bound);
        // The page opens on the questions and this only ever moves it past
        // them -- so a workspace that will not say who this is, or says
        // nobody, leaves the reader exactly where they started, with Skip
        // one press away.
        if (answered(kept)) {
          if (st.newProject) {
            st.screen = "talk";
            if (!st.msgs.length) say("engelbart", OPEN);
            draw();
            return;
          }
          if (st.adopting) {
            st.screen = "adopt";
            readChat();
            return;
          }
          st.screen = "fork";
        }
        draw();
      })
      .catch(function () { draw(); });
  }

  boot();
  window.__hcSetup = { state: function () { return st; }, draw: draw,
                       boot: boot };
})();
