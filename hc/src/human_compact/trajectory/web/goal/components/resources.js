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
  const resource = resources.find(r => r.id === state.resourceId && (state.tab !== "dataset" || r.kind === "dataset"))
    || (state.tab === "dataset" ? resources.find(r => r.id === state.project?.activeDatasetId) || resources.find(r => r.kind === "dataset") : null);
  const upload = state.tab === "dataset" ? renderUpload(state, actions) : null;
  if (!resource && state.tab === "dataset") return h("section", {class:"resource-detail",role:"tabpanel", "aria-label":"Dataset"},
    h("h3",{},"Dataset"), h("p",{},"Upload the data this project will use."), upload);
  if (!resource) return h("section", { class: "pane is-blank", role: "tabpanel" }, "This resource is no longer available.");
  if (state.tab === "paper" && resource.kind === "paper" && resource.status === "ready") {
    return h("iframe", { key: resource.id, class: "paper-frame", title: resource.name, src: state.resourceUrl });
  }
  const fallback = resource.provenance?.fallbackOf || resource.metadata?.fallbackOf;
  return h("section", { key: resource.id, class: "resource-detail", "aria-label": "Resource details", role: "tabpanel" },
    upload,
    resource.kind === "dataset" && (state.project.resources.filter(r => r.kind === "dataset").length > 1) && h("select", {
      "aria-label": "Dataset", onchange: event => actions.openResource(event.target.value), value: resource.id,
    }, state.project.resources.filter(r => r.kind === "dataset").map(r => h("option", { value: r.id }, r.name + (r.id === state.project.activeDatasetId ? " · Active" : "")))),
    h("h3", {}, fallback?.kind === "synthetic_fallback" ? `Synthetic stand-in for ${fallback.title}` : resource.name), h("p", {}, `${resource.kind} · ${label(resource)}${resource.id === state.project?.activeDatasetId ? " · Active" : ""}`),
    resource.error && h("p", {}, resource.error),
    resource.source?.kind === "upload" && h("p",{},`Uploaded original: ${resource.source.originalFilename}`),
    resource.provenance?.replaces?.length && h("p",{class:"resource-provenance"},
      "You supplied this dataset after " + resource.provenance.replaces.map(r => r.fallbackOf?.kind === "synthetic_fallback"
        ? `a synthetic stand-in for ${r.fallbackOf.title}` : r.name).join(", ") + ". Previous resources remain in project history."),
    resource.kind === "dataset" && state.project?.activeDatasetId && resource.id !== state.project.activeDatasetId
      && h("p",{},"Historical resource — Build uses the active dataset."),
    fallback && h("p", { class: "resource-provenance" },
      `${fallback.kind === "synthetic_fallback" ? "Synthetic stand-in" : "Fallback"} for ${fallback.title}. ${fallback.reason || fallback.access?.reason || ""}`,
      fallback.source?.length ? ` Original source: ${fallback.source.map(l => l.url).join(", ")}` : ""),
    h("p", {}, "Local: " + (resource.access?.localPath || resource.access?.pdf || "Not acquired")),
    (resource.status === "ready" ? resource.metadata?.files || [] : []).map((f, index) => h("div", {},
      h("p", {}, f.path), h("p", {}, `${f.format || "data"} · ${f.size ?? "Unknown"} bytes · ${f.rowCount ?? "Unknown"} rows · ${f.columns?.length || 0} inspected columns`),
      h("h4", {}, "Columns"),
      h("p", {}, (f.columns || []).map(c => `${c.name} (${c.type})`).join(", ")), index === 0 && renderSample(f))),
    sourceLink(fallback?.source?.[0]?.url, "Original source ↗"),
    sourceLink(resource.source?.url, "Source ↗"),
    h("p", {}, "Source: " + (resource.source?.url || resource.source?.objectPath || (fallback?.kind === "synthetic_fallback" ? "Generated stand-in; not research observations" : ""))));
}

function renderSample(file) {
  let rows = file.sample;
  if (!Array.isArray(rows)) {
    try { rows = JSON.parse(file.sampleSummary || "[]"); } catch (_) { rows = []; }
  }
  rows = Array.isArray(rows) ? rows.slice(0, 10) : [];
  const columns = (file.columns || []).slice(0, 20).map(c => c.name);
  if (!rows.length || !columns.length) return null;
  return h("div", { class: "dataset-preview" }, h("h4", {}, "Preview"),
    h("table", {}, h("thead", {}, h("tr", {}, columns.map(c => h("th", {}, c)))),
      h("tbody", {}, rows.map(row => h("tr", {}, columns.map(c => h("td", {}, String(row?.[c] ?? "").slice(0, 240))))))));
}

function sourceLink(url, label) {
  try {
    const parsed = new URL(url);
    return ["https:", "http:"].includes(parsed.protocol) && h("p", {}, h("a", { href: parsed.href, target: "_blank", rel: "noopener noreferrer" }, label));
  } catch (_) { return null; }
}


function renderUpload(state, actions) {
  const busy = state.datasetUpload?.busy;
  return h("div", {class:"dataset-upload", ondragover:event=>{event.preventDefault();},
    ondrop:event=>{event.preventDefault();if (!busy) actions.uploadDataset(event.dataTransfer?.files?.[0]);}},
    h("label", {class:"ghost-btn dataset-upload-button"}, "Upload dataset",
      h("input", {type:"file", "aria-label":"Upload dataset", accept:".csv,.tsv,.parquet,.xlsx", disabled:busy || null,
        onchange:event=>{actions.uploadDataset(event.target.files?.[0]);event.target.value="";}})),
    h("span", {}, "or drop a CSV, TSV, Parquet or XLSX file here · up to 50 MB"),
    state.datasetUpload && h("p", {role:state.datasetUpload.error ? "alert" : "status"}, state.datasetUpload.text));
}
