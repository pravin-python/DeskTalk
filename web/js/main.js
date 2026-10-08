/**
 * main.js - application bootstrap (SPEC 9.1, 9.8).
 *
 * This file deliberately has NO static imports: the browser feature gate must run before any other
 * module is parsed, so an old browser sees "Please update your browser" instead of a blank page.
 * After the gate the core modules are loaded with dynamic import(), then:
 *   /api/info + /api/me -> auth view | session (store, outbox, notify, sound, router, socket)
 *   first ev.ready      -> shell: sidebar view + route view (conversation | starred | settings | admin)
 *   socket states       -> login (expired / kicked), forced password change, "too many windows", "reload"
 * View modules are mounted through `mount(container, ctx)`; a missing module only produces a
 * notice in its slot (docs/ui-core-api.md section 1).
 */

/* ------------------------------------------------------------------------------------------ */
/* Browser feature gate                                                                       */
/* ------------------------------------------------------------------------------------------ */

/**
 * Names of the missing browser features (empty when the browser is supported).
 * @returns {string[]}
 */
function missingFeatures() {
  const missing = [];
  if (typeof WebSocket !== 'function') missing.push('WebSocket');
  if (typeof fetch !== 'function') missing.push('fetch');
  try {
    if (!(window.CSS && typeof CSS.supports === 'function' && CSS.supports('height', '1dvh'))) missing.push('modern CSS (dvh units)');
  } catch (_) {
    missing.push('modern CSS (dvh units)');
  }
  try {
    new RegExp('\\p{L}', 'u');
  } catch (_) {
    missing.push('Unicode regular expressions');
  }
  return missing;
}

/**
 * Replace the page with the "update your browser" notice (DOM APIs only).
 * @param {string[]} missing
 */
function showUnsupported(missing) {
  const body = document.body;
  while (body.firstChild) body.removeChild(body.firstChild);
  const box = document.createElement('div');
  box.className = 'unsupported';
  const h1 = document.createElement('h1');
  h1.textContent = 'Please update your browser';
  const p = document.createElement('p');
  p.textContent = 'DeskTalk needs a recent browser: Chrome or Edge 108+, Firefox 101+, or Safari / iOS 15.4+.';
  const small = document.createElement('p');
  small.className = 'muted';
  small.textContent = 'Missing in this browser: ' + missing.join(', ');
  box.appendChild(h1);
  box.appendChild(p);
  box.appendChild(small);
  body.appendChild(box);
}

/* ------------------------------------------------------------------------------------------ */
/* Module loading                                                                             */
/* ------------------------------------------------------------------------------------------ */

/** @returns {Promise<any>} all core modules */
async function loadCore() {
  const [dom, iconsMod, util, apiMod, socketMod, storeMod, outboxMod, routerMod, uiMod, notifyMod, soundMod] = await Promise.all([
    import('./core/dom.js'),
    import('./core/icons.js'),
    import('./core/util.js'),
    import('./core/api.js'),
    import('./core/socket.js'),
    import('./core/store.js'),
    import('./core/outbox.js'),
    import('./core/router.js'),
    import('./core/ui.js'),
    import('./core/notify.js'),
    import('./core/sound.js'),
  ]);
  return {
    h: dom.h, clear: dom.clear, icon: iconsMod.icon,
    storage: util.storage, isInsecureRemote: util.isInsecureRemote,
    api: apiMod.api, socket: socketMod.socket, store: storeMod.store, prefs: storeMod.prefs,
    outbox: outboxMod.outbox, router: routerMod.router, ui: uiMod.ui, notify: notifyMod.notify, sound: soundMod.sound,
  };
}

/**
 * Load a view module by name. Literal specifiers keep the static import lint happy.
 * @param {string} name
 * @returns {Promise<any>}
 */
function importView(name) {
  switch (name) {
    case 'auth': return import('./views/auth.js');
    case 'sidebar': return import('./views/sidebar.js');
    case 'conversation': return import('./views/conversation.js');
    case 'starred': return import('./views/starred.js');
    case 'settings': return import('./views/settings.js');
    case 'admin': return import('./views/admin.js');
    default: return Promise.reject(new Error(`unknown view "${name}"`));
  }
}

/** Route name -> view module name. */
const ROUTE_VIEW = { chat: 'conversation', starred: 'starred', settings: 'settings', admin: 'admin' };

