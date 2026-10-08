/**
 * views/searchpanel.js - the sidebar's global search (SPEC 9.2 "Search", 9.6 "Search client rules").
 *
 * While the search box holds text the chat list is replaced by three sections: "Chats" (local filter,
 * rendered with the sidebar's own row component), "People" (local filter; a person without a chat starts
 * one) and "Messages" (`msg.search` through core/api.js `createSearcher`, which owns the 400 ms debounce,
 * sequence numbers, the single request in flight and the `rate_limited` wait; results are ui-conv's
 * `createSearchResult` rows).
 */

import { h } from '../core/dom.js';
import { userAvatar } from '../core/avatar.js';
import { store } from '../core/store.js';
import { createSearcher } from '../core/api.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { cpLength, fold, formatLastSeen } from '../core/util.js';
import { openDirect } from './newchat.js';

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
 * @param {{createChatList: (opts?: object) => any}} deps the sidebar's chat-list component (injected: the sidebar imports this module)
 * @returns {{el: HTMLElement, setQuery: (q: string) => void, refresh: () => void, focusFirst: () => boolean, activateFirst: () => boolean, destroy: () => void}}
 */
export function createSearchPanel(deps) {
  let query = '';
  let found = { chats: 0, people: 0 };
  let msgState = { q: '', results: /** @type {any[]} */ ([]), hasMore: false, loading: false, error: /** @type {any} */ (null) };
  /** The searcher has answered for the current query (so "No messages found" is true). */
  let answered = false;
  let sawLoading = false;
  let renderToken = 0;

  const chatList = deps.createChatList();
  const chatsSec = h('section.sp-section', { 'aria-label': 'Chats' }, h('h3.sp-heading', 'Chats'), chatList.el);
  const peopleList = h('ul.sp-people', { role: 'list' });
  const peopleSec = h('section.sp-section', { 'aria-label': 'People' }, h('h3.sp-heading', 'People'), peopleList);
  const msgStatus = h('p.sp-status.muted', { role: 'status' });
  const msgList = h('div.sp-results');
  const moreBtn = h('button.btn.btn-secondary.btn-sm.sp-more', { type: 'button', hidden: true, onClick: () => searcher.loadMore() }, 'Show more results');
  const msgSec = h('section.sp-section', { 'aria-label': 'Messages', hidden: true }, h('h3.sp-heading', 'Messages'), msgStatus, msgList, moreBtn);
  const emptyEl = h('div.empty-state.sp-empty', { hidden: true });
  const el = h('div.sp', { role: 'region', 'aria-label': 'Search results' }, chatsSec, peopleSec, msgSec, emptyEl);

  const searcher = createSearcher();
  const offUpdate = searcher.on('update', (state) => {
    msgState = state;
    if (state.loading) sawLoading = true;
    else if (sawLoading && !state.error) answered = true;
    renderMessages();
    renderEmpty();
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
    const active = cpLength(query.trim()) >= 2;
    msgSec.hidden = !active;
    if (!active) {
      msgList.replaceChildren();
      moreBtn.hidden = true;
      msgStatus.textContent = '';
      return;
    }
    const s = msgState;
    if (s.error) msgStatus.textContent = s.error.message;
    else if (!answered && !s.results.length) msgStatus.textContent = 'Searching…';
    else if (answered && !s.results.length) msgStatus.textContent = 'No messages found';
    else msgStatus.textContent = '';
    moreBtn.hidden = !(s.hasMore && !s.loading && !s.error);
    if (!s.results.length) {
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
      msgList.replaceChildren(...s.results.map((r) => view.createSearchResult(r.message, {
        query: s.q.trim(),
        showChat: true,
        onOpen: (message) => router.openChat(message.chat_id, { messageId: message.id }),
      })));
    } catch (err) {
      console.error('[searchpanel] rendering the results failed', err);
      msgStatus.textContent = 'Message results are not available right now.';
      return;
    }
    if (answered && !s.loading) ui.announce(`${s.results.length}${s.hasMore ? '+' : ''} messages found`);
  }

  function renderEmpty() {
    const q = query.trim();
    const noLocal = found.chats === 0 && found.people === 0;
    const settled = cpLength(q) < 2 || (answered && msgState.results.length === 0);
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
      answered = false;
      sawLoading = false;
      renderLocal();
      searcher.setQuery(q);
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
      offUpdate();
      searcher.destroy();
      renderToken += 1;
      chatList.destroy();
    },
  };
}
