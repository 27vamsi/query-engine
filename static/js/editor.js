/* CodeMirror 5 SQL editor.
 *
 * CM5 rather than CM6 on purpose: CM6 is ESM-only and would need a bundler,
 * and this project is deliberately build-free. CM5 ships every feature used
 * here from two script tags.
 *
 * The underlying <textarea> stays a real form field, so the page still works
 * with JS disabled and `submit_query` keeps receiving `sql` either way.
 */
(function () {
  "use strict";

  window.QE = window.QE || {};

  function initEditor(root) {
    QE.els(root, "[data-sql-editor]").forEach(function (host) {
      if (!QE.claim(host, "sql-editor")) return;

      var textarea = host.querySelector("textarea");
      if (!textarea) return;

      var form = textarea.form;
      var cm = null;

      if (window.CodeMirror) {
        cm = CodeMirror.fromTextArea(textarea, {
          mode: "text/x-sql",
          lineNumbers: true,
          lineWrapping: true,
          matchBrackets: true,
          styleActiveLine: true,
          indentUnit: 2,
          tabSize: 2,
          smartIndent: true,
          autofocus: false,
          extraKeys: {
            "Ctrl-Enter": submit,
            "Cmd-Enter": submit,
            "Shift-Tab": "indentLess",
            Tab: function (editor) {
              if (editor.somethingSelected()) editor.indentSelection("add");
              else editor.replaceSelection("  ", "end");
            },
          },
        });

        // CM mirrors into the textarea on form submit, but htmx reads form
        // values itself without firing that hook, so sync explicitly.
        cm.on("change", function () {
          cm.save();
        });
      }

      function submit() {
        if (!form) return;
        if (cm) cm.save();
        if (window.htmx) htmx.trigger(form, "submit");
        else form.submit();
      }

      function setValue(sql) {
        if (cm) {
          cm.setValue(sql);
          cm.focus();
          cm.setCursor(cm.lineCount(), 0);
          cm.save();
        } else {
          textarea.value = sql;
          textarea.focus();
        }
      }

      function insert(token) {
        if (cm) {
          cm.replaceSelection(token);
          cm.focus();
          cm.save();
        } else {
          textarea.value += token;
          textarea.focus();
        }
      }

      function getValue() {
        return cm ? cm.getValue() : textarea.value;
      }

      // Expose the editor on its host so sibling controls (examples, column
      // list, clear/copy) can drive it without reaching into CodeMirror.
      host.qeEditor = { setValue: setValue, insert: insert, getValue: getValue, submit: submit };

      // Ctrl/Cmd+Enter from anywhere in the workspace, not just the editor.
      var scope = host.closest("[data-editor-scope]") || document;
      scope.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
          e.preventDefault();
          submit();
        }
      });
    });

    bindEditorControls(root);
  }

  function editorFor(el) {
    var scope = el.closest("[data-editor-scope]") || document;
    var host = scope.querySelector("[data-sql-editor]");
    return host && host.qeEditor ? host.qeEditor : null;
  }

  function bindEditorControls(root) {
    // Example queries and column names are passed as data-* attributes read
    // with .dataset / getAttribute, never interpolated into a JS literal.
    QE.els(root, "[data-set-sql]").forEach(function (btn) {
      if (!QE.claim(btn, "set-sql")) return;
      btn.addEventListener("click", function () {
        var ed = editorFor(btn);
        if (!ed) return;
        ed.setValue(btn.getAttribute("data-set-sql"));
        if (btn.hasAttribute("data-run-now")) ed.submit();
      });
    });

    QE.els(root, "[data-insert-token]").forEach(function (btn) {
      if (!QE.claim(btn, "insert-token")) return;
      btn.addEventListener("click", function () {
        var ed = editorFor(btn);
        if (ed) ed.insert(btn.getAttribute("data-insert-token"));
      });
    });

    QE.els(root, "[data-editor-clear]").forEach(function (btn) {
      if (!QE.claim(btn, "editor-clear")) return;
      btn.addEventListener("click", function () {
        var ed = editorFor(btn);
        if (ed) ed.setValue("");
      });
    });

    QE.els(root, "[data-editor-copy]").forEach(function (btn) {
      if (!QE.claim(btn, "editor-copy")) return;
      btn.addEventListener("click", function () {
        var ed = editorFor(btn);
        if (!ed) return;
        QE.copyText(ed.getValue()).then(function (ok) {
          QE.toast(ok ? "SQL copied" : "Could not copy", ok ? "good" : "bad");
        });
      });
    });

    // htmx reads form fields at request time; make sure CM has flushed.
    QE.els(root, "[data-editor-scope] form").forEach(function (form) {
      if (!QE.claim(form, "editor-sync")) return;
      form.addEventListener("htmx:configRequest", function (e) {
        var host = (form.closest("[data-editor-scope]") || document).querySelector(
          "[data-sql-editor]"
        );
        if (host && host.qeEditor) e.detail.parameters.sql = host.qeEditor.getValue();
      });
    });
  }

  QE.initEditor = initEditor;
})();
