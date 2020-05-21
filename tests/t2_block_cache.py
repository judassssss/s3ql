#!/usr/bin/env python3
'''
t2_block_cache.py - this file is part of S3QL.

Copyright © 2008 Nikolaus Rath <Nikolaus@rath.org>

This work can be distributed under the terms of the GNU GPLv3.
'''

if __name__ == '__main__':
    import pytest
    import sys
    sys.exit(pytest.main([__file__] + sys.argv[1:]))

from contextlib import contextmanager
from s3ql.backends import local
from s3ql.backends.common import AbstractBackend
from s3ql.backends.pool import BackendPool
from s3ql.block_cache import BlockCache, QuitSentinel
from s3ql.mkfs import init_tables
from s3ql.metadata import create_tables
from s3ql.database import Connection
from s3ql.common import AsyncFn, time_ns
import s3ql.block_cache
from argparse import Namespace
from common import safe_sleep
from pytest_checklogs import assert_logs
from unittest.mock import patch
import os
import errno
import logging
import shutil
import stat
import tempfile
import threading
import queue
import pytest

log = logging.getLogger(__name__)

# A dummy removal queue to monkeypatch around the need for removal and upload
# threads
class DummyQueue:
    def __init__(self, cache):
        self.obj = None
        self.cache = cache

    def get_nowait(self):
        return self.get(block=False)

    def put(self, obj, timeout=None):
        self.obj = obj
        self.cache._removal_loop_simple()
        return True

    def get(self, block=True):
        if self.obj is None:
            raise queue.Empty()
        elif self.obj is QuitSentinel:
            self.obj = None
            return QuitSentinel
        else:
            tmp = self.obj
            self.obj = QuitSentinel
            return tmp

    def qsize(self):
        return 0

def random_data(len_):
    with open("/dev/urandom", "rb") as fh:
        return fh.read(len_)

@pytest.yield_fixture
def ctx():
        ctx = Namespace()
        ctx.backend_dir = tempfile.mkdtemp(prefix='s3ql-backend-')
        ctx.backend_pool = BackendPool(lambda: local.Backend(
            Namespace(storage_url='local://' + ctx.backend_dir)))

        ctx.cachedir = tempfile.mkdtemp(prefix='s3ql-cache-')
        ctx.max_obj_size = 1024

        # Destructors are not guaranteed to run, and we can't unlink
        # the file immediately because apsw refers to it by name.
        # Therefore, we unlink the file manually in tearDown()
        ctx.dbfile = tempfile.NamedTemporaryFile(delete=False)
        ctx.db = Connection(ctx.dbfile.name)
        create_tables(ctx.db)
        init_tables(ctx.db)

        # Create an inode we can work with
        ctx.inode = 42
        now_ns = time_ns()
        ctx.db.execute("INSERT INTO inodes (id,mode,uid,gid,mtime_ns,atime_ns,ctime_ns,refcount,size) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (ctx.inode, stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                         | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH,
                         os.getuid(), os.getgid(), now_ns, now_ns, now_ns, 1, 32))

        cache = BlockCache(ctx.backend_pool, ctx.db, ctx.cachedir + "/cache",
                           ctx.max_obj_size * 100)
        ctx.cache = cache

        # Monkeypatch around the need for removal and upload threads
        cache.to_remove = DummyQueue(cache)

        class DummyDistributor:
            def put(ctx, arg, timeout=None):
                cache._do_upload(*arg)
                return True
        cache.to_upload = DummyDistributor()

        # Tested methods assume that they are called from
        # file system request handler
        s3ql.block_cache.lock = MockLock()
        s3ql.block_cache.lock_released = MockLock()

        try:
            yield ctx
        finally:
            ctx.cache.backend_pool = ctx.backend_pool
            ctx.cache.destroy()
            shutil.rmtree(ctx.cachedir)
            shutil.rmtree(ctx.backend_dir)
            ctx.dbfile.close()
            os.unlink(ctx.dbfile.name)

