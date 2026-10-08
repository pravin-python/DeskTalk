/**
 * core/sound.js - notification chimes without audio files (SPEC 9.4 "Sound engine").
 *
 * Web Audio generates a short two-note chime and a soft "sent" tick. On iOS (Web Audio obeys the
 * silent switch) the same tones are rendered once into a WAV Blob and played through an <audio>
 * element. The audio context is created lazily and unlocked on the first pointerdown / keydown /
 * touchend; while it is locked a chime is dropped (never queued) and `store.ui.soundBlocked`
 * becomes true so the sidebar can show "Sound is off - click anywhere to enable".
 * Throttles: at most one chime per 2 s globally and one per chat per 5 s (mentions bypass the
 * per-chat limit); global sound off and volume come from the preferences.
 *
 * Exported API (docs/ui-core-api.md section 11): sound.start, state, unlock, chime, sent, test.
 */

import { store as defaultStore } from './store.js';
import { isIOS } from './util.js';

const GLOBAL_MIN_GAP_MS = 2000;
const CHAT_MIN_GAP_MS = 5000;

/** [frequency Hz, start s, duration s] */
const CHIME_NOTES = [[880, 0, 0.18], [1318.5, 0.12, 0.3]];
const SENT_NOTES = [[660, 0, 0.07]];

const UNLOCK_EVENTS = ['pointerdown', 'keydown', 'touchend'];

/**
 * Render notes into a 16-bit mono WAV file.
 * @param {Array<[number, number, number]>} notes
 * @param {number} [sampleRate=22050]
 * @returns {Uint8Array}
 */
export function buildWav(notes, sampleRate = 22050) {
  const total = notes.reduce((m, [, s, d]) => Math.max(m, s + d), 0) + 0.05;
  const n = Math.ceil(total * sampleRate);
  const samples = new Float32Array(n);
  for (const [freq, start, dur] of notes) {
    const from = Math.floor(start * sampleRate);
    const len = Math.floor(dur * sampleRate);
    for (let i = 0; i < len && from + i < n; i += 1) {
      const t = i / sampleRate;
      const attack = Math.min(1, t / 0.015);
      const decay = Math.exp((-5 * t) / dur);
      samples[from + i] += Math.sin(2 * Math.PI * freq * t) * attack * decay * 0.6;
    }
  }
  const bytes = new Uint8Array(44 + n * 2);
  const dv = new DataView(bytes.buffer);
  const writeStr = (off, s) => { for (let i = 0; i < s.length; i += 1) dv.setUint8(off + i, s.charCodeAt(i)); };
  writeStr(0, 'RIFF');
  dv.setUint32(4, 36 + n * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true);
  dv.setUint32(24, sampleRate, true);
  dv.setUint32(28, sampleRate * 2, true);
  dv.setUint16(32, 2, true);
  dv.setUint16(34, 16, true);
  writeStr(36, 'data');
  dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i += 1) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    dv.setInt16(44 + i * 2, Math.round(v * 32767), true);
  }
  return bytes;
}

export class Sound {
  /**
   * @param {{store?: any}} [deps]
   */
  constructor(deps = {}) {
    this._store = deps.store || defaultStore;
    /** @type {any} */
    this._ctx = null;
    this._unlocked = false;
    this._started = false;
    this._ios = false;
    this._lastAt = 0;
    /** @type {Map<number, number>} */
    this._chatAt = new Map();
    /** @type {{chime: HTMLAudioElement|null, sent: HTMLAudioElement|null}} */
    this._audio = { chime: null, sent: null };
    this._onGesture = () => this.unlock();
  }

  /** Install the first-gesture unlock listeners (main.js, once). */
  start() {
    if (this._started || typeof document === 'undefined') return;
    this._started = true;
    this._ios = isIOS();
    for (const ev of UNLOCK_EVENTS) document.addEventListener(ev, this._onGesture, { capture: true, passive: true });
  }

  /** @returns {'locked'|'unlocked'|'unsupported'} */
  state() {
    if (!this._ios && !this._audioContextClass()) return 'unsupported';
    return this._unlocked ? 'unlocked' : 'locked';
  }

  /** @returns {any} AudioContext constructor or null */
  _audioContextClass() {
    if (typeof window === 'undefined') return null;
    return window.AudioContext || /** @type {any} */ (window).webkitAudioContext || null;
  }

  /** @returns {any} the lazily created AudioContext, or null */
  _context() {
    if (this._ctx) return this._ctx;
    const AC = this._audioContextClass();
    if (!AC) return null;
    try {
      this._ctx = new AC();
    } catch (_) {
      this._ctx = null;
    }
    return this._ctx;
  }

