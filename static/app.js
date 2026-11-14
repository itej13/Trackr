'use strict';
// Timesheet front end. One file, no build step. sw.js caches it so the app opens with no network.

// ---------- helpers ----------
const view = document.getElementById('view');
const nav = document.getElementById('nav');
const who = document.getElementById('who');
const banner = document.getElementById('banner');

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, '');
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el; // text goes in via append(), never innerHTML, so transcripts can't inject markup
}
const TZ = { timeZone: 'Asia/Kolkata' };
const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString('en-IN', { ...TZ, hour: 'numeric', minute: '2-digit' });
const fmtDate = d => new Date(d + 'T12:00:00+05:30').toLocaleDateString('en-IN', { ...TZ, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
const fmtMonth = m => new Date(m + '-15T12:00:00+05:30').toLocaleDateString('en-IN', { ...TZ, month: 'long', year: 'numeric' });
const fmtStamp = ts => new Date(ts * 1000).toLocaleString('en-IN', { ...TZ, dateStyle: 'medium', timeStyle: 'short' });
const shiftMonth = (m, n) => { const [y, mo] = m.split('-').map(Number); return new Date(Date.UTC(y, mo - 1 + n, 1)).toISOString().slice(0, 7); };
const field = (label, input) => h('label', { class: 'field' }, h('span', {}, label), input);

// A button that runs an async action, shows errors, then re-renders (unless the action returns false).
function button(label, fn, cls = '') {
  const b = h('button', { type: 'button', class: cls }, label);
  b.addEventListener('click', async () => {
    b.disabled = true;
    try { if ((await fn()) !== false) route(); }
    catch (e) { if (e.status !== 401) alert(e.message); }
    finally { b.disabled = false; }
  });
  return b;
}

// ---------- server ----------
let me = null;
try { me = JSON.parse(localStorage.getItem('ts_me')); } catch {}

function setMe(u) {
  me = u;
  try { u ? localStorage.setItem('ts_me', JSON.stringify(u)) : localStorage.removeItem('ts_me'); } catch {}
  renderChrome();
}

async function api(path, { method = 'GET', json, body, headers = {}, timeout = 15000 } = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  let res;
  try {
    res = await fetch(path, {
      method, signal: ctl.signal, credentials: 'same-origin',
      body: json ? JSON.stringify(json) : body,
      headers: json ? { 'Content-Type': 'application/json', ...headers } : headers,
    });
  } catch {
    banner.hidden = false;
    throw Object.assign(new Error("Can't reach the server"), { offline: true });
  } finally { clearTimeout(timer); }
  banner.hidden = true;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && path !== '/api/login') { setMe(null); route(); }
    throw Object.assign(new Error(data.error || `Error ${res.status}`), { status: res.status });
  }
  return data;
}

