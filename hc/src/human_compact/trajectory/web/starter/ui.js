// Small optional helpers. All dataset values are untrusted text, never HTML.
export async function loadDataset({offset = 0, limit = 200} = {}) {
  const response = await fetch(`/api/dataset?offset=${encodeURIComponent(offset)}&limit=${encodeURIComponent(limit)}`);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Could not read the active dataset.');
  return result;
}
const element = (tag, text) => { const node = document.createElement(tag); if (text != null) node.textContent = String(text); return node; };
export function selectControl(name, options, onChange) {
  const label = element('label', name), select = element('select');
  for (const option of options) { const item = element('option', option.label ?? option); item.value = String(option.value ?? option); select.append(item); }
  select.addEventListener('change', () => onChange(select.value)); label.append(select); return label;
}
export function renderTable(rows, columns = Object.keys(rows[0] || {})) {
  const wrapper = element('div'), table = element('table'), head = element('thead'), heading = element('tr'), body = element('tbody');
  wrapper.className = 'table-scroll';
  for (const name of columns) { const cell = element('th', name); cell.scope = 'col'; heading.append(cell); }
  head.append(heading);
  for (const row of rows) { const line = element('tr'); for (const name of columns) line.append(element('td', row[name] ?? '')); body.append(line); }
  table.append(head, body); wrapper.append(table); return wrapper;
}
// Callers choose actual field names and ordering; no inferred research semantics.
export function renderTimeline(rows, {time, label}) {
  const list = element('ol'); list.className = 'timeline';
  for (const row of rows) list.append(element('li', `${row[time] ?? ''} — ${row[label] ?? ''}`));
  return list;
}
