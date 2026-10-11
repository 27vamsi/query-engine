/* Mermaid plan diagrams, safe under repeated htmx swaps.
 *
 * Two hazards this file exists to handle:
 *
 * 1. The run-status partial re-swaps itself every ~700ms while a query is
 *    running, so a naive `mermaid.run()` inside the partial re-renders the
 *    diagram on every tick. Rendering is therefore keyed on a data-rendered
 *    guard and driven from a single delegated htmx:afterSwap listener.
 *
 * 2. Mermaid writes ids *into* the generated SVG and throws if the same id is
 *    rendered twice, so each render gets a unique id.
 *
 * The graph source is read with textContent, never innerHTML. The engine's
 * to_mermaid escapes only `"` -> `'`, so labels can still contain SQL
 * fragments and dataset names that must not be parsed as markup.
 */
(function () {
  "use strict";

  window.QE = window.QE || {};

  var seq = 0;
  var ready = false;

  function configure() {
    if (ready || !window.mermaid) return ready;
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      fontFamily: '"Inter", system-ui, sans-serif',
      flowchart: { curve: "basis", padding: 14, htmlLabels: false, useMaxWidth: true },
      themeVariables: {
        background: "#0d1117",
        primaryColor: "#181d28",
        primaryBorderColor: "#323b4d",
        primaryTextColor: "#e8edf5",
        lineColor: "#5b8cff",
        secondaryColor: "#12161f",
        tertiaryColor: "#202634",
        mainBkg: "#181d28",
        nodeBorder: "#323b4d",
        clusterBkg: "#0d1117",
        edgeLabelBackground: "#0d1117",
        fontSize: "13px",
      },
    });
    ready = true;
    return true;
  }

  function render(el) {
    var source = (el.textContent || "").trim();
    if (!source) {
      el.setAttribute("data-rendered", "empty");
      return;
    }
    if (!configure()) return; // mermaid CDN unavailable; text plan stays visible

    el.setAttribute("data-rendered", "1");
    var id = "qe-plan-" + ++seq;

    try {
      var out = mermaid.render(id, source);
      // mermaid 11 returns a promise; 9/10 returned a string synchronously.
      if (out && typeof out.then === "function") {
        out.then(
          function (res) {
            el.innerHTML = res.svg;
          },
          function (err) {
            fail(el, source, err);
          }
        );
      } else if (typeof out === "string") {
        el.innerHTML = out;
      }
    } catch (err) {
      fail(el, source, err);
    }
  }

  // A diagram that will not parse must never take the results page with it:
  // fall back to the plain-text source, which is just as informative.
  function fail(el, source, err) {
    el.setAttribute("data-rendered", "error");
    el.textContent = "";
    var pre = document.createElement("pre");
    pre.className = "muted";
    pre.textContent = source;
    var note = document.createElement("p");
    note.className = "muted";
    note.style.fontSize = "12px";
    note.textContent = "Diagram could not be rendered; showing the graph source.";
    el.appendChild(note);
    el.appendChild(pre);
    if (window.console && err) console.warn("mermaid render failed", err);
  }

  function initPlan(root) {
    QE.els(root, ".mermaid").forEach(function (el) {
      if (el.hasAttribute("data-rendered")) return;
      render(el);
    });
  }

  QE.initPlan = initPlan;

  // A diagram inside a hidden tab panel measures as zero-width, so render it
  // when its panel is first shown instead.
  document.addEventListener("qe:tabshown", function (e) {
    if (e.detail && e.detail.panel) initPlan(e.detail.panel);
  });
})();
