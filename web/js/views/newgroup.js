/**
 * views/newgroup.js - the "New group" dialog (SPEC 9.2, 7.4 `chat.create_group`): group name,
 * optional description and the members. Opened from the sidebar menu and from the New chat dialog.
 * The members list is the multi-select `ui.peoplePicker` of core/ui.js.
 */

import { h } from '../core/dom.js';
import { store } from '../core/store.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { cpLength, errorText, pluralize } from '../core/util.js';

const TITLE_MAX = 60;
const DESCRIPTION_MAX = 500;

/**
 * Show the New group dialog.
 * @returns {Promise<any|null>} the created chat (already in the store and open), or null when cancelled
 */
export async function open() {
  const me = store.me;
  const maxMembers = Math.max(1, (store.limits.max_group_members || 200) - 1);
  const title = /** @type {HTMLInputElement} */ (h('input.input', {
    id: 'ng-title', type: 'text', maxlength: TITLE_MAX * 2, autocomplete: 'off', dir: 'auto', placeholder: 'Group name',
  }));
  const description = /** @type {HTMLTextAreaElement} */ (h('textarea.input', {
    id: 'ng-description', rows: 2, maxlength: DESCRIPTION_MAX * 2, dir: 'auto', placeholder: 'What is this group for? (optional)',
  }));
  const error = h('p.error-text', { role: 'alert', hidden: true });
  const count = h('p.hint.ng-count', { 'aria-live': 'polite' }, 'No members selected yet.');
  const picker = ui.peoplePicker({
    multi: true,
    exclude: me ? [me.id] : [],
    onPick: (_user, _on, all) => {
      const n = all ? all.length : 0;
      count.textContent = n ? `${n} ${pluralize(n, 'member')} selected` : 'No members selected yet.';
    },
  });
  const content = h('div.ng',
    h('div.field', h('label.label', { for: 'ng-title' }, 'Group name'), title),
    h('div.field', h('label.label', { for: 'ng-description' }, 'Description'), description),
    h('div.field', h('span.label', 'Add members'), count, picker.el),
    error);

  /** @type {any} */
  let created = null;

  /**
   * @param {string} text
   * @param {HTMLElement|null} field element to focus
   * @returns {false} so that callers can `return fail(...)` to keep the dialog open
   */
  const fail = (text, field) => {
    error.textContent = text;
    error.hidden = false;
    if (field) field.focus();
    return false;
  };

  /**
   * @param {{setBusy: (b: boolean) => void}} handle
   * @returns {Promise<false|undefined>} false keeps the dialog open
   */
  const create = async (handle) => {
    error.hidden = true;
    const name = title.value.trim();
    const about = description.value.trim();
    const memberIds = picker.getSelected().map((u) => u.id);
    if (!name) return fail('Give the group a name.', title);
    if (cpLength(name) > TITLE_MAX) return fail(`The name can have up to ${TITLE_MAX} characters.`, title);
    if (cpLength(about) > DESCRIPTION_MAX) return fail(`The description can have up to ${DESCRIPTION_MAX} characters.`, description);
    if (memberIds.length > maxMembers) return fail(`A group can have up to ${maxMembers + 1} members including you. Remove ${memberIds.length - maxMembers}.`, null);
    handle.setBusy(true);
    try {
      const body = { title: name, member_ids: memberIds };
      if (about) body.description = about;
      const res = await store.request('chat.create_group', body);
      created = res && res.chat ? store.getChat(res.chat.id) || res.chat : null;
    } catch (err) {
      handle.setBusy(false);
      return fail(errorText(err, 'Could not create the group.'), null);
    }
    handle.setBusy(false);
    return undefined;
  };

  const dlg = ui.dialog({
    title: 'New group',
    size: 'md',
    className: 'dialog-people',
    content,
    initialFocus: title,
    actions: [
      { label: 'Cancel', value: false },
      { label: 'Create group', primary: true, value: true, onClick: create },
    ],
  });
  title.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      const btn = dlg.panel.querySelector('.dialog-actions .btn-primary');
      if (btn) /** @type {HTMLElement} */ (btn).click();
    }
  });
  await dlg.closed;
  picker.destroy();
  if (created) router.openChat(created.id);
  return created;
}
