/* The reader's level, in the account menu: how technical every prompt is
   pitched, in the four stops reader.py knows, on the bar slider the web
   setup asks it with -- bars that step up, a line, a thumb, and the stops
   named under it. Dragging paints in place; the release, or a click on a
   stop, is what is kept. */

import { h } from "../dom.js";

// The four stops, in reader.LEVELS' order, said the way the setup says them.
export const LEVELS = [
  { key: "plain", label: "Plain", name: "Plain language",
    desc: "Plain words, no jargon, analogies where they help." },
  { key: "some", label: "Some detail", name: "Some technical detail",
    desc: "Uses some technical language when necessary; assumes some familiarity." },
  { key: "full", label: "Technical", name: "Fully technical",
    desc: "Assumes you know the field well; explanations of niche concepts." },
  { key: "expert", label: "Expert", name: "Expert",
    desc: "Terse and precise; uses specific jargon and references advanced concepts." },
];

function indexOf(level) {
  return LEVELS.findIndex((stop) => stop.key === level);
}

// A position on the track (0..1) to the stop it lands in.
function snap(pos) {
  return Math.max(0, Math.min(LEVELS.length - 1, Math.ceil(pos * LEVELS.length) - 1));
}

export function renderExpertise(state, actions) {
  const reader = state.reader;
  const level = reader && reader.profile ? String(reader.profile.level || "") : "";
  const idx = indexOf(level);
  const stop = idx >= 0 ? LEVELS[idx] : null;
  return h("div", { class: "menu-expertise", "data-key": "expertise" },
    h("div", { class: "menu-cap" }, "Expertise"),
    !reader
      ? h("div", { class: "menu-hint" }, "Reading the profile…")
      : [
        h("div", { class: "slider-head" },
          h("div", { class: "slider-name", "data-slider-name": "" },
            stop ? stop.name : "Not set"),
          h("div", { class: "slider-desc", "data-slider-desc": "" },
            stop ? stop.desc : "Pick how technical to be with you.")),
        renderTrack(idx, state, actions),
        h("div", { class: "slider-stops", role: "radiogroup", "aria-label": "Level" },
          LEVELS.map((s, i) => h("button", {
            key: `stop-${s.key}`, type: "button", role: "radio",
            class: "slider-stop", "aria-checked": i === idx ? "true" : "false",
            "data-on": i === idx ? "1" : "0",
            disabled: state.readerBusy || null,
            onclick: () => actions.setLevel(s.key),
          }, s.label))),
        state.readerNote && h("div", { class: "menu-note is-error", role: "alert" }, state.readerNote.text),
      ]);
}

/* The track: bars that grow left to right, filled up to the position; a
   rule with the filled part over it; the thumb. The position while a
   finger is down is the finger's, painted here without a redraw; the
   release snaps to a stop and hands it to the action. */
function renderTrack(idx, state, actions) {
  const n = LEVELS.length;
  const committed = idx >= 0 ? (idx + 1) / n : 0;
  const fills = [];
  const bars = h("div", { class: "slider-bars" },
    LEVELS.map((s, i) => {
      const fill = h("div", { class: "slider-bar-fill" });
      fills.push(fill);
      const bar = h("div", { class: "slider-bar" }, fill);
      bar.style.height = `${25 + 75 * (i / (n - 1))}%`;
      return bar;
    }));
  const lineOn = h("div", { class: "slider-line-on" });
  const thumb = h("div", { class: "slider-thumb" });
  const track = h("div", {
    class: "slider-track", "data-drag": "0", "aria-hidden": "true",
  }, bars, h("div", { class: "slider-line" }), lineOn, thumb);

  function paint(pos) {
    const at = snap(pos);
    fills.forEach((fill, i) => {
      fill.style.width = `${(Math.min(1, Math.max(0, pos * n - i)) * 100).toFixed(1)}%`;
    });
    lineOn.style.width = `${(pos * 100).toFixed(2)}%`;
    thumb.style.left = `${(pos * 100).toFixed(2)}%`;
    const menu = track.closest(".menu-expertise");
    if (!menu) return;
    const name = menu.querySelector("[data-slider-name]");
    const desc = menu.querySelector("[data-slider-desc]");
    if (name) name.textContent = LEVELS[at].name;
    if (desc) desc.textContent = LEVELS[at].desc;
  }
  paint(committed);

  let dragging = false;
  let pos = committed;
  const where = (event) => {
    const box = track.getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  };
  const move = (event) => { if (dragging) { pos = where(event); paint(pos); } };
  const end = () => {
    if (!dragging) return;
    dragging = false;
    track.setAttribute("data-drag", "0");
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", end);
    window.removeEventListener("pointercancel", end);
    actions.setLevel(LEVELS[snap(pos)].key);
  };
  track.onpointerdown = (event) => {
    if (state.readerBusy) return;
    dragging = true;
    track.setAttribute("data-drag", "1");
    pos = where(event);
    paint(pos);
    // On the window, not the track: a finger that has left the track still
    // has to be able to let go, and the release is what commits.
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
  };
  return track;
}
