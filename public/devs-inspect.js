(function() {
  // HMR resilience — keep Vite's HMR socket from forcing a full page reload
  // on transient cross-site drops. Runs first (before @vite/client connects).
  // See HMR_WS_RESILIENCE_SNIPPET above for the full rationale.
  (function() {
    try {
      var Native = window.WebSocket;
      if (!Native || Native.__devsaiHmrWrapped) return;

      function isViteHmrProtocol(protocols) {
        // Vite ALWAYS opens its HMR socket with the 'vite-hmr' subprotocol
        // (new WebSocket(url, 'vite-hmr')). This is the definitive signal:
        // version-independent and impossible for an app socket to collide
        // with by accident. It's checked first for exactly that reason.
        if (protocols === 'vite-hmr') return true;
        if (Array.isArray(protocols) && protocols.indexOf('vite-hmr') !== -1) return true;
        return false;
      }

      function isViteHmrUrl(url) {
        try {
          var u = new URL(String(url), window.location.href);
          var isWs = u.protocol === 'ws:' || u.protocol === 'wss:';
          // Fallback signal for environments that drop the subprotocol:
          // Vite's HMR socket connects to the SAME host as the page and
          // carries a ?token= query (Vite >= 5.1). Any other socket (the
          // previewed app's own WebSockets) is passed straight through to
          // the native constructor, untouched.
          return isWs && u.host === window.location.host && u.searchParams.has('token');
        } catch (e) {
          return false;
        }
      }

      function postHmrState(state) {
        // Tell the parent App Builder panel whether Vite's HMR socket is live.
        // Best-effort and same-shape as the other devs:* preview messages.
        // Skips when not framed (window.parent === window) so a standalone
        // preview never posts to itself.
        try {
          if (window.parent && window.parent !== window &&
              typeof window.parent.postMessage === 'function') {
            window.parent.postMessage({ type: 'devs:hmr', state: state }, '*');
          }
        } catch (e) {}
      }

      function ReconnectingWebSocket(url, protocols) {
        // Non-HMR sockets: return a real native WebSocket. A constructor
        // that returns an object overrides 'this', so callers get the
        // genuine instance with no behavioral change.
        if (!isViteHmrProtocol(protocols) && !isViteHmrUrl(url)) {
          return protocols === undefined ? new Native(url) : new Native(url, protocols);
        }

        var self = this;
        var listeners = { open: [], message: [], close: [], error: [] };
        var closedByCaller = false;
        var backoff = 500;
        var inner = null;

        self.url = String(url);
        self.binaryType = 'blob';
        self.readyState = Native.CONNECTING;
        self.onopen = null;
        self.onmessage = null;
        self.onclose = null;
        self.onerror = null;

        self.addEventListener = function(type, cb) {
          if (!listeners[type]) listeners[type] = [];
          if (typeof cb === 'function') listeners[type].push(cb);
        };
        self.removeEventListener = function(type, cb) {
          var arr = listeners[type];
          if (!arr) return;
          var i = arr.indexOf(cb);
          if (i !== -1) arr.splice(i, 1);
        };
        function emit(type, ev) {
          var on = self['on' + type];
          if (typeof on === 'function') { try { on.call(self, ev); } catch (e) {} }
          var arr = listeners[type] || [];
          for (var i = 0; i < arr.length; i++) { try { arr[i].call(self, ev); } catch (e) {} }
        }

        function scheduleReconnect() {
          var delay = Math.min(backoff, 5000);
          backoff = Math.min(backoff * 2, 5000);
          window.setTimeout(function() { if (!closedByCaller) connect(); }, delay);
        }

        function connect() {
          try {
            inner = protocols === undefined ? new Native(self.url) : new Native(self.url, protocols);
          } catch (e) {
            self.readyState = Native.CONNECTING;
            scheduleReconnect();
            return;
          }
          self.__inner = inner;
          try { inner.binaryType = self.binaryType; } catch (e) {}
          inner.addEventListener('open', function(ev) {
            backoff = 500;
            self.readyState = Native.OPEN;
            postHmrState('open');
            emit('open', ev);
          });
          inner.addEventListener('message', function(ev) { emit('message', ev); });
          inner.addEventListener('error', function() { /* swallow: handled on close */ });
          inner.addEventListener('close', function(ev) {
            if (closedByCaller) {
              self.readyState = Native.CLOSED;
              emit('close', ev);
              return;
            }
            // Transient drop: reconnect quietly and do NOT emit 'close'.
            // Emitting 'close' is exactly what makes Vite reload the page.
            self.readyState = Native.CONNECTING;
            postHmrState('dropped');
            scheduleReconnect();
          });
        }

        self.send = function(data) {
          try { if (inner && inner.readyState === Native.OPEN) inner.send(data); } catch (e) {}
        };
        self.close = function(code, reason) {
          closedByCaller = true;
          try { if (inner) inner.close(code, reason); } catch (e) {}
        };

        connect();
      }

      ReconnectingWebSocket.CONNECTING = Native.CONNECTING;
      ReconnectingWebSocket.OPEN = Native.OPEN;
      ReconnectingWebSocket.CLOSING = Native.CLOSING;
      ReconnectingWebSocket.CLOSED = Native.CLOSED;
      ReconnectingWebSocket.__devsaiHmrWrapped = true;

      window.WebSocket = ReconnectingWebSocket;
    } catch (e) {
      // Never let the resilience shim break the previewed app.
    }
  })();

  var INSPECT_SCRIPT_VERSION = '2026-08-14.preview-location-v5';
  var inspectEnabled = false;
  var deckEditEnabled = false;
  var deckEditingEl = null;
  var deckEditingOriginal = '';
  var deckEditingLocator = null;
  var highlightEl = null;

  function getElementClassName(el) {
    if (!el) return '';
    var attrClass = typeof el.getAttribute === 'function' ? el.getAttribute('class') : null;
    if (typeof attrClass === 'string') return attrClass;
    if (typeof el.className === 'string') return el.className;
    if (el.className && typeof el.className.baseVal === 'string') return el.className.baseVal;
    return '';
  }

  // CSS identifiers carry colons, brackets, slashes, hash and other
  // characters that are syntactically meaningful in selectors. Tailwind
  // is the most common offender -- variant prefixes ("md:px-4",
  // "hover:bg-blue-500", "data-[state=open]:opacity-100"), arbitrary
  // values ("bg-[#abc]"), and fractional sizes ("w-1/2") all emit
  // class names that, naively concatenated as ".name", produce an
  // invalid selector. document.querySelector then either throws or
  // returns null, and the inline-comment locate RPC reports the
  // element as missing -- the "cannot locate element" the user hits
  // on every button. CSS.escape exists in every browser the sandbox
  // targets; fall back to a manual regex only as a safety net.
  function cssEscape(value) {
    if (typeof window.CSS !== 'undefined' && typeof window.CSS.escape === 'function') {
      return window.CSS.escape(value);
    }
    return String(value).replace(/[!"#$%&'()*+,./:;<=>?@[\]^`{|}~]/g, '\\$&');
  }

  function getElementClasses(el) {
    var raw = getElementClassName(el);
    if (!raw) return [];
    return raw.trim().split(/\s+/).filter(function(c) {
      // Drop classes that change with interaction state (hover/focus
      // pseudo-classes baked into Tailwind variants). The class is
      // present at click time but may be absent the next time we
      // re-query (or vice versa), so it makes selectors non-portable
      // across paint frames.
      return c && !c.startsWith('hover') && !c.startsWith('focus');
    });
  }

  function buildNodeSelector(el) {
    // Per-element selector piece: tag + (optional) class set + (optional)
    // :nth-of-type, chosen to be UNIQUE among the element's siblings.
    // The previous implementation took the first class-bearing ancestor
    // as the terminal selector and never disambiguated -- the moment
    // the user clicked any element that shared a class set with one or
    // more siblings (the dominant case in any Tailwind/utility-CSS
    // app), document.querySelector() would return the first match and
    // their comment would silently anchor to the wrong element. We
    // always check sibling uniqueness here so the leaf piece pins
    // exactly the element the user clicked.
    var tag = el.tagName.toLowerCase();
    var classes = getElementClasses(el);
    var classSel = classes.length > 0 ? '.' + classes.map(cssEscape).join('.') : '';
    var parent = el.parentElement;
    if (!parent) return tag + classSel;

    // Among siblings of the SAME tag (the universe :nth-of-type
    // filters across), find how many share this element's class set.
    var sameTag = [];
    for (var i = 0; i < parent.children.length; i += 1) {
      if (parent.children[i].tagName === el.tagName) {
        sameTag.push(parent.children[i]);
      }
    }
    if (sameTag.length === 1) {
      // Only child of its tag; tag (with or without classes) is enough.
      return tag + classSel;
    }
    if (classSel) {
      // If the class set already singles us out among same-tag
      // siblings, skip the noise of :nth-of-type.
      var withSameClasses = sameTag.filter(function(c) {
        var cClasses = getElementClasses(c);
        for (var j = 0; j < classes.length; j += 1) {
          if (cClasses.indexOf(classes[j]) === -1) return false;
        }
        return true;
      });
      if (withSameClasses.length === 1 && withSameClasses[0] === el) {
        return tag + classSel;
      }
    }
    // Fall through: nth-of-type indexed against same-tag siblings.
    var idx = sameTag.indexOf(el) + 1;
    return tag + classSel + ':nth-of-type(' + idx + ')';
  }

  function isUniqueMatch(selector, el) {
    if (!selector) return false;
    try {
      var matches = document.querySelectorAll(selector);
      return matches.length === 1 && matches[0] === el;
    } catch (_) {
      return false;
    }
  }

  function quoteAttrValue(v) {
    // JSON.stringify produces a double-quoted JS string literal with
    // backslash + double-quote escaping baked in -- the same lexical
    // form CSS attribute selectors accept inside [attr="..."], so we
    // get safe quoting for special characters (colons, brackets,
    // unicode, embedded quotes) for free.
    return JSON.stringify(String(v));
  }

  // Prefer "intentionally stable" identifiers when building selectors:
  // explicit ids, designer-curated test ids, and accessibility labels.
  // These survive React re-renders, prop changes, and minor markup
  // edits in a way class-based selectors don't (Tailwind variants
  // regenerate on every breakpoint, and "btn-primary" looks the same
  // on five different buttons in a row). Each candidate is gated by
  // isUniqueMatch -- a non-unique data-testid or shared aria-label is
  // no better than a class set for our purposes.
  function getStableAnchor(el) {
    if (el.id) {
      var idSel = '#' + cssEscape(el.id);
      if (isUniqueMatch(idSel, el)) return idSel;
    }
    var STABLE_ATTRS = ['data-devs-edit', 'data-testid', 'data-test-id', 'data-test', 'data-cy', 'data-qa', 'data-id'];
    for (var i = 0; i < STABLE_ATTRS.length; i += 1) {
      var attr = STABLE_ATTRS[i];
      var val = el.getAttribute(attr);
      if (!val) continue;
      var sel = '[' + attr + '=' + quoteAttrValue(val) + ']';
      if (isUniqueMatch(sel, el)) return sel;
    }
    // aria-label gets a tag prefix -- the bare label "Edit" or
    // "Delete" lights up on every icon button in a typical list, so
    // it's rarely unique on its own. Combining with tag narrows it
    // enough for the common case (e.g. <button aria-label="Save">)
    // without being so strict it never matches.
    var ariaLabel = el.getAttribute('aria-label');
    if (ariaLabel) {
      var tag = el.tagName.toLowerCase();
      var labelSel = tag + '[aria-label=' + quoteAttrValue(ariaLabel) + ']';
      if (isUniqueMatch(labelSel, el)) return labelSel;
    }
    // Form controls keyed by their submission name -- usually unique
    // within a form, and the most stable identifier a developer can
    // give a field without committing to a data-testid convention.
    var nameAttr = el.getAttribute('name');
    if (nameAttr) {
      var t = el.tagName;
      if (t === 'INPUT' || t === 'SELECT' || t === 'TEXTAREA' || t === 'BUTTON') {
        var nameSel = t.toLowerCase() + '[name=' + quoteAttrValue(nameAttr) + ']';
        if (isUniqueMatch(nameSel, el)) return nameSel;
      }
    }
    return null;
  }

  function buildFullStructuralPath(el) {
    // Last-resort selector: tag + :nth-of-type for every ancestor up
    // to the document root, no classes at all. Guaranteed unique by
    // construction (every level pins exactly one child within its
    // parent), at the cost of being verbose and fragile to layout
    // edits. We only fall back here when the class-aware path failed
    // its uniqueness check -- typically because the user's app rewrote
    // classes between click and persistence.
    var parts = [];
    var current = el;
    while (current && current.nodeType === 1 && current !== document.documentElement) {
      var tag = current.tagName.toLowerCase();
      var parent = current.parentElement;
      if (!parent) {
        parts.unshift(tag);
        break;
      }
      var sameTag = [];
      for (var i = 0; i < parent.children.length; i += 1) {
        if (parent.children[i].tagName === current.tagName) {
          sameTag.push(parent.children[i]);
        }
      }
      if (sameTag.length === 1) {
        parts.unshift(tag);
      } else {
        parts.unshift(tag + ':nth-of-type(' + (sameTag.indexOf(current) + 1) + ')');
      }
      current = parent;
    }
    return parts.join(' > ');
  }

  function getSelector(el) {
    if (!el || el.nodeType !== 1) return '';
    // Strategy 1: a stable anchor on the clicked element itself wins
    // outright -- shortest selector AND robust to class churn.
    var leafAnchor = getStableAnchor(el);
    if (leafAnchor) return leafAnchor;
    // Strategy 2: walk up. At each level, see whether the ancestor
    // carries a stable anchor that uniquely resolves back to the
    // clicked element when combined with the path we've collected so
    // far. If so, we lock in there -- DOM edits above that anchor
    // can't break the selector. Otherwise we extend the class-aware
    // path one more level and keep climbing.
    var path = [];
    var current = el;
    while (current && current.nodeType === 1 && current !== document.documentElement) {
      if (current !== el) {
        var ancestor = getStableAnchor(current);
        if (ancestor) {
          var candidate = path.length ? ancestor + ' > ' + path.join(' > ') : ancestor;
          if (isUniqueMatch(candidate, el)) return candidate;
          // Anchor was unique in isolation but the descendant chain
          // we've built so far didn't disambiguate enough children
          // beneath it (rare; usually means buildNodeSelector hit a
          // shape its sibling logic couldn't model). Fall through and
          // keep climbing so we can either find a stricter anchor or
          // ultimately fall to the structural path.
        }
      }
      path.unshift(buildNodeSelector(current));
      current = current.parentElement;
    }
    var selector = path.join(' > ');
    // Strategy 3: defensive verification + structural fallback. If
    // anything about strategies 1-2 didn't pin the element exactly
    // once (shadow DOM, ::slotted content, mutation between click and
    // persistence, very repetitive markup), use the brute-force
    // :nth-of-type-only path. It's verbose but unique by construction.
    if (isUniqueMatch(selector, el)) return selector;
    return buildFullStructuralPath(el);
  }

  function getAncestorPath(el, depth) {
    var parts = [];
    var current = el;
    for (var i = 0; i < (depth || 3); i++) {
      if (!current || current === document.documentElement) break;
      parts.unshift(current.tagName.toLowerCase());
      current = current.parentElement;
    }
    return parts.join(' > ');
  }

  // ---- Agent browse helpers (devs:browse-*) --------------------------------
  // The accessible-ish name for an interactive element, used both to label it
  // for the agent and to match it by description on a click/type.
  function browseAccessibleName(el) {
    if (!el || !el.getAttribute) return '';
    var aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    var labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      var lblEl = document.getElementById(labelledby);
      if (lblEl) return (lblEl.innerText || lblEl.textContent || '').trim();
    }
    if (el.id) {
      var escId = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
      var forLabel = null;
      try { forLabel = document.querySelector('label[for="' + escId + '"]'); } catch (_) { forLabel = null; }
      if (forLabel) return (forLabel.innerText || forLabel.textContent || '').trim();
    }
    if (el.closest) {
      var wrap = el.closest('label');
      if (wrap) return (wrap.innerText || wrap.textContent || '').trim();
    }
    var ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    var title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    var txt = (el.innerText || el.textContent || '').trim();
    if (txt) return txt;
    return (el.value || el.name || '').trim();
  }

  function browseIsVisible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.type === 'hidden') return false;
    var r = el.getBoundingClientRect();
    return (r.width > 0 || r.height > 0);
  }

  var BROWSE_INTERACTIVE_SELECTOR =
    'input,textarea,select,button,a[href],[role="button"],[role="link"],[role="textbox"],[role="checkbox"],[role="combobox"],[contenteditable="true"],[onclick],summary';

  // Monotonic counter for the stable refs we stamp onto elements (below).
  var browseRefCounter = 0;

  // Snapshot of the interactive elements on the page so the agent can target
  // them precisely. Generated CSS selectors round-trip unreliably (hashed
  // CSS-in-JS classes, structural paths that shift on re-render), so we stamp a
  // STABLE data-devs-ref attribute on each element and return that as the
  // selector -- [data-devs-ref="N"] always resolves back to the same node.
  function browseInteractiveElements(limit) {
    var nodes;
    try { nodes = Array.prototype.slice.call(document.querySelectorAll(BROWSE_INTERACTIVE_SELECTOR)); }
    catch (_) { nodes = []; }
    var out = [];
    for (var i = 0; i < nodes.length && out.length < limit; i++) {
      var el = nodes[i];
      if (!browseIsVisible(el)) continue;
      var tag = el.tagName.toLowerCase();
      var ref = el.getAttribute('data-devs-ref');
      if (!ref) {
        ref = 'r' + (browseRefCounter++);
        try { el.setAttribute('data-devs-ref', ref); } catch (_) {}
      }
      var entry = {
        selector: '[data-devs-ref="' + ref + '"]',
        tag: tag,
        type: (el.getAttribute && el.getAttribute('type')) || (el.getAttribute && el.getAttribute('role')) || '',
        label: browseAccessibleName(el).slice(0, 80),
      };
      if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        entry.value = String(el.value == null ? '' : el.value).slice(0, 80);
      }
      out.push(entry);
    }
    return out;
  }

  // Find an element by accessible name (exact match preferred), restricted to a
  // selector set. Used to re-resolve targets FRESH on every click/type so the
  // agent isn't broken by CSS selectors / injected refs going stale when the
  // app re-renders between turns. The label/placeholder is the stable handle.
  function browseFindByName(needle, selectorSet) {
    var lower = String(needle || '').trim().toLowerCase();
    if (!lower) return null;
    var nodes;
    try { nodes = Array.prototype.slice.call(document.querySelectorAll(selectorSet)); }
    catch (_) { return null; }
    var exact = null, contains = null;
    for (var i = 0; i < nodes.length; i++) {
      if (!browseIsVisible(nodes[i])) continue;
      var name = browseAccessibleName(nodes[i]).toLowerCase();
      if (!name) continue;
      if (name === lower) { exact = nodes[i]; break; }
      // Bidirectional contains: tolerate the agent passing a slightly longer or
      // shorter description than the exact accessible name.
      if (!contains && (name.indexOf(lower) !== -1 || lower.indexOf(name) !== -1)) contains = nodes[i];
    }
    return exact || contains || null;
  }

  function browseFindClickable(needle) {
    return browseFindByName(needle, BROWSE_INTERACTIVE_SELECTOR);
  }

  var BROWSE_FIELD_SELECTOR = 'input,textarea,select,[contenteditable="true"],[role="textbox"],[role="combobox"]';
  function browseFindField(needle) {
    // 1. By accessible name (aria-label / associated <label> / placeholder).
    var el = browseFindByName(needle, BROWSE_FIELD_SELECTOR);
    if (el) return el;
    // 2. By PROXIMITY to visible label text. Many apps render a label as a
    //    plain sibling (no for=/wrapping), so the input has no accessible name.
    //    Find the element whose own text matches, then the nearest field around
    //    it -- this mirrors how a person reads a label and types in the adjacent
    //    box.
    return browseFindFieldByProximity(needle);
  }

  function browseFindFieldByProximity(needle) {
    var lower = String(needle || '').trim().toLowerCase();
    if (!lower) return null;
    var textNodes;
    try { textNodes = Array.prototype.slice.call(document.querySelectorAll('label,span,div,p,h1,h2,h3,h4,h5,td,th,legend,dt')); }
    catch (_) { return null; }
    var labelEl = null;
    for (var i = 0; i < textNodes.length; i++) {
      var node = textNodes[i];
      if (!browseIsVisible(node)) continue;
      // own text: ignore nested form-control text so we match the label itself
      var own = (node.textContent || '').replace(/s+/g, ' ').trim().toLowerCase();
      if (!own || own.length > 60) continue;
      if (own === lower || own.indexOf(lower) !== -1 || lower.indexOf(own) !== -1) { labelEl = node; break; }
    }
    if (!labelEl) return null;
    // Search the label, then climb ancestors looking for the closest field.
    var scope = labelEl;
    for (var d = 0; d < 4 && scope; d++) {
      var field = null;
      try { field = scope.querySelector(BROWSE_FIELD_SELECTOR); } catch (_) { field = null; }
      if (field && browseIsVisible(field)) return field;
      // also check the immediate next sibling subtree
      var sib = scope.nextElementSibling;
      if (sib) {
        if (sib.matches && sib.matches(BROWSE_FIELD_SELECTOR) && browseIsVisible(sib)) return sib;
        try { field = sib.querySelector(BROWSE_FIELD_SELECTOR); } catch (_) { field = null; }
        if (field && browseIsVisible(field)) return field;
      }
      scope = scope.parentElement;
    }
    return null;
  }

  // Resolve a target element from an action payload. Prefers the stable
  // accessible-name (description) because selectors die across turns; falls
  // back to the CSS selector (live querySelector), then treats the selector
  // string itself as an accessible name.
  function browseResolveTarget(data, byNameFn) {
    var el = null;
    if (typeof data.description === 'string' && data.description) {
      el = byNameFn(data.description);
      if (el) return el;
    }
    if (typeof data.selector === 'string' && data.selector) {
      try { el = document.querySelector(data.selector); } catch (_) { el = null; }
      if (el) return el;
      el = byNameFn(data.selector);
      if (el) return el;
    }
    return null;
  }

  // --- Agent virtual cursor ("My preview": let the user watch the AI act) ---
  // A fixed, non-interactive pointer + label overlay drawn on top of the live
  // preview. The agent's click/type handlers glide it to the target and pulse on
  // click so the interaction is visible. Idle-fades after a few seconds.
  var DEVS_CURSOR_ID = 'devs-agent-cursor';
  var devsCursorIdleTimer = null;
  function browseEnsureCursor() {
    var existing = document.getElementById(DEVS_CURSOR_ID);
    if (existing) return existing;
    var wrap = document.createElement('div');
    wrap.id = DEVS_CURSOR_ID;
    wrap.setAttribute('aria-hidden', 'true');
    wrap.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483646;pointer-events:none;opacity:0;transform:translate(-120px,-120px);transition:transform 0.45s cubic-bezier(0.22,1,0.36,1),opacity 0.5s ease;will-change:transform,opacity;';
    var arrow = document.createElement('div');
    arrow.style.cssText = 'position:absolute;left:0;top:0;';
    arrow.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,0.45));"><path d="M5 3l14 7-6 1.6-2.4 6.4L5 3z" fill="#6d4aff" stroke="#ffffff" stroke-width="1.3" stroke-linejoin="round"/></svg>';
    var label = document.createElement('div');
    label.setAttribute('data-devs-cursor-label', '1');
    label.style.cssText = 'position:absolute;left:20px;top:18px;white-space:nowrap;background:#6d4aff;color:#ffffff;font:600 11px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;padding:2px 8px;border-radius:10px;box-shadow:0 1px 5px rgba(0,0,0,0.3);';
    label.textContent = 'AI testing';
    wrap.appendChild(arrow);
    wrap.appendChild(label);
    (document.body || document.documentElement).appendChild(wrap);
    return wrap;
  }
  function browseCursorPoint(el) {
    var r = el.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  }
  function browseShowCursor(el, labelText) {
    if (!el || !el.getBoundingClientRect) return;
    var c = browseEnsureCursor();
    var lbl = c.querySelector('[data-devs-cursor-label]');
    if (lbl && labelText) { lbl.textContent = labelText; }
    var p = browseCursorPoint(el);
    c.style.opacity = '1';
    c.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px)';
    if (devsCursorIdleTimer) { clearTimeout(devsCursorIdleTimer); }
    devsCursorIdleTimer = setTimeout(function() {
      var cc = document.getElementById(DEVS_CURSOR_ID);
      if (cc) { cc.style.opacity = '0'; }
    }, 4000);
  }
  function browseCursorPulse(el) {
    if (!el || !el.getBoundingClientRect) return;
    var p = browseCursorPoint(el);
    var ring = document.createElement('div');
    ring.setAttribute('aria-hidden', 'true');
    ring.style.cssText = 'position:fixed;left:' + p.x + 'px;top:' + p.y + 'px;z-index:2147483645;pointer-events:none;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;background:rgba(109,74,255,0.4);transition:transform 0.4s ease-out,opacity 0.4s ease-out;';
    (document.body || document.documentElement).appendChild(ring);
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(function() { ring.style.transform = 'scale(3.4)'; ring.style.opacity = '0'; });
    } else {
      ring.style.transform = 'scale(3.4)'; ring.style.opacity = '0';
    }
    setTimeout(function() { if (ring.parentNode) { ring.parentNode.removeChild(ring); } }, 460);
  }

  // --- Parent-origin handshake (browse security) -------------------------
  // Resolve the embedding AppBuilder origin WITHOUT trusting message content,
  // so browse commands forged by the previewed app itself (whose messages
  // carry the preview's own origin) can be rejected, and replies (which can
  // include DOM text/field values) are only sent to the real parent.
  var _devsParentOrigin;
  var _devsParentOriginResolved = false;
  function devsParentOrigin() {
    if (_devsParentOriginResolved) return _devsParentOrigin;
    _devsParentOriginResolved = true;
    _devsParentOrigin = null;
    try {
      if (window.location.ancestorOrigins && window.location.ancestorOrigins.length > 0) {
        _devsParentOrigin = window.location.ancestorOrigins[0];
        return _devsParentOrigin;
      }
    } catch (_) {}
    try {
      if (document.referrer) { _devsParentOrigin = new URL(document.referrer).origin; }
    } catch (_) {}
    return _devsParentOrigin;
  }
  // True when an inbound message did NOT come from the parent AppBuilder origin
  // (used to drop forged browse commands). When the parent origin can't be
  // resolved we fail open rather than break browsing.
  function devsIsForeignOrigin(e) {
    var po = devsParentOrigin();
    return !!po && e.origin !== po;
  }
  // Post a browse reply to the parent, pinned to its origin when known.
  function browseReply(payload) {
    window.parent.postMessage(payload, devsParentOrigin() || '*');
  }

  function getRect(el) {
    var r = el.getBoundingClientRect();
    return { top: r.top, left: r.left, width: r.width, height: r.height };
  }

  // The preview iframe is cross-origin from AppBuilder, so the parent
  // cannot read location. Report path+search+hash so the address bar
  // can stay in sync with in-app (History API) navigation.
  var lastReportedPath = null;
  var suppressNavReportUntil = 0;
  function currentLocationPath() {
    return window.location.pathname + window.location.search + window.location.hash;
  }
  function reportLocation() {
    try {
      if (window.parent === window) return;
      var path = currentLocationPath();
      lastReportedPath = path;
      window.parent.postMessage({
        type: 'devs:location',
        path: path
      }, '*');
    } catch (_) { /* ignore */ }
  }
  function reportLocationIfChanged() {
    try {
      if (currentLocationPath() === lastReportedPath) return;
      reportLocation();
    } catch (_) { /* ignore */ }
  }

  function getSnippet(el) {
    var clone = el.cloneNode(true);
    // Truncate deep children
    var children = clone.querySelectorAll('*');
    for (var i = 0; i < children.length; i++) {
      if (children[i].children.length > 0) {
        children[i].innerHTML = '...';
      }
    }
    var html = clone.outerHTML;
    return html.length > 500 ? html.slice(0, 500) + '...' : html;
  }

  // Returns true when the event target is one of the in-iframe
  // inline-comment marker bubbles (or any descendant). Used to bail
  // out of inspect/hover handlers so a reviewer's mouse over their
  // own pin doesn't grab the bubble as the inspect target.
  function isInsideInlineMarker(el) {
    if (!el || typeof el.closest !== 'function') return false;
    return !!el.closest('#__devs_inline_marker_root');
  }

  function onHover(e) {
    if (!inspectEnabled) return;
    var el = e.target;
    if (!el || el === document.body || el === document.documentElement) return;
    if (isInsideInlineMarker(el)) return;
    window.parent.postMessage({
      type: 'devs:hover',
      payload: {
        rect: getRect(el),
        tagName: el.tagName.toLowerCase(),
        selector: getSelector(el)
      }
    }, '*');
  }

  function onHoverOut() {
    if (!inspectEnabled) return;
    window.parent.postMessage({ type: 'devs:hover-out' }, '*');
  }

  var _h2cPromise = null;
  // html2canvas-pro is a maintained fork that supports modern CSS
  // color functions (oklch / lab / lch / color()). The original
  // html2canvas@1.4.1 throws "Attempting to parse an unsupported
  // color function 'oklch'" on Tailwind v4 apps and any app using
  // modern color spaces, which is the majority of generated apps.
  // The fork is a drop-in replacement: same global window.html2canvas
  // callable, same options surface.
  var H2C_URL = 'https://cdn.jsdelivr.net/npm/html2canvas-pro@2.0.2/dist/html2canvas-pro.min.js';
  // SHA-256 of the pinned html2canvas-pro@2.0.2 bundle. Used by
  // verifySha256 below as a defense-in-depth integrity check: if
  // jsdelivr or the upstream npm tarball is ever tampered with, the
  // fetched JS will hash to a different value and the script will
  // refuse to eval(). Derive locally by running:
  //   curl -sSL <H2C_URL> | shasum -a 256
  // (or sha256sum on Linux). Bump alongside H2C_URL when pinning
  // a new version.
  var H2C_SHA256 = '1c86f97c0617828870a1c24cca3cd614eae1034aec412aea0fe3a0764bc4597e';
  function verifySha256(text, expectedHex) {
    if (!window.crypto || !window.crypto.subtle || !window.TextEncoder) {
      return Promise.resolve(false);
    }
    return window.crypto.subtle.digest('SHA-256', new window.TextEncoder().encode(text))
      .then(function(buffer) {
        var actualHex = Array.from(new Uint8Array(buffer)).map(function(b) {
          return b.toString(16).padStart(2, '0');
        }).join('');
        return actualHex === expectedHex;
      })
      .catch(function() {
        return false;
      });
  }
  function loadHtml2Canvas() {
    if (_h2cPromise) return _h2cPromise;
    // Hard timeout protects against silent stalls inside the dynamic
    // <script> tag (e.g. a sandbox CSP that disallows blob: sources
    // would prevent both onload and onerror from ever firing). Without
    // this cap, captureThumbnail() can never respond to the parent
    // and the parent's 15s capture timeout is the only signal that
    // something went wrong.
    var H2C_LOAD_TIMEOUT_MS = 10000;
    var loadPromise = fetch(H2C_URL)
      .then(function(r) {
        if (!r.ok) throw new Error('Failed to fetch html2canvas');
        return r.text();
      })
      .then(function(code) {
        return verifySha256(code, H2C_SHA256).then(function(valid) {
          if (!valid) throw new Error('html2canvas hash mismatch');
          return code;
        });
      })
      .then(function(code) {
        var blob = new Blob([code], { type: 'application/javascript' });
        var url = URL.createObjectURL(blob);
        var s = document.createElement('script');
        s.src = url;
        return new Promise(function(resolve) {
          s.onload = function() { URL.revokeObjectURL(url); resolve(window.html2canvas || null); };
          s.onerror = function() { URL.revokeObjectURL(url); resolve(null); };
          document.head.appendChild(s);
        });
      });
    var timeoutPromise = new Promise(function(resolve) {
      setTimeout(function() { resolve('timeout'); }, H2C_LOAD_TIMEOUT_MS);
    });
    _h2cPromise = Promise.race([loadPromise, timeoutPromise])
      .then(function(h2c) {
        if (h2c === 'timeout' || !h2c) {
          // Reset so a future capture can attempt the load again
          // (transient CDN issue, slow cold start, etc.).
          _h2cPromise = null;
          return null;
        }
        return h2c;
      })
      .catch(function() {
        _h2cPromise = null;
        return null;
      });
    return _h2cPromise;
  }

  function sendScreenshot(dataUrl, selectionToken) {
    window.parent.postMessage({ type: 'devs:select-screenshot', dataUrl: dataUrl, selectionToken: selectionToken }, '*');
  }

  function hasVisibleBackground(color) {
    if (!color || color === 'transparent') return false;
    var normalized = color.replace(/\s+/g, '').toLowerCase();
    if (normalized === 'rgba(0,0,0,0)') return false;
    var rgbaMatch = normalized.match(/^rgba\((\d+),(\d+),(\d+),([^)]+)\)$/);
    if (!rgbaMatch) return true;
    return parseFloat(rgbaMatch[4]) > 0;
  }

  function getCaptureBackgroundColor(el) {
    var current = el;
    while (current && current !== document.documentElement) {
      var bg = window.getComputedStyle(current).backgroundColor;
      if (hasVisibleBackground(bg)) return bg;
      current = current.parentElement;
    }
    var bodyBg = window.getComputedStyle(document.body).backgroundColor;
    return hasVisibleBackground(bodyBg) ? bodyBg : '#ffffff';
  }

  function parseRgbColor(color) {
    if (!color) return null;
    var normalized = color.replace(/\s+/g, '').toLowerCase();
    var match = normalized.match(/^rgba?\((\d+),(\d+),(\d+)(?:,([^)]+))?\)$/);
    if (!match) return null;
    return {
      r: parseInt(match[1], 10),
      g: parseInt(match[2], 10),
      b: parseInt(match[3], 10)
    };
  }

  function channelToLuminance(channel) {
    var scaled = channel / 255;
    return scaled <= 0.03928 ? scaled / 12.92 : Math.pow((scaled + 0.055) / 1.055, 2.4);
  }

  function getContrastRatio(colorA, colorB) {
    var lumA = 0.2126 * channelToLuminance(colorA.r) + 0.7152 * channelToLuminance(colorA.g) + 0.0722 * channelToLuminance(colorA.b);
    var lumB = 0.2126 * channelToLuminance(colorB.r) + 0.7152 * channelToLuminance(colorB.g) + 0.0722 * channelToLuminance(colorB.b);
    var lighter = Math.max(lumA, lumB);
    var darker = Math.min(lumA, lumB);
    return (lighter + 0.05) / (darker + 0.05);
  }

  function ensureReadableBackgroundColor(el, backgroundColor) {
    var textColor = parseRgbColor(window.getComputedStyle(el).color);
    var backgroundRgb = parseRgbColor(backgroundColor);
    if (!textColor || !backgroundRgb) return backgroundColor;
    if (getContrastRatio(textColor, backgroundRgb) >= 4.5) return backgroundColor;
    var white = { r: 255, g: 255, b: 255 };
    var black = { r: 0, g: 0, b: 0 };
    return getContrastRatio(textColor, white) >= getContrastRatio(textColor, black) ? '#ffffff' : '#000000';
  }

  function exportCanvasWithBackground(sourceCanvas, backgroundColor) {
    var exportCanvas = document.createElement('canvas');
    exportCanvas.width = sourceCanvas.width;
    exportCanvas.height = sourceCanvas.height;
    var ctx = exportCanvas.getContext('2d');
    if (!ctx) return sourceCanvas.toDataURL('image/jpeg', 0.7);
    ctx.fillStyle = backgroundColor;
    ctx.fillRect(0, 0, exportCanvas.width, exportCanvas.height);
    ctx.drawImage(sourceCanvas, 0, 0);
    return exportCanvas.toDataURL('image/jpeg', 0.7);
  }

  function svgFallback(el, selectionToken) {
    var rect = el.getBoundingClientRect();
    var w = Math.ceil(rect.width);
    var h = Math.ceil(rect.height);
    if (w === 0 || h === 0) return;
    var maxDim = 300;
    var scale = Math.min(1, maxDim / Math.max(w, h));
    var backgroundColor = ensureReadableBackgroundColor(el, getCaptureBackgroundColor(el));

    var clone = el.cloneNode(true);
    (function inlineStyles(src, dst) {
      var cs = window.getComputedStyle(src);
      var t = '';
      for (var i = 0; i < cs.length; i++) { var p = cs[i]; t += p + ':' + cs.getPropertyValue(p) + ';'; }
      dst.setAttribute('style', t);
      for (var j = 0; j < src.children.length && j < dst.children.length; j++) {
        inlineStyles(src.children[j], dst.children[j]);
      }
    })(el, clone);
    clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');

    var html = new XMLSerializer().serializeToString(clone);
    var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '">' +
      '<foreignObject width="100%" height="100%">' + html + '</foreignObject></svg>';

    var img = new Image();
    img.onload = function() {
      try {
        var canvas = document.createElement('canvas');
        canvas.width = Math.round(w * scale);
        canvas.height = Math.round(h * scale);
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = backgroundColor;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        sendScreenshot(canvas.toDataURL('image/jpeg', 0.7), selectionToken);
      } catch(e) { /* tainted canvas — give up */ }
    };
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  }

  function captureElement(el, selectionToken) {
    loadHtml2Canvas().then(function(h2c) {
      if (!h2c) { svgFallback(el, selectionToken); return; }
      var rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) {
        svgFallback(el, selectionToken);
        return;
      }
      var padding = 8;
      var captureWidth = Math.ceil(rect.width + padding * 2);
      var captureHeight = Math.ceil(rect.height + padding * 2);
      var captureX = Math.max(0, Math.floor(rect.left + window.scrollX - padding));
      var captureY = Math.max(0, Math.floor(rect.top + window.scrollY - padding));
      var maxDim = 300;
      var scale = Math.min(1, maxDim / Math.max(captureWidth || 1, captureHeight || 1));
      var backgroundColor = ensureReadableBackgroundColor(el, getCaptureBackgroundColor(el));
      h2c(document.body, {
        scale: scale,
        useCORS: true,
        logging: false,
        backgroundColor: null,
        removeContainer: true,
        x: captureX,
        y: captureY,
        width: captureWidth,
        height: captureHeight
      }).then(function(canvas) {
        sendScreenshot(exportCanvasWithBackground(canvas, backgroundColor), selectionToken);
      }).catch(function() { svgFallback(el, selectionToken); });
    }).catch(function() { svgFallback(el, selectionToken); });
  }

  // App card thumbnails are now captured server-side by the Temporal
  // appBuilderThumbnailCaptureWorkflow (Playwright + MCP in a per-chat
  // sandbox). The iframe used to expose a devs:capture-thumbnail
  // handler here that called loadHtml2Canvas and shipped a PNG
  // dataURL back via devs:thumbnail — both have been removed. The
  // remaining html2canvas usage powers captureElement /
  // devs:select-screenshot, the click-to-attach-screenshot inspect
  // feature, which is unrelated.
  var _selectionCounter = 0;
  function nextSelectionToken() {
    _selectionCounter += 1;
    return 'inspect-' + _selectionCounter;
  }

  function onClick(e) {
    // Deck edit mode owns clicks (and contenteditable needs the caret).
    if (deckEditEnabled) return;
    if (!inspectEnabled) return;
    var el = e.target;
    if (!el || el === document.body || el === document.documentElement) return;
    // Inline-comment marker bubbles handle their own clicks via their
    // bound listener; we MUST NOT preventDefault here or that listener
    // never fires (and we'd treat the bubble as the inspect target,
    // grabbing its selector instead of the element underneath).
    if (isInsideInlineMarker(el)) return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    var textContent = (el.textContent || '').trim();
    var selectionToken = nextSelectionToken();
    window.parent.postMessage({
      type: 'devs:select',
      payload: {
        selectionToken: selectionToken,
        selector: getSelector(el),
        tagName: el.tagName.toLowerCase(),
        className: getElementClassName(el),
        id: el.id || '',
        textContent: textContent.length > 100 ? textContent.slice(0, 100) + '...' : textContent,
        htmlSnippet: getSnippet(el),
        rect: getRect(el),
        ancestorPath: getAncestorPath(el, 3)
      }
    }, '*');
    captureElement(el, selectionToken);
  }

  function onPreviewNavClick(e) {
    if (deckEditEnabled || inspectEnabled) return;
    if (Date.now() < suppressNavReportUntil) {
      setTimeout(reportLocationIfChanged, 0);
      return;
    }
    var el = e.target;
    if (el && el.nodeType === 3) el = el.parentElement;
    if (!el || typeof el.closest !== 'function') return;
    if (el.closest('input,textarea,select,option,[contenteditable="true"]')) return;
    if (isInsideInlineMarker(el)) return;

    var href = null;
    var labels = [];
    var seen = {};
    var node = el;
    for (var depth = 0; depth < 8 && node && node !== document.body && node !== document.documentElement; depth++) {
      if (!href && node.getAttribute) {
        var h = node.getAttribute('href') || node.getAttribute('data-href');
        if (h) href = h;
      }
      if (node.getAttribute) {
        var aria = node.getAttribute('aria-label') || node.getAttribute('title');
        if (aria) {
          aria = String(aria).replace(/s+/g, ' ').trim();
          if (aria && aria.length <= 48 && !seen[aria.toLowerCase()]) {
            seen[aria.toLowerCase()] = true;
            labels.push(aria);
          }
        }
      }
      var text = (node.textContent || '').replace(/s+/g, ' ').trim();
      if (text && text.length > 0 && text.length <= 48 && !seen[text.toLowerCase()]) {
        seen[text.toLowerCase()] = true;
        labels.push(text);
      }
      node = node.parentElement;
    }
    if (!href && labels.length === 0) return;
    try {
      window.parent.postMessage({
        type: 'devs:preview-click',
        href: href,
        label: labels[0] || '',
        labels: labels
      }, '*');
    } catch (_) { /* ignore */ }
    setTimeout(reportLocationIfChanged, 0);
    setTimeout(reportLocationIfChanged, 50);
    setTimeout(reportLocationIfChanged, 250);
  }

  // -------------------------------------------------------------
  // Presentation deck editor (contenteditable + rich toolbar + drag).
  // Parent enables via devs:enable-deck-edit.
  // - Hover shows editable outlines + drag handle
  // - Click focuses a block; floating toolbar: B / I / U / Link
  // - Drag handle reorders among sibling blocks in the slide
  // - Blur / Enter commits; Escape cancels
  // -------------------------------------------------------------
  var DECK_EDITABLE_TAGS = {
    H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1,
    P: 1, LI: 1, BLOCKQUOTE: 1, SPAN: 1, DIV: 1, A: 1, LABEL: 1
  };
  var deckToolbarEl = null;
  var deckColorPanelEl = null;
  var deckColorPanelOpen = false;
  var deckCommitGeneration = 0;
  var deckBlurTimer = null;
  var deckDragCleanup = null;
  var deckHintEl = null;
  var deckStyleEl = null;
  var deckDragEl = null;
  var deckDragFrom = -1;
  var deckPendingReorder = null;
  var deckDragGhost = null;
  var deckDragOffsetX = 0;
  var deckDragOffsetY = 0;
  var deckSuppressBlur = false;
  var deckSavedSelectionRange = null;
  // target:hex key — avoid double-wrap when native picker fires input then change
  var deckLastCustomColorCommit = null;
  var deckEditingOriginalHtml = '';
  var deckEditingOriginalHtmlBackup = '';

  function isDeckLeafEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.getAttribute && el.getAttribute('data-devs-edit')) return true;
    var tag = el.tagName;
    if (!DECK_EDITABLE_TAGS[tag]) return false;
    if (tag === 'DIV' || tag === 'SPAN') {
      if (el.querySelector('h1,h2,h3,h4,h5,h6,p,li,blockquote,div')) return false;
    }
    // Allow inline rich children (bold/italic/links) but not nested blocks.
    if (el.querySelector('h1,h2,h3,h4,h5,h6,p,li,blockquote,ul,ol,section,div')) return false;
    var t = (el.textContent || '').trim();
    return !!(t && t.length <= 2000);
  }

  function findDeckEditableTarget(el) {
    var cur = el;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      if (isDeckLeafEditable(cur)) return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  function getRevealSlideIndices(el) {
    try {
      if (window.Reveal && typeof window.Reveal.getIndices === 'function') {
        var idx = window.Reveal.getIndices(el);
        if (idx && typeof idx.h === 'number') {
          return { h: idx.h, v: typeof idx.v === 'number' ? idx.v : 0 };
        }
      }
    } catch (_) {}
    var slidesRoot = document.querySelector('.reveal .slides') || document.querySelector('.slides');
    var section = el.closest ? el.closest('section') : null;
    if (!slidesRoot || !section) return { h: 0, v: 0 };
    var parentSection = section.parentElement && section.parentElement.closest
      ? section.parentElement.closest('section')
      : null;
    if (parentSection && slidesRoot.contains(parentSection)) {
      var horizontals = [];
      for (var i = 0; i < slidesRoot.children.length; i += 1) {
        if (slidesRoot.children[i].tagName === 'SECTION') horizontals.push(slidesRoot.children[i]);
      }
      var h = Math.max(0, horizontals.indexOf(parentSection));
      var verticals = [];
      for (var j = 0; j < parentSection.children.length; j += 1) {
        if (parentSection.children[j].tagName === 'SECTION') verticals.push(parentSection.children[j]);
      }
      return { h: h, v: Math.max(0, verticals.indexOf(section)) };
    }
    var tops = [];
    for (var k = 0; k < slidesRoot.children.length; k += 1) {
      if (slidesRoot.children[k].tagName === 'SECTION') tops.push(slidesRoot.children[k]);
    }
    var top = section;
    while (top.parentElement && top.parentElement !== slidesRoot && top.parentElement.tagName === 'SECTION') {
      top = top.parentElement;
    }
    return { h: Math.max(0, tops.indexOf(top)), v: 0 };
  }

  function ensureDeckEditId(el) {
    var indices = getRevealSlideIndices(el);
    var tagName = el.tagName.toLowerCase();
    var section = el.closest ? el.closest('section') : null;
    var tagIndex = 0;
    if (section) {
      var nodes = section.querySelectorAll(tagName);
      tagIndex = Math.max(0, Array.prototype.indexOf.call(nodes, el));
    }
    var existing = el.getAttribute('data-devs-edit');
    if (existing) {
      return {
        editId: existing,
        stamped: false,
        slideH: indices.h,
        slideV: indices.v,
        tagName: tagName,
        tagIndex: tagIndex
      };
    }
    var editId = 's' + indices.h + '-' + indices.v + '-' + tagName + '-' + tagIndex;
    if (document.querySelector('[data-devs-edit="' + editId + '"]')) {
      editId = editId + '-' + Date.now().toString(36);
    }
    try { el.setAttribute('data-devs-edit', editId); } catch (_) {}
    return {
      editId: editId,
      stamped: true,
      slideH: indices.h,
      slideV: indices.v,
      tagName: tagName,
      tagIndex: tagIndex
    };
  }

  function siblingElementIndex(el) {
    var parent = el.parentElement;
    if (!parent) return -1;
    var kids = [];
    for (var i = 0; i < parent.children.length; i += 1) kids.push(parent.children[i]);
    return kids.indexOf(el);
  }

  function normalizeDeckHtml(html) {
    return String(html || '').replace(/s+/g, ' ').trim();
  }

  function ensureDeckEditorChrome() {
    if (!document.body) return;
    if (!deckStyleEl) {
      deckStyleEl = document.createElement('style');
      deckStyleEl.id = '__devs_deck_edit_style';
      deckStyleEl.textContent = [
        'body.__devs-deck-editing .reveal .slides section [data-devs-edit],',
        'body.__devs-deck-editing .reveal .slides section h1,',
        'body.__devs-deck-editing .reveal .slides section h2,',
        'body.__devs-deck-editing .reveal .slides section h3,',
        'body.__devs-deck-editing .reveal .slides section p,',
        'body.__devs-deck-editing .reveal .slides section li,',
        'body.__devs-deck-editing .reveal .slides section blockquote {',
        '  outline: 1px dashed rgba(20,184,166,0.35);',
        '  outline-offset: 4px;',
        '  border-radius: 2px;',
        '  cursor: text;',
        '  transition: outline-color 120ms ease, box-shadow 120ms ease;',
        '}',
        'body.__devs-deck-editing .reveal .slides section [data-devs-edit]:hover,',
        'body.__devs-deck-editing .__devs-deck-editable:hover {',
        '  outline: 1.5px solid rgba(20,184,166,0.7);',
        '}',
        'body.__devs-deck-editing .__devs-deck-active {',
        '  outline: 2px solid #14b8a6 !important;',
        '  outline-offset: 3px !important;',
        '  box-shadow: 0 0 0 4px rgba(20,184,166,0.15);',
        '  cursor: text !important;',
        '}',
        'body.__devs-deck-editing .__devs-deck-dragging {',
        '  opacity: 0.35;',
        '  outline: 2px dashed rgba(20,184,166,0.55) !important;',
        '  outline-offset: 2px !important;',
        '  box-shadow: none !important;',
        '  pointer-events: none !important;',
        '}',
        'body.__devs-deck-editing .__devs-deck-drop-before { box-shadow: inset 0 3px 0 #14b8a6; }',
        'body.__devs-deck-editing .__devs-deck-drop-after { box-shadow: inset 0 -3px 0 #14b8a6; }',
        'body.__devs-deck-editing.__devs-deck-reordering { cursor: grabbing !important; }',
        'body.__devs-deck-editing.__devs-deck-reordering .__devs_deck_handle { opacity: 0; pointer-events: none; }',
        '.__devs-deck-ghost {',
        '  position: fixed; z-index: 2147483646; pointer-events: none; margin: 0;',
        '  opacity: 0.92; transform: rotate(1.25deg) scale(1.02);',
        '  box-shadow: 0 18px 40px rgba(15,23,42,0.28), 0 0 0 1px rgba(20,184,166,0.35);',
        '  border-radius: 4px; background: rgba(255,255,255,0.96);',
        '  max-width: min(92vw, 720px); overflow: hidden;',
        '}',
        'body.__devs-deck-editing .__devs-deck-flip {',
        '  transition: transform 160ms cubic-bezier(0.2, 0.8, 0.2, 1);',
        '  will-change: transform;',
        '}',
        '#__devs_deck_toolbar {',
        '  position: fixed; z-index: 2147483647; display: none;',
        '  align-items: center; gap: 4px; padding: 6px 8px;',
        '  background: rgba(15,23,42,0.96); color: #f8fafc; border-radius: 12px;',
        '  border: 1px solid rgba(148,163,184,0.22);',
        '  backdrop-filter: blur(10px);',
        '  box-shadow: 0 12px 40px rgba(2,6,23,0.45);',
        '  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;',
        '  font-size: 12px; line-height: 1; user-select: none;',
        '}',
        '#__devs_deck_toolbar button {',
        '  appearance: none; border: 0; background: transparent; color: #e2e8f0;',
        '  min-width: 28px; height: 28px; padding: 0 8px; border-radius: 8px; cursor: pointer;',
        '  font-weight: 600; font-size: 12px; display: inline-flex; align-items: center; justify-content: center;',
        '}',
        '#__devs_deck_toolbar button:hover { background: rgba(148,163,184,0.16); color: #fff; }',
        '#__devs_deck_toolbar button.__on:not(.__swatch) { background: rgba(20,184,166,0.28); color: #99f6e4; }',
        '#__devs_deck_toolbar .__sep { width: 1px; height: 16px; background: rgba(148,163,184,0.28); margin: 0 2px; }',
        '#__devs_deck_toolbar .__size {',
        '  display: inline-flex; align-items: center; gap: 2px; padding: 2px;',
        '  background: rgba(148,163,184,0.12); border-radius: 8px;',
        '}',
        '#__devs_deck_toolbar .__size button { min-width: 26px; height: 24px; padding: 0 6px; font-size: 11px; }',
        '#__devs_deck_toolbar .__picker-btn {',
        '  gap: 6px; padding: 0 10px; min-width: auto; font-weight: 600;',
        '}',
        '#__devs_deck_toolbar .__picker-btn .__preview {',
        '  width: 14px; height: 14px; border-radius: 4px;',
        '  border: 1px solid rgba(255,255,255,0.35); box-sizing: border-box; flex-shrink: 0;',
        '}',
        '#__devs_deck_toolbar .__picker-btn .__preview.__text-preview {',
        '  display: inline-flex; align-items: center; justify-content: center;',
        '  font-size: 10px; font-weight: 800; line-height: 1; background: #1e293b;',
        '}',
        '#__devs_deck_toolbar .__picker-btn .__preview.__hl-preview {',
        '  width: 12px; height: 12px; border-radius: 3px;',
        '}',
        '#__devs_deck_toolbar .__picker-btn .__chev { opacity: 0.65; font-size: 9px; }',
        '#__devs_deck_toolbar .__picker-btn.__open { background: rgba(148,163,184,0.22); }',
        '#__devs_deck_toolbar .__link { padding: 0 10px; letter-spacing: 0.01em; }',
        '#__devs_deck_color_panel {',
        '  position: absolute; left: 0; top: calc(100% + 8px); z-index: 2;',
        '  display: none; width: 228px; padding: 10px;',
        '  background: rgba(15,23,42,0.98); color: #f8fafc; border-radius: 12px;',
        '  border: 1px solid rgba(148,163,184,0.25);',
        '  box-shadow: 0 16px 40px rgba(2,6,23,0.55);',
        '}',
        '#__devs_deck_color_panel.__open { display: block; }',
        '#__devs_deck_color_panel.__above { top: auto; bottom: calc(100% + 8px); }',
        '#__devs_deck_color_panel .__section + .__section { margin-top: 10px; padding-top: 10px; border-top: 1px solid rgba(148,163,184,0.18); }',
        '#__devs_deck_color_panel .__label {',
        '  display: block; margin: 0 0 8px; font-size: 11px; font-weight: 600;',
        '  color: #94a3b8; letter-spacing: 0.02em; text-transform: uppercase;',
        '}',
        '#__devs_deck_color_panel .__grid {',
        '  display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px;',
        '}',
        '#__devs_deck_color_panel .__swatch {',
        '  width: 100%; aspect-ratio: 1; min-width: 0; height: auto; padding: 0;',
        '  border-radius: 8px; border: 1px solid rgba(255,255,255,0.2);',
        '  box-sizing: border-box; cursor: pointer;',
        '}',
        '#__devs_deck_color_panel .__swatch:hover { transform: scale(1.06); }',
        '#__devs_deck_color_panel .__swatch.__on {',
        '  outline: 2px solid #99f6e4; outline-offset: 1px;',
        '}',
        '#__devs_deck_color_panel .__swatch.__text {',
        '  font-size: 12px; font-weight: 800; background: #1e293b;',
        '}',
        '#__devs_deck_color_panel .__swatch.__clear {',
        '  background: repeating-linear-gradient(135deg,#334155,#334155 4px,#1e293b 4px,#1e293b 8px);',
        '  color: #e2e8f0; font-size: 11px; font-weight: 700;',
        '}',
        '#__devs_deck_color_panel .__custom-row {',
        '  display: flex; align-items: center; gap: 8px; margin-top: 8px;',
        '}',
        '#__devs_deck_color_panel .__custom-row label {',
        '  flex: 1; font-size: 11px; color: #cbd5e1; font-weight: 500;',
        '}',
        '#__devs_deck_color_panel input[type="color"] {',
        '  appearance: none; -webkit-appearance: none; border: 0; padding: 0;',
        '  width: 28px; height: 28px; border-radius: 8px; cursor: pointer;',
        '  background: transparent; overflow: hidden;',
        '  box-shadow: inset 0 0 0 1px rgba(148,163,184,0.35);',
        '}',
        '#__devs_deck_color_panel input[type="color"]::-webkit-color-swatch-wrapper { padding: 0; }',
        '#__devs_deck_color_panel input[type="color"]::-webkit-color-swatch {',
        '  border: 0; border-radius: 8px;',
        '}',
        '#__devs_deck_color_panel input[type="color"]::-moz-color-swatch {',
        '  border: 0; border-radius: 8px;',
        '}',
        '#__devs_deck_hint {',
        '  position: fixed; left: 50%; transform: translateX(-50%); bottom: 18px;',
        '  z-index: 2147483647; display: none; padding: 8px 14px; border-radius: 999px;',
        '  background: rgba(15,23,42,0.92); color: #e2e8f0; font-size: 12px;',
        '  font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;',
        '  box-shadow: 0 8px 24px rgba(15,23,42,0.3); pointer-events: none;',
        '}',
        '.__devs_deck_handle {',
        '  position: fixed; width: 18px; height: 28px;',
        '  border-radius: 6px; background: #0f172a; color: #94a3b8; cursor: grab;',
        '  display: none; align-items: center; justify-content: center;',
        '  font-size: 12px; line-height: 1; z-index: 2147483645; user-select: none;',
        '  box-shadow: 0 4px 12px rgba(15,23,42,0.25);',
        '}',
        'body.__devs-deck-editing .__devs_deck_handle { display: flex; }',
        '.__devs_deck_handle:active { cursor: grabbing; }'
      ].join('\n');
      document.documentElement.appendChild(deckStyleEl);
    }
    if (!deckToolbarEl) {
      deckToolbarEl = document.createElement('div');
      deckToolbarEl.id = '__devs_deck_toolbar';
      deckToolbarEl.setAttribute('role', 'toolbar');
      deckToolbarEl.setAttribute('aria-label', 'Text formatting');
      deckToolbarEl.innerHTML = [
        '<button type="button" data-cmd="bold" title="Bold (\u2318B)"><b>B</b></button>',
        '<button type="button" data-cmd="italic" title="Italic (\u2318I)"><i>I</i></button>',
        '<button type="button" data-cmd="underline" title="Underline (\u2318U)"><span style="text-decoration:underline">U</span></button>',
        '<span class="__sep"></span>',
        '<span class="__size" role="group" aria-label="Text size">',
        '<button type="button" data-cmd="size-sm" title="Smaller text" style="font-size:10px">A</button>',
        '<button type="button" data-cmd="size-md" title="Default text" style="font-size:12px">A</button>',
        '<button type="button" data-cmd="size-lg" title="Larger text" style="font-size:15px">A</button>',
        '</span>',
        '<span class="__sep"></span>',
        '<button type="button" class="__picker-btn" data-action="toggle-color-panel" title="Text color & highlight" aria-haspopup="true" aria-expanded="false">',
        '<span class="__preview __text-preview" data-role="color-preview">A</span>',
        '<span class="__preview __hl-preview" data-role="highlight-preview"></span>',
        '<span>Color</span>',
        '<span class="__chev">\u25BE</span>',
        '</button>',
        '<span class="__sep"></span>',
        '<button type="button" class="__link" data-cmd="createLink" title="Add link">Link</button>',
        '<button type="button" class="__link" data-cmd="unlink" title="Remove link">Unlink</button>',
        '<div id="__devs_deck_color_panel" role="dialog" aria-label="Color and highlight">',
        '<div class="__section">',
        '<span class="__label">Text color</span>',
        '<div class="__grid">',
        '<button type="button" class="__swatch __clear" data-cmd="color:default" title="Default">\u00D7</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#ffffff" title="White" style="color:#ffffff">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#94a3b8" title="Slate" style="color:#94a3b8">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#ef4444" title="Red" style="color:#ef4444">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#f97316" title="Orange" style="color:#f97316">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#eab308" title="Amber" style="color:#eab308">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#22c55e" title="Green" style="color:#22c55e">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#14b8a6" title="Teal" style="color:#14b8a6">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#3b82f6" title="Blue" style="color:#3b82f6">A</button>',
        '<button type="button" class="__swatch __text" data-cmd="color:#8b5cf6" title="Violet" style="color:#8b5cf6">A</button>',
        '</div>',
        '<div class="__custom-row">',
        '<label for="__devs_deck_text_color">Custom</label>',
        '<input type="color" id="__devs_deck_text_color" data-color-target="text" value="#3b82f6" title="Custom text color" />',
        '</div>',
        '</div>',
        '<div class="__section">',
        '<span class="__label">Highlight</span>',
        '<div class="__grid">',
        '<button type="button" class="__swatch __clear" data-cmd="highlight:none" title="No highlight">\u00D7</button>',
        '<button type="button" class="__swatch" data-cmd="highlight:#fef08a" title="Yellow" style="background:#fef08a"></button>',
        '<button type="button" class="__swatch" data-cmd="highlight:#bbf7d0" title="Green" style="background:#bbf7d0"></button>',
        '<button type="button" class="__swatch" data-cmd="highlight:#bfdbfe" title="Blue" style="background:#bfdbfe"></button>',
        '<button type="button" class="__swatch" data-cmd="highlight:#fbcfe8" title="Pink" style="background:#fbcfe8"></button>',
        '<button type="button" class="__swatch" data-cmd="highlight:#fed7aa" title="Orange" style="background:#fed7aa"></button>',
        '</div>',
        '<div class="__custom-row">',
        '<label for="__devs_deck_highlight_color">Custom</label>',
        '<input type="color" id="__devs_deck_highlight_color" data-color-target="highlight" value="#fef08a" title="Custom highlight" />',
        '</div>',
        '</div>',
        '</div>'
      ].join('');
      // Keep CSS position:fixed — do not set inline position (that hid the toolbar).
      // Fixed already creates a containing block for the absolute color panel.
      deckColorPanelEl = deckToolbarEl.querySelector('#__devs_deck_color_panel');
      function isDeckColorPickerControl(el) {
        // Clicks on label text hit a Text node — walk up to an Element first.
        var node = el;
        while (node && node.nodeType !== 1) node = node.parentNode;
        if (!node || node.nodeType !== 1) return false;
        if (node.tagName === 'INPUT' && node.type === 'color' && node.getAttribute('data-color-target')) {
          return true;
        }
        var label = node.tagName === 'LABEL' ? node : (node.closest ? node.closest('label') : null);
        if (!label) return false;
        var forId = label.getAttribute('for');
        if (!forId) return false;
        var input = document.getElementById(forId);
        return !!(input && input.tagName === 'INPUT' && input.type === 'color' && input.getAttribute('data-color-target'));
      }
      function snapshotDeckSelectionForColorPicker() {
        try {
          var sel = window.getSelection();
          // Only replace a previously saved range when the current selection is
          // still non-empty (still inside the editor). After the native picker
          // steals focus, getSelection() is often empty — keep the mousedown snapshot.
          if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
            deckSavedSelectionRange = sel.getRangeAt(0).cloneRange();
          } else if (!deckSavedSelectionRange) {
            deckSavedSelectionRange = null;
          }
        } catch (_) {
          if (!deckSavedSelectionRange) deckSavedSelectionRange = null;
        }
        // New picker session — allow one commit for this selection.
        deckLastCustomColorCommit = null;
      }
      deckToolbarEl.addEventListener('mousedown', function(e) {
        // Allow native color inputs (and their labels) to open; still suppress
        // blur for other controls. Snapshot selection before focus moves.
        if (isDeckColorPickerControl(e.target)) {
          snapshotDeckSelectionForColorPicker();
        } else {
          e.preventDefault();
        }
        deckSuppressBlur = true;
      });
      // Keyboard / non-mouse open paths never fire mousedown on the input first.
      // Always run snapshot: non-empty selection refreshes the range + clears the
      // prior commit key; empty selection (native picker stole focus) keeps the
      // existing mousedown/prior range instead of wiping it.
      deckToolbarEl.addEventListener('focusin', function(e) {
        var input = e.target && e.target.closest
          ? e.target.closest('input[type="color"][data-color-target]')
          : null;
        if (!input) return;
        snapshotDeckSelectionForColorPicker();
        deckSuppressBlur = true;
      });
      deckToolbarEl.addEventListener('focusout', function(e) {
        var leaving = e.target && e.target.closest
          ? e.target.closest('input[type="color"][data-color-target]')
          : null;
        if (!leaving) return;
        var next = e.relatedTarget;
        if (next && isDeckColorPickerControl(next)) return;
        // Picker closed without change (Esc / click-away) — re-enable blur commits.
        setTimeout(function() { deckSuppressBlur = false; }, 0);
      });
      function restoreDeckSavedSelection() {
        if (!deckSavedSelectionRange) return false;
        try {
          if (deckEditingEl) deckEditingEl.focus();
          var sel = window.getSelection();
          if (!sel) return false;
          sel.removeAllRanges();
          sel.addRange(deckSavedSelectionRange);
          return true;
        } catch (_) {
          return false;
        }
      }
      function previewCustomColorInput(input) {
        if (!input || !deckToolbarEl) return;
        var hex = String(input.value || '').toLowerCase();
        if (!/^#[0-9a-f]{6}$/.test(hex)) return;
        var target = input.getAttribute('data-color-target');
        if (target === 'highlight') {
          var hlPreview = deckToolbarEl.querySelector('[data-role="highlight-preview"]');
          if (hlPreview) {
            hlPreview.style.background = hex;
            hlPreview.style.borderStyle = 'solid';
          }
        } else {
          var colorPreview = deckToolbarEl.querySelector('[data-role="color-preview"]');
          if (colorPreview) colorPreview.style.color = hex;
        }
      }
      function applyCustomColorInput(input) {
        if (!input || !deckEditingEl) return;
        var hex = String(input.value || '').toLowerCase();
        if (!/^#[0-9a-f]{6}$/.test(hex)) return;
        var target = input.getAttribute('data-color-target') || 'text';
        var commitKey = target + ':' + hex;
        // Native pickers fire input while dragging then change on close — only
        // commit once so we don't nest identical <span style> wrappers.
        if (deckLastCustomColorCommit === commitKey) return;
        restoreDeckSavedSelection();
        if (target === 'highlight') runDeckFormatCommand('highlight:' + hex);
        else runDeckFormatCommand('color:' + hex);
        deckLastCustomColorCommit = commitKey;
        refreshDeckToolbarState();
      }
      deckToolbarEl.addEventListener('input', function(e) {
        var input = e.target && e.target.closest ? e.target.closest('input[type="color"][data-color-target]') : null;
        if (!input) return;
        // Preview only — do not mutate the DOM until change.
        previewCustomColorInput(input);
      });
      deckToolbarEl.addEventListener('change', function(e) {
        var input = e.target && e.target.closest ? e.target.closest('input[type="color"][data-color-target]') : null;
        if (!input) return;
        applyCustomColorInput(input);
        setTimeout(function() { deckSuppressBlur = false; }, 0);
      });
      deckToolbarEl.addEventListener('click', function(e) {
        if (!deckEditingEl) return;
        var toggle = e.target && e.target.closest ? e.target.closest('[data-action="toggle-color-panel"]') : null;
        if (toggle) {
          e.preventDefault();
          e.stopPropagation();
          toggleDeckColorPanel();
          setTimeout(function() { deckSuppressBlur = false; }, 0);
          return;
        }
        if (isDeckColorPickerControl(e.target)) return;
        var btn = e.target && e.target.closest ? e.target.closest('button[data-cmd]') : null;
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        runDeckFormatCommand(btn.getAttribute('data-cmd'));
        refreshDeckToolbarState();
        setTimeout(function() { deckSuppressBlur = false; }, 0);
      });
      document.body.appendChild(deckToolbarEl);
    }
    if (!deckHintEl) {
      deckHintEl = document.createElement('div');
      deckHintEl.id = '__devs_deck_hint';
      deckHintEl.textContent = 'Edit mode — click text to edit · drag ⋮⋮ to reorder · Esc cancels';
      document.body.appendChild(deckHintEl);
    }
  }

  function setDeckEditModeUi(on) {
    ensureDeckEditorChrome();
    try { document.body.classList.toggle('__devs-deck-editing', !!on); } catch (_) {}
    if (deckHintEl) deckHintEl.style.display = on ? 'block' : 'none';
    if (!on) {
      hideDeckToolbar();
      clearDeckDropIndicators();
      // Commit in-progress typing (don't discard like Escape).
      if (deckEditingEl) commitDeckEdit(deckEditingEl, false);
      // Tear down an in-flight drag so mouseup can't post after disable.
      if (typeof deckDragCleanup === 'function') {
        try { deckDragCleanup(); } catch (_) {}
        deckDragCleanup = null;
      }
      if (deckDragGhost && deckDragGhost.parentNode) {
        try { deckDragGhost.parentNode.removeChild(deckDragGhost); } catch (_) {}
      }
      deckDragGhost = null;
      try { document.body.classList.remove('__devs-deck-reordering'); } catch (_) {}
      deckDragEl = null;
      deckDragFrom = -1;
      deckPendingReorder = null;
      var handles = document.querySelectorAll('.__devs_deck_handle');
      for (var i = 0; i < handles.length; i += 1) handles[i].style.display = 'none';
    } else {
      stampVisibleSlideEditables();
    }
  }

  function stampVisibleSlideEditables() {
    var section = document.querySelector('.reveal .slides section.present') ||
      document.querySelector('.reveal .slides section');
    if (!section) return;
    var candidates = section.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,blockquote,[data-devs-edit]');
    for (var i = 0; i < candidates.length; i += 1) {
      var el = candidates[i];
      if (!isDeckLeafEditable(el) && !el.getAttribute('data-devs-edit')) continue;
      ensureDeckEditId(el);
      el.classList.add('__devs-deck-editable');
      attachDeckDragHandle(el);
    }
  }

  function attachDeckDragHandle(el) {
    if (!el || el.__devsDragHandle) {
      if (el && typeof el.__devsPlaceHandle === 'function') el.__devsPlaceHandle();
      return;
    }
    var handle = document.createElement('div');
    handle.className = '__devs_deck_handle';
    handle.textContent = '⋮⋮';
    handle.title = 'Drag to reorder';
    handle.setAttribute('contenteditable', 'false');
    handle.addEventListener('mousedown', function(e) {
      e.preventDefault();
      e.stopPropagation();
      beginDeckDrag(el, e);
    });
    el.__devsDragHandle = handle;
    function place() {
      if (!deckEditEnabled || !el.isConnected) {
        handle.style.display = 'none';
        return;
      }
      var r = el.getBoundingClientRect();
      handle.style.left = Math.max(4, r.left - 22) + 'px';
      handle.style.top = (r.top + Math.max(0, (r.height - 28) / 2)) + 'px';
      handle.style.display = 'flex';
    }
    el.__devsPlaceHandle = place;
    document.body.appendChild(handle);
    place();
  }

  function placeAllDeckHandles() {
    var nodes = document.querySelectorAll('.__devs-deck-editable, [data-devs-edit]');
    for (var i = 0; i < nodes.length; i += 1) {
      if (typeof nodes[i].__devsPlaceHandle === 'function') nodes[i].__devsPlaceHandle();
    }
  }

  function beginDeckDrag(el, startEv) {
    if (!deckEditEnabled || !el || !el.parentElement) return;
    // Drag handle mousedown blurs the active block — cancel any pending blur
    // commit so we don't double-save (blur commit + reorder commit).
    deckSuppressBlur = true;
    if (deckBlurTimer) {
      clearTimeout(deckBlurTimer);
      deckBlurTimer = null;
    }
    if (deckEditingEl && deckEditingEl !== el) commitDeckEdit(deckEditingEl, false);
    else if (deckEditingEl === el) commitDeckEdit(deckEditingEl, false);
    if (typeof deckDragCleanup === 'function') {
      try { deckDragCleanup(); } catch (_) {}
      deckDragCleanup = null;
    }
    deckDragEl = el;
    deckDragFrom = siblingElementIndex(el);
    deckPendingReorder = null;
    // Stamp + capture locator BEFORE live DOM moves so write-back injects the
    // id using the pre-move tagIndex (unstamped siblings would otherwise get
    // the wrong block after flipLiveMove).
    var dragLocator = ensureDeckEditId(el);
    var originalNext = el.nextElementSibling;
    el.classList.add('__devs-deck-dragging');
    try { document.body.classList.add('__devs-deck-reordering'); } catch (_) {}
    hideDeckToolbar();

    var startRect = el.getBoundingClientRect();
    var pointerX = startEv && typeof startEv.clientX === 'number' ? startEv.clientX : startRect.left + startRect.width / 2;
    var pointerY = startEv && typeof startEv.clientY === 'number' ? startEv.clientY : startRect.top + startRect.height / 2;
    deckDragOffsetX = pointerX - startRect.left;
    deckDragOffsetY = pointerY - startRect.top;

    try {
      deckDragGhost = el.cloneNode(true);
      deckDragGhost.classList.add('__devs-deck-ghost');
      deckDragGhost.classList.remove('__devs-deck-dragging', '__devs-deck-active', '__devs-deck-editable');
      deckDragGhost.removeAttribute('contenteditable');
      deckDragGhost.removeAttribute('data-devs-edit');
      deckDragGhost.style.width = Math.max(40, startRect.width) + 'px';
      deckDragGhost.style.left = (pointerX - deckDragOffsetX) + 'px';
      deckDragGhost.style.top = (pointerY - deckDragOffsetY) + 'px';
      document.body.appendChild(deckDragGhost);
    } catch (_) {
      deckDragGhost = null;
    }

    setTimeout(function() { deckSuppressBlur = false; }, 0);

    function moveGhost(clientX, clientY) {
      if (!deckDragGhost) return;
      deckDragGhost.style.left = (clientX - deckDragOffsetX) + 'px';
      deckDragGhost.style.top = (clientY - deckDragOffsetY) + 'px';
    }

    function clearFlipTransforms(parent) {
      if (!parent) return;
      var kids = parent.children;
      for (var i = 0; i < kids.length; i += 1) {
        kids[i].classList.remove('__devs-deck-flip');
        kids[i].style.transform = '';
        kids[i].style.transition = '';
      }
    }

    function flipLiveMove(parent, moveEl, beforeNode) {
      if (!parent || !moveEl) return;
      var kids = Array.prototype.slice.call(parent.children);
      var first = [];
      for (var i = 0; i < kids.length; i += 1) {
        first.push({ el: kids[i], rect: kids[i].getBoundingClientRect() });
      }
      if (beforeNode) parent.insertBefore(moveEl, beforeNode);
      else parent.appendChild(moveEl);
      for (var j = 0; j < first.length; j += 1) {
        var item = first[j];
        if (item.el === moveEl) continue;
        var last = item.el.getBoundingClientRect();
        var dy = item.rect.top - last.top;
        var dx = item.rect.left - last.left;
        if (!dx && !dy) continue;
        item.el.style.transition = 'none';
        item.el.style.transform = 'translate(' + dx + 'px,' + dy + 'px)';
        void item.el.offsetWidth;
        item.el.classList.add('__devs-deck-flip');
        item.el.style.transition = '';
        item.el.style.transform = '';
      }
      placeAllDeckHandles();
    }

    function liveReorderToward(clientX, clientY) {
      if (!deckDragEl || !deckDragEl.parentElement) return;
      var parent = deckDragEl.parentElement;
      // Ghost / dragged node use pointer-events:none, so hit-testing sees siblings.
      var over = document.elementFromPoint(clientX, clientY);
      var target = findDeckEditableTarget(over);
      if (!target || target === deckDragEl) return;
      if (target.parentElement !== parent) return;
      var rect = target.getBoundingClientRect();
      var before = clientY < rect.top + rect.height / 2;
      var kids = Array.prototype.slice.call(parent.children);
      var from = kids.indexOf(deckDragEl);
      var targetIdx = kids.indexOf(target);
      if (from < 0 || targetIdx < 0) return;
      var insertIdx = before ? targetIdx : targetIdx + 1;
      var to = insertIdx > from ? insertIdx - 1 : insertIdx;
      if (to === from) return;
      var beforeNode = null;
      if (to < kids.length) {
        var refIdx = to < from ? to : to + 1;
        beforeNode = kids[refIdx] || null;
        if (beforeNode === deckDragEl) beforeNode = deckDragEl.nextElementSibling;
      }
      flipLiveMove(parent, deckDragEl, beforeNode);
      deckPendingReorder = { from: deckDragFrom, to: to };
    }

    function teardownGhost() {
      if (deckDragGhost && deckDragGhost.parentNode) {
        try { deckDragGhost.parentNode.removeChild(deckDragGhost); } catch (_) {}
      }
      deckDragGhost = null;
    }

    function onMove(ev) {
      if (!deckEditEnabled || !deckDragEl) return;
      if (ev.cancelable) ev.preventDefault();
      moveGhost(ev.clientX, ev.clientY);
      liveReorderToward(ev.clientX, ev.clientY);
    }

    function finishDrag(commit) {
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mouseup', onUp, true);
      deckDragCleanup = null;
      clearDeckDropIndicators();
      teardownGhost();
      try { document.body.classList.remove('__devs-deck-reordering'); } catch (_) {}
      var parent = deckDragEl ? deckDragEl.parentElement : null;
      if (deckDragEl) {
        deckDragEl.classList.remove('__devs-deck-dragging');
        clearFlipTransforms(parent);
      }
      if (!commit || !deckEditEnabled || !deckDragEl || !parent) {
        if (deckDragEl && parent) {
          try {
            if (originalNext && originalNext.parentElement === parent) {
              parent.insertBefore(deckDragEl, originalNext);
            } else {
              parent.appendChild(deckDragEl);
            }
          } catch (_) {}
        }
        deckDragEl = null;
        deckDragFrom = -1;
        deckPendingReorder = null;
        placeAllDeckHandles();
        return;
      }

      var kids = Array.prototype.slice.call(parent.children);
      var finalIdx = kids.indexOf(deckDragEl);
      var from = deckDragFrom;
      var to = finalIdx;
      if (from >= 0 && to >= 0 && to !== from) {
        // Reuse the pre-move locator — do not recompute tagIndex after live moves.
        var locator = dragLocator || ensureDeckEditId(deckDragEl);
        var txt = (deckDragEl.textContent || '').replace(/s+/g, ' ').trim();
        var reorderHtml = deckDragEl.innerHTML;
        // Snap siblings back to the pre-drag order before the parent remounts
        // from saved Deck.tsx. Leaving the live flip DOM up lets HMR reconcile
        // over a reordered tree during the async save and corrupt markup.
        try {
          if (originalNext && originalNext.parentElement === parent) {
            parent.insertBefore(deckDragEl, originalNext);
          } else {
            parent.appendChild(deckDragEl);
          }
        } catch (_) {}
        window.parent.postMessage({
          type: 'devs:deck-edit-commit',
          payload: {
            oldText: txt,
            newText: txt,
            newHtml: reorderHtml,
            editId: locator.editId,
            slideH: locator.slideH,
            slideV: locator.slideV,
            tagName: locator.tagName,
            tagIndex: locator.tagIndex,
            reorder: { from: from, to: to }
          }
        }, '*');
      }
      deckDragEl = null;
      deckDragFrom = -1;
      deckPendingReorder = null;
      placeAllDeckHandles();
    }

    function onUp() {
      finishDrag(true);
    }

    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('mouseup', onUp, true);
    deckDragCleanup = function() {
      finishDrag(false);
    };
  }

  function clearDeckDropIndicators() {
    var nodes = document.querySelectorAll('.__devs-deck-drop-before, .__devs-deck-drop-after');
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].classList.remove('__devs-deck-drop-before');
      nodes[i].classList.remove('__devs-deck-drop-after');
    }
  }

  function showDeckToolbar(el) {
    if (!deckToolbarEl || !el) return;
    var r = el.getBoundingClientRect();
    deckToolbarEl.style.display = 'flex';
    var tw = deckToolbarEl.offsetWidth || 180;
    var left = Math.min(window.innerWidth - tw - 8, Math.max(8, r.left + r.width / 2 - tw / 2));
    var top = r.top - 44;
    if (top < 8) top = r.bottom + 8;
    deckToolbarEl.style.left = left + 'px';
    deckToolbarEl.style.top = top + 'px';
    refreshDeckToolbarState();
  }

  function setDeckColorPanelOpen(open) {
    deckColorPanelOpen = !!open;
    if (deckColorPanelEl) {
      deckColorPanelEl.classList.toggle('__open', deckColorPanelOpen);
      // Flip above the toolbar when there isn't room below.
      if (deckColorPanelOpen && deckToolbarEl) {
        var rect = deckToolbarEl.getBoundingClientRect();
        var placeAbove = rect.bottom + 280 > window.innerHeight && rect.top > 280;
        deckColorPanelEl.classList.toggle('__above', !!placeAbove);
      }
    }
    var toggle = deckToolbarEl && deckToolbarEl.querySelector('[data-action="toggle-color-panel"]');
    if (toggle) {
      toggle.classList.toggle('__open', deckColorPanelOpen);
      toggle.setAttribute('aria-expanded', deckColorPanelOpen ? 'true' : 'false');
    }
  }

  function toggleDeckColorPanel() {
    setDeckColorPanelOpen(!deckColorPanelOpen);
  }

  function hideDeckToolbar() {
    setDeckColorPanelOpen(false);
    deckSavedSelectionRange = null;
    deckLastCustomColorCommit = null;
    deckSuppressBlur = false;
    if (deckToolbarEl) deckToolbarEl.style.display = 'none';
  }

  function refreshDeckToolbarState() {
    if (!deckToolbarEl) return;
    var activeSize = detectDeckSelectionFontSize();
    var activeColor = detectDeckSelectionStyle('color') || 'default';
    var activeHighlight = detectDeckSelectionStyle('backgroundColor') || 'none';
    var buttons = deckToolbarEl.querySelectorAll('button[data-cmd]');
    for (var i = 0; i < buttons.length; i += 1) {
      var cmd = buttons[i].getAttribute('data-cmd') || '';
      var on = false;
      try {
        if (cmd === 'bold') on = !!findDeckAncestorTag(['STRONG', 'B']);
        else if (cmd === 'italic') on = !!findDeckAncestorTag(['EM', 'I']);
        else if (cmd === 'underline') on = !!findDeckAncestorTag(['U']);
        else if (cmd === 'size-sm' || cmd === 'size-md' || cmd === 'size-lg') {
          on = activeSize === cmd;
        } else if (cmd.indexOf('color:') === 0) {
          on = normalizeDeckColor(cmd.slice(6)) === normalizeDeckColor(activeColor) ||
            (cmd === 'color:default' && (!activeColor || activeColor === 'default' || activeColor === 'inherit'));
        } else if (cmd.indexOf('highlight:') === 0) {
          var hv = cmd.slice(10);
          on = (hv === 'none' && (!activeHighlight || activeHighlight === 'none' || activeHighlight === 'transparent')) ||
            normalizeDeckColor(hv) === normalizeDeckColor(activeHighlight);
        }
      } catch (_) {}
      buttons[i].classList.toggle('__on', !!on);
    }
    // Mirror current color/highlight on the compact Color button.
    var colorPreview = deckToolbarEl.querySelector('[data-role="color-preview"]');
    if (colorPreview) {
      var previewColor = (activeColor && activeColor !== 'default' && activeColor !== 'inherit')
        ? activeColor
        : '#e2e8f0';
      colorPreview.style.color = previewColor;
    }
    var hlPreview = deckToolbarEl.querySelector('[data-role="highlight-preview"]');
    if (hlPreview) {
      var previewHl = (activeHighlight && activeHighlight !== 'none' && activeHighlight !== 'transparent')
        ? activeHighlight
        : 'transparent';
      hlPreview.style.background = previewHl;
      hlPreview.style.borderStyle = previewHl === 'transparent' ? 'dashed' : 'solid';
    }
    var textColorInput = deckToolbarEl.querySelector('#__devs_deck_text_color');
    if (textColorInput) {
      var textHex = normalizeDeckColor(activeColor);
      if (/^#[0-9a-f]{6}$/.test(textHex)) textColorInput.value = textHex;
    }
    var highlightColorInput = deckToolbarEl.querySelector('#__devs_deck_highlight_color');
    if (highlightColorInput) {
      var hlHex = normalizeDeckColor(activeHighlight);
      if (/^#[0-9a-f]{6}$/.test(hlHex)) highlightColorInput.value = hlHex;
    }
  }

  function deckSizeToCss(cmd) {
    if (cmd === 'size-sm') return '0.85em';
    if (cmd === 'size-lg') return '1.35em';
    return '1em';
  }

  function normalizeDeckColor(value) {
    if (!value) return '';
    var v = String(value).trim().toLowerCase();
    if (v === 'default' || v === 'none' || v === 'inherit' || v === 'transparent') return v;
    var hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
    if (hex) {
      if (hex[1].length === 3) {
        return '#' + hex[1].split('').map(function(c) { return c + c; }).join('');
      }
      return '#' + hex[1];
    }
    var rgb = new RegExp('^rgba?\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)', 'i').exec(v);
    if (rgb) {
      return '#' + [rgb[1], rgb[2], rgb[3]].map(function(n) {
        var h = Number(n).toString(16);
        return h.length === 1 ? '0' + h : h;
      }).join('');
    }
    return v;
  }

  function detectDeckSelectionFontSize() {
    try {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return 'size-md';
      var node = sel.anchorNode;
      var el = node && node.nodeType === 3 ? node.parentElement : node;
      while (el && el !== deckEditingEl) {
        if (el.nodeType === 1) {
          var fs = (el.style && el.style.fontSize) || '';
          if (fs === '0.85em' || fs === '85%') return 'size-sm';
          if (fs === '1.35em' || fs === '135%') return 'size-lg';
          if (fs === '1em' || fs === '100%') return 'size-md';
        }
        el = el.parentElement;
      }
    } catch (_) {}
    return 'size-md';
  }

  function detectDeckSelectionStyle(prop) {
    try {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return '';
      var node = sel.anchorNode;
      var el = node && node.nodeType === 3 ? node.parentElement : node;
      while (el && el !== deckEditingEl) {
        if (el.nodeType === 1 && el.style) {
          var raw = el.style[prop] || '';
          if (raw) return normalizeDeckColor(raw);
        }
        el = el.parentElement;
      }
    } catch (_) {}
    return '';
  }

  function applyDeckInlineStyles(styles) {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return;
    try {
      var range = sel.getRangeAt(0);
      var span = document.createElement('span');
      var keys = Object.keys(styles);
      for (var i = 0; i < keys.length; i += 1) {
        var key = keys[i];
        var val = styles[key];
        if (val === '' || val === 'default' || val === 'none') continue;
        span.style[key] = val;
      }
      if (!span.getAttribute('style')) return;
      // extractContents + insertNode can leave the original text behind if the
      // range is detached/stale. Prefer surroundContents for simple ranges;
      // fall back to extract/insert only when surround fails (partial elements).
      try {
        range.surroundContents(span);
      } catch (_) {
        var extracted = range.extractContents();
        if (!extracted || (!extracted.childNodes.length && !(extracted.textContent || '').length)) {
          return;
        }
        span.appendChild(extracted);
        range.insertNode(span);
      }
      sel.removeAllRanges();
      var next = document.createRange();
      next.selectNodeContents(span);
      sel.addRange(next);
    } catch (_) {}
  }

  function clearStyleInSubtree(root, cssProperty) {
    if (!root) return;
    // Snapshot children first — unwrap moves them to the parent, so we must
    // clear nested styled spans before (or via the snapshot) unwrapping.
    var children = [];
    if (root.childNodes) {
      for (var c = 0; c < root.childNodes.length; c += 1) children.push(root.childNodes[c]);
    }
    for (var i = 0; i < children.length; i += 1) {
      clearStyleInSubtree(children[i], cssProperty);
    }
    if (root.nodeType === 1 && root.style && root.style[cssProperty]) {
      root.style[cssProperty] = '';
      var styleAttr = (root.getAttribute('style') || '').replace(/;/g, ' ').trim();
      if (!styleAttr) root.removeAttribute('style');
      if (root.tagName === 'SPAN' && root.attributes && root.attributes.length === 0) {
        unwrapDeckElement(root);
      }
    }
  }

  function clearDeckInlineStyle(cssProperty) {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed || !deckEditingEl) return;
    try {
      var range = sel.getRangeAt(0);
      // extractContents splits partially-selected styled ancestors, so only the
      // selected substring is cleared — text outside the selection keeps its style.
      var fragment = range.extractContents();
      clearStyleInSubtree(fragment, cssProperty);
      var first = fragment.firstChild;
      var last = fragment.lastChild;
      range.insertNode(fragment);
      if (first && last) {
        sel.removeAllRanges();
        var next = document.createRange();
        next.setStartBefore(first);
        next.setEndAfter(last);
        sel.addRange(next);
      }
    } catch (_) {}
  }

  function findDeckAncestorTag(tagNames) {
    try {
      var sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return null;
      var node = sel.anchorNode;
      var el = node && node.nodeType === 3 ? node.parentElement : node;
      while (el && el !== deckEditingEl) {
        if (el.nodeType === 1 && tagNames.indexOf(el.tagName) !== -1) return el;
        el = el.parentElement;
      }
    } catch (_) {}
    return null;
  }

  function unwrapDeckElement(el) {
    if (!el || !el.parentNode) return;
    var parent = el.parentNode;
    while (el.firstChild) parent.insertBefore(el.firstChild, el);
    parent.removeChild(el);
  }

  function wrapDeckSelection(tagName, attrs) {
    var sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    try {
      var range = sel.getRangeAt(0);
      var wrapper = document.createElement(tagName);
      if (attrs) {
        var keys = Object.keys(attrs);
        for (var i = 0; i < keys.length; i += 1) wrapper.setAttribute(keys[i], attrs[keys[i]]);
      }
      try {
        range.surroundContents(wrapper);
      } catch (_) {
        var extracted = range.extractContents();
        if (!extracted || (!extracted.childNodes.length && !(extracted.textContent || '').length)) {
          return null;
        }
        wrapper.appendChild(extracted);
        range.insertNode(wrapper);
      }
      sel.removeAllRanges();
      var next = document.createRange();
      next.selectNodeContents(wrapper);
      sel.addRange(next);
      return wrapper;
    } catch (_) {
      return null;
    }
  }

  function toggleDeckInlineTag(tagName, aliases) {
    var existing = findDeckAncestorTag(aliases || [tagName.toUpperCase()]);
    if (existing) {
      unwrapDeckElement(existing);
      return;
    }
    wrapDeckSelection(tagName);
  }

  function applyDeckLink() {
    var url = window.prompt('Link URL', 'https://');
    if (!url) return;
    url = url.trim();
    // Use RegExp ctor — a /...\/.../ literal inside this template string
    // collapses \/ → / and breaks parse of the whole inspect script.
    if (!new RegExp('^(https?:|mailto:|/|#)', 'i').test(url)) url = 'https://' + url;
    var existing = findDeckAncestorTag(['A']);
    if (existing) {
      existing.setAttribute('href', url);
      return;
    }
    wrapDeckSelection('a', { href: url });
  }

  function removeDeckLink() {
    var existing = findDeckAncestorTag(['A']);
    if (existing) unwrapDeckElement(existing);
  }

  function applyDeckFontSize(cmd) {
    applyDeckInlineStyles({ fontSize: deckSizeToCss(cmd) });
  }

  function runDeckFormatCommand(cmd) {
    if (!deckEditingEl) return;
    try { deckEditingEl.focus(); } catch (_) {}
    if (cmd === 'bold') {
      toggleDeckInlineTag('strong', ['STRONG', 'B']);
      return;
    }
    if (cmd === 'italic') {
      toggleDeckInlineTag('em', ['EM', 'I']);
      return;
    }
    if (cmd === 'underline') {
      toggleDeckInlineTag('u', ['U']);
      return;
    }
    if (cmd === 'createLink') {
      applyDeckLink();
      return;
    }
    if (cmd === 'unlink') {
      removeDeckLink();
      return;
    }
    if (cmd === 'size-sm' || cmd === 'size-md' || cmd === 'size-lg') {
      applyDeckFontSize(cmd);
      return;
    }
    if (cmd === 'color:default') {
      clearDeckInlineStyle('color');
      return;
    }
    if (cmd === 'highlight:none') {
      clearDeckInlineStyle('backgroundColor');
      return;
    }
    if (cmd && cmd.indexOf('color:') === 0) {
      applyDeckInlineStyles({ color: cmd.slice(6) });
      return;
    }
    if (cmd && cmd.indexOf('highlight:') === 0) {
      applyDeckInlineStyles({ backgroundColor: cmd.slice(10) });
      return;
    }
  }

  function commitDeckEdit(el, cancelled) {
    if (!el) return;
    if (deckBlurTimer) {
      clearTimeout(deckBlurTimer);
      deckBlurTimer = null;
    }
    deckCommitGeneration += 1;
    var oldText = deckEditingOriginal;
    var oldHtml = deckEditingOriginalHtml;
    var newText = (el.textContent || '').replace(/s+/g, ' ').trim();
    var newHtml = el.innerHTML || '';
    var locator = deckEditingLocator || ensureDeckEditId(el);
    var htmlBackup = deckEditingOriginalHtmlBackup;
    try {
      el.removeAttribute('contenteditable');
      el.classList.remove('__devs-deck-active');
    } catch (_) {}
    hideDeckToolbar();
    deckEditingEl = null;
    deckEditingOriginal = '';
    deckEditingOriginalHtml = '';
    deckEditingOriginalHtmlBackup = '';
    deckEditingLocator = null;
    if (cancelled) {
      try {
        if (typeof htmlBackup === 'string') el.innerHTML = htmlBackup;
        else el.textContent = oldText;
      } catch (_) {}
      window.parent.postMessage({ type: 'devs:deck-edit-cancel' }, '*');
      placeAllDeckHandles();
      return;
    }
    var textSame = newText === oldText;
    var htmlSame = normalizeDeckHtml(newHtml) === normalizeDeckHtml(oldHtml);
    if (textSame && htmlSame) {
      window.parent.postMessage({ type: 'devs:deck-edit-cancel' }, '*');
      placeAllDeckHandles();
      return;
    }
    // Keep the edited markup visible until the parent remounts from the saved
    // Deck.tsx. Reverting here showed stale text during the async save and let
    // a follow-up edit commit against the pre-edit baseline. Remount (not HMR)
    // is what prevents contenteditable/React duplication.
    window.parent.postMessage({
      type: 'devs:deck-edit-commit',
      payload: {
        oldText: oldText,
        newText: newText,
        newHtml: newHtml,
        editId: locator.editId || el.getAttribute('data-devs-edit') || '',
        slideH: locator.slideH,
        slideV: locator.slideV,
        tagName: locator.tagName || el.tagName.toLowerCase(),
        tagIndex: locator.tagIndex,
        selector: getSelector(el)
      }
    }, '*');
    placeAllDeckHandles();
  }

  function beginDeckEdit(el) {
    if (!el || deckEditingEl === el) return;
    if (deckEditingEl) commitDeckEdit(deckEditingEl, false);
    deckEditingEl = el;
    deckEditingOriginal = (el.textContent || '').replace(/s+/g, ' ').trim();
    deckEditingOriginalHtml = el.innerHTML || '';
    deckEditingOriginalHtmlBackup = el.innerHTML || '';
    deckEditingLocator = ensureDeckEditId(el);
    try {
      el.setAttribute('contenteditable', 'true');
      el.classList.add('__devs-deck-active');
      el.focus();
    } catch (_) {}
    showDeckToolbar(el);
    window.parent.postMessage({
      type: 'devs:deck-edit-start',
      payload: {
        editId: deckEditingLocator.editId,
        oldText: deckEditingOriginal,
        tagName: deckEditingLocator.tagName,
        slideH: deckEditingLocator.slideH,
        slideV: deckEditingLocator.slideV,
        tagIndex: deckEditingLocator.tagIndex
      }
    }, '*');
  }

  function onDeckClick(e) {
    if (!deckEditEnabled) return;
    if (inspectEnabled) return;
    if (e.target && e.target.closest && e.target.closest('#__devs_deck_toolbar, .__devs_deck_handle')) return;
    if (deckColorPanelOpen) setDeckColorPanelOpen(false);
    var el = findDeckEditableTarget(e.target);
    if (!el) {
      if (deckEditingEl) commitDeckEdit(deckEditingEl, false);
      return;
    }
    if (isInsideInlineMarker(el)) return;
    e.preventDefault();
    e.stopPropagation();
    beginDeckEdit(el);
  }

  function onDeckDblClick(e) {
    if (!deckEditEnabled) return;
    if (findDeckEditableTarget(e.target)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }

  function onDeckKeyDown(e) {
    if (!deckEditEnabled) return;
    if (!deckEditingEl) return;
    e.stopPropagation();
    if (e.key === 'Escape') {
      e.preventDefault();
      if (deckColorPanelOpen) {
        setDeckColorPanelOpen(false);
        return;
      }
      commitDeckEdit(deckEditingEl, true);
      return;
    }
    var meta = e.metaKey || e.ctrlKey;
    if (meta && (e.key === 'b' || e.key === 'B')) {
      e.preventDefault(); runDeckFormatCommand('bold'); refreshDeckToolbarState(); return;
    }
    if (meta && (e.key === 'i' || e.key === 'I')) {
      e.preventDefault(); runDeckFormatCommand('italic'); refreshDeckToolbarState(); return;
    }
    if (meta && (e.key === 'u' || e.key === 'U')) {
      e.preventDefault(); runDeckFormatCommand('underline'); refreshDeckToolbarState(); return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      var tag = deckEditingEl.tagName;
      if (tag === 'H1' || tag === 'H2' || tag === 'H3' || tag === 'H4' || tag === 'P' || tag === 'LI') {
        e.preventDefault();
        commitDeckEdit(deckEditingEl, false);
      }
    }
  }

  function onDeckBlur(e) {
    if (!deckEditingEl) return;
    if (e.target !== deckEditingEl) return;
    var gen = deckCommitGeneration;
    if (deckBlurTimer) clearTimeout(deckBlurTimer);
    deckBlurTimer = setTimeout(function() {
      deckBlurTimer = null;
      if (deckSuppressBlur) return;
      // Another commit already ran (click-outside / Enter / disable).
      if (gen !== deckCommitGeneration) return;
      if (deckEditingEl && document.activeElement !== deckEditingEl) {
        var ae = document.activeElement;
        if (ae && deckToolbarEl && deckToolbarEl.contains(ae)) return;
        commitDeckEdit(deckEditingEl, false);
      }
    }, 0);
  }

  function onDeckSelectionChange() {
    if (!deckEditingEl) return;
    refreshDeckToolbarState();
  }

  function onDeckScrollOrResize() {
    if (!deckEditEnabled) return;
    placeAllDeckHandles();
    if (deckEditingEl) showDeckToolbar(deckEditingEl);
  }

  // -------------------------------------------------------------
  // Inline-comment marker pins (review-comments overlay).
  //
  // Markers are real DOM nodes inside the iframe (NOT a parent-side
  // overlay). They follow scroll/reflow natively because they live in
  // the same document as the element they annotate -- when the user
  // scrolls inside the iframe, browser layout moves both target and
  // marker together. The parent pushes the marker spec via
  // devs:set-inline-markers (idempotent, replaces the whole set),
  // and we report devs:inline-markers-state so the drawer / preview
  // toolbar know which threads resolved vs. went stale. Clicks
  // postMessage back as devs:inline-marker-click { threadId, rect }
  // so the parent can open its Mantine popover anchored to the
  // bubble; any scroll inside the iframe also posts
  // devs:inline-markers-scroll so the parent can dismiss the
  // popover (the active design is "freeze + close on scroll", so we
  // do NOT push live rect updates while the popover is open).
  // -------------------------------------------------------------
  var INLINE_MARKER_ROOT_ID = '__devs_inline_marker_root';
  var INLINE_MARKER_CLASS = '__devs_inline_marker';
  var _inlineMarkerRoot = null;
  var _inlineMarkers = Object.create(null); // threadId -> { bubble, selector, kind, ro }
  var _inlineLastState = ''; // last "mounted|stale" signature for change detection
  var _inlineReposScheduled = false;
  var _inlineMo = null;
  var _inlineScrollDebounce = 0;

  function ensureInlineMarkerRoot() {
    if (_inlineMarkerRoot && _inlineMarkerRoot.isConnected) return _inlineMarkerRoot;
    if (!document.body) return null;
    // position: fixed root keeps marker placement entirely independent
    // of the previewed app's layout. An absolute-in-body approach would
    // have to promote body to position: relative to act as a containing
    // block, which mutates the app's own absolutely-positioned children
    // (modals, headers, anything anchored off body) and ships a visible
    // regression to apps without a CSS reset. Fixed avoids that entirely
    // -- the root is anchored to the viewport at (0, 0) with zero size,
    // and child bubbles (position: absolute inside this fixed root)
    // resolve their containing block to the root's padding edge, which
    // sits at viewport (0, 0). Markers are positioned in viewport coords
    // directly; the existing scroll listener fires repositionInlineMarkers
    // on every scroll so the bubbles track their target as the user scrolls.
    var root = document.createElement('div');
    root.id = INLINE_MARKER_ROOT_ID;
    // pointer-events: none so the root itself never blocks clicks
    // on the underlying app; the bubbles individually re-enable
    // pointer events. z-index: 2147483646 keeps markers above all
    // realistic app content without claiming the absolute max (we
    // reserve max for the popover dim, parent-side).
    root.style.cssText =
      'position: fixed; top: 0; left: 0; width: 0; height: 0; ' +
      'pointer-events: none; z-index: 2147483646;';
    root.setAttribute('aria-hidden', 'true');
    document.body.appendChild(root);
    _inlineMarkerRoot = root;
    return root;
  }

  function inlineMarkerBorderColor(kind) {
    return kind === 'resolved' ? '#0ca678' /* teal 6 */ : '#ae3ec9' /* grape 6 */;
  }

  function buildInlineMarkerBubble(spec) {
    var bubble = document.createElement('div');
    bubble.className = INLINE_MARKER_CLASS;
    bubble.setAttribute('data-thread-id', spec.threadId);
    bubble.setAttribute('role', 'button');
    bubble.setAttribute('aria-label', spec.title || 'Inline comment');
    bubble.setAttribute('tabindex', '0');
    if (spec.title) bubble.title = spec.title;
    // Inline styles, not classes -- the user's app CSS could otherwise
    // bleed into our chrome (Tailwind reset, BEM globals, the works).
    // "all: initial" would be cleaner but also wipes font defaults we
    // depend on for the initials fallback, so we set every property
    // we care about explicitly.
    var resolved = spec.kind === 'resolved';
    bubble.style.cssText =
      'position: absolute; ' +
      'width: 24px; height: 24px; ' +
      'border-radius: 50%; ' +
      'border: 2px solid ' + inlineMarkerBorderColor(spec.kind) + '; ' +
      'background: #ffffff; ' +
      'color: #1a1b1e; ' +
      'opacity: ' + (resolved ? '0.6' : '1') + '; ' +
      'box-shadow: 0 1px 3px rgba(0,0,0,0.3); ' +
      'overflow: hidden; ' +
      'box-sizing: border-box; ' +
      'cursor: pointer; ' +
      'pointer-events: auto; ' +
      'display: none; ' + // hidden until first positionMarker() call
      'align-items: center; ' +
      'justify-content: center; ' +
      'font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; ' +
      'font-size: 10px; ' +
      'font-weight: 700; ' +
      'line-height: 1; ' +
      'letter-spacing: 0; ' +
      'padding: 0; margin: 0; ' +
      'user-select: none;';

    if (spec.imageUrl) {
      var img = document.createElement('img');
      img.src = spec.imageUrl;
      img.alt = '';
      img.style.cssText = 'width: 100%; height: 100%; object-fit: cover; display: block; pointer-events: none;';
      // Image-load error fallback -- swap to initials so a broken
      // avatar URL doesn't leave a broken-image glyph inside the
      // circle (mirrors the parent's MarkerBubble fallback).
      img.onerror = function() {
        if (img.parentNode === bubble) bubble.removeChild(img);
        bubble.appendChild(buildInlineMarkerInitials(spec.initials));
      };
      bubble.appendChild(img);
    } else {
      bubble.appendChild(buildInlineMarkerInitials(spec.initials));
    }

    // Stop propagation BEFORE the inspect-mode onClick listener can
    // see it (capture phase). We also need to keep the app underneath
    // from receiving the click -- otherwise tapping a pin on a
    // <button> would also trigger the button.
    //
    // The rect we ship up is the TARGET element's rect (not the
    // bubble's). The parent popover uses it to draw the Figma-style
    // spotlight cutout around the highlighted element -- if we sent
    // the bubble rect, the spotlight would hug the pin instead of the
    // thing the user is commenting on. Bubble rect is a sensible
    // fallback only when the target has been ripped out from under
    // the bubble between paint and click (rare but possible across a
    // React re-render that nukes the element seconds after the
    // pointer goes down).
    bubble.addEventListener('click', function(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      var entry = _inlineMarkers[spec.threadId];
      var targetEl = entry ? entry.targetEl : null;
      var r;
      if (targetEl) {
        r = targetEl.getBoundingClientRect();
      } else {
        r = bubble.getBoundingClientRect();
      }
      window.parent.postMessage({
        type: 'devs:inline-marker-click',
        threadId: spec.threadId,
        rect: { top: r.top, left: r.left, width: r.width, height: r.height }
      }, '*');
    }, true);

    return bubble;
  }

  function buildInlineMarkerInitials(initials) {
    var span = document.createElement('span');
    // Sub-pixel nudge matching the parent's hand-rolled MarkerBubble
    // -- system sans-serif uppercase glyphs render ~0.5px above the
    // geometric centre of a 24px circle. Without this they read as
    // "slightly high".
    span.style.cssText =
      'transform: translateY(0.5px); ' +
      'font-size: 10px; font-weight: 700; line-height: 1; ' +
      'letter-spacing: 0; user-select: none; pointer-events: none;';
    span.textContent = (initials || '?').slice(0, 2);
    return span;
  }

  function positionInlineMarker(entry) {
    var target = null;
    try {
      target = document.querySelector(entry.selector);
    } catch (_) {
      target = null;
    }
    if (!target) {
      entry.bubble.style.display = 'none';
      entry.located = false;
      entry.targetEl = null;
      return;
    }
    if (entry.targetEl !== target) {
      // Selector now resolves to a different element (re-render
      // mounted a fresh node with the same class set). Re-observe so
      // size changes on the new element trigger repositions.
      if (entry.ro) {
        try { entry.ro.disconnect(); } catch (_) { /* noop */ }
      }
      entry.targetEl = target;
      try {
        entry.ro = new ResizeObserver(scheduleInlineReposition);
        entry.ro.observe(target);
      } catch (_) { /* noop */ }
    }
    var r = target.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) {
      // Element is in the DOM but has no layout (display:none, etc.).
      // Treat as unlocatable for state-reporting purposes; do not
      // anchor a stale pin to (0,0).
      entry.bubble.style.display = 'none';
      entry.located = false;
      return;
    }
    // Marker root is position: fixed at viewport (0, 0). Bubbles use
    // position: absolute inside it, so their containing block is the
    // root's padding edge -- also at viewport (0, 0). That means the
    // marker's CSS top/left are just viewport coords; no body margin,
    // border, or scroll term is involved. The bubbles do NOT scroll
    // with the page automatically (fixed sticks to the viewport), so
    // the document-level scroll listener calls scheduleInlineReposition
    // and re-runs this math on every scroll frame to keep each bubble
    // glued to its target.
    var top = r.top - 12;
    var left = r.left + r.width - 12;
    entry.bubble.style.top = top + 'px';
    entry.bubble.style.left = left + 'px';
    entry.bubble.style.display = 'inline-flex';
    entry.located = true;
  }

  function repositionInlineMarkers() {
    _inlineReposScheduled = false;
    var mounted = [];
    var stale = [];
    for (var threadId in _inlineMarkers) {
      var entry = _inlineMarkers[threadId];
      positionInlineMarker(entry);
      if (entry.located) mounted.push(threadId);
      else stale.push(threadId);
    }
    // Emit state only on change -- the rAF + MutationObserver pair
    // can fire many times per second on a busy app, and the drawer's
    // stale-id pill would re-render every frame otherwise.
    var sig = mounted.slice().sort().join(',') + '|' + stale.slice().sort().join(',');
    if (sig !== _inlineLastState) {
      _inlineLastState = sig;
      window.parent.postMessage({
        type: 'devs:inline-markers-state',
        mounted: mounted,
        stale: stale
      }, '*');
    }
  }

  function scheduleInlineReposition() {
    if (_inlineReposScheduled) return;
    _inlineReposScheduled = true;
    window.requestAnimationFrame(repositionInlineMarkers);
  }

  function clearInlineMarkers() {
    for (var threadId in _inlineMarkers) {
      var entry = _inlineMarkers[threadId];
      if (entry.ro) {
        try { entry.ro.disconnect(); } catch (_) { /* noop */ }
      }
      if (entry.bubble && entry.bubble.parentNode) {
        entry.bubble.parentNode.removeChild(entry.bubble);
      }
    }
    _inlineMarkers = Object.create(null);
    _inlineLastState = '';
    // Tear down the global MutationObserver too -- with no markers
    // there is nothing to reposition, but the observer would still
    // wake on every DOM mutation in the previewed app and schedule a
    // do-nothing rAF. ensureInlineGlobalObservers re-creates it the
    // next time setInlineMarkers is called with a non-empty spec.
    if (_inlineMo) {
      try { _inlineMo.disconnect(); } catch (_) { /* noop */ }
      _inlineMo = null;
    }
  }

  function setInlineMarkers(specs) {
    if (!Array.isArray(specs)) specs = [];
    var root = ensureInlineMarkerRoot();
    if (!root) return;
    var nextIds = Object.create(null);
    for (var i = 0; i < specs.length; i += 1) {
      var spec = specs[i];
      if (!spec || typeof spec.threadId !== 'string' || typeof spec.selector !== 'string') continue;
      nextIds[spec.threadId] = true;
      var existing = _inlineMarkers[spec.threadId];
      if (existing) {
        // Same thread -- refresh visual props in place (kind change
        // on resolve/reopen, selector edit, avatar update). Rebuild
        // the bubble only when the structural inputs changed; for a
        // pure selector swap we just update the cached selector.
        var visualChanged = existing.kind !== spec.kind ||
          existing.initials !== spec.initials ||
          existing.imageUrl !== spec.imageUrl ||
          existing.title !== spec.title;
        if (visualChanged) {
          var rebuilt = buildInlineMarkerBubble(spec);
          if (existing.bubble.parentNode) {
            existing.bubble.parentNode.replaceChild(rebuilt, existing.bubble);
          } else {
            root.appendChild(rebuilt);
          }
          existing.bubble = rebuilt;
          existing.kind = spec.kind;
          existing.initials = spec.initials;
          existing.imageUrl = spec.imageUrl;
          existing.title = spec.title;
        }
        if (existing.selector !== spec.selector) {
          existing.selector = spec.selector;
          existing.targetEl = null; // force re-observe on next position
        }
      } else {
        var bubble = buildInlineMarkerBubble(spec);
        root.appendChild(bubble);
        _inlineMarkers[spec.threadId] = {
          bubble: bubble,
          selector: spec.selector,
          kind: spec.kind,
          initials: spec.initials,
          imageUrl: spec.imageUrl,
          title: spec.title,
          targetEl: null,
          ro: null,
          located: false
        };
      }
    }
    // Drop any markers that the new spec set no longer references.
    for (var oldId in _inlineMarkers) {
      if (nextIds[oldId]) continue;
      var dead = _inlineMarkers[oldId];
      if (dead.ro) {
        try { dead.ro.disconnect(); } catch (_) { /* noop */ }
      }
      if (dead.bubble && dead.bubble.parentNode) {
        dead.bubble.parentNode.removeChild(dead.bubble);
      }
      delete _inlineMarkers[oldId];
    }
    // Force a state recompute so transitions (e.g. empty -> non-empty)
    // emit even when the signature was already empty-string.
    _inlineLastState = '';
    // Mirror clearInlineMarkers when the diff just emptied the marker
    // set (resolved-only filter flipped on, URL navigated off the
    // anchored page, the last comment was deleted, etc.). Without this
    // the global MutationObserver keeps watching the entire app body
    // and re-firing the filter callback on every DOM mutation, even
    // though every reposition cycle would walk an empty marker set.
    // ensureInlineGlobalObservers re-arms the observer the next time
    // setInlineMarkers is called with a non-empty spec, so the live
    // tracking path is unchanged.
    var hasMarkers = false;
    for (var anyId in _inlineMarkers) { hasMarkers = true; break; }
    if (hasMarkers) {
      ensureInlineGlobalObservers();
    } else if (_inlineMo) {
      try { _inlineMo.disconnect(); } catch (_) { /* noop */ }
      _inlineMo = null;
    }
    scheduleInlineReposition();
  }

  function ensureInlineGlobalObservers() {
    // Single MutationObserver across the whole subtree drives
    // reposition for layout-affecting DOM changes (an accordion
    // opens, the user types into an input that grows the page, a
    // React re-render swaps the tracked element). Cheap because the
    // observer just schedules an rAF; the actual reposition only
    // walks the active marker set.
    //
    // Filter out records whose target lives inside our own marker
    // root. Each positionInlineMarker call writes style.top / left /
    // display on a bubble, and the bubble sits inside body's subtree.
    // Without this filter, every reposition pass triggers the
    // observer, which schedules another reposition, creating a
    // continuous ~60fps loop while any markers exist. The
    // _inlineReposScheduled flag does not help because it is reset
    // at the start of repositionInlineMarkers (so the MO microtask
    // always finds it false during the style writes).
    if (!_inlineMo && document.body) {
      _inlineMo = new MutationObserver(function (records) {
        for (var i = 0; i < records.length; i += 1) {
          if (!isInsideInlineMarker(records[i].target)) {
            scheduleInlineReposition();
            return;
          }
        }
      });
      _inlineMo.observe(document.body, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['style', 'class', 'hidden']
      });
    }
  }

  function notifyInlineScroll() {
    // Single coalesced ping per "scroll session". The parent uses
    // this to dismiss any active inline-comment popover ("freeze and
    // close on scroll" UX); it does NOT need a stream.
    if (_inlineScrollDebounce) return;
    _inlineScrollDebounce = window.setTimeout(function() {
      _inlineScrollDebounce = 0;
    }, 250);
    window.parent.postMessage({ type: 'devs:inline-markers-scroll' }, '*');
  }

  // Capture-phase scroll listener catches scroll on the document AND
  // every inner overflow:scroll container -- scroll events don't
  // bubble by default but DO reach capture-phase listeners on the
  // root. We don't reposition markers from this hook: absolute
  // positioning inside body handles document-scroll natively, and
  // inner-container scroll moves the element AND its computed
  // getBoundingClientRect together, which the per-element
  // ResizeObserver + MutationObserver pair already catch.
  document.addEventListener('scroll', function() {
    if (Object.keys(_inlineMarkers).length === 0) return;
    scheduleInlineReposition();
    notifyInlineScroll();
  }, { capture: true, passive: true });
  window.addEventListener('resize', scheduleInlineReposition);

  window.addEventListener('message', function(e) {
    if (!e.data || !e.data.type) return;
    // Browse commands act on / read the live DOM, so accept them only from the
    // embedding AppBuilder parent. This blocks the previewed app's own code from
    // dispatching devs:browse-* to its window to exfiltrate page content.
    if (typeof e.data.type === 'string' && e.data.type.indexOf('devs:browse-') === 0 && devsIsForeignOrigin(e)) {
      return;
    }
    if (e.data.type === 'devs:enable-inspect') {
      inspectEnabled = true;
      document.body.style.cursor = 'crosshair';
      // Inspect and deck-edit are mutually exclusive.
      if (deckEditEnabled) {
        deckEditEnabled = false;
        setDeckEditModeUi(false);
      }
    } else if (e.data.type === 'devs:disable-inspect') {
      inspectEnabled = false;
      document.body.style.cursor = '';
    } else if (e.data.type === 'devs:enable-deck-edit') {
      deckEditEnabled = true;
      inspectEnabled = false;
      document.body.style.cursor = '';
      setDeckEditModeUi(true);
      window.parent.postMessage({ type: 'devs:deck-edit-ready', version: INSPECT_SCRIPT_VERSION }, '*');
    } else if (e.data.type === 'devs:disable-deck-edit') {
      deckEditEnabled = false;
      setDeckEditModeUi(false);
    } else if (e.data.type === 'devs:setup-touch' && !window.__devsTouchSetup) {
      window.__devsTouchSetup = true;
      document.addEventListener('touchstart', function(te) {
        if (!inspectEnabled) return;
        var t = te.touches[0]; if (!t) return;
        var el = document.elementFromPoint(t.clientX, t.clientY);
        if (!el || el === document.body || el === document.documentElement) return;
        if (isInsideInlineMarker(el)) return;
        te.preventDefault();
        window.parent.postMessage({ type: 'devs:hover', payload: { rect: getRect(el), tagName: el.tagName.toLowerCase(), selector: getSelector(el) } }, '*');
      }, { capture: true, passive: false });
      document.addEventListener('touchend', function(te) {
        if (!inspectEnabled) return;
        var t = te.changedTouches[0]; if (!t) return;
        var el = document.elementFromPoint(t.clientX, t.clientY);
        if (!el || el === document.body || el === document.documentElement) return;
        if (isInsideInlineMarker(el)) return;
        te.preventDefault();
        var tc = (el.textContent || '').trim();
        var selectionToken = nextSelectionToken();
        window.parent.postMessage({ type: 'devs:select', payload: { selectionToken: selectionToken, selector: getSelector(el), tagName: el.tagName.toLowerCase(), className: getElementClassName(el), id: el.id || '', textContent: tc.length > 100 ? tc.slice(0, 100) + '...' : tc, htmlSnippet: getSnippet(el), rect: getRect(el), ancestorPath: getAncestorPath(el, 3) } }, '*');
        captureElement(el, selectionToken);
      }, { capture: true, passive: false });
    } else if (e.data.type === 'devs:ping') {
      window.parent.postMessage({ type: 'devs:pong', version: INSPECT_SCRIPT_VERSION }, '*');
    } else if (e.data.type === 'devs:get-location') {
      reportLocation();
    } else if (e.data.type === 'devs:browse-back') {
      history.back();
    } else if (e.data.type === 'devs:browse-forward') {
      history.forward();
    } else if (e.data.type === 'devs:set-inline-markers') {
      // Parent → iframe: replace the in-document marker set. Specs
      // are { threadId, selector, kind: 'open'|'resolved', initials,
      // imageUrl, title }. Iframe responds asynchronously via
      // devs:inline-markers-state once the rAF reposition cycle
      // resolves each selector.
      setInlineMarkers(Array.isArray(e.data.markers) ? e.data.markers : []);
    } else if (e.data.type === 'devs:clear-inline-markers') {
      // Parent → iframe: tear down all marker DOM. Sent when the
      // overlay unmounts (e.g. user closes review mode) so we don't
      // leave dangling pin nodes attached to the user's app.
      clearInlineMarkers();
      // Final empty-state ack so the parent's stale/visible counters
      // reset deterministically rather than relying on the next
      // implicit reposition cycle.
      window.parent.postMessage({
        type: 'devs:inline-markers-state',
        mounted: [],
        stale: []
      }, '*');
    } else if (e.data.type === 'devs:locate-selectors') {
      // RPC used by the review-comment pin-marker overlay. Takes an
      // array of CSS selectors, returns a parallel array of viewport
      // rects (relative to the iframe viewport, NOT the page) so the
      // parent can absolute-position avatar bubbles over the iframe.
      // Selectors that no longer match anything resolve to rect=null,
      // which the parent uses to surface a "can't locate element"
      // badge in the drawer.
      var requestId = e.data.requestId;
      var selectors = Array.isArray(e.data.selectors) ? e.data.selectors : [];
      var results = selectors.map(function(selector) {
        if (typeof selector !== 'string' || !selector) {
          return { selector: selector, rect: null };
        }
        var el = null;
        try { el = document.querySelector(selector); } catch (_) { el = null; }
        if (!el) return { selector: selector, rect: null };
        var rect = el.getBoundingClientRect();
        return {
          selector: selector,
          rect: {
            top: rect.top,
            left: rect.left,
            width: rect.width,
            height: rect.height,
          },
        };
      });
      window.parent.postMessage({
        type: 'devs:located-selectors',
        requestId: requestId,
        results: results,
        viewport: { width: window.innerWidth, height: window.innerHeight },
      }, '*');
    } else if (e.data.type === 'devs:db-query') {
      var queryId = e.data.queryId;
      var sql = e.data.sql;
      (function() {
        var pglite = window.__devs_pglite;
        if (!pglite) {
          // Try to dynamically import PGlite and open the IDB database
          import('@electric-sql/pglite').then(function(mod) {
            var client = new mod.PGlite('idb://app-db');
            window.__devs_pglite = client;
            return client.waitReady ? client.waitReady.then(function() { return client; }) : client;
          }).then(function(client) {
            return client.query(sql);
          }).then(function(result) {
            window.parent.postMessage({ type: 'devs:db-result', queryId: queryId, rows: result.rows, fields: result.fields ? result.fields.map(function(f) { return f.name; }) : [] }, '*');
          }).catch(function(err) {
            window.parent.postMessage({ type: 'devs:db-result', queryId: queryId, error: String(err.message || err) }, '*');
          });
          return;
        }
        var ready = pglite.waitReady ? pglite.waitReady : Promise.resolve();
        ready.then(function() {
          return pglite.query(sql);
        }).then(function(result) {
          window.parent.postMessage({ type: 'devs:db-result', queryId: queryId, rows: result.rows, fields: result.fields ? result.fields.map(function(f) { return f.name; }) : [] }, '*');
        }).catch(function(err) {
          window.parent.postMessage({ type: 'devs:db-result', queryId: queryId, error: String(err.message || err) }, '*');
        });
      })();
    } else if (e.data.type === 'devs:browse-navigate') {
      // Agent-driven and path-bar navigation of the LIVE preview. Prefer the
      // History API so SPA routers react without a full reload (which would
      // tear down this script and orphan the pending promise). Reply after a
      // short settle. Do NOT click in-page controls here — fuzzy name matching
      // was clicking the wrong element and breaking the preview.
      var navId = e.data.commandId;
      var path = typeof e.data.path === 'string' ? e.data.path : '/';
      suppressNavReportUntil = Date.now() + 800;
      try {
        var navUrl = new URL(path, window.location.href);
        if (navUrl.origin === window.location.origin) {
          history.pushState({}, '', navUrl.pathname + navUrl.search + navUrl.hash);
          window.dispatchEvent(new PopStateEvent('popstate'));
        } else {
          window.location.assign(navUrl.href);
        }
        setTimeout(function() {
          browseReply({ type: 'devs:browse-result', commandId: navId, result: { ok: true, url: window.location.href, title: document.title } });
        }, 350);
      } catch (err) {
        browseReply({ type: 'devs:browse-result', commandId: navId, error: String((err && err.message) || err) });
      }
    } else if (e.data.type === 'devs:browse-click') {
      var clickId = e.data.commandId;
      try {
        var clickEl = browseResolveTarget(e.data, browseFindClickable);
        if (!clickEl) {
          // Help the agent retarget: return the visible clickable labels.
          var candidates = browseInteractiveElements(25)
            .filter(function(x) { return x.tag === 'button' || x.tag === 'a' || x.type === 'submit' || x.type === 'button' || x.type === 'link'; })
            .map(function(x) { return { selector: x.selector, label: x.label }; })
            .slice(0, 12);
          browseReply({ type: 'devs:browse-result', commandId: clickId, result: { ok: false, matched: false, error: 'Element not found', candidates: candidates } });
        } else {
          if (typeof clickEl.scrollIntoView === 'function') { try { clickEl.scrollIntoView({ block: 'center' }); } catch (_) {} }
          // Glide the agent cursor to the target, then click on arrival so the
          // user can watch the interaction in the live preview.
          browseShowCursor(clickEl, 'AI clicking');
          setTimeout(function() {
            try {
              browseCursorPulse(clickEl);
              // Native .click() triggers default behavior AND React's delegated
              // onClick; fall back to a full synthetic sequence for exotic targets.
              if (typeof clickEl.click === 'function') {
                clickEl.click();
              } else {
                ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(function(t) {
                  try { clickEl.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window })); } catch (_) {}
                });
              }
              browseReply({ type: 'devs:browse-result', commandId: clickId, result: { ok: true, matched: true, selector: getSelector(clickEl) } });
            } catch (innerErr) {
              browseReply({ type: 'devs:browse-result', commandId: clickId, error: String((innerErr && innerErr.message) || innerErr) });
            }
          }, 420);
        }
      } catch (err) {
        browseReply({ type: 'devs:browse-result', commandId: clickId, error: String((err && err.message) || err) });
      }
    } else if (e.data.type === 'devs:browse-type') {
      var typeId = e.data.commandId;
      try {
        var field = browseResolveTarget(e.data, browseFindField);
        if (!field) {
          var fieldCandidates = browseInteractiveElements(25)
            .filter(function(x) { return x.tag === 'input' || x.tag === 'textarea' || x.tag === 'select' || x.type === 'textbox'; })
            .map(function(x) { return { selector: x.selector, label: x.label, type: x.type }; })
            .slice(0, 12);
          browseReply({ type: 'devs:browse-result', commandId: typeId, result: { ok: false, error: 'Field not found', candidates: fieldCandidates } });
        } else {
          if (typeof field.scrollIntoView === 'function') { try { field.scrollIntoView({ block: 'center' }); } catch (_) {} }
          // Glide the agent cursor to the field, then fill on arrival.
          browseShowCursor(field, 'AI typing');
          setTimeout(function() {
            try {
              browseCursorPulse(field);
              var text = typeof e.data.text === 'string' ? e.data.text : '';
              if (typeof field.focus === 'function') { try { field.focus(); } catch (_) {} }
              var isContentEditable = field.getAttribute && field.getAttribute('contenteditable') === 'true';
              if (isContentEditable) {
                field.textContent = text;
                field.dispatchEvent(new Event('input', { bubbles: true }));
              } else {
                var proto = (typeof HTMLTextAreaElement !== 'undefined' && field instanceof HTMLTextAreaElement)
                  ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                var desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) { desc.set.call(field, text); } else { field.value = text; }
                // input + change drive React/Vue controlled components; keyup nudges
                // listeners that only watch keystrokes.
                field.dispatchEvent(new Event('input', { bubbles: true }));
                field.dispatchEvent(new Event('change', { bubbles: true }));
                try { field.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true })); } catch (_) {}
              }
              if (e.data.submit) {
                var form = field.form || (field.closest ? field.closest('form') : null);
                if (form && typeof form.requestSubmit === 'function') { form.requestSubmit(); }
                else if (form && typeof form.submit === 'function') { form.submit(); }
                else {
                  field.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', keyCode: 13, which: 13 }));
                  field.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', keyCode: 13, which: 13 }));
                }
              }
              browseReply({ type: 'devs:browse-result', commandId: typeId, result: { ok: true, selector: getSelector(field) } });
            } catch (innerErr) {
              browseReply({ type: 'devs:browse-result', commandId: typeId, error: String((innerErr && innerErr.message) || innerErr) });
            }
          }, 420);
        }
      } catch (err) {
        browseReply({ type: 'devs:browse-result', commandId: typeId, error: String((err && err.message) || err) });
      }
    } else if (e.data.type === 'devs:browse-read') {
      var readId = e.data.commandId;
      try {
        var readEl = document.body;
        if (typeof e.data.selector === 'string' && e.data.selector) {
          try { readEl = document.querySelector(e.data.selector) || document.body; } catch (_) { readEl = document.body; }
        }
        var maxChars = typeof e.data.maxChars === 'number' && e.data.maxChars > 0 ? e.data.maxChars : 2000;
        var readText = (readEl.innerText || readEl.textContent || '').replace(/\s+/g, ' ').trim().slice(0, maxChars);
        // Include the interactive elements (with selectors) so the agent can
        // target inputs/buttons precisely instead of guessing selectors.
        var elements = browseInteractiveElements(40);
        browseReply({ type: 'devs:browse-result', commandId: readId, result: { ok: true, url: window.location.href, title: document.title, text: readText, elements: elements } });
      } catch (err) {
        browseReply({ type: 'devs:browse-result', commandId: readId, error: String((err && err.message) || err) });
      }
    } else if (e.data.type === 'devs:browse-screenshot') {
      var shotId = e.data.commandId;
      loadHtml2Canvas().then(function(h2c) {
        if (!h2c) { browseReply({ type: 'devs:browse-result', commandId: shotId, result: { ok: false, error: 'Screenshot unavailable' } }); return; }
        var vw = window.innerWidth;
        var vh = window.innerHeight;
        var maxDim = 1024;
        var scale = Math.min(1, maxDim / Math.max(vw || 1, vh || 1));
        h2c(document.documentElement, {
          scale: scale,
          useCORS: true,
          logging: false,
          backgroundColor: '#ffffff',
          removeContainer: true,
          x: window.scrollX,
          y: window.scrollY,
          width: vw,
          height: vh
        }).then(function(canvas) {
          var dataUrl = canvas.toDataURL('image/jpeg', 0.7);
          browseReply({ type: 'devs:browse-result', commandId: shotId, result: { ok: true, dataUrl: dataUrl } });
        }).catch(function(err) {
          browseReply({ type: 'devs:browse-result', commandId: shotId, error: String((err && err.message) || err) });
        });
      }).catch(function(err) {
        browseReply({ type: 'devs:browse-result', commandId: shotId, error: String((err && err.message) || err) });
      });
    } else if (e.data.type === 'devs:browse-wait') {
      var waitId = e.data.commandId;
      var waitCap = 10000;
      var selector = typeof e.data.selector === 'string' && e.data.selector ? e.data.selector : null;
      if (selector) {
        var deadline = Date.now() + waitCap;
        var poll = function() {
          var found = null;
          try { found = document.querySelector(selector); } catch (_) { found = null; }
          if (found) {
            browseReply({ type: 'devs:browse-result', commandId: waitId, result: { ok: true, found: true } });
          } else if (Date.now() >= deadline) {
            browseReply({ type: 'devs:browse-result', commandId: waitId, result: { ok: true, found: false } });
          } else {
            setTimeout(poll, 200);
          }
        };
        poll();
      } else {
        var ms = typeof e.data.ms === 'number' && e.data.ms > 0 ? Math.min(e.data.ms, waitCap) : 500;
        setTimeout(function() {
          browseReply({ type: 'devs:browse-result', commandId: waitId, result: { ok: true } });
        }, ms);
      }
    }
  });

  document.addEventListener('mouseover', onHover, true);
  document.addEventListener('mouseout', onHoverOut, true);
  document.addEventListener('click', onClick, true);
  document.addEventListener('click', onPreviewNavClick, true);
  document.addEventListener('click', onDeckClick, true);
  document.addEventListener('dblclick', onDeckDblClick, true);
  document.addEventListener('keydown', onDeckKeyDown, true);
  document.addEventListener('focusout', onDeckBlur, true);
  document.addEventListener('selectionchange', onDeckSelectionChange, true);
  window.addEventListener('scroll', onDeckScrollOrResize, true);
  window.addEventListener('resize', onDeckScrollOrResize, true);
  // Reveal fires slidechanged on the .reveal root (and sometimes window).
  document.addEventListener('slidechanged', function() {
    if (deckEditEnabled) stampVisibleSlideEditables();
  }, true);
  try {
    if (window.Reveal && typeof window.Reveal.on === 'function') {
      window.Reveal.on('slidechanged', function() {
        if (deckEditEnabled) stampVisibleSlideEditables();
      });
      window.Reveal.on('ready', function() {
        if (deckEditEnabled) stampVisibleSlideEditables();
      });
    }
  } catch (_) {}

  // Touch support for mobile inspect
  window.__devsTouchSetup = true;
  document.addEventListener('touchstart', function(e) {
    if (!inspectEnabled) return;
    var touch = e.touches[0];
    if (!touch) return;
    var el = document.elementFromPoint(touch.clientX, touch.clientY);
    if (!el || el === document.body || el === document.documentElement) return;
    if (isInsideInlineMarker(el)) return;
    e.preventDefault();
    window.parent.postMessage({
      type: 'devs:hover',
      payload: {
        rect: getRect(el),
        tagName: el.tagName.toLowerCase(),
        selector: getSelector(el)
      }
    }, '*');
  }, { capture: true, passive: false });

  document.addEventListener('touchend', function(e) {
    if (!inspectEnabled) return;
    var touch = e.changedTouches[0];
    if (!touch) return;
    var el = document.elementFromPoint(touch.clientX, touch.clientY);
    if (!el || el === document.body || el === document.documentElement) return;
    if (isInsideInlineMarker(el)) return;
    e.preventDefault();
    var textContent = (el.textContent || '').trim();
    var selectionToken = nextSelectionToken();
    window.parent.postMessage({
      type: 'devs:select',
      payload: {
        selectionToken: selectionToken,
        selector: getSelector(el),
        tagName: el.tagName.toLowerCase(),
        className: getElementClassName(el),
        id: el.id || '',
        textContent: textContent.length > 100 ? textContent.slice(0, 100) + '...' : textContent,
        htmlSnippet: getSnippet(el),
        rect: getRect(el),
        ancestorPath: getAncestorPath(el, 3)
      }
    }, '*');
    captureElement(el, selectionToken);
  }, { capture: true, passive: false });

  // Forward console output to parent for the Logs panel
  var _origLog = console.log;
  var _origWarn = console.warn;
  var _origError = console.error;
  var _origInfo = console.info;

  function forwardConsole(level, args) {
    try {
      var parts = [];
      for (var i = 0; i < args.length; i++) {
        var a = args[i];
        parts.push(typeof a === 'string' ? a : JSON.stringify(a));
      }
      window.parent.postMessage({
        type: 'devs:console',
        level: level,
        text: parts.join(' ')
      }, '*');
    } catch (e) { /* ignore serialization errors */ }
  }

  console.log = function() { forwardConsole('log', arguments); return _origLog.apply(console, arguments); };
  console.warn = function() { forwardConsole('warn', arguments); return _origWarn.apply(console, arguments); };
  console.error = function() { forwardConsole('error', arguments); return _origError.apply(console, arguments); };
  console.info = function() { forwardConsole('info', arguments); return _origInfo.apply(console, arguments); };

  // Forward unhandled errors
  window.addEventListener('error', function(e) {
    window.parent.postMessage({
      type: 'devs:console',
      level: 'error',
      text: '[Runtime Error] ' + (e.message || String(e.error))
    }, '*');
  });

  window.addEventListener('unhandledrejection', function(e) {
    window.parent.postMessage({
      type: 'devs:console',
      level: 'error',
      text: '[Unhandled Promise] ' + (e.reason && e.reason.message ? e.reason.message : String(e.reason))
    }, '*');
  });

  // Intercept fetch to detect failed HTTP responses (4xx/5xx)
  var _origFetch = window.fetch;
  window.fetch = function() {
    var url = arguments[0];
    var urlStr = typeof url === 'string' ? url : (url && url.url ? url.url : String(url));
    return _origFetch.apply(this, arguments).then(function(response) {
      if (!response.ok && response.status >= 400) {
        var level = response.status >= 500 ? 'error' : 'warn';
        var label = response.status >= 500 ? 'Server error' : 'Request failed';
        try {
          var path = new URL(urlStr, location.origin).pathname;
          window.parent.postMessage({
            type: 'devs:console',
            level: level,
            text: '[HTTP ' + response.status + '] ' + label + ': ' + path
          }, '*');
        } catch(e) {
          window.parent.postMessage({
            type: 'devs:console',
            level: level,
            text: '[HTTP ' + response.status + '] ' + label + ': (unparseable URL)'
          }, '*');
        }
      }
      return response;
    });
  };

  // Keep the parent's address bar current across SPA navigations.
  // Only wrap instance methods bound to history. Assigning onto
  // History.prototype breaks some routers with an Illegal invocation.
  if (window.__devsLocationHooked !== INSPECT_SCRIPT_VERSION) {
    window.__devsLocationHooked = INSPECT_SCRIPT_VERSION;
    var _origPushState = history.pushState.bind(history);
    var _origReplaceState = history.replaceState.bind(history);
    history.pushState = function() {
      var ret = _origPushState.apply(history, arguments);
      reportLocationIfChanged();
      return ret;
    };
    history.replaceState = function() {
      var ret = _origReplaceState.apply(history, arguments);
      reportLocationIfChanged();
      return ret;
    };
    window.addEventListener('popstate', reportLocationIfChanged);
    window.addEventListener('hashchange', reportLocationIfChanged);
    try {
      if (window.navigation && typeof window.navigation.addEventListener === 'function') {
        window.navigation.addEventListener('navigate', function() {
          setTimeout(reportLocationIfChanged, 0);
        });
      }
    } catch (_) {}
    if (window.__devsLocationPoll) {
      try { clearInterval(window.__devsLocationPoll); } catch (_) {}
    }
    window.__devsLocationPoll = setInterval(reportLocationIfChanged, 400);
  }
  reportLocation();
})();