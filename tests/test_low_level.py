import threading

import pytest

from cacheops.redis import LazyObject, redis_client

from .models import User
from .utils import BaseTestCase


def test_lazy_object_concurrent_first_access():
    """Concurrent first access must morph exactly once and never raise.

    Regression for the funcy LazyObject race where one thread observed a
    half-morphed object and hit "'X' object has no attribute '_setup'".
    """
    init_count = []
    start = threading.Barrier(16)

    class Wrapped:
        value = 42

    def init():
        init_count.append(1)
        return Wrapped()

    lazy = LazyObject(init)
    errors, results = [], []

    def worker():
        start.wait()  # release all threads into first-access at once
        try:
            results.append(lazy.value)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert results == [42] * 16
    assert sum(init_count) == 1
    assert lazy.__class__ is Wrapped


@pytest.fixture()
def base(db):
    case = BaseTestCase()
    case.setUp()
    yield
    case.tearDown()


def test_ttl(base):
    user = User.objects.create(username='Suor')
    qs = User.objects.cache(timeout=100).filter(pk=user.pk)
    list(qs)
    assert 90 <= redis_client.ttl(qs._cache_key()) <= 100
    assert redis_client.ttl(f'{qs._prefix}conj:auth_user:id={user.id}') > 100