def test_thread_hang(ctx):
    # Make sure that we don't deadlock if uploads threads or removal
    # threads have died and we try to expire or terminate

    # Monkeypatch to avoid error messages about uncaught exceptions
    # in other threads
    upload_exc = False
    removal_exc = False
    def _upload_loop(*a, fn=ctx.cache._upload_loop):
        try:
            return fn(*a)
        except NotADirectoryError:
            nonlocal upload_exc
            upload_exc = True
    def _removal_loop_multi(*a, fn=ctx.cache._removal_loop_multi):
        try:
            return fn(*a)
        except NotADirectoryError:
            nonlocal removal_exc
            removal_exc = True
    ctx.cache._upload_loop = _upload_loop
    ctx.cache._removal_loop_multi = _removal_loop_multi

    # Start threads
    ctx.cache.init(threads=3)

    # Create first object (we'll try to remove that)
    with ctx.cache.get(ctx.inode, 0) as fh:
        fh.write(b'bar wurfz!')
    ctx.cache.start_flush()
    ctx.cache.wait()

    # Make sure that upload and removal will fail
    os.rename(ctx.backend_dir, ctx.backend_dir + '-tmp')
    open(ctx.backend_dir, 'w').close()

    # Create second object (we'll try to upload that)
    with ctx.cache.get(ctx.inode, 1) as fh:
        fh.write(b'bar wurfz number two!')

    # Schedule a removal
    ctx.cache.remove(ctx.inode, 0)

    try:
        # Try to clean-up (implicitly calls expire)
        with assert_logs('Unable to flush cache, no upload threads left alive',
                         level=logging.ERROR, count=1):
            ctx.cache.destroy(keep_cache=True)
        assert upload_exc
        assert removal_exc
    finally:
        # Fix backend dir
        os.unlink(ctx.backend_dir)
        os.rename(ctx.backend_dir + '-tmp', ctx.backend_dir)

        # Remove objects from cache and make final destroy
        # call into no-op.
        ctx.cache.remove(ctx.inode, 1)
        ctx.cache.destroy = lambda: None

def test_get(ctx):
    inode = ctx.inode
    blockno = 11
    data = random_data(int(0.5 * ctx.max_obj_size))

    # Case 1: Object does not exist yet
    with ctx.cache.get(inode, blockno) as fh:
        fh.seek(0)
        fh.write(data)

    # Case 2: Object is in cache
    with ctx.cache.get(inode, blockno) as fh:
        fh.seek(0)
        assert data == fh.read(len(data))

    # Case 3: Object needs to be downloaded
    ctx.cache.drop()
    with ctx.cache.get(inode, blockno) as fh:
        fh.seek(0)
        assert data == fh.read(len(data))

def test_expire(ctx):
    inode = ctx.inode

    # Define the 4 most recently accessed ones
    most_recent = [7, 11, 10, 8]
    for i in most_recent:
        safe_sleep(0.2)
        with ctx.cache.get(inode, i) as fh:
            fh.write(('%d' % i).encode())

    # And some others
    for i in range(20):
        if i in most_recent:
            continue
        with ctx.cache.get(inode, i) as fh:
            fh.write(('%d' % i).encode())

    # Flush the 2 most recently accessed ones
    start_flush(ctx.cache, inode, most_recent[-2])
    start_flush(ctx.cache, inode, most_recent[-3])

    # We want to expire 4 entries, 2 of which are already flushed
    ctx.cache.cache.max_entries = 16
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=2)
    ctx.cache.expire()
    ctx.cache.backend_pool.verify()
    assert len(ctx.cache.cache) == 16

    for i in range(20):
        if i in most_recent:
            assert (inode, i) not in ctx.cache.cache
        else:
            assert (inode, i) in ctx.cache.cache

