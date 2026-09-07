/* Browser-local geometry; never project state. */
import { h } from "./dom.js";
const key = "engelbart.workspace.layout.v1";
let saved = {};
try { saved = JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch (_) {}
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const mobile = () => matchMedia("(max-width: 700px)").matches;
function persist() { try { localStorage.setItem(key, JSON.stringify(saved)); } catch (_) {} }
export function fitLayout(host = document) {
  const body = host.querySelector(".body"), cols = host.querySelector(".columns.has-todos");
  if (body) {
    const width = clamp(Number(saved.plan) || 340, 180, Math.max(180, Math.min(520, body.clientWidth - 480)));
    body.style.setProperty("--rail-width", `${width}px`);
    body.querySelector('[data-resize="plan"]')?.setAttribute("aria-valuenow", Math.round(width));
  }
  if (cols) {
    const min = Math.min(.45, 220 / Math.max(1, cols.clientWidth));
    const split = clamp(Number(saved.split) || .5, min, 1 - min);
    cols.style.setProperty("--bart-fr", `${split}fr`);
    cols.style.setProperty("--todo-fr", `${1-split}fr`);
    cols.querySelector('[data-resize="split"]')?.setAttribute("aria-valuenow", Math.round(split*100));
  }
  for (const input of host.querySelectorAll("textarea.todo-text")) {
    input.style.height = "auto";
    input.style.height = `${input.scrollHeight}px`;
  }
}
export function separator(kind, label) {
  const change = (event, delta) => {
    if (mobile()) return;
    const region = event.currentTarget.parentElement;
    const box = region.getBoundingClientRect();
    if (kind === "plan") saved.plan = clamp(delta == null ? event.clientX-box.left : (Number(saved.plan)||340)+delta,
      180, Math.max(180, Math.min(520, box.width-480)));
    else { const min=Math.min(.45,220/box.width); saved.split=clamp(delta == null ? (event.clientX-box.left)/box.width : (Number(saved.split)||.5)+delta/box.width,min,1-min); }
    persist(); fitLayout();
  };
  return h("div", { class: `divider resize-divider resize-${kind}`, "data-resize":kind,
    role:"separator", "aria-label":label, "aria-orientation":"vertical", tabindex:"0",
    onpointerdown:event => { if (mobile()) return; event.preventDefault(); event.currentTarget.setPointerCapture(event.pointerId); document.documentElement.classList.add("resizing"); },
    onpointermove:event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) change(event); },
    onpointerup:event => { if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); document.documentElement.classList.remove("resizing"); },
    onlostpointercapture:() => document.documentElement.classList.remove("resizing"),
    ondblclick:() => { delete saved[kind]; persist(); fitLayout(); },
    onkeydown:event => { if (["ArrowLeft","ArrowRight"].includes(event.key)) { event.preventDefault(); change(event,event.key==="ArrowLeft"?-20:20); } },
  });
}
