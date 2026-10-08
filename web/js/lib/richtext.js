/**
 * lib/richtext.js - WhatsApp-style rich text (SPEC 9.3), produced as DOM nodes, never as HTML strings.
 *
 * Supported: *bold*, _italic_, ~strike~, `mono`, ```block```, "> quote" lines, "- " and "1. " lists,
 * auto-linked http(s) URLs, @mention chips (`util.findMentions`: the greedy-then-trim rule of SPEC 7.4,
 * matched WITHOUT regex lookbehind), search highlighting and emoji-only detection.
 *
 * All scanning is linear (or n log n) in the input length: URLs are found with one sticky pass, format
 * markers are matched through pre-computed closer lists, nesting is capped. The pure parts
 * (`parseRichText`, `findUrls`, `makeSnippet`, `emojiOnlyCount`) have no DOM dependency.
 *
 * Exported API (docs/ui-conv-api.md section 5): renderRichText, parseRichText, highlightText, makeSnippet,
 * findUrls, emojiOnlyCount.
 */

import { h, text as textNode } from '../core/dom.js';
import { findMentions } from '../core/util.js';

/** Marker characters of the inline formats. */
const MARKERS = '*_~`';
/** Placeholder for atoms (links, mentions) while formats are parsed; the server strips NUL from bodies. */
const ATOM = '\u0000';
const MAX_DEPTH = 3;
const MAX_URL_LENGTH = 2048;
const MAX_HIGHLIGHTS = 50;
const URL_START = /https?:\/\//gi;
const WORD = /[\p{L}\p{N}\u0000]/u;
const TRAILING_URL_CHARS = ".,;:!?'\")]}>*_~`";

/** @type {RegExp|null} */
let emojiOnlyRe = null;
/** @type {RegExp|null} */
let emojiClusterRe = null;
try {
  const cluster = '(?:\\p{Extended_Pictographic}(?:\\uFE0F|\\p{Emoji_Modifier}|\\u200D\\p{Extended_Pictographic})*|\\p{Regional_Indicator}{2}|[0-9#*]\\uFE0F?\\u20E3)';
  emojiClusterRe = new RegExp(cluster, 'gu');
  emojiOnlyRe = new RegExp(`^(?:${cluster}|\\s)+$`, 'u');
} catch (_) {
  emojiOnlyRe = null;
  emojiClusterRe = null;
}

/* ------------------------------------------------------------------------------------------ */
/* URLs                                                                                       */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {string} ch single UTF-16 unit
 * @returns {boolean}
 */
function isSpace(ch) {
  return /\s/.test(ch);
}

/**
 * Number of occurrences of `ch` in `s`.
 * @param {string} s
 * @param {string} ch
 * @returns {number}
 */
function countChar(s, ch) {
  let n = 0;
  for (let i = 0; i < s.length; i += 1) if (s[i] === ch) n += 1;
  return n;
}

/**
 * Trim trailing punctuation from a URL candidate; a closing bracket stays when it balances an
 * opening one inside the URL (Wikipedia style links).
 * @param {string} url
 * @returns {string}
 */
function trimUrl(url) {
  let u = url;
  while (u.length > 0) {
    const last = u[u.length - 1];
    if (TRAILING_URL_CHARS.indexOf(last) < 0) break;
    if (last === ')' && countChar(u, '(') >= countChar(u, ')')) break;
    if (last === ']' && countChar(u, '[') >= countChar(u, ']')) break;
    if (last === '}' && countChar(u, '{') >= countChar(u, '}')) break;
    u = u.slice(0, -1);
  }
  return u;
}

/**
 * Find http(s) URLs (linear time). A URL runs to the next whitespace; trailing punctuation is trimmed.
 * @param {string} text
 * @returns {Array<{start: number, end: number, url: string}>}
 */
