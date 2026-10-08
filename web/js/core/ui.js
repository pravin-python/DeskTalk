/**
 * core/ui.js - shared UI primitives: toasts, confirm/prompt/dialog, popovers, menus, bottom sheets,
 * focus trap, long-press helper, live-region announcements, drawer switch and the fatal screen.
 * Overlays register as router layers so Escape and the hardware Back button close them (SPEC 9.8).
 * Styled by css/base.css (`.toast`, `.dialog`, `.popover`, `.menu`, `.sheet`, ...).
 *
 * Exported API (docs/ui-core-api.md section 10): ui = { toast, confirm, prompt, dialog, popover,
 *   sheet, menu, prefersSheet, trapFocus, focusables, onLongPress, announce, spinner,
 *   setDrawerOpen, isDrawerOpen, fatal, peoplePicker }; the same functions are also named exports.
 */

import { h, clear } from './dom.js';
import { icon } from './icons.js';
import { router } from './router.js';
import { store } from './store.js';
import { userAvatar } from './avatar.js';
import { fold, isTouchDevice, prefersReducedMotion } from './util.js';

const MAX_TOASTS = 4;
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"]), [contenteditable="true"]';

let uidCounter = 0;

/** @returns {string} a page-unique id fragment */
function uid() {
  uidCounter += 1;
  return `ui${uidCounter}`;
}

/**
 * The overlay container (created when the page did not ship one).
 * @returns {HTMLElement}
 */
function overlayRoot() {
  let el = document.getElementById('overlay-root');
  if (!el) {
    el = h('div.overlay-root', { id: 'overlay-root' });
    document.body.appendChild(el);
  }
  return el;
}

/* ------------------------------------------------------------------------------------------ */
/* Focus                                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * Visible, focusable descendants in DOM order.
 * @param {HTMLElement} container
 * @returns {HTMLElement[]}
 */
export function focusables(container) {
  return Array.from(container.querySelectorAll(FOCUSABLE)).filter((el) => {
    const e = /** @type {HTMLElement} */ (el);
    return !e.hidden && e.getAttribute('aria-hidden') !== 'true' && (e.offsetWidth > 0 || e.offsetHeight > 0 || e.getClientRects().length > 0);
  }).map((el) => /** @type {HTMLElement} */ (el));
}

/**
 * Keep keyboard focus inside `container`; focus moves in now and returns to the previously
 * focused element on release.
 * @param {HTMLElement} container
 * @param {{initialFocus?: string|HTMLElement|null, returnFocus?: boolean, opener?: HTMLElement|null}} [opts]
 *        opener: the element to refocus on release (default: the element focused now; callers that
 *        make the page inert first must capture it before)
 * @returns {() => void} release
 */
