/**
 * core/upload.js - the attachment pipeline (SPEC 9.9): validation, client-side image downscale,
 * an XHR queue (<= 2 parallel) with progress and cancel, and the glue that creates one outbox item
 * per file. `msg.send` is issued by the outbox only after the upload resolved.
 *
 * Exported API (docs/ui-core-api.md section 8): precheck, enqueueFiles, describeError,
 * UploadError, UploadJob (internal use by tests).
 */

import { outbox as defaultOutbox } from './outbox.js';
import { store as defaultStore } from './store.js';
import { api } from './api.js';
import { formatBytes } from './util.js';

const MAX_PARALLEL = 2;
const MAX_FILES_PER_SEND = 10;
const DOWNSCALE_MIN_BYTES = 1024 * 1024;
const MAX_IMAGE_SIDE = 1600;
const JPEG_QUALITY = 0.85;
const MAX_429_RETRIES = 5;
const META_TIMEOUT_MS = 4000;

/**
 * Error of an upload. `message` is already the end-user text of SPEC 9.9(4).
 */
export class UploadError extends Error {
  /**
   * @param {string} code 'cancelled' | 'network' | 'too_large' | 'quota_exceeded' | 'insufficient_storage' | 'blocked_type' | 'unauthorized' | 'rate_limited' | ...
   * @param {string} message
   * @param {number} [status=0] HTTP status (0 = none)
   */
  constructor(code, message, status = 0) {
    super(message);
    this.name = 'UploadError';
    this.code = code;
    this.status = status;
  }
}

/**
 * End-user text for an upload failure (SPEC 9.9(4)).
 * @param {{code?: string, status?: number, message?: string}} err
 * @param {number} [limitBytes] the server's max upload size, for the `too_large` text
 * @returns {string}
 */
export function describeError(err, limitBytes) {
  const code = err && err.code;
  switch (code) {
    case 'cancelled': return 'Upload cancelled';
    case 'too_large': return `File is larger than the limit (${limitBytes ? formatBytes(limitBytes) : 'server limit'})`;
    case 'quota_exceeded': return 'Upload quota reached - try again later';
    case 'insufficient_storage': return 'Server storage is full - contact your admin';
    case 'blocked_type': return 'This file type is not allowed';
    case 'unauthorized': return 'Your session has expired. Please sign in again.';
    case 'rate_limited': return 'Too many uploads - please wait a moment';
    case 'password_change_required': return 'Please change your password first';
    case 'network':
    case 'timeout':
    case 'request_timeout': return 'Upload failed - tap to retry';
    default: return err && err.status === 0 ? 'Upload failed - tap to retry' : (err && err.message) || 'Upload failed';
  }
}

/**
 * @param {any} file
 * @returns {string} display name
 */
function nameOf(file) {
  return (file && typeof file.name === 'string' && file.name) || 'file';
}

/**
 * Guess the attachment kind from the MIME type (display only; the server sniffs the real type).
 * @param {{type?: string}} file
 * @returns {'image'|'video'|'audio'|'file'}
 */
function kindOf(file) {
  const t = (file && file.type) || '';
  if (t.startsWith('image/')) return 'image';
  if (t.startsWith('video/')) return 'video';
  if (t.startsWith('audio/')) return 'audio';
  return 'file';
}

/**
 * Cheap client-side validation (SPEC 9.9(2)).
 * @param {File|Blob} file
 * @param {{max_upload_bytes?: number}} [limits]
 * @returns {{ok: true}|{ok: false, reason: string}}
 */
export function precheck(file, limits) {
  const lim = limits || defaultStore.limits;
  const name = nameOf(file);
  const size = file && typeof file.size === 'number' ? file.size : 0;
  if (!file || size === 0) return { ok: false, reason: `"${name}" is empty or a folder - folders cannot be sent` };
  if (size === 4096 && !file.type && !/\.[^./\\]+$/.test(name)) return { ok: false, reason: `"${name}" looks like a folder - folders cannot be sent` };
  if (lim && lim.max_upload_bytes && size > lim.max_upload_bytes) {
    return { ok: false, reason: `"${name}" is larger than the limit (${formatBytes(lim.max_upload_bytes)})` };
  }
  return { ok: true };
}

/* ------------------------------------------------------------------------------------------ */
/* Image / media helpers                                                                      */
/* ------------------------------------------------------------------------------------------ */

/**
 * Decode an image for measuring and drawing. Resolves null when the browser cannot decode it.
 * @param {Blob} blob
 * @returns {Promise<{width: number, height: number, draw: (ctx: CanvasRenderingContext2D, w: number, h: number) => void, close: () => void}|null>}
 */
