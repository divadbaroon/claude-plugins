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

export function renderResourcePane(state) {
  const resource = state.project?.resources?.find(r => r.id === state.resourceId);
  if (!resource) return h("section", { class: "pane is-blank", role: "tabpanel" }, "This resource is no longer available.");
  if (state.tab === "paper" && resource.kind === "paper" && resource.status === "ready") {
    return h("iframe", { key: resource.id, class: "paper-frame", title: resource.name, src: state.resourceUrl });
  }
  const fallback = resource.provenance?.fallbackOf || resource.metadata?.fallbackOf;
  return h("section", { key: resource.id, class: "resource-detail", "aria-label": "Resource details", role: "tabpanel" },
    h("h3", {}, resource.name), h("p", {}, `${resource.kind} · ${label(resource)}`),
    resource.error && h("p", {}, resource.error),
    fallback && h("p", { class: "resource-provenance" },
      `${fallback.kind === "synthetic_fallback" ? "Synthetic stand-in" : "Fallback"} for ${fallback.title}. ${fallback.reason || fallback.access?.reason || ""}`,
      fallback.source?.length ? ` Original source: ${fallback.source.map(l => l.url).join(", ")}` : ""),
    h("p", {}, "Local: " + (resource.access?.localPath || resource.access?.pdf || "Not acquired")),
    (resource.metadata?.files || []).map(f => h("div", {},
      h("p", {}, f.path), h("p", {}, `${f.format || "data"} · ${f.size ?? "Unknown"} bytes · ${f.rowCount ?? "Unknown"} rows · ${f.columns?.length || 0} inspected columns`),
      h("p", {}, (f.columns || []).map(c => `${c.name} (${c.type})`).join(", ")))),
    h("p", {}, "Source: " + (resource.source?.url || resource.source?.objectPath || (fallback?.kind === "synthetic_fallback" ? "Generated stand-in; not research observations" : ""))));
}
