"""Public web facilities, loaded only when a caller requests them.

Importing ``web.security`` for pure URL checks does not import the crawler,
HTTP transport or DNS lookup implementation.
"""
from importlib import import_module

_EXPORTS = {
    'fetch_company': ('crawler', 'fetch_company'),
    'lookup_mx': ('dns', 'lookup_mx'),
    'WebError': ('errors', 'WebError'),
    'safe_http': ('http', 'safe_http'),
    'observation_context': ('observation', 'observation_context'),
    'observe_http': ('observation', 'observe_http'),
    'observe_lookup': ('observation', 'observe_lookup'),
    'current_lineage': ('observation', 'current_lineage'),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, symbol = _EXPORTS[name]
    value = getattr(import_module('.' + module, __name__), symbol)
    globals()[name] = value
    return value
