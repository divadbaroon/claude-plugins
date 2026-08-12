/* hc ui bridge: seeds the Claude Design app's localStorage from goals.json
   before boot, then mirrors app edits back through /api/import. */
(function () {
  var KEY = "hc-vault-ui-v1";
  function toNode(g, byParent, todosOf) {
    var kids = (byParent[g.id] || []).map(function (c) { return toNode(c, byParent, todosOf); });
    var tds = (todosOf[g.id] || []).map(function (t, i) {
      return { id: "t:" + g.id + ":" + i, title: t.text, prio: "normal",
               done: !!t.done, open: true, status: "todo", notes: "", desc: "",
               labels: [], children: [] };
    });
    return { id: g.id, title: g.title, prio: g.priority || "normal",
             done: g.status === "completed" || g.status === "abandoned",
             open: true,
             status: g.status === "in_progress" ? "inprog" : "todo",
             notes: g.notes || "", desc: g.description || "", labels: [],
             children: tds.concat(kids) };
  }
  function seed() {
    try {
      var x = new XMLHttpRequest();
      x.open("GET", "/api/state", false);   // sync on purpose: must beat app boot
      x.send();
      var st = JSON.parse(x.responseText);
      var byParent = {}, todosOf = {};
      st.goals.forEach(function (g) {
        todosOf[g.id] = g.todos || [];
        var p = g.parent_goal_id || null;
        (byParent[p] = byParent[p] || []).push(g);
      });
      var roots = (byParent[null] || []).map(function (g) { return toNode(g, byParent, todosOf); });
      var sel = roots.length ? roots[0].id : null;
      localStorage.setItem(KEY, JSON.stringify({
        v: 6, goals: roots, selId: sel, filter: "active",
        updatedAt: st.generated_at ? Date.parse(st.generated_at) : Date.now(),
        labels: [], paneTab: "prompt", themeMode: null, view: "split"
      }));
      window.__hcSeed = JSON.stringify(roots);
    } catch (e) { /* offline model missing: let the app boot with whatever it has */ }
  }
  function watch() {
    var last = window.__hcSeed || null;
    setInterval(function () {
      var raw;
      try { raw = localStorage.getItem(KEY); } catch (e) { return; }
      if (!raw) return;
      var goals;
      try { goals = JSON.stringify(JSON.parse(raw).goals); } catch (e) { return; }
      if (goals === last) return;
      last = goals;
      fetch("/api/import", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: goals })
        .catch(function () {});
    }, 800);
  }
  seed();
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", watch);
  else watch();
})();
