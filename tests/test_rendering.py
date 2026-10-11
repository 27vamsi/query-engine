"""Result cells must read like a SQL client, not a Python dict dump.

The engine goes to some trouble to return a real NULL for an all-NULL
aggregate group (see test_nulls.py). That is worth nothing if the UI prints
`None`, so the rendering is pinned here too.
"""

from apps.frontend.templatetags.dict_extras import cell


def test_null_renders_as_null():
    assert "NULL" in cell(None)
    assert "None" not in cell(None)


def test_nan_is_distinguishable_from_null():
    assert "NaN" in cell(float("nan"))


def test_floats_are_rounded_and_grouped():
    assert cell(10.418733437300153) == "10.4187"
    assert cell(928632.13) == "928,632.13"


def test_ints_are_grouped():
    assert cell(300000) == "300,000"


def test_strings_pass_through_unescaped_for_django_to_escape():
    # Returning a plain str (not mark_safe) keeps Django's autoescaping in
    # charge, so a value like <b> cannot inject markup.
    assert cell("<b>EU</b>") == "<b>EU</b>"
