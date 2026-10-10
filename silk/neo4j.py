"""Records Cypher queries run through the official neo4j Python driver.

The driver is optional: if it is not installed nothing is patched.
"""
import functools
import logging
import re
import traceback
from contextlib import contextmanager

from django.utils import timezone

from silk.collector import DataCollector
from silk.config import SilkyConfig
from silk.sql import _should_wrap

Logger = logging.getLogger('silk.neo4j')

# Every public driver class that can run a query. Session.run (auto-commit) does not
# go through Transaction.run, so wrapping all of them does not record a query twice.
_SYNC_CLASSES = ('Session', 'Transaction', 'ManagedTransaction')
_ASYNC_CLASSES = ('AsyncSession', 'AsyncTransaction', 'AsyncManagedTransaction')

_PARAMETER_RE = re.compile(r'\$(\w+)')


def _cypher_literal(value):
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'%s'" % value.replace('\\', '\\\\').replace("'", "\\'")
    if isinstance(value, (list, tuple, set, frozenset)):
        return '[%s]' % ', '.join(_cypher_literal(v) for v in value)
    if isinstance(value, dict):
        return '{%s}' % ', '.join(f'{k}: {_cypher_literal(v)}' for k, v in value.items())
    return str(value)


def render_cypher(query, parameters=None, kwparameters=None):
    """Returns the query text with its parameters inlined, for display only."""
    text = str(getattr(query, 'text', query))
    params = dict(parameters or {}, **(kwparameters or {}))
    if not params:
        return text

    def replace(match):
        name = match.group(1)
        if name in params:
            return _cypher_literal(params[name])
        return match.group(0)

    return _PARAMETER_RE.sub(replace, text)


@contextmanager
def _capture(query, parameters, kwparameters):
    if not DataCollector().request:
        yield
        return
    try:
        cypher_query = render_cypher(query, parameters, kwparameters)
    except Exception:  # never let profiling break the query itself
        Logger.debug('Could not render cypher parameters', exc_info=True)
        cypher_query = str(getattr(query, 'text', query))
    if not _should_wrap(cypher_query):
        yield
        return
    query_dict = {
        'query': cypher_query,
        'start_time': timezone.now(),
        'traceback': ''.join(reversed(traceback.format_stack())),
        'analysis': None,
    }
    try:
        yield
    finally:
        query_dict['end_time'] = timezone.now()
        request = DataCollector().request
        if request:
            query_dict['request'] = request
            DataCollector().register_query(query_dict)


def _wrap_sync(original):
    @functools.wraps(original)
    def run(self, query, parameters=None, **kwparameters):
        with _capture(query, parameters, kwparameters):
            return original(self, query, parameters, **kwparameters)
    run._silk_original = original
    return run


def _wrap_async(original):
    @functools.wraps(original)
    async def run(self, query, parameters=None, **kwparameters):
        with _capture(query, parameters, kwparameters):
            return await original(self, query, parameters, **kwparameters)
    run._silk_original = original
    return run


def _patch(cls, wrap):
    # Transaction and ManagedTransaction inherit run() from a shared base class,
    # so patch the class that actually defines it, and only once.
    owner = next((klass for klass in cls.__mro__ if 'run' in vars(klass)), None)
    if owner is None or hasattr(vars(owner)['run'], '_silk_original'):
        return
    owner.run = wrap(vars(owner)['run'])


def patch_neo4j():
    """Wraps the neo4j driver's run() methods. Safe to call repeatedly."""
    if not SilkyConfig().SILKY_NEO4J:
        return
    try:
        import neo4j
    except ImportError:
        return
    for names, wrap in ((_SYNC_CLASSES, _wrap_sync), (_ASYNC_CLASSES, _wrap_async)):
        for name in names:
            cls = getattr(neo4j, name, None)
            if cls is not None:
                _patch(cls, wrap)