/** Texts of SPEC 8.5 for `ev.kicked` reasons. */
const KICK_TEXT = {
  disabled: 'Your account has been disabled. Contact your admin.',
  revoked: 'You were signed out (this session was ended on another device or by an administrator).',
  logout: 'You signed out.',
  password_changed: 'Password changed - please sign in again.',
};
const EXPIRED_TEXT = 'Your session expired. Please sign in again.';

/* ------------------------------------------------------------------------------------------ */
/* Application                                                                                */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {any} core the loaded core modules
 * @returns {{start: () => Promise<void>}}
 */
function createApp(core) {
  const { h, clear, icon, storage, api, socket, store, prefs, outbox, router, ui, notify, sound } = core;
  const $ = (id) => document.getElementById(id);
  const $boot = $('boot');
  const $auth = $('auth-root');
  const $app = $('app');
  const $sidebar = $('pane-sidebar');
  const $main = $('pane-main');
  const $drawer = $('pane-drawer');
  const $banner = $('conn-banner');

  /** @type {any} */
  const S = {
    info: null, me: null, active: false, wired: false, shellShown: false,
    sidebarRec: null, routeRec: null, authRec: null, routeToken: 0, routeChain: Promise.resolve(),
    empty: null, tooMany: null, offUnauthorized: null,
  };

  /* ------------------------------ small helpers ------------------------------ */

  /** Apply theme and font-size preferences to <html>. */
  function applyPrefs() {
    const root = document.documentElement;
    const theme = prefs.get('theme');
    if (theme === 'light' || theme === 'dark') root.dataset.theme = theme;
    else delete root.dataset.theme;
    const font = prefs.get('fontSize');
    if (font === 'small' || font === 'large') root.dataset.font = font;
    else delete root.dataset.font;
  }

  /** Keep `--app-h` / `--app-top` in sync with the visual viewport (iOS keyboard, SPEC 9.8). */
  function bindViewport() {
    const root = document.documentElement;
    const update = () => {
      const vv = window.visualViewport;
      root.style.setProperty('--app-h', `${Math.round(vv ? vv.height : window.innerHeight)}px`);
      root.style.setProperty('--app-top', `${Math.round(vv ? vv.offsetTop : 0)}px`);
    };
    update();
    window.addEventListener('resize', update);
    window.addEventListener('orientationchange', update);
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', update);
      window.visualViewport.addEventListener('scroll', update);
    }
  }

  /** Escape closes layers; Ctrl+K focuses search; Alt+Up/Down switches chats. */
  function bindGlobalKeys() {
    document.addEventListener('keydown', (e) => {
      const key = e.key || '';
      if (key === 'Escape' && !e.defaultPrevented) {
        if (router.closeTopLayer()) e.preventDefault();
        return;
      }
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && key.toLowerCase() === 'k') {
        const el = /** @type {HTMLInputElement|null} */ (document.querySelector('[data-shortcut="search"]'));
        if (el) {
          e.preventDefault();
          el.focus();
          if (typeof el.select === 'function') el.select();
        }
        return;
      }
      if (e.altKey && !e.ctrlKey && !e.shiftKey && !e.metaKey && (key === 'ArrowUp' || key === 'ArrowDown') && S.active && store.isReady) {
        const list = store.chatList({});
        if (!list.length) return;
        const cur = router.current.chatId;
        const i = list.findIndex((c) => c.id === cur);
        const next = key === 'ArrowDown' ? (i < 0 ? 0 : Math.min(list.length - 1, i + 1)) : (i < 0 ? list.length - 1 : Math.max(0, i - 1));
        e.preventDefault();
        router.openChat(list[next].id);
      }
    });
  }

  /** Show the "Reconnecting..." banner (store computes the 2 s delay). */
  function renderBanner() {
    const c = store.connection;
    const live = S.active && ['connecting', 'open', 'waiting'].includes(c.state);
    const show = live && (c.banner || !c.online);
    $banner.hidden = !show;
    if (show) $banner.textContent = c.online ? 'Reconnecting…' : 'No network connection - waiting to reconnect…';
  }

  /** @param {string} name view name @returns {HTMLElement} */
  function missingNotice(name) {
    return h('div.view-missing.empty-state', h('p', `The "${name}" screen is not available yet.`));
  }

  /* ------------------------------ view mounting ------------------------------ */

  /**
   * @param {AbortController} ac
   * @returns {object} the ctx passed to views
   */
  function makeCtx(ac) {
    return {
      slots: { root: document.body, auth: $auth, sidebar: $sidebar, main: $main, drawer: $drawer, overlay: $('overlay-root') },
      signal: ac.signal,
      logout,
    };
  }

  /**
   * Mount a view module into a slot. Never throws.
   * @param {string} name
   * @param {HTMLElement} slot
   * @param {object} [extra] extra ctx fields
   * @returns {Promise<{name: string, slot: HTMLElement, handle: any, ac: AbortController, dead: boolean}>}
   */
  async function mountInto(name, slot, extra = {}) {
    const rec = { name, slot, handle: /** @type {any} */ (null), ac: new AbortController(), dead: false };
    clear(slot);
    let mod = null;
    try {
      mod = await importView(name);
      if (typeof mod.mount !== 'function') throw new Error('the module has no mount() export');
    } catch (err) {
      console.warn(`[main] view "${name}" could not be loaded`, err);
      mod = null;
    }
    if (rec.dead) return rec;
    if (!mod) {
      slot.appendChild(missingNotice(name));
      return rec;
    }
    try {
      const res = await mod.mount(slot, { ...makeCtx(rec.ac), ...extra });
      if (rec.dead) {
        safeUnmount(res);
        return rec;
      }
      rec.handle = res || {};
    } catch (err) {
      console.error(`[main] view "${name}" failed to mount`, err);
      clear(slot);
      slot.appendChild(h('div.view-missing.empty-state', h('p', `The "${name}" screen failed to load.`)));
    }
    return rec;
  }

  /** @param {any} handle */
  function safeUnmount(handle) {
    try {
      if (handle && typeof handle.unmount === 'function') handle.unmount();
    } catch (err) {
      console.error('[main] view unmount failed', err);
    }
  }

  /** @param {any} rec a record from mountInto (or null) */
  function unmountRec(rec) {
    if (!rec) return;
    rec.dead = true;
    rec.ac.abort();
    safeUnmount(rec.handle);
    clear(rec.slot);
  }

  /** Close dialogs, menus and sheets that belong to a session that is ending. */
  function closeOverlays() {
    for (let i = 0; i < 20 && router.layerCount > 0; i += 1) router.closeTopLayer();
  }

  function unmountAll() {
    closeOverlays();
    S.routeToken += 1;
    unmountRec(S.sidebarRec);
    unmountRec(S.routeRec);
    unmountRec(S.authRec);
    S.sidebarRec = S.routeRec = S.authRec = null;
    teardownEmpty();
    ui.setDrawerOpen(false);
  }

  /* ------------------------------ main pane: empty state ------------------------------ */

  function teardownEmpty() {
    if (!S.empty) return;
    for (const off of S.empty.offs) off();
    S.empty = null;
  }

  /** Desktop "no chat open" pane: workspace name, hint and status chips (SPEC 9.10). */
  function renderEmpty() {
    teardownEmpty();
    clear($main);
    const title = h('h1', store.workspace.name);
    const soundChip = h('span.chip');
    const notifyChip = h('span.chip');
    const render = () => {
      title.textContent = store.workspace.name;
      soundChip.textContent = `Sound: ${prefs.get('sound') ? 'on' : 'off'}`;
      soundChip.title = prefs.get('sound') && store.ui.soundBlocked ? 'Sound is off - click anywhere to enable' : '';
      notifyChip.textContent = notify.status().text;
    };
    render();
    $main.appendChild(h('div.empty-pane',
      h('div.empty-pane-icon', icon('chat', { size: 72 })),
      title,
      h('p.muted', 'Select a chat to start messaging'),
      h('div.row.chips', soundChip, notifyChip)));
    S.empty = { offs: [store.on('workspace', render), store.on('prefs', render), store.on('ui', render)] };
  }

  /* ------------------------------ routing ------------------------------ */

  /**
   * Show the view of a route (serialised; only the latest request matters).
   * @param {any} route
   */
  function showRoute(route) {
    const token = ++S.routeToken;
    S.routeChain = S.routeChain.then(() => applyRoute(route, token)).catch((err) => console.error('[main] route failed', err));
  }

  /**
   * @param {any} route
   * @param {number} token
   */
  async function applyRoute(route, token) {
    if (token !== S.routeToken || !S.active || !S.shellShown) return;
    $app.dataset.view = route.name;
    const name = /** @type {Record<string, string>} */ (ROUTE_VIEW)[route.name] || null;
    if (!name) {
      unmountRec(S.routeRec);
      S.routeRec = null;
      renderEmpty();
      return;
    }
    if (S.routeRec && S.routeRec.name === name && !S.routeRec.dead && typeof S.routeRec.handle?.update === 'function') {
      try {
        S.routeRec.handle.update(route);
      } catch (err) {
        console.error('[main] view update failed', err);
      }
      return;
    }
    unmountRec(S.routeRec);
    S.routeRec = null;
    teardownEmpty();
    const rec = await mountInto(name, $main, { route });
    if (token !== S.routeToken || !S.active) {
      unmountRec(rec);
      return;
    }
    S.routeRec = rec;
  }

  /**
   * Route guard: a chat that does not exist in the store is not navigable.
   * @param {any} route
   * @returns {any}
   */
  function guard(route) {
    if (route.name === 'chat' && store.isReady && !store.getChat(route.chatId)) {
      ui.toast('Chat not found', { type: 'error', key: 'chat-not-found' });
      return { name: 'list', path: '/' };
    }
    return null;
  }

  /* ------------------------------ auth ------------------------------ */

  /**
   * Show the login / register / setup / forced password-change screen.
   * @param {{mode?: 'auth'|'change_password', notice?: string|null, me?: any}} [o]
   */
  async function showAuth(o = {}) {
    const { mode = 'auth', notice = null, me = null } = o;
    unmountAll();
    $app.hidden = true;
    $boot.hidden = true;
    $banner.hidden = true;
    $auth.hidden = false;
    document.title = (S.info && S.info.name) || 'DeskTalk';
    S.authRec = await mountInto('auth', $auth, { mode, info: S.info, notice, me, done: onAuthDone });
  }

  /** @param {any} me result of login / register / password change */
  async function onAuthDone(me) {
    let who = me;
    if (!who || typeof who.id !== 'number') {
      try {
        who = (await api.getMe({ silent401: true })).me;
      } catch (err) {
        await showAuth({ notice: EXPIRED_TEXT });
        return;
      }
    }
    if (who.must_change_password) {
      await showAuth({ mode: 'change_password', me: who });
      return;
    }
    startSession(who);
  }

  /**
   * Leave the signed-in state.
   * @param {{notice?: string|null, wipe?: boolean, keepStorage?: boolean}} [o]
   */
  async function endSession(o = {}) {
    const { notice = null, wipe = true } = o;
    if (!S.active) return;
    S.active = false;
    S.shellShown = false;
    socket.stop();
    if (S.tooMany) {
      S.tooMany.close();
      S.tooMany = null;
    }
    unmountAll();
    if (wipe) storage.wipe();
    outbox.reset({ wipe });
    store.reset();
    $app.hidden = true;
    $banner.hidden = true;
    router.home({ replace: true });
    try {
      S.info = await api.getInfo();
    } catch (_) {
      /* keep the last known info */
    }
    await showAuth({ notice });
  }

  /**
   * Explicit logout (Settings). Resolves false when the user cancelled.
   * @param {{clearDevice?: boolean}} [o]
   * @returns {Promise<boolean>}
   */
  async function logout(o = {}) {
    const { clearDevice = true } = o;
    if (clearDevice && outbox.hasPending()) {
      const n = outbox.count();
      const ok = await ui.confirm({
        title: 'Sign out?',
        message: `${n} unsent ${n === 1 ? 'message' : 'messages'} will be lost.`,
        confirmLabel: 'Sign out',
        danger: true,
      });
      if (!ok) return false;
    }
    socket.stop();
    await api.logout();
    await endSession({ notice: KICK_TEXT.logout, wipe: clearDevice });
    return true;
  }

  /* ------------------------------ session ------------------------------ */

  /** Connection states that end or interrupt the session. */
  function onSocketState(state) {
    if (!S.active) return;
    renderBanner();
    if (state === 'unauthorized') {
      endSession({ notice: EXPIRED_TEXT });
    } else if (state === 'kicked') {
      endSession({ notice: KICK_TEXT[socket.info.kickReason] || EXPIRED_TEXT });
    } else if (state === 'password_change') {
      socket.stop();
      S.active = false;
      S.shellShown = false;
      outbox.reset({ wipe: false });
      store.reset();
      showAuth({ mode: 'change_password', me: S.me });
    } else if (state === 'toomany') {
      if (!S.tooMany) {
        S.tooMany = ui.fatal('DeskTalk is open in too many windows (max 8)', 'Close another window, then use this one.', {
          actionLabel: 'Use this window',
          onAction: () => {
            S.tooMany = null;
            socket.reconnect();
          },
        });
      }
    } else if (state === 'outdated') {
      ui.fatal('DeskTalk was updated', 'Please reload the page to continue.', { actionLabel: 'Reload', onAction: () => location.reload() });
    } else if (state === 'ready' && S.tooMany) {
      S.tooMany.close();
      S.tooMany = null;
    }
  }

  /** One-time wiring of store / router / socket listeners (idempotent per page). */
  function wire() {
    if (S.wired) return;
    S.wired = true;
    store.on('connection', renderBanner);
    store.on('prefs', applyPrefs);
    store.on('ready', () => {
      if (!S.active) return;
      if (!S.shellShown) showShell();
      else router.revalidate();
    });
    store.on('chat_removed', (e) => {
      const r = router.current;
      if (S.active && r.name === 'chat' && r.chatId === e.chat_id) {
        router.home({ replace: true });
        ui.toast(`You were removed from ${e.title || 'the chat'}`, { type: 'error' });
      }
    });
    socket.on('state', onSocketState);
    router.on('route', (route) => {
      if (S.active && S.shellShown) showRoute(route);
    });
  }

  /** First ev.ready: reveal the shell and mount the views. */
  function showShell() {
    S.shellShown = true;
    $boot.hidden = true;
    $auth.hidden = true;
    $app.hidden = false;
    document.title = store.workspace.name || 'DeskTalk';
    S.sidebarRec = null;
    mountInto('sidebar', $sidebar).then((rec) => {
      if (!S.active) {
        unmountRec(rec);
        return;
      }
      S.sidebarRec = rec;
    });
    router.revalidate();
    showRoute(router.current);
  }

  /**
   * Begin an authenticated session for `me`.
   * @param {any} me
   */
  function startSession(me) {
    unmountRec(S.authRec);
    S.authRec = null;
    S.me = me;
    S.active = true;
    S.shellShown = false;
    $auth.hidden = true;
    $app.hidden = true;
    renderBoot('Connecting…');
    wire();
    if (S.offUnauthorized) S.offUnauthorized();
    S.offUnauthorized = api.onUnauthorized(() => endSession({ notice: EXPIRED_TEXT }));
    store.start();
    outbox.start();
    notify.start();
    sound.start();
    router.setGuard(guard);
    router.start();
    socket.start();
  }

  /**
   * Splash screen with an optional retry button.
   * @param {string} text
   * @param {{retry?: boolean}} [o]
   */
  function renderBoot(text, o = {}) {
    $boot.hidden = false;
    clear($boot);
    $boot.appendChild(h('div.boot-card',
      h('div.boot-icon', icon('chat', { size: 56 })),
      o.retry ? null : ui.spinner(24),
      h('p', { role: 'status' }, text),
      o.retry ? h('button.btn.btn-primary', { type: 'button', onClick: () => start() }, 'Try again') : null));
  }

  /** Entry point: /api/info, /api/me, then login or session. */
  async function start() {
    renderBoot('Loading…');
    try {
      S.info = await api.getInfo();
    } catch (err) {
      renderBoot("Can't reach DeskTalk. Check your network connection and try again.", { retry: true });
      return;
    }
    document.title = S.info.name || 'DeskTalk';
    try {
      const { me } = await api.getMe({ silent401: true });
      if (me.must_change_password) await showAuth({ mode: 'change_password', me });
      else startSession(me);
    } catch (err) {
      if (err && err.code === 'unauthorized') await showAuth({});
      else renderBoot("Can't reach DeskTalk. Check your network connection and try again.", { retry: true });
    }
  }

  return {
    async start() {
      applyPrefs();
      bindViewport();
      bindGlobalKeys();
      window.addEventListener('unhandledrejection', (e) => console.error('[main] unhandled rejection', e.reason));
      await start();
    },
  };
}

/* ------------------------------------------------------------------------------------------ */
/* Go                                                                                         */
/* ------------------------------------------------------------------------------------------ */

(async function main() {
  const missing = missingFeatures();
  if (missing.length) {
    showUnsupported(missing);
    return;
  }
  try {
    const core = await loadCore();
    await createApp(core).start();
  } catch (err) {
    console.error('[main] fatal start-up error', err);
    const el = document.getElementById('boot');
    if (el) {
      el.hidden = false;
      el.textContent = 'DeskTalk could not start. Reload the page, or ask your admin if the problem continues.';
    }
  }
}());
