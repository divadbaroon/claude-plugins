/* The Live preview pane: a browser frame around the goal's app, or the
   placeholder the design draws until there is one to frame. */

import { h } from "../dom.js";

export function renderPreview(state) {
  const preview = state.preview;
  const dot = () => h("span", { class: "dot", "aria-hidden": "true" });
  return h("section", { key: "pane-preview", class: "pane preview", role: "tabpanel" },
    h("div", { class: "preview-chrome" },
      dot(), dot(), dot(),
      h("span", { class: "preview-url" }, preview ? preview.app : "")),
    preview && preview.url
      ? h("iframe", { class: "preview-frame", src: preview.url, title: preview.app })
      : h("div", { class: "preview-body" },
        preview && h("button", { type: "button", class: "dark-btn" }, preview.placeholder.action),
        preview && h("span", { class: "preview-hint" }, preview.placeholder.hint)));
}
