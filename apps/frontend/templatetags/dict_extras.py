import math

from django import template
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def get_item(value, key):
    """Dict/list lookup in templates: {{ mydict|get_item:somekey }}."""
    if value is None:
        return None
    try:
        return value.get(key)
    except AttributeError:
        try:
            return value[key]
        except (KeyError, IndexError, TypeError):
            return None


@register.filter
def cell(value):
    """Render a result cell the way a SQL client would.

    `None` is a real SQL NULL, not a missing dict key, so it has to read as
    NULL rather than Python's `None` -- an all-NULL aggregate group is one of
    the things this engine goes out of its way to get right. Floats come back
    at full repr (10.418733437300153), which is noise in a results table.
    """
    if value is None:
        return mark_safe('<span class="null">NULL</span>')
    if isinstance(value, float):
        if math.isnan(value):
            return mark_safe('<span class="null">NaN</span>')
        return f"{round(value, 4):,}"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return value
