/**
 * views/searchpanel.js - the sidebar's global search (SPEC 9.2 "Search", 9.6 "Search client rules").
 *
 * While the search box holds text the chat list is replaced by three sections: "Chats" (local filter,
 * rendered with the sidebar's own row component), "People" (local filter; a person without a chat starts
 * one) and "Messages" (`msg.search`, only for 2+ characters). The client rules of 9.6 are implemented in
 * `createSession` below: 400 ms debounce, sequence numbers (stale answers are dropped), one request in
 * flight (the newest follow-up waits for it), `rate_limited` waits `retry_after` silently and keeps the
 * previous results.
 */

import { h } from '../core/dom.js';
import { userAvatar } from '../core/avatar.js';
import { store } from '../core/store.js';
import { socket } from '../core/socket.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { cpLength, errorText, fold, formatLastSeen } from '../core/util.js';
import { openDirect } from './newchat.js';

const DEBOUNCE_MS = 400;
const PAGE = 30;
const MAX_PEOPLE = 30;

/** @type {Promise<any>|null} */
let messageViewPromise = null;

/** @returns {Promise<any>} views/messageview.js, loaded on first use */
function loadMessageView() {
  if (!messageViewPromise) {
    messageViewPromise = import('./messageview.js').catch((err) => {
      messageViewPromise = null;
      throw err;
    });
  }
  return messageViewPromise;
}

/**
 * The message-search client of SPEC 9.6.
 * @param {{onState: (s: {status: 'idle'|'loading'|'done'|'error', error: string, messages: any[], hasMore: boolean, query: string}) => void}} o
 * @returns {{search: (q: string) => void, more: () => void, cancel: () => void}}
 */
function createSession(o) {
  let debounce = 0;
  let wait = 0;
  let seq = 0;
  let inflight = false;
  /** @type {null|{q: string, beforeId: number|null, seq: number}} */
  let wanted = null;
  const view = { status: /** @type {'idle'|'loading'|'done'|'error'} */ ('idle'), error: '', messages: /** @type {any[]} */ ([]), hasMore: false, query: '' };

  const emit = () => o.onState({ ...view, messages: view.messages });

  async function pump() {
    if (inflight || !wanted) return;
    const job = wanted;
    wanted = null;
    inflight = true;
    let retryAfter = 0;
    try {
      const body = { q: job.q, limit: PAGE };
      if (job.beforeId) body.before_id = job.beforeId;
      const res = await socket.request('msg.search', body);
      if (job.seq === seq) {
        const found = (res.results || []).map((r) => r.message).filter(Boolean);
        view.messages = job.beforeId ? view.messages.concat(found) : found;
        view.hasMore = Boolean(res.has_more);
        view.status = 'done';
        view.error = '';
        view.query = job.q;
        emit();
      }
    } catch (err) {
      if (err && err.code === 'rate_limited') {
        retryAfter = Math.max(0.5, Number(err.retry_after) || 1);
        if (job.seq === seq && !wanted) wanted = job;
      } else if (job.seq === seq) {
        view.status = 'error';
        view.error = errorText(err, 'Search failed. Please try again.');
        emit();
      }
    } finally {
      inflight = false;
    }
    if (retryAfter > 0) {
      wait = setTimeout(() => {
        wait = 0;
        pump();
      }, retryAfter * 1000);
    } else {
      pump();
    }
  }

  function begin(q, beforeId) {
    seq += 1;
    if (wait) {
      clearTimeout(wait);
      wait = 0;
    }
    wanted = { q, beforeId, seq };
    view.status = 'loading';
    emit();
    pump();
  }

  return {
    search(q) {
      clearTimeout(debounce);
      debounce = 0;
      const query = q.trim();
      if (cpLength(query) < 2) {
        seq += 1;
        wanted = null;
        if (wait) {
          clearTimeout(wait);
          wait = 0;
        }
        Object.assign(view, { status: 'idle', error: '', messages: [], hasMore: false, query: '' });
        emit();
        return;
      }
      view.status = 'loading';
      emit();
      debounce = setTimeout(() => {
        debounce = 0;
        begin(query, null);
      }, DEBOUNCE_MS);
    },
    more() {
      const last = view.messages[view.messages.length - 1];
      if (last && view.hasMore && view.status === 'done') begin(view.query, last.id);
    },
    cancel() {
      clearTimeout(debounce);
      if (wait) clearTimeout(wait);
      debounce = 0;
      wait = 0;
      seq += 1;
      wanted = null;
    },
  };
}

/**
 * @param {{createChatList: (opts?: object) => any}} deps the sidebar's chat-list component (injected: the sidebar imports this module)
 * @returns {{el: HTMLElement, setQuery: (q: string) => void, refresh: () => void, focusFirst: () => boolean, activateFirst: () => boolean, destroy: () => void}}
 */