// ---------- offline recording queue (IndexedDB) ----------
const idb = new Promise((resolve, reject) => {
  const r = indexedDB.open('timesheet', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('queue', { keyPath: 'id' });
  r.onsuccess = () => resolve(r.result);
  r.onerror = () => reject(r.error);
});
async function store(mode, fn) {
  const db = await idb;
  return new Promise((resolve, reject) => {
    const tx = db.transaction('queue', mode);
    const req = fn(tx.objectStore('queue'));
    tx.oncomplete = () => resolve(req.result);
    tx.onerror = () => reject(tx.error);
  });
}
const queueAdd = item => store('readwrite', s => s.put(item));
const queueDel = id => store('readwrite', s => s.delete(id));
const queueMine = async () => me ? (await store('readonly', s => s.getAll())).filter(it => it.userId === me.id) : [];

let flushing = false;
async function flushQueue() {
  if (flushing || !me) return;
  flushing = true;
  let sent = 0;
  try {
    for (const it of await queueMine()) {
      try {
        await api('/api/entries', {
          method: 'POST', body: it.buf, timeout: 180000,
          headers: { 'Content-Type': it.type || 'application/octet-stream', 'X-Client-Id': it.id, 'X-Recorded-At': String(it.recordedAt) },
        });
      } catch (e) {
        if (e.offline || e.status === 401 || e.status === 429 || e.status >= 500) break; // keep it, try later
        alert(`A recording was refused by the server: ${e.message}`); // retrying won't help, so drop it
      }
      await queueDel(it.id);
      sent++;
    }
  } finally { flushing = false; }
  if (sent && onToday()) route();
}
window.addEventListener('online', flushQueue);
document.addEventListener('visibilitychange', () => document.visibilityState === 'visible' && flushQueue());
setInterval(flushQueue, 20000);

// ---------- recorder (one instance, kept alive across re-renders) ----------
const recorder = (() => {
  const btn = h('button', { type: 'button', class: 'rec', 'aria-label': 'Start recording' }, h('span', { class: 'dot' }));
  const label = h('p', { class: 'rec-label', 'aria-live': 'polite' }, 'Tap to record');
  const el = h('section', { class: 'card recorder' }, btn, label);
  const MAX_MS = 15 * 60 * 1000;
  let mr = null, tick = null;

  btn.addEventListener('click', () => (mr ? mr.stop() : start()));

  async function start() {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      label.textContent = 'The microphone only works over HTTPS. Ask your admin to set up the certificate (README, "Phones").';
      return;
    }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch { label.textContent = 'Microphone access is blocked. Allow it in the browser settings, then try again.'; return; }
    const chunks = [], startedAt = Date.now(), owner = me.id;
    mr = new MediaRecorder(stream);
    mr.ondataavailable = e => e.data.size && chunks.push(e.data);
    mr.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      const blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
      mr = null;
      clearInterval(tick);
      btn.classList.remove('on');
      btn.setAttribute('aria-label', 'Start recording');
      // Saved on the device first, so nothing is lost if the network drops.
      await queueAdd({ id: crypto.randomUUID(), userId: owner, recordedAt: startedAt, type: blob.type, buf: await blob.arrayBuffer() });
      label.textContent = 'Saved. Tap to record another.';
      route();
      flushQueue();
    };
    mr.start(1000);
    btn.classList.add('on');
    btn.setAttribute('aria-label', 'Stop recording');
    const show = () => {
      const s = Math.floor((Date.now() - startedAt) / 1000);
      label.textContent = `Recording ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}. Tap to stop.`;
      if (Date.now() - startedAt > MAX_MS) mr.stop();
    };
    show();
    tick = setInterval(show, 500);
  }
  return { el };
})();

// ---------- routing ----------
let renderSeq = 0, pollTimer = null;
const onToday = () => !location.hash || location.hash === '#/' || location.hash === '#';
// Polls must not wipe something the user is typing.
const editing = () => [...view.querySelectorAll('textarea, input:not([type=checkbox])')]
  .some(el => el === document.activeElement || el.value !== el.defaultValue);

function renderChrome() {
  const links = !me ? [] : [['#/', 'Today'], ['#/history', 'History'],
    ...(me.is_admin ? [['#/team', 'Team'], ['#/company', 'Company'], ['#/glossary', 'Glossary']] : [])];
  const sect = (location.hash.slice(2).split(/[/?]/)[0]) || '';
  const cur = '#/' + ({ day: 'history' }[sect] ?? sect);
  nav.replaceChildren(...links.map(([href, text]) => h('a', { href, class: href === cur ? 'on' : null }, text)));
  who.replaceChildren(...(me ? [h('span', {}, me.name), h('button', { type: 'button', class: 'link', onclick: logout }, 'Log out')] : []));
}

async function route(poll = false) {
  clearTimeout(pollTimer);
  if (!poll) renderChrome(); // rebuilding the header on every poll would steal keyboard focus
  if (!me) { view.replaceChildren(loginView()); return; }
  const seq = ++renderSeq;
  const [path, qs] = location.hash.slice(1).split('?');
  const p = path.split('/').filter(Boolean), params = new URLSearchParams(qs || '');
  let page;
  try { page = await pick(p, params); }
  catch (e) {
    if (e.status === 401) return;
    page = { node: h('div', { class: 'card' }, h('p', { class: 'error' }, e.offline ? "Can't reach the server right now." : e.message)) };
  }
  if (seq !== renderSeq) return; // the user navigated while this was loading
  if (!(poll && editing())) view.replaceChildren(page.node);
  if (page.poll) pollTimer = setTimeout(() => route(true), 3000);
}