export function findUrls(text) {
  const s = String(text == null ? '' : text);
  /** @type {Array<{start: number, end: number, url: string}>} */
  const out = [];
  URL_START.lastIndex = 0;
  let m;
  while ((m = URL_START.exec(s)) !== null) {
    const start = m.index;
    if (start > 0 && /[A-Za-z0-9]/.test(s[start - 1])) continue;
    let j = start + m[0].length;
    while (j < s.length && !isSpace(s[j])) j += 1;
    const raw = s.slice(start, j);
    URL_START.lastIndex = j;
    const url = trimUrl(raw);
    const hostPart = url.slice(m[0].length);
    if (url.length > MAX_URL_LENGTH || !/^[A-Za-z0-9[]/.test(hostPart)) continue;
    out.push({ start, end: start + url.length, url });
    URL_START.lastIndex = start + url.length;
  }
  return out;
}

/* ------------------------------------------------------------------------------------------ */
/* Inline parsing                                                                             */
/* ------------------------------------------------------------------------------------------ */

/**
 * @typedef {{t: 'text', v: string}|{t: 'b'|'i'|'s', c: Inline[]}|{t: 'mono', v: string}|{t: 'link', v: string, url: string}|{t: 'mention', v: string, user_id: number, self: boolean}} Inline
 * @typedef {{type: 'p'|'quote', inline: Inline[]}|{type: 'ul'|'ol', items: Inline[][], start?: number}|{type: 'code', text: string}} Block
 * @typedef {{raw: string, node: Inline}} Atom
 */

/**
 * Replace URLs and resolvable @mentions of one line by placeholders.
 * @param {string} line
 * @param {((username: string) => ({user_id: number, name: string, self: boolean}|null)|undefined)} resolve
 * @returns {{work: string, atoms: Atom[]}}
 */
function extractAtoms(line, resolve) {
  /** @type {Atom[]} */
  const atoms = [];
  let work = '';
  let pos = 0;
  const scanGap = (gap) => {
    if (!resolve || gap.indexOf('@') < 0) {
      work += gap;
      return;
    }
    let last = 0;
    for (const token of findMentions(gap, (username) => resolve(username) !== null)) {
      const found = resolve(token.username);
      if (!found) continue;
      work += gap.slice(last, token.start) + ATOM;
      atoms.push({ raw: gap.slice(token.start, token.end), node: { t: 'mention', v: found.name, user_id: found.user_id, self: Boolean(found.self) } });
      last = token.end;
    }
    work += gap.slice(last);
  };
  for (const u of findUrls(line)) {
    scanGap(line.slice(pos, u.start));
    work += ATOM;
    atoms.push({ raw: u.url, node: { t: 'link', v: u.url, url: u.url } });
    pos = u.end;
  }
  scanGap(line.slice(pos));
  return { work, atoms };
}

/**
 * Closing-marker positions of every marker character in `s` (a closer is preceded by a non-space and
 * followed by the end or a non-word character).
 * @param {string} s
 * @returns {Record<string, number[]>}
 */
function closerLists(s) {
  /** @type {Record<string, number[]>} */
  const lists = { '*': [], _: [], '~': [], '`': [] };
  for (let j = 1; j < s.length; j += 1) {
    const c = s[j];
    if (MARKERS.indexOf(c) < 0 || isSpace(s[j - 1])) continue;
    const next = s[j + 1];
    if (next === undefined || !WORD.test(next)) lists[c].push(j);
  }
  return lists;
}

/**
 * Parse the format markers of one placeholder-substituted line.
 * @param {string} s
 * @param {Atom[]} atoms
 * @param {{i: number}} cursor index of the next atom to place
 * @param {number} depth
 * @returns {Inline[]}
 */
function parseFormats(s, atoms, cursor, depth) {
  /** @type {Inline[]} */
  const out = [];
  let buf = '';
  const flush = () => {
    if (buf === '') return;
    const parts = buf.split(ATOM);
    for (let k = 0; k < parts.length; k += 1) {
      if (parts[k] !== '') out.push({ t: 'text', v: parts[k] });
      if (k < parts.length - 1) {
        const atom = atoms[cursor.i];
        cursor.i += 1;
        if (atom) out.push(atom.node);
      }
    }
    buf = '';
  };
  const lists = depth < MAX_DEPTH ? closerLists(s) : null;
  /** @type {Record<string, number>} */
  const ptr = { '*': 0, _: 0, '~': 0, '`': 0 };
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (lists && MARKERS.indexOf(c) >= 0) {
      const prev = i > 0 ? s[i - 1] : '';
      const next = s[i + 1];
      const opens = (prev === '' || !WORD.test(prev)) && next !== undefined && !isSpace(next) && next !== c;
      let close = -1;
      if (opens) {
        const list = lists[c];
        let p = ptr[c];
        while (p < list.length && list[p] <= i + 1) p += 1;
        ptr[c] = p;
        if (p < list.length) close = list[p];
      }
      if (close > 0) {
        flush();
        const inner = s.slice(i + 1, close);
        if (c === '`') {
          out.push({ t: 'mono', v: restoreAtoms(inner, atoms, cursor) });
        } else {
          out.push({ t: c === '*' ? 'b' : c === '_' ? 'i' : 's', c: parseFormats(inner, atoms, cursor, depth + 1) });
        }
        i = close + 1;
        continue;
      }
    }
    buf += c;
    i += 1;
  }
  flush();
  return out;
}

