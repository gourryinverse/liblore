# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for LoreNode filesystem caching."""

from __future__ import annotations

import gzip
import os
import time
from pathlib import Path
from unittest import mock

import pytest
import responses

from liblore import LoreNode, RemoteError
from liblore.node import _ORPHAN_TMP_AGE, _write_atomic

DISK_FULL = OSError('disk full')


class TestCacheReadWrite:
    """Unit tests for _cache_key, _cache_read, _cache_write."""

    def test_cache_disabled_by_default(self) -> None:
        node = LoreNode('https://lore.kernel.org/all')
        assert node._cache_dir is None
        assert node._cache_read('anything') is None

    def test_cache_write_and_read(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path), cache_ttl=60)
        key = node._cache_key('test', 'data')
        node._cache_write(key, b'hello world')
        assert node._cache_read(key) == b'hello world'

    def test_cache_miss_returns_none(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path))
        key = node._cache_key('test', 'nonexistent')
        assert node._cache_read(key) is None

    def test_cache_expired_returns_none(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path), cache_ttl=1)
        key = node._cache_key('test', 'data')
        node._cache_write(key, b'old data')
        # Backdate the file to make it stale
        path = tmp_path / f'{key}.lore.cache'
        os.utime(path, (0, 0))
        assert node._cache_read(key) is None
        # Stale file should be cleaned up
        assert not path.exists()

    def test_cache_write_noop_when_disabled(self) -> None:
        node = LoreNode()
        # Should not raise even though cache_dir is None
        node._cache_write('key', b'data')

    def test_cache_write_atomic(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path))
        key = node._cache_key('test', 'atomic')
        node._cache_write(key, b'data')
        # No .tmp files left behind
        tmp_files = [f for f in os.listdir(tmp_path) if f.endswith('.tmp')]
        assert tmp_files == []

    def test_cache_key_varies_by_url(self) -> None:
        node_a = LoreNode('https://lore.kernel.org/all')
        node_b = LoreNode('https://other.example.com/all')
        key_a = node_a._cache_key('ns', 'data')
        key_b = node_b._cache_key('ns', 'data')
        assert key_a != key_b

    def test_cache_key_varies_by_namespace(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path))
        key_a = node._cache_key('mbox_by_msgid', 'test@x.com')
        key_b = node._cache_key('mbox_by_query', 'test@x.com')
        assert key_a != key_b

    def test_cache_empty_bytes(self, tmp_path: Path) -> None:
        """Empty bytes (valid server response) should be cached."""
        node = LoreNode(cache_dir=str(tmp_path), cache_ttl=60)
        key = node._cache_key('test', 'empty')
        node._cache_write(key, b'')
        assert node._cache_read(key) == b''

    def test_cache_dir_created(self, tmp_path: Path) -> None:
        cache_dir = str(tmp_path / 'sub' / 'dir')
        LoreNode(cache_dir=cache_dir)
        assert os.path.isdir(cache_dir)