def test_upload(ctx):
    inode = ctx.inode
    datalen = int(0.1 * ctx.cache.cache.max_size)
    blockno1 = 21
    blockno2 = 25
    blockno3 = 7

    data1 = random_data(datalen)
    data2 = random_data(datalen)
    data3 = random_data(datalen)

    # Case 1: create new object
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=1)
    with ctx.cache.get(inode, blockno1) as fh:
        fh.seek(0)
        fh.write(data1)
        el1 = fh
    assert ctx.cache.upload_if_dirty(el1)
    ctx.cache.backend_pool.verify()

    # Case 2: Link new object
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool)
    with ctx.cache.get(inode, blockno2) as fh:
        fh.seek(0)
        fh.write(data1)
        el2 = fh
    assert not ctx.cache.upload_if_dirty(el2)
    ctx.cache.backend_pool.verify()

    # Case 3: Upload old object, still has references
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=1)
    with ctx.cache.get(inode, blockno1) as fh:
        fh.seek(0)
        fh.write(data2)
    assert ctx.cache.upload_if_dirty(el1)
    ctx.cache.backend_pool.verify()

    # Case 4: Upload old object, no references left
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_del=1, no_write=1)
    with ctx.cache.get(inode, blockno2) as fh:
        fh.seek(0)
        fh.write(data3)
    assert ctx.cache.upload_if_dirty(el2)
    ctx.cache.backend_pool.verify()

    # Case 5: Link old object, no references left
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_del=1)
    with ctx.cache.get(inode, blockno2) as fh:
        fh.seek(0)
        fh.write(data2)
    assert not ctx.cache.upload_if_dirty(el2)
    ctx.cache.backend_pool.verify()

    # Case 6: Link old object, still has references
    # (Need to create another object first)
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=1)
    with ctx.cache.get(inode, blockno3) as fh:
        fh.seek(0)
        fh.write(data1)
        el3 = fh
    assert ctx.cache.upload_if_dirty(el3)
    ctx.cache.backend_pool.verify()

    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool)
    with ctx.cache.get(inode, blockno1) as fh:
        fh.seek(0)
        fh.write(data1)
    assert not ctx.cache.upload_if_dirty(el1)
    ctx.cache.drop()
    ctx.cache.backend_pool.verify()

def test_remove_referenced(ctx):
    inode = ctx.inode
    datalen = int(0.1 * ctx.cache.cache.max_size)
    blockno1 = 21
    blockno2 = 24
    data = random_data(datalen)

    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=1)
    with ctx.cache.get(inode, blockno1) as fh:
        fh.seek(0)
        fh.write(data)
    with ctx.cache.get(inode, blockno2) as fh:
        fh.seek(0)
        fh.write(data)
    ctx.cache.drop()
    ctx.cache.backend_pool.verify()

    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool)
    ctx.cache.remove(inode, blockno1)
    ctx.cache.backend_pool.verify()

def test_remove_cache(ctx):
    inode = ctx.inode
    data1 = random_data(int(0.4 * ctx.max_obj_size))

    # Case 1: Elements only in cache
    with ctx.cache.get(inode, 1) as fh:
        fh.seek(0)
        fh.write(data1)
    ctx.cache.remove(inode, 1)
    with ctx.cache.get(inode, 1) as fh:
        fh.seek(0)
        assert fh.read(42) == b''

def test_upload_race(ctx):
    inode = ctx.inode
    blockno = 1
    data1 = random_data(int(0.4 * ctx.max_obj_size))
    with ctx.cache.get(inode, blockno) as fh:
        fh.seek(0)
        fh.write(data1)

    # Remove it
    ctx.cache.remove(inode, blockno)

    # Try to upload it, may happen if CommitThread is interrupted
    ctx.cache.upload_if_dirty(fh)

def test_expire_race(ctx):
    # Create element
    inode = ctx.inode
    blockno = 1
    data1 = random_data(int(0.4 * ctx.max_obj_size))
    with ctx.cache.get(inode, blockno) as fh:
        fh.seek(0)
        fh.write(data1)
    assert ctx.cache.upload_if_dirty(fh)

    # Make sure entry will be expired
    ctx.cache.cache.max_entries = 0

    # Lock it
    ctx.cache._lock_entry(inode, blockno, release_global=True)

    try:
        # Start expiration, will block on lock
        t1 = AsyncFn(ctx.cache.expire)
        t1.start()

        # Start second expiration, will block
        t2 = AsyncFn(ctx.cache.expire)
        t2.start()

        # Release lock
        ctx.cache._unlock_entry(inode, blockno)
        t1.join_and_raise()
        t2.join_and_raise()

        assert len(ctx.cache.cache) == 0
    finally:
            ctx.cache._unlock_entry(inode, blockno, release_global=True,
                                     noerror=True)


