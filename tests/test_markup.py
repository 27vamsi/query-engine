"""Structural guards on the rendered HTML.

These exist because of a bug that no other test could see. Django's ``{# #}``
comment syntax is *single-line only*; a ``{#`` that closes on a later line is
not a comment at all and the whole block is emitted verbatim into the page.
One such leaked comment in ``base.html`` contained the word ``<template>``,
which the browser parsed as a real element -- and because a ``<template>``'s
contents are inert, it swallowed the two ``<script>`` tags that followed it.
Every page still returned HTTP 200 with correct data while *all* JavaScript on
the site silently failed to run.

So: asserting on status codes and context is not enough. These tests parse the
markup the browser would actually receive.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.urls import reverse

from apps.catalog.models import Dataset

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "frontend" / "templates"

PAGES = [
    "dashboard",
    "workspace",
    "datasets",
    "history",
    "plans",
    "performance",
    "capabilities",
]

# Elements with no end tag. The SVG shapes matter: the icons are inline SVG.
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
    "circle", "ellipse", "line", "path", "polygon", "polyline", "rect",
    "stop", "use",
}


class TagBalance(HTMLParser):
    """Reports unclosed and stray tags with the line they occur on."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[tuple[str, int]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.open.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        pass  # self-closing, nothing to track

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.open:
            self.errors.append(f"line {self.getpos()[0]}: stray </{tag}>")
            return
        if self.open[-1][0] == tag:
            self.open.pop()
            return
        for i in range(len(self.open) - 1, -1, -1):
            if self.open[i][0] == tag:
                for unclosed, line in self.open[i + 1:]:
                    self.errors.append(
                        f"line {line}: <{unclosed}> never closed, found "
                        f"</{tag}> at line {self.getpos()[0]}"
                    )
                del self.open[i:]
                return
        self.errors.append(f"line {self.getpos()[0]}: stray </{tag}>")

    def problems(self) -> list[str]:
        return self.errors + [
            f"line {line}: <{tag}> never closed (end of document)"
            for tag, line in self.open
        ]


@pytest.fixture
def seeded(db):
    return Dataset.objects.create(name="trips", uri="./data/trips", description="s")


# --- Source-level guard ---------------------------------------------------


def test_no_multiline_hash_comments_in_any_template():
    """A multi-line {# #} renders as text. Catching it in the source is more
    useful than catching its symptoms, because the symptoms are arbitrary --
    it depends entirely on what the comment text happens to contain."""
    offenders = []
    for path in sorted(TEMPLATE_DIR.rglob("*.html")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            for match in re.finditer(r"\{#", line):
                if "#}" not in line[match.end():]:
                    rel = path.relative_to(TEMPLATE_DIR).as_posix()
                    offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "multi-line {# #} comments render as literal text; use "
        "{% comment %}...{% endcomment %} instead: " + ", ".join(offenders)
    )


# --- Rendered-output guards ----------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_emit_well_formed_markup(client, seeded, name):
    html = client.get(reverse(name)).content.decode()
    checker = TagBalance()
    checker.feed(html)
    assert not checker.problems(), f"{name}: " + "; ".join(checker.problems()[:8])


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_leak_no_template_syntax(client, seeded, name):
    """Unrendered template syntax in the output means a tag or comment was
    malformed, even when the page still looks mostly right."""
    html = client.get(reverse(name)).content.decode()
    for marker in ("{#", "#}", "{% comment", "{% endcomment", "{{", "{%"):
        assert marker not in html, f"{name} leaked {marker!r} into the response"


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_script_tags_are_not_trapped_inside_a_template_element(client, seeded, name):
    """The specific regression: scripts nested in a <template> never execute,
    so the whole UI goes inert while every test still passes."""
    html = client.get(reverse(name)).content.decode()
    depth = 0
    for token in re.finditer(r"<(/?)(template|script)\b", html, re.I):
        closing, tag = token.group(1), token.group(2).lower()
        if tag == "template":
            depth += -1 if closing else 1
        elif tag == "script" and not closing:
            assert depth == 0, (
                f"{name}: a <script> at offset {token.start()} is inside a "
                f"<template>, so it will never execute"
            )


def test_scrim_is_out_of_flow_outside_the_mobile_breakpoint():
    """.scrim sits inside .shell, which is a fixed 2-column grid. If it is ever
    in-flow at desktop width it becomes grid item #1, takes the sidebar column,
    pushes .sidebar into the content column and wraps the page onto row 2 at
    232px -- the whole layout in a narrow strip.

    Its only positioning used to live inside @media (max-width: 1024px), so
    every desktop viewport was broken. This asserts a base rule exists before
    the first @media block.
    """
    css = (Path(__file__).resolve().parent.parent
           / "static" / "css" / "shell.css").read_text(encoding="utf-8")
    base = css.split("@media")[0]
    match = re.search(r"\.scrim\s*\{([^}]*)\}", base)
    assert match, ".scrim has no rule outside a media query; it will be in-flow"
    body = match.group(1)
    assert re.search(r"display\s*:\s*none", body) or re.search(
        r"position\s*:\s*(fixed|absolute)", body
    ), f".scrim base rule must take it out of flow, got: {body.strip()!r}"


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_shell_has_exactly_one_sidebar_and_one_main(client, seeded, name):
    """Both grid columns must be filled by the elements that are supposed to
    fill them -- a duplicate or missing one shifts the entire page."""
    html = client.get(reverse(name)).content.decode()
    assert html.count('class="sidebar"') == 1
    assert html.count('class="main"') == 1


@pytest.mark.django_db
@pytest.mark.parametrize("name", PAGES)
def test_pages_load_the_shell_script(client, seeded, name):
    """shell.js owns the delegated htmx:afterSwap listener and the tab wiring;
    if it is missing the page renders but nothing responds to input."""
    html = client.get(reverse(name)).content.decode()
    assert "js/shell.js" in html