/**
 * Literal text of a span with its placeholders turned back into the original source text.
 * @param {string} s
 * @param {Atom[]} atoms
 * @param {{i: number}} cursor
 * @returns {string}
 */
function restoreAtoms(s, atoms, cursor) {
  if (s.indexOf(ATOM) < 0) return s;
  return s.split(ATOM).map((part, k, all) => {
    if (k === all.length - 1) return part;
    const atom = atoms[cursor.i];
    cursor.i += 1;
    return part + (atom ? atom.raw : '');
  }).join('');
}

/**
 * Inline parse of a paragraph / quote / list item (may contain "\n").
 * @param {string} text
 * @param {((username: string) => ({user_id: number, name: string, self: boolean}|null)|undefined)} resolve
 * @returns {Inline[]}
 */
function parseInline(text, resolve) {
  /** @type {Inline[]} */
  const out = [];
  const lines = text.split('\n');
  for (let k = 0; k < lines.length; k += 1) {
    const { work, atoms } = extractAtoms(lines[k], resolve);
    const nodes = parseFormats(work, atoms, { i: 0 }, 0);
    for (const n of nodes) out.push(n);
    if (k < lines.length - 1) out.push({ t: 'text', v: '\n' });
  }
  return out;
}

/* ------------------------------------------------------------------------------------------ */
/* Block parsing                                                                              */
/* ------------------------------------------------------------------------------------------ */

/**
 * Split a code-free text into paragraph / quote / list blocks.
 * @param {string} seg
 * @param {((username: string) => ({user_id: number, name: string, self: boolean}|null)|undefined)} resolve
 * @param {Block[]} out
 */
function parseLines(seg, resolve, out) {
  const lines = seg.split('\n');
  /** @type {string[]} */
  let para = [];
  /** @type {string[]} */
  let quote = [];
  /** @type {Block|null} */
  let list = null;
  const flushPara = () => {
    if (para.length) out.push({ type: 'p', inline: parseInline(para.join('\n'), resolve) });
    para = [];
  };
  const flushQuote = () => {
    if (quote.length) out.push({ type: 'quote', inline: parseInline(quote.join('\n'), resolve) });
    quote = [];
  };
  const flushList = () => {
    if (list) out.push(list);
    list = null;
  };
  for (const line of lines) {
    let m;
    if (line.startsWith('> ')) {
      flushPara();
      flushList();
      quote.push(line.slice(2));
    } else if ((m = /^[-•] (.*)$/.exec(line)) !== null) {
      flushPara();
      flushQuote();
      if (!list || list.type !== 'ul') {
        flushList();
        list = { type: 'ul', items: [] };
      }
      list.items.push(parseInline(m[1], resolve));
    } else if ((m = /^(\d{1,3})\. (.*)$/.exec(line)) !== null) {
      flushPara();
      flushQuote();
      if (!list || list.type !== 'ol') {
        flushList();
        list = { type: 'ol', items: [], start: Number(m[1]) };
      }
      list.items.push(parseInline(m[2], resolve));
    } else {
      flushQuote();
      flushList();
      para.push(line);
    }
  }
  flushPara();
  flushQuote();
  flushList();
}