async function decodeImage(blob) {
  if (typeof createImageBitmap === 'function') {
    try {
      const bmp = await createImageBitmap(blob);
      return {
        width: bmp.width,
        height: bmp.height,
        draw: (ctx, w, h) => ctx.drawImage(bmp, 0, 0, w, h),
        close: () => {
          if (typeof bmp.close === 'function') bmp.close();
        },
      };
    } catch (_) {
      /* try the <img> route */
    }
  }
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    await new Promise((resolve, reject) => {
      img.onload = () => resolve(undefined);
      img.onerror = () => reject(new Error('decode failed'));
      img.src = url;
    });
    return {
      width: img.naturalWidth,
      height: img.naturalHeight,
      draw: (ctx, w, h) => ctx.drawImage(img, 0, 0, w, h),
      close: () => URL.revokeObjectURL(url),
    };
  } catch (_) {
    URL.revokeObjectURL(url);
    return null;
  }
}

/**
 * Re-encode an image as JPEG with the longest side <= 1600 px (SPEC 9.9(5)).
 * @param {Blob} blob
 * @param {{width: number, height: number, draw: Function, close: Function}} img
 * @returns {Promise<Blob|null>}
 */
async function encodeJpeg(blob, img) {
  const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');
  if (!ctx) return null;
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, w, h);
  img.draw(ctx, w, h);
  return new Promise((resolve) => {
    try {
      canvas.toBlob((b) => resolve(b), 'image/jpeg', JPEG_QUALITY);
    } catch (_) {
      resolve(null);
    }
  });
}

/**
 * Read duration (and video size) through a detached media element.
 * @param {Blob} blob
 * @param {'video'|'audio'} tag
 * @returns {Promise<{duration: number|null, width: number|null, height: number|null}>}
 */
function readMediaMeta(blob, tag) {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const el = document.createElement(tag);
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      el.removeAttribute('src');
      el.load();
      URL.revokeObjectURL(url);
      resolve(v);
    };
    const timer = setTimeout(() => finish({ duration: null, width: null, height: null }), META_TIMEOUT_MS);
    el.preload = 'metadata';
    el.onloadedmetadata = () => {
      const d = Number.isFinite(el.duration) ? el.duration : null;
      finish({
        duration: d,
        width: tag === 'video' && /** @type {HTMLVideoElement} */ (el).videoWidth ? /** @type {HTMLVideoElement} */ (el).videoWidth : null,
        height: tag === 'video' && /** @type {HTMLVideoElement} */ (el).videoHeight ? /** @type {HTMLVideoElement} */ (el).videoHeight : null,
      });
    };
    el.onerror = () => finish({ duration: null, width: null, height: null });
    el.src = url;
  });
}

/**
 * Replace the extension of a file name.
 * @param {string} name
 * @param {string} ext without dot
 * @returns {string}
 */
function withExtension(name, ext) {
  const i = name.lastIndexOf('.');
  return `${i > 0 ? name.slice(0, i) : name}.${ext}`;
}

/* ------------------------------------------------------------------------------------------ */
/* Upload queue                                                                               */
/* ------------------------------------------------------------------------------------------ */

/** @type {UploadJob[]} */
const waiting = [];
let active = 0;

function pumpQueue() {
  while (active < MAX_PARALLEL && waiting.length > 0) {
    const job = /** @type {UploadJob} */ (waiting.shift());
    active += 1;
    job._run().finally(() => {
      active -= 1;
      pumpQueue();
    });
  }
}

/**
 * One file upload: prepare (downscale + metadata) then POST it with XHR.
 */
export class UploadJob {
  /**
   * @param {File|Blob} file
   * @param {{asFile?: boolean, audioOnly?: boolean, duration?: number, limitBytes?: number}} [opts]
   */
  constructor(file, opts = {}) {
    this.file = file;
    this.opts = opts;
    /** @type {'queued'|'uploading'|'done'|'failed'|'cancelled'} */
    this.state = 'queued';
    this.progress = 0;
    /** @type {((p: number) => void)|null} */
    this.onProgress = null;
    /** @type {((d: {width: number|null, height: number|null, duration: number|null}) => void)|null} */
    this.onMeta = null;
    /** @type {any} */
    this.xhr = null;
    this._cancelled = false;
    /** @type {{blob: Blob, name: string, meta: object}|null} */
    this._prepared = null;
    /** @type {{width: number|null, height: number|null, duration: number|null}|null} */
    this.dims = null;
    /** @type {(a: any) => void} */
    this._resolve = () => {};
    /** @type {(e: any) => void} */
    this._reject = () => {};
    /** @type {Promise<any>} */
    this.promise = this._newPromise();
  }

  /** @returns {Promise<any>} */
  _newPromise() {
    return new Promise((resolve, reject) => {
      this._resolve = resolve;
      this._reject = reject;
    });
  }

  /** Put the job in the queue. */
  start() {
    waiting.push(this);
    pumpQueue();
  }

