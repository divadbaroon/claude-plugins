/* Chat Markdown is built from DOM nodes. Model/user text never enters an HTML
   parser; unknown markup stays readable text, and links use an explicit scheme
   allowlist. This deliberately implements the chat subset, not raw HTML. */
import { h } from "./dom.js";

export function safeMarkdownHref(value) {
  const href = String(value || "").trim();
  if (!href || /[\s\u0000-\u001f\u007f\\]/u.test(href)) return null;
  if (href.startsWith("#") || (href.startsWith("/") && !href.startsWith("//"))) return href;
  try {
    const url = new URL(href);
    return ["https:", "http:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch (_) { return null; }
}

function closingParen(text, start) {
  let depth = 1;
  for (let i = start; i < text.length; i += 1) {
    if (text[i] === "\\") { i += 1; continue; }
    if (text[i] === "(") depth += 1;
    if (text[i] === ")" && --depth === 0) return i;
  }
  return -1;
}

function inline(text, depth = 0, links = true) {
  if (depth > 12) return [text];
  const nodes = [];
  let plain = "";
  const flush = () => { if (plain) { nodes.push(plain); plain = ""; } };
  for (let i = 0; i < text.length;) {
    if (text[i] === "\\" && /[\\`*_{}[\]()#+.!>~-]/.test(text[i + 1] || "")) {
      plain += text[i + 1]; i += 2; continue;
    }
    if (text[i] === "`") {
      const mark = text.slice(i).match(/^`+/)[0];
      const end = text.indexOf(mark, i + mark.length);
      if (end !== -1) {
        flush(); nodes.push(h("code", {}, text.slice(i + mark.length, end).replace(/\n/g, " ")));
        i = end + mark.length; continue;
      }
    }
    if (links && text[i] === "[") {
      const labelEnd = text.indexOf("](", i + 1);
      const urlEnd = labelEnd === -1 ? -1 : closingParen(text, labelEnd + 2);
      if (urlEnd !== -1) {
        const href = safeMarkdownHref(text.slice(labelEnd + 2, urlEnd));
        if (href) {
          flush(); nodes.push(h("a", {href, target:href.startsWith("#") ? null : "_blank", rel:"noopener noreferrer"},
            inline(text.slice(i + 1, labelEnd), depth + 1, false)));
          i = urlEnd + 1; continue;
        }
      }
    }
    if (links && /https?:\/\//.test(text.slice(i, i + 8)) && (i === 0 || /[\s(]/.test(text[i - 1]))) {
      const match = text.slice(i).match(/^https?:\/\/[^\s<>]+/);
      if (match) {
        let label = match[0].replace(/[.,;:!?]+$/, "");
        while (label.endsWith(")") && (label.match(/\)/g) || []).length > (label.match(/\(/g) || []).length) label = label.slice(0, -1);
        const href = safeMarkdownHref(label);
        if (href) { flush(); nodes.push(h("a", {href, target:"_blank", rel:"noopener noreferrer"}, label)); i += label.length; continue; }
      }
    }
    const emphasis = text.slice(i).match(/^(\*\*\*|___|\*\*|__|\*|_)/);
    if (emphasis && !(text[i] === "_" && /[\p{L}\p{N}]/u.test(text[i - 1] || ""))) {
      const mark = emphasis[0], start = i + mark.length, end = text.indexOf(mark, start);
      if (end > start && !/\s/.test(text[start]) && !/\s/.test(text[end - 1])) {
        flush();
        const contents = inline(text.slice(start, end), depth + 1, links);
        nodes.push(mark.length === 3 ? h("strong", {}, h("em", {}, contents)) : h(mark.length === 2 ? "strong" : "em", {}, contents));
        i = end + mark.length; continue;
      }
    }
    if (text[i] === "\n") { flush(); nodes.push(h("br", {})); i += 1; continue; }
    plain += text[i++];
  }
  flush();
  return nodes;
}

const listItem = line => line.match(/^(\s*)([-+*]|\d+[.)])\s+(.+)$/);
const blockStart = line => /^\s*(```|~~~|#{1,6}\s|>\s?)/.test(line) || Boolean(listItem(line));

function blocks(lines, depth = 0) {
  if (depth > 12) return [h("p", {}, lines.join("\n"))];
  const nodes = [];
  for (let i = 0; i < lines.length;) {
    if (!lines[i].trim()) { i += 1; continue; }
    const fence = lines[i].match(/^\s*(`{3,}|~{3,})([^\s]*)\s*$/);
    if (fence) {
      const code = [], marker = fence[1];
      i += 1;
      while (i < lines.length && !new RegExp(`^\\s*${marker[0]}{${marker.length},}\\s*$`).test(lines[i])) code.push(lines[i++]);
      if (i < lines.length) i += 1;
      nodes.push(h("pre", {}, h("code", {"data-language":fence[2] || null}, code.join("\n")))); continue;
    }
    const heading = lines[i].match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    if (heading) { nodes.push(h(`h${Math.min(heading[1].length + 2, 6)}`, {}, inline(heading[2]))); i += 1; continue; }
    if (/^\s*>/.test(lines[i])) {
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      nodes.push(h("blockquote", {}, blocks(quote, depth + 1))); continue;
    }
    const first = listItem(lines[i]);
    if (first) {
      const ordered = /^\d/.test(first[2]), indent = first[1].length, items = [];
      const list = h(ordered ? "ol" : "ul", {start:ordered && parseInt(first[2], 10) !== 1 ? parseInt(first[2], 10) : null});
      while (i < lines.length) {
        const item = listItem(lines[i]);
        if (!item || item[1].length !== indent || /^\d/.test(item[2]) !== ordered) break;
        const content = [item[3]];
        i += 1;
        while (i < lines.length && lines[i].trim()) {
          const next = listItem(lines[i]);
          const spaces = lines[i].match(/^\s*/)[0].length;
          if (spaces <= indent || (next && next[1].length <= indent)) break;
          content.push(lines[i++].slice(Math.min(spaces, indent + 2)));
        }
        items.push(h("li", {}, blocks(content, depth + 1)));
      }
      list.append(...items); nodes.push(list); continue;
    }
    const paragraph = [lines[i++]];
    while (i < lines.length && lines[i].trim() && !blockStart(lines[i])) paragraph.push(lines[i++]);
    nodes.push(h("p", {}, inline(paragraph.join("\n"))));
  }
  return nodes;
}

export function renderMarkdown(value) {
  return h("div", {class:"chat-markdown"}, blocks(String(value ?? "").replace(/\r\n?/g, "\n").split("\n")));
}
