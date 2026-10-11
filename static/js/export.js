/* Copy / download result sets.
 *
 * Exports the RAW values the engine returned, read from a
 * {{ payload|json_script:"id" }} tag -- not the rendered table text. The
 * rendered table is deliberately lossy: dict_extras.cell prints NULL for a
 * real SQL NULL and rounds floats to 4 places for legibility. Scraping the
 * DOM would bake that formatting into the export.
 *
 * A NULL therefore leaves as an empty CSV field (the CSV convention for
 * "absent") and as `null` in JSON, not the string "NULL".
 */
(function () {
  "use strict";

  function payloadFor(el) {
    var id = el.getAttribute("data-export-source");
    var node = id && document.getElementById(id);
    if (!node) return null;
    try {
      var data = JSON.parse(node.textContent);
      if (!data || !data.columns || !data.rows) return null;
      return data;
    } catch (err) {
      return null;
    }
  }

  /* RFC 4180: quote when the value contains a comma, quote or newline, and
   * escape an embedded quote by doubling it. */
  function csvField(v) {
    if (v === null || v === undefined) return "";
    var s = String(v);
    if (/[",\r\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function toCSV(data) {
    var lines = [data.columns.map(csvField).join(",")];
    data.rows.forEach(function (row) {
      lines.push(
        data.columns
          .map(function (c) {
            return csvField(row[c]);
          })
          .join(",")
      );
    });
    // CRLF, so the file opens correctly in Excel on Windows.
    return lines.join("\r\n");
  }

  function toJSON(data) {
    return JSON.stringify(data.rows, null, 2);
  }

  function download(text, filename, mime) {
    var blob = new Blob([text], { type: mime + ";charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // Revoking immediately can cancel the download in some builds; one tick
    // is enough for the navigation to have started.
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 1000);
  }

  function initExport(root) {
    QE.els(root || document, "[data-export]").forEach(function (btn) {
      if (!QE.claim(btn, "export")) return;
      btn.addEventListener("click", function () {
        var data = payloadFor(btn);
        if (!data) {
          QE.toast("Nothing to export", "warn");
          return;
        }
        var kind = btn.getAttribute("data-export");
        var name = btn.getAttribute("data-export-name") || "result";
        if (kind === "json") {
          download(toJSON(data), name + ".json", "application/json");
        } else {
          download(toCSV(data), name + ".csv", "text/csv");
        }
        QE.toast(
          data.rows.length + " row" + (data.rows.length === 1 ? "" : "s") +
            " exported as " + kind.toUpperCase(),
          "good"
        );
      });
    });
  }

  window.QE = window.QE || {};
  QE.initExport = initExport;
})();