class TestWriteAtomic:
    def test_writes(self, tmp_path: Path) -> None:
        path = tmp_path / 'x.lore.cache'
        _write_atomic(str(path), b'data')
        assert path.read_bytes() == b'data'
        assert os.listdir(tmp_path) == ['x.lore.cache']

    def test_two_writers_of_one_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second process writes the file while the first is writing it."""
        path = str(tmp_path / 'x.lore.cache')
        real_replace = os.replace
        calls = 0

        def replace(src: str, dst: str) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                _write_atomic(path, b'second')
            real_replace(src, dst)

        monkeypatch.setattr(os, 'replace', replace)
        # With a shared temporary name, the second writer would find
        # the first one's file, and one of the two writes would fail.
        _write_atomic(path, b'first')
        assert calls == 2
        # The last one to finish wins, and nothing is left behind.
        assert Path(path).read_bytes() == b'first'
        assert os.listdir(tmp_path) == ['x.lore.cache']

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX file modes')
    def test_mode_follows_umask(self, tmp_path: Path) -> None:
        """Like open(), not like mkstemp(), which always gives 0o600."""
        path = tmp_path / 'x.lore.cache'
        old = os.umask(0o022)
        try:
            _write_atomic(str(path), b'data')
        finally:
            os.umask(old)
        assert path.stat().st_mode & 0o777 == 0o644

    def test_failure_removes_temporary_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, 'replace', mock.Mock(side_effect=DISK_FULL))
        with pytest.raises(OSError, match='disk full'):
            _write_atomic(str(tmp_path / 'x.lore.cache'), b'data')
        assert os.listdir(tmp_path) == []

    def test_cache_write_failure_is_only_logged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, 'replace', mock.Mock(side_effect=DISK_FULL))
        node = LoreNode(cache_dir=str(tmp_path))
        node._cache_write(node._cache_key('a', '1'), b'data')
        assert os.listdir(tmp_path) == []


class TestClearCache:
    def test_clears_cache_files(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path))
        node._cache_write(node._cache_key('a', '1'), b'data1')
        node._cache_write(node._cache_key('b', '2'), b'data2')
        assert len(list(tmp_path.glob('*.lore.cache'))) == 2
        node.clear_cache()
        assert len(list(tmp_path.glob('*.lore.cache'))) == 0

    def test_preserves_non_cache_files(self, tmp_path: Path) -> None:
        node = LoreNode(cache_dir=str(tmp_path))
        (tmp_path / 'other.txt').write_text('keep me')
        node._cache_write(node._cache_key('a', '1'), b'data')
        node.clear_cache()
        assert (tmp_path / 'other.txt').exists()

    def test_noop_when_disabled(self) -> None:
        node = LoreNode()
        node.clear_cache()  # Should not raise

    def test_removes_orphaned_temporary_files(self, tmp_path: Path) -> None:
        """Left by a killed process: nothing else would ever remove them."""
        orphans = [
            'a.lore.cache.0123456789abcdef.tmp',
            'b.lore.upstream.0123456789abcdef.tmp',
            # The name that versions before 0.10 used.
            'c.lore.cache.tmp',
        ]
        kept = ['d.lore.upstream', 'other.tmp', 'e.lore.cache.notours.tmp']
        for name in orphans + kept:
            (tmp_path / name).write_bytes(b'x')
        old = time.time() - _ORPHAN_TMP_AGE - 1
        for name in orphans:
            os.utime(tmp_path / name, (old, old))
        LoreNode(cache_dir=str(tmp_path)).clear_cache()
        assert sorted(os.listdir(tmp_path)) == sorted(kept)

    def test_keeps_temporary_files_in_use(self, tmp_path: Path) -> None:
        """Another process may be writing it right now."""
        fresh = tmp_path / 'a.lore.cache.0123456789abcdef.tmp'
        fresh.write_bytes(b'x')
        LoreNode(cache_dir=str(tmp_path)).clear_cache()
        assert os.listdir(tmp_path) == [fresh.name]


class TestProperties:
    def test_url_property(self) -> None:
        node = LoreNode('https://lore.kernel.org/all/')
        # Trailing slash is stripped by constructor
        assert node.url == 'https://lore.kernel.org/all'

    def test_hostname_property(self) -> None:
        node = LoreNode('https://lore.kernel.org/all')
        assert node.hostname == 'lore.kernel.org'

    def test_hostname_different_host(self) -> None:
        node = LoreNode('https://inbox.example.com/linux-kernel')
        assert node.hostname == 'inbox.example.com'


class TestCachedMethods:
    """Integration tests: verify cache hit avoids network, cache miss hits network."""

    def _make_node(self, tmp_path: Path, _sample_mbox: bytes) -> LoreNode:
        node = LoreNode(
            'https://lore.kernel.org/all', cache_dir=str(tmp_path), cache_ttl=60
        )
        return node

    def test_get_mbox_by_msgid_caches(self, tmp_path: Path, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = self._make_node(tmp_path, sample_mbox)
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            result1 = node.get_mbox_by_msgid('test@example.com')
            result2 = node.get_mbox_by_msgid('test@example.com')
            assert result1 == result2 == sample_mbox
            # Network should only be hit once
            assert len(rsps.calls) == 1

    def test_get_mbox_by_query_caches(self, tmp_path: Path, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = self._make_node(tmp_path, sample_mbox)
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test+query',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            result1 = node.get_mbox_by_query('test query')
            result2 = node.get_mbox_by_query('test query')
            assert result1 == result2 == sample_mbox
            assert len(rsps.calls) == 1

    def test_get_mbox_by_query_full_threads_separate_key(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        with responses.RequestsMock() as rsps:
            node = self._make_node(tmp_path, sample_mbox)
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&t=1&q=test',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            node.get_mbox_by_query('test', full_threads=False)
            node.get_mbox_by_query('test', full_threads=True)
            # Different full_threads values = different cache keys = two network calls
            assert len(rsps.calls) == 2

    def test_get_message_by_msgid_caches(self, tmp_path: Path) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode(
                'https://lore.kernel.org/all', cache_dir=str(tmp_path), cache_ttl=60
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/raw',
                body=b'raw email bytes',
                status=200,
            )

            result1 = node.get_message_by_msgid('test@example.com')
            result2 = node.get_message_by_msgid('test@example.com')
            assert result1 == result2 == b'raw email bytes'
            assert len(rsps.calls) == 1

    def test_cache_expired_refetches(self, tmp_path: Path, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = self._make_node(tmp_path, sample_mbox)
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            node._cache_ttl = 1
            node.get_mbox_by_msgid('test@example.com')
            assert len(rsps.calls) == 1
            # Backdate the cache file
            for f in tmp_path.glob('*.lore.cache'):
                os.utime(f, (0, 0))
            node.get_mbox_by_msgid('test@example.com')
            assert len(rsps.calls) == 2

    def test_no_cache_no_files(self, tmp_path: Path, sample_mbox: bytes) -> None:
        """When cache_dir is None, no files are written."""
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_mbox_by_msgid('test@example.com')
            # tmp_path should be empty since we didn't set cache_dir to it
            assert list(tmp_path.iterdir()) == []

    def test_fetch_thread_since_not_cached(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        """_fetch_thread_since should NOT be cached."""
        with responses.RequestsMock() as rsps:
            node = self._make_node(tmp_path, sample_mbox)
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/test%40example.com/?x=m&q=dt%3A20240101..',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            node._fetch_thread_since('test@example.com', 'dt:20240101..')
            node._fetch_thread_since('test@example.com', 'dt:20240101..')
            # Both calls should hit the network
            assert len(rsps.calls) == 2

    def test_error_not_cached(self, tmp_path: Path) -> None:
        """Network errors should not be cached."""
        with responses.RequestsMock() as rsps:
            node = LoreNode(
                'https://lore.kernel.org/all', cache_dir=str(tmp_path), cache_ttl=60
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/bad%40example.com/t.mbox.gz',
                status=404,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/bad%40example.com/',
                status=404,
            )

            with pytest.raises(RemoteError):
                node.get_mbox_by_msgid('bad@example.com')
            # No cache file should be written
            assert list(tmp_path.glob('*.lore.cache')) == []
