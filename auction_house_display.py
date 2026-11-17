"""User-designated display aliases, not verified API faction/neutral mappings."""

_HOUSE_ALIASES = {113: '호드 경매장', 114: '중립 경매장'}


def house_label(house_id, fallback=None):
    """Aliases take precedence over API/cache names; retain unknown fallbacks.

    Keep IDs unchanged in report data. HTML callers must escape this plain text.
    """
    alias = _HOUSE_ALIASES.get(house_id) if type(house_id) is int else None
    if alias is not None:
        return f'{alias} (#{house_id})'
    return fallback if fallback is not None else f'#{house_id}'
