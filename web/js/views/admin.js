/**
 * views/admin.js - the `#/admin[/<tab>]` screen (SPEC 9.2 "Admin", 9.10 "Onboarding", 7.4 `admin.*`),
 * for workspace admins only:
 *   Users      people table (search, make/remove admin, enable/disable, reset password), "Create user" and
 *              "Add employees" (paced bulk creation with generated temporary passwords)
 *   Workspace  name, registration switch + join code, invitation text with the LAN links, the
 *              notification-policy card for plain http
 *   Stats      counters, disk, backup, uptime, links
 *   Audit log  the newest 100 admin actions
 * The server re-checks the role on every call (`forbidden`); this view only avoids showing the tools.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { userAvatar } from '../core/avatar.js';
import { store } from '../core/store.js';
import { socket } from '../core/socket.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { copyToClipboard, cpLength, debounce, errorText, fold, formatBytes, formatDateTime, formatLastSeen, runPaced, serverNow } from '../core/util.js';
import { createField, describeError } from './auth.js';
import { mountTabbed, switchRow } from './settings.js';
import { openDirect } from './newchat.js';

const USERNAME_RE = /^[a-z0-9._-]{3,32}$/;
const PASSWORD_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
const CREATE_GAP_MS = 2100;
const MAX_BULK = 300;
const ROWS_PER_PAGE = 100;

/* ------------------------------------------------------------------------------------------ */
/* Helpers                                                                                    */
/* ------------------------------------------------------------------------------------------ */

/**
 * A temporary password: 10 characters from an alphabet without look-alikes, drawn with the CSPRNG
 * (rejection sampling, so every character is uniform) and containing a lower-case letter, an upper-case
 * letter and a digit.
 * @param {number} [length]
 * @returns {string}
 */
function generatePassword(length = 10) {
  const n = PASSWORD_ALPHABET.length;
  const limit = 256 - (256 % n);
  const buf = new Uint8Array(32);
  for (;;) {
    let out = '';
    while (out.length < length) {
      crypto.getRandomValues(buf);
      for (const b of buf) {
        if (b < limit && out.length < length) out += PASSWORD_ALPHABET[b % n];
      }
    }
    if (/[a-z]/.test(out) && /[A-Z]/.test(out) && /\d/.test(out)) return out;
  }
}

/**
 * @param {string} type a SPEC 7.4 admin request
 * @param {object} [d]
 * @returns {Promise<any>}
 */
function ask(type, d = {}) {
  return socket.request(type, d);
}

/**
 * @param {number|null|undefined} id
 * @returns {string} "Display Name" of a user id, or a fallback
 */
function nameOf(id) {
  if (id === null || id === undefined) return '';
  const u = store.getUser(id);
  return u ? u.display_name : `User #${id}`;
}

/**
 * "3 min ago" style relative time.
 * @param {number} ts epoch seconds
 * @returns {string}
 */
