/**
 * views/settings.js - the `#/settings[/<tab>]` screen (SPEC 9.2 "Settings", 9.4, 8.1, 9.7):
 *   profile (display name, about), privacy (read receipts, last seen), notifications (sound, volume,
 *   test sound, previews, desktop-notification status), appearance (theme, font size, Enter sends) and
 *   account (change password, sessions, sign out).
 * Device preferences go through `prefs`; profile / privacy go through `profile.update`.
 * Exports the small `switchRow` / `radioGroup` builders that views/admin.js reuses.
 */

import { h, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { userAvatar } from '../core/avatar.js';
import { api } from '../core/api.js';
import { store, prefs } from '../core/store.js';
import { router } from '../core/router.js';
import { ui } from '../core/ui.js';
import { sound } from '../core/sound.js';
import { notify } from '../core/notify.js';
import { cpLength, errorText, formatDateTime, isInsecureRemote } from '../core/util.js';
import { createField, createForm, passwordRules } from './auth.js';

const INSECURE_TEXT = 'This connection is not encrypted. Anyone on this network can read your password and messages.';
let seq = 0;

/* ------------------------------------------------------------------------------------------ */
/* Small builders                                                                             */
/* ------------------------------------------------------------------------------------------ */

/**
 * A titled switch row (`<input type=checkbox role=switch>` in the `.switch` look).
 * @param {{title: string, hint?: string, checked?: boolean, disabled?: boolean, onChange?: (checked: boolean, input: HTMLInputElement) => void}} o
 * @returns {{el: HTMLElement, input: HTMLInputElement}}
 */
export function switchRow(o) {
  seq += 1;
  const id = `st-switch-${seq}`;
  const input = /** @type {HTMLInputElement} */ (h('input', {
    type: 'checkbox',
    id,
    role: 'switch',
    checked: Boolean(o.checked),
    disabled: Boolean(o.disabled),
    'aria-describedby': o.hint ? `${id}-hint` : null,
    onChange: () => {
      if (o.onChange) o.onChange(input.checked, input);
    },
  }));
  const el = h('div.st-row',
    h('div.st-row-text', h('label.st-row-title', { for: id }, o.title), o.hint ? h('p.hint', { id: `${id}-hint` }, o.hint) : null),
    h('label.switch', input, h('span.track')));
  return { el, input };
}

/**
 * A group of radio buttons in a fieldset.
 * @param {{legend: string, name: string, value: string, options: Array<{value: string, label: string}>, onChange: (value: string) => void}} o
 * @returns {HTMLElement}
 */
export function radioGroup(o) {
  return h('fieldset.st-radios',
    h('legend.st-row-title', o.legend),
    ...o.options.map((opt) => h('label.st-radio',
      h('input', { type: 'radio', name: o.name, value: opt.value, checked: opt.value === o.value, onChange: () => o.onChange(opt.value) }),
      h('span', opt.label))));
}

/**
 * @param {string} title
 * @param {...any} children
 * @returns {HTMLElement}
 */
function card(title, ...children) {
  return h('section.st-card', title ? h('h2.st-card-title', title) : null, ...children);
}

/**
 * Short device description from a User-Agent string.
 * @param {string} ua
 * @returns {string}
 */
function describeAgent(ua) {
  const s = String(ua || '');
  let browser = 'Browser';
  if (/Edg\//.test(s)) browser = 'Edge';
  else if (/OPR\/|Opera/.test(s)) browser = 'Opera';
  else if (/Firefox\//.test(s)) browser = 'Firefox';
  else if (/Chrome\/|CriOS\//.test(s)) browser = 'Chrome';
  else if (/Safari\//.test(s)) browser = 'Safari';
  let os = '';
  if (/Windows/.test(s)) os = 'Windows';
  else if (/Android/.test(s)) os = 'Android';
  else if (/iPhone|iPad|iPod/.test(s)) os = 'iOS';
  else if (/Mac OS X|Macintosh/.test(s)) os = 'macOS';
  else if (/Linux|X11/.test(s)) os = 'Linux';
  if (!s) return 'Unknown device';
  return os ? `${browser} on ${os}` : browser;
}

/**
 * `profile.update` with a toast on failure.
 * @param {object} patch
 * @returns {Promise<boolean>}
 */
async function saveProfile(patch) {
  try {
    await store.request('profile.update', patch);
    return true;
  } catch (err) {
    ui.toast(errorText(err, 'Could not save your settings.'), { type: 'error' });
    return false;
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Panels: each returns { el, destroy? }                                                      */
/* ------------------------------------------------------------------------------------------ */

/** @param {{timers: Set<any>, isAlive: () => boolean}} env */
function profilePanel(env) {
  const me = store.me;
  const name = createField({ name: 'display_name', label: 'Display name', autocomplete: 'off', maxlength: 80, value: me.display_name, hint: 'Shown to everyone in chats. Names are unique.' });
  const about = createField({ name: 'status_text', label: 'About', autocomplete: 'off', maxlength: 280, value: me.status_text || '', hint: 'A short line under your name (up to 140 characters).' });
  const saved = h('p.hint.st-saved', { role: 'status' });
  const form = createForm({
    label: 'Profile',
    fields: [name, about],
    submitLabel: 'Save changes',
    submitClass: '',
    isAlive: env.isAlive,
    timers: env.timers,
    validate: (v) => {
      const p = {};
      const n = v.display_name.trim();
      if (!n) p.display_name = 'Enter a name.';
      else if (cpLength(n) > 40) p.display_name = 'Use at most 40 characters.';
      if (cpLength(v.status_text.trim()) > 140) p.status_text = 'Use at most 140 characters.';
      return Object.keys(p).length ? p : null;
    },
    onSubmit: async (v) => {
      saved.textContent = '';
      const patch = {};
      if (v.display_name.trim() !== store.me.display_name) patch.display_name = v.display_name.trim();
      if (v.status_text.trim() !== (store.me.status_text || '')) patch.status_text = v.status_text.trim();
      if (!Object.keys(patch).length) {
        saved.textContent = 'Nothing to save.';
        return;
      }
      await store.request('profile.update', patch);
      name.input.value = store.me.display_name;
      about.input.value = store.me.status_text || '';
      saved.textContent = 'Saved.';
    },
  });
  const avatarHost = h('div.st-avatar');
  const draw = () => {
    avatarHost.replaceChildren(userAvatar(store.me, { size: 'xl', online: null }));
  };
  draw();
  const off = store.on('me', draw);
  return {
    el: h('div.st-stack',
      card('',
        h('div.st-profile', avatarHost,
          h('div.st-profile-text', h('p.st-profile-name', { dir: 'auto' }, me.display_name), h('p.muted', `@${me.username}`),
            h('p.hint', 'Your username cannot be changed.'))),
        form.el, saved)),
    destroy: off,
  };
}

function privacyPanel() {
  const me = store.me;
  const receipts = switchRow({
    title: 'Read receipts',
    hint: 'Let others see when you have read their messages (blue ticks). Turning this off does not hide ticks that were already shown. '
      + 'If you turn it back on, messages you read in the meantime will then show as read.',
    checked: me.read_receipts,
    onChange: async (on, input) => {
      input.disabled = true;
      const ok = await saveProfile({ read_receipts: on });
      input.disabled = false;
      if (!ok) input.checked = !on;
      else if (on && store.activeChatId) store.requestRead(store.activeChatId);
    },
  });
  const lastSeen = switchRow({
    title: 'Last seen',
    hint: 'Show when you were last online. People can still see that you are online while you use DeskTalk.',
    checked: me.show_last_seen !== false,
    onChange: async (on, input) => {
      input.disabled = true;
      const ok = await saveProfile({ show_last_seen: on });
      input.disabled = false;
      if (!ok) input.checked = !on;
    },
  });
  const off = store.on('me', (m) => {
    if (!receipts.input.disabled) receipts.input.checked = Boolean(m.read_receipts);
    if (!lastSeen.input.disabled) lastSeen.input.checked = m.show_last_seen !== false;
  });
  return { el: card('Privacy', receipts.el, lastSeen.el), destroy: off };
}

function notificationsPanel() {
  const soundRow = switchRow({
    title: 'Message sounds',
    hint: 'Play a short chime when a message arrives.',
    checked: prefs.get('sound'),
    onChange: (on) => {
      prefs.set('sound', on);
      sync();
    },
  });
  const volumeOut = h('output.st-volume-out');
  const volume = h('input.st-range', {
    type: 'range',
    min: 0,
    max: 100,
    step: 5,
    'aria-label': 'Volume',
    value: Math.round(prefs.get('volume') * 100),
    onInput: (e) => {
      const v = Number(/** @type {HTMLInputElement} */ (e.target).value);
      volumeOut.textContent = `${v}%`;
      prefs.set('volume', v / 100);
    },
  });
  const testBtn = h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => sound.test() }, icon('volume', { size: 18 }), 'Play test sound');
  const volumeRow = h('div.st-row.st-row-wide',
    h('div.st-row-text', h('span.st-row-title', 'Volume'), h('p.hint', 'The phone silent switch may mute DeskTalk.')),
    h('div.st-volume', volume, volumeOut, testBtn));
  const sentRow = switchRow({
    title: 'Sent sound',
    hint: 'A soft tick when your message has been sent.',
    checked: prefs.get('sentSound'),
    onChange: (on) => prefs.set('sentSound', on),
  });
  const previewRow = switchRow({
    title: 'Show message previews',
    hint: 'Include the message text in notifications. When off they only say "New message".',
    checked: prefs.get('previews'),
    onChange: (on) => prefs.set('previews', on),
  });
  const desktopRow = switchRow({
    title: 'Desktop notifications',
    hint: 'Show a notification when DeskTalk is in the background.',
    checked: prefs.get('desktopNotifications'),
    onChange: (on) => prefs.set('desktopNotifications', on),
  });

  const statusLine = h('p.st-notify-status', { role: 'status' });
  const noteLine = h('p.hint.st-notify-note');
  const whyBody = h('p.hint.st-why', { hidden: true });
  const whyBtn = h('button.btn-link', {
    type: 'button',
    'aria-expanded': 'false',
    onClick: () => {
      const open = whyBody.hidden;
      whyBody.hidden = !open;
      whyBtn.setAttribute('aria-expanded', String(open));
    },
  }, 'Why?');
  const enableBtn = h('button.btn.btn-secondary.btn-sm', {
    type: 'button',
    onClick: async () => {
      await notify.requestPermission();
      sync();
    },
  }, icon('bell', { size: 18 }), 'Enable desktop notifications');
  const statusBox = h('div.st-notify', statusLine, h('div.row.st-notify-actions', whyBtn, enableBtn), whyBody, noteLine);

  function sync() {
    const on = prefs.get('sound');
    volume.disabled = !on;
    testBtn.disabled = !on;
    sentRow.input.disabled = !on;
    const st = notify.status();
    statusLine.textContent = st.text;
    whyBody.textContent = st.why || '';
    whyBtn.hidden = !st.why;
    if (!st.why) whyBody.hidden = true;
    noteLine.textContent = st.note || '';
    noteLine.hidden = !st.note;
    enableBtn.hidden = st.state !== 'default';
    desktopRow.input.disabled = st.state === 'unavailable' || st.state === 'blocked';
  }
  volumeOut.textContent = `${Math.round(prefs.get('volume') * 100)}%`;
  sync();
  const off = store.on('prefs', sync);
  return {
    el: h('div.st-stack',
      card('Sound', soundRow.el, volumeRow, sentRow.el),
      card('Notifications', previewRow.el, desktopRow.el, statusBox)),
    destroy: off,
  };
}

function appearancePanel() {
  const theme = radioGroup({
    legend: 'Theme',
    name: 'st-theme',
    value: prefs.get('theme'),
    options: [{ value: 'system', label: 'Same as my device' }, { value: 'light', label: 'Light' }, { value: 'dark', label: 'Dark' }],
    onChange: (v) => prefs.set('theme', v),
  });
  const font = radioGroup({
    legend: 'Text size',
    name: 'st-font',
    value: prefs.get('fontSize'),
    options: [{ value: 'small', label: 'Small' }, { value: 'normal', label: 'Normal' }, { value: 'large', label: 'Large' }],
    onChange: (v) => prefs.set('fontSize', v),
  });
  const enter = switchRow({
    title: 'Enter sends message',
    hint: 'When off, Enter starts a new line and only the Send button sends. (Shift+Enter always starts a new line.)',
    checked: prefs.enterSends(),
    onChange: (on) => prefs.set('enterSends', on),
  });
  return { el: h('div.st-stack', card('', theme), card('', font), card('', enter.el)) };
}

/**
 * Sessions list with sign-out of single sessions / all others.
 * @param {() => boolean} isAlive
 * @returns {HTMLElement}
 */
function sessionsCard(isAlive) {
  const list = h('ul.st-sessions', { role: 'list' });
  const status = h('p.hint', { role: 'status' });
  const allBtn = h('button.btn.btn-secondary.btn-sm', { type: 'button', hidden: true, onClick: signOutOthers }, 'Sign out all other devices');
  const el = card('Devices signed in', status, list, allBtn);

  async function load() {
    status.textContent = 'Loading…';
    try {
      const { sessions } = await api.listSessions();
      if (!isAlive()) return;
      status.textContent = '';
      list.replaceChildren(...sessions.map((s) => h('li.st-session',
        h('span.st-session-icon', icon('devices', { size: 22 })),
        h('div.st-session-text',
          h('p.st-session-title', describeAgent(s.user_agent), s.current ? h('span.badge.badge-unread.st-current', 'This device') : null),
          h('p.hint', `${s.ip || 'Unknown address'} · last active ${formatDateTime(s.last_used_at)}`)),
        s.current ? null : h('button.btn.btn-secondary.btn-sm', { type: 'button', onClick: () => revoke(s.id) }, 'Sign out'))));
      allBtn.hidden = !sessions.some((s) => !s.current);
    } catch (err) {
      if (isAlive()) status.textContent = errorText(err, 'Could not load your sessions.');
    }
  }

  async function revoke(id) {
    try {
      await api.revokeSession({ id });
      ui.toast('That device was signed out.', { type: 'success' });
    } catch (err) {
      ui.toast(errorText(err, 'Could not sign that device out.'), { type: 'error' });
    }
    load();
  }

  async function signOutOthers() {
    const ok = await ui.confirm({ title: 'Sign out all other devices?', message: 'Every other browser and phone signed in to your account will have to sign in again.', confirmLabel: 'Sign out', danger: true });
    if (!ok) return;
    try {
      await api.revokeSession({ all_others: true });
      ui.toast('All other devices were signed out.', { type: 'success' });
    } catch (err) {
      ui.toast(errorText(err, 'Could not sign the other devices out.'), { type: 'error' });
    }
    load();
  }

  load();
  return el;
}

/**
 * @param {{timers: Set<any>, isAlive: () => boolean, logout: (o?: object) => Promise<boolean>}} env
 */
function accountPanel(env) {
  const me = store.me;
  const who = h('input.sr-only', { type: 'text', name: 'username', value: me.username, autocomplete: 'username', readOnly: true, tabIndex: -1, 'aria-hidden': 'true' });
  const old = createField({ name: 'old_password', label: 'Current password', autocomplete: 'current-password', secret: true, maxlength: 256 });
  const password = createField({ name: 'password', label: 'New password', autocomplete: 'new-password', secret: true, maxlength: 256, hint: 'At least 8 characters.' });
  const confirm = createField({ name: 'confirm', label: 'Repeat new password', autocomplete: 'new-password', secret: true, maxlength: 256 });
  const form = createForm({
    label: 'Change password',
    fields: [old, password, confirm],
    submitLabel: 'Change password',
    submitClass: '',
    isAlive: env.isAlive,
    timers: env.timers,
    lead: [who],
    validate: (v) => {
      const p = passwordRules(v);
      if (!v.old_password) p.old_password = 'Enter your current password.';
      else if (v.old_password === v.password) p.password = 'Choose a password that is different from your current one.';
      return Object.keys(p).length ? p : null;
    },
    onSubmit: async (v) => {
      await api.changePassword(v.old_password, v.password);
      for (const f of [old, password, confirm]) f.input.value = '';
      ui.toast('Password changed. Your other devices were signed out.', { type: 'success' });
    },
  });

  const clearDevice = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', id: 'st-clear-device', checked: true }));
  const signOut = h('button.btn.btn-danger', { type: 'button', onClick: () => env.logout({ clearDevice: clearDevice.checked }) }, icon('logout', { size: 18 }), 'Sign out');

  return {
    el: h('div.st-stack',
      card('Change password', form.el),
      sessionsCard(env.isAlive),
      card('Sign out',
        h('label.st-check', clearDevice, h('span', 'Also clear this device (drafts and unsent messages)')),
        signOut)),
  };
}

/* ------------------------------------------------------------------------------------------ */
/* Tabbed pane (shared with views/admin.js)                                                   */
/* ------------------------------------------------------------------------------------------ */

/**
 * A main-pane view with a header (back button on narrow screens, title), a tab bar and one panel.
 * The tab lives in the route (`<base>/<tab>`), so reloads and Back work; tab switches replace the
 * history entry. Each tab's `build(env)` returns `{ el, destroy? }`; `env` = `{ timers, isAlive, ...o.env }`
 * where `timers` is a Set of interval ids that is cleared when the tab changes.
 * @param {HTMLElement} container
 * @param {{title: string, base: string, tabs: Array<{id: string, label: string, build: (env: any) => {el: Node, destroy?: () => void}}>,
 *          route?: {tab?: string}, banner?: Node|null, env?: object}} o
 * @returns {{update: (route: any) => void, unmount: () => void}}
 */
export function mountTabbed(container, o) {
  const { tabs: TABS } = o;
  let alive = true;
  /** @type {Set<any>} */
  let timers = new Set();
  /** @type {null|{destroy?: () => void}} */
  let panel = null;
  let current = '';

  const go = (id) => router.go(`${o.base}/${id}`, { replace: true });
  const back = h('button.btn-icon.only-narrow', { type: 'button', 'aria-label': 'Back', onClick: () => router.up() }, icon('back'));
  const header = h('header.pane-header', back, h('h1.pane-title.grow.truncate', o.title));
  const tabBtns = TABS.map((t) => h('button.tv-tab', {
    type: 'button',
    role: 'tab',
    id: `tv-tab-${t.id}`,
    'aria-controls': 'tv-panel',
    onClick: () => go(t.id),
  }, t.label));
  const tabBar = h('div.tv-tabs', { role: 'tablist', 'aria-label': `${o.title} sections` }, ...tabBtns);
  tabBar.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return;
    const i = TABS.findIndex((t) => t.id === current);
    let next;
    if (e.key === 'ArrowRight') next = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') next = (i + TABS.length - 1) % TABS.length;
    else if (e.key === 'Home') next = 0;
    else next = TABS.length - 1;
    e.preventDefault();
    go(TABS[next].id);
    tabBtns[next].focus();
  });
  const panelHost = h('div.tv-panel', { id: 'tv-panel', role: 'tabpanel', tabIndex: -1 });
  container.append(header, tabBar, h('div.pane-body.tv-body', o.banner || null, panelHost));

  function destroyPanel() {
    for (const t of timers) clearInterval(t);
    timers = new Set();
    if (panel && typeof panel.destroy === 'function') panel.destroy();
    panel = null;
  }

  /** @param {string|undefined} id */
  function select(id) {
    const tab = TABS.find((t) => t.id === id) || TABS[0];
    if (tab.id === current) return;
    current = tab.id;
    destroyPanel();
    TABS.forEach((t, i) => {
      const on = t.id === tab.id;
      tabBtns[i].setAttribute('aria-selected', String(on));
      tabBtns[i].tabIndex = on ? 0 : -1;
    });
    panelHost.setAttribute('aria-labelledby', `tv-tab-${tab.id}`);
    clear(panelHost);
    try {
      panel = tab.build({ ...o.env, timers, isAlive: () => alive });
      panelHost.appendChild(panel.el);
    } catch (err) {
      console.error(`[${o.base}] could not build the "${tab.id}" panel`, err);
      panelHost.appendChild(h('div.empty-state', h('p', 'This part of the screen could not be shown.')));
    }
  }

  select(o.route && o.route.tab);

  return {
    update(route) {
      select(route && route.tab);
    },
    unmount() {
      alive = false;
      destroyPanel();
    },
  };
}

/* ------------------------------------------------------------------------------------------ */
/* The view                                                                                   */
/* ------------------------------------------------------------------------------------------ */

const TABS = [
  { id: 'profile', label: 'Profile', build: profilePanel },
  { id: 'privacy', label: 'Privacy', build: privacyPanel },
  { id: 'notifications', label: 'Notifications', build: notificationsPanel },
  { id: 'appearance', label: 'Appearance', build: appearancePanel },
  { id: 'account', label: 'Account', build: accountPanel },
];

/**
 * Mount the Settings view.
 * @param {HTMLElement} container #pane-main
 * @param {{route: {tab?: string}, logout: (o?: object) => Promise<boolean>}} ctx
 * @returns {{update: (route: any) => void, unmount: () => void}}
 */
export function mount(container, ctx) {
  const banner = isInsecureRemote()
    ? h('div.banner.banner-warn.tv-banner', { role: 'alert' }, icon('warning', { size: 18 }), h('span', INSECURE_TEXT))
    : null;
  return mountTabbed(container, { title: 'Settings', base: '/settings', tabs: TABS, route: ctx.route, banner, env: { logout: ctx.logout } });
}
