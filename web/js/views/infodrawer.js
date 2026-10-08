/**
 * views/infodrawer.js - the right-hand info drawer of a conversation (SPEC 9.2 "Drawer / info"):
 * contact or group info (name, @username, about, presence), mute, group settings (title, description,
 * only-admins-post, members with admin badges, add / remove / make admin, leave), pinned messages and the
 * shared media / files / links / starred tabs (`msg.shared`, `msg.starred`).
 *
 * Exported API (docs/ui-conv-api.md section 7): createInfoDrawer(container, { chatId, onClose, onJump })
 *   -> { focus(), destroy() }.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { store } from '../core/store.js';
import { ui } from '../core/ui.js';
import { serialSearch } from '../core/api.js';
import { chatAvatar, userAvatar } from '../core/avatar.js';
import { errorText, formatBytes, formatDateTime, formatDayLabel, formatLastSeen, formatDuration, pluralize, cpLength } from '../core/util.js';
import { findUrls, makeSnippet } from '../lib/richtext.js';
import { createMessageCard } from './messageview.js';
import { afterMenuClose } from './messagemenu.js';
import { openLightbox, lightboxItemFromMessage } from './lightbox.js';

const PAGE = 30;
const ADD_MEMBERS_MAX = 50;
const TABS = [
  { id: 'media', label: 'Media' },
  { id: 'files', label: 'Files' },
  { id: 'links', label: 'Links' },
  { id: 'starred', label: 'Starred' },
];

/** @returns {Promise<any>} views/sidebar.js (chat actions shared with the sidebar), loaded on use */
function loadSidebar() {
  return import('./sidebar.js').catch((err) => {
    console.error('[infodrawer] sidebar helpers unavailable', err);
    ui.toast('That action is not available right now.', { type: 'error' });
    return null;
  });
}

/** @returns {Promise<any>} views/newchat.js, loaded on use */
function loadNewChat() {
  return import('./newchat.js').catch((err) => {
    console.error('[infodrawer] newchat helpers unavailable', err);
    ui.toast('That action is not available right now.', { type: 'error' });
    return null;
  });
}

/**
 * Run a request in the single msg.search/msg.shared lane, retrying while the server says "rate limited".
 * @param {() => Promise<any>} fn
 * @returns {Promise<any>}
 */
async function sharedRequest(fn) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await serialSearch(fn);
    } catch (err) {
      if (!err || (err.code !== 'rate_limited' && err.code !== 'server_busy') || attempt >= 3) throw err;
      await new Promise((r) => setTimeout(r, Math.max(0.5, Number(err.retry_after) || 1) * 1000));
    }
  }
}

/**
 * @param {HTMLElement} container
 * @param {{chatId: number, onClose: () => void, onJump: (messageId: number) => void}} opts
 * @returns {{focus: () => void, destroy: () => void}}
 */