def test_parallel_expire(ctx):
    # Create elements
    inode = ctx.inode
    for i in range(5):
        data1 = random_data(int(0.4 * ctx.max_obj_size))
        with ctx.cache.get(inode, i) as fh:
            fh.write(data1)

    # We want to expire just one element, but have
    # several threads running expire() simultaneously
    ctx.cache.cache.max_entries = 4

    # Lock first element so that we have time to start threads
    ctx.cache._lock_entry(inode, 0, release_global=True)

    try:
        # Start expiration, will block on lock
        t1 = AsyncFn(ctx.cache.expire)
        t1.start()

        # Start second expiration, will block
        t2 = AsyncFn(ctx.cache.expire)
        t2.start()

        # Release lock
        ctx.cache._unlock_entry(inode, 0)
        t1.join_and_raise()
        t2.join_and_raise()

        assert len(ctx.cache.cache) == 4
    finally:
            ctx.cache._unlock_entry(inode, 0, release_global=True,
                                     noerror=True)

def test_remove_cache_db(ctx):
    inode = ctx.inode
    data1 = random_data(int(0.4 * ctx.max_obj_size))

    # Case 2: Element in cache and db
    with ctx.cache.get(inode, 1) as fh:
        fh.seek(0)
        fh.write(data1)
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=1)
    start_flush(ctx.cache, inode)
    ctx.cache.backend_pool.verify()
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_del=1)
    ctx.cache.remove(inode, 1)
    ctx.cache.backend_pool.verify()

    with ctx.cache.get(inode, 1) as fh:
        fh.seek(0)
        assert fh.read(42) == b''

def test_remove_db(ctx):
    inode = ctx.inode
    data1 = random_data(int(0.4 * ctx.max_obj_size))

    # Case 3: Element only in DB
    with ctx.cache.get(inode, 1) as fh:
        fh.seek(0)
        fh.write(data1)
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_write=1)
    ctx.cache.drop()
    ctx.cache.backend_pool.verify()
    ctx.cache.backend_pool = MockBackendPool(ctx.backend_pool, no_del=1)
    ctx.cache.remove(inode, 1)
    ctx.cache.backend_pool.verify()
    with ctx.cache.get(inode, 1) as fh:
        fh.seek(0)
        assert fh.read(42) ==  b''

def test_issue_241(ctx):

    inode = ctx.inode

    # Create block
    with ctx.cache.get(inode, 0) as fh:
        fh.write(random_data(500))

    # "Fill" cache
    ctx.cache.cache.max_entries = 0

    # Mock locking to reproduce race condition
    mlock = MockMultiLock(ctx.cache.mlock)
    with patch.object(ctx.cache, 'mlock', mlock):
        # Start first expiration run, will block in upload
        thread1 = AsyncFn(ctx.cache.expire)
        thread1.start()

        # Remove the object while the expiration thread waits
        # for it to become available.
        thread2 = AsyncFn(ctx.cache.remove, inode, 0, 1)
        thread2.start()
        mlock.yield_to(thread2)
        thread2.join_and_raise(timeout=10)
        assert not thread2.is_alive()

    # Create a new object for the same block
    with ctx.cache.get(inode, 0) as fh:
        fh.write(random_data(500))

    # Continue first expiration run
    mlock.yield_to(thread1, block=False)
    thread1.join_and_raise(timeout=10)
    assert not thread1.is_alive()


