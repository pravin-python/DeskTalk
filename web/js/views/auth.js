/**
 * views/auth.js - the signed-out screens (SPEC 9.2 "Auth", 4.2, 4.3, 0):
 * first-run "Set up your workspace" (asks for the setup code), login, registration (asks for the join
 * code while it is open) and the forced change-password screen.
 *
 * The field / form builders and the error mapping are exported for views/settings.js and views/admin.js.
 *
 * Mounted by main.js into #auth-root (docs/ui-core-api.md 1.2): `mount(container, ctx)` with
 * ctx = { mode:'auth'|'change_password', info, notice, me, done(me), logout, signal }.
 * Every screen is a real <form> with the right autocomplete attributes, server errors are shown inline,
 * Enter submits, and a rate-limit answer disables the submit button until it has expired.
 */

import { h, clear } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { api } from '../core/api.js';
import { cpLength, errorText, isInsecureRemote } from '../core/util.js';

const USERNAME_RE = /^[a-z0-9._-]{3,32}$/;
const INSECURE_TEXT = 'This connection is not encrypted. Anyone on this network can read your password and messages.';

let fieldSeq = 0;

/**
 * Human text for a wait in seconds ("12 s", "5 min").
 * @param {number} seconds
 * @returns {string}
 */
function formatWait(seconds) {
  const s = Math.max(1, Math.ceil(seconds));
  return s < 90 ? `${s} s` : `${Math.ceil(s / 60)} min`;
}

/**
 * Translate a failed REST / WebSocket call into where and how to show it.
 * @param {any} err ApiError or SocketError (or anything thrown)
 * @returns {{field: string|null, text: string, wait: number}}
 */
export function describeError(err) {
  const code = err && err.code;
  const reason = err && err.reason;
  const text = errorText(err, 'Something went wrong. Please try again.');
  switch (code) {
    case 'username_taken': return { field: 'username', text, wait: 0 };
    case 'name_taken': return { field: 'display_name', text, wait: 0 };
    case 'setup_code_required':
    case 'bad_setup_code': return { field: 'setup_code', text, wait: 0 };
    case 'bad_join_code': return { field: 'join_code', text, wait: 0 };
    case 'bad_request':
      return { field: reason === 'weak_password' ? 'password' : null, text, wait: 0 };
    case 'weak_password':
      return {
        field: 'password',
        text: reason === 'same_as_old' ? 'Choose a password that is different from your current one.' : text,
        wait: 0,
      };
    case 'forbidden':
      if (reason === 'bad_old_password') return { field: 'old_password', text: 'That is not your current password.', wait: 0 };
      return { field: null, text, wait: 0 };
    case 'conflict':
      if (reason === 'name_taken') return { field: 'display_name', text, wait: 0 };
      return { field: reason === 'username_taken' ? 'username' : null, text, wait: 0 };
    case 'rate_limited': {
      const retry = err.retryAfter ?? err.retry_after;
      const wait = Number.isFinite(retry) && retry > 0 ? retry : 5;
      return { field: null, text: `Too many attempts. Please wait ${formatWait(wait)} and try again.`, wait };
    }
    case 'server_busy': {
      const retry = err.retryAfter ?? err.retry_after;
      return { field: null, text, wait: Number.isFinite(retry) ? retry : 0 };
    }
    default: return { field: null, text, wait: 0 };
  }
}

/**
 * One labelled input with its inline error slot.
 * @param {{name: string, label: string, type?: string, autocomplete: string, hint?: string, maxlength?: number,
 *          value?: string, plain?: boolean, secret?: boolean, readOnly?: boolean}} o
 * @returns {{name: string, el: HTMLElement, input: HTMLInputElement, value: string, setError: (msg: string) => void, clearError: () => void}}
 */
