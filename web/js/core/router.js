/**
 * core/router.js - hash routing, mobile history rules and the overlay "layer" stack (SPEC 9.8).
 *
 * Routes: #/ (list), #/c/<chatId>[/m/<messageId>] (chat), #/starred, #/settings[/<tab>], #/admin[/<tab>].
 * On narrow screens (< 900 px) navigations use pushState (so the hardware Back button pops them)
 * and every open layer (lightbox, menu, drawer, search bar, dialog ...) owns a history entry too;
 * on wide screens opening a chat uses replaceState. A cold load of a sub-view on a narrow screen
 * first inserts a list entry so Back never leaves the app.
 *
 * Exported API (docs/ui-core-api.md section 9): router, parseRoute, Router.
 */

import { Emitter } from './util.js';

export const NARROW_PX = 900;

/** @typedef {{name: 'list'|'chat'|'starred'|'settings'|'admin', path: string, chatId?: number, messageId?: number, tab?: string}} Route */

/**
 * @param {string} s
 * @returns {number|null} positive integer or null
 */
function parseId(s) {
  return /^[1-9]\d{0,14}$/.test(s || '') ? Number(s) : null;
}

/**
 * @param {string} seg
 * @returns {string}
 */
function safeDecode(seg) {
  try {
    return decodeURIComponent(seg);
  } catch (_) {
    return seg;
  }
}

/**
 * Parse a hash (with or without leading "#") or a path into a route. Unknown input is the list.
 * @param {string} hash
 * @returns {Route}
 */
export function parseRoute(hash) {
  let s = String(hash || '');
  if (s.startsWith('#')) s = s.slice(1);
  const q = s.indexOf('?');
  if (q >= 0) s = s.slice(0, q);
  const parts = s.split('/').filter(Boolean).map(safeDecode);
  const list = /** @type {Route} */ ({ name: 'list', path: '/' });
  if (parts.length === 0) return list;
  const [a, b, c, d] = parts;
  if (a === 'c') {
    const chatId = parseId(b);
    if (chatId === null) return list;
    if (parts.length === 2) return { name: 'chat', path: `/c/${chatId}`, chatId };
    if (parts.length === 4 && c === 'm') {
      const messageId = parseId(d);
      if (messageId !== null) return { name: 'chat', path: `/c/${chatId}/m/${messageId}`, chatId, messageId };
    }
    return list;
  }
  if (a === 'starred' && parts.length === 1) return { name: 'starred', path: '/starred' };
  if ((a === 'settings' || a === 'admin') && parts.length <= 2) {
    if (b !== undefined && !/^[A-Za-z0-9_-]{1,32}$/.test(b)) return list;
    return { name: /** @type {'settings'|'admin'} */ (a), path: b ? `/${a}/${b}` : `/${a}`, tab: b };
  }
  return list;
}

/**
 * Build a route object from its parts.
 * @param {'list'|'chat'|'starred'|'settings'|'admin'} name
 * @param {{chatId?: number, messageId?: number, tab?: string}} [o]
 * @returns {Route}
 */
function makeRoute(name, o = {}) {
  if (name === 'chat' && o.chatId) {
    return o.messageId
      ? { name, path: `/c/${o.chatId}/m/${o.messageId}`, chatId: o.chatId, messageId: o.messageId }
      : { name, path: `/c/${o.chatId}`, chatId: o.chatId };
  }
  if (name === 'starred') return { name, path: '/starred' };
  if (name === 'settings' || name === 'admin') return { name, path: o.tab ? `/${name}/${o.tab}` : `/${name}`, tab: o.tab };
  return { name: 'list', path: '/' };
}

/**
 * @typedef {object} Layer
 * @property {string} id
 * @property {number} priority
 * @property {() => void} close
 * @property {number} seq
 * @property {boolean} pushed owns a history entry
 * @property {boolean} closing
 * @property {{release: () => void, dismiss: () => void, isTop: () => boolean}} handle
 */

export class Router extends Emitter {
  constructor() {
    super();
    /** @type {Route} */
    this._route = { name: 'list', path: '/' };
    /** @type {((r: Route) => Route|null)|null} */
    this._guard = null;
    /** @type {Layer[]} */
    this._layers = [];
    this._seq = 0;
    this._expectPops = 0;
    this._lastHash = '';
    this._started = false;
    this._emitted = false;
  }

  /** @returns {Route} the current route */
  get current() {
    return this._route;
  }

  /** @returns {number} number of open layers */
  get layerCount() {
    return this._layers.length;
  }

  /** @returns {boolean} viewport narrower than 900 px */
  isNarrow() {
    try {
      if (typeof matchMedia === 'function') return matchMedia(`(max-width: ${NARROW_PX - 0.02}px)`).matches;
    } catch (_) {
      /* fall back */
    }
    return typeof window !== 'undefined' ? window.innerWidth < NARROW_PX : false;
  }

