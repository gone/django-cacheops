import threading
import warnings

from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from funcy import decorator, identity, memoize, omit
import redis
from redis.sentinel import Sentinel
from .conf import settings


_lazy_setup_lock = threading.Lock()


def _lazy_setup(lazy):
    # Morph `lazy` into the object returned by its init function. Guarded by a
    # lock and made idempotent by popping `_init`, so concurrent first-access
    # from threaded workers can't observe a half-morphed object. The lock lives
    # at module scope (not on the instance) so it survives the morph, and we
    # never route through `lazy._setup()` — once `__class__` is swapped that
    # attribute is gone, which is the original race.
    with _lazy_setup_lock:
        init = lazy.__dict__.pop('_init', None)
        if init is None:
            return  # another thread already morphed us
        wrapped = init()
        object.__setattr__(lazy, '__dict__', wrapped.__dict__)
        object.__setattr__(lazy, '__class__', wrapped.__class__)


class LazyObject:
    """A thread-safe lazy init object that rewrites itself on first access.

    Drop-in for funcy.LazyObject. funcy's version swaps __class__ then __dict__
    in __getattr__ without locking; under threaded workers two threads racing on
    first access make one see a half-morphed object and raise
    "'X' object has no attribute '_setup'". See _lazy_setup for the guard.
    """

    def __init__(self, init):
        self.__dict__['_init'] = init

    def __getattr__(self, name):
        _lazy_setup(self)
        return getattr(self, name)

    def __setattr__(self, name, value):
        _lazy_setup(self)
        return setattr(self, name, value)


@decorator
def _handle_connection_failure(call):
    try:
        return call()
    except redis.ConnectionError as e:
        warnings.warn("The cacheops cache is unreachable! Error: %s" % e, RuntimeWarning)
    except redis.TimeoutError as e:
        warnings.warn("The cacheops cache timed out! Error: %s" % e, RuntimeWarning)

handle_connection_failure = _handle_connection_failure if settings.CACHEOPS_DEGRADE_ON_FAILURE \
    else identity


@LazyObject
def redis_client():
    if settings.CACHEOPS_REDIS and settings.CACHEOPS_SENTINEL:
        raise ImproperlyConfigured("CACHEOPS_REDIS and CACHEOPS_SENTINEL are mutually exclusive")

    client_class = redis.Redis
    if settings.CACHEOPS_CLIENT_CLASS:
        client_class = import_string(settings.CACHEOPS_CLIENT_CLASS)

    if settings.CACHEOPS_SENTINEL:
        if not {'locations', 'service_name'} <= set(settings.CACHEOPS_SENTINEL):
            raise ImproperlyConfigured("Specify locations and service_name for CACHEOPS_SENTINEL")

        sentinel = Sentinel(
            settings.CACHEOPS_SENTINEL['locations'],
            **omit(settings.CACHEOPS_SENTINEL, ('locations', 'service_name', 'db')))
        return sentinel.master_for(
            settings.CACHEOPS_SENTINEL['service_name'],
            redis_class=client_class,
            db=settings.CACHEOPS_SENTINEL.get('db', 0)
        )

    # Allow client connection settings to be specified by a URL.
    if isinstance(settings.CACHEOPS_REDIS, str):
        return client_class.from_url(settings.CACHEOPS_REDIS)
    else:
        return client_class(**settings.CACHEOPS_REDIS)


### Lua script loader

import os.path
import re


@memoize
def load_script(name):
    filename = os.path.join(os.path.dirname(__file__), 'lua/%s.lua' % name)
    with open(filename) as f:
        code = f.read()
    if is_redis_7():
        code = re.sub(r'REDIS_4.*?/REDIS_4', '', code, flags=re.S)
    else:
        code = re.sub(r'REDIS_7.*?/REDIS_7', '', code, flags=re.S)
    return redis_client.register_script(code)


@memoize
def is_redis_7():
    # Some arcane redis version may return X.X, which redis-py turns into float
    redis_version = str(redis_client.info('server')['redis_version'])
    return int(redis_version.split('.')[0]) >= 7