/**
 * Parse a message body into blocks (pure; the AST is what the unit tests check).
 * @param {string} text
 * @param {((username: string) => ({user_id: number, name: string, self: boolean}|null)|undefined)} [resolveMention]
 * @returns {Block[]}
 */
export function parseRichText(text, resolveMention) {
  const src = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split(ATOM).join('');
  /** @type {Block[]} */
  const out = [];
  let pos = 0;
  /** @type {Array<{text: string, code: boolean}>} */
  const segs = [];
  for (;;) {
    const open = src.indexOf('```', pos);
    const close = open < 0 ? -1 : src.indexOf('```', open + 3);
    if (open < 0 || close < 0) break;
    const code = src.slice(open + 3, close).replace(/^\n/, '').replace(/\n$/, '');
    if (code.trim() === '') {
      segs.push({ text: src.slice(pos, close + 3), code: false });
    } else {
      if (open > pos) segs.push({ text: src.slice(pos, open), code: false });
      segs.push({ text: code, code: true });
    }
    pos = close + 3;
  }
  if (pos < src.length || segs.length === 0) segs.push({ text: src.slice(pos), code: false });
  segs.forEach((seg, k) => {
    if (seg.code) {
      out.push({ type: 'code', text: seg.text });
      return;
    }
    let t = seg.text;
    if (k > 0 && segs[k - 1].code) t = t.replace(/^\n/, '');
    if (k < segs.length - 1 && segs[k + 1].code) t = t.replace(/\n$/, '');
    if (t === '' && segs.length > 1) return;
    parseLines(t, resolveMention, out);
  });
  return out;
}

/* ------------------------------------------------------------------------------------------ */
/* Highlighting and snippets                                                                  */
/* ------------------------------------------------------------------------------------------ */

/**
 * Split `text` into plain and highlighted pieces for a case-insensitive `query`.
 * @param {string} text
 * @param {string} query
 * @returns {Array<{v: string, hit: boolean}>}
 */
function splitByQuery(text, query) {
  const q = String(query || '').trim().toLowerCase();
  const lower = text.toLowerCase();
  if (q === '' || lower.length !== text.length) return [{ v: text, hit: false }];
  /** @type {Array<{v: string, hit: boolean}>} */
  const parts = [];
  let pos = 0;
  let n = 0;
  while (n < MAX_HIGHLIGHTS) {
    const at = lower.indexOf(q, pos);
    if (at < 0) break;
    if (at > pos) parts.push({ v: text.slice(pos, at), hit: false });
    parts.push({ v: text.slice(at, at + q.length), hit: true });
    pos = at + q.length;
    n += 1;
  }
  if (pos < text.length) parts.push({ v: text.slice(pos), hit: false });
  return parts.length ? parts : [{ v: text, hit: false }];
}

/**
 * Plain text with `<mark class="hl">` around every case-insensitive occurrence of `query`.
 * @param {string} text
 * @param {string} query
 * @returns {DocumentFragment}
 */
export function highlightText(text, query) {
  const f = document.createDocumentFragment();
  for (const p of splitByQuery(String(text == null ? '' : text), query)) {
    f.appendChild(p.hit ? h('mark.hl', p.v) : textNode(p.v));
  }
  return f;
}

/**
 * One-line excerpt of at most `max` code points, centred on the first match of `query`.
 * @param {string} text
 * @param {string} query
 * @param {number} [max=100]
 * @returns {string}
 */
