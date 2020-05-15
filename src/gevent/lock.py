# Copyright (c) 2009-2012 Denis Bilenko. See LICENSE for details.
"""
Locking primitives.

These include semaphores with arbitrary bounds (:class:`Semaphore` and
its safer subclass :class:`BoundedSemaphore`) and a semaphore with
infinite bounds (:class:`DummySemaphore`), along with a reentrant lock
(:class:`RLock`) with the same API as :class:`threading.RLock`.
"""
from __future__ import absolute_import

from gevent.hub import getcurrent
from gevent._compat import PURE_PYTHON
# This is the one exception to the rule of where to
# import Semaphore, obviously
from gevent import monkey
from gevent._semaphore import Semaphore
from gevent._semaphore import BoundedSemaphore


__all__ = [
    'Semaphore',
    'BoundedSemaphore',
    'DummySemaphore',
    'RLock',
]

# On PyPy, we don't compile the Semaphore class with Cython. Under
# Cython, each individual method holds the GIL for its entire
# duration, ensuring that no other thread can interrupt us in an
# unsafe state (only when we _do_wait do we call back into Python and
# allow switching threads). Simulate that here through the use of a manual
# lock. (We use a separate lock for each semaphore to allow sys.settrace functions
# to use locks *other* than the one being traced.) This, of course, must also
# hold for PURE_PYTHON mode when no optional C extensions are used.

_allocate_lock, _get_ident = monkey.get_original(
    ('_thread', 'thread'),
    ('allocate_lock', 'get_ident')
)


class _OwnedLock(object):
    __slots__ = (
        '_owner',
        '_block',
        '_locking',
        '_count',
    )

    def __init__(self):
        self._owner = None
        self._block = _allocate_lock()
        self._locking = {}
        self._count = 0

    # Don't allow re-entry to these functions in a single thread, as can
    # happen if a sys.settrace is used.

    def __begin(self):
        # Return (me, count) if we should proceed, otherwise return
        # None. The function should exit in that case.
        # In either case, it must call __end.
        me = _get_ident()
        try:
            count = self._locking[me]
        except KeyError:
            count = self._locking[me] = 1
        else:
            count = self._locking[me] = count + 1
        return (me, count) if not count else (None, None)

    def __end(self, me, count):
        if me is None:
            return
        count = count - 1
        if not count:
            del self._locking[me]
        else:
            self._locking[me] = count

    def __enter__(self):
        me, lock_count = self.__begin()
        try:
            if me is None:
                return

            if self._owner == me:
                self._count += 1
                return

            self._owner = me
            self._block.acquire()
            self._count = 1
        finally:
            self.__end(me, lock_count)

    def __exit__(self, t, v, tb):
        self.release()

    acquire = __enter__

    def release(self):
        me, lock_count = self.__begin()
        try:
            if me is None:
                return

            self._count = count = self._count - 1
            if not count:
                self._block.release()
                self._owner = None
        finally:
            self.__end(me, lock_count)


class _AtomicSemaphore(Semaphore):
    # Behaves as though the GIL was held for the duration of acquire, wait,
    # and release, just as if we were in Cython.
    #
    # acquire, wait, and release all acquire the lock on entry and release it
    # on exit. acquire and wait can call _wait, which must release it on entry
    # and re-acquire it for them on exit.
    #
    # Note that this does *NOT* make semaphores safe to use from multiple threads
    __slots__ = (
        '_lock_lock',
    )
    def __init__(self, *args, **kwargs):
        self._lock_lock = _OwnedLock()

        super(_AtomicSemaphore, self).__init__(*args, **kwargs)

    def _wait(self, *args, **kwargs):
        self._lock_lock.release()
        try:
            return super(_AtomicSemaphore, self)._wait(*args, **kwargs)
        finally:
            self._lock_lock.acquire()

    def release(self):
        with self._lock_lock:
            return super(_AtomicSemaphore, self).release()

    def acquire(self, blocking=True, timeout=None):
        with self._lock_lock:
            return super(_AtomicSemaphore, self).acquire(blocking, timeout)

    _py3k_acquire = acquire

    def wait(self, timeout=None):
        with self._lock_lock:
            return super(_AtomicSemaphore, self).wait(timeout)



if PURE_PYTHON:
    Semaphore = _AtomicSemaphore