  /**
   * @param {Route|string} target a route or a path/hash
   * @returns {Route}
   */
  _toRoute(target) {
    return typeof target === 'string' ? parseRoute(target) : target;
  }

  /**
   * "#/c/12" for a route or path.
   * @param {Route|string} target
   * @returns {string}
   */
  href(target) {
    return '#' + this._toRoute(target).path;
  }

  /**
   * @param {string} hash
   * @returns {Route}
   */
  parse(hash) {
    return parseRoute(hash);
  }

  /** Install listeners and apply the route of the current URL (main.js, once). */
  start() {
    if (this._started) return;
    this._started = true;
    try {
      history.scrollRestoration = 'manual';
    } catch (_) {
      /* not supported */
    }
    const route = parseRoute(location.hash);
    const fc = (history.state && history.state.fc) || 0;
    try {
      if (this.isNarrow() && route.name !== 'list' && !fc) {
        history.replaceState({ fc: 0 }, '', '#/');
        history.pushState({ fc: 1 }, '', '#' + route.path);
      } else {
        history.replaceState({ fc }, '', location.href);
      }
    } catch (_) {
      /* history may be unavailable in sandboxes */
    }
    window.addEventListener('popstate', (e) => this._onPop(e));
    window.addEventListener('hashchange', () => this._onHashChange());
    this._lastHash = location.hash;
    this._apply(route);
  }

  /** @param {(r: Route) => Route|null} fn route guard; a returned route redirects (replace) */
  setGuard(fn) {
    this._guard = fn;
  }

  /** Run the guard again for the current route (after ev.ready, main.js). */
  revalidate() {
    const r = this._guarded(this._route);
    if (r.path !== this._route.path) this._apply(r);
  }

  /**
   * Navigate. Narrow screens push history entries; on wide screens moving between chats / to
   * the list replaces the entry. An explicit `replace` wins.
   * @param {Route|string} target
   * @param {{replace?: boolean}} [opts]
   */
  go(target, opts = {}) {
    const route = this._toRoute(target);
    const hash = '#' + route.path;
    const cur = location.hash === '' ? '#/' : location.hash;
    if (hash === cur && route.path === this._route.path) return;
    const narrow = this.isNarrow();
    const replace = opts.replace !== undefined
      ? opts.replace
      : !narrow && (route.name === 'chat' || route.name === 'list') && (this._route.name === 'chat' || this._route.name === 'list');
    const fc = (history.state && history.state.fc) || 0;
    try {
      if (replace) history.replaceState({ fc }, '', hash);
      else history.pushState({ fc: fc + 1 }, '', hash);
    } catch (_) {
      location.hash = hash;
      return;
    }
    this._lastHash = hash;
    this._apply(route);
  }

  /**
   * @param {number} chatId
   * @param {{messageId?: number, replace?: boolean}} [opts]
   */
  openChat(chatId, opts = {}) {
    this.go(makeRoute('chat', { chatId, messageId: opts.messageId }), { replace: opts.replace });
  }

  /** @param {{replace?: boolean}} [opts] */
  home(opts = {}) {
    this.go(makeRoute('list'), opts);
  }

  /** @param {string} [tab] */
  openSettings(tab) {
    this.go(makeRoute('settings', { tab }));
  }

  /** @param {string} [tab] */
  openAdmin(tab) {
    this.go(makeRoute('admin', { tab }));
  }

  openStarred() {
    this.go(makeRoute('starred'));
  }

  /**
   * Back: closes the top layer first, else history.back() when the app has an earlier entry,
   * else the list.
   */
  back() {
    const top = this._topLayer();
    if (top) {
      top.handle.dismiss();
      return;
    }
    const fc = (history.state && history.state.fc) || 0;
    if (fc > 0) history.back();
    else if (this._route.name !== 'list') this.go('/', { replace: true });
  }