function pick(p, q) {
  if (p[0] === 'history') return monthView(me.id, q.get('m'), false);
  if (p[0] === 'day' && p[1]) return dayView(me.id, p[1]);
  if (me.is_admin) {
    if (p[0] === 'team' && p[1] && p[2]) return dayView(+p[1], p[2]);
    if (p[0] === 'team' && p[1]) return monthView(+p[1], q.get('m'), true);
    if (p[0] === 'team') return teamView();
    if (p[0] === 'company') return companyView(q.get('m'));
    if (p[0] === 'glossary') return glossaryView();
  }
  return todayView();
}

// ---------- shared pieces ----------
const STATUS = { queued: ['Transcribing…', 'wait'], draft: ['Draft, not submitted yet', 'draft'], submitted: ['Submitted', 'ok'], failed: ['Failed', 'bad'] };
const needsPoll = s => s && (s.pending || (s.stale && !s.error));
const hasText = entries => entries.some(e => e.status === 'draft' || e.status === 'submitted');

function summaryCard(title, s, generate) {
  return h('section', { class: 'card' },
    h('div', { class: 'row' }, h('h2', {}, title),
      s.pending ? h('span', { class: 'badge wait' }, 'Writing…') : null,
      generate && s.stale && !s.pending ? button(s.text ? 'Refresh' : 'Generate', generate, 'primary small') : null),
    s.error ? h('p', { class: 'error small' }, s.error) : null,
    s.text ? h('p', { class: 'summary' }, s.text)
      : h('p', { class: 'muted' }, s.pending ? 'The summary is being written on the server…' : s.stale ? 'Not generated yet.' : 'Nothing to summarise yet.'),
    generate && s.stale && s.text && !s.pending ? h('p', { class: 'muted small' }, 'There are newer entries than this summary.') : null,
    s.updated_at ? h('p', { class: 'muted small' }, 'Written ' + fmtStamp(s.updated_at)) : null);
}

function entryCard(e, editable, owner) {
  const [label, tone] = STATUS[e.status];
  const meta = h('div', { class: 'meta' }, owner ? h('span', { class: `badge ${tone}` }, label) : null, fmtTime(e.recorded_at), e.edited ? '· edited' : null);
  if (e.status === 'queued') return h('div', { class: 'card entry' }, meta, h('p', { class: 'muted' }, 'Turning your voice into text. This takes a few seconds to a minute.'));
  if (e.status === 'failed') return h('div', { class: 'card entry' }, meta, h('p', { class: 'error' }, e.error || 'Something went wrong.'),
    h('div', { class: 'actions' }, button('Discard', () => del(e), 'danger')));
  if (e.status === 'draft' && editable) {
    const ta = h('textarea', { rows: 6, 'aria-label': 'Transcript' }, e.transcript);
    return h('div', { class: 'card entry draft' }, meta,
      h('p', { class: 'muted small' }, 'Check the text and fix anything it misheard. The summary below updates when you save.'), ta,
      h('div', { class: 'actions' },
        button('Submit', async () => { await save(e, ta.value); await api(`/api/entries/${e.id}/submit`, { method: 'POST' }); }, 'primary'),
        button('Save changes', () => save(e, ta.value)),
        button('Discard', () => del(e), 'danger')));
  }
  const card = h('div', { class: 'card entry' }, meta, h('p', { class: 'transcript' }, e.transcript));
  if (editable && e.status === 'submitted') {
    card.append(h('div', { class: 'actions' }, button('Edit', () => { editInPlace(card, e); return false; }), button('Delete', () => del(e), 'danger')));
  }
  if (e.original) card.append(h('details', {}, h('summary', {}, 'Text before the edit'), h('p', { class: 'transcript muted' }, e.original)));
  return card;
}

function editInPlace(card, e) {
  const ta = h('textarea', { rows: 6, 'aria-label': 'Transcript' }, e.transcript);
  card.replaceChildren(h('div', { class: 'meta' }, 'Editing'), ta,
    h('div', { class: 'actions' }, button('Save', () => save(e, ta.value), 'primary'), button('Cancel', () => { ta.value = ta.defaultValue; })));
  ta.focus();
}

async function save(e, text) {
  if (text.trim() !== e.transcript) await api(`/api/entries/${e.id}`, { method: 'PATCH', json: { transcript: text } });
}
async function del(e) {
  if (!confirm('Delete this entry? This cannot be undone.')) return false;
  await api(`/api/entries/${e.id}`, { method: 'DELETE' });
}