export function createField(o) {
  fieldSeq += 1;
  const id = `auth-${o.name}-${fieldSeq}`;
  const errId = `${id}-err`;
  const hintId = `${id}-hint`;
  const error = h('p.error-text', { id: errId, role: 'alert', hidden: true });
  const input = /** @type {HTMLInputElement} */ (h('input.input', {
    id,
    name: o.name,
    type: o.secret ? 'password' : o.type || 'text',
    autocomplete: o.autocomplete,
    maxlength: o.maxlength || null,
    autocapitalize: o.plain ? 'none' : null,
    autocorrect: o.plain ? 'off' : null,
    spellcheck: o.plain || o.secret ? 'false' : null,
    'aria-required': 'true',
    'aria-describedby': o.hint ? `${hintId} ${errId}` : errId,
    dir: o.plain || o.secret ? null : 'auto',
  }));
  if (o.value) input.value = o.value;
  let control = /** @type {HTMLElement} */ (input);
  if (o.secret) {
    const toggle = h('button.btn-icon.pw-toggle', {
      type: 'button',
      'aria-label': 'Show password',
      'aria-pressed': 'false',
      onClick: () => {
        const show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        toggle.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
        toggle.setAttribute('aria-pressed', String(show));
        toggle.replaceChildren(icon(show ? 'eye-off' : 'eye', { size: 20 }));
        input.focus();
      },
    }, icon('eye', { size: 20 }));
    control = h('div.input-wrap', input, toggle);
  }
  const el = h('div.field', h('label.label', { for: id }, o.label), control,
    o.hint ? h('p.hint', { id: hintId }, o.hint) : null, error);
  return {
    name: o.name,
    el,
    input,
    get value() { return input.value; },
    setError(msg) {
      error.textContent = msg;
      error.hidden = false;
      input.setAttribute('aria-invalid', 'true');
    },
    clearError() {
      error.textContent = '';
      error.hidden = true;
      input.removeAttribute('aria-invalid');
    },
  };
}

/**
 * A real <form> with inline errors, busy state and rate-limit cooldown.
 * @param {{label: string, fields: ReturnType<typeof createField>[], submitLabel: string,
 *          validate?: (v: Record<string, string>) => Record<string, string>|null,
 *          onSubmit: (v: Record<string, string>) => Promise<void>, isAlive: () => boolean, timers: Set<any>,
 *          lead?: Node[], submitClass?: string}} o
 * @returns {{el: HTMLFormElement, focusFirst: () => void}}
 */