  /** Logical parent: every sub-view goes to the list (replaces the entry). */
  up() {
    if (this._route.name !== 'list') this.go('/', { replace: true });
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Route application                                                                        */
  /* ---------------------------------------------------------------------------------------- */

  /** Browser Back/Forward. @param {PopStateEvent} e */
  _onPop(e) {
    if (this._expectPops > 0) {
      this._expectPops -= 1;
      this._lastHash = location.hash;
      return;
    }
    const hash = location.hash;
    const pushedTop = this._latestPushed();
    if (pushedTop && hash === this._lastHash) {
      this._closeByBack(pushedTop);
      return;
    }
    if (e.state && e.state.fcLayer && !pushedTop) {
      // An entry of a layer that was closed by a route change: skip over it.
      history.back();
      return;
    }
    this._syncFromLocation();
  }

  _onHashChange() {
    if (location.hash === this._lastHash) return;
    this._syncFromLocation();
  }

  _syncFromLocation() {
    this._lastHash = location.hash;
    this._apply(parseRoute(location.hash));
  }

  /**
   * Run the guard; a redirect replaces the history entry.
   * @param {Route} route
   * @returns {Route}
   */
  _guarded(route) {
    if (!this._guard) return route;
    let redirect = null;
    try {
      redirect = this._guard(route);
    } catch (err) {
      console.error('[router] guard threw', err);
    }
    if (!redirect || redirect.path === route.path) return route;
    try {
      history.replaceState({ fc: (history.state && history.state.fc) || 0 }, '', '#' + redirect.path);
    } catch (_) {
      /* ignore */
    }
    this._lastHash = '#' + redirect.path;
    return redirect;
  }

  /**
   * Make `route` current: run the guard, close layers, emit 'route'.
   * @param {Route} route
   */
  _apply(route) {
    const r = this._guarded(route);
    const prev = this._route;
    if (this._emitted && prev.path === r.path) return;
    if (prev.path !== r.path) this._closeAllLayers();
    this._emitted = true;
    this._route = r;
    this.emit('route', r, prev);
  }

  /* ---------------------------------------------------------------------------------------- */
  /* Layers                                                                                   */
  /* ---------------------------------------------------------------------------------------- */

  /**
   * Register something that Escape and the hardware Back button must close.
   * @param {{id: string, priority?: number, close: () => void}} spec
   * @returns {{release: () => void, dismiss: () => void, isTop: () => boolean}}
   */
  pushLayer(spec) {
    this._seq += 1;
    /** @type {any} */
    const layer = { id: spec.id, priority: spec.priority === undefined ? 20 : spec.priority, close: spec.close, seq: this._seq, pushed: false, closing: false };
    this._layers.push(layer);
    if (this.isNarrow()) {
      try {
        history.pushState({ fc: (history.state && history.state.fc) || 0, fcLayer: layer.seq }, '', location.href);
        layer.pushed = true;
      } catch (_) {
        /* the layer still works for Escape */
      }
    }
    layer.handle = {
      release: () => this._releaseLayer(layer),
      dismiss: () => this._dismissLayer(layer),
      isTop: () => this._topLayer() === layer,
    };
    return layer.handle;
  }

  /**
   * Close the highest-priority layer (Escape).
   * @returns {boolean} whether a layer was closed
   */
  closeTopLayer() {
    const top = this._topLayer();
    if (!top) return false;
    this._dismissLayer(top);
    return true;
  }

  /** @returns {Layer|null} highest priority, latest first */
  _topLayer() {
    let best = null;
    for (const l of this._layers) {
      if (!best || l.priority > best.priority || (l.priority === best.priority && l.seq > best.seq)) best = l;
    }
    return best;
  }

  /** @returns {Layer|null} the most recently pushed layer that owns a history entry */
  _latestPushed() {
    for (let i = this._layers.length - 1; i >= 0; i -= 1) if (this._layers[i].pushed) return this._layers[i];
    return null;
  }

  /**
   * The owner closed the layer itself.
   * @param {Layer} layer
   */
  _releaseLayer(layer) {
    if (layer.closing) return;
    layer.closing = true;
    this._layers = this._layers.filter((l) => l !== layer);
    this._popEntry(layer);
  }

  /**
   * Close the layer on behalf of the user (Escape, Back button handler).
   * @param {Layer} layer
   */
  _dismissLayer(layer) {
    if (layer.closing) return;
    layer.closing = true;
    this._layers = this._layers.filter((l) => l !== layer);
    this._popEntry(layer);
    this._callClose(layer);
  }

  /**
   * The history entry of the layer was already popped by the browser.
   * @param {Layer} layer
   */
  _closeByBack(layer) {
    if (layer.closing) return;
    layer.closing = true;
    this._layers = this._layers.filter((l) => l !== layer);
    this._callClose(layer);
  }

  /** @param {Layer} layer */
  _popEntry(layer) {
    if (!layer.pushed) return;
    this._expectPops += 1;
    try {
      history.back();
    } catch (_) {
      this._expectPops -= 1;
    }
  }

  /** @param {Layer} layer */
  _callClose(layer) {
    try {
      layer.close();
    } catch (err) {
      console.error('[router] layer close handler threw', err);
    }
  }

  /** A route change closes every layer; their history entries become inert orphans. */
  _closeAllLayers() {
    const layers = this._layers;
    this._layers = [];
    for (const l of layers) {
      if (l.closing) continue;
      l.closing = true;
      this._callClose(l);
    }
  }
}

export const router = new Router();
