/**
 * views/forward.js - the Forward dialog (SPEC 9.10) and the searchable chat/people target picker it is built
 * on (also used by the info drawer's "Add members").
 *
 * A person without a direct chat gets `chat.open_direct` first; the chosen chats receive `msg.forward`
 * (all-or-nothing on the server, idempotent through `client_id`).
 *
 * Exported API (docs/ui-conv-api.md section 3): openForward, createTargetPicker.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store } from '../core/store.js';
import { ui } from '../core/ui.js';
import { router } from '../core/router.js';
import { isTransientError } from '../core/socket.js';
import { chatAvatar, userAvatar } from '../core/avatar.js';
import { fold, errorText, newClientId, pluralize } from '../core/util.js';

const MAX_ROWS = 300;

/**
 * One selectable row of the picker.
 * @typedef {{key: string, kind: 'user'|'chat', id: number, name: string, sub: string, node: () => HTMLElement, text: string}} Target
 */

/**
 * Candidate targets: chats I can post to, then people who have no direct chat with me.
 * @param {{people: boolean, chats: boolean, exclude: Set<number>}} o
 * @returns {Target[]}
 */
function collectTargets(o) {
  const out = [];
  const me = store.me;
  if (o.chats) {
    for (const archived of [false, true]) {
      for (const c of store.chatList({ archived })) {
        if (!store.canPost(c).ok) continue;
        const peer = store.chatPeer(c);
        out.push({
          key: `chat:${c.id}`,
          kind: 'chat',
          id: c.id,
          name: store.chatTitle(c),
          sub: c.kind === 'group' ? `${(c.members || []).length} members` : peer ? `@${peer.username}` : '',
          node: () => chatAvatar(c, { size: 'md', online: null }),
          text: fold(`${store.chatTitle(c)} ${peer ? peer.username : ''}`),
        });
      }
    }
  }
  if (o.people) {
    const people = store.users()
      .filter((u) => !u.disabled && !o.exclude.has(u.id) && (!o.chats || !store.directChatWith(u.id)))
      .sort((a, b) => a.display_name.localeCompare(b.display_name));
    for (const u of people) {
      out.push({
        key: `user:${u.id}`,
        kind: 'user',
        id: u.id,
        name: me && u.id === me.id ? `${u.display_name} (You)` : u.display_name,
        sub: `@${u.username}`,
        node: () => userAvatar(u, { size: 'md', online: null }),
        text: fold(`${u.display_name} ${u.username}`),
      });
    }
  }
  return out;
}

/**
 * Searchable list of chats and/or people with multi-select chips.
 * @param {{people?: boolean, chats?: boolean, exclude?: number[], max?: number, onChange?: (selection: Array<{kind: 'user'|'chat', id: number}>) => void}} [opts]
 * @returns {{el: HTMLElement, selection: () => Array<{kind: 'user'|'chat', id: number}>, focus: () => void, destroy: () => void}}
 */
export function createTargetPicker(opts = {}) {
  const { people = true, chats = true, max = Infinity, onChange } = opts;
  const exclude = new Set(opts.exclude || []);
  /** @type {Map<string, Target>} */
  const selected = new Map();
  let all = collectTargets({ people, chats, exclude });
  /** @type {Target[]} */
  let shown = [];
  let focusIndex = 0;

  const input = h('input.input.fwd-search', {
    type: 'search',
    placeholder: chats ? 'Search chats and people' : 'Search people',
    'aria-label': chats ? 'Search chats and people' : 'Search people',
    autocomplete: 'off',
    dir: 'auto',
    onInput: () => renderList(),
    onKeydown: (e) => {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        focusRow(0);
      }
    },
  });
  const chipBox = h('div.fwd-chips', { hidden: true, 'aria-label': 'Selected' });
  const hint = h('p.fwd-hint.muted', { role: 'status' });
  const list = h('div.fwd-list.scroll-y', { role: 'listbox', 'aria-multiselectable': 'true', 'aria-label': 'Results' });
  const el = h('div.fwd-picker', input, chipBox, list, hint);

  /** @returns {boolean} whether the selection is full */
  const full = () => selected.size >= max;

  function renderChips() {
    chipBox.hidden = selected.size === 0;
    chipBox.replaceChildren(...Array.from(selected.values()).map((t) => h('span.fwd-chip',
      h('span.truncate', t.name),
      h('button.fwd-chip-x', { type: 'button', 'aria-label': `Remove ${t.name}`, onClick: () => toggle(t) }, icon('close', { size: 14 })))));
    hint.textContent = Number.isFinite(max) && full() ? `You can select up to ${max}.` : '';
  }

  /** @param {Target} t */
  function toggle(t) {
    if (selected.has(t.key)) selected.delete(t.key);
    else if (!full()) selected.set(t.key, t);
    renderChips();
    syncRows();
    if (onChange) onChange(selection());
  }

  function syncRows() {
    for (const row of /** @type {HTMLElement[]} */ (Array.from(list.children))) {
      const key = row.dataset.key || '';
      const on = selected.has(key);
      row.setAttribute('aria-selected', String(on));
      row.classList.toggle('on', on);
      /** @type {HTMLButtonElement} */ (row).disabled = !on && full();
    }
  }

  function renderList() {
    const q = fold(input.value).trim();
    const matches = q ? all.filter((t) => t.text.includes(q)) : all;
    shown = matches.slice(0, MAX_ROWS);
    focusIndex = 0;
    if (shown.length === 0) {
      list.replaceChildren(h('p.fwd-empty.muted', q ? `No chats or people match "${input.value.trim()}"` : 'Nobody to show'));
      return;
    }
    list.replaceChildren(...shown.map((t, i) => h('button.fwd-row', {
      type: 'button',
      role: 'option',
      tabIndex: i === 0 ? 0 : -1,
      dataset: { key: t.key },
      onClick: () => toggle(t),
      onKeydown: (e) => onRowKey(e, i),
    },
    t.node(),
    h('span.fwd-row-main', h('span.fwd-row-name.truncate', { dir: 'auto' }, t.name), t.sub ? h('span.fwd-row-sub.muted.truncate', t.sub) : null),
    h('span.fwd-check', icon('check', { size: 18 })))));
    if (matches.length > shown.length) list.appendChild(h('p.fwd-empty.muted', 'Keep typing to narrow the list'));
    syncRows();
  }

  /** @param {number} i */
  function focusRow(i) {
    const rows = /** @type {HTMLElement[]} */ (Array.from(list.querySelectorAll('.fwd-row')));
    if (!rows.length) return;
    const n = Math.max(0, Math.min(rows.length - 1, i));
    rows[focusIndex].tabIndex = -1;
    focusIndex = n;
    rows[n].tabIndex = 0;
    rows[n].focus();
  }

  /**
   * @param {KeyboardEvent} e
   * @param {number} i
   */
  function onRowKey(e, i) {
    if (e.key === 'ArrowDown') focusRow(i + 1);
    else if (e.key === 'ArrowUp') {
      if (i === 0) input.focus();
      else focusRow(i - 1);
    } else if (e.key === 'Home') focusRow(0);
    else if (e.key === 'End') focusRow(shown.length - 1);
    else return;
    e.preventDefault();
  }

  /** @returns {Array<{kind: 'user'|'chat', id: number}>} */
  function selection() {
    return Array.from(selected.values()).map((t) => ({ kind: t.kind, id: t.id }));
  }

  const offs = [store.on('users', () => {
    all = collectTargets({ people, chats, exclude });
    renderList();
  })];
  renderList();
  renderChips();
  return {
    el,
    selection,
    focus: () => input.focus({ preventScroll: true }),
    destroy: () => {
      for (const off of offs) off();
    },
  };
}

