/* Shared resource rendering. State and operations belong to the existing workspace. */
import { h } from "../dom.js";

const STATUS = { ready: "Ready", acquiring: "Acquiring…", needs_user: "Needs you", failed: "Failed", selected: "Selected", discovered: "Discovered" };
const label = r => STATUS[r.status] || "Unavailable";

export function renderResources(state, actions) {
  const resources = state.project?.resources || [];
  if (!resources.length) return null;
  return h("section", { class: "resource-list", "aria-label": "Resources" },
    h("div", { class: "rail-label" }, "Resources"),
    resources.map(r => h("button", { key: r.id, type: "button", class: "resource-link", onclick: () => actions.openResource(r.id) },
      `${r.kind === "paper" ? "▤" : "▣"} ${r.name} · ${label(r)}`)));
}

export function renderResourcePane(state, actions) {
  const resources = state.project?.resources || [];
  const kind = state.tab === "paper" ? "paper" : "dataset";
  const candidates = resources.filter(r => r.kind === kind);
  const resource = candidates.find(r => r.id === state.resourceId)
    || candidates.find(r => r.id === state.project?.activeDatasetId) || candidates[0];
  const progress = kind === "paper" ? state.paperUpload : state.datasetUpload;
  const fallback = resource?.provenance?.fallbackOf || resource?.metadata?.fallbackOf;
  const title = fallback?.kind === "synthetic_fallback" ? `Synthetic stand-in for ${fallback.title}` : resource?.name || (kind === "paper" ? "Paper" : "Dataset");
  const busy = progress?.busy;
  const upload = file => kind === "paper" ? actions.uploadPaper(file) : actions.uploadDataset(file);
  return h("section", {class: `resource-detail resource-${kind}`, "aria-label":"Resource details", role:"tabpanel",
    ondragover:event=>event.preventDefault(), ondrop:event=>{event.preventDefault();if (!busy) upload(event.dataTransfer?.files?.[0]);}},
    h("div", {class:"resource-header"}, h("h3", {}, title),
      h("label", {class:"ghost-btn dataset-upload-button"}, `Upload ${kind}`,
        h("input", {type:"file", "aria-label":`Upload ${kind}`, accept:kind === "paper" ? ".pdf" : ".csv,.tsv,.parquet,.xlsx,.json,.jsonl,.ndjson",
          disabled:busy || null, onchange:event=>{upload(event.target.files?.[0]);event.target.value="";}}))),
    progress && h("p", {role:progress.error ? "alert" : "status"}, progress.text),
    kind === "paper" ? (resource?.status === "ready"
      ? h("iframe", {key:resource.id,class:"paper-frame",title:resource.name,src:state.resourceUrl}) : null)
      : h("div", {},
        resource?.status === "ready" ? renderSample(resource.metadata?.files?.[0] || {})
          : resource?.status === "acquiring" ? h("p", {role:"status"}, "Preparing dataset…")
          : !progress?.error && h("p", {}, "Upload a dataset to view it here."),
        fallback && h("p", {class:"resource-provenance"},
          `${fallback.kind === "synthetic_fallback" ? "Synthetic stand-in" : "Fallback"} for ${fallback.title}. ${fallback.reason || fallback.access?.reason || ""}`)));
}

function renderSample(file) {
  let rows = file.sample;
  if (!Array.isArray(rows)) {
    try { rows = JSON.parse(file.sampleSummary || "[]"); } catch (_) { rows = []; }
  }
  rows = Array.isArray(rows) ? rows.slice(0, 10) : [];
  const columns = (file.columns || []).slice(0, 20).map(c => c.name);
  if (!rows.length || !columns.length) return null;
  return h("div", { class: "dataset-preview" },
    h("table", {}, h("thead", {}, h("tr", {}, columns.map(c => h("th", {}, c)))),
      h("tbody", {}, rows.map(row => h("tr", {}, columns.map(c => h("td", {}, String(row?.[c] ?? "").slice(0, 240))))))));
}