  /**
   * Run the upload again after a failure (restarts from 0, no resume in v1).
   * @returns {Promise<any>} a fresh promise
   */
  restart() {
    this._cancelled = false;
    this.state = 'queued';
    this.progress = 0;
    this.promise = this._newPromise();
    this.start();
    return this.promise;
  }

  /** Abort a queued or running upload; the promise rejects with UploadError('cancelled'). */
  cancel() {
    if (this.state === 'done' || this.state === 'cancelled') return;
    this._cancelled = true;
    const i = waiting.indexOf(this);
    if (i >= 0) {
      waiting.splice(i, 1);
      this._finishCancelled();
      return;
    }
    if (this.xhr) {
      try {
        this.xhr.abort();
      } catch (_) {
        /* ignore */
      }
    }
  }

  _finishCancelled() {
    this.state = 'cancelled';
    this._reject(new UploadError('cancelled', describeError({ code: 'cancelled' })));
  }

  /** @param {number} p 0..1 */
  _setProgress(p) {
    this.progress = p;
    if (this.onProgress) this.onProgress(p);
  }

  /** Worker body, called by the queue. @returns {Promise<void>} */
  async _run() {
    this.state = 'uploading';
    try {
      if (!this._prepared) {
        this._prepared = await this._prepare();
        if (this.onMeta && this.dims) this.onMeta(this.dims);
      }
      if (this._cancelled) throw new UploadError('cancelled', describeError({ code: 'cancelled' }));
      const att = await this._postWithRetry(this._prepared);
      this.state = 'done';
      this._setProgress(1);
      this._resolve(att);
    } catch (err) {
      const e = err instanceof UploadError ? err : new UploadError('network', describeError({ code: 'network' }));
      this.state = e.code === 'cancelled' ? 'cancelled' : 'failed';
      this._reject(e);
    }
  }

  /**
   * Downscale images (unless "Send as file"), measure media (SPEC 9.9(5), 5.6 X-Meta).
   * @returns {Promise<{blob: Blob, name: string, meta: Record<string, any>}>}
   */
  async _prepare() {
    try {
      return await this._prepareInner();
    } catch (_) {
      // Downscaling or measuring is an optimisation; the original file is always sendable.
      return { blob: /** @type {Blob} */ (this.file), name: nameOf(this.file), meta: {} };
    }
  }

  /** @returns {Promise<{blob: Blob, name: string, meta: Record<string, any>}>} */
  async _prepareInner() {
    const file = this.file;
    const kind = kindOf(file);
    const name = nameOf(file);
    /** @type {Record<string, any>} */
    const meta = {};
    let blob = /** @type {Blob} */ (file);
    let outName = name;
    if (kind === 'image') {
      const img = await decodeImage(file);
      if (img) {
        let { width, height } = img;
        if (!this.opts.asFile && file.type !== 'image/gif' && (file.size > DOWNSCALE_MIN_BYTES || Math.max(width, height) > MAX_IMAGE_SIDE)) {
          const out = await encodeJpeg(file, img);
          if (out && (out.size < file.size || Math.max(width, height) > MAX_IMAGE_SIDE)) {
            const scale = Math.min(1, MAX_IMAGE_SIDE / Math.max(width, height));
            width = Math.max(1, Math.round(width * scale));
            height = Math.max(1, Math.round(height * scale));
            blob = out;
            outName = withExtension(name, 'jpg');
          }
        }
        img.close();
        meta.width = width;
        meta.height = height;
        this.dims = { width, height, duration: null };
      }
    } else if (kind === 'video' || (kind === 'audio' && !this.opts.duration)) {
      const m = await readMediaMeta(file, kind);
      if (m.duration !== null) meta.duration = Math.round(m.duration * 100) / 100;
      if (kind === 'video' && m.width && m.height) {
        meta.width = m.width;
        meta.height = m.height;
      }
      this.dims = { width: m.width, height: m.height, duration: m.duration };
    }
    if (kind === 'audio' || this.opts.audioOnly) {
      if (this.opts.duration) meta.duration = Math.round(Number(this.opts.duration) * 100) / 100;
      if (this.opts.audioOnly) {
        meta.audio_only = true;
        delete meta.width;
        delete meta.height;
      }
      if (!this.dims) this.dims = { width: null, height: null, duration: meta.duration ?? null };
    }
    return { blob, name: outName, meta };
  }

