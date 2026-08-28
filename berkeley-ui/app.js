'use strict';

// Berkeley research directory. Two views over one loaded set of people:
// a ranked search, and a browse-by-department tree. Either one opens the
// same detail drawer.
//
// The exact column names in the published view are not pinned down, so every
// field is read through a list of candidates rather than a single key. A
// column that gets renamed upstream degrades to "missing" instead of breaking
// the page.

const CONFIG_KEY = 'berkeley-directory-config';
const PEOPLE_VIEW = 'berkeley_people';
const PROJECTS_VIEW = 'berkeley_projects';
const SEARCH_RPC = 'berkeley_search';

const FIELDS = {
  id: ['id', 'person_id', 'people_id', 'uuid'],
  name: ['name', 'full_name', 'person_name', 'display_name'],
  role: ['role', 'person_type', 'kind', 'type', 'position_type'],
  title: ['title', 'position', 'job_title'],
  dept: ['department', 'department_name', 'dept', 'dept_name'],
  bio: ['bio', 'research_bio', 'biography', 'description', 'about', 'summary'],
  interests: ['research_interests', 'research_expertise', 'interests', 'expertise'],
  lab: ['lab_name', 'lab'],
  labUrl: ['lab_url', 'laburl', 'lab_website'],
  majors: ['majors', 'major'],
  url: ['source_url', 'profile_url', 'url', 'website'],
  email: ['email', 'email_address'],
  advisor: ['advisor_id', 'advisor', 'supervisor_id'],
  projects: ['projects'],
};

const PROJECT_FIELDS = {
  owner: ['person_id', 'people_id', 'owner_id', 'professor_id', 'student_id', 'researcher_id'],
  title: ['name', 'title', 'project_name', 'project_title'],
  body: ['description', 'summary', 'abstract', 'details'],
  url: ['url', 'project_url', 'link', 'source_url'],
  named: ['formally_named'],
};

// ---------------------------------------------------------------- utilities

const $ = (sel) => document.querySelector(sel);

