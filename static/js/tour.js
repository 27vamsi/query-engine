/* Guided tour of the workspace.
 *
 * Steps are declared in the markup, not here: any element carrying
 * data-tour-step="N" and data-tour-text="..." becomes a step. That keeps the
 * copy in the template with the thing it describes, so a renamed panel cannot
 * leave the tour pointing at nothing.
 *
 * Dismissal is remembered in localStorage, so a returning visitor is not
 * interrupted twice.
 */
(function () {
  "use strict";

  var KEY = "qe.tour.done";
  var active = null;

  function steps(root) {
    return QE.els(root, "[data-tour-step]").sort(function (a, b) {
      return (
        parseInt(a.getAttribute("data-tour-step"), 10) -
        parseInt(b.getAttribute("data-tour-step"), 10)
      );
    });
  }

  function build() {
    var host = document.createElement("div");
    host.className = "tour";
    host.setAttribute("role", "dialog");
    host.setAttribute("aria-modal", "false");
    host.setAttribute("aria-label", "Guided tour");
    host.innerHTML =
      '<div class="tour__count"></div>' +
      '<p class="tour__text"></p>' +
      '<div class="tour__actions">' +
      '<button class="btn btn--sm btn--ghost" type="button" data-tour-skip>Skip</button>' +
      '<span class="spacer"></span>' +
      '<button class="btn btn--sm btn--ghost" type="button" data-tour-back>Back</button>' +
      '<button class="btn btn--sm btn--primary" type="button" data-tour-next>Next</button>' +
      "</div>";
    document.body.appendChild(host);
    return host;
  }

  function place(host, target) {
    var r = target.getBoundingClientRect();
    var w = Math.min(330, window.innerWidth - 24);
    host.style.width = w + "px";

    var top = r.bottom + 10;
    // Flip above the target when there is no room below it.
    if (top + host.offsetHeight > window.innerHeight - 12) {
      top = Math.max(12, r.top - host.offsetHeight - 10);
    }
    var left = Math.min(Math.max(12, r.left), window.innerWidth - w - 12);
    host.style.top = top + "px";
    host.style.left = left + "px";
  }

  function stop(remember) {
    if (!active) return;
    active.targets.forEach(function (t) {
      t.classList.remove("is-tour-target");
    });
    active.host.remove();
    window.removeEventListener("resize", active.reposition);
    window.removeEventListener("scroll", active.reposition, true);
    document.removeEventListener("keydown", active.onKey);
    active = null;
    if (remember) {
      try {
        localStorage.setItem(KEY, "1");
      } catch (err) {
        /* private mode: the tour simply shows again next visit */
      }
    }
  }

  function show(i) {
    if (!active) return;
    var targets = active.targets;
    if (i < 0 || i >= targets.length) {
      stop(true);
      return;
    }
    active.index = i;
    var target = targets[i];

    targets.forEach(function (t) {
      t.classList.remove("is-tour-target");
    });
    target.classList.add("is-tour-target");
    target.scrollIntoView({ block: "nearest", behavior: "smooth" });

    var host = active.host;
    host.querySelector(".tour__count").textContent =
      "Step " + (i + 1) + " of " + targets.length;
    // textContent, never innerHTML: step copy comes from a template attribute
    // and must not be able to inject markup.
    host.querySelector(".tour__text").textContent =
      target.getAttribute("data-tour-text") || "";
    host.querySelector("[data-tour-back]").disabled = i === 0;
    host.querySelector("[data-tour-next]").textContent =
      i === targets.length - 1 ? "Finish" : "Next";

    place(host, target);
  }

  function start(root) {
    stop(false);
    var targets = steps(root || document);
    if (!targets.length) return;

    var host = build();
    active = {
      host: host,
      targets: targets,
      index: 0,
      reposition: function () {
        if (active) place(active.host, active.targets[active.index]);
      },
      onKey: function (e) {
        if (e.key === "Escape") stop(true);
        else if (e.key === "ArrowRight") show(active.index + 1);
        else if (e.key === "ArrowLeft") show(active.index - 1);
      }
    };

    host.querySelector("[data-tour-next]").addEventListener("click", function () {
      show(active.index + 1);
    });
    host.querySelector("[data-tour-back]").addEventListener("click", function () {
      show(active.index - 1);
    });
    host.querySelector("[data-tour-skip]").addEventListener("click", function () {
      stop(true);
    });

    window.addEventListener("resize", active.reposition);
    window.addEventListener("scroll", active.reposition, true);
    document.addEventListener("keydown", active.onKey);

    show(0);
    host.querySelector("[data-tour-next]").focus();
  }

  function initTour(root) {
    QE.els(root || document, "[data-tour-start]").forEach(function (btn) {
      if (!QE.claim(btn, "tour")) return;
      btn.addEventListener("click", function () {
        start(document);
      });
    });
  }

  window.QE = window.QE || {};
  QE.initTour = initTour;
  QE.startTour = start;

  document.addEventListener("DOMContentLoaded", function () {
    var seen = false;
    try {
      seen = localStorage.getItem(KEY) === "1";
    } catch (err) {
      seen = false;
    }
    // Auto-start only where the page asks for it, and only once ever.
    if (!seen && document.querySelector("[data-tour-auto]")) start(document);
  });
})();
