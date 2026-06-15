from django import template

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
