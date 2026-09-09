/* The artifact replaces its document during boot; attach after it finishes. */
function mountCutout() {
  const sheet = document.createElement('link');
  sheet.rel = 'stylesheet';
  sheet.href = '/cutout.css';
  document.head.appendChild(sheet);
  document.title = 'Legacy TODO rail · Engelbart reference';
  window.__hcPromptUI.setRailHidden('right', false);
  window.__hcPromptUI.tour.close();
  const head = document.createElement('header');
  head.className = 'rail-demo-head';
  const name = document.createElement('strong');
  name.textContent = 'Legacy TODO rail';
  const guide = document.createElement('a');
  guide.href = '/README.md';
  guide.target = '_blank';
  guide.textContent = 'Handoff notes';
  head.append(name, guide);
  const foot = document.createElement('footer');
  foot.className = 'rail-demo-foot';
  const mode = document.createElement('span');
  mode.textContent = 'Editable demo · builds are simulated';
  const keys = document.createElement('span');
  keys.textContent = 'Enter: add · Tab: indent · ⌘↵: build';
  foot.append(mode, keys);
  document.body.append(head, foot);
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', mountCutout);
} else {
  mountCutout();
}
