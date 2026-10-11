/* Chart.js rendering.
 *
 * Data never reaches this file as a JS literal -- every chart reads its points
 * from a {{ value|json_script:"id" }} tag, so nothing user-influenced is ever
 * parsed as code. A canvas declares what it wants:
 *
 *   <canvas data-chart="duration" data-src="duration-data"></canvas>
 *
 * Guarded by QE.claim so an htmx re-swap cannot stack a second Chart instance
 * on the same canvas (Chart.js leaks the old one and the tooltips double up).
 */
(function () {
  "use strict";

  function readJSON(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (err) {
      return null;
    }
  }

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || "").trim() || fallback;
  }

  function palette() {
    return {
      accent: css("--c-accent", "#5b8cff"),
      read: css("--viz-read", "#5b8cff"),
      saved: css("--viz-saved", "#2a3140"),
      good: css("--c-good", "#3fd17f"),
      grid: css("--c-border", "#242b38"),
      text: css("--c-text-mute", "#8b97ab"),
      surface: css("--c-raised", "#141922")
    };
  }

  function baseOptions(p) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 220 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: false,
          labels: { color: p.text, boxWidth: 10, boxHeight: 10 }
        },
        tooltip: {
          backgroundColor: p.surface,
          borderColor: p.grid,
          borderWidth: 1,
          titleColor: css("--c-text", "#e8edf5"),
          bodyColor: p.text,
          padding: 10,
          displayColors: true,
          cornerRadius: 6
        }
      },
      scales: {
        x: {
          grid: { color: p.grid, drawTicks: false },
          border: { color: p.grid },
          ticks: { color: p.text, maxRotation: 0, autoSkipPadding: 18, font: { size: 10 } }
        },
        y: {
          beginAtZero: true,
          grid: { color: p.grid, drawTicks: false },
          border: { display: false },
          ticks: { color: p.text, font: { size: 10 } }
        }
      }
    };
  }

  function bytes(n) {
    if (n === null || n === undefined) return "n/a";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    var v = Number(n);
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i += 1;
    }
    return (i === 0 ? v : v.toFixed(1)) + " " + units[i];
  }

  /* --- builders ---------------------------------------------------------- */

  var builders = {
    /* Latency over the recent run window. */
    duration: function (canvas, data) {
      var p = palette();
      var opts = baseOptions(p);
      opts.scales.y.ticks.callback = function (v) {
        return v + " ms";
      };
      opts.plugins.tooltip.callbacks = {
        title: function (items) {
          return "Run #" + items[0].label;
        },
        label: function (item) {
          return item.parsed.y + " ms";
        }
      };
      return new Chart(canvas, {
        type: "line",
        data: {
          labels: data.map(function (d) {
            return d.id;
          }),
          datasets: [
            {
              data: data.map(function (d) {
                return d.ms;
              }),
              borderColor: p.accent,
              backgroundColor: "rgba(91,140,255,0.14)",
              borderWidth: 2,
              pointRadius: 0,
              pointHoverRadius: 4,
              tension: 0.3,
              fill: true
            }
          ]
        },
        options: opts
      });
    },

    /* Bytes read vs bytes the same query could have read, stacked so the
     * column height is always the dataset size and the blue part is the cost. */
    scan: function (canvas, data) {
      var p = palette();
      var opts = baseOptions(p);
      opts.scales.x.stacked = true;
      opts.scales.y.stacked = true;
      opts.scales.y.ticks.callback = bytes;
      opts.plugins.legend.display = true;
      opts.plugins.tooltip.callbacks = {
        title: function (items) {
          return "Run #" + items[0].label;
        },
        label: function (item) {
          return item.dataset.label + ": " + bytes(item.parsed.y);
        },
        footer: function (items) {
          var row = data[items[0].dataIndex];
          return row ? row.ratio + "% of dataset bytes read" : "";
        }
      };
      return new Chart(canvas, {
        type: "bar",
        data: {
          labels: data.map(function (d) {
            return d.id;
          }),
          datasets: [
            {
              label: "Read",
              data: data.map(function (d) {
                return d.read;
              }),
              backgroundColor: p.read,
              borderWidth: 0,
              borderRadius: 2
            },
            {
              label: "Avoided",
              data: data.map(function (d) {
                return d.saved;
              }),
              backgroundColor: p.saved,
              borderWidth: 0,
              borderRadius: 2
            }
          ]
        },
        options: opts
      });
    },

    /* Wall-clock per benchmarked query shape. Benchmark data from
       benchmark_results.json, not live telemetry. */
    benchmark: function (canvas, data) {
      var p = palette();
      var opts = baseOptions(p);
      opts.indexAxis = "y";
      opts.scales.x.ticks.callback = function (v) {
        return v + " ms";
      };
      opts.scales.y.grid.display = false;
      opts.plugins.legend.display = false;
      opts.plugins.tooltip.callbacks = {
        label: function (item) {
          return item.parsed.x + " ms";
        }
      };
      return new Chart(canvas, {
        type: "bar",
        data: {
          labels: data.map(function (d) {
            return d.label;
          }),
          datasets: [
            {
              label: "Wall clock",
              data: data.map(function (d) {
                return d.engine_ms;
              }),
              backgroundColor: p.accent,
              borderWidth: 0,
              borderRadius: 3
            }
          ]
        },
        options: opts
      });
    }
  };

  /* --- init -------------------------------------------------------------- */

  function initCharts(root) {
    if (typeof Chart === "undefined") return;
    QE.els(root || document, "canvas[data-chart]").forEach(function (canvas) {
      if (!QE.claim(canvas, "chart")) return;
      var build = builders[canvas.getAttribute("data-chart")];
      var data = readJSON(canvas.getAttribute("data-src"));
      if (!build || !data || !data.length) return;
      try {
        build(canvas, data);
      } catch (err) {
        // A failed chart must not take the page with it. Every chart on this
        // site sits next to a table of the same numbers, so hiding the canvas
        // loses the visual and nothing else.
        canvas.hidden = true;
      }
    });
  }

  window.QE = window.QE || {};
  QE.initCharts = initCharts;

  // Charts live in tab panels that start hidden; a canvas sized at 0x0 renders
  // blank, so re-run on the tab-shown event shell.js dispatches.
  document.addEventListener("qe:tabshown", function (e) {
    initCharts(e.detail && e.detail.panel ? e.detail.panel : document);
  });
})();