function pick(row, keys) {
  for (const key of keys) {
    const value = row[key];
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return null;
}

function asList(value) {
  if (value === null || value === undefined) return [];
  if (Array.isArray(value)) return value.filter(Boolean).map(String);
  const text = String(value).trim();
  if (!text) return [];
  // Only split things that read like a list, not a sentence.
  if (text.includes(';')) return text.split(';').map((s) => s.trim()).filter(Boolean);
  if (text.includes(',') && !/[.!?]\s/.test(text)) {
    return text.split(',').map((s) => s.trim()).filter(Boolean);
  }
  return [text];
}

function asText(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.filter(Boolean).join(', ');
  if (typeof value === 'object') return '';
  return String(value);
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function terms(query) {
  return String(query || '')
    .toLowerCase()
    .split(/[^a-z0-9+#-]+/)
    .filter((t) => t.length > 2);
}

function highlight(text, words) {
  const safe = escapeHtml(text);
  if (!words.length) return safe;
  const pattern = new RegExp('(' + words.map(escapeRe).join('|') + ')', 'gi');
  return safe.replace(pattern, '<mark>$1</mark>');
}

function escapeRe(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function excerpt(text, words, length = 260) {
  const body = String(text || '').replace(/\s+/g, ' ').trim();
  if (body.length <= length) return body;
  let start = 0;
  for (const word of words) {
    const at = body.toLowerCase().indexOf(word);
    if (at > -1) { start = Math.max(0, at - 60); break; }
  }
  const cut = body.slice(start, start + length).trim();
  return (start > 0 ? '…' : '') + cut + '…';
}

function normalizeRole(value, row) {
  const text = (asText(value) || '').toLowerCase();
  if (text.includes('prof') || text.includes('faculty') || text.includes('pi')) return 'professor';
  if (text.includes('phd') || text.includes('student') || text.includes('grad')) return 'student';
  // No role column: an advisor pointer is the giveaway for a student.
  if (row && pick(row, FIELDS.advisor)) return 'student';
  return 'other';
}

const ROLE_LABEL = { professor: 'Professor', student: 'PhD student', other: 'Researcher' };

function normalizeUrl(url) {
  const text = String(url || '').trim().replace(/\/+$/, '');
  if (!text) return '';
  return /^https?:\/\//i.test(text) ? text : 'https://' + text;
}

// ------------------------------------------------------------------- config

let config = readConfig();

function readConfig() {
  let stored = null;
  try { stored = JSON.parse(localStorage.getItem(CONFIG_KEY) || 'null'); } catch (err) { stored = null; }
  const fallback = window.BERKELEY_CONFIG || {};
  const url = normalizeUrl((stored && stored.url) || fallback.url || '');
  const anonKey = String((stored && stored.anonKey) || fallback.anonKey || '').trim();
  return { url, anonKey };
}

function writeConfig(url, anonKey) {
  config = { url: normalizeUrl(url), anonKey: String(anonKey).trim() };
  localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
}

function configured() {
  return Boolean(config.url && config.anonKey);
}

// --------------------------------------------------------------------- rest

async function rest(path, options = {}) {
  const response = await fetch(config.url + '/rest/v1/' + path, Object.assign({}, options, {
    headers: Object.assign({
      apikey: config.anonKey,
      Authorization: 'Bearer ' + config.anonKey,
      'Content-Type': 'application/json',
      Accept: 'application/json',
    }, options.headers || {}),
  }));
  if (!response.ok) {
    const body = await response.text();
    const error = new Error(body || response.statusText);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

// --------------------------------------------------------------------- data

const state = {
  people: [],
  byId: new Map(),
  extra: new Map(),
  departments: [],
  view: 'search',
  dept: '',
  query: '',
  rpcArg: null,
  rpcDead: false,
};

function normalizePerson(row, index) {
  const id = pick(row, FIELDS.id);
  const person = {
    key: 'p' + index,
    id: id === null ? null : String(id),
    name: asText(pick(row, FIELDS.name)) || 'Unnamed',
    role: normalizeRole(pick(row, FIELDS.role), row),
    rawRole: asText(pick(row, FIELDS.role)),
    title: asText(pick(row, FIELDS.title)),
    dept: asText(pick(row, FIELDS.dept)) || 'Unlisted',
    bio: asText(pick(row, FIELDS.bio)),
    interests: asText(pick(row, FIELDS.interests)),
    lab: asText(pick(row, FIELDS.lab)),
    labUrl: normalizeUrl(pick(row, FIELDS.labUrl)),
    majors: asList(pick(row, FIELDS.majors)),
    url: normalizeUrl(pick(row, FIELDS.url)),
    email: asText(pick(row, FIELDS.email)),
    advisorId: pick(row, FIELDS.advisor) === null ? null : String(pick(row, FIELDS.advisor)),
    projects: [],
    row,
  };
  const inline = pick(row, FIELDS.projects);
  if (Array.isArray(inline)) person.projects = inline.map(normalizeProject).filter(Boolean);
  person.haystack = [
    person.name, person.dept, person.title, person.bio, person.interests,
    person.lab, person.majors.join(' '), person.rawRole,
  ].join(' ').toLowerCase();
  return person;
}

function normalizeProject(row) {
  if (!row) return null;
  if (typeof row === 'string') return { title: row, body: '', url: '' };
  return {
    title: asText(pick(row, PROJECT_FIELDS.title)) || 'Untitled project',
    body: asText(pick(row, PROJECT_FIELDS.body)),
    url: normalizeUrl(pick(row, PROJECT_FIELDS.url)),
  };
}

async function loadPeople() {
  const rows = await rest(PEOPLE_VIEW + '?select=*&limit=2000');
  state.people = rows.map(normalizePerson);
  state.byId = new Map();
  state.extra = new Map();
  state.rpcArg = null;
  state.rpcDead = false;
  for (const person of state.people) {
    if (person.id) state.byId.set(person.id, person);
  }
  const counts = new Map();
  for (const person of state.people) {
    counts.set(person.dept, (counts.get(person.dept) || 0) + 1);
  }
  state.departments = [...counts.entries()]
    .map(([name, count]) => ({ name, count }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

// Projects live in their own view if one was published; otherwise the people
// rows may already carry them inline, and otherwise there are simply none.
async function loadProjects() {
  let rows;
  try {
    rows = await rest(PROJECTS_VIEW + '?select=*&limit=5000');
  } catch (err) {
    return;
  }
  for (const row of rows) {
    const owner = pick(row, PROJECT_FIELDS.owner);
    if (owner === null) continue;
    const person = state.byId.get(String(owner));
    if (!person) continue;
    const project = normalizeProject(row);
    person.projects.push(project);
    person.haystack += ' ' + (project.title + ' ' + project.body).toLowerCase();
  }
}

// The published RPC does the ranked full-text work. Its parameter name is not
// pinned down either, so the first name that the server accepts is kept.
async function searchServer(query) {
  if (state.rpcDead) return null;
  const names = state.rpcArg ? [state.rpcArg] : ['q', 'query', 'search_query', 'term', 'search', 'q_text'];
  for (const name of names) {
    try {
      const rows = await rest('rpc/' + SEARCH_RPC, {
        method: 'POST',
        body: JSON.stringify({ [name]: query }),
      });
      state.rpcArg = name;
      return Array.isArray(rows) ? rows : [];
    } catch (err) {
      // PostgREST answers 404 both for "no such function" and for "no
      // function with that argument name", so a 404 is not a reason to stop
      // trying the other candidates.
    }
  }
  state.rpcDead = true;
  return null;
}

// Map a search hit back onto a loaded person so the drawer has the full record.
function resolveHit(row, index) {
  const id = pick(row, FIELDS.id);
  if (id !== null && state.byId.has(String(id))) return state.byId.get(String(id));
  const name = asText(pick(row, FIELDS.name));
  if (name) {
    const match = state.people.find((person) => person.name === name);
    if (match) return match;
  }
  // A hit the loaded set does not cover: keep it reachable by key so the card
  // it draws still opens a drawer.
  const loose = normalizePerson(row, state.people.length + index);
  state.extra.set(loose.key, loose);
  return loose;
}

// ------------------------------------------------------------------ filters

function filters() {
  return {
    role: $('#f-role').value,
    dept: $('#f-dept').value,
    interest: $('#f-interest').value.trim().toLowerCase(),
  };
}

function keep(person, active) {
  if (active.role && person.role !== active.role) return false;
  if (active.dept && person.dept !== active.dept) return false;
  if (active.interest && !person.haystack.includes(active.interest)) return false;
  return true;
}

// ------------------------------------------------------------------ drawing

function personCard(person, words) {
  const bits = [person.title, person.dept, person.lab].filter(Boolean);
  const body = person.bio || person.interests;
  return `
    <li class="card" data-key="${escapeHtml(person.key)}">
      <div class="card-top">
        <span class="name">${highlight(person.name, words)}</span>
        <span class="pill ${person.role}">${ROLE_LABEL[person.role]}</span>
      </div>
      ${bits.length ? `<div class="meta">${highlight(bits.join(' · '), words)}</div>` : ''}
      ${body ? `<p class="snip">${highlight(excerpt(body, words), words)}</p>` : ''}
      ${person.majors.length ? `<div class="tags">${person.majors.slice(0, 6)
        .map((m) => `<span class="tag">${escapeHtml(m)}</span>`).join('')}</div>` : ''}
    </li>`;
}

function drawResults(people, words, note) {
  $('#search-status').textContent = note;
  $('#results').innerHTML = people.length
    ? people.map((person) => personCard(person, words)).join('')
    : '<li class="empty">Nothing matched. Try fewer words, or clear the filters.</li>';
}

async function runSearch() {
  if (!state.people.length) return;
  const active = filters();
  const query = state.query.trim();
  const words = terms(query);

  if (!query) {
    const shown = state.people.filter((person) => keep(person, active));
    drawResults(shown.slice(0, 200), [], `${shown.length} of ${state.people.length} people` +
      (shown.length > 200 ? ' — showing the first 200' : ''));
    return;
  }

  $('#search-status').textContent = 'Searching…';
  const rows = await searchServer(query);
  let hits;
  let how;
  if (rows) {
    hits = rows.map(resolveHit);
    how = 'ranked by relevance';
  } else {
    hits = state.people.filter((person) => words.every((word) => person.haystack.includes(word)));
    how = 'matched here in the browser';
  }
  const shown = hits.filter((person) => keep(person, active));
  drawResults(shown.slice(0, 200), words,
    `${shown.length} result${shown.length === 1 ? '' : 's'} for “${query}” — ${how}`);
}

function drawDepartments() {
  const all = `<li data-dept="" class="${state.dept ? '' : 'is-on'}">
      <span>All departments</span><span class="n">${state.people.length}</span></li>`;
  $('#dept-list').innerHTML = all + state.departments.map((dept) => `
    <li data-dept="${escapeHtml(dept.name)}" class="${dept.name === state.dept ? 'is-on' : ''}">
      <span>${escapeHtml(dept.name)}</span><span class="n">${dept.count}</span>
    </li>`).join('');
}

function drawBrowsePeople() {
  const needle = $('#people-filter').value.trim().toLowerCase();
  const shown = state.people.filter((person) => (
    (!state.dept || person.dept === state.dept) &&
    (!needle || person.name.toLowerCase().includes(needle))
  ));
  $('#people-head').textContent = state.dept || 'Everyone';

  const groups = [
    ['Professors', shown.filter((p) => p.role === 'professor')],
    ['PhD students', shown.filter((p) => p.role === 'student')],
    ['Other', shown.filter((p) => p.role === 'other')],
  ].filter(([, list]) => list.length);

  $('#people-list').innerHTML = groups.length
    ? groups.map(([label, list]) => `
        <div class="group">
          <h3>${label} · ${list.length}</h3>
          <ul class="results">${list.map((person) => personCard(person, [])).join('')}</ul>
        </div>`).join('')
    : '<p class="empty">No one here yet.</p>';
}

// -------------------------------------------------------------------- detail

function openDetail(person) {
  const advisor = person.advisorId ? state.byId.get(person.advisorId) : null;
  const advisees = person.id
    ? state.people.filter((other) => other.advisorId === person.id)
    : [];

  const rows = [];
  if (person.dept) rows.push(['Department', escapeHtml(person.dept)]);
  if (person.rawRole) rows.push(['Role', escapeHtml(person.rawRole)]);
  if (person.lab) {
    rows.push(['Lab', person.labUrl
      ? `<a href="${escapeHtml(person.labUrl)}" target="_blank" rel="noopener">${escapeHtml(person.lab)}</a>`
      : escapeHtml(person.lab)]);
  }
  if (person.majors.length) rows.push(['Majors', escapeHtml(person.majors.join(', '))]);
  if (person.email) rows.push(['Email', `<a href="mailto:${escapeHtml(person.email)}">${escapeHtml(person.email)}</a>`]);
  if (advisor) {
    rows.push(['Advisor', `<button type="button" class="linkish" data-key="${escapeHtml(advisor.key)}">${escapeHtml(advisor.name)}</button>`]);
  }

  const sections = [];
  if (rows.length) {
    sections.push(`<section><h3>Details</h3><dl class="rows">${rows
      .map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join('')}</dl></section>`);
  }
  if (person.bio) {
    sections.push(`<section><h3>Research bio</h3><p>${escapeHtml(person.bio)}</p></section>`);
  }
  if (person.interests && person.interests !== person.bio) {
    sections.push(`<section><h3>Research interests</h3><p>${escapeHtml(person.interests)}</p></section>`);
  }
  if (person.projects.length) {
    sections.push(`<section><h3>Projects · ${person.projects.length}</h3><ul class="projects">${person.projects
      .map((project) => `<li>
        <div class="p-title">${project.url
          ? `<a href="${escapeHtml(project.url)}" target="_blank" rel="noopener">${escapeHtml(project.title)}</a>`
          : escapeHtml(project.title)}</div>
        ${project.body ? `<div class="p-body">${escapeHtml(project.body)}</div>` : ''}
      </li>`).join('')}</ul></section>`);
  }
  if (advisees.length) {
    sections.push(`<section><h3>Advises · ${advisees.length}</h3><ul class="projects">${advisees
      .map((student) => `<li><button type="button" class="linkish" data-key="${escapeHtml(student.key)}">${escapeHtml(student.name)}</button></li>`)
      .join('')}</ul></section>`);
  }
  if (person.url) {
    sections.push(`<section><h3>Source</h3><p><a href="${escapeHtml(person.url)}" target="_blank" rel="noopener">${escapeHtml(person.url)}</a></p></section>`);
  }

  const sub = [person.title, ROLE_LABEL[person.role], person.dept].filter(Boolean).join(' · ');
  $('#detail').innerHTML = `
    <button type="button" class="close" aria-label="Close">&times;</button>
    <h2>${escapeHtml(person.name)}</h2>
    <p class="sub">${escapeHtml(sub)}</p>
    ${sections.join('')}`;
  $('#detail').hidden = false;
  $('#scrim').hidden = false;
  $('#detail').scrollTop = 0;
}

function closeDetail() {
  $('#detail').hidden = true;
  $('#scrim').hidden = true;
}

function byKey(key) {
  return state.people.find((person) => person.key === key) || state.extra.get(key) || null;
}

function personFor(element) {
  const host = element.closest('[data-key]');
  return host ? byKey(host.dataset.key) : null;
}

// --------------------------------------------------------------------- views

function showView(name) {
  state.view = name;
  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('is-on', tab.dataset.view === name);
  }
  $('#view-search').hidden = name !== 'search';
  $('#view-browse').hidden = name !== 'browse';
}

function showNotice(title, body, actionLabel, action) {
  const notice = $('#notice');
  notice.hidden = false;
  notice.classList.toggle('bad', Boolean(actionLabel));
  $('#notice-title').textContent = title;
  $('#notice-body').textContent = body;
  const button = $('#notice-action');
  button.hidden = !actionLabel;
  if (actionLabel) {
    button.textContent = actionLabel;
    button.onclick = action;
  }
}

function hideNotice() {
  $('#notice').hidden = true;
}

// ---------------------------------------------------------------- settings

function openSettings() {
  $('#s-url').value = config.url;
  $('#s-key').value = config.anonKey;
  $('#settings').showModal();
}

// ------------------------------------------------------------------- wiring

function wire() {
  $('#tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (tab) showView(tab.dataset.view);
  });

  $('#search-form').addEventListener('submit', (event) => {
    event.preventDefault();
    state.query = $('#q').value;
    runSearch();
  });

  let typing;
  $('#q').addEventListener('input', () => {
    clearTimeout(typing);
    typing = setTimeout(() => {
      state.query = $('#q').value;
      runSearch();
    }, 260);
  });

  for (const id of ['#f-role', '#f-dept', '#f-interest']) {
    $(id).addEventListener('input', () => runSearch());
  }

  $('#f-clear').addEventListener('click', () => {
    $('#f-role').value = '';
    $('#f-dept').value = '';
    $('#f-interest').value = '';
    $('#q').value = '';
    state.query = '';
    runSearch();
  });

  $('#results').addEventListener('click', (event) => {
    const person = personFor(event.target);
    if (person) openDetail(person);
  });

  $('#dept-list').addEventListener('click', (event) => {
    const item = event.target.closest('li');
    if (!item) return;
    state.dept = item.dataset.dept;
    drawDepartments();
    drawBrowsePeople();
  });

  $('#people-filter').addEventListener('input', () => drawBrowsePeople());

  $('#people-list').addEventListener('click', (event) => {
    const person = personFor(event.target);
    if (person) openDetail(person);
  });

  $('#detail').addEventListener('click', (event) => {
    if (event.target.closest('.close')) return closeDetail();
    const link = event.target.closest('.linkish');
    if (link) {
      const person = byKey(link.dataset.key);
      if (person) openDetail(person);
    }
  });

  $('#scrim').addEventListener('click', closeDetail);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('#detail').hidden) closeDetail();
  });

  $('#settings-btn').addEventListener('click', openSettings);
  $('#s-cancel').addEventListener('click', () => $('#settings').close());

  $('#settings-form').addEventListener('submit', () => {
    writeConfig($('#s-url').value, $('#s-key').value);
    start();
  });
}

// --------------------------------------------------------------------- start

async function start() {
  if (!configured()) {
    showNotice(
      'Not connected yet',
      'Point the directory at the Supabase project holding the Berkeley data — the project URL and its anon (public) key.',
      'Add the connection', openSettings);
    return;
  }

  showNotice('Loading', 'Fetching the directory…', '', null);
  try {
    await loadPeople();
  } catch (err) {
    showNotice(
      'Could not reach the directory',
      `${PEOPLE_VIEW} did not answer (${err.status || 'network error'}). Check the project URL, the anon key, and that the view is exposed.`,
      'Edit the connection', openSettings);
    return;
  }

  await loadProjects();
  hideNotice();

  $('#counts').textContent = `${state.people.length} people · ${state.departments.length} departments`;
  $('#f-dept').innerHTML = '<option value="">All departments</option>' + state.departments
    .map((dept) => `<option value="${escapeHtml(dept.name)}">${escapeHtml(dept.name)} (${dept.count})</option>`)
    .join('');

  drawDepartments();
  drawBrowsePeople();
  runSearch();
}

wire();
start();