  /** @param {'chime'|'sent'} kind @returns {HTMLAudioElement|null} the WAV-backed element used on iOS */
  _element(kind) {
    if (this._audio[kind]) return this._audio[kind];
    try {
      const wav = buildWav(kind === 'chime' ? CHIME_NOTES : SENT_NOTES);
      const url = URL.createObjectURL(new Blob([wav], { type: 'audio/wav' }));
      const a = new Audio(url);
      a.preload = 'auto';
      this._audio[kind] = a;
      return a;
    } catch (_) {
      return null;
    }
  }

  /** Unlock audio from a user gesture (also called automatically on the first gesture). */
  unlock() {
    if (this._unlocked) return;
    if (this._ios) {
      const a = this._element('chime');
      if (!a) return;
      a.muted = true;
      const p = a.play();
      Promise.resolve(p).then(() => {
        a.pause();
        a.currentTime = 0;
        a.muted = false;
        this._markUnlocked();
      }).catch(() => { a.muted = false; });
      return;
    }
    const ctx = this._context();
    if (!ctx) return;
    try {
      Promise.resolve(ctx.resume()).then(() => {
        if (ctx.state === 'running') {
          const src = ctx.createBufferSource();
          src.buffer = ctx.createBuffer(1, 1, 22050);
          src.connect(ctx.destination);
          src.start(0);
          this._markUnlocked();
        }
      }).catch(() => {});
    } catch (_) {
      /* stay locked, retry on the next gesture */
    }
  }

  /** The browser suspended the context again: wait for the next gesture (and try to resume now). */
  _relock() {
    this._unlocked = false;
    for (const ev of UNLOCK_EVENTS) document.addEventListener(ev, this._onGesture, { capture: true, passive: true });
    const ctx = this._ctx;
    if (!ctx) return;
    try {
      Promise.resolve(ctx.resume()).then(() => {
        if (ctx.state === 'running') this._markUnlocked();
      }).catch(() => {});
    } catch (_) {
      /* wait for a gesture */
    }
  }

  _markUnlocked() {
    this._unlocked = true;
    for (const ev of UNLOCK_EVENTS) document.removeEventListener(ev, this._onGesture, { capture: true });
    this._store.setUi({ soundBlocked: false });
  }

  /**
   * @param {Array<[number, number, number]>} notes
   * @param {'chime'|'sent'} kind
   * @returns {boolean} whether playback started
   */
  _play(notes, kind) {
    const volume = Number(this._store.prefs.get('volume'));
    const vol = Number.isFinite(volume) ? volume : 0.7;
    if (this._ios) {
      const a = this._element(kind);
      if (!a || !this._unlocked) return false;
      try {
        a.volume = vol;
        a.currentTime = 0;
        const p = a.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
        return true;
      } catch (_) {
        return false;
      }
    }
    const ctx = this._ctx;
    if (!ctx || ctx.state !== 'running') return false;
    try {
      const t0 = ctx.currentTime + 0.01;
      const master = ctx.createGain();
      master.gain.value = vol * 0.35;
      master.connect(ctx.destination);
      for (const [freq, start, dur] of notes) {
        const osc = ctx.createOscillator();
        const g = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(freq, t0 + start);
        g.gain.setValueAtTime(0.0001, t0 + start);
        g.gain.exponentialRampToValueAtTime(1, t0 + start + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + start + dur);
        osc.connect(g);
        g.connect(master);
        osc.start(t0 + start);
        osc.stop(t0 + start + dur + 0.05);
      }
      return true;
    } catch (_) {
      return false;
    }
  }

  /**
   * Play the incoming-message chime, honouring preferences and throttles.
   * @param {{chatId?: number|null, mention?: boolean, force?: boolean}} [o]
   * @returns {boolean} true when a sound was started
   */
  chime(o = {}) {
    const { chatId = null, mention = false, force = false } = o;
    if (!force && !this._store.prefs.get('sound')) return false;
    const now = Date.now();
    if (!force) {
      if (now - this._lastAt < GLOBAL_MIN_GAP_MS) return false;
      if (!mention && chatId !== null && now - (this._chatAt.get(chatId) || 0) < CHAT_MIN_GAP_MS) return false;
    }
    if (!this._unlocked || !this._play(CHIME_NOTES, 'chime')) {
      if (this._unlocked) this._relock();
      this._store.setUi({ soundBlocked: true });
      return false;
    }
    this._lastAt = now;
    if (chatId !== null) this._chatAt.set(chatId, now);
    return true;
  }

  /** Soft tick for a sent message (only when the preferences allow it). */
  sent() {
    if (!this._store.prefs.get('sound') || !this._unlocked) return;
    this._play(SENT_NOTES, 'sent');
  }

  /** Settings "Play test sound": unlocks (the click is a gesture) and ignores throttles. */
  test() {
    this.unlock();
    // The context may still be resuming; give the first gesture a moment.
    if (!this.chime({ force: true })) setTimeout(() => this.chime({ force: true }), 250);
  }
}

export const sound = new Sound();