export function createInfoDrawer(container, opts) {
  const chatId = opts.chatId;
  let destroyed = false;
  let token = 0;
  /** @type {Array<() => void>} */
  let offs = [];
  let repaintTimer = 0;
  let tab = 'media';
  /** @type {Record<string, {items: any[], hasMore: boolean, loading: boolean, loaded: boolean, error: string}>} */
  let lists = {};
  /** @type {Array<{card: any, id: number}>} */
  let cards = [];

  const title = h('h2.pane-title.grow.truncate');
  const closeBtn = h('button.btn-icon', { type: 'button', 'aria-label': 'Close info', title: 'Close', onClick: () => opts.onClose() }, icon('close'));
  const header = h('header.pane-header.drawer-header', closeBtn, title);
  const hero = h('section.drawer-hero');
  const prefs = h('section.drawer-section.drawer-prefs');
  const pinned = h('section.drawer-section.drawer-pinned');
  const members = h('section.drawer-section.drawer-members');
  const tabBar = h('div.drawer-tabs', { role: 'tablist', 'aria-label': 'Shared content' });
  const panel = h('div.drawer-panel', { role: 'tabpanel' });
  const sharedSection = h('section.drawer-section.drawer-shared', h('h3.drawer-heading', 'Media, files and links'), tabBar, panel);
  const danger = h('section.drawer-section.drawer-danger');
  const body = h('div.pane-body.drawer-body.scroll-y', hero, prefs, pinned, sharedSection, members, danger);
  container.replaceChildren(h('div.drawer', { role: 'region', 'aria-label': 'Chat info' }, header, body));

  /** @returns {any} */
  const chat = () => store.getChat(chatId);

  /* ---------------------------------------------------------------------------------------- */
  /* Hero                                                                                     */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Prompt for a new value and send `chat.update`.
   * @param {'title'|'description'} field
   */
  async function editField(field) {
    const c = chat();
    if (!c) return;
    const isTitle = field === 'title';
    const value = await ui.prompt({
      title: isTitle ? 'Group name' : 'Group description',
      label: isTitle ? 'Name' : 'Description',
      value: isTitle ? c.title || '' : c.description || '',
      confirmLabel: 'Save',
      maxLength: isTitle ? 120 : 1000,
      multiline: !isTitle,
      validate: (v) => {
        const n = cpLength(v.trim());
        if (isTitle && (n < 1 || n > 60)) return 'The name must have 1 to 60 characters.';
        if (!isTitle && n > 500) return 'The description can have up to 500 characters.';
        return null;
      },
    });
    if (value === null) return;
    try {
      await store.request('chat.update', { chat_id: chatId, [field]: value.trim() });
    } catch (err) {
      ui.toast(errorText(err, 'Could not save the change.'), { type: 'error' });
    }
  }

  function paintHero() {
    const c = chat();
    if (!c) return;
    const isGroup = c.kind === 'group';
    title.textContent = isGroup ? 'Group info' : 'Contact info';
    const admin = isGroup && store.isGroupAdmin(c);
    const rows = [chatAvatar(c, { size: 'xl', online: null })];
    if (isGroup) {
      rows.push(h('div.drawer-name-row',
        h('h3.drawer-name', { dir: 'auto' }, store.chatTitle(c)),
        admin ? h('button.btn-icon', { type: 'button', 'aria-label': 'Edit group name', title: 'Edit name', onClick: () => editField('title') }, icon('edit', { size: 20 })) : null));
      rows.push(h('p.muted.drawer-sub', `Group · ${(c.members || []).length} ${pluralize((c.members || []).length, 'member')}`));
      rows.push(h('div.drawer-about',
        c.description ? h('p.drawer-desc', { dir: 'auto' }, c.description) : h('p.muted.drawer-desc', admin ? 'No description yet.' : 'No description.'),
        admin ? h('button.btn.btn-ghost.btn-sm', { type: 'button', onClick: () => editField('description') }, c.description ? 'Edit description' : 'Add description') : null));
      const creator = c.created_by ? store.userName(c.created_by) : '';
      if (c.created_at) rows.push(h('p.muted.drawer-created', `Created${creator ? ` by ${creator}` : ''} on ${formatDateTime(c.created_at)}`));
    } else {
      const peer = store.chatPeer(c);
      rows.push(h('h3.drawer-name', { dir: 'auto' }, store.chatTitle(c)));
      if (peer) {
        rows.push(h('p.muted.drawer-sub', `@${peer.username}`));
        rows.push(h('p.drawer-presence', peer.disabled ? 'Account disabled' : formatLastSeen(peer)));
        if (peer.status_text) rows.push(h('div.drawer-about', h('p.muted.drawer-label', 'About'), h('p.drawer-desc', { dir: 'auto' }, peer.status_text)));
      } else {
        rows.push(h('p.muted.drawer-sub', 'Message yourself'));
      }
    }
    hero.replaceChildren(...rows);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Preferences                                                                              */
  /* ---------------------------------------------------------------------------------------- */

  /** @returns {string} */
  function muteText() {
    const c = chat();
    if (!c || !store.isMuted(c)) return 'On';
    const until = c.me.muted_until;
    return until >= 4102444800 ? 'Muted' : `Muted until ${formatDateTime(until)}`;
  }

  async function openMuteMenu() {
    const c = chat();
    if (!c) return;
    const mod = await loadSidebar();
    if (mod) ui.menu(mod.muteItems(c), { anchor: muteBtn, label: 'Notifications' });
  }

  const muteBtn = h('button.drawer-row', { type: 'button', onClick: () => openMuteMenu() });

  async function toggleOnlyAdmins(input) {
    const c = chat();
    if (!c) return;
    try {
      await store.request('chat.update', { chat_id: chatId, only_admins_post: input.checked });
    } catch (err) {
      input.checked = !input.checked;
      ui.toast(errorText(err, 'Could not save the change.'), { type: 'error' });
    }
  }

  function paintPrefs() {
    const c = chat();
    if (!c) return;
    muteBtn.replaceChildren(
      h('span.drawer-row-icon', icon(store.isMuted(c) ? 'bell-off' : 'bell', { size: 22 })),
      h('span.drawer-row-main', h('span.drawer-row-title', 'Notifications'), h('span.drawer-row-sub.muted', muteText())),
      icon('chevron-right', { size: 20 }));
    const rows = [muteBtn];
    if (c.kind === 'group') {
      const admin = store.isGroupAdmin(c);
      if (admin) {
        const input = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', id: 'drawer-admins-only', checked: Boolean(c.only_admins_post), onChange: (e) => toggleOnlyAdmins(e.target) }));
        rows.push(h('label.drawer-row', { for: 'drawer-admins-only' },
          h('span.drawer-row-icon', icon('lock', { size: 22 })),
          h('span.drawer-row-main', h('span.drawer-row-title', 'Only admins can send messages'), h('span.drawer-row-sub.muted', 'Everyone else can still read and react')),
          h('span.switch', input, h('span.track'))));
      } else if (c.only_admins_post) {
        rows.push(h('p.drawer-row.muted', h('span.drawer-row-icon', icon('lock', { size: 22 })), 'Only admins can send messages to this group.'));
      }
    }
    prefs.replaceChildren(...rows);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Pinned messages                                                                          */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {any} m */
  async function unpin(m) {
    try {
      await store.request('msg.pin', { chat_id: chatId, message_id: m.id, pinned: false });
    } catch (err) {
      ui.toast(errorText(err, 'Could not unpin the message.'), { type: 'error' });
    }
  }

  function paintPinned() {
    const c = chat();
    const pins = c && Array.isArray(c.pinned_messages) ? c.pinned_messages : [];
    pinned.hidden = pins.length === 0;
    if (!c || !pins.length) return;
    const canUnpin = store.canPost(c).ok;
    pinned.replaceChildren(
      h('h3.drawer-heading', `Pinned messages (${pins.length})`),
      h('ul.drawer-list', pins.map((m) => h('li.drawer-pin',
        h('button.drawer-pin-main', { type: 'button', onClick: () => opts.onJump(m.id) },
          h('span.drawer-pin-sender', m.sender_id === null ? '' : store.me && m.sender_id === store.me.id ? 'You' : store.userName(m.sender_id)),
          h('span.drawer-pin-text.truncate', { dir: 'auto' }, store.summarize(m)),
          h('span.drawer-pin-time.muted', formatDateTime(m.created_at))),
        canUnpin ? h('button.btn-icon', { type: 'button', 'aria-label': 'Unpin message', title: 'Unpin', onClick: () => unpin(m) }, icon('close', { size: 18 })) : null))));
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Members                                                                                  */
  /* ---------------------------------------------------------------------------------------- */

  /** @param {number} userId */
  async function messageUser(userId) {
    const mod = await loadNewChat();
    if (mod) await mod.openDirect(userId);
  }

  /**
   * @param {number} userId
   * @param {boolean} makeAdmin
   */
  async function setAdmin(userId, makeAdmin) {
    try {
      await store.request('chat.set_admin', { chat_id: chatId, user_id: userId, admin: makeAdmin });
    } catch (err) {
      ui.toast(errorText(err, 'Could not change the role.'), { type: 'error' });
    }
  }

  /** @param {number} userId */
  async function removeMember(userId) {
    const c = chat();
    const ok = await ui.confirm({
      title: 'Remove from group?',
      message: `${store.userName(userId)} will be removed from ${c ? store.chatTitle(c) : 'the group'}.`,
      confirmLabel: 'Remove',
      danger: true,
    });
    if (!ok) return;
    try {
      await store.request('chat.remove_member', { chat_id: chatId, user_id: userId });
    } catch (err) {
      ui.toast(errorText(err, 'Could not remove the member.'), { type: 'error' });
    }
  }

  /**
   * @param {any} member
   * @param {HTMLElement} anchor
   */
  function openMemberMenu(member, anchor) {
    const c = chat();
    if (!c || !store.me || member.user_id === store.me.id) return;
    const items = [{ label: `Message ${store.userName(member.user_id)}`, icon: 'chat-outline', onSelect: () => messageUser(member.user_id) }];
    if (store.isGroupAdmin(c) && !c.is_default) {
      items.push({ label: member.role === 'admin' ? 'Dismiss as group admin' : 'Make group admin', icon: 'shield', onSelect: () => setAdmin(member.user_id, member.role !== 'admin') });
      items.push({ label: 'Remove from group', icon: 'trash', danger: true, onSelect: () => afterMenuClose(() => removeMember(member.user_id)) });
    }
    ui.menu(items, { anchor, label: `Actions for ${store.userName(member.user_id)}`, title: store.userName(member.user_id) });
  }

  async function addMembers() {
    const c = chat();
    if (!c) return;
    const existing = (c.members || []).map((m) => m.user_id);
    const error = h('p.error-text', { role: 'alert', hidden: true });
    let count = 0;
    const picker = ui.peoplePicker({
      multi: true,
      exclude: existing,
      placeholder: 'Search people to add',
      onPick: (_u, _on, all) => {
        count = all ? all.length : 0;
        const btn = /** @type {HTMLButtonElement|null} */ (handle.panel.querySelector('[data-action="add"]'));
        if (btn) btn.disabled = count === 0;
        error.hidden = true;
      },
    });
    const handle = ui.dialog({
      title: 'Add members',
      size: 'sm',
      className: 'dialog-people',
      content: h('div', picker.el, error),
      initialFocus: picker.input,
      actions: [
        { label: 'Cancel', id: 'cancel', value: false },
        {
          label: 'Add',
          id: 'add',
          primary: true,
          value: true,
          onClick: async () => {
            const ids = picker.getSelected().map((u) => u.id);
            if (!ids.length) return false;
            if (ids.length > ADD_MEMBERS_MAX) {
              error.textContent = `You can add up to ${ADD_MEMBERS_MAX} people at a time.`;
              error.hidden = false;
              return false;
            }
            handle.setBusy(true);
            try {
              await store.request('chat.add_members', { chat_id: chatId, user_ids: ids });
              return undefined;
            } catch (err) {
              handle.setBusy(false);
              error.textContent = errorText(err, 'Could not add the members.');
              error.hidden = false;
              return false;
            }
          },
        },
      ],
      onClose: () => picker.destroy(),
    });
    const addBtn = /** @type {HTMLButtonElement|null} */ (handle.panel.querySelector('[data-action="add"]'));
    if (addBtn) addBtn.disabled = true;
  }

  function paintMembers() {
    const c = chat();
    members.hidden = !c || c.kind !== 'group';
    if (!c || c.kind !== 'group') return;
    const meId = store.me ? store.me.id : -1;
    const admin = store.isGroupAdmin(c);
    const list = (c.members || []).slice().sort((a, b) => {
      if (a.user_id === meId) return -1;
      if (b.user_id === meId) return 1;
      if (a.role !== b.role) return a.role === 'admin' ? -1 : 1;
      return store.userName(a.user_id).localeCompare(store.userName(b.user_id));
    });
    members.replaceChildren(
      h('div.drawer-members-head',
        h('h3.drawer-heading', `${list.length} ${pluralize(list.length, 'member')}`),
        admin && !c.is_default ? h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => addMembers() }, icon('person-add', { size: 18 }), 'Add members') : null),
      h('ul.drawer-list', list.map((m) => {
        const u = store.getUser(m.user_id);
        const me = m.user_id === meId;
        const parts = [
          userAvatar(m.user_id, { size: 'md' }),
          h('span.drawer-member-main',
            h('span.drawer-member-name.truncate', { dir: 'auto' }, me ? 'You' : store.userName(m.user_id)),
            h('span.drawer-member-sub.muted.truncate', u ? (u.disabled ? 'Account disabled' : u.status_text || `@${u.username}`) : '')),
          m.role === 'admin' ? h('span.drawer-badge', 'Group admin') : null,
        ];
        return h('li.drawer-member', me
          ? h('div.drawer-member-row', parts)
          : h('button.drawer-member-row', { type: 'button', 'aria-haspopup': 'menu', onClick: (e) => openMemberMenu(m, e.currentTarget) }, parts));
      })));
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Danger zone                                                                              */
  /* ---------------------------------------------------------------------------------------- */

  async function runSidebarAction(name) {
    const c = chat();
    const mod = await loadSidebar();
    if (c && mod && typeof mod[name] === 'function') await mod[name](c);
  }

  function paintDanger() {
    const c = chat();
    if (!c) return;
    const rows = [h('button.drawer-row.danger', { type: 'button', onClick: () => runSidebarAction('clearChat') },
      h('span.drawer-row-icon', icon('trash', { size: 22 })), h('span.drawer-row-main', h('span.drawer-row-title', 'Clear chat'), h('span.drawer-row-sub.muted', 'Removes the messages from your view only')))];
    if (c.kind === 'group' && !c.is_default) {
      rows.push(h('button.drawer-row.danger', { type: 'button', onClick: () => runSidebarAction('leaveChat') },
        h('span.drawer-row-icon', icon('logout', { size: 22 })), h('span.drawer-row-main', h('span.drawer-row-title', 'Leave group'))));
    }
    danger.replaceChildren(...rows);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Shared media / files / links / starred                                                   */
  /* ---------------------------------------------------------------------------------------- */

  function resetLists() {
    for (const c of cards) c.card.destroy();
    cards = [];
    lists = {};
    for (const t of TABS) lists[t.id] = { items: [], hasMore: false, loading: false, loaded: false, error: '' };
  }

  function paintTabs() {
    tabBar.replaceChildren(...TABS.map((t) => h('button.drawer-tab', {
      type: 'button',
      role: 'tab',
      id: `drawer-tab-${t.id}`,
      'aria-selected': String(t.id === tab),
      tabIndex: t.id === tab ? 0 : -1,
      class: { on: t.id === tab },
      onClick: () => selectTab(t.id),
      onKeydown: (e) => {
        const i = TABS.findIndex((x) => x.id === tab);
        const n = e.key === 'ArrowRight' ? i + 1 : e.key === 'ArrowLeft' ? i - 1 : -1;
        if (n >= 0 && n < TABS.length) {
          e.preventDefault();
          selectTab(TABS[n].id);
          const el = tabBar.querySelector(`#drawer-tab-${TABS[n].id}`);
          if (el instanceof HTMLElement) el.focus();
        }
      },
    }, t.label)));
    panel.setAttribute('aria-labelledby', `drawer-tab-${tab}`);
  }

  /** @param {string} id */
  function selectTab(id) {
    tab = id;
    paintTabs();
    paintPanel();
    const l = lists[id];
    if (!l.loaded && !l.loading) loadMore(id);
  }

  /** @param {string} id */
  async function loadMore(id) {
    const l = lists[id];
    const mine = token;
    if (!l || l.loading) return;
    l.loading = true;
    l.error = '';
    if (tab === id) paintPanel();
    try {
      /** @type {Record<string, any>} */
      const d = { chat_id: chatId, limit: PAGE };
      if (l.items.length) d.before_id = l.items[l.items.length - 1].id;
      let res;
      if (id === 'starred') res = await store.request('msg.starred', d);
      else res = await sharedRequest(() => store.request('msg.shared', { ...d, kind: id }));
      if (mine !== token || destroyed) return;
      const seen = new Set(l.items.map((m) => m.id));
      for (const m of res.messages || []) if (!seen.has(m.id)) l.items.push(m);
      l.hasMore = Boolean(res.has_more);
      l.loaded = true;
    } catch (err) {
      if (mine !== token || destroyed) return;
      l.error = errorText(err, 'Could not load this list.');
    } finally {
      if (mine === token && !destroyed) {
        l.loading = false;
        if (tab === id) paintPanel();
      }
    }
  }

  /** @param {any} m */
  function senderLine(m) {
    const who = store.me && m.sender_id === store.me.id ? 'You' : store.userName(m.sender_id);
    return `${who} · ${formatDayLabel(m.created_at)}`;
  }

  function paintPanel() {
    for (const c of cards) c.card.destroy();
    cards = [];
    const l = lists[tab];
    if (!l) return;
    /** @type {Node[]} */
    const out = [];
    if (tab === 'media') {
      out.push(h('div.drawer-media-grid', l.items.map((m) => {
        const att = m.attachment;
        return h('button.drawer-media', {
          type: 'button',
          'aria-label': `Open ${att ? att.name : 'media'}`,
          onClick: () => {
            const items = l.items.map((x) => lightboxItemFromMessage(x)).filter(Boolean);
            const index = items.findIndex((it) => it && it.url === (att && att.url));
            openLightbox({ items: /** @type {any[]} */ (items), index: Math.max(0, index) });
          },
        }, att && att.kind === 'image' ? h('img', { src: att.url, alt: '', loading: 'lazy', decoding: 'async' }) : h('span.drawer-media-video', icon('play', { size: 28 })),
        att && att.kind === 'video' && att.duration ? h('span.drawer-media-dur', formatDuration(att.duration)) : null);
      })));
    } else if (tab === 'files') {
      out.push(h('ul.drawer-list', l.items.map((m) => {
        const att = m.attachment;
        return h('li', h('a.drawer-file', { href: att ? `/files/${att.id}?dl=1` : null, download: att ? att.name : null },
          h('span.drawer-file-icon', icon(att && att.kind === 'audio' ? 'volume' : 'file', { size: 24 })),
          h('span.drawer-file-main', h('span.drawer-file-name.truncate', { dir: 'auto' }, att ? att.name : 'File'),
            h('span.drawer-file-sub.muted.truncate', [att ? formatBytes(att.size) : '', senderLine(m)].filter(Boolean).join(' · '))),
          icon('download', { size: 20 })));
      })));
    } else if (tab === 'links') {
      out.push(h('ul.drawer-list', l.items.map((m) => {
        const urls = findUrls(m.body || '');
        return h('li.drawer-link',
          urls.length ? h('a.rt-link.drawer-link-url.truncate', { href: urls[0].url, target: '_blank', rel: 'noopener noreferrer' }, urls[0].url) : null,
          h('span.drawer-link-text.muted.truncate', { dir: 'auto' }, makeSnippet(m.body || '', '', 80)),
          h('span.drawer-link-sub.muted', senderLine(m)));
      })));
    } else {
      out.push(h('div.drawer-cards', l.items.map((m) => {
        const card = createMessageCard(m, { showChat: false, onOpen: (msg) => opts.onJump(msg.id) });
        cards.push({ card, id: m.id });
        return card.el;
      })));
    }
    if (l.loading) out.push(h('div.drawer-loading', ui.spinner(22)));
    else if (l.error) out.push(h('div.drawer-error', h('p.error-text', { role: 'alert' }, l.error), h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => loadMore(tab) }, 'Try again')));
    else if (l.loaded && l.items.length === 0) out.push(h('p.muted.drawer-empty', tab === 'starred' ? 'No starred messages in this chat.' : `No ${tab} shared yet.`));
    else if (l.hasMore) out.push(h('button.btn.btn-secondary.btn-sm.drawer-more', { type: 'button', onClick: () => loadMore(tab) }, 'Show more'));
    panel.replaceChildren(...out);
  }

  /** A message changed or vanished: keep the shared lists honest. @param {any} m */
  function onMessageUpdate(m) {
    if (!m || m.chat_id !== chatId) return;
    let changed = false;
    for (const id of Object.keys(lists)) {
      const l = lists[id];
      const i = l.items.findIndex((x) => x.id === m.id);
      if (i < 0) continue;
      if (m.deleted || (id === 'starred' && !m.starred)) l.items.splice(i, 1);
      else store.patchMessage(l.items[i], m);
      if (id === tab) changed = true;
    }
    const entry = cards.find((c) => c.id === m.id);
    if (entry && !changed) entry.card.update(m);
    else if (changed) paintPanel();
  }

  /** @param {{chat_id: number, message_ids: number[]}} e */
  function onMessageRemoved(e) {
    if (e.chat_id !== chatId) return;
    const gone = new Set(e.message_ids);
    let changed = false;
    for (const id of Object.keys(lists)) {
      const before = lists[id].items.length;
      lists[id].items = lists[id].items.filter((m) => !gone.has(m.id));
      if (lists[id].items.length !== before && id === tab) changed = true;
    }
    if (changed) paintPanel();
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Wiring                                                                                   */
  /* ---------------------------------------------------------------------------------------- */

  function paintAll() {
    if (destroyed) return;
    paintHero();
    paintPrefs();
    paintPinned();
    paintMembers();
    paintDanger();
  }

  /** Coalesce bursts of directory events (presence flaps) into one repaint. */
  function scheduleRepaint() {
    if (repaintTimer) return;
    repaintTimer = window.setTimeout(() => {
      repaintTimer = 0;
      paintAll();
    }, 120);
  }

  resetLists();
  offs = [
    store.on(`chat:${chatId}`, () => paintAll()),
    store.on('users', () => scheduleRepaint()),
    store.on('message_update', onMessageUpdate),
    store.on('message_removed', onMessageRemoved),
  ];
  paintAll();
  paintTabs();
  paintPanel();
  loadMore('media');

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    token += 1;
    window.clearTimeout(repaintTimer);
    for (const off of offs.splice(0)) off();
    for (const c of cards) c.card.destroy();
    cards = [];
    container.replaceChildren();
  }

  return { focus: () => closeBtn.focus({ preventScroll: true }), destroy };
}
