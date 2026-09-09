/* Shared resource rendering. State and operations belong to the existing workspace. */
import { selectedFiles, droppedFiles } from "../dataset-files.js";
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
  const empty = kind === "dataset" && !resource;
  const controls = () => h("div", {class:"resource-upload-actions"},
    h("label", {class:"ghost-btn dataset-upload-button"}, kind === "paper" ? "Upload paper" : empty ? "Upload dataset" : "Choose file",
      h("input", {type:"file", "aria-label":`Upload ${kind}`, accept:kind === "paper" ? ".pdf" : ".csv,.tsv,.parquet,.xlsx,.json,.jsonl,.ndjson",
        disabled:busy || null, onchange:event=>{upload(event.target.files?.[0]);event.target.value="";}})),
    kind === "dataset" && h("button", {type:"button",class:"ghost-btn",disabled:busy || null,onclick:()=>actions.chooseLocalDataset()}, "Choose local folder"),
    kind === "dataset" && h("label", {class:"ghost-btn dataset-upload-button"}, "Choose folder",
      h("input", {type:"file",webkitdirectory:true,multiple:true,"aria-label":"Choose dataset folder",disabled:busy || null,
        onchange:event=>{upload(selectedFiles(event.target.files));event.target.value="";}})));
  const progressMessage = () => progress && h("p", {role:progress.error ? "alert" : "status"}, progress.text);
  return h("section", {class: `resource-detail resource-${kind}${empty ? " is-empty" : ""}`, "aria-label":"Resource details", role:"tabpanel",
    ondragover:event=>event.preventDefault(), ondrop:event=>{event.preventDefault();if(busy)return;if(kind==='paper') upload(event.dataTransfer?.files?.[0]);else droppedFiles(event.dataTransfer).then(upload).catch(actions.datasetUploadError);}},
    empty ? h("div", {class:"dataset-empty-card", "aria-labelledby":"dataset-upload-title"},
      h("span", {class:"dataset-empty-icon", "aria-hidden":"true"}, "+"),
      h("h2", {id:"dataset-upload-title"}, "Add your dataset"),
      h("p", {}, "Upload a file or folder to explore your data here."),
      controls(), h("p", {class:"dataset-drop-hint"}, "Or drop a file or folder here"), progressMessage())
      : [h("div", {class:"resource-header"}, h("h3", {}, title), controls()), progressMessage()],
    kind === "paper" && resource?.status === "ready" && h("div", {class:"paper-view-controls", role:"group", "aria-label":"Paper format"},
      [["pdf", "Original PDF"], ["lines", "Numbered text"]].map(([view, name]) => h("button", {
        type:"button", class:"ghost-btn", "aria-pressed":(state.paperView || "pdf") === view ? "true" : "false",
        onclick:()=>actions.setPaperView(view),
      }, name))),
    kind === "paper" ? (resource?.status === "ready"
      ? h("iframe", {key:resource.id,class:"paper-frame",title:resource.name,src:state.resourceUrl}) : null)
      : !empty && h("div", {},
        resource?.status === "ready" ? h("div", {},
          resource.manifest && h("p", {}, `${resource.manifest.folderCount || 0} folders · ${resource.manifest.fileCount} files · ${((resource.manifest.totalBytes || 0)/1024/1024).toFixed(1)} MB`),
          resource.manifest && h("ul", {}, (resource.manifest.files || []).filter(f=>f.role==='table').slice(0,12).map(f=>h("li",{},f.path))),
          renderSample(resource.metadata?.files?.[0] || {}))
          : resource?.status === "acquiring" ? h("p", {role:"status"}, "Preparing dataset…")
          : !progress?.error && h("p", {}, resource?.error || "Add dataset — Drop a file or folder here."),
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
