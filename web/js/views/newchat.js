/**
 * views/newchat.js - "New chat" (SPEC 9.2 sidebar) and the one shared way to open a direct chat.
 *
 * `open()` shows a dialog with a "New group" row and a searchable list of people; `openDirect(userId)`
 * starts (or reopens) the direct chat with a person and navigates to it. The people list is the target
 * picker of views/forward.js (docs/ui-conv-api.md section 3), loaded on demand.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { avatar } from '../core/avatar.js';
import { store } from '../core/store.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { errorText } from '../core/util.js';
import * as newgroup from './newgroup.js';

/**
 * Start (or reopen) the direct chat with a person and show it. Never throws: a failure becomes an
 * error toast and the result is `null`. `userId` may be my own id (the "You" self chat).
 * @param {number} userId
 * @param {{replace?: boolean}} [opts] replace the history entry instead of pushing one
 * @returns {Promise<any|null>} the chat
 */
export async function openDirect(userId, { replace = false } = {}) {
  try {
    let chat = store.directChatWith(userId);
    if (!chat) {
      const res = await store.request('chat.open_direct', { user_id: userId });
      chat = res && res.chat ? store.getChat(res.chat.id) || res.chat : null;
    }
    if (!chat) throw new Error('chat.open_direct returned no chat');
    router.openChat(chat.id, { replace });
    return chat;
  } catch (err) {
    ui.toast(errorText(err, 'Could not open this chat.'), { type: 'error' });
    return null;
  }
}

/**
 * Show the New chat dialog. Resolves when it has been closed.
 * @returns {Promise<void>}
 */
export async function open() {
  let forward;
  try {
    forward = await import('./forward.js');
  } catch (err) {
    console.error('[newchat] the people picker could not be loaded', err);
    ui.toast('The people list is not available right now.', { type: 'error' });
    return;
  }
  /** @type {null|number|'group'} */
  let choice = null;
  /** @type {any} */
  let dlg = null;
  const finish = (value) => {
    if (choice !== null) return;
    choice = value;
    if (dlg) dlg.close();
  };
  const picker = forward.createTargetPicker({
    people: true,
    chats: false,
    max: 1,
    onChange: (selection) => {
      const first = selection && selection[0];
      if (first && first.kind === 'user') finish(first.id);
    },
  });
  const groupRow = h('button.nc-group', { type: 'button', onClick: () => finish('group') },
    avatar({ id: 0, name: '', group: true }, { size: 'md' }),
    h('span.grow', h('span.nc-group-title', 'New group'), h('span.muted.nc-group-sub', 'Chat with several people at once')),
    icon('chevron-right', { size: 20 }));
  dlg = ui.dialog({
    title: 'New chat',
    size: 'sm',
    className: 'dialog-people',
    content: h('div.nc', groupRow, picker.el),
    actions: [{ label: 'Cancel', value: false }],
  });
  picker.focus();
  await dlg.closed;
  picker.destroy();
  if (choice === 'group') await newgroup.open();
  else if (typeof choice === 'number') await openDirect(choice);
}