  /**
   * POST with automatic retries after 429 (Retry-After).
   * @param {{blob: Blob, name: string, meta: Record<string, any>}} prepared
   * @returns {Promise<any>} the attachment object
   */
  async _postWithRetry(prepared) {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this._post(prepared);
      } catch (err) {
        if (!(err instanceof UploadError) || err.code !== 'rate_limited' || attempt >= MAX_429_RETRIES || this._cancelled) throw err;
        const wait = Math.max(1, Number(/** @type {any} */ (err).retryAfter) || 2) * 1000;
        await new Promise((r) => setTimeout(r, wait));
        if (this._cancelled) throw new UploadError('cancelled', describeError({ code: 'cancelled' }));
      }
    }
  }

  /**
   * One XHR POST /api/upload.
   * @param {{blob: Blob, name: string, meta: Record<string, any>}} prepared
   * @returns {Promise<any>}
   */
  _post(prepared) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      this.xhr = xhr;
      xhr.open('POST', '/api/upload');
      xhr.setRequestHeader('X-Requested-With', 'desktalk');
      xhr.setRequestHeader('X-File-Name', encodeURIComponent(prepared.name));
      if (Object.keys(prepared.meta).length) xhr.setRequestHeader('X-Meta', JSON.stringify(prepared.meta));
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total > 0) this._setProgress(Math.min(0.99, e.loaded / e.total));
      };
      xhr.onload = () => {
        this.xhr = null;
        /** @type {any} */
        let body = null;
        try {
          body = JSON.parse(xhr.responseText);
        } catch (_) {
          body = null;
        }
        if (xhr.status === 201 && body && body.attachment) {
          resolve(body.attachment);
          return;
        }
        const e = body && body.error ? body.error : {};
        const code = typeof e.code === 'string' ? e.code : xhr.status === 401 ? 'unauthorized' : xhr.status === 429 ? 'rate_limited' : 'server_error';
        const limit = (this.opts.limitBytes) || defaultStore.limits.max_upload_bytes;
        const err = new UploadError(code, describeError({ code, status: xhr.status, message: e.msg }, limit), xhr.status);
        if (code === 'rate_limited') {
          const ra = Number(xhr.getResponseHeader('Retry-After'));
          /** @type {any} */ (err).retryAfter = Number.isFinite(ra) && ra > 0 ? ra : e.retry_after;
        }
        if (code === 'unauthorized') api.reportUnauthorized(/** @type {any} */ (err));
        reject(err);
      };
      xhr.onerror = () => {
        this.xhr = null;
        reject(new UploadError('network', describeError({ code: 'network' })));
      };
      xhr.ontimeout = xhr.onerror;
      xhr.onabort = () => {
        this.xhr = null;
        reject(new UploadError('cancelled', describeError({ code: 'cancelled' })));
      };
      xhr.send(prepared.blob);
    });
  }
}

/* ------------------------------------------------------------------------------------------ */
/* Public entry point                                                                         */
/* ------------------------------------------------------------------------------------------ */

/**
 * Queue files for sending: one outbox item (and one message) per file, in selection order. The
 * caption and the reply target go on the first message only. At most 10 files per call.
 * @param {number} chatId
 * @param {Iterable<File|Blob>|ArrayLike<File|Blob>} files
 * @param {string} [caption='']
 * @param {{reply_to_id?: number|null, asFile?: boolean, audioOnly?: boolean, duration?: number}} [opts]
 * @returns {{items: any[], rejected: Array<{name: string, reason: string}>}}
 */
export function enqueueFiles(chatId, files, caption = '', opts = {}) {
  const outbox = defaultOutbox;
  const list = Array.from(/** @type {any} */ (files) || []);
  /** @type {Array<{name: string, reason: string}>} */
  const rejected = [];
  /** @type {Array<File|Blob>} */
  const accepted = [];
  for (const f of list) {
    if (accepted.length >= MAX_FILES_PER_SEND) {
      rejected.push({ name: nameOf(f), reason: `You can send up to ${MAX_FILES_PER_SEND} files at once` });
      continue;
    }
    const pre = precheck(f);
    if (pre.ok) accepted.push(f);
    else rejected.push({ name: nameOf(f), reason: /** @type {{reason: string}} */ (pre).reason });
  }
  const items = accepted.map((file, i) => {
    const job = new UploadJob(file, { asFile: opts.asFile, audioOnly: opts.audioOnly, duration: opts.duration, limitBytes: defaultStore.limits.max_upload_bytes });
    const kind = kindOf(file);
    let url = null;
    if (kind === 'image' || kind === 'video') {
      try {
        url = URL.createObjectURL(file);
      } catch (_) {
        url = null;
      }
    }
    const item = outbox.add({
      chat_id: chatId,
      body: i === 0 ? caption : '',
      reply_to_id: i === 0 ? opts.reply_to_id || null : null,
      attachment_promise: job.promise,
      upload: job,
      preview: { name: nameOf(file), size: file.size, mime: file.type || '', kind, width: null, height: null, duration: opts.duration ?? null, url },
    });
    job.onProgress = (p) => outbox.update(item.client_id, { progress: p });
    job.onMeta = (d) => outbox.update(item.client_id, { attachment: { width: d.width, height: d.height, duration: d.duration ?? opts.duration ?? null } });
    job.start();
    return item;
  });
  return { items, rejected };
}
