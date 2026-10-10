"""Tracebacks as silk stores them: innermost frame first."""

# Recorded for a neomodel query issued from a DRF serializer method.
NEOMODEL = '''\
  File "/usr/local/lib/python3.11/site-packages/silk/sql.py", line 115, in execute_cypher
    tb = ''.join(reversed(traceback.format_stack()))
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/query.py", line 321, in _run_cypher_query
    response: Result = session.run(
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/query.py", line 280, in cypher_query
    results, meta = self._run_cypher_query(
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/connection.py", line 86, in wrapper
    return func(self, *args, **kwargs)
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/database.py", line 331, in cypher_query
    return self._query.cypher_query(
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/match.py", line 1299, in _count
    results, _ = db.cypher_query(query, self._query_params)
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/match.py", line 1465, in __bool__
    _count = ast._count()
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/relationship_manager.py", line 452, in __bool__
    return self._new_traversal().__bool__()
  File "/backend/training/serializers.py", line 114, in get_moves_from_parent
    if not obj.parent:
  File "/usr/local/lib/python3.11/site-packages/rest_framework/fields.py", line 1872, in to_representation
    return method(value)
  File "/usr/local/lib/python3.11/site-packages/rest_framework/serializers.py", line 538, in to_representation
    ret[field.field_name] = field.to_representation(attribute)
  File "/usr/local/lib/python3.11/site-packages/rest_framework/serializers.py", line 714, in <listcomp>
    self.child.to_representation(item) for item in iterable
  File "/usr/local/lib/python3.11/site-packages/rest_framework/serializers.py", line 249, in data
    self._data = self.to_representation(self.instance)
  File "/backend/training/views.py", line 133, in get
    ).data,
  File "/usr/local/lib/python3.11/site-packages/django/utils/decorators.py", line 190, in _view_wrapper
    response = view_func(request, *args, **kwargs)
  File "/usr/local/lib/python3.11/site-packages/rest_framework/views.py", line 506, in dispatch
    response = handler(request, *args, **kwargs)
  File "/usr/local/lib/python3.11/site-packages/django/views/generic/base.py", line 105, in view
    return self.dispatch(request, *args, **kwargs)
  File "/usr/local/lib/python3.11/contextlib.py", line 81, in inner
    return func(*args, **kwds)
  File "/usr/local/lib/python3.11/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request, *callback_args, **callback_kwargs)
  File "/usr/local/lib/python3.11/site-packages/silk/middleware.py", line 74, in __call__
    response = self.get_response(request)
  File "/usr/local/lib/python3.11/site-packages/django/core/handlers/wsgi.py", line 124, in __call__
    response = self.get_response(request)
  File "/usr/local/lib/python3.11/wsgiref/handlers.py", line 137, in run
    self.result = application(self.environ, self.start_response)
  File "/usr/local/lib/python3.11/socketserver.py", line 755, in __init__
    self.handle()
  File "/usr/local/lib/python3.11/threading.py", line 1002, in _bootstrap
    self._bootstrap_inner()
'''

# The same query issued straight from the view: library frames, the view, Django frames.
NEOMODEL_FROM_VIEW = '''\
  File "/usr/local/lib/python3.11/site-packages/silk/sql.py", line 115, in execute_cypher
    tb = ''.join(reversed(traceback.format_stack()))
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/query.py", line 321, in _run_cypher_query
    response: Result = session.run(
  File "/usr/local/lib/python3.11/site-packages/neomodel/sync_/database.py", line 331, in cypher_query
    return self._query.cypher_query(
  File "/backend/training/views.py", line 128, in get
    rows, _ = db.cypher_query(ROOT_OPENINGS)
  File "/usr/local/lib/python3.11/site-packages/django/utils/decorators.py", line 190, in _view_wrapper
    response = view_func(request, *args, **kwargs)
  File "/usr/local/lib/python3.11/site-packages/django/views/generic/base.py", line 105, in view
    return self.dispatch(request, *args, **kwargs)
  File "/usr/local/lib/python3.11/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request, *callback_args, **callback_kwargs)
'''

# Recorded by another image of the same project, on a newer Python that marks
# the failing expression under the source line.
OTHER_PYTHON = '''\
  File "/usr/local/lib/python3.14/site-packages/silk/sql.py", line 82, in execute_sql
    tb = ''.join(reversed(traceback.format_stack()))
  File "/usr/local/lib/python3.14/site-packages/django/db/models/query.py", line 93, in __iter__
    results = compiler.execute_sql(
              ~~~~~~~~~~~~~~~~~~~~^
  File "/usr/local/lib/python3.14/site-packages/django/db/models/query.py", line 631, in get
    num = len(clone)
  File "/backend/users/services.py", line 41, in current_rating
    return Rating.objects.get(user=user)
           ~~~~~~~~~~~~~~~~~~^^^^^^^^^^^
  File "/usr/local/lib/python3.14/site-packages/django/core/handlers/base.py", line 197, in _get_response
    response = wrapped_callback(request, *callback_args, **callback_kwargs)
  File "/usr/local/lib/python3.14/threading.py", line 1044, in _bootstrap
    self._bootstrap_inner()
'''

LIBRARY_ONLY = '''\
  File "/usr/local/lib/python3.14/site-packages/silk/sql.py", line 82, in execute_sql
    tb = ''.join(reversed(traceback.format_stack()))
  File "/usr/local/lib/python3.14/site-packages/django/contrib/sites/models.py", line 30, in _get_site_by_id
    site = self.get(pk=site_id)
  File "/usr/lib/python3/dist-packages/gunicorn/workers/sync.py", line 176, in handle_request
    respiter = self.wsgi(environ, resp.start_response)
  File "<frozen runpy>", line 198, in _run_module_as_main
  File "/usr/local/lib/python3.14/threading.py", line 1044, in _bootstrap
    self._bootstrap_inner()
'''

# silk installed from a checkout, so its frames are not under site-packages.
SILK_CHECKOUT = '''\
  File "/src/django-silk-neo4j/silk/sql.py", line 82, in execute_sql
    tb = ''.join(reversed(traceback.format_stack()))
  File "/usr/local/lib/python3.14/site-packages/django/db/models/query.py", line 93, in __iter__
    results = compiler.execute_sql(
  File "/backend/training/views.py", line 133, in get
    ).data,
  File "/src/django-silk-neo4j/silk/middleware.py", line 74, in __call__
    response = self.get_response(request)
'''
