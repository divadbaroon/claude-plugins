/* The goal page's own DOM layer.

   Components build a tree with h() and the page redraws as a pure function
   of state on every change. mount() then morphs the tree already on screen
   toward the new one instead of replacing it: an element the new tree still
   describes -- matched by position, or by key where a list can reorder --
   is kept and patched, so the focus, caret and scroll position the reader
   has in it survive the redraw. */

const PROPS = new WeakMap();

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  const live = {};
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "key") {
      el.setAttribute("data-key", String(value));
    } else if (name === "class") {
      el.className = value;
    } else if (name.startsWith("on") || name === "value"
               || name === "checked" || name === "disabled") {
      // Properties rather than attributes: a handler is a function, and an
      // input's value attribute stops describing it after the first edit.
      el[name] = value;
      live[name] = value;
    } else {
      el.setAttribute(name, value === true ? "" : String(value));
    }
  }
  PROPS.set(el, live);
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined
        || child === false || child === true) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/* An SVG element from its markup, for the few icons the page draws. The
   HTML parser puts it in the SVG namespace, which createElement cannot. */
export function svg(markup) {
  const template = document.createElement("template");
  template.innerHTML = markup.trim();
  return template.content.firstElementChild;
}

export function mount(host, tree) {
  if (host.firstChild) morph(host.firstChild, tree);
  else host.replaceChildren(tree);
}

export function morph(from, to) {
  if (from.nodeType !== to.nodeType || from.nodeName !== to.nodeName
      || keyOf(from) !== keyOf(to)) {
    from.replaceWith(to);
    return to;
  }
  if (from.nodeType === Node.TEXT_NODE) {
    if (from.data !== to.data) from.data = to.data;
    return from;
  }
  if (from.nodeType !== Node.ELEMENT_NODE) return from;
  for (const { name } of Array.from(from.attributes)) {
    if (!to.hasAttribute(name)) from.removeAttribute(name);
  }
  for (const { name, value } of Array.from(to.attributes)) {
    if (from.getAttribute(name) !== value) from.setAttribute(name, value);
  }
  const was = PROPS.get(from) || {};
  const now = PROPS.get(to) || {};
  for (const name of Object.keys(was)) {
    if (name in now) continue;
    from[name] = name.startsWith("on") ? null : name === "value" ? "" : false;
  }
  for (const [name, value] of Object.entries(now)) {
    // An input the reader is typing in already holds the value the state
    // has, so this leaves it alone; only a change made elsewhere lands.
    if (from[name] !== value) from[name] = value;
  }
  PROPS.set(from, now);
  morphChildren(from, to);
  return from;
}

function morphChildren(from, to) {
  const wanted = Array.from(to.childNodes);
  const wantedKeys = new Set(wanted.map(keyOf).filter((key) => key !== null));
  const spare = new Map();
  for (const child of Array.from(from.childNodes)) {
    const key = keyOf(child);
    if (key === null) continue;
    // A keyed row the new tree no longer has goes first, before anything
    // is moved around it: a row deleted above the one being edited must
    // not cost that row its focus.
    if (wantedKeys.has(key)) spare.set(key, child);
    else from.removeChild(child);
  }
  wanted.forEach((want, index) => {
    const key = keyOf(want);
    const at = from.childNodes[index] || null;
    let match = null;
    if (key !== null) {
      match = spare.get(key) || null;
      spare.delete(key);
    } else {
      for (let j = index; j < from.childNodes.length; j += 1) {
        const candidate = from.childNodes[j];
        if (keyOf(candidate) === null && candidate.nodeType === want.nodeType
            && candidate.nodeName === want.nodeName) {
          match = candidate;
          break;
        }
      }
    }
    if (!match) {
      from.insertBefore(want, at);
      return;
    }
    if (match !== at) from.insertBefore(match, at);
    morph(match, want);
  });
  while (from.childNodes.length > wanted.length) from.removeChild(from.lastChild);
}

function keyOf(node) {
  return node.nodeType === Node.ELEMENT_NODE ? node.getAttribute("data-key") : null;
}