export function createForm(o) {
  const { fields } = o;
  const formError = h('div.auth-error', { role: 'alert', hidden: true });
  const submit = /** @type {HTMLButtonElement} */ (h(`button.btn.btn-primary.auth-submit${o.submitClass === undefined ? '.btn-block' : o.submitClass}`, { type: 'submit' }, o.submitLabel));
  let busy = false;
  let cooldownTimer = /** @type {any} */ (null);

  const showFormError = (text) => {
    formError.textContent = text;
    formError.hidden = !text;
  };
  const clearErrors = () => {
    showFormError('');
    for (const f of fields) f.clearError();
  };
  const setBusy = (b) => {
    busy = b;
    submit.disabled = b;
    submit.setAttribute('aria-busy', String(b));
    submit.replaceChildren(b ? h('span.spinner', { 'aria-hidden': 'true' }) : '', b ? ' Please wait…' : o.submitLabel);
  };
  const stopCooldown = () => {
    if (cooldownTimer) {
      clearInterval(cooldownTimer);
      o.timers.delete(cooldownTimer);
      cooldownTimer = null;
    }
  };
  const startCooldown = (seconds) => {
    stopCooldown();
    const until = Date.now() + seconds * 1000;
    submit.disabled = true;
    const tick = () => {
      const left = Math.ceil((until - Date.now()) / 1000);
      if (left <= 0 || !o.isAlive()) {
        stopCooldown();
        submit.disabled = false;
        if (formError.dataset.cooldown) showFormError('');
        delete formError.dataset.cooldown;
        return;
      }
      formError.dataset.cooldown = '1';
      formError.textContent = `Too many attempts. Please wait ${formatWait(left)} and try again.`;
    };
    cooldownTimer = setInterval(tick, 1000);
    o.timers.add(cooldownTimer);
    tick();
  };

  const form = /** @type {HTMLFormElement} */ (h('form.auth-form', {
    novalidate: true,
    'aria-label': o.label,
    onSubmit: async (e) => {
      e.preventDefault();
      if (busy || cooldownTimer) return;
      clearErrors();
      const values = {};
      for (const f of fields) values[f.name] = f.value;
      const problems = o.validate ? o.validate(values) : null;
      if (problems) {
        let first = null;
        for (const f of fields) {
          if (problems[f.name]) {
            f.setError(problems[f.name]);
            if (!first) first = f;
          }
        }
        if (first) first.input.focus();
        return;
      }
      setBusy(true);
      try {
        await o.onSubmit(values);
      } catch (err) {
        if (!o.isAlive()) return;
        const d = describeError(err);
        const target = d.field ? fields.find((f) => f.name === d.field) : null;
        if (target) {
          target.setError(d.text);
          target.input.focus();
        } else {
          showFormError(d.text);
        }
        if (d.wait > 0) startCooldown(d.wait);
      } finally {
        if (o.isAlive()) {
          const keepDisabled = Boolean(cooldownTimer);
          setBusy(false);
          submit.disabled = keepDisabled;
        }
      }
    },
  }, ...(o.lead || []), ...fields.map((f) => f.el), formError, submit));

  return {
    el: form,
    focusFirst() {
      const f = fields.find((x) => x.input.type !== 'hidden' && !x.input.readOnly);
      if (f) f.input.focus({ preventScroll: true });
    },
  };
}

/**
 * Client-side password checks shared by the forms (the server has the final say, SPEC 4.1).
 * @param {Record<string, string>} v field values
 * @param {string} [key] name of the new-password field
 * @returns {Record<string, string>} problems by field name (empty when fine)
 */
export function passwordRules(v, key = 'password') {
  const problems = {};
  if (!v[key]) problems[key] = 'Enter a password.';
  else if (cpLength(v[key]) < 8) problems[key] = 'Use at least 8 characters.';
  else if (cpLength(v[key]) > 128) problems[key] = 'Use at most 128 characters.';
  else if (v.confirm !== undefined && v.confirm !== v[key]) problems.confirm = 'The two passwords do not match.';
  return problems;
}

/**
 * Mount the auth view.
 * @param {HTMLElement} container #auth-root
 * @param {{mode?: 'auth'|'change_password', info?: any, notice?: string|null, me?: any, done: (me: any) => void}} ctx
 * @returns {{unmount: () => void}}
 */