function monthNav(m, today, href) {
  const next = shiftMonth(m, 1);
  return h('div', { class: 'monthnav' },
    h('a', { href: href(shiftMonth(m, -1)), 'aria-label': 'Previous month' }, '‹'),
    h('strong', {}, fmtMonth(m)),
    next <= today.slice(0, 7) ? h('a', { href: href(next), 'aria-label': 'Next month' }, '›') : h('span', { class: 'muted', 'aria-hidden': 'true' }, '›'));
}

function calendar(d, hrefFor) {
  const [y, mo] = d.month.split('-').map(Number);
  const days = new Date(Date.UTC(y, mo, 0)).getUTCDate();
  const lead = (new Date(Date.UTC(y, mo - 1, 1)).getUTCDay() + 6) % 7; // weeks start on Monday
  const cells = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map(w => h('div', { class: 'dow' }, w));
  for (let i = 0; i < lead; i++) cells.push(h('div'));
  for (let i = 1; i <= days; i++) {
    const date = `${d.month}-${String(i).padStart(2, '0')}`, n = d.days[date] || 0;
    const state = n ? 'has' : date > d.today ? 'future' : date >= d.user.created && date < d.today ? 'missed' : 'none';
    const title = n ? `${n} ${n === 1 ? 'entry' : 'entries'}` : state === 'missed' ? 'No entry' : '';
    const kids = [h('span', {}, i), h('span', { class: 'cnt' }, n ? `${n}` : state === 'missed' ? '–' : '')];
    const cls = `day ${state}${date === d.today ? ' is-today' : ''}`;
    cells.push(n || date === d.today ? h('a', { class: cls, href: hrefFor(date), title, 'aria-label': `${fmtDate(date)}: ${title || 'no entries'}` }, ...kids)
      : h('div', { class: cls, title }, ...kids));
  }
  return h('div', { class: 'stack' }, h('div', { class: 'cal' }, ...cells),
    h('div', { class: 'legend' }, h('span', { class: 'l-has' }, 'Has entries (number = how many)'), h('span', { class: 'l-missed' }, 'No entry')));
}

// ---------- pages ----------
async function todayView() {
  let day = null;
  try { day = await api('/api/day'); } catch (e) { if (!e.offline) throw e; }
  const local = await queueMine();
  if (day && local.length) flushQueue(); // the server is back: send what was recorded offline
  const cards = local.map(it => h('div', { class: 'card entry' },
    h('div', { class: 'meta' }, h('span', { class: 'badge wait' }, 'Waiting to upload'), fmtTime(it.recordedAt / 1000)),
    h('p', { class: 'muted' }, 'Saved on this device. It uploads by itself once the server is reachable.')));
  if (day) cards.push(...day.entries.map(e => entryCard(e, day.editable, true)));
  const node = h('div', { class: 'stack' },
    recorder.el,
    h('p', { class: 'hint' }, "Say what you worked on, in Hindi, English or both. You can record more than once a day. Today's entries can be changed until midnight."),
    ...cards,
    day && hasText(day.entries) ? summaryCard(day.preview ? "Today's summary (preview, includes drafts)" : "Today's summary", day.summary) : null,
    !day ? h('p', { class: 'muted' }, "You're offline. You can still record; it's kept safely on this device.") : null);
  return { node, poll: local.length > 0 || (day && (day.entries.some(e => e.status === 'queued') || needsPoll(day.summary))) };
}

async function monthView(uid, m, asAdmin) {
  const d = await api(`/api/month?user=${uid}&m=${m || ''}`);
  const base = asAdmin ? `#/team/${uid}` : '#/history';
  const node = h('div', { class: 'stack' },
    asAdmin ? h('a', { class: 'back', href: '#/team' }, '← Team') : null,
    h('div', { class: 'row' }, h('h1', {}, asAdmin ? d.user.name : 'History'), asAdmin ? h('span', { class: 'muted' }, d.user.role || 'No role set') : null),
    h('section', { class: 'card stack' }, monthNav(d.month, d.today, mm => `${base}?m=${mm}`),
      calendar(d, date => (asAdmin ? `#/team/${uid}/${date}` : `#/day/${date}`))),
    d.summary ? summaryCard(`${fmtMonth(d.month)} summary`, d.summary, () => api('/api/summaries', { method: 'POST', json: { user: uid, month: d.month } })) : null);
  return { node, poll: d.summary && d.summary.pending };
}