/**
 * Resolve the chat ids of the selection (people without a DM get one opened first).
 * @param {Array<{kind: 'user'|'chat', id: number}>} sel
 * @returns {Promise<number[]>}
 */
async function resolveChatIds(sel) {
  const ids = [];
  for (const s of sel) {
    if (s.kind === 'chat') {
      ids.push(s.id);
      continue;
    }
    const existing = store.directChatWith(s.id);
    if (existing) {
      ids.push(existing.id);
      continue;
    }
    const res = await store.request('chat.open_direct', { user_id: s.id });
    ids.push(res.chat.id);
  }
  return Array.from(new Set(ids));
}

/**
 * Open the Forward dialog for the given messages.
 * @param {{messages: any[], fromChatId?: number}} opts
 * @returns {Promise<boolean>} true after a successful forward
 */
export async function openForward(opts) {
  const max = store.limits.max_forward_messages;
  const usable = (opts.messages || []).filter((m) => m && m.id > 0 && !m.deleted && m.kind !== 'system').slice(0, max);
  if (usable.length === 0) {
    ui.toast('There is nothing to forward.', { type: 'error', key: 'forward-empty' });
    return false;
  }
  const limit = store.limits.max_forward_chats;
  /** @type {HTMLButtonElement|null} */
  let forwardBtn = null;
  const picker = createTargetPicker({
    max: limit,
    onChange: (sel) => {
      if (forwardBtn && !sending) forwardBtn.disabled = sel.length === 0;
    },
  });
  const error = h('p.error-text.fwd-error', { role: 'alert', hidden: true });
  const summary = h('p.fwd-summary.muted.truncate', { dir: 'auto' }, usable.length === 1 ? store.summarize(usable[0]) : `${usable.length} messages`);
  let clientId = newClientId();
  let sending = false;

  const handle = ui.dialog({
    title: 'Forward to…',
    size: 'sm',
    className: 'fwd-dialog',
    content: h('div.fwd-body', summary, picker.el, error),
    initialFocus: picker.el.querySelector('.fwd-search') || undefined,
    actions: [
      { label: 'Cancel', id: 'cancel', value: false },
      {
        label: 'Forward',
        id: 'forward',
        primary: true,
        value: true,
        onClick: async () => {
          const sel = picker.selection();
          if (sel.length === 0 || sending) return false;
          sending = true;
          error.hidden = true;
          const btn = /** @type {HTMLButtonElement|null} */ (handle.panel.querySelector('[data-action="forward"]'));
          if (btn) btn.disabled = true;
          try {
            const chatIds = await resolveChatIds(sel);
            await store.request('msg.forward', { message_ids: usable.map((m) => m.id), chat_ids: chatIds, client_id: clientId });
            clientId = newClientId();
            const n = chatIds.length;
            ui.toast(`Forwarded to ${n} ${pluralize(n, 'chat')}`, n === 1 ? { type: 'success', onClick: () => router.openChat(chatIds[0]) } : { type: 'success' });
            return undefined;
          } catch (err) {
            if (!isTransientError(err)) clientId = newClientId();
            error.textContent = errorText(err, 'Could not forward the message.');
            error.hidden = false;
            return false;
          } finally {
            sending = false;
            if (btn) btn.disabled = picker.selection().length === 0;
          }
        },
      },
    ],
    onClose: () => picker.destroy(),
  });
  forwardBtn = /** @type {HTMLButtonElement|null} */ (handle.panel.querySelector('[data-action="forward"]'));
  if (forwardBtn) forwardBtn.disabled = true;
  const result = await handle.closed;
  return result === true;
}