export function makeSnippet(text, query, max = 100) {
  const s = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
  if (Array.from(s).length <= max) return s;
  const q = String(query || '').trim().toLowerCase();
  const lower = s.toLowerCase();
  const at = q && lower.length === s.length ? lower.indexOf(q) : -1;
  const window = Math.max(1, max - 2);
  let start = at < 0 ? 0 : Math.max(0, at - Math.floor(window / 3));
  let end = Math.min(s.length, start + window);
  start = Math.max(0, end - window);
  const isLow = (i) => i > 0 && i < s.length && s.charCodeAt(i) >= 0xdc00 && s.charCodeAt(i) <= 0xdfff;
  if (isLow(start)) start += 1;
  if (isLow(end)) end -= 1;
  return (start > 0 ? '…' : '') + s.slice(start, end).trim() + (end < s.length ? '…' : '');
}

/**
 * Number of emoji clusters when `text` consists only of emoji and spaces (at most 30 clusters), else 0.
 * @param {string} text
 * @returns {number}
 */
export function emojiOnlyCount(text) {
  const s = String(text == null ? '' : text).trim();
  if (!emojiOnlyRe || !emojiClusterRe || s === '' || s.length > 120 || !emojiOnlyRe.test(s)) return 0;
  const n = (s.match(emojiClusterRe) || []).length;
  return n > 30 ? 0 : n;
}

/* ------------------------------------------------------------------------------------------ */
/* Rendering                                                                                  */
/* ------------------------------------------------------------------------------------------ */

/**
 * @param {string} v
 * @param {string} query
 * @returns {Node[]}
 */
function textNodes(v, query) {
  if (!query) return [textNode(v)];
  return splitByQuery(v, query).map((p) => (p.hit ? h('mark.hl', p.v) : textNode(p.v)));
}

/**
 * @param {Inline[]} nodes
 * @param {string} query
 * @returns {Node[]}
 */
function renderInline(nodes, query) {
  /** @type {Node[]} */
  const out = [];
  for (const n of nodes) {
    switch (n.t) {
      case 'text':
        out.push(...textNodes(n.v, query));
        break;
      case 'b':
        out.push(h('strong', renderInline(n.c, query)));
        break;
      case 'i':
        out.push(h('em', renderInline(n.c, query)));
        break;
      case 's':
        out.push(h('s', renderInline(n.c, query)));
        break;
      case 'mono':
        out.push(h('code.rt-mono', textNodes(n.v, query)));
        break;
      case 'link':
        out.push(h('a.rt-link', { href: n.url, target: '_blank', rel: 'noopener noreferrer' }, textNodes(n.v, query)));
        break;
      case 'mention':
        out.push(h(`span.mention${n.self ? '.self' : ''}`, { dataset: { userId: n.user_id } }, `@${n.v}`));
        break;
      default:
        break;
    }
  }
  return out;
}

/**
 * Render a message body (SPEC 9.3) as DOM nodes.
 * @param {string} text
 * @param {{highlight?: string, resolveMention?: (username: string) => ({user_id: number, name: string, self: boolean}|null)}} [opts]
 * @returns {DocumentFragment}
 */
export function renderRichText(text, opts = {}) {
  const query = opts.highlight ? String(opts.highlight).trim() : '';
  const f = document.createDocumentFragment();
  for (const b of parseRichText(text, opts.resolveMention)) {
    if (b.type === 'p') {
      for (const n of renderInline(b.inline, query)) f.appendChild(n);
    } else if (b.type === 'quote') {
      f.appendChild(h('blockquote.rt-quote', { dir: 'auto' }, renderInline(b.inline, query)));
    } else if (b.type === 'code') {
      f.appendChild(h('pre.rt-code', { dir: 'ltr' }, h('code', textNodes(b.text, query))));
    } else {
      const items = b.items.map((it) => h('li', { dir: 'auto' }, renderInline(it, query)));
      f.appendChild(b.type === 'ul' ? h('ul.rt-list', items) : h('ol.rt-list', { start: b.start && b.start !== 1 ? b.start : null }, items));
    }
  }
  return f;
}