async function dayView(uid, date) {
  const d = await api(`/api/day?user=${uid}&date=${date}`);
  const owner = uid === me.id;
  const back = owner ? `#/history?m=${d.date.slice(0, 7)}` : `#/team/${uid}?m=${d.date.slice(0, 7)}`;
  const node = h('div', { class: 'stack' },
    h('a', { class: 'back', href: back }, '← ' + (owner ? 'History' : d.user.name)),
    h('div', { class: 'row' }, h('h1', {}, fmtDate(d.date)), !owner ? h('span', { class: 'muted' }, `${d.user.name} · ${d.user.role || 'No role set'}`) : null),
    owner && !d.editable && d.entries.length ? h('p', { class: 'muted small' }, 'Past days are read-only.') : null,
    hasText(d.entries) ? summaryCard(d.preview ? 'Summary (preview, includes drafts)' : 'Summary of the day', d.summary)
      : d.entries.length ? null : h('p', { class: 'muted' }, 'No entries on this day.'),
    ...d.entries.map(e => entryCard(e, d.editable, owner)),
    owner ? null : commentsCard(uid, d));
  return { node, poll: needsPoll(d.summary) || d.entries.some(e => e.status === 'queued') };
}

function commentsCard(uid, d) {
  const ta = h('textarea', { rows: 2, placeholder: 'Write a comment. Only admins can see comments.', 'aria-label': 'New comment' });
  return h('section', { class: 'card' }, h('h2', {}, 'Comments'),
    d.comments.length ? d.comments.map(c => h('div', { class: 'comment' }, h('div', { class: 'meta' }, `${c.author} · ${fmtStamp(c.created_at)}`), h('p', { class: 'transcript' }, c.text)))
      : h('p', { class: 'muted small' }, 'No comments yet.'),
    ta, h('div', { class: 'actions' }, button('Add comment', async () => {
      if (!ta.value.trim()) return false;
      await api('/api/comments', { method: 'POST', json: { user: uid, date: d.date, text: ta.value } });
      ta.value = '';
    })));
}

async function teamView() {
  const d = await api('/api/users');
  const active = d.users.filter(u => u.active), inactive = d.users.filter(u => !u.active);
  const node = h('div', { class: 'stack' },
    h('div', { class: 'row' }, h('h1', {}, 'Team'), h('span', { class: 'muted' }, `${active.length} people · ${active.filter(u => u.today_count).length} logged today`)),
    h('datalist', { id: 'roles' }, ...d.roles.map(r => h('option', { value: r }))),
    h('details', { class: 'card' }, h('summary', {}, 'Add a person'), personForm()),
    active.length ? h('section', { class: 'card flush' }, ...active.map(personRow)) : null,
    inactive.length ? h('details', { class: 'card' }, h('summary', {}, `Deactivated (${inactive.length})`), ...inactive.map(personRow)) : null);
  return { node };
}

function personRow(u) {
  return h('div', { class: 'person' + (u.active ? '' : ' off') },
    h('a', { class: 'who', href: `#/team/${u.id}` }, h('strong', {}, u.name),
      h('span', { class: 'muted' }, [u.role || 'No role set', u.username, u.is_admin ? 'admin' : null].filter(Boolean).join(' · '))),
    u.active ? h('span', { class: u.today_count ? 'badge ok' : 'badge' }, u.today_count ? `Today: ${u.today_count}` : 'Nothing yet today') : null,
    h('details', { class: 'edit' }, h('summary', {}, 'Edit'), personForm(u)));
}