export function mount(container, ctx) {
  let alive = true;
  const timers = new Set();
  const isAlive = () => alive;
  const state = {
    info: ctx.info || { name: 'DeskTalk', registration_open: false, needs_setup: false, tls: false },
    notice: ctx.notice || null,
  };

  /** Re-read /api/info (registration may have been opened or closed since the page loaded). */
  async function refreshInfo() {
    try {
      state.info = await api.getInfo();
    } catch (_) {
      /* keep what we know */
    }
  }

  /**
   * Draw one screen into the container.
   * @param {HTMLElement} card
   * @param {Node[]} children
   */
  function paint(card, children) {
    for (const t of timers) clearInterval(t);
    timers.clear();
    clear(container);
    const banners = [];
    if (state.notice) banners.push(h('div.banner.banner-info.auth-banner', { role: 'status' }, icon('info', { size: 18 }), h('span', state.notice)));
    if (isInsecureRemote()) {
      banners.push(h('div.banner.banner-warn.auth-banner', { role: 'alert' }, icon('warning', { size: 18 }), h('span', INSECURE_TEXT)));
    }
    container.appendChild(h('div.auth-page',
      h('div.auth-shell',
        h('div.auth-brand', h('span.auth-logo', icon('chat', { size: 28 })), h('span.auth-ws.truncate', state.info.name || 'DeskTalk')),
        ...banners,
        card)));
    for (const child of children) card.appendChild(child);
  }

  /** @param {() => void} fn a screen builder */
  function show(fn) {
    if (alive) fn();
  }

  /* ------------------------------ screens ------------------------------ */

  function identityRules(v) {
    const problems = {};
    const name = v.display_name.trim();
    if (!name) problems.display_name = 'Enter your name.';
    else if (cpLength(name) > 40) problems.display_name = 'Use at most 40 characters.';
    const user = v.username.trim().toLowerCase();
    if (!USERNAME_RE.test(user)) problems.username = 'Use 3 to 32 letters, digits, dots, dashes or underscores.';
    return problems;
  }

  function loginScreen() {
    const username = createField({ name: 'username', label: 'Username', autocomplete: 'username', plain: true, maxlength: 64 });
    const password = createField({ name: 'password', label: 'Password', autocomplete: 'current-password', secret: true, maxlength: 256 });
    const form = createForm({
      label: 'Sign in',
      fields: [username, password],
      submitLabel: 'Sign in',
      isAlive,
      timers,
      validate: (v) => {
        const p = {};
        if (!v.username.trim()) p.username = 'Enter your username.';
        if (!v.password) p.password = 'Enter your password.';
        return Object.keys(p).length ? p : null;
      },
      onSubmit: async (v) => {
        const { me } = await api.login(v.username.trim().toLowerCase(), v.password);
        if (alive) ctx.done(me);
      },
    });
    const card = h('section.auth-card', { 'aria-labelledby': 'auth-title' },
      h('h1.auth-title', { id: 'auth-title' }, 'Sign in'),
      h('p.muted.auth-lead', `Welcome to ${state.info.name || 'DeskTalk'}.`));
    paint(card, [
      form.el,
      h('p.auth-aside.muted', 'Forgot password? Ask your admin to reset it.'),
      state.info.registration_open
        ? h('p.auth-aside', 'New here? ', h('button.btn-link', { type: 'button', onClick: openRegister }, 'Create an account'))
        : null,
    ]);
    form.focusFirst();
  }

  async function openRegister() {
    await refreshInfo();
    state.notice = null;
    show(state.info.needs_setup ? setupScreen : state.info.registration_open ? registerScreen : loginScreen);
  }

  async function openLogin() {
    await refreshInfo();
    state.notice = null;
    show(state.info.needs_setup ? setupScreen : loginScreen);
  }

  /**
   * Register / first-run share one body.
   * @param {boolean} setup first-run flow (creates the admin, asks for the setup code)
   */
  function accountScreen(setup) {
    const display = createField({ name: 'display_name', label: 'Your name', autocomplete: 'name', maxlength: 80, hint: 'This is how people see you.' });
    const username = createField({
      name: 'username', label: 'Username', autocomplete: 'username', plain: true, maxlength: 64,
      hint: '3 to 32 letters, digits, dots, dashes or underscores.',
    });
    const password = createField({ name: 'password', label: 'Password', autocomplete: 'new-password', secret: true, maxlength: 256, hint: 'At least 8 characters.' });
    const confirm = createField({ name: 'confirm', label: 'Repeat password', autocomplete: 'new-password', secret: true, maxlength: 256 });
    const code = setup
      ? createField({
        name: 'setup_code',
        label: 'Setup code',
        autocomplete: 'off',
        plain: true,
        maxlength: 32,
        hint: 'Shown in the window where the server was started and in the file setup_code.txt in its data folder.',
      })
      : createField({ name: 'join_code', label: 'Join code', autocomplete: 'off', plain: true, maxlength: 32, hint: 'Ask your admin for the join code.' });
    const form = createForm({
      label: setup ? 'Set up your workspace' : 'Create an account',
      fields: [display, username, password, confirm, code],
      submitLabel: setup ? 'Create admin account' : 'Create account',
      isAlive,
      timers,
      validate: (v) => {
        const p = { ...identityRules(v), ...passwordRules(v) };
        const c = v[code.name].trim();
        if (!c) p[code.name] = setup ? 'Enter the setup code.' : 'Enter the join code.';
        return Object.keys(p).length ? p : null;
      },
      onSubmit: async (v) => {
        const body = {
          username: v.username.trim().toLowerCase(),
          display_name: v.display_name.trim(),
          password: v.password,
        };
        if (setup) body.setup_code = v.setup_code.trim();
        else body.join_code = v.join_code.trim();
        try {
          const { me } = await api.register(body);
          if (alive) ctx.done(me);
        } catch (err) {
          if (err && err.code === 'registration_closed') {
            await refreshInfo();
            state.notice = 'Registration is closed. Ask your admin for an account.';
            show(loginScreen);
            return;
          }
          throw err;
        }
      },
    });
    const card = h('section.auth-card', { 'aria-labelledby': 'auth-title' },
      h('h1.auth-title', { id: 'auth-title' }, setup ? 'Set up your workspace' : 'Create your account'),
      h('p.muted.auth-lead', setup
        ? `Create the administrator account for ${state.info.name || 'DeskTalk'}. You will use it to add everyone else.`
        : `Join ${state.info.name || 'DeskTalk'}.`));
    paint(card, [
      form.el,
      setup ? null : h('p.auth-aside', 'Already have an account? ', h('button.btn-link', { type: 'button', onClick: openLogin }, 'Sign in')),
    ]);
    form.focusFirst();
  }

  const setupScreen = () => accountScreen(true);
  const registerScreen = () => accountScreen(false);

  function changeScreen() {
    const me = ctx.me || {};
    const who = h('input.sr-only', {
      type: 'text', name: 'username', value: me.username || '', autocomplete: 'username', readOnly: true, tabIndex: -1, 'aria-hidden': 'true',
    });
    const old = createField({ name: 'old_password', label: 'Current (temporary) password', autocomplete: 'current-password', secret: true, maxlength: 256 });
    const password = createField({ name: 'password', label: 'New password', autocomplete: 'new-password', secret: true, maxlength: 256, hint: 'At least 8 characters.' });
    const confirm = createField({ name: 'confirm', label: 'Repeat new password', autocomplete: 'new-password', secret: true, maxlength: 256 });
    const form = createForm({
      label: 'Choose a new password',
      fields: [old, password, confirm],
      submitLabel: 'Change password',
      isAlive,
      timers,
      lead: [who],
      validate: (v) => {
        const p = passwordRules(v);
        if (!v.old_password) p.old_password = 'Enter your current password.';
        else if (v.old_password === v.password) p.password = 'Choose a password that is different from your current one.';
        return Object.keys(p).length ? p : null;
      },
      onSubmit: async (v) => {
        await api.changePassword(v.old_password, v.password);
        const res = await api.getMe({ silent401: true });
        if (alive) ctx.done(res.me);
      },
    });
    const signOut = async () => {
      await api.logout();
      await refreshInfo();
      state.notice = 'You signed out.';
      show(state.info.needs_setup ? setupScreen : loginScreen);
    };
    const card = h('section.auth-card', { 'aria-labelledby': 'auth-title' },
      h('h1.auth-title', { id: 'auth-title' }, 'Choose a new password'),
      h('p.muted.auth-lead', 'Your administrator gave you a temporary password. Choose your own password to continue.'));
    paint(card, [
      form.el,
      h('p.auth-aside', h('button.btn-link', { type: 'button', onClick: signOut }, 'Sign out')),
    ]);
    form.focusFirst();
  }

  if (ctx.mode === 'change_password') changeScreen();
  else if (state.info.needs_setup) setupScreen();
  else loginScreen();

  return {
    unmount() {
      alive = false;
      for (const t of timers) clearInterval(t);
      timers.clear();
    },
  };
}