function ago(ts) {
  const s = Math.max(0, serverNow() - ts);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

/**
 * @param {number} seconds
 * @returns {string} "3 d 4 h" / "2 h 5 min" / "5 min"
 */
function formatUptime(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const hr = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} d ${hr} h`;
  if (hr > 0) return `${hr} h ${m} min`;
  return `${Math.max(1, m)} min`;
}

/**
 * A button that copies text (re-evaluated on click) and says so.
 * @param {() => string} getText
 * @param {string} label
 * @param {{small?: boolean}} [o]
 * @returns {HTMLElement}
 */
function copyButton(getText, label, o = {}) {
  return h(`button.btn.btn-secondary${o.small === false ? '' : '.btn-sm'}`, {
    type: 'button',
    onClick: async () => {
      const ok = await copyToClipboard(getText());
      ui.toast(ok ? 'Copied.' : 'Could not copy automatically - select the text and copy it yourself.', { type: ok ? 'success' : 'error' });
    },
  }, icon('copy', { size: 16 }), label);
}

/**
 * Dialog that hands over a username and a temporary password (create / reset).
 * @param {{title: string, display_name: string, username: string, password: string}} o
 * @returns {Promise<any>}
 */
function showCredentials(o) {
  const text = () => `Sign in to ${store.workspace.name}: ${location.origin}\nUsername: ${o.username}\nTemporary password: ${o.password}\nYou will be asked to choose your own password.`;
  const dlg = ui.dialog({
    title: o.title,
    size: 'sm',
    content: h('div.ad-cred',
      h('p', `Give these sign-in details to ${o.display_name}. The password is shown only now.`),
      h('dl.ad-cred-list', h('dt', 'Username'), h('dd.mono', o.username), h('dt', 'Temporary password'), h('dd.mono.ad-cred-pw', o.password)),
      h('p.hint', 'They will be asked to choose their own password the first time they sign in.')),
    actions: [{ label: 'Done', value: true, primary: true }],
  });
  dlg.panel.querySelector('.dialog-actions').prepend(copyButton(text, 'Copy details', { small: false }));
  return dlg.closed;
}

/* ------------------------------------------------------------------------------------------ */
/* Users                                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {{timers: Set<any>, isAlive: () => boolean}} env
 * @returns {{el: HTMLElement, destroy: () => void}}
 */
function usersPanel(env) {
  /** @type {Map<number, any>} */
  const rows = new Map();
  let query = '';
  let shown = ROWS_PER_PAGE;
  let phase = 'loading';
  let failure = '';
  let lastReload = 0;

  const search = /** @type {HTMLInputElement} */ (h('input.input.ad-search', {
    type: 'search', placeholder: 'Search people', 'aria-label': 'Search people', autocomplete: 'off', dir: 'auto',
    onInput: () => {
      query = search.value;
      shown = ROWS_PER_PAGE;
      render();
    },
  }));
  const tbody = h('tbody');
  const head = ['Person', 'Role', 'Status', 'Last sign-in', 'Messages'].map((t) => h('th', { scope: 'col' }, t));
  const table = h('table.ad-table', h('caption.sr-only', 'People'), h('thead', h('tr', ...head, h('th', { scope: 'col' }, h('span.sr-only', 'Actions')))), tbody);
  const note = h('div.ad-note', { role: 'status' });
  const moreBtn = h('button.btn.btn-secondary.btn-sm', { type: 'button', hidden: true, onClick: () => { shown += ROWS_PER_PAGE; render(); } }, 'Show more people');
  const welcome = h('div.banner.banner-info.ad-welcome', { hidden: true },
    icon('info', { size: 18 }),
    h('span.grow', 'You are the only person here so far. Add your colleagues, then share the invitation from the Workspace tab.'),
    h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => router.go('/admin/workspace', { replace: true }) }, 'Invite people'));
  const refreshBtn = h('button.btn-icon', { type: 'button', 'aria-label': 'Refresh the list', title: 'Refresh', onClick: () => load() }, icon('refresh'));
  const toolbar = h('div.ad-toolbar',
    h('div.ad-search-wrap', icon('search', { size: 18 }), search),
    h('div.row.ad-toolbar-actions',
      h('button.btn.btn-primary.btn-sm', { type: 'button', onClick: () => openCreateUser() }, icon('person-add', { size: 18 }), 'Create user'),
      h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => openAddEmployees() }, icon('group-add', { size: 18 }), 'Add employees'),
      refreshBtn));

  function mergeDirectory(row) {
    const u = store.getUser(row.id);
    if (!u) return;
    for (const k of ['username', 'display_name', 'role', 'disabled', 'online', 'last_seen', 'activated', 'status_text']) if (k in u) row[k] = u[k];
  }

  function otherAdmins(exceptId) {
    let n = 0;
    for (const r of rows.values()) if (r.id !== exceptId && r.role === 'admin' && !r.disabled) n += 1;
    return n;
  }

  async function update(row, patch, okText) {
    try {
      const { user } = await ask('admin.update_user', { user_id: row.id, ...patch });
      Object.assign(row, user);
      render();
      ui.toast(okText, { type: 'success' });
    } catch (err) {
      ui.toast(errorText(err, 'Could not change that account.'), { type: 'error' });
    }
  }

  function openMenu(row, anchor) {
    const me = store.me;
    const self = Boolean(me && row.id === me.id);
    const items = [];
    const lastAdminSelf = self && row.role === 'admin' && otherAdmins(row.id) === 0;
    if (!lastAdminSelf) {
      items.push(row.role === 'admin'
        ? { label: 'Remove admin rights', icon: 'shield', onSelect: () => update(row, { role: 'member' }, `${row.display_name} is no longer an admin.`) }
        : { label: 'Make admin', icon: 'shield', onSelect: () => update(row, { role: 'admin' }, `${row.display_name} is now an admin.`) });
    }
    if (!self) {
      if (row.disabled) {
        items.push({ label: 'Enable account', icon: 'check-circle', onSelect: () => update(row, { disabled: false }, `${row.display_name} can sign in again.`) });
      } else {
        items.push({
          label: 'Disable account',
          icon: 'lock',
          danger: true,
          onSelect: async () => {
            const ok = await ui.confirm({
              title: `Disable ${row.display_name}?`,
              message: 'They are signed out everywhere and cannot sign in until you enable the account again. Their messages stay.',
              confirmLabel: 'Disable account',
              danger: true,
            });
            if (ok) update(row, { disabled: true }, `${row.display_name} was disabled.`);
          },
        });
      }
    }
    items.push({ label: 'Reset password…', icon: 'key', onSelect: () => openReset(row) });
    if (!self && !row.disabled) items.push({ label: 'Message', icon: 'chat-outline', onSelect: () => openDirect(row.id) });
    ui.menu(items, { anchor, label: `Actions for ${row.display_name}`, title: row.display_name });
  }

  function statusCell(row) {
    const parts = [];
    if (row.disabled) parts.push(h('span.badge.ad-badge.ad-badge-danger', 'Disabled'));
    else parts.push(h('span.ad-presence', { 'data-online': row.online ? '1' : '0' }, h('span.ad-dot', { 'aria-hidden': 'true' }), row.online ? 'Online' : formatLastSeen(row) || 'Offline'));
    if (!row.activated) parts.push(h('span.badge.ad-badge', 'Never signed in'));
    if (row.must_change_password) parts.push(h('span.badge.ad-badge', 'Temporary password'));
    return parts;
  }

  function buildRow(row) {
    const btn = h('button.btn-icon', {
      type: 'button',
      'aria-label': `Actions for ${row.display_name}`,
      'aria-haspopup': 'menu',
      onClick: () => openMenu(row, btn),
    }, icon('more'));
    return h(`tr${row.disabled ? '.ad-dim' : ''}`,
      h('td.ad-person', { 'data-label': 'Person' }, userAvatar(row, { size: 'sm', online: null }),
        h('span.ad-person-text', h('span.ad-person-name.truncate', { dir: 'auto' }, row.display_name), h('span.ad-person-sub.truncate', `@${row.username}`))),
      h('td', { 'data-label': 'Role' }, row.role === 'admin' ? h('span.badge.badge-unread.ad-badge', 'Admin') : 'Member'),
      h('td.ad-status', { 'data-label': 'Status' }, ...statusCell(row)),
      h('td', { 'data-label': 'Last sign-in' }, row.last_login_at ? formatDateTime(row.last_login_at) : 'Never'),
      h('td', { 'data-label': 'Messages' }, String(row.message_count ?? 0)),
      h('td.ad-actions', btn));
  }

  function render() {
    for (const r of rows.values()) mergeDirectory(r);
    const key = fold(query).trim();
    const all = Array.from(rows.values()).filter((r) => !key || fold(`${r.display_name} ${r.username}`).includes(key));
    all.sort((a, b) => a.display_name.localeCompare(b.display_name));
    const list = all.slice(0, shown);
    tbody.replaceChildren(...list.map(buildRow));
    table.hidden = phase !== 'ready' || list.length === 0;
    moreBtn.hidden = all.length <= shown;
    welcome.hidden = !(phase === 'ready' && rows.size <= 1);
    if (phase === 'loading' && !rows.size) note.replaceChildren(ui.spinner(22));
    else if (phase === 'error') note.replaceChildren(h('p.error-text', failure), h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => load() }, 'Try again'));
    else if (phase === 'ready' && !list.length) note.replaceChildren(h('p.muted', key ? `No people match "${query.trim()}"` : 'No people yet.'));
    else note.replaceChildren(h('p.hint', `${all.length} ${all.length === 1 ? 'person' : 'people'}`));
  }

  async function load() {
    lastReload = Date.now();
    if (!rows.size) phase = 'loading';
    render();
    try {
      const res = await ask('admin.users', {});
      if (!env.isAlive()) return;
      rows.clear();
      for (const u of res.users || []) rows.set(u.id, { ...u });
      phase = 'ready';
    } catch (err) {
      if (!env.isAlive()) return;
      phase = 'error';
      failure = errorText(err, 'Could not load the people list.');
    }
    render();
  }

  const sync = debounce(() => {
    if (!env.isAlive()) return;
    const unknown = store.users().some((u) => !rows.has(u.id));
    if (unknown && Date.now() - lastReload > 5000) load();
    else render();
  }, 500);
  const off = store.on('users', sync);

  /* ---- create one user ---- */

  async function openCreateUser() {
    const display = createField({ name: 'display_name', label: 'Full name', autocomplete: 'off', maxlength: 80 });
    const username = createField({ name: 'username', label: 'Username', autocomplete: 'off', plain: true, maxlength: 64, hint: '3 to 32 letters, digits, dots, dashes or underscores.' });
    const password = createField({ name: 'password', label: 'Temporary password', autocomplete: 'off', plain: true, maxlength: 128, value: generatePassword(), hint: 'They choose their own password the first time they sign in.' });
    const generate = h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => { password.input.value = generatePassword(); } }, 'Generate another');
    const role = h('select.input', { id: 'ad-create-role' }, h('option', { value: 'member' }, 'Member'), h('option', { value: 'admin' }, 'Admin'));
    const error = h('p.error-text', { role: 'alert', hidden: true });
    const fields = [display, username, password];
    /** @type {any} */
    let created = null;
    const dlg = ui.dialog({
      title: 'Create user',
      size: 'sm',
      content: h('div.ad-form', display.el, username.el, h('div.col.gap-2', password.el, generate),
        h('div.field', h('label.label', { for: 'ad-create-role' }, 'Role'), role), error),
      actions: [
        { label: 'Cancel', value: false },
        {
          label: 'Create account',
          primary: true,
          value: true,
          onClick: async (handle) => {
            error.hidden = true;
            for (const f of fields) f.clearError();
            const body = {
              display_name: display.value.trim(),
              username: username.value.trim().toLowerCase(),
              password: password.value,
              role: role.value,
            };
            const problems = {};
            if (!body.display_name) problems.display_name = 'Enter a name.';
            else if (cpLength(body.display_name) > 40) problems.display_name = 'Use at most 40 characters.';
            if (!USERNAME_RE.test(body.username)) problems.username = 'Use 3 to 32 letters, digits, dots, dashes or underscores.';
            if (cpLength(body.password) < 8) problems.password = 'Use at least 8 characters.';
            const bad = fields.find((f) => problems[f.name]);
            if (bad) {
              for (const f of fields) if (problems[f.name]) f.setError(problems[f.name]);
              bad.input.focus();
              return false;
            }
            handle.setBusy(true);
            try {
              const res = await ask('admin.create_user', body);
              created = { user: res.user, password: body.password, body };
            } catch (err) {
              handle.setBusy(false);
              const d = describeError(err);
              const target = fields.find((f) => f.name === d.field);
              if (target) {
                target.setError(d.text);
                target.input.focus();
              } else {
                error.textContent = d.text;
                error.hidden = false;
              }
              return false;
            }
            handle.setBusy(false);
            return undefined;
          },
        },
      ],
    });
    await dlg.closed;
    if (!created) return;
    rows.set(created.user.id, { message_count: 0, created_at: serverNow(), last_login_at: null, must_change_password: true, ...created.user });
    render();
    await showCredentials({ title: 'Account created', display_name: created.user.display_name, username: created.user.username, password: created.password });
  }

  /* ---- reset a password ---- */

  async function openReset(row) {
    const password = createField({ name: 'password', label: 'New temporary password', autocomplete: 'off', plain: true, maxlength: 128, value: generatePassword() });
    const generate = h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => { password.input.value = generatePassword(); } }, 'Generate another');
    const error = h('p.error-text', { role: 'alert', hidden: true });
    let chosen = '';
    const dlg = ui.dialog({
      title: `Reset password for ${row.display_name}`,
      size: 'sm',
      content: h('div.ad-form',
        h('p.muted', 'They are signed out everywhere and must choose a new password the next time they sign in.'),
        h('div.col.gap-2', password.el, generate), error),
      actions: [
        { label: 'Cancel', value: false },
        {
          label: 'Reset password',
          primary: true,
          value: true,
          onClick: async (handle) => {
            error.hidden = true;
            password.clearError();
            const pw = password.value;
            if (cpLength(pw) < 8) {
              password.setError('Use at least 8 characters.');
              password.input.focus();
              return false;
            }
            handle.setBusy(true);
            try {
              await ask('admin.reset_password', { user_id: row.id, new_password: pw });
              chosen = pw;
            } catch (err) {
              handle.setBusy(false);
              const d = describeError(err);
              if (d.field === 'password') {
                password.setError(d.text);
                password.input.focus();
              } else {
                error.textContent = d.text;
                error.hidden = false;
              }
              return false;
            }
            handle.setBusy(false);
            return undefined;
          },
        },
      ],
    });
    await dlg.closed;
    if (!chosen) return;
    row.must_change_password = true;
    render();
    await showCredentials({ title: 'Password reset', display_name: row.display_name, username: row.username, password: chosen });
  }

  /* ---- add many employees ---- */

  /**
   * "Full Name, username" lines.
   * @param {string} text
   * @returns {Array<{name: string, username: string, error: string, status: string, note: string, password: string}>}
   */
  function parseEmployees(text) {
    const seen = new Set();
    const out = [];
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const cut = line.lastIndexOf(',');
      const row = { name: '', username: '', error: '', status: 'waiting', note: '', password: '' };
      if (cut < 0) {
        row.name = line;
        row.error = 'Use "Full Name, username"';
      } else {
        row.name = line.slice(0, cut).trim();
        row.username = line.slice(cut + 1).trim().toLowerCase();
        if (!row.name || cpLength(row.name) > 40) row.error = 'The name must have 1 to 40 characters';
        else if (!USERNAME_RE.test(row.username)) row.error = 'The username needs 3 to 32 letters, digits, dots, dashes or underscores';
        else if (seen.has(row.username)) row.error = 'This username appears twice';
      }
      if (row.username) seen.add(row.username);
      if (row.error) row.status = 'invalid';
      out.push(row);
    }
    return out;
  }

  function openAddEmployees() {
    const ac = new AbortController();
    const text = /** @type {HTMLTextAreaElement} */ (h('textarea.input.ad-emp-text', {
      id: 'ad-emp-text', rows: 7, spellcheck: 'false', placeholder: 'Asha Verma, asha\nRavi Kumar, ravi.kumar', dir: 'auto',
    }));
    const problem = h('p.error-text', { role: 'alert', hidden: true });
    const progress = h('span.hint', { role: 'status' });
    const startBtn = h('button.btn.btn-primary', { type: 'button', onClick: () => start() }, 'Create accounts');
    const stopBtn = h('button.btn.btn-secondary', { type: 'button', hidden: true, onClick: () => { ac.abort(); stopBtn.disabled = true; } }, 'Stop');
    const resultBody = h('tbody');
    const results = h('table.ad-table.ad-results', h('caption.sr-only', 'Accounts'),
      h('thead', h('tr', ...['Name', 'Username', 'Temporary password', 'Result'].map((t) => h('th', { scope: 'col' }, t)))), resultBody);
    const printTitle = h('p.print-only.ad-print-title');
    const copyBtn = copyButton(() => {
      const lines = list.filter((r) => r.status === 'created').map((r) => `${r.name}\t${r.username}\t${r.password}`);
      return [`Sign in at ${location.origin}`, 'Name\tUsername\tTemporary password', ...lines].join('\n');
    }, 'Copy list');
    const printBtn = h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => printResults() }, icon('print', { size: 16 }), 'Print');
    const summary = h('div.ad-emp-results', { hidden: true },
      h('p.banner.banner-warn', 'Temporary passwords are shown only on this screen. Copy or print them now.'),
      h('div.print-area', printTitle, results),
      h('div.row.no-print', copyBtn, printBtn));
    let list = [];
    let running = false;
    let open = true;

    function printResults() {
      printTitle.textContent = `${store.workspace.name} - new accounts - ${location.origin} - ${formatDateTime(serverNow())}`;
      const root = document.documentElement;
      root.classList.add('print-results');
      const done = () => {
        root.classList.remove('print-results');
        window.removeEventListener('afterprint', done);
      };
      window.addEventListener('afterprint', done);
      try {
        window.print();
      } finally {
        setTimeout(done, 1000);
      }
    }

    const STATUS_TEXT = { waiting: 'Waiting', creating: 'Creating…', created: 'Created', exists: 'Already exists', failed: 'Failed', skipped: 'Not created', invalid: 'Skipped' };

    function paint() {
      resultBody.replaceChildren(...list.map((r) => h(`tr.ad-res-${r.status}`,
        h('td', { 'data-label': 'Name', dir: 'auto' }, r.name),
        h('td', { 'data-label': 'Username' }, r.username),
        h('td.mono', { 'data-label': 'Temporary password' }, r.status === 'created' ? r.password : ''),
        h('td', { 'data-label': 'Result' }, STATUS_TEXT[r.status] || r.status, r.note ? ` - ${r.note}` : ''))));
      const done = list.filter((r) => !['waiting', 'creating'].includes(r.status)).length;
      const todo = list.filter((r) => r.status !== 'invalid').length;
      const made = list.filter((r) => r.status === 'created').length;
      progress.textContent = running ? `Creating accounts… ${Math.min(done, list.length)} of ${list.length}` : `${made} of ${todo} accounts created.`;
    }

    async function start() {
      problem.hidden = true;
      list = parseEmployees(text.value);
      if (!list.length) {
        problem.textContent = 'Type or paste at least one line: Full Name, username';
        problem.hidden = false;
        text.focus();
        return;
      }
      if (list.length > MAX_BULK) {
        problem.textContent = `Add at most ${MAX_BULK} people at a time.`;
        problem.hidden = false;
        return;
      }
      const invalid = list.filter((r) => r.error);
      if (invalid.length) {
        problem.textContent = `${invalid.length} ${invalid.length === 1 ? 'line is' : 'lines are'} invalid and will be skipped: ${invalid.slice(0, 3).map((r) => `"${r.name}" (${r.error})`).join('; ')}${invalid.length > 3 ? '…' : ''}`;
        problem.hidden = false;
      }
      for (const r of list) {
        if (r.status === 'waiting') r.password = generatePassword();
        if (r.error) r.note = r.error;
      }
      running = true;
      text.readOnly = true;
      startBtn.hidden = true;
      stopBtn.hidden = false;
      stopBtn.disabled = false;
      summary.hidden = false;
      paint();
      await runPaced(list.filter((r) => r.status === 'waiting'), async (r) => {
        r.status = 'creating';
        paint();
        try {
          return await ask('admin.create_user', { username: r.username, display_name: r.name, password: r.password, role: 'member' });
        } catch (err) {
          if (err && err.reason === 'max_users') ac.abort();
          throw err;
        }
      }, {
        gapMs: CREATE_GAP_MS,
        signal: ac.signal,
        onRow: (row) => {
          const r = row.item;
          if (row.status === 'ok') {
            r.status = 'created';
          } else if (row.status === 'exists') {
            r.status = 'exists';
            r.note = 'that username or name is already used';
          } else {
            r.status = 'failed';
            const code = row.error && row.error.code;
            r.note = code === 'timeout' || code === 'connection_lost'
              ? 'no answer from the server - check whether the account exists'
              : (row.error && row.error.message) || 'unknown error';
          }
          paint();
        },
      });
      for (const r of list) if (r.status === 'waiting' || r.status === 'creating') r.status = 'skipped';
      running = false;
      stopBtn.hidden = true;
      if (open) {
        paint();
        load();
      }
    }

    const dlg = ui.dialog({
      title: 'Add employees',
      size: 'lg',
      className: 'dialog-bulk',
      content: h('div.ad-form',
        h('div.field', h('label.label', { for: 'ad-emp-text' }, 'One person per line: Full Name, username'), text,
          h('p.hint', 'Each person gets a generated temporary password and must choose their own at first sign-in. About 28 accounts are created per minute.')),
        problem,
        h('div.row.ad-emp-controls', startBtn, stopBtn, progress),
        summary),
      actions: [{ label: 'Close', value: true }],
      onClose: () => {
        open = false;
        ac.abort();
      },
    });
    text.focus();
    return dlg.closed;
  }

  load();
  const el = h('div.ad-panel', welcome, toolbar, note, table, moreBtn);
  return { el, destroy: () => { sync.cancel(); off(); } };
}

/* ------------------------------------------------------------------------------------------ */
/* Workspace                                                                                  */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {{timers: Set<any>, isAlive: () => boolean}} env
 * @returns {{el: HTMLElement, destroy: () => void}}
 */
function workspacePanel(env) {
  let ws = { name: store.workspace.name, registration_open: store.workspace.registration_open, join_code: '' };
  let urls = /** @type {string[]} */ ([]);
  const host = h('div.st-stack');
  host.append(h('div.center', ui.spinner(22)));

  const origin = () => (urls.length ? urls[0].replace(/\/+$/, '') : location.origin);
  const inviteText = () => {
    const lines = [`Join ${ws.name} on DeskTalk: ${origin()}`, 'Your browser may say "Not secure" - that is expected on the office network.'];
    lines.push(ws.registration_open
      ? `Create your account there with the join code ${ws.join_code}.`
      : 'Your admin will give you a username and a temporary password.');
    return lines.join('\n');
  };

  function build() {
    const name = createField({ name: 'name', label: 'Workspace name', autocomplete: 'off', maxlength: 80, value: ws.name });
    const saveName = h('button.btn.btn-primary.btn-sm', { type: 'submit' }, 'Save name');
    const nameBox = h('form.ad-inline', {
      novalidate: true,
      onSubmit: async (e) => {
        e.preventDefault();
        name.clearError();
        const v = name.value.trim();
        if (!v || cpLength(v) > 40) {
          name.setError(v ? 'Use at most 40 characters.' : 'Enter a name.');
          return;
        }
        if (v === ws.name) return;
        saveName.disabled = true;
        try {
          const res = await ask('admin.settings', { workspace_name: v });
          ws = { ...ws, ...res.workspace };
          name.input.value = ws.name;
          ui.toast('Workspace name saved.', { type: 'success' });
        } catch (err) {
          name.setError(errorText(err, 'Could not save the name.'));
        } finally {
          saveName.disabled = false;
        }
      },
    }, name.el, h('div.row', saveName));

    const code = h('code.ad-code', ws.join_code || '-');
    const reg = switchRow({
      title: 'Let people create their own account',
      hint: 'Anyone on this network who knows the join code can create an account.',
      checked: ws.registration_open,
      onChange: async (on, input) => {
        input.disabled = true;
        try {
          const res = await ask('admin.settings', { registration_open: on });
          ws = { ...ws, ...res.workspace };
          ui.toast(on ? 'Registration is open.' : 'Registration is closed.', { type: 'success' });
        } catch (err) {
          input.checked = !on;
          ui.toast(errorText(err, 'Could not change registration.'), { type: 'error' });
        } finally {
          input.disabled = false;
        }
      },
    });
    const rotate = h('button.btn.btn-secondary.btn-sm', {
      type: 'button',
      onClick: async () => {
        const ok = await ui.confirm({ title: 'Make a new join code?', message: 'The old code stops working at once. People who already have an account are not affected.', confirmLabel: 'New code' });
        if (!ok) return;
        try {
          const res = await ask('admin.settings', { rotate_join_code: true });
          ws = { ...ws, ...res.workspace };
          code.textContent = ws.join_code;
          ui.toast('New join code created.', { type: 'success' });
        } catch (err) {
          ui.toast(errorText(err, 'Could not create a new code.'), { type: 'error' });
        }
      },
    }, icon('refresh', { size: 16 }), 'New code');
    const codeRow = h('div.st-row.st-row-wide',
      h('div.st-row-text', h('span.st-row-title', 'Join code'), h('p.hint', 'Needed to register while registration is open.')),
      h('div.row.ad-code-row', code, copyButton(() => ws.join_code, 'Copy'), rotate));

    const urlList = urls.length
      ? h('ul.ad-urls', { role: 'list' }, ...urls.map((u, i) => h('li.ad-url',
        h('div.grow', h('span.mono.ad-url-text', u), i === 0 ? h('span.hint', 'Share this link') : h('span.hint', 'Other adapter (may not be reachable by phones)')),
        copyButton(() => u, 'Copy'))))
      : h('p.hint', `No network address found yet. This page is at ${location.origin}.`);
    const invite = h('section.st-card',
      h('h2.st-card-title', 'Invite your team'),
      h('p.muted', 'Everyone on the office network opens one of these links in a browser. Phones work too.'),
      urlList,
      h('div.row', copyButton(inviteText, 'Copy invitation text', { small: false })));

    const policy = h('section.st-card',
      h('h2.st-card-title', 'Notifications and the "Not secure" label'),
      h('p', 'Over plain http browsers show "Not secure" and only allow in-page alerts, sounds and the tab badge. Desktop notifications, the clipboard and voice notes need a secure connection.'),
      h('ul.ad-list',
        h('li', 'On the server PC itself, type localhost:', location.port || '8765', ' into the browser (that counts as secure).'),
        h('li', 'Best: restart the server with HTTPS (--tls) and trust its certificate once per device.'),
        h('li', 'Or, on each Chrome or Edge PC, set the policy ', h('code', 'OverrideSecurityRestrictionsOnInsecureOrigin'), ' to ', h('code', origin()), ' (registry or group policy) and restart the browser.')),
      h('p.hint', 'Alerts never reach a closed browser or a locked phone; that needs a push service. Keep DeskTalk pinned in an open tab.'));

    host.replaceChildren(
      h('section.st-card', h('h2.st-card-title', 'Workspace'), nameBox),
      h('section.st-card', h('h2.st-card-title', 'Registration'), reg.el, codeRow),
      invite,
      policy);
    return { reg, code, name };
  }

  /** @type {null|{reg: any, code: HTMLElement, name: any}} */
  let parts = null;

  async function load() {
    try {
      const [s, st] = await Promise.all([ask('admin.settings', {}), ask('admin.stats', {}).catch(() => null)]);
      if (!env.isAlive()) return;
      ws = { ...ws, ...s.workspace };
      urls = st && Array.isArray(st.urls) ? st.urls : [];
      parts = build();
    } catch (err) {
      if (!env.isAlive()) return;
      host.replaceChildren(h('div.empty-state', h('p.error-text', errorText(err, 'Could not load the workspace settings.')),
        h('button.btn.btn-secondary', { type: 'button', onClick: () => { host.replaceChildren(h('div.center', ui.spinner(22))); load(); } }, 'Try again')));
    }
  }

  const off = store.on('workspace', (w) => {
    ws = { ...ws, name: w.name, registration_open: w.registration_open };
    if (!parts) return;
    if (!parts.reg.input.disabled) parts.reg.input.checked = Boolean(w.registration_open);
    if (document.activeElement !== parts.name.input) parts.name.input.value = w.name;
  });
  load();
  return { el: host, destroy: off };
}

/* ------------------------------------------------------------------------------------------ */
/* Stats                                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {{timers: Set<any>, isAlive: () => boolean}} env
 * @returns {{el: HTMLElement}}
 */
function statsPanel(env) {
  const grid = h('div.ad-stats');
  const links = h('div');
  const status = h('p.hint', { role: 'status' });
  const refresh = h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => load() }, icon('refresh', { size: 16 }), 'Refresh');

  function tile(label, value, warn = false) {
    return h(`div.ad-stat${warn ? '.ad-stat-warn' : ''}`, h('span.ad-stat-label', label), h('span.ad-stat-value', value));
  }

  function paint(s) {
    const backupOld = !s.last_backup_at || serverNow() - s.last_backup_at > 3 * 86400;
    grid.replaceChildren(
      tile('People', String(s.users)),
      tile('Online now', String(s.online)),
      tile('Chats', String(s.chats)),
      tile('Messages', String(s.messages)),
      tile('Attachments', String(s.attachments)),
      tile('Files on disk', formatBytes(s.storage_bytes)),
      tile('Database size', formatBytes(s.db_bytes)),
      tile('Disk free', formatBytes(s.disk_free_bytes)),
      tile('Last backup', s.last_backup_at ? `${formatDateTime(s.last_backup_at)} (${ago(s.last_backup_at)})` : 'Never', backupOld),
      tile('Server running for', formatUptime(s.uptime_s)),
      tile('Python', String(s.python)),
      tile('Version', String(s.version)));
    const urls = Array.isArray(s.urls) ? s.urls : [];
    links.replaceChildren(h('section.st-card',
      h('h2.st-card-title', 'Share this link'),
      urls.length
        ? h('ul.ad-urls', { role: 'list' }, ...urls.map((u) => h('li.ad-url', h('span.mono.ad-url-text.grow', u), copyButton(() => u, 'Copy'))))
        : h('p.hint', 'No network address found yet.')));
  }

  async function load() {
    refresh.disabled = true;
    try {
      const s = await ask('admin.stats', {});
      if (!env.isAlive()) return;
      status.textContent = `Updated ${formatDateTime(serverNow())}`;
      paint(s);
    } catch (err) {
      if (env.isAlive()) status.textContent = errorText(err, 'Could not load the statistics.');
    } finally {
      refresh.disabled = false;
    }
  }

  load();
  const timer = setInterval(() => {
    if (env.isAlive() && document.visibilityState === 'visible') load();
  }, 30000);
  env.timers.add(timer);
  return { el: h('div.st-stack', h('div.row.ad-stats-head', status, h('span.spacer'), refresh), grid, links) };
}

/* ------------------------------------------------------------------------------------------ */
/* Audit log                                                                                  */
/* ------------------------------------------------------------------------------------------ */

const ACTION_TEXT = {
  'admin.create_user': 'Created an account',
  'admin.update_user': 'Changed an account',
  'admin.reset_password': 'Reset a password',
  'admin.settings': 'Changed workspace settings',
  'cli.create_admin': 'Created an admin (server console)',
  'cli.reset_password': 'Reset a password (server console)',
  'cli.restore': 'Restored a backup (server console)',
};

/**
 * @param {{timers: Set<any>, isAlive: () => boolean}} env
 * @returns {{el: HTMLElement}}
 */
function auditPanel(env) {
  const body = h('tbody');
  const table = h('table.ad-table.ad-audit', { hidden: true }, h('caption.sr-only', 'Audit log'),
    h('thead', h('tr', ...['When', 'Who', 'What', 'Account', 'Address'].map((t) => h('th', { scope: 'col' }, t)))), body);
  const status = h('p.hint', { role: 'status' }, 'Loading…');
  const refresh = h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => load() }, icon('refresh', { size: 16 }), 'Refresh');

  async function load() {
    refresh.disabled = true;
    try {
      const res = await ask('admin.audit', { limit: 100 });
      if (!env.isAlive()) return;
      const entries = res.entries || [];
      body.replaceChildren(...entries.map((e) => h('tr',
        h('td', { 'data-label': 'When' }, formatDateTime(e.ts)),
        h('td', { 'data-label': 'Who' }, e.actor_id === null ? 'Server console' : nameOf(e.actor_id)),
        h('td', { 'data-label': 'What' }, ACTION_TEXT[e.action] || e.action),
        h('td', { 'data-label': 'Account' }, e.target_id === null || e.target_id === undefined ? '-' : nameOf(e.target_id)),
        h('td.mono', { 'data-label': 'Address' }, e.ip || '-'))));
      table.hidden = entries.length === 0;
      status.textContent = entries.length ? `The newest ${entries.length} actions.` : 'Nothing has been recorded yet.';
    } catch (err) {
      if (env.isAlive()) status.textContent = errorText(err, 'Could not load the audit log.');
    } finally {
      refresh.disabled = false;
    }
  }

  load();
  return { el: h('div.st-stack', h('div.row', status, h('span.spacer'), refresh), table) };
}

/* ------------------------------------------------------------------------------------------ */
/* The view                                                                                   */
/* ------------------------------------------------------------------------------------------ */

const TABS = [
  { id: 'users', label: 'Users', build: usersPanel },
  { id: 'workspace', label: 'Workspace', build: workspacePanel },
  { id: 'stats', label: 'Stats', build: statsPanel },
  { id: 'audit', label: 'Audit log', build: auditPanel },
];

/**
 * Mount the Admin view (admins only).
 * @param {HTMLElement} container #pane-main
 * @param {{route: {tab?: string}}} ctx
 * @returns {{update: (route: any) => void, unmount: () => void}|undefined}
 */
export function mount(container, ctx) {
  const isAdmin = () => Boolean(store.me && store.me.role === 'admin');
  if (!isAdmin()) {
    container.append(
      h('header.pane-header', h('button.btn-icon.only-narrow', { type: 'button', 'aria-label': 'Back', onClick: () => router.up() }, icon('back')), h('h1.pane-title.grow', 'Administration')),
      h('div.empty-state.grow', icon('shield', { size: 48 }), h('p', 'This area is for admins only.'),
        h('button.btn.btn-secondary', { type: 'button', onClick: () => router.home() }, 'Back to chats')));
    return undefined;
  }
  const tabbed = mountTabbed(container, { title: 'Administration', base: '/admin', tabs: TABS, route: ctx.route });
  const off = store.on('me', () => {
    if (!isAdmin()) router.home({ replace: true });
  });
  return {
    update: tabbed.update,
    unmount() {
      off();
      tabbed.unmount();
    },
  };
}
