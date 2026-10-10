from datetime import timedelta

import factory
import factory.fuzzy
from django.utils import timezone
from example_app.models import Blind

from silk.models import Profile, Request, Response, SQLQuery

HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'HEAD', 'OPTIONS']
STATUS_CODES = [200, 201, 300, 301, 302, 401, 403, 404]


class SQLQueryFactory(factory.django.DjangoModelFactory):

    query = factory.Sequence(lambda num: 'SELECT foo FROM bar WHERE foo=%s' % num)
    traceback = factory.Sequence(lambda num: 'Traceback #%s' % num)

    class Meta:
        model = SQLQuery


class RequestMinFactory(factory.django.DjangoModelFactory):

    path = factory.Faker('uri_path')
    method = factory.fuzzy.FuzzyChoice(HTTP_METHODS)

    class Meta:
        model = Request


class ResponseFactory(factory.django.DjangoModelFactory):
    request = factory.SubFactory(RequestMinFactory)
    status_code = factory.fuzzy.FuzzyChoice(STATUS_CODES)

    class Meta:
        model = Response


class ProfileFactory(factory.django.DjangoModelFactory):
    name = factory.Sequence(lambda num: 'block-%s' % num)
    func_name = 'do_work'
    file_path = '/app/example_app/views.py'
    line_num = 10

    class Meta:
        model = Profile


def recorded_request(time_ms=100, status_code=200, start_time=None, **kwargs):
    """A request as the middleware leaves it: timed, with a response unless `status_code` is None."""
    start_time = start_time or timezone.now()
    end_time = None if time_ms is None else start_time + timedelta(milliseconds=time_ms)
    request = RequestMinFactory(start_time=start_time, end_time=end_time, **kwargs)
    if status_code is not None:
        ResponseFactory(request=request, status_code=status_code)
    # Reloaded, so that the id is the stored string and not a UUID object.
    return Request.objects.get(pk=request.pk)


def timed_query(request, query, time_ms, offset_ms=0, **kwargs):
    """A query of `request` that started `offset_ms` into it and took `time_ms`."""
    start_time = request.start_time + timedelta(milliseconds=offset_ms)
    end_time = None if time_ms is None else start_time + timedelta(milliseconds=time_ms)
    return SQLQueryFactory(request=request, query=query, start_time=start_time, end_time=end_time, **kwargs)


def timed_profile(request, time_ms, queries=(), **kwargs):
    """A silk_profile block of `request` that took `time_ms` and ran `queries`."""
    end_time = request.start_time + timedelta(milliseconds=time_ms)
    profile = ProfileFactory(request=request, start_time=request.start_time, end_time=end_time, **kwargs)
    profile.queries.set(queries)
    return profile


class BlindFactory(factory.django.DjangoModelFactory):
    name = factory.Faker('pystr', min_chars=5, max_chars=10)
    child_safe = factory.Faker('pybool')
    photo = factory.django.ImageField()

    class Meta:
        model = Blind