class MockMultiLock:
    def __init__(self, real_mlock):
        self.cond = real_mlock.cond
        self.cleared = set()
        self.real_mlock = real_mlock

    def yield_to(self, thread, block=True):
        '''Allow *thread* to proceed'''

        me = threading.current_thread()
        log.debug('%s blocked in yield_to(), phase 1', me.name)
        with self.cond:
            self.cleared.add(thread)
            self.cond.notify_all()

        if not block:
            return
        log.debug('%s blocked in yield_to(), phase 2', me.name)
        with self.cond:
            if not self.cond.wait_for(lambda: thread not in self.cleared, 10):
                pytest.fail('Timeout waiting for lock')
        log.debug('%s completed yield_to()', me.name)

    @contextmanager
    def __call__(self, *key):
        self.acquire(*key)
        try:
            yield
        finally:
            self.release(*key)

    def acquire(self, *key, timeout=None):
        if timeout == 0:
            return False
        me = threading.current_thread()
        log.debug('%s blocked in acquire()', me.name)
        with self.cond:
            if not self.cond.wait_for(lambda: me in self.cleared, 10):
                pytest.fail('Timeout waiting for lock')
            self.real_mlock.locked_keys.add(key)
        log.debug('%s got lock', me.name)
        return True

    def release(self, *key, noerror=False):
        me = threading.current_thread()
        log.debug('%s blocked in release()', me.name)
        with self.cond:
            self.cleared.remove(me)
            self.cond.notify_all()
            if noerror:
                self.real_mlock.locked_keys.discard(key)
            else:
                self.real_mlock.locked_keys.remove(key)
        log.debug('%s released lock', me.name)


class MockBackendPool(AbstractBackend):
    has_native_rename = False

    def __init__(self, backend_pool, no_read=0, no_write=0, no_del=0):
        super().__init__()
        self.no_read = no_read
        self.no_write = no_write
        self.no_del = no_del
        self.backend_pool = backend_pool
        self.backend = backend_pool.pop_conn()
        self.lock = threading.Lock()

    def __del__(self):
        self.backend_pool.push_conn(self.backend)

    def verify(self):
        if self.no_read != 0:
            raise RuntimeError('Got too few open_read calls')
        if self.no_write != 0:
            raise RuntimeError('Got too few open_write calls')
        if self.no_del != 0:
            raise RuntimeError('Got too few delete calls')

    @contextmanager
    def __call__(self):
        '''Provide connection from pool (context manager)'''

        with self.lock:
            yield self

    def lookup(self, key):
        return self.backend.lookup(key)

    def open_read(self, key):
        self.no_read -= 1
        if self.no_read < 0:
            raise RuntimeError('Got too many open_read calls')

        return self.backend.open_read(key)

    def open_write(self, key, metadata=None, is_compressed=False):
        self.no_write -= 1
        if self.no_write < 0:
            raise RuntimeError('Got too many open_write calls')

        return self.backend.open_write(key, metadata, is_compressed)

    def is_temp_failure(self, exc):
        return self.backend.is_temp_failure(exc)

    def contains(self, key):
        return self.backend.contains(key)

    def delete(self, key, force=False):
        self.no_del -= 1
        if self.no_del < 0:
            raise RuntimeError('Got too many delete calls')

        return self.backend.delete(key, force)

    def list(self, prefix=''):
        '''List keys in backend

        Returns an iterator over all keys in the backend.
        '''
        return self.backend.list(prefix)

    def copy(self, src, dest, metadata=None):
        return self.backend.copy(src, dest, metadata)

    def rename(self, src, dest, metadata=None):
        return self.backend.rename(src, dest, metadata)

    def update_meta(self, key, metadata):
        return self.backend.update_meta(key, metadata)

    def get_size(self, key):
        '''Return size of object stored under *key*'''

        return self.backend.get_size(key)

def start_flush(cache, inode, block=None):
    """Upload data for `inode`

    This is only for testing purposes, since the method blocks until all current
    uploads have been completed.
    """

    for el in cache.cache.values():
        if el.inode != inode:
            continue
        if not el.dirty:
            continue

        if block is not None and el.blockno != block:
            continue

        cache.upload_if_dirty(el)


class MockLock():
    def __call__(self):
        return self

    def acquire(self, timeout=None):
        pass

    def release(self):
        pass

    def __enter__(self):
        pass

    def __exit__(self, *args):
        pass