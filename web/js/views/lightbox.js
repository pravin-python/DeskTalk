/**
 * views/lightbox.js - full-screen image / video viewer with previous / next navigation (SPEC 9.2, 9.8).
 *
 * `role="dialog" aria-modal="true"`, focus is trapped and returns to the opener, Escape and the hardware
 * Back button close it through a router layer (priority 50). Arrow keys and horizontal swipes navigate;
 * a click on an image toggles between "fit" and "actual size".
 *
 * Exported API (docs/ui-conv-api.md section 4): openLightbox, lightboxItemFromMessage.
 */

import { h } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { ui } from '../core/ui.js';
import { router } from '../core/router.js';
import { store } from '../core/store.js';
import { formatDateTime, formatBytes } from '../core/util.js';

const SWIPE_PX = 60;

/**
 * @typedef {object} LightboxItem
 * @property {string} url
 * @property {string} name
 * @property {'image'|'video'} kind
 * @property {number|null} [width]
 * @property {number|null} [height]
 * @property {number} [size]
 * @property {number} [created_at]
 * @property {string} [sender]
 */

/**
 * The viewer item of a message, or null when it has no viewable image / video.
 * @param {any} message
 * @returns {LightboxItem|null}
 */
export function lightboxItemFromMessage(message) {
  const att = message && !message.deleted ? message.attachment : null;
  if (!att || !att.url || (att.kind !== 'image' && att.kind !== 'video')) return null;
  return {
    url: att.url,
    name: att.name || (att.kind === 'image' ? 'Image' : 'Video'),
    kind: att.kind,
    width: att.width,
    height: att.height,
    size: att.size,
    created_at: message.created_at,
    sender: message.sender_id === null || message.sender_id === undefined ? '' : store.me && message.sender_id === store.me.id ? 'You' : store.userName(message.sender_id),
  };
}

/**
 * Open the viewer.
 * @param {{items: LightboxItem[], index?: number, opener?: Element|null}} opts
 * @returns {{close: () => void}}
 */
export function openLightbox(opts) {
  const items = (opts.items || []).filter((i) => i && i.url);
  if (items.length === 0) return { close: () => {} };
  let index = Math.max(0, Math.min(items.length - 1, opts.index || 0));
  const opener = /** @type {HTMLElement|null} */ (opts.opener || document.activeElement);

  const title = h('span.lightbox-title.truncate', { dir: 'auto' });
  const sub = h('span.lightbox-sub.truncate');
  const counter = h('span.lightbox-counter', { 'aria-live': 'polite' });
  const download = h('a.btn-icon.lightbox-btn', { 'aria-label': 'Download', title: 'Download', download: '' }, icon('download', { size: 24 }));
  const stage = h('div.lightbox-stage');
  const prev = h('button.btn-icon.lightbox-btn.lightbox-nav.prev', { type: 'button', 'aria-label': 'Previous', onClick: () => go(-1) }, icon('chevron-left', { size: 32 }));
  const next = h('button.btn-icon.lightbox-btn.lightbox-nav.next', { type: 'button', 'aria-label': 'Next', onClick: () => go(1) }, icon('chevron-right', { size: 32 }));
  const closeBtn = h('button.btn-icon.lightbox-btn', { type: 'button', 'aria-label': 'Close', onClick: () => close() }, icon('close', { size: 24 }));
  const root = h('div.lightbox', { role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Media viewer', tabIndex: -1 },
    h('div.lightbox-bar', h('span.lightbox-meta', title, sub), counter, download, closeBtn),
    h('div.lightbox-body', prev, stage, next));

  let closed = false;
  /** @type {HTMLElement|null} */
  let media = null;

  /** Stop and drop the current media element (a playing video must not outlive its slide). */
  function clearMedia() {
    if (media && media.tagName === 'VIDEO') {
      const v = /** @type {HTMLVideoElement} */ (media);
      v.pause();
      v.removeAttribute('src');
      v.load();
    }
    media = null;
    stage.replaceChildren();
  }

  function show() {
    const it = items[index];
    clearMedia();
    title.textContent = it.name;
    sub.textContent = [it.sender, it.created_at ? formatDateTime(it.created_at) : '', it.size ? formatBytes(it.size) : ''].filter(Boolean).join(' · ');
    counter.textContent = items.length > 1 ? `${index + 1} / ${items.length}` : '';
    prev.hidden = next.hidden = items.length < 2;
    prev.disabled = index === 0;
    next.disabled = index === items.length - 1;
    const isFile = it.url.startsWith('/files/');
    download.hidden = !isFile;
    if (isFile) {
      download.setAttribute('href', `${it.url}?dl=1`);
      download.setAttribute('download', it.name);
    }
    if (it.kind === 'video') {
      media = h('video.lightbox-media', { controls: true, autoplay: true, playsinline: true, src: it.url });
      media.addEventListener('error', () => stage.replaceChildren(h('p.lightbox-error', 'This video cannot be played here. Use Download.')));
    } else {
      const img = h('img.lightbox-media', { src: it.url, alt: it.name, draggable: false });
      img.addEventListener('click', () => stage.classList.toggle('zoomed'));
      img.addEventListener('error', () => stage.replaceChildren(h('p.lightbox-error', 'This image cannot be shown.')));
      media = img;
      const neighbour = items[index + 1];
      if (neighbour && neighbour.kind === 'image') new Image().src = neighbour.url;
    }
    stage.classList.remove('zoomed');
    stage.appendChild(media);
  }

  /** @param {number} delta */
  function go(delta) {
    const n = index + delta;
    if (n < 0 || n >= items.length) return;
    index = n;
    show();
  }

  const onKey = (e) => {
    if (e.key === 'ArrowLeft') go(-1);
    else if (e.key === 'ArrowRight') go(1);
  };
  let startX = 0;
  let startY = 0;
  let tracking = false;
  stage.addEventListener('pointerdown', (e) => {
    tracking = e.pointerType !== 'mouse' && !stage.classList.contains('zoomed');
    startX = e.clientX;
    startY = e.clientY;
  });
  stage.addEventListener('pointerup', (e) => {
    if (!tracking) return;
    tracking = false;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) > SWIPE_PX && Math.abs(dx) > Math.abs(dy) * 1.5) go(dx < 0 ? 1 : -1);
  });
  stage.addEventListener('pointercancel', () => {
    tracking = false;
  });

  /** @type {() => void} */
  let releaseTrap = () => {};
  function finish() {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    clearMedia();
    root.remove();
    releaseTrap();
  }
  const layer = router.pushLayer({ id: 'lightbox', priority: 50, close: finish });
  function close() {
    if (closed) return;
    layer.release();
    finish();
  }

  (document.getElementById('overlay-root') || document.body).appendChild(root);
  show();
  document.addEventListener('keydown', onKey);
  releaseTrap = ui.trapFocus(root, { initialFocus: closeBtn, opener });
  return { close };
}