export function createSearchPanel(deps) {
  let query = '';
  let found = { chats: 0, people: 0 };
  /** @type {any} */
  let msgState = { status: 'idle', error: '', messages: [], hasMore: false, query: '' };
  let renderToken = 0;

  const chatList = deps.createChatList();
  const chatsHead = h('h3.sp-heading', 'Chats');
  const chatsSec = h('section.sp-section', { 'aria-label': 'Chats' }, chatsHead, chatList.el);
  const peopleList = h('ul.sp-people', { role: 'list' });
  const peopleSec = h('section.sp-section', { 'aria-label': 'People' }, h('h3.sp-heading', 'People'), peopleList);
  const msgStatus = h('p.sp-status.muted', { role: 'status' });
  const msgList = h('div.sp-results');
  const moreBtn = h('button.btn.btn-secondary.btn-sm.sp-more', { type: 'button', hidden: true, onClick: () => session.more() }, 'Show more results');
  const msgSec = h('section.sp-section', { 'aria-label': 'Messages', hidden: true }, h('h3.sp-heading', 'Messages'), msgStatus, msgList, moreBtn);
  const emptyEl = h('div.empty-state.sp-empty', { hidden: true });
  const el = h('div.sp', { role: 'region', 'aria-label': 'Search results' }, chatsSec, peopleSec, msgSec, emptyEl);

  const session = createSession({
    onState(s) {
      msgState = s;
      renderMessages();
      renderEmpty();
    },
  });

  function renderLocal() {
    const q = query.trim();
    const chatIds = [];
    if (q) {
      for (const c of store.chatList({ query: q })) chatIds.push(c.id);
      for (const c of store.chatList({ query: q, archived: true })) chatIds.push(c.id);
    }
    chatList.setIds(chatIds);
    chatsSec.hidden = chatIds.length === 0;

    const key = fold(q);
    const meId = store.me ? store.me.id : -1;
    const people = key
      ? store.users()
        .filter((u) => u.id !== meId && !store.directChatWith(u.id) && (fold(u.display_name).includes(key) || fold(u.username).includes(key)))
        .sort((a, b) => a.display_name.localeCompare(b.display_name))
        .slice(0, MAX_PEOPLE)
      : [];
    peopleList.replaceChildren(...people.map((u) => {
      const seen = formatLastSeen(u);
      return h('li', h('button.sp-person', {
        type: 'button',
        disabled: Boolean(u.disabled),
        onClick: () => openDirect(u.id),
      }, userAvatar(u, { size: 'md' }),
      h('span.sp-person-text',
        h('span.sp-person-name.truncate', { dir: 'auto' }, u.display_name),
        h('span.sp-person-sub.truncate', u.disabled ? 'Account disabled' : `@${u.username}${seen ? ' · ' + seen : ''}`))));
    }));
    peopleSec.hidden = people.length === 0;
    found = { chats: chatIds.length, people: people.length };
  }

  async function renderMessages() {
    const token = (renderToken += 1);
    const q = query.trim();
    const active = cpLength(q) >= 2;
    msgSec.hidden = !active;
    if (!active) {
      msgList.replaceChildren();
      moreBtn.hidden = true;
      msgStatus.textContent = '';
      return;
    }
    const s = msgState;
    if (s.status === 'loading' && !s.messages.length) msgStatus.textContent = 'Searching…';
    else if (s.status === 'error') msgStatus.textContent = s.error;
    else if (s.status === 'done' && !s.messages.length) msgStatus.textContent = 'No messages found';
    else msgStatus.textContent = '';
    moreBtn.hidden = !(s.status === 'done' && s.hasMore);
    if (!s.messages.length) {
      msgList.replaceChildren();
      return;
    }
    let view;
    try {
      view = await loadMessageView();
    } catch (err) {
      console.error('[searchpanel] messageview could not be loaded', err);
      msgStatus.textContent = 'Message results are not available right now.';
      return;
    }
    if (token !== renderToken) return;
    try {
      msgList.replaceChildren(...s.messages.map((m) => view.createSearchResult(m, {
        query: s.query,
        showChat: true,
        onOpen: (message) => router.openChat(message.chat_id, { messageId: message.id }),
      })));
    } catch (err) {
      console.error('[searchpanel] rendering the results failed', err);
      msgStatus.textContent = 'Message results are not available right now.';
      return;
    }
    if (s.status === 'done') ui.announce(`${s.messages.length}${s.hasMore ? '+' : ''} messages found`);
  }

  function renderEmpty() {
    const q = query.trim();
    const noLocal = found.chats === 0 && found.people === 0;
    const settled = cpLength(q) < 2 || (msgState.status === 'done' && msgState.messages.length === 0);
    const show = Boolean(q) && noLocal && settled;
    emptyEl.hidden = !show;
    if (show) emptyEl.replaceChildren(h('p', `No chats or people match "${q}"`));
  }

  function focusables() {
    return Array.from(el.querySelectorAll('.chat-main, .sp-person:not(:disabled), .sr-row'));
  }

  return {
    el,
    setQuery(q) {
      query = q;
      renderLocal();
      session.search(q);
      renderMessages();
      renderEmpty();
    },
    refresh() {
      renderLocal();
      renderEmpty();
    },
    focusFirst() {
      const first = /** @type {HTMLElement|undefined} */ (focusables()[0]);
      if (first) first.focus();
      return Boolean(first);
    },
    activateFirst() {
      const first = /** @type {HTMLElement|undefined} */ (focusables()[0]);
      if (first) first.click();
      return Boolean(first);
    },
    destroy() {
      session.cancel();
      renderToken += 1;
      chatList.destroy();
    },
  };
}