function personForm(u) {
  const f = h('form', { class: 'form' },
    field('Name', h('input', { name: 'name', required: true, maxlength: 80, value: u?.name ?? '' })),
    u ? null : field('Login ID', h('input', { name: 'username', required: true, pattern: '[A-Za-z0-9._\\-]{2,32}', autocapitalize: 'none', autocomplete: 'off', spellcheck: 'false' })),
    field('Role', h('input', { name: 'role', list: 'roles', maxlength: 60, value: u?.role ?? '', placeholder: 'e.g. Video editor' })),
    field(u ? 'New password (leave empty to keep the current one)' : 'Password (at least 6 characters)',
      h('input', { name: 'password', type: 'password', minlength: 6, required: !u, autocomplete: 'new-password' })),
    h('label', { class: 'check' }, h('input', { type: 'checkbox', name: 'is_admin', checked: !!u?.is_admin }), "Admin (can see everyone's logs and manage people)"),
    h('div', { class: 'actions' }, h('button', { type: 'submit', class: 'primary' }, u ? 'Save' : 'Add person'),
      u ? button(u.active ? 'Deactivate' : 'Reactivate', async () => {
        if (u.active && !confirm(`Deactivate ${u.name}? They won't be able to log in. Their history stays.`)) return false;
        await api(`/api/users/${u.id}`, { method: 'PATCH', json: { active: !u.active } });
      }, u.active ? 'danger' : '') : null));
  f.addEventListener('submit', async ev => {
    ev.preventDefault();
    const v = Object.fromEntries(new FormData(f));
    v.is_admin = f.elements.is_admin.checked;
    if (u && !v.password) delete v.password;
    try {
      await api(u ? `/api/users/${u.id}` : '/api/users', { method: u ? 'PATCH' : 'POST', json: v });
      f.reset();
      route();
    } catch (e) { alert(e.message); }
  });
  return f;
}

async function companyView(m) {
  const d = await api(`/api/company?m=${m || ''}`);
  const node = h('div', { class: 'stack' },
    h('h1', {}, 'Company'),
    h('section', { class: 'card' }, monthNav(d.month, d.today, mm => `#/company?m=${mm}`)),
    summaryCard(`${fmtMonth(d.month)}: whole team`, d.summary, () => api('/api/summaries', { method: 'POST', json: { month: d.month } })),
    h('p', { class: 'muted small' }, "Built from each person's monthly summary, which are written first if needed. With the whole team this can take several minutes; it runs in the background, so you can leave this page."),
    d.people.length ? h('section', { class: 'card flush' }, ...d.people.map(p => h('a', { class: 'person', href: `#/team/${p.id}?m=${d.month}` },
      h('span', { class: 'who' }, h('strong', {}, p.name), h('span', { class: 'muted' }, p.role || 'No role set')),
      h('span', { class: p.days ? 'badge ok' : 'badge' }, `${p.days} day${p.days === 1 ? '' : 's'} logged`)))) : null);
  return { node, poll: d.summary.pending };
}

async function glossaryView() {
  const d = await api('/api/glossary');
  const ta = h('textarea', { rows: 8, 'aria-label': 'Glossary' }, d.text);
  return { node: h('div', { class: 'stack' }, h('h1', {}, 'Glossary'),
    h('p', { class: 'muted' }, 'Tool names, client and brand names, and terms your team says often, separated by commas. Speech recognition uses this list to spell them right. Keep it under about 100 terms: only the end of a longer list is used.'),
    h('section', { class: 'card' }, ta, h('div', { class: 'actions' }, button('Save', async () => {
      await api('/api/glossary', { method: 'PUT', json: { text: ta.value } });
      ta.defaultValue = ta.value;
    }, 'primary')))) };
}

function loginView() {
  const err = h('p', { class: 'error', hidden: true, role: 'alert' });
  const f = h('form', { class: 'card stack login' }, h('h1', {}, 'Log in'),
    field('Login ID', h('input', { name: 'username', required: true, autocapitalize: 'none', autocomplete: 'username', spellcheck: 'false' })),
    field('Password', h('input', { name: 'password', type: 'password', required: true, autocomplete: 'current-password' })),
    err, h('button', { type: 'submit', class: 'primary' }, 'Log in'));
  f.addEventListener('submit', async ev => {
    ev.preventDefault();
    try {
      const r = await api('/api/login', { method: 'POST', json: Object.fromEntries(new FormData(f)) });
      setMe(r.me);
      location.hash = '#/';
      route();
      flushQueue();
    } catch (e) { err.textContent = e.message; err.hidden = false; }
  });
  return f;
}

async function logout() {
  try { await api('/api/logout', { method: 'POST' }); } catch {}
  setMe(null);
  route();
}

// ---------- start ----------
window.addEventListener('hashchange', () => route());
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
route();
if (me) api('/api/me').then(r => { setMe(r.me); flushQueue(); }).catch(() => {});