export function trapFocus(container, opts = {}) {
  const { initialFocus = null, returnFocus = true } = opts;
  const opener = opts.opener !== undefined ? opts.opener : /** @type {HTMLElement|null} */ (document.activeElement);
  const onKey = (e) => {
    if (e.key !== 'Tab') return;
    const f = focusables(container);
    if (f.length === 0) {
      e.preventDefault();
      container.focus();
      return;
    }
    const first = f[0];
    const last = f[f.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !container.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !container.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  };
  const onFocusIn = (e) => {
    if (container.isConnected && !container.contains(e.target)) {
      const f = focusables(container);
      (f[0] || container).focus();
    }
  };
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('focusin', onFocusIn, true);
  let target = null;
  if (typeof initialFocus === 'string') target = container.querySelector(initialFocus);
  else if (initialFocus) target = initialFocus;
  if (!target) target = container.querySelector('[data-autofocus]');
  if (!target) target = focusables(container)[0] || null;
  /** @type {HTMLElement} */ (target || container).focus({ preventScroll: true });
  let released = false;
  return () => {
    if (released) return;
    released = true;
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('focusin', onFocusIn, true);
    if (returnFocus && opener && opener.isConnected && typeof opener.focus === 'function') {
      try {
        opener.focus({ preventScroll: true });
      } catch (_) {
        /* ignore */
      }
    }
  };
}

let inertCount = 0;

/**
 * Mark the application inert while a modal is open.
 * @param {number} delta +1 / -1
 */
function adjustInert(delta) {
  inertCount = Math.max(0, inertCount + delta);
  for (const id of ['app', 'auth-root']) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (inertCount > 0) el.setAttribute('inert', '');
    else el.removeAttribute('inert');
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Toasts                                                                                     */
/* ------------------------------------------------------------------------------------------ */

/** @type {Array<{key: string|null, el: HTMLElement, dismiss: () => void, setText: (m: string) => void, restart: () => void}>} */
const toasts = [];

/**
 * @returns {HTMLElement}
 */
function toastRoot() {
  let el = document.getElementById('toast-root');
  if (!el) {
    el = h('div.toast-root', { id: 'toast-root', role: 'region', 'aria-live': 'polite', 'aria-label': 'Notifications' });
    document.body.appendChild(el);
  }
  return el;
}

/**
 * Show a transient message.
 * @param {string} message
 * @param {{type?: 'info'|'success'|'error', timeout?: number, key?: string, action?: {label: string, onClick: () => void}, onClick?: () => void}} [opts]
 * @returns {{dismiss: () => void, update: (message: string) => void}}
 */
export function toast(message, opts = {}) {
  const { type = 'info', key = null, action, onClick } = opts;
  const timeout = opts.timeout !== undefined ? opts.timeout : type === 'error' ? 6000 : 4000;
  if (key) {
    const existing = toasts.find((t) => t.key === key);
    if (existing) {
      existing.setText(message);
      existing.restart();
      return { dismiss: existing.dismiss, update: existing.setText };
    }
  }
  const textNode = onClick
    ? h('button.toast-text.toast-link', { type: 'button', onClick: () => { try { onClick(); } finally { dismiss(); } } }, message)
    : h('div.toast-text', message);
  const iconName = type === 'success' ? 'check-circle' : type === 'error' ? 'error' : 'info';
  const el = h(`div.toast.toast-${type}`, { role: type === 'error' ? 'alert' : 'status' },
    h('span.toast-icon', icon(iconName, { size: 20 })),
    textNode,
    action ? h('button.toast-action', { type: 'button', onClick: () => { try { action.onClick(); } finally { dismiss(); } } }, action.label) : null,
    h('button.toast-close', { type: 'button', 'aria-label': 'Dismiss', onClick: () => dismiss() }, icon('close', { size: 16 })));
  let timer = null;
  let done = false;
  const entry = {
    key,
    el,
    dismiss: () => dismiss(),
    setText: (m) => { textNode.textContent = m; },
    restart: () => arm(),
  };
  function arm() {
    if (timer) clearTimeout(timer);
    timer = null;
    if (timeout > 0) timer = setTimeout(dismiss, timeout);
  }
  function dismiss() {
    if (done) return;
    done = true;
    if (timer) clearTimeout(timer);
    const i = toasts.indexOf(/** @type {any} */ (entry));
    if (i >= 0) toasts.splice(i, 1);
    el.classList.add('leaving');
    setTimeout(() => el.remove(), prefersReducedMotion() ? 0 : 180);
  }
  el.addEventListener('mouseenter', () => { if (timer) clearTimeout(timer); timer = null; });
  el.addEventListener('mouseleave', () => { if (!done) { if (timer) clearTimeout(timer); timer = timeout > 0 ? setTimeout(dismiss, 1500) : null; } });
  toasts.push(/** @type {any} */ (entry));
  toastRoot().appendChild(el);
  while (toasts.length > MAX_TOASTS) toasts[0].dismiss();
  arm();
  return { dismiss, update: entry.setText };
}

/* ------------------------------------------------------------------------------------------ */
/* Dialogs                                                                                    */
/* ------------------------------------------------------------------------------------------ */

/**
 * Modal dialog.
 * @param {{title?: string, label?: string, content?: Node|string, actions?: Array<{label: string, id?: string, value?: any, primary?: boolean, danger?: boolean, disabled?: boolean, onClick?: (h: any) => any}>,
 *          size?: 'sm'|'md'|'lg'|'full', dismissable?: boolean, className?: string, initialFocus?: string|HTMLElement,
 *          role?: 'dialog'|'alertdialog', onClose?: (result: any) => void}} [opts]
 * @returns {{el: HTMLElement, panel: HTMLElement, body: HTMLElement, close: (result?: any) => void, closed: Promise<any>, setTitle: (t: string) => void, setBusy: (b: boolean) => void}}
 */
export function dialog(opts = {}) {
  const { title = '', label = '', content, actions = [], size = 'md', dismissable = true, className = '', initialFocus, role = 'dialog', onClose } = opts;
  const opener = /** @type {HTMLElement|null} */ (document.activeElement);
  const titleId = uid();
  const titleEl = h('h2.dialog-title', { id: titleId }, title);
  const body = h('div.dialog-body', typeof content === 'string' ? h('p.dialog-message', content) : content || null);
  const initiallyDisabled = actions.map((a) => Boolean(a.disabled));
  const buttons = actions.map((a) => h(`button.btn.${a.danger ? 'btn-danger' : a.primary ? 'btn-primary' : 'btn-secondary'}`, {
    type: 'button',
    disabled: Boolean(a.disabled),
    dataset: { action: a.id || '' },
    onClick: () => runAction(a),
  }, a.label));
  const header = title || dismissable
    ? h('div.dialog-header', title ? titleEl : h('span.grow'),
      dismissable ? h('button.btn-icon.dialog-close', { type: 'button', 'aria-label': 'Close', onClick: () => close(undefined) }, icon('close')) : null)
    : null;
  const panel = h(`div.dialog.dialog-${size}${className ? '.' + className.split(/\s+/).join('.') : ''}`, {
    role,
    'aria-modal': 'true',
    'aria-labelledby': title ? titleId : null,
    'aria-label': title ? null : label || null,
    tabIndex: -1,
  }, header, body, buttons.length ? h('div.dialog-actions', buttons) : null);
  let downOnBackdrop = false;
  const backdrop = h('div.dialog-backdrop', {
    onPointerdown: (e) => { downOnBackdrop = e.target === backdrop; },
    onClick: (e) => { if (dismissable && downOnBackdrop && e.target === backdrop) close(undefined); downOnBackdrop = false; },
  }, panel);

  let closed = false;
  /** @type {(v: any) => void} */
  let resolveClosed = () => {};
  const closedPromise = new Promise((resolve) => { resolveClosed = resolve; });
  /** @type {null|{release: () => void}} */
  let layer = null;
  let releaseTrap = () => {};

  function finish(result) {
    if (closed) return;
    closed = true;
    backdrop.remove();
    adjustInert(-1);
    releaseTrap();
    try {
      if (onClose) onClose(result);
    } finally {
      resolveClosed(result);
    }
  }

  function close(result) {
    if (closed) return;
    if (layer) layer.release();
    finish(result);
  }

  async function runAction(a) {
    if (a.onClick) {
      let keep;
      try {
        keep = await a.onClick(handle);
      } catch (err) {
        console.error('[ui] dialog action threw', err);
        return;
      }
      if (keep === false) return;
    }
    close(a.value !== undefined ? a.value : a.label);
  }

  const handle = {
    el: backdrop,
    panel,
    body,
    close,
    closed: closedPromise,
    setTitle: (t) => { titleEl.textContent = t; },
    setBusy: (b) => {
      panel.setAttribute('aria-busy', b ? 'true' : 'false');
      buttons.forEach((btn, i) => { /** @type {HTMLButtonElement} */ (btn).disabled = b || initiallyDisabled[i]; });
    },
  };
  overlayRoot().appendChild(backdrop);
  adjustInert(1);
  if (dismissable) layer = router.pushLayer({ id: 'dialog', priority: 60, close: () => finish(undefined) });
  releaseTrap = trapFocus(panel, { initialFocus: initialFocus || undefined, opener });
  return handle;
}

/**
 * Yes/no confirmation.
 * @param {{title?: string, message?: string|Node, confirmLabel?: string, cancelLabel?: string, danger?: boolean}} opts
 * @returns {Promise<boolean>}
 */
export function confirm(opts) {
  const { title = '', message = '', confirmLabel = 'OK', cancelLabel = 'Cancel', danger = false } = opts || {};
  return new Promise((resolve) => {
    dialog({
      title,
      size: 'sm',
      role: 'alertdialog',
      content: typeof message === 'string' ? h('p.dialog-message', message) : message,
      actions: [
        { label: cancelLabel, value: false, id: 'cancel' },
        { label: confirmLabel, value: true, primary: !danger, danger, id: 'confirm' },
      ],
      initialFocus: danger ? '[data-action="cancel"]' : '[data-action="confirm"]',
      onClose: (r) => resolve(r === true),
    });
  });
}

/**
 * Ask for a line of text.
 * @param {{title?: string, label?: string, value?: string, placeholder?: string, confirmLabel?: string, maxLength?: number, validate?: (v: string) => string|null, multiline?: boolean}} opts
 * @returns {Promise<string|null>} the entered text, or null when cancelled
 */
export function prompt(opts) {
  const { title = '', label = '', value = '', placeholder = '', confirmLabel = 'OK', maxLength, validate, multiline = false } = opts || {};
  return new Promise((resolve) => {
    const inputId = uid();
    const input = multiline
      ? h('textarea.input', { id: inputId, rows: 4, maxlength: maxLength || null, placeholder, dir: 'auto', 'data-autofocus': '' })
      : h('input.input', { id: inputId, type: 'text', maxlength: maxLength || null, placeholder, dir: 'auto', 'data-autofocus': '', autocomplete: 'off' });
    /** @type {HTMLInputElement} */ (input).value = value;
    const error = h('p.error-text', { role: 'alert', hidden: true });
    const content = h('div.field', label ? h('label.label', { for: inputId }, label) : null, input, error);
    const submit = () => {
      const v = /** @type {HTMLInputElement} */ (input).value;
      const msg = validate ? validate(v) : null;
      if (msg) {
        error.textContent = msg;
        error.hidden = false;
        return false;
      }
      return true;
    };
    const d = dialog({
      title,
      size: 'sm',
      content,
      actions: [
        { label: 'Cancel', value: null, id: 'cancel' },
        { label: confirmLabel, primary: true, id: 'confirm', onClick: () => submit(), value: '__ok__' },
      ],
      onClose: (r) => resolve(r === '__ok__' ? /** @type {HTMLInputElement} */ (input).value : null),
    });
    if (!multiline) {
      input.addEventListener('keydown', (e) => {
        const ev = /** @type {KeyboardEvent} */ (e);
        if (ev.key === 'Enter' && !ev.isComposing) {
          ev.preventDefault();
          if (submit()) d.close('__ok__');
        }
      });
    }
  });
}

/* ------------------------------------------------------------------------------------------ */
/* Popovers, sheets, menus                                                                    */
/* ------------------------------------------------------------------------------------------ */

/**
 * Do menus render as bottom sheets here? (coarse pointer or viewport < 600 px)
 * @returns {boolean}
 */
export function prefersSheet() {
  try {
    return isTouchDevice() || window.innerWidth < 600;
  } catch (_) {
    return false;
  }
}

/**
 * Place a fixed-position element near an anchor rectangle or point, inside the viewport.
 * @param {HTMLElement} el
 * @param {{anchor?: Element|null, x?: number, y?: number, placement?: string}} o
 */
function positionNear(el, o) {
  const margin = 8;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = el.offsetWidth;
  const hgt = el.offsetHeight;
  let left;
  let top;
  const place = o.placement || 'bottom-start';
  if (o.anchor) {
    const r = o.anchor.getBoundingClientRect();
    const below = !place.startsWith('top');
    const alignEnd = place.endsWith('end');
    left = alignEnd ? r.right - w : r.left;
    top = below ? r.bottom + 4 : r.top - hgt - 4;
    if (below && top + hgt > vh - margin && r.top - hgt - 4 >= margin) top = r.top - hgt - 4;
    if (!below && top < margin && r.bottom + 4 + hgt <= vh - margin) top = r.bottom + 4;
  } else {
    left = o.x === undefined ? (vw - w) / 2 : o.x;
    top = o.y === undefined ? (vh - hgt) / 2 : o.y;
    if (top + hgt > vh - margin) top = (o.y === undefined ? vh : o.y) - hgt;
  }
  left = Math.max(margin, Math.min(left, vw - w - margin));
  top = Math.max(margin, Math.min(top, vh - hgt - margin));
  el.style.left = `${Math.round(left)}px`;
  el.style.top = `${Math.round(top)}px`;
}

/**
 * Non-modal floating panel next to an anchor (or at x/y). Closes on outside pointerdown, Escape,
 * resize and route changes.
 * @param {Node} content
 * @param {{anchor?: Element|null, x?: number, y?: number, placement?: string, label?: string, role?: string, onClose?: () => void, dismissOnBlur?: boolean, className?: string}} [opts]
 * @returns {{el: HTMLElement, close: () => void, reposition: () => void}}
 */
export function popover(content, opts = {}) {
  const { label = '', role = 'dialog', onClose, dismissOnBlur = true, className = '' } = opts;
  const opener = /** @type {HTMLElement|null} */ (document.activeElement);
  const el = h(`div.popover${className ? '.' + className.split(/\s+/).join('.') : ''}`, { role, 'aria-label': label || null }, content);
  let closed = false;
  /** @type {null|{release: () => void}} */
  let layer = null;
  const onOutside = (e) => { if (dismissOnBlur && !el.contains(e.target) && !(opts.anchor && opts.anchor.contains(e.target))) close(); };
  const onResize = () => close();
  function finish() {
    if (closed) return;
    closed = true;
    document.removeEventListener('pointerdown', onOutside, true);
    window.removeEventListener('resize', onResize);
    el.remove();
    if (opener && opener.isConnected && (document.activeElement === document.body || !document.activeElement)) {
      try {
        opener.focus({ preventScroll: true });
      } catch (_) {
        /* ignore */
      }
    }
    if (onClose) onClose();
  }
  function close() {
    if (closed) return;
    if (layer) layer.release();
    finish();
  }
  overlayRoot().appendChild(el);
  positionNear(el, opts);
  document.addEventListener('pointerdown', onOutside, true);
  window.addEventListener('resize', onResize);
  layer = router.pushLayer({ id: 'popover', priority: 40, close: finish });
  return { el, close, reposition: () => positionNear(el, opts) };
}

/**
 * Bottom sheet (touch / narrow screens): modal, focus-trapped, closes on backdrop tap.
 * @param {Node} content
 * @param {{title?: string, label?: string, onClose?: () => void}} [opts]
 * @returns {{el: HTMLElement, body: HTMLElement, close: () => void}}
 */
export function sheet(content, opts = {}) {
  const { title = '', label = '', onClose } = opts;
  const opener = /** @type {HTMLElement|null} */ (document.activeElement);
  const titleId = uid();
  const body = h('div.sheet-body', content);
  const panel = h('div.sheet', {
    role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': title ? titleId : null, 'aria-label': title ? null : label || null, tabIndex: -1,
  }, h('div.sheet-handle', { 'aria-hidden': 'true' }), title ? h('h2.sheet-title', { id: titleId }, title) : null, body);
  let down = false;
  const backdrop = h('div.sheet-backdrop', {
    onPointerdown: (e) => { down = e.target === backdrop; },
    onClick: (e) => { if (down && e.target === backdrop) close(); down = false; },
  }, panel);
  let closed = false;
  /** @type {null|{release: () => void}} */
  let layer = null;
  let releaseTrap = () => {};
  function finish() {
    if (closed) return;
    closed = true;
    backdrop.remove();
    adjustInert(-1);
    releaseTrap();
    if (onClose) onClose();
  }
  function close() {
    if (closed) return;
    if (layer) layer.release();
    finish();
  }
  overlayRoot().appendChild(backdrop);
  adjustInert(1);
  layer = router.pushLayer({ id: 'sheet', priority: 40, close: finish });
  releaseTrap = trapFocus(panel, { opener });
  return { el: backdrop, body, close };
}

/**
 * Action menu: a popover next to the anchor/point on pointer devices, a bottom sheet on touch.
 * @param {Array<{label?: string, icon?: string, onSelect?: () => void, danger?: boolean, disabled?: boolean, checked?: boolean, hint?: string, separator?: boolean}>} items
 * @param {{anchor?: Element|null, x?: number, y?: number, label?: string, title?: string, sheet?: 'auto'|'always'|'never', onClose?: () => void}} [opts]
 * @returns {{close: () => void}}
 */
export function menu(items, opts = {}) {
  const { label = 'Menu', title = '', onClose } = opts;
  const asSheet = opts.sheet === 'always' || (opts.sheet !== 'never' && prefersSheet());
  /** @type {null|{close: () => void}} */
  let host = null;
  const buttons = [];
  const list = h('ul.menu', { role: 'menu', 'aria-label': label });
  for (const it of items) {
    if (it.separator) {
      list.appendChild(h('li', { role: 'separator', class: 'menu-separator' }));
      continue;
    }
    const checkable = typeof it.checked === 'boolean';
    const btn = h(`button.menu-item${it.danger ? '.danger' : ''}`, {
      type: 'button',
      role: checkable ? 'menuitemcheckbox' : 'menuitem',
      'aria-checked': checkable ? String(it.checked) : null,
      disabled: Boolean(it.disabled),
      tabIndex: -1,
      onClick: () => {
        if (host) host.close();
        if (it.onSelect) it.onSelect();
      },
    }, it.icon ? h('span.menu-icon', icon(it.icon, { size: 20 })) : null, h('span.menu-label', it.label || ''),
    it.hint ? h('span.menu-hint', it.hint) : null,
    checkable && it.checked ? h('span.menu-check', icon('check', { size: 18 })) : null);
    buttons.push(btn);
    list.appendChild(h('li', { role: 'none' }, btn));
  }
  list.addEventListener('keydown', (e) => {
    const enabled = buttons.filter((b) => !b.disabled);
    if (!enabled.length) return;
    const i = enabled.indexOf(/** @type {any} */ (document.activeElement));
    let next = null;
    if (e.key === 'ArrowDown') next = enabled[(i + 1) % enabled.length];
    else if (e.key === 'ArrowUp') next = enabled[(i - 1 + enabled.length) % enabled.length];
    else if (e.key === 'Home') next = enabled[0];
    else if (e.key === 'End') next = enabled[enabled.length - 1];
    else if (e.key === 'Tab') {
      e.preventDefault();
      if (host) host.close();
      return;
    }
    if (next) {
      e.preventDefault();
      next.focus();
    }
  });
  if (asSheet) {
    host = sheet(list, { title, label, onClose });
  } else {
    host = popover(list, { anchor: opts.anchor, x: opts.x, y: opts.y, label, role: 'presentation', onClose, className: 'popover-menu' });
  }
  const first = buttons.find((b) => !b.disabled);
  if (first) first.focus({ preventScroll: true });
  return { close: () => host && host.close() };
}

/* ------------------------------------------------------------------------------------------ */
/* Long press                                                                                 */
/* ------------------------------------------------------------------------------------------ */

/**
 * Touch long-press (SPEC 9.8): a pointer-event timer cancelled by movement beyond `tolerance`.
 * Suppresses the click that follows the press and the native context menu.
 * @param {HTMLElement} el
 * @param {(info: {x: number, y: number, target: EventTarget|null, event: PointerEvent}) => void} handler
 * @param {{ms?: number, tolerance?: number, mouse?: boolean}} [opts]
 * @returns {() => void} remover
 */
export function onLongPress(el, handler, opts = {}) {
  const { ms = 450, tolerance = 8, mouse = false } = opts;
  let timer = null;
  let sx = 0;
  let sy = 0;
  let fired = false;
  const cancel = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  const down = (e) => {
    if (e.pointerType === 'mouse' && (!mouse || e.button !== 0)) return;
    fired = false;
    sx = e.clientX;
    sy = e.clientY;
    cancel();
    timer = setTimeout(() => {
      timer = null;
      fired = true;
      handler({ x: sx, y: sy, target: e.target, event: e });
    }, ms);
  };
  const move = (e) => {
    if (timer && Math.hypot(e.clientX - sx, e.clientY - sy) > tolerance) cancel();
  };
  const click = (e) => {
    if (fired) {
      e.preventDefault();
      e.stopPropagation();
      fired = false;
    }
  };
  const ctx = (e) => {
    if (fired || timer) e.preventDefault();
  };
  el.addEventListener('pointerdown', down);
  el.addEventListener('pointermove', move);
  el.addEventListener('pointerup', cancel);
  el.addEventListener('pointercancel', cancel);
  el.addEventListener('pointerleave', cancel);
  el.addEventListener('click', click, true);
  el.addEventListener('contextmenu', ctx);
  return () => {
    cancel();
    el.removeEventListener('pointerdown', down);
    el.removeEventListener('pointermove', move);
    el.removeEventListener('pointerup', cancel);
    el.removeEventListener('pointercancel', cancel);
    el.removeEventListener('pointerleave', cancel);
    el.removeEventListener('click', click, true);
    el.removeEventListener('contextmenu', ctx);
  };
}

/* ------------------------------------------------------------------------------------------ */
/* Misc                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/**
 * Announce text to screen readers through a polite live region.
 * @param {string} text
 */
export function announce(text) {
  let el = document.getElementById('sr-live');
  if (!el) {
    el = h('div.sr-only', { id: 'sr-live', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
    document.body.appendChild(el);
  }
  const node = el;
  node.textContent = '';
  setTimeout(() => { node.textContent = text; }, 50);
}

/**
 * @param {number} [size=20] px
 * @returns {HTMLElement}
 */
export function spinner(size = 20) {
  return h('span.spinner', { role: 'status', 'aria-label': 'Loading', style: { width: `${size}px`, height: `${size}px` } });
}

/** @param {boolean} open show or hide the info drawer pane */
export function setDrawerOpen(open) {
  const app = document.getElementById('app');
  if (app) app.dataset.drawer = open ? 'open' : 'closed';
  const pane = document.getElementById('pane-drawer');
  if (pane) pane.hidden = !open;
}

/** @returns {boolean} whether the info drawer is open */
export function isDrawerOpen() {
  const app = document.getElementById('app');
  return Boolean(app && app.dataset.drawer === 'open');
}

/**
 * Full-screen blocking notice (4003 "too many windows", outdated protocol).
 * @param {string} title
 * @param {string} message
 * @param {{actionLabel?: string, onAction?: () => void}} [opts]
 * @returns {{close: () => void}}
 */
export function fatal(title, message, opts = {}) {
  const { actionLabel, onAction } = opts;
  const opener = /** @type {HTMLElement|null} */ (document.activeElement);
  const el = h('div.fatal', { role: 'alertdialog', 'aria-modal': 'true', 'aria-labelledby': 'fatal-title' },
    h('div.fatal-card',
      h('h1', { id: 'fatal-title' }, title),
      h('p', message),
      actionLabel ? h('button.btn.btn-primary', { type: 'button', 'data-autofocus': '', onClick: () => { close(); if (onAction) onAction(); } }, actionLabel) : null));
  let release = () => {};
  function close() {
    el.remove();
    adjustInert(-1);
    release();
  }
  overlayRoot().appendChild(el);
  adjustInert(1);
  release = trapFocus(el, { opener });
  return { close };
}

/* ------------------------------------------------------------------------------------------ */
/* People picker (SPEC 1, 4.3)                                                                */
/* ------------------------------------------------------------------------------------------ */

const PICKER_MAX_ROWS = 300;

/**
 * Searchable list of the people in the directory, showing the display name and `@username`
 * (display names are unique but usernames disambiguate, SPEC 4.3). Used by "New chat", "New group",
 * the forward picker and the info drawer's "Add members".
 * Single mode: clicking a row (or Enter in the search box with a query) calls `onPick(user)`.
 * Multi mode: rows toggle and `onPick(user, selected, allSelected)` fires on every toggle;
 * `getSelected()` returns the chosen users. Disabled users are hidden unless `includeDisabled`.
 * Call `destroy()` when the owner closes (it also detaches itself once the element left the page).
 * @param {{multi?: boolean, exclude?: number[], onPick?: (user: any, selected?: boolean, all?: any[]) => void,
 *          placeholder?: string, includeDisabled?: boolean}} [opts]
 * @returns {{el: HTMLElement, input: HTMLInputElement, focus: () => void, getSelected: () => any[],
 *            clear: () => void, setExclude: (ids: number[]) => void, destroy: () => void}}
 */
export function peoplePicker(opts = {}) {
  const { multi = false, onPick, placeholder = 'Search people', includeDisabled = false } = opts;
  let excluded = new Set(opts.exclude || []);
  /** @type {Map<number, any>} */
  const selected = new Map();
  /** @type {Map<number, HTMLElement>} */
  const buttons = new Map();
  const input = /** @type {HTMLInputElement} */ (h('input.input.people-search', {
    type: 'search', placeholder, 'aria-label': placeholder, autocomplete: 'off', dir: 'auto', 'data-autofocus': '',
  }));
  const list = h('ul.people-list', { role: 'listbox', 'aria-label': 'People', 'aria-multiselectable': multi ? 'true' : null });
  const note = h('p.hint.people-note', { hidden: true });
  const el = h('div.people-picker', input, list, note);
  let destroyed = false;
  let wasConnected = false;

  /** @returns {any[]} the users that match the query */
  function matches() {
    const q = fold(input.value).trim();
    return store.users()
      .filter((u) => !excluded.has(u.id) && (includeDisabled || !u.disabled))
      .filter((u) => !q || fold(u.display_name).includes(q) || fold(u.username).includes(q))
      .sort((a, b) => fold(a.display_name).localeCompare(fold(b.display_name)));
  }

  /** @param {any} u */
  function paintRow(u) {
    const btn = buttons.get(u.id);
    if (!btn) return;
    const on = selected.has(u.id);
    btn.setAttribute('aria-selected', String(on));
    const check = btn.querySelector('.people-check');
    if (check) {
      clear(check);
      check.classList.toggle('on', on);
      if (on) check.appendChild(icon('check-circle', { size: 22 }));
    }
  }

  /** @param {any} u */
  function pick(u) {
    if (!multi) {
      if (onPick) onPick(u);
      return;
    }
    const on = !selected.has(u.id);
    if (on) selected.set(u.id, u);
    else selected.delete(u.id);
    paintRow(u);
    if (onPick) onPick(u, on, Array.from(selected.values()));
  }

  function render() {
    const all = matches();
    const shown = all.slice(0, PICKER_MAX_ROWS);
    clear(list);
    buttons.clear();
    for (const u of shown) {
      const mine = Boolean(store.me && u.id === store.me.id);
      const btn = h('button.list-row.people-row', {
        type: 'button', role: 'option', 'aria-selected': String(selected.has(u.id)), dataset: { userId: u.id },
        'aria-label': `${mine ? `${u.display_name} (You)` : u.display_name}, @${u.username}`,
        onClick: () => pick(u),
      }, userAvatar(u, { size: 'md' }),
      h('div.grow', h('div.truncate.people-name', { dir: 'auto' }, mine ? `${u.display_name} (You)` : u.display_name), h('div.truncate.muted.people-username', `@${u.username}`)),
      multi ? h('span.people-check', { 'aria-hidden': 'true' }) : null);
      buttons.set(u.id, btn);
      paintRow(u);
      list.appendChild(h('li', { role: 'presentation' }, btn));
    }
    const q = input.value.trim();
    note.hidden = all.length > 0 && all.length <= shown.length;
    if (all.length === 0) note.textContent = q ? `No chats or people match "${q}"` : 'No people to show';
    else if (all.length > shown.length) note.textContent = `Showing the first ${PICKER_MAX_ROWS} people - type to narrow the list`;
  }

  /** @returns {HTMLElement[]} the row buttons in order */
  const rows = () => Array.from(buttons.values());

  input.addEventListener('input', render);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      const first = rows()[0];
      if (first) {
        e.preventDefault();
        first.focus();
      }
    } else if (e.key === 'Enter' && !multi && input.value.trim() && !e.isComposing) {
      const first = matches()[0];
      if (first) {
        e.preventDefault();
        pick(first);
      }
    }
  });
  list.addEventListener('keydown', (e) => {
    const all = rows();
    const i = all.indexOf(/** @type {any} */ (document.activeElement));
    if (i < 0) return;
    if (e.key === 'ArrowDown' && i < all.length - 1) {
      e.preventDefault();
      all[i + 1].focus();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (i > 0) all[i - 1].focus();
      else input.focus();
    }
  });

  const off = store.on('users', () => {
    if (destroyed) return;
    if (el.isConnected) wasConnected = true;
    else if (wasConnected) {
      destroy();
      return;
    }
    render();
  });

  function destroy() {
    destroyed = true;
    off();
  }

  render();
  return {
    el,
    input,
    focus: () => input.focus(),
    getSelected: () => Array.from(selected.values()),
    clear: () => {
      for (const u of Array.from(selected.values())) {
        selected.delete(u.id);
        paintRow(u);
      }
      input.value = '';
      render();
    },
    setExclude: (ids) => {
      excluded = new Set(ids || []);
      render();
    },
    destroy,
  };
}

export const ui = {
  toast, confirm, prompt, dialog, popover, sheet, menu, prefersSheet, trapFocus, focusables,
  onLongPress, announce, spinner, setDrawerOpen, isDrawerOpen, fatal, peoplePicker,
};
