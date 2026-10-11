/* App shell behavior: sidebar collapse, toasts, copy-to-clipboard, tabs.
 *
 * htmx swaps replace DOM nodes, which destroys anything bound inside the old
 * node and leaves anything newly inserted unbound. So every initializer here
 * is (a) scoped to a root element and (b) idempotent, guarded by a
 * data-initialized attribute, and is re-run on htmx:afterSwap.
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------- utils */

  function els(root, selector) {
    var out = Array.prototype.slice.call(root.querySelectorAll(selector));
    // The swapped node itself can be a match, not just its descendants.
    if (root.matches && root.matches(selector)) out.unshift(root);
    return out;
  }

  // Marks `el` as initialized for `name`. Returns false if it already was.
  function claim(el, name) {
    var done = (el.getAttribute("data-initialized") || "").split(" ");
    if (done.indexOf(name) !== -1) return false;
    done.push(name);
    el.setAttribute("data-initialized", done.join(" ").trim());
    return true;
  }

  window.QE = window.QE || {};
  QE.els = els;
  QE.claim = claim;

  /* --------------------------------------------------------------- toasts */

  function toast(message, tone) {
    var host = document.getElementById("toasts");
    if (!host) return;
    var node = document.createElement("div");
    node.className = "toast" + (tone ? " toast--" + tone : "");
    // textContent, never innerHTML: messages can contain SQL and engine errors.
    node.textContent = message;
    host.appendChild(node);
    setTimeout(function () {
      node.classList.add("is-leaving");
      setTimeout(function () {
        if (node.parentNode) node.parentNode.removeChild(node);
      }, 220);
    }, 4200);
  }

  QE.toast = toast;

  /* -------------------------------------------------------------- sidebar */

  var SIDEBAR_KEY = "qe.sidebar.collapsed";

  function initSidebar() {
    var shell = document.getElementById("shell");
    if (!shell || !claim(shell, "sidebar")) return;

    var toggle = shell.querySelector("[data-sidebar-toggle]");
    var burger = shell.querySelector("[data-sidebar-open]");
    var scrim = shell.querySelector(".scrim");

    function setCollapsed(on) {
      shell.classList.toggle("is-collapsed", on);
      try {
        localStorage.setItem(SIDEBAR_KEY, on ? "1" : "0");
      } catch (e) {
        /* private browsing: collapse still works, just is not remembered */
      }
      if (toggle) toggle.setAttribute("aria-expanded", on ? "false" : "true");
    }

    if (toggle) {
      toggle.addEventListener("click", function () {
        setCollapsed(!shell.classList.contains("is-collapsed"));
      });
    }

    // Below 1024px the sidebar is an off-canvas drawer instead.
    function setOpen(on) {
      shell.classList.toggle("is-open", on);
      if (burger) burger.setAttribute("aria-expanded", on ? "true" : "false");
    }

    if (burger) {
      burger.addEventListener("click", function () {
        setOpen(!shell.classList.contains("is-open"));
      });
    }

    if (scrim) {
      scrim.addEventListener("click", function () {
        setOpen(false);
      });
    }

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && shell.classList.contains("is-open")) setOpen(false);
    });
  }

  /* ----------------------------------------------------------------- copy */

  function initCopy(root) {
    els(root, "[data-copy]").forEach(function (btn) {
      if (!claim(btn, "copy")) return;
      btn.addEventListener("click", function () {
        var text = btn.getAttribute("data-copy");
        if (text === "@target") {
          var sel = btn.getAttribute("data-copy-target");
          var target = sel ? document.querySelector(sel) : null;
          text = target ? target.textContent : "";
        }
        copyText(text).then(function (ok) {
          if (!ok) {
            toast("Could not copy to clipboard", "bad");
            return;
          }
          btn.classList.add("copy-ok");
          setTimeout(function () {
            btn.classList.remove("copy-ok");
          }, 1100);
          toast(btn.getAttribute("data-copy-label") || "Copied", "good");
        });
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return legacyCopy(text);
        }
      );
    }
    return Promise.resolve(legacyCopy(text));
  }

  // runserver is plain http, where navigator.clipboard is unavailable.
  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (e) {
      ok = false;
    }
    document.body.removeChild(ta);
    return ok;
  }

  QE.copyText = copyText;

  /* ----------------------------------------------------------------- tabs */

  function initTabs(root) {
    els(root, "[data-tabs]").forEach(function (group) {
      if (!claim(group, "tabs")) return;

      var tabs = Array.prototype.slice.call(group.querySelectorAll("[role=tab]"));
      if (!tabs.length) return;

      function select(tab, focus) {
        tabs.forEach(function (t) {
          var on = t === tab;
          t.setAttribute("aria-selected", on ? "true" : "false");
          t.setAttribute("tabindex", on ? "0" : "-1");
          var panel = document.getElementById(t.getAttribute("aria-controls"));
          if (panel) panel.hidden = !on;
        });
        if (focus) tab.focus();
        // Let late-initializing visuals (mermaid, charts) measure a panel that
        // was display:none when they first ran.
        document.dispatchEvent(
          new CustomEvent("qe:tabshown", {
            detail: { panel: document.getElementById(tab.getAttribute("aria-controls")) },
          })
        );
      }

      tabs.forEach(function (tab, i) {
        tab.addEventListener("click", function () {
          select(tab, false);
        });
        tab.addEventListener("keydown", function (e) {
          var next = null;
          if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
          else if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
          else if (e.key === "Home") next = tabs[0];
          else if (e.key === "End") next = tabs[tabs.length - 1];
          if (next) {
            e.preventDefault();
            select(next, true);
          }
        });
      });

      var current = group.querySelector('[role=tab][aria-selected=true]') || tabs[0];
      select(current, false);

      group.qeSelectTab = function (id) {
        var match = tabs.filter(function (t) {
          return t.getAttribute("aria-controls") === id;
        })[0];
        if (match) select(match, false);
      };
    });
  }

  /* --------------------------------------------------------- elapsed timer */

  // Counts up while a run is pending/running so the UI is not frozen between
  // 700ms polls. Stops as soon as the node is swapped away.
  function initElapsed(root) {
    els(root, "[data-elapsed-from]").forEach(function (el) {
      if (!claim(el, "elapsed")) return;
      var start = parseFloat(el.getAttribute("data-elapsed-from")) * 1000;
      if (!start) start = Date.now();
      var timer = setInterval(function () {
        if (!el.isConnected) {
          clearInterval(timer);
          return;
        }
        var ms = Date.now() - start;
        el.textContent = (ms / 1000).toFixed(1) + "s";
      }, 100);
    });
  }

  /* ----------------------------------------------------------- init bridge */

  function initWithin(root) {
    if (!root || root.nodeType !== 1) root = document.body || document;
    initCopy(root);
    initTabs(root);
    initElapsed(root);
    // Each of these is optional: its file is only loaded on the pages that
    // need it, and an absent initializer must not break the rest of the pass.
    if (QE.initEditor) QE.initEditor(root);
    if (QE.initPlan) QE.initPlan(root);
    if (QE.initCharts) QE.initCharts(root);
    if (QE.initExport) QE.initExport(root);
    if (QE.initTour) QE.initTour(root);
  }

  QE.initWithin = initWithin;

  document.addEventListener("DOMContentLoaded", function () {
    initSidebar();
    initWithin(document);

    // Django messages are rendered into a template and replayed as toasts so
    // the shell has exactly one notification surface.
    var queued = document.getElementById("queued-messages");
    if (queued) {
      els(queued.content || queued, "[data-message]").forEach(function (m) {
        toast(m.getAttribute("data-message"), m.getAttribute("data-tone") || "");
      });
    }
  });

  document.addEventListener("htmx:afterSwap", function (e) {
    initWithin(e.target);
  });

  // htmx does not swap on 4xx/5xx, so without this a failed request looks
  // like nothing happened at all.
  document.addEventListener("htmx:responseError", function (e) {
    var status = e.detail && e.detail.xhr ? e.detail.xhr.status : 0;
    toast("Request failed (HTTP " + status + ")", "bad");
  });

  document.addEventListener("htmx:sendError", function () {
    toast("Network error — is the server still running?", "bad");
  });
})();
