/* The Live preview pane: the project as the preview engine sees it.

   The engine keeps two things apart -- what surface there is to show (a
   page, a terminal, a file, instructions) and where the run stands -- and
   this pane draws the pairing honestly: the project's page in a frame once
   something answers on its address, the process's output while it runs
   without one, the one step that has to happen first when a file on disk
   says so, and an offer to find out how the project runs when nothing is
   configured yet. Nothing here starts on its own; every start is a click. */

import { h } from "../dom.js";

export function renderPreview(state, actions) {
  const preview = state.panes && state.panes.preview;
  const dot = () => h("span", { class: "dot", "aria-hidden": "true" });
  return h("section", { key: "pane-preview", class: "pane preview", role: "tabpanel" },
    h("div", { class: "preview-chrome" },
      dot(), dot(), dot(),
      h("span", { class: "preview-url" }, addressOf(preview)),
      running(preview) && h("button", {
        type: "button", class: "chrome-btn", onclick: actions.previewStop, disabled: state.previewBusy,
      }, "Stop")),
    renderBody(state, preview, actions));
}

const running = (preview) => Boolean(preview && preview.ok
  && (preview.status === "running" || preview.status === "starting" || preview.status === "not_ready"));

function addressOf(preview) {
  if (!preview || !preview.ok) return "";
  if (preview.url) return preview.url;
  if (preview.run && preview.run.command) return preview.run.command;
  if (preview.profile && preview.profile.command) return preview.profile.command;
  return preview.cwd || "";
}

function renderBody(state, preview, actions) {
  if (!preview) return body(h("p", { class: "pv-text" }, "Reading the project…"));
  if (!preview.ok) return body(h("p", { class: "pv-text" }, preview.error || "There is no project to run here."));

  const note = state.previewNote && h("p", { class: "pv-note", role: "status" }, state.previewNote.text);
  const busy = state.previewBusy;

  // A page: the project itself, framed.
  if (preview.url && (preview.status === "running" || preview.status === "starting")) {
    return h("iframe", { key: `frame:${preview.url}`, class: "preview-frame", src: preview.url, title: "Live preview",
      onload: () => actions.interaction("artifact.opened", { url: preview.url, kind: "preview" }) });
  }

  switch (preview.status) {
    case "unconfigured":
      return body(
        h("p", { class: "pv-text" }, preview.reason || "Nothing is set up to run yet."),
        preview.cwd && h("button", {
          type: "button", class: "dark-btn", onclick: actions.previewConfigure, disabled: busy,
        }, busy ? "Looking…" : "Find how to run it"),
        note);
    case "needs_user_action":
      return body(
        h("p", { class: "pv-text" }, "One thing has to happen first."),
        (preview.blockers || []).map((b) => h("div", { key: b.id, class: "pv-card" },
          h("p", { class: "pv-card-text" }, b.text),
          b.command && h("code", { class: "pv-cmd" }, b.command),
          b.why && h("p", { class: "pv-why" }, b.why))),
        preview.ui && preview.ui.available && h("button", {
          type: "button", class: "dark-btn", onclick: actions.previewShowUi, disabled: busy,
        }, busy ? "Starting…" : "Do it and show the UI"),
        note);
    case "ready":
    case "stale":
      return body(
        preview.profile && h("div", { class: "pv-card" },
          h("p", { class: "pv-card-text" }, preview.profile.name || "Run it"),
          preview.profile.command && h("code", { class: "pv-cmd" }, preview.profile.command),
          preview.profile.why && h("p", { class: "pv-why" }, preview.profile.why)),
        preview.status === "stale" && h("p", { class: "pv-text" },
          "The project's build files changed since this was worked out."),
        h("div", { class: "pv-actions" },
          preview.ui && preview.ui.available && h("button", {
            type: "button", class: "dark-btn", onclick: actions.previewShowUi, disabled: busy,
          }, busy ? "Starting…" : "Show UI"),
          h("button", {
            type: "button", class: "ghost-btn pv-btn", onclick: () => actions.previewRun(), disabled: busy,
          }, busy ? "Starting…" : "Run"),
          preview.status === "stale" && h("button", {
            type: "button", class: "ghost-btn pv-btn", onclick: actions.previewConfigure, disabled: busy,
          }, "Look again")),
        preview.ui && !preview.ui.available && h("p", { class: "pv-why" }, preview.ui.reason),
        note);
    case "starting":
    case "running":
      return output(preview, "Running.", note);
    case "not_ready":
      return output(preview, "It has been up a while and nothing is serving a page yet.", note);
    case "finished":
      return output(preview, exitLine(preview), note, again(actions, busy),
        (preview.artifacts || []).length
          ? h("p", { class: "pv-text" }, "It wrote: " + preview.artifacts.join(", "))
          : null);
    case "failed":
      return output(preview, exitLine(preview), note, again(actions, busy));
    default:
      return body(h("p", { class: "pv-text" }, preview.reason || ""), note);
  }
}

const body = (...children) => h("div", { class: "preview-body" }, children);

const again = (actions, busy) => h("button", {
  type: "button", class: "ghost-btn pv-btn", onclick: actions.previewForget, disabled: busy,
}, "Back to the start");

function exitLine(preview) {
  const code = preview.run ? preview.run.exit_code : null;
  if (preview.status === "failed") return code == null ? "It failed." : `It failed (exit ${code}).`;
  return code ? `It ended (exit ${code}).` : "It ended.";
}

// A run that prints rather than serves: its output, as it comes.
function output(preview, headline, ...rest) {
  const lines = (preview.run && preview.run.lines) || [];
  return h("div", { class: "preview-body is-output" },
    h("p", { class: "pv-text" }, headline),
    h("pre", { class: "pv-out" }, lines.length ? lines.join("\n") : "(nothing printed yet)"),
    rest);
}