class DummySemaphore(object):
    """
    DummySemaphore(value=None) -> DummySemaphore

    An object with the same API as :class:`Semaphore`,
    initialized with "infinite" initial value. None of its
    methods ever block.

    This can be used to parameterize on whether or not to actually
    guard access to a potentially limited resource. If the resource is
    actually limited, such as a fixed-size thread pool, use a real
    :class:`Semaphore`, but if the resource is unbounded, use an
    instance of this class. In that way none of the supporting code
    needs to change.

    Similarly, it can be used to parameterize on whether or not to
    enforce mutual exclusion to some underlying object. If the
    underlying object is known to be thread-safe itself mutual
    exclusion is not needed and a ``DummySemaphore`` can be used, but
    if that's not true, use a real ``Semaphore``.
    """

    # Internally this is used for exactly the purpose described in the
    # documentation. gevent.pool.Pool uses it instead of a Semaphore
    # when the pool size is unlimited, and
    # gevent.fileobject.FileObjectThread takes a parameter that
    # determines whether it should lock around IO to the underlying
    # file object.

    def __init__(self, value=None):
        """
        .. versionchanged:: 1.1rc3
            Accept and ignore a *value* argument for compatibility with Semaphore.
        """

    def __str__(self):
        return '<%s>' % self.__class__.__name__

    def locked(self):
        """A DummySemaphore is never locked so this always returns False."""
        return False

    def ready(self):
        """A DummySemaphore is never locked so this always returns True."""
        return True

    def release(self):
        """Releasing a dummy semaphore does nothing."""

    def rawlink(self, callback):
        # XXX should still work and notify?
        pass

    def unlink(self, callback):
        pass

    def wait(self, timeout=None): # pylint:disable=unused-argument
        """Waiting for a DummySemaphore returns immediately."""
        return 1

    def acquire(self, blocking=True, timeout=None):
        """
        A DummySemaphore can always be acquired immediately so this always
        returns True and ignores its arguments.

        .. versionchanged:: 1.1a1
           Always return *true*.
        """
        # pylint:disable=unused-argument
        return True

    def __enter__(self):
        pass

    def __exit__(self, typ, val, tb):
        pass


class RLock(object):
    """
    A mutex that can be acquired more than once by the same greenlet.

    A mutex can only be locked by one greenlet at a time. A single greenlet
    can `acquire` the mutex as many times as desired, though. Each call to
    `acquire` must be paired with a matching call to `release`.

    It is an error for a greenlet that has not acquired the mutex
    to release it.

    Instances are context managers.
    """

    __slots__ = (
        '_block',
        '_owner',
        '_count',
        '__weakref__',
    )

    def __init__(self):
        self._block = Semaphore(1)
        self._owner = None
        self._count = 0

    def __repr__(self):
        return "<%s at 0x%x _block=%s _count=%r _owner=%r)>" % (
            self.__class__.__name__,
            id(self),
            self._block,
            self._count,
            self._owner)

    def acquire(self, blocking=True, timeout=None):
        """
        Acquire the mutex, blocking if *blocking* is true, for up to
        *timeout* seconds.

        .. versionchanged:: 1.5a4
           Added the *timeout* parameter.

        :return: A boolean indicating whether the mutex was acquired.
        """
        me = getcurrent()
        if self._owner is me:
            self._count = self._count + 1
            return 1
        rc = self._block.acquire(blocking, timeout)
        if rc:
            self._owner = me
            self._count = 1
        return rc

    def __enter__(self):
        return self.acquire()

    def release(self):
        """
        Release the mutex.

        Only the greenlet that originally acquired the mutex can
        release it.
        """
        if self._owner is not getcurrent():
            raise RuntimeError("cannot release un-acquired lock")
        self._count = count = self._count - 1
        if not count:
            self._owner = None
            self._block.release()

    def __exit__(self, typ, value, tb):
        self.release()

    # Internal methods used by condition variables

    def _acquire_restore(self, count_owner):
        count, owner = count_owner
        self._block.acquire()
        self._count = count
        self._owner = owner

    def _release_save(self):
        count = self._count
        self._count = 0
        owner = self._owner
        self._owner = None
        self._block.release()
        return (count, owner)

    def _is_owned(self):
        return self._owner is getcurrent()
