"""Content-keyed, read-only HTML views scoped to one company and worker.

Callers must not mutate returned trees. Cleaned projections have separate
trees, so removing scripts never destroys identity, forms or JSON-LD evidence.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import time

from bs4 import BeautifulSoup

_scope = ContextVar('company_html_cache', default=None)
MAX_ENTRIES = 32
MAX_CHARACTERS = 4 * 1024 * 1024


@contextmanager
def parsing_scope():
    state = {'trees': OrderedDict(), 'characters': 0, 'html_parse_count': 0,
             'html_cache_hits': 0, 'html_parse_seconds': 0.0}
    token = _scope.set(state)
    try:
        yield state
    finally:
        state['trees'].clear()
        _scope.reset(token)


def parsed_html(html, *, remove=(), hidden=False):
    """Return an immutable-by-contract tree; standalone calls remain uncached."""
    html = html or ''
    key = (html, tuple(remove), hidden)
    state = _scope.get()
    if state is not None and key in state['trees']:
        state['html_cache_hits'] += 1
        state['trees'].move_to_end(key)
        return state['trees'][key]
    started = time.perf_counter()
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(list(remove)) if remove else ():
        node.decompose()
    if hidden:
        for node in soup.select('[hidden], [aria-hidden="true"]'):
            node.decompose()
    if state is not None:
        state['html_parse_count'] += 1
        state['html_parse_seconds'] += time.perf_counter() - started
        # Account every projection separately. Oversized trees are not retained.
        if len(html) <= MAX_CHARACTERS:
            while state['trees'] and (len(state['trees']) >= MAX_ENTRIES or
                                      state['characters'] + len(html) > MAX_CHARACTERS):
                old, _ = state['trees'].popitem(last=False)
                state['characters'] -= len(old[0])
            state['trees'][key] = soup
            state['characters'] += len(html)
    return soup
