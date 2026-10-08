/**
 * views/starred.js - the `#/starred` screen (SPEC 9.2 "Starred messages", 7.4 `msg.starred`).
 *
 * A newest-first list (by message id) of the messages I starred, paged 30 at a time. Each message is
 * rendered with ui-conv's message card (views/messageview.js); the list follows live changes: unstarring,
 * deleting for everyone / for me, leaving a chat, and a reload after every reconnect.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store } from '../core/store.js';
import { router } from '../core/router.js';
import { errorText } from '../core/util.js';
import { createMessageCard } from './messageview.js';

const PAGE = 30;

/**
 * Mount the Starred view.
 * @param {HTMLElement} container #pane-main
 * @returns {{unmount: () => void}}
 */
export function mount(container) {
  /** @type {Array<{message: any, card: {el: HTMLElement, update: (m: any) => void, destroy: () => void}, el: HTMLElement}>} */
  let items = [];
  let hasMore = false;
  let loading = false;
  let loaded = false;
  let error = '';
  let token = 0;
  let alive = true;

  const list = h('div.starred-list', { role: 'list', 'aria-label': 'Starred messages' });
  const status = h('div.starred-status', { role: 'status' });
  const moreBtn = h('button.btn.btn-secondary.starred-more', { type: 'button', hidden: true, onClick: () => load(false) }, 'Show more');
  const body = h('div.pane-body.starred-body', list, status, moreBtn);
  const back = h('button.btn-icon.only-narrow', { type: 'button', 'aria-label': 'Back', onClick: () => router.up() }, icon('back'));
  const header = h('header.pane-header', back, h('h1.pane-title.grow.truncate', 'Starred messages'));
  container.append(header, body);

  const io = typeof IntersectionObserver === 'function'
    ? new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting) && hasMore && !loading) load(false);
    }, { root: body, rootMargin: '200px' })
    : null;

  /** @param {any} message */
  function openMessage(message) {
    router.openChat(message.chat_id, { messageId: message.id });
  }

  /** @param {any} message */
  function createItem(message) {
    const card = createMessageCard(message, { onOpen: openMessage, showChat: true, onChanged: (m) => onMessage(m) });
    const el = h('div.starred-item', { role: 'listitem' }, card.el);
    return { message, card, el };
  }

  /**
   * Add a message at its place (the list is sorted by id, newest first).
   * @param {any} message
   */
  function insert(message) {
    const item = createItem(message);
    const at = items.findIndex((it) => it.message.id < message.id);
    if (at < 0) {
      items.push(item);
      list.appendChild(item.el);
    } else {
      list.insertBefore(item.el, items[at].el);
      items.splice(at, 0, item);
    }
  }

  /** @param {(it: any) => boolean} drop */
  function remove(drop) {
    const keep = [];
    for (const it of items) {
      if (drop(it)) {
        it.card.destroy();
        it.el.remove();
      } else {
        keep.push(it);
      }
    }
    items = keep;
  }

  function clearAll() {
    remove(() => true);
  }

  /** Reflect loading / empty / error / "show more" state. */
  function renderStatus() {
    status.replaceChildren();
    if (error) {
      status.append(h('p.error-text', error), h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => load(items.length === 0) }, 'Try again'));
    } else if (loading && items.length === 0) {
      status.append(h('span.spinner', { role: 'status', 'aria-label': 'Loading' }));
    } else if (loaded && items.length === 0) {
      status.append(h('div.empty-state',
        icon('star-outline', { size: 56 }),
        h('p.starred-empty-title', 'No starred messages'),
        h('p.muted', 'Long-press or right-click a message and choose Star to keep it here')));
    }
    moreBtn.hidden = !(hasMore && !loading && !error);
    if (io) {
      io.unobserve(moreBtn);
      if (!moreBtn.hidden) io.observe(moreBtn);
    }
  }

  /**
   * @param {boolean} reset start again from the newest message
   */
  async function load(reset) {
    if (loading && !reset) return;
    const mine = reset ? (token += 1) : token;
    loading = true;
    error = '';
    renderStatus();
    try {
      const req = { limit: PAGE };
      if (!reset && items.length) req.before_id = items[items.length - 1].message.id;
      const res = await store.request('msg.starred', req);
      if (!alive || mine !== token) return;
      if (reset) clearAll();
      for (const m of res.messages || []) {
        if (!items.some((it) => it.message.id === m.id)) insert(m);
      }
      hasMore = Boolean(res.has_more);
      loaded = true;
    } catch (err) {
      if (!alive || mine !== token) return;
      error = errorText(err, 'Could not load your starred messages.');
    } finally {
      if (alive && mine === token) {
        loading = false;
        renderStatus();
      }
    }
  }

  /**
   * A message changed somewhere (event, own response or the card's menu).
   * @param {any} incoming
   */
  function onMessage(incoming) {
    if (!alive || !incoming) return;
    const held = items.find((it) => it.message.id === incoming.id);
    if (held) {
      if (incoming.deleted || incoming.starred === false) {
        remove((it) => it === held);
        renderStatus();
      } else {
        store.patchMessage(held.message, incoming);
        held.card.update(held.message);
      }
      return;
    }
    const oldest = items.length ? items[items.length - 1].message.id : 0;
    if (incoming.starred && !incoming.deleted && loaded && (!hasMore || incoming.id > oldest)) {
      insert(incoming);
      renderStatus();
    }
  }

  const offs = [
    store.on('message_update', onMessage),
    store.on('message_removed', (e) => {
      const gone = new Set(e.message_ids);
      remove((it) => gone.has(it.message.id));
      renderStatus();
    }),
    store.on('chat_removed', (e) => {
      remove((it) => it.message.chat_id === e.chat_id);
      renderStatus();
    }),
    store.on('ready', () => {
      load(true);
    }),
  ];

  load(true);

  return {
    unmount() {
      alive = false;
      token += 1;
      for (const off of offs) off();
      if (io) io.disconnect();
      clearAll();
    },
  };
}
