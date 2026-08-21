(function () {
  "use strict";
  // The redrawn workspace, wired to the vault.
  //
  // The design ships as a self-contained demo: it makes no network calls at
  // all, and every project, goal, and note it shows is a string literal --
  // including a frozen snapshot of one real session. Left alone it narrates
  // a project nobody has.
  //
  // Nothing here reimplements the goal logic. The vault stays the source of
  // truth and the existing endpoints stay the way to reach it; this file only
  // rebinds the design's data sources onto them, and blanks the tables the
  // vault has no answer for so the view says nothing rather than something
  // untrue.

  var PAIRS = [
    [
        "const KNOWN = [",
        "const KNOWN = (window.__ebData && window.__ebData.KNOWN) || ["
    ],
    [
        "const OVD = {",
        "const OVD = (window.__ebData && window.__ebData.OVD) || {"
    ],
    [
        "const ENV = {",
        "const ENV = (window.__ebData && window.__ebData.ENV) || {"
    ],
    [
        "const SEEDS = {",
        "const SEEDS = (window.__ebData && window.__ebData.SEEDS) || {"
    ],
    [
        "const INJ = {",
        "const INJ = (window.__ebData && window.__ebData.INJ) || {"
    ],
    [
        "const SRCD = {",
        "const SRCD = (window.__ebData && window.__ebData.SRCD) || {"
    ],
    [
        "const REPOTREE = {",
        "const REPOTREE = (window.__ebData && window.__ebData.REPOTREE) || {"
    ],
    [
        "const FILEPREV = {",
        "const FILEPREV = (window.__ebData && window.__ebData.FILEPREV) || {"
    ],
    [
        "const REPOMETA = {",
        "const REPOMETA = (window.__ebData && window.__ebData.REPOMETA) || {"
    ],
    [
        "const OBJD = {",
        "const OBJD = (window.__ebData && window.__ebData.OBJD) || {"
    ],
    [
        "const OLDOBJ = [",
        "const OLDOBJ = (window.__ebData && window.__ebData.OLDOBJ) || ["
    ],
    [
        "const PROJECTS = [",
        "const PROJECTS = (window.__ebData && window.__ebData.PROJECTS) || ["
    ],
    [
        "const EVD = {",
        "const EVD = (window.__ebData && window.__ebData.EVD) || {"
    ],
    [
        "const TRAJ = {",
        "const TRAJ = (window.__ebData && window.__ebData.TRAJ) || {"
    ],
    [
        "defaultGoals() {",
        "defaultGoals() { if (window.__ebData && window.__ebData.GOALS) return window.__ebData.GOALS;"
    ],
    [
        "_load() {",
        "_load() { if (window.__ebData && window.__ebData.GOALS) return { goals: window.__ebData.GOALS, sel: (window.__ebData.GOALS[0] || {}).id || null, filter: 'all', collapsed: {}, light: null };"
    ],
    [
        "proj: 'claude-plugins',",
        "proj: (window.__ebData && window.__ebData.PROJ) || 'claude-plugins',"
    ],
    [
        "const ctxLevels = proj === 'claude-plugins' ? [",
        "const ctxLevels = false ? ["
    ],
    [
        "objectives: { 'claude-plugins': 'Enable two collaborators in independent Claude chats to share and work from the same goal tree.', 'engelbart-site': '' }",
        "objectives: {}"
    ],
    [
        "goalsStore: { 'engelbart-site': [] }",
        "goalsStore: {}"
    ],
    [
        ">refreshed 9:06 PM</span>",
        "></span>"
    ]
];

  function getJSON(path) {
    // Synchronous by necessity: the design reads its tables once, at boot,
    // before anything async could land.
    try {
      var request = new XMLHttpRequest();
      request.open("GET", path, false);
      request.send(null);
      return JSON.parse(request.responseText);
    } catch (error) {
      return null;
    }
  }

  // The vault stores goals flat, parented by id, and keeps a deleted goal on
  // disk marked "abandoned" rather than erasing it. The design expects a
  // nested tree. Translate, and honour the existing rule that an abandoned
  // goal is not shown (bridge.js does the same).
  var STATUS = { completed: "done", in_progress: "progress", active: "active" };

  function adaptGoals(goals) {
    var byId = {}, roots = [];
    (goals || []).forEach(function (goal) {
      if (!goal || goal.status === "abandoned") return;
      byId[goal.id] = {
        id: goal.id,
        title: goal.title || "",
        status: STATUS[goal.status] || "active",
        desc: goal.description || "",
        notes: goal.notes || "",
        sources: goal.sources || [],
        rel: goal.prompt_ids || [],
        kids: []
      };
    });
    (goals || []).forEach(function (goal) {
      var node = goal && byId[goal.id];
      if (!node) return;
      var parent = goal.parent_goal_id && byId[goal.parent_goal_id];
      if (parent) parent.kids.push(node); else roots.push(node);
    });
    return roots;
  }

  function mapOf(key, value) {
    var out = {};
    out[key] = value;
    return out;
  }

  function seed() {
    var state = getJSON("/api/state") || {};
    var tree = getJSON("/api/tree") || {};
    var who = state.project || {};
    var name = who.name || "";

    window.__ebData = {
      PROJ: name,
      GOALS: adaptGoals(state.goals),

      KNOWN: name ? [name] : [],
      PROJECTS: name ? [{ id: name, meta: "" }] : [],
      ENV: name ? mapOf(name, {
        repo: name,
        rows: [
          { k: "DIRECTORY", v: who.cwd || "" },
          { k: "REPO", v: who.remote || "" },
          { k: "BRANCH", v: who.branch || "" },
          { k: "LANGUAGES", v: "" }
        ],
        about: ""
      }) : {},
      REPOMETA: name ? mapOf(name, {
        url: who.remote || "", full: who.remote || name,
        def: who.branch || "", branches: who.branch ? [who.branch] : []
      }) : {},
      REPOTREE: name ? mapOf(name, tree.tree || []) : {},

      // The project's own repository, which is the one context source that
      // genuinely exists today: a real directory, a real remote, a real
      // branch, and a real file tree. Its narrative sections stay empty --
      // nothing infers an overview or a structure summary yet, and an
      // invented one would read exactly like a real one.
      SRCD: name ? mapOf(name, [{
        id: "repo", name: name, type: "Repository", scope: "project-wide",
        glyph: "R", inj: false, kind: "repo", title: name, meta: "",
        sub2: [who.remote, who.branch].filter(Boolean).join(" \u00b7 "),
        actions: [], sections: []
      }]) : {},

      // Narration the vault does not infer yet. Empty, not illustrated.
      OVD: {}, OBJD: {}, OLDOBJ: [], SEEDS: {}, INJ: {},
      FILEPREV: {}, EVD: {}, TRAJ: {}
    };
  }

  function patchSource(source) {
    return PAIRS.reduce(function (patched, part) {
      var at = patched.indexOf(part[0]);
      if (at < 0) {
        // Either this ran already, or the design moved under the patch. Those
        // need telling apart: a re-run must stay quiet, and drift must be
        // loud, because a patch that silently misses looks exactly like a
        // patch that worked. Testing the replacement first cannot tell them
        // apart -- a short replacement can occur naturally in the source and
        // fake a success.
        if (patched.indexOf(part[1]) < 0) {
          console.warn("[engelbart] patch did not apply:",
                       JSON.stringify(part[0]));
        }
        return patched;
      }
      // A replacement that begins with its own match -- a guard inserted just
      // after an opening brace -- would match again on every later pass and
      // stack copies of itself. Compare in place instead of searching, which
      // also keeps a short replacement from matching somewhere unrelated.
      if (patched.substr(at, part[1].length) === part[1]) return patched;
      return patched.slice(0, at) + part[1] + patched.slice(at + part[0].length);
    }, source);
  }

  function patchTemplate() {
    var island = document.querySelector('script[type="__bundler/template"]');
    if (!island) return false;
    var source;
    try { source = JSON.parse(island.textContent); }
    catch (error) { return false; }
    island.textContent = JSON.stringify(patchSource(source));
    return true;
  }

  seed();
  patchTemplate();

  window.__ebProjects = { patchSource: patchSource, adaptGoals: adaptGoals };
}());
