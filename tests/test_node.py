# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for liblore.node (LoreNode)."""

from __future__ import annotations

import gzip
import os
import pickle
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, call, patch

import pytest
import requests
import responses
from typing_extensions import override

from liblore import OperationCancelledError, RemoteError
from liblore.node import LoreNode


def request_url(rsps: responses.RequestsMock, index: int) -> str:
    url = rsps.calls[index].request.url
    assert url is not None
    return url


# =====================================================================
# RemoteError
# =====================================================================


class TestRemoteError:
    def test_status_code_defaults_to_none(self) -> None:
        # Code written before status_code existed keeps working.
        err = RemoteError('boom')
        assert str(err) == 'boom'
        assert err.status_code is None

    def test_status_code(self) -> None:
        err = RemoteError('not found', status_code=404)
        assert str(err) == 'not found'
        assert err.status_code == 404

    def test_pickle_keeps_status_code(self) -> None:
        # Errors cross process boundaries (concurrent.futures).
        err = pickle.loads(pickle.dumps(RemoteError('gone', status_code=410)))
        assert str(err) == 'gone'
        assert err.status_code == 410


# =====================================================================
# Session management
# =====================================================================


class TestSessionManagement:
    def test_creates_session(self) -> None:
        node = LoreNode()
        s = node._get_session()
        assert s is not None
        user_agent = s.headers['User-Agent']
        assert isinstance(user_agent, str)
        assert 'liblore/' in user_agent
        node.close()

    def test_returns_same_session(self) -> None:
        node = LoreNode()
        s1 = node._get_session()
        s2 = node._get_session()
        assert s1 is s2
        node.close()

    def test_close_and_reopen(self) -> None:
        node = LoreNode()
        s1 = node._get_session()
        node.close()
        s2 = node._get_session()
        assert s1 is not s2
        node.close()

    def test_set_user_agent(self) -> None:
        node = LoreNode()
        node.set_user_agent('myapp', '1.0')
        s = node._get_session()
        assert s.headers['User-Agent'] == 'myapp/1.0'
        node.close()

    def test_set_user_agent_updates_existing(self) -> None:
        node = LoreNode()
        s = node._get_session()
        node.set_user_agent('otherapp', '2.0')
        assert s.headers['User-Agent'] == 'otherapp/2.0'
        node.close()

    def test_set_user_agent_plus(self) -> None:
        node = LoreNode()
        node.set_user_agent('korgalore', '0.6', plus='abcd1234')
        s = node._get_session()
        assert s.headers['User-Agent'] == 'korgalore/0.6+abcd1234'
        node.close()

    def test_no_plus(self) -> None:
        node = LoreNode()
        node.set_user_agent('korgalore', '0.6')
        s = node._get_session()
        assert s.headers['User-Agent'] == 'korgalore/0.6'
        node.close()

    def test_default_no_plus(self) -> None:
        node = LoreNode()
        s = node._get_session()
        user_agent = s.headers['User-Agent']
        assert isinstance(user_agent, str)
        assert '+' not in user_agent
        node.close()

    def test_set_requests_session(self) -> None:
        external = requests.Session()
        external.headers.update({'User-Agent': 'custom/1.0'})
        node = LoreNode()
        node.set_requests_session(external)
        s = node._get_session()
        assert s is external
        assert s.headers['User-Agent'] == 'custom/1.0'
        node.close()

    def test_set_requests_session_ua_not_overwritten(self) -> None:
        external = requests.Session()
        external.headers.update({'User-Agent': 'myapp/2.0'})
        node = LoreNode()
        node.set_user_agent('liblore', '0.1')
        node.set_requests_session(external)
        assert node._get_session().headers['User-Agent'] == 'myapp/2.0'
        node.close()

    def test_close_only_owned_session(self) -> None:
        external = requests.Session()
        node = LoreNode()
        node.set_requests_session(external)
        node.close()
        # External session should NOT be closed — still usable
        external.headers.update({'X-Test': 'alive'})
        external.close()

    def test_context_manager(self) -> None:
        with LoreNode() as node:
            s = node._get_session()
            assert s is not None
        # After exiting, session should be cleaned up
        assert node._session is None


# =====================================================================
# get_mbox_by_msgid / get_mbox_by_query
# =====================================================================


class TestGetMboxByMsgid:
    def test_returns_raw_bytes(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/first%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('first@example.com')
            assert result == sample_mbox

    def test_http_error(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40x.com/t.mbox.gz',
                status=500,
            )

            with pytest.raises(RemoteError, match='Server returned an error') as ei:
                node.get_mbox_by_msgid('test@x.com')
            assert ei.value.status_code == 500

    def test_404_falls_back_to_head_redirect(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """On 404, try HEAD against the bare origin to discover the list path."""
            node = LoreNode('https://lore.kernel.org/all')
            # First GET returns 404
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                status=404,
            )
            # HEAD follows redirect and succeeds
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/test%40example.com/',
                status=302,
                headers={
                    'Location': 'https://lore.kernel.org/tools/test%40example.com/'
                },
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/tools/test%40example.com/',
                status=200,
            )
            # Second GET (to resolved URL) succeeds
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/tools/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('test@example.com')
            assert result == sample_mbox
            # Verify the HEAD was sent to the bare origin
            assert request_url(rsps, 1) == 'https://lore.kernel.org/test%40example.com/'

    def test_404_no_redirect_raises(self) -> None:
        with responses.RequestsMock() as rsps:
            """When HEAD also 404s (no redirect), raise RemoteError."""
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/nonexistent%40example.com/t.mbox.gz',
                status=404,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/nonexistent%40example.com/',
                status=404,
            )

            with pytest.raises(
                RemoteError, match='Server returned an error: 404'
            ) as ei:
                node.get_mbox_by_msgid('nonexistent@example.com')
            assert ei.value.status_code == 404


class TestGetMboxByQuery:
    def test_returns_raw_bytes(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test+query',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_query('test query')
            assert result == sample_mbox

    def test_full_threads_adds_t_param(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&t=1&q=test+query',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_mbox_by_query('test query', full_threads=True)
            url = request_url(rsps, 0)
            assert '&t=1&' in url

    def test_no_full_threads_omits_t_param(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test+query',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_mbox_by_query('test query')
            url = request_url(rsps, 0)
            assert 't=1' not in url

    def test_http_error(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test',
                status=500,
            )

            with pytest.raises(RemoteError, match='Server returned an error') as ei:
                node.get_mbox_by_query('test')
            assert ei.value.status_code == 500

    def test_no_results_is_404(self) -> None:
        with responses.RequestsMock() as rsps:
            """lore.kernel.org answers a search with no hits with a 404."""
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=nomatch',
                body=b'No results found\n',
                status=404,
            )

            with pytest.raises(RemoteError, match='Server returned an error') as ei:
                node.get_mbox_by_query('nomatch')
            assert ei.value.status_code == 404

    # =====================================================================
    # get_thread_by_msgid
    # =====================================================================


class TestGetThreadByMsgid:
    def test_full_thread(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/first%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            msgs = node.get_thread_by_msgid('first@example.com')
            assert len(msgs) >= 1
            # Without since, fetches full thread via GET /{msgid}/t.mbox.gz
            assert len(rsps.calls) == 1

    def test_query_contains_msgid(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/first%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_thread_by_msgid('first@example.com')
            call_url = request_url(rsps, 0)
            assert 'first%40example.com' in call_url or 'first@example.com' in call_url
            assert call_url.endswith('/t.mbox.gz')

    def test_since_uses_dt_prefix(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/first%40example.com/?x=m&q=dt%3A20240101..',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_thread_by_msgid('first@example.com', since='20240101')
            call_url = request_url(rsps, 0)
            assert 'dt%3A20240101' in call_url or 'dt:20240101' in call_url
            assert 'first%40example.com' in call_url or 'first@example.com' in call_url

    def test_raises_on_empty(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/nonexistent%40x.com/t.mbox.gz',
                body=gzip.compress(b''),
                status=200,
            )

            with pytest.raises(LookupError):
                node.get_thread_by_msgid('nonexistent@x.com')

    def test_sort_parameter(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/first%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            msgs = node.get_thread_by_msgid('first@example.com', sort=True)
            assert len(msgs) >= 1

    # =====================================================================
    # get_thread_updates_since
    # =====================================================================


class TestGetThreadUpdatesSince:
    def test_returns_messages(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/first%40example.com/?x=m&q=rt%3A1705320000..',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            since = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            msgs = node.get_thread_updates_since('first@example.com', since)
            assert len(msgs) >= 1
            assert len(rsps.calls) == 1

    def test_empty_returns_empty_list(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/first%40example.com/?x=m&q=rt%3A1705320000..',
                body=gzip.compress(b''),
                status=200,
            )

            since = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            msgs = node.get_thread_updates_since('first@example.com', since)
            assert msgs == []

    def test_converts_datetime_to_rt_epoch(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            since = datetime(2024, 3, 15, 8, 30, 45, tzinfo=timezone.utc)
            epoch = int(since.timestamp())  # 1710491445
            rsps.add(
                responses.POST,
                f'https://lore.kernel.org/all/first%40example.com/?x=m&q=rt%3A{epoch}..',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            node.get_thread_updates_since('first@example.com', since)
            call_url = request_url(rsps, 0)
            assert f'rt%3A{epoch}' in call_url or f'rt:{epoch}' in call_url
            assert 'first%40example.com' in call_url or 'first@example.com' in call_url

    def test_with_sort(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/first%40example.com/?x=m&q=rt%3A1705320000..',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            since = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            msgs = node.get_thread_updates_since(
                'first@example.com',
                since,
                sort=True,
            )
            assert len(msgs) >= 1

    def test_server_error_returns_empty_list(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/first%40example.com/?x=m&q=rt%3A1705320000..',
                status=404,
            )

            since = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
            msgs = node.get_thread_updates_since('first@example.com', since)
            assert msgs == []

    # =====================================================================
    # get_thread_by_query
    # =====================================================================


class TestGetThreadByQuery:
    def test_posts_query(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test+query',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            msgs = node.get_thread_by_query('test query')
            assert len(msgs) == 2
            assert len(rsps.calls) == 1

    def test_query_with_date_filter(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/?x=m&q=test+d%3A20240101..',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_thread_by_query('test d:20240101..')
            call_url = request_url(rsps, 0)
            assert 'd%3A20240101' in call_url or 'd:20240101' in call_url

    # =====================================================================
    # get_message_by_msgid
    # =====================================================================


class TestGetMessageByMsgid:
    def test_fetches_raw(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40x.com/raw',
                body=b'raw email bytes',
                status=200,
            )

            result = node.get_message_by_msgid('test@x.com')
            assert result == b'raw email bytes'

    def test_raises_remote_error(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40x.com/raw',
                body=requests.ConnectionError('connection refused'),
            )

            with pytest.raises(RemoteError) as ei:
                node.get_message_by_msgid('test@x.com')
            assert ei.value.status_code is None

    def test_not_found_has_status(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40x.com/raw',
                status=404,
            )

            with pytest.raises(RemoteError, match='Failed to fetch message') as ei:
                node.get_message_by_msgid('test@x.com')
            assert ei.value.status_code == 404

    # =====================================================================
    # batch_get_thread_by_msgid
    # =====================================================================


class TestBatchGetThreadByMsgid:
    class _Node(LoreNode):
        def __init__(self, results: list[list[EmailMessage]]) -> None:
            super().__init__()
            self.calls: list[tuple[str, bool, bool, str | None]] = []
            self._results = results

        @override
        def get_thread_by_msgid(
            self,
            msgid: str,
            *,
            strict: bool = True,
            sort: bool = False,
            since: str | None = None,
        ) -> list[EmailMessage]:
            self.calls.append((msgid, strict, sort, since))
            return self._results.pop(0)

    def test_returns_ordered_results(self) -> None:
        thread_a = [EmailMessage()]
        thread_b = [EmailMessage(), EmailMessage()]
        node = self._Node([thread_a, thread_b])
        with patch('liblore.node.time.sleep') as mock_sleep:
            results = node.batch_get_thread_by_msgid(['a@x', 'b@x'])

        assert results == [thread_a, thread_b]
        assert len(node.calls) == 2
        mock_sleep.assert_called_once_with(0.1)

    def test_no_sleep_for_single_msgid(self) -> None:
        thread = [EmailMessage()]
        node = self._Node([thread])
        with patch('liblore.node.time.sleep') as mock_sleep:
            results = node.batch_get_thread_by_msgid(['only@x'])

        assert results == [thread]
        mock_sleep.assert_not_called()

    def test_passes_kwargs(self) -> None:
        node = self._Node([[EmailMessage()]])
        with patch('liblore.node.time.sleep'):
            node.batch_get_thread_by_msgid(
                ['a@x'],
                strict=False,
                sort=True,
                since='20240101',
            )

        assert node.calls == [('a@x', False, True, '20240101')]

    def test_sleep_count_matches_gaps(self) -> None:
        node = self._Node([[EmailMessage()], [EmailMessage()], [EmailMessage()]])
        with patch('liblore.node.time.sleep') as mock_sleep:
            node.batch_get_thread_by_msgid(['a@x', 'b@x', 'c@x'])

        assert mock_sleep.call_args_list == [call(0.1), call(0.1)]

    def test_empty_list(self) -> None:
        node = self._Node([])
        with patch('liblore.node.time.sleep') as mock_sleep:
            results = node.batch_get_thread_by_msgid([])

        assert results == []
        mock_sleep.assert_not_called()
        assert node.calls == []


# =====================================================================
# batch_get_thread_by_query
# =====================================================================


class TestBatchGetThreadByQuery:
    class _Node(LoreNode):
        def __init__(self, results: list[list[EmailMessage]]) -> None:
            super().__init__()
            self.calls: list[tuple[str, bool]] = []
            self._results = results

        @override
        def get_thread_by_query(
            self,
            query: str,
            *,
            full_threads: bool = False,
        ) -> list[EmailMessage]:
            self.calls.append((query, full_threads))
            return self._results.pop(0)

    def test_returns_ordered_results(self) -> None:
        result_a = [EmailMessage()]
        result_b = [EmailMessage(), EmailMessage()]
        node = self._Node([result_a, result_b])
        with patch('liblore.node.time.sleep') as mock_sleep:
            results = node.batch_get_thread_by_query(['q1', 'q2'])

        assert results == [result_a, result_b]
        assert len(node.calls) == 2
        mock_sleep.assert_called_once_with(0.1)

    def test_no_sleep_for_single_query(self) -> None:
        result = [EmailMessage()]
        node = self._Node([result])
        with patch('liblore.node.time.sleep') as mock_sleep:
            results = node.batch_get_thread_by_query(['only_query'])

        assert results == [result]
        mock_sleep.assert_not_called()

    def test_sleep_count_matches_gaps(self) -> None:
        node = self._Node(
            [[EmailMessage()], [EmailMessage()], [EmailMessage()], [EmailMessage()]]
        )
        with patch('liblore.node.time.sleep') as mock_sleep:
            node.batch_get_thread_by_query(['q1', 'q2', 'q3', 'q4'])

        assert mock_sleep.call_args_list == [call(0.1), call(0.1), call(0.1)]

    def test_empty_list(self) -> None:
        node = self._Node([])
        with patch('liblore.node.time.sleep') as mock_sleep:
            results = node.batch_get_thread_by_query([])

        assert results == []
        mock_sleep.assert_not_called()
        assert node.calls == []


# =====================================================================
# validate
# =====================================================================


class TestValidate:
    def test_valid_url(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://lore.kernel.org/lkml')
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/lkml/_/text/help/',
                status=200,
            )

            node.validate()
            assert request_url(rsps, 0) == 'https://lore.kernel.org/lkml/_/text/help/'

    def test_not_public_inbox(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://example.com/not-pi')
            rsps.add(
                responses.HEAD,
                'https://example.com/not-pi/_/text/help/',
                status=404,
            )

            with pytest.raises(RemoteError, match='does not appear') as ei:
                node.validate()
            assert ei.value.status_code == 404

    def test_connection_error(self) -> None:
        with responses.RequestsMock() as rsps:
            node = LoreNode('https://unreachable.example.com')
            rsps.add(
                responses.HEAD,
                'https://unreachable.example.com/_/text/help/',
                body=requests.ConnectionError('connection refused'),
            )

            with pytest.raises(RemoteError, match='Failed to reach') as ei:
                node.validate()
            assert ei.value.status_code is None

    # =====================================================================
    # URL fallback
    # =====================================================================


class TestFallback:
    """Tests for the fallback_urls feature."""

    def test_no_fallbacks_unchanged(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """Without fallback_urls, behavior is identical to before."""
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('test@example.com')
            assert result == sample_mbox
            assert len(rsps.calls) == 1
            url = request_url(rsps, 0)
            assert url.startswith('https://lore.kernel.org/')

    def test_fallback_on_connection_error(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """Primary raises ConnectionError, fallback succeeds."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/all/test%40example.com/t.mbox.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('test@example.com')
            assert result == sample_mbox
            assert len(rsps.calls) == 2
            # First call goes to the fallback (tried first)
            first_url = request_url(rsps, 0)
            assert first_url.startswith('http://mirror.local/all/')
            # Second call goes to the canonical URL
            second_url = request_url(rsps, 1)
            assert second_url.startswith('https://lore.kernel.org/all/')

    def test_fallback_on_timeout(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """Primary raises Timeout, fallback succeeds."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://ams.lore.kernel.org'],
            )
            rsps.add(
                responses.GET,
                'https://ams.lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=requests.Timeout('timed out'),
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('test@example.com')
            assert result == sample_mbox
            assert len(rsps.calls) == 2

    def test_fallback_on_5xx(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """Primary returns 500, fallback returns 200."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/all/test%40example.com/t.mbox.gz',
                status=500,
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('test@example.com')
            assert result == sample_mbox
            assert len(rsps.calls) == 2

    def test_no_fallback_on_4xx(self) -> None:
        with responses.RequestsMock() as rsps:
            """4xx is not retriable — fallback should NOT be tried."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/all/test%40example.com/t.mbox.gz',
                status=404,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/test%40example.com/',
                status=404,
            )

            with pytest.raises(RemoteError, match='Server returned an error'):
                node.get_mbox_by_msgid('test@example.com')
            # Fallback returns 404, then redirect discovery also returns 404.
            assert len(rsps.calls) == 2

    def test_all_hosts_fail_connection(self) -> None:
        with responses.RequestsMock() as rsps:
            """All origins raise ConnectionError → RemoteError."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/all/test%40example.com/t.mbox.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=requests.ConnectionError('refused'),
            )

            with pytest.raises(RemoteError, match='All hosts failed') as ei:
                node.get_mbox_by_msgid('test@example.com')
            assert ei.value.status_code is None
            assert len(rsps.calls) == 2

    def test_all_hosts_fail_5xx(self) -> None:
        with responses.RequestsMock() as rsps:
            """All origins return 5xx → caller gets the error response."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/all/test%40example.com/t.mbox.gz',
                status=503,
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                status=503,
            )

            # get_mbox_by_msgid checks status_code and raises RemoteError
            with pytest.raises(RemoteError, match='Server returned an error') as ei:
                node.get_mbox_by_msgid('test@example.com')
            assert ei.value.status_code == 503
            assert len(rsps.calls) == 2

    def test_all_hosts_fail_no_raise(self) -> None:
        with responses.RequestsMock() as rsps:
            """_fetch_thread_since path: all fail, returns empty list."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.POST,
                'http://mirror.local/all/test%40example.com/?x=m&q=dt%3A20240101..',
                status=503,
            )
            rsps.add(
                responses.POST,
                'https://lore.kernel.org/all/test%40example.com/?x=m&q=dt%3A20240101..',
                status=503,
            )

            result = node._fetch_thread_since('test@example.com', 'dt:20240101..')
            assert result == []
            assert len(rsps.calls) == 2

    def test_url_rewriting_preserves_path(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """Verify full URL rewriting with scheme change."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mymirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mymirror.local/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_mbox_by_msgid('test@example.com')
            url = request_url(rsps, 0)
            assert url.startswith('http://mymirror.local/all/')
            assert url.endswith('/t.mbox.gz')

    def test_url_rewriting_post(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """Verify URL rewriting works for POST requests too."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://ams.lore.kernel.org'],
            )
            rsps.add(
                responses.POST,
                'https://ams.lore.kernel.org/all/?x=m&q=test+query',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            node.get_mbox_by_query('test query')
            url = request_url(rsps, 0)
            assert url.startswith('https://ams.lore.kernel.org/all/')

    def test_validate_does_not_use_fallback(self) -> None:
        with responses.RequestsMock() as rsps:
            """validate() hits canonical URL only, ignoring fallbacks."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/all/_/text/help/',
                body=requests.ConnectionError('connection refused'),
            )

            with pytest.raises(RemoteError, match='Failed to reach'):
                node.validate()
            # Only 1 call to the canonical URL — no fallback
            assert len(rsps.calls) == 1
            url = request_url(rsps, 0)
            assert 'lore.kernel.org' in url

    def test_invalid_fallback_url_no_scheme(self) -> None:
        """Fallback URL without scheme raises LibloreError."""
        from liblore import LibloreError

        with pytest.raises(LibloreError, match='Invalid fallback URL'):
            LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['mirror.local'],
            )

    def test_invalid_fallback_url_with_path(self) -> None:
        """Fallback URL with a path component raises LibloreError."""
        from liblore import LibloreError

        with pytest.raises(LibloreError, match='must be a scheme://host origin'):
            LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local/some/path'],
            )

    def test_hostname_returns_canonical(self) -> None:
        """hostname property reflects the canonical URL, not fallbacks."""
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=['http://mirror.local'],
        )
        assert node.hostname == 'lore.kernel.org'

    def test_multiple_fallbacks_tried_in_order(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """With 3 fallbacks, they are tried in the configured order."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=[
                    'http://mirror1.local',
                    'https://ams.lore.kernel.org',
                ],
            )
            rsps.add(
                responses.GET,
                'http://mirror1.local/all/test%40example.com/t.mbox.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.GET,
                'https://ams.lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )

            result = node.get_mbox_by_msgid('test@example.com')
            assert result == sample_mbox
            assert len(rsps.calls) == 3
            urls = [request_url(rsps, i) for i in range(len(rsps.calls))]
            assert urls[0].startswith('http://mirror1.local/')
            assert urls[1].startswith('https://ams.lore.kernel.org/')
            assert urls[2].startswith('https://lore.kernel.org/')

    # =====================================================================
    # Origin probing
    # =====================================================================


class TestProbeOrigins:
    """Tests for the probe_origins() fastest-mirror feature."""

    def test_probe_reorders_by_latency(self) -> None:
        """Origins are reordered fastest-first after probing."""
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=[
                'https://slow.example.com',
                'https://fast.example.com',
            ],
        )
        # Map each origin to a deterministic fake latency
        latencies = {
            'slow.example.com': 2.0,
            'fast.example.com': 0.1,
            'lore.kernel.org': 0.5,
        }

        def fake_probe_one(origin: str) -> tuple[str, float]:
            for host, lat in latencies.items():
                if host in origin:
                    return origin, lat
            return origin, float('inf')

        with patch.object(node, '_probe_one', side_effect=fake_probe_one):
            results = node.probe_origins()

        assert len(results) == 3
        origins = [o for o, _ in results]
        assert origins[0] == 'https://fast.example.com'
        assert origins[1] == 'https://lore.kernel.org'
        assert origins[2] == 'https://slow.example.com'
        # _all_origins should be reordered too
        assert node._all_origins == origins

    def test_probe_unreachable_sorted_last(self) -> None:
        with responses.RequestsMock() as rsps:
            """Unreachable origins get inf elapsed and sort to the end."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://dead.example.com'],
            )

            rsps.add(
                responses.HEAD,
                'https://dead.example.com/manifest.js.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            results = node.probe_origins()

            assert len(results) == 2
            # canonical should be first (reachable), dead last
            assert results[0][0] == 'https://lore.kernel.org'
            assert results[0][1] < float('inf')
            assert results[1][0] == 'https://dead.example.com'
            assert results[1][1] == float('inf')

    def test_probe_4xx_treated_as_unreachable(self) -> None:
        with responses.RequestsMock() as rsps:
            """Origins returning 4xx are treated as unreachable."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://nomanifest.example.com'],
            )

            rsps.add(
                responses.HEAD,
                'https://nomanifest.example.com/manifest.js.gz',
                status=404,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            results = node.probe_origins()

            assert results[0][0] == 'https://lore.kernel.org'
            assert results[1][1] == float('inf')

    def test_probe_single_origin_noop(self) -> None:
        """With only one origin, probe is a no-op."""
        node = LoreNode('https://lore.kernel.org/all')
        results = node.probe_origins()
        assert len(results) == 1
        assert results[0] == ('https://lore.kernel.org', 0.0)
        assert node._probe_done is True

    def test_probe_uses_manifest_url(self) -> None:
        with responses.RequestsMock() as rsps:
            """Probe hits /manifest.js.gz on each origin."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            probed_urls: list[str] = []

            def callback(
                request: requests.PreparedRequest,
            ) -> tuple[int, dict[str, str], str]:
                assert request.url is not None
                probed_urls.append(request.url)
                return 200, {}, ''

            rsps.add_callback(
                responses.HEAD,
                'http://mirror.local/manifest.js.gz',
                callback=callback,
            )
            rsps.add_callback(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                callback=callback,
            )
            node.probe_origins()

            assert len(probed_urls) == 2
            assert 'http://mirror.local/manifest.js.gz' in probed_urls
            assert 'https://lore.kernel.org/manifest.js.gz' in probed_urls

    def test_probe_sends_user_agent(self) -> None:
        with responses.RequestsMock() as rsps:
            """Probe requests include the configured User-Agent."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            node.set_user_agent('myapp', '1.0')

            captured_headers: list[dict[str, str]] = []

            def callback(
                request: requests.PreparedRequest,
            ) -> tuple[int, dict[str, str], str]:
                captured_headers.append(dict(request.headers))
                return 200, {}, ''

            rsps.add_callback(
                responses.HEAD,
                'http://mirror.local/manifest.js.gz',
                callback=callback,
            )
            rsps.add_callback(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                callback=callback,
            )
            node.probe_origins()

            for h in captured_headers:
                assert h['User-Agent'] == 'myapp/1.0'

    def test_auto_probe_triggers_on_first_request(
        self,
        sample_mbox: bytes,
    ) -> None:
        with responses.RequestsMock() as rsps:
            """With auto_probe=True, first _request() triggers probe."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                auto_probe=True,
            )
            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.GET,
                'https://fast.example.com/all/test%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            node.get_mbox_by_msgid('test@example.com')

            assert node._probe_done is True

    def test_auto_probe_only_once(self, sample_mbox: bytes) -> None:
        with responses.RequestsMock() as rsps:
            """auto_probe fires only on the first request, not subsequent ones."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                auto_probe=True,
            )
            probe_count = 0

            def callback(
                _request: requests.PreparedRequest,
            ) -> tuple[int, dict[str, str], str]:
                nonlocal probe_count
                probe_count += 1
                return 200, {}, ''

            rsps.add_callback(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                callback=callback,
            )
            rsps.add_callback(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                callback=callback,
            )
            rsps.add(
                responses.GET,
                'https://fast.example.com/all/first%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            rsps.add(
                responses.GET,
                'https://fast.example.com/all/second%40example.com/t.mbox.gz',
                body=gzip.compress(sample_mbox),
                status=200,
            )
            node.get_mbox_by_msgid('first@example.com')
            first_probe_count = probe_count
            node.get_mbox_by_msgid('second@example.com')

            # Second request should NOT trigger another probe
            assert probe_count == first_probe_count

    def test_probe_cache_write_and_read(self, tmp_path: object) -> None:
        with responses.RequestsMock() as rsps:
            """Probe results are cached and restored on next probe call."""
            cache_dir = str(tmp_path)
            node1 = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                cache_dir=cache_dir,
            )

            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            with patch('liblore.node.time.monotonic') as mock_mono:
                # fast: 0.1s, canonical: 0.5s
                mock_mono.side_effect = [0.0, 0.1, 0.0, 0.5]
                node1.probe_origins()

            expected_order = node1._all_origins[:]

            # New node with same origins should get cached order
            node2 = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                cache_dir=cache_dir,
            )
            # Without patching requests.head — cache should be used
            node2.probe_origins()
            assert node2._all_origins == expected_order

    def test_probe_cache_expired(self, tmp_path: object) -> None:
        with responses.RequestsMock() as rsps:
            """Expired probe cache triggers a fresh probe."""
            cache_dir = str(tmp_path)
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                cache_dir=cache_dir,
                probe_ttl=10,
            )

            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            node.probe_origins()

            # Backdate cache file to force expiry
            import glob as glob_mod

            for f in glob_mod.glob(os.path.join(cache_dir, '*.lore.cache')):
                os.utime(f, (0, 0))

            node._probe_done = False
            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            node.probe_origins()
            assert len(rsps.calls) == 4

    def test_probe_cache_ignored_when_origins_change(
        self,
        tmp_path: object,
    ) -> None:
        with responses.RequestsMock() as rsps:
            """Cache is ignored when the set of origins differs."""
            cache_dir = str(tmp_path)
            node1 = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                cache_dir=cache_dir,
            )

            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            node1.probe_origins()

            # New node with DIFFERENT fallbacks
            node2 = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://other.example.com'],
                cache_dir=cache_dir,
            )

            rsps.add(
                responses.HEAD,
                'https://other.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            node2.probe_origins()

            # Different origins → cache miss → fresh probe
            assert len(rsps.calls) == 4

    def test_probe_nocache_skips_cache(self, tmp_path: object) -> None:
        with responses.RequestsMock() as rsps:
            """nocache=True forces a live probe even when cache is fresh."""
            cache_dir = str(tmp_path)
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['https://fast.example.com'],
                cache_dir=cache_dir,
            )

            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            # First probe — populates cache
            with patch('liblore.node.time.monotonic') as mock_mono:
                mock_mono.side_effect = [0.0, 0.1, 0.0, 0.5]
                node.probe_origins()

            # Second probe with nocache — should do a live probe, not read cache
            node._probe_done = False
            rsps.add(
                responses.HEAD,
                'https://fast.example.com/manifest.js.gz',
                status=200,
            )
            rsps.add(
                responses.HEAD,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )
            with patch('liblore.node.time.monotonic') as mock_mono:
                mock_mono.side_effect = [0.0, 1.0, 2.0, 3.0]
                results = node.probe_origins(nocache=True)

            assert len(rsps.calls) == 4
            # Results should have real timing, not 0.0
            assert all(elapsed > 0.0 for _, elapsed in results)

    # =====================================================================
    # Git config integration
    # =====================================================================


class TestGitBool:
    """Tests for the _git_bool() helper."""

    @pytest.mark.parametrize(
        'value,expected',
        [
            ('true', True),
            ('TRUE', True),
            ('yes', True),
            ('On', True),
            ('1', True),
            ('42', True),
            ('-1', True),
            ('false', False),
            ('No', False),
            ('off', False),
            ('0', False),
            ('', False),
            (' true ', True),
        ],
    )
    def test_git_values(self, value: str, expected: bool) -> None:
        from liblore.node import _git_bool

        assert _git_bool(value) is expected

    @pytest.mark.parametrize('value', ['maybe', 'tru', '1.5'])
    def test_unknown_value(self, value: str) -> None:
        from liblore.node import _git_bool

        assert _git_bool(value) is None


class TestFromGitConfig:
    """Tests for LoreNode.from_git_config() with legacy [lore] section.

    These tests simulate the [lore] fallback path by returning empty
    from _get_subsection_config (no [liblore] subsection found).
    """

    def test_reads_all_config_keys(self) -> None:
        """Reads all lore.* keys via [lore] fallback."""
        gitcfg: dict[str, str | list[str]] = {
            'fallback': [
                'https://tor.lore.kernel.org',
                'https://sea.lore.kernel.org',
            ],
            'autoprobe': 'true',
            'probetimeout': '10.0',
            'probettl': '7200',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        assert node._all_origins == [
            'https://tor.lore.kernel.org',
            'https://sea.lore.kernel.org',
            'https://lore.kernel.org',
        ]
        assert node._auto_probe is True
        assert node._probe_timeout == 10.0
        assert node._probe_ttl == 7200

    def test_explicit_kwargs_override_git_config(self) -> None:
        """Explicit kwargs take precedence over git config values."""
        gitcfg: dict[str, str | list[str]] = {
            'fallback': ['https://from-git-config.example.com'],
            'autoprobe': 'true',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config(
                fallback_urls=['https://explicit.example.com'],
                auto_probe=False,
            )

        assert node._all_origins == [
            'https://explicit.example.com',
            'https://lore.kernel.org',
        ]
        assert node._auto_probe is False

    def test_git_not_installed(self) -> None:
        """Works fine when both config helpers return empty."""
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            node = LoreNode.from_git_config()

        assert node._all_origins == ['https://lore.kernel.org']

    def test_no_config_keys(self) -> None:
        """Works fine when no lore.* keys exist in git config."""
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            node = LoreNode.from_git_config()

        assert node._all_origins == ['https://lore.kernel.org']
        assert node._auto_probe is False

    def test_invalid_probe_timeout_ignored(self) -> None:
        """Non-numeric lore.probetimeout is silently ignored."""
        gitcfg: dict[str, str | list[str]] = {'probetimeout': 'notanumber'}

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        assert node._probe_timeout == 5.0  # default

    def test_autoprobe_accepts_git_bools(self) -> None:
        """autoprobe = yes turns probing on, as it would in git."""
        gitcfg: dict[str, str | list[str]] = {'autoprobe': 'yes'}

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        assert node._auto_probe is True

    def test_invalid_autoprobe_keeps_default(self) -> None:
        """An autoprobe value git would reject keeps the default."""
        gitcfg: dict[str, str | list[str]] = {'autoprobe': 'maybe'}

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        assert node._auto_probe is False  # default

    def test_custom_url_passed_through(self) -> None:
        """The url argument is forwarded to __init__."""
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            node = LoreNode.from_git_config(
                url='https://my-inbox.example.com/lists',
            )

        assert node._url == 'https://my-inbox.example.com/lists'

    def test_reads_useragentplus(self) -> None:
        """Reads lore.useragentplus and applies it via set_user_agent."""
        gitcfg: dict[str, str | list[str]] = {
            'useragentplus': '550e8400-e29b-41d4',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        assert node._user_agent_plus == '550e8400-e29b-41d4'
        # Now when the caller sets their app name, plus is auto-applied
        node.set_user_agent('korgalore', '0.7')
        assert node._user_agent == 'korgalore/0.7+550e8400-e29b-41d4'

    def test_useragentplus_overridden_by_explicit_plus(self) -> None:
        """Explicit plus= in set_user_agent overrides git config value."""
        gitcfg: dict[str, str | list[str]] = {
            'useragentplus': 'from-git-config',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        node.set_user_agent('myapp', '1.0', plus='explicit')
        assert node._user_agent == 'myapp/1.0+explicit'

    def test_useragentplus_not_set_without_git_config(self) -> None:
        """Without git config, set_user_agent works as before."""
        node = LoreNode()
        node.set_user_agent('myapp', '1.0')
        assert node._user_agent == 'myapp/1.0'

    def test_useragentplus_applied_to_session(self) -> None:
        """The plus identifier propagates to the session headers."""
        gitcfg: dict[str, str | list[str]] = {
            'useragentplus': 'myuuid',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()

        node.set_user_agent('bugspray', '0.3')
        session = node._get_session()
        assert session.headers['User-Agent'] == 'bugspray/0.3+myuuid'


class TestGetConfigFromGit:
    """Tests for the _get_config_from_git() helper."""

    def test_parses_nul_separated_output(self) -> None:
        """Parses git config -z output correctly."""
        from liblore.node import _get_config_from_git

        result = subprocess.CompletedProcess[str](
            args=['git'],
            returncode=0,
            stdout=(
                'lore.fallback\nhttps://tor.lore.kernel.org\x00'
                'lore.fallback\nhttps://sea.lore.kernel.org\x00'
                'lore.autoprobe\ntrue\x00'
                'lore.probettl\n7200\x00'
            ),
        )
        with patch('liblore.node.subprocess.run', return_value=result):
            cfg = _get_config_from_git(r'^lore\.', multivals=['fallback'])

        assert cfg == {
            'fallback': [
                'https://tor.lore.kernel.org',
                'https://sea.lore.kernel.org',
            ],
            'autoprobe': 'true',
            'probettl': '7200',
        }

    def test_git_not_installed(self) -> None:
        """Returns empty dict when git is not installed."""
        from liblore.node import _get_config_from_git

        with patch(
            'liblore.node.subprocess.run',
            side_effect=FileNotFoundError('git not found'),
        ):
            cfg = _get_config_from_git(r'^lore\.')

        assert cfg == {}

    def test_no_matching_keys(self) -> None:
        """Returns empty dict when no keys match the regexp."""
        from liblore.node import _get_config_from_git

        result = subprocess.CompletedProcess[str](
            args=['git'],
            returncode=1,
            stdout='',
        )
        with patch('liblore.node.subprocess.run', return_value=result):
            cfg = _get_config_from_git(r'^lore\.')

        assert cfg == {}

    def test_bare_key_defaults_to_true(self) -> None:
        """A key without a value (no newline) defaults to 'true'."""
        from liblore.node import _get_config_from_git

        result = subprocess.CompletedProcess[str](
            args=['git'],
            returncode=0,
            stdout='lore.autoprobe\x00',
        )
        with patch('liblore.node.subprocess.run', return_value=result):
            cfg = _get_config_from_git(r'^lore\.')

        assert cfg == {'autoprobe': 'true'}


class TestGetSubsectionConfig:
    """Tests for the _get_subsection_config() helper."""

    def test_parses_subsection_keys(self) -> None:
        """Parses keys from [liblore "https://lore.kernel.org"]."""
        from liblore.node import _get_subsection_config

        result = subprocess.CompletedProcess[str](
            args=['git'],
            returncode=0,
            stdout=(
                'liblore.https://lore.kernel.org.fallback\nhttps://tor.lore.kernel.org\x00'
                'liblore.https://lore.kernel.org.fallback\nhttps://sea.lore.kernel.org\x00'
                'liblore.https://lore.kernel.org.autoprobe\ntrue\x00'
                'liblore.https://lore.kernel.org.useragentplus\nmyuuid\x00'
            ),
        )
        with patch('liblore.node.subprocess.run', return_value=result):
            cfg = _get_subsection_config(
                'liblore',
                'https://lore.kernel.org',
                multivals=['fallback'],
            )

        assert cfg == {
            'fallback': [
                'https://tor.lore.kernel.org',
                'https://sea.lore.kernel.org',
            ],
            'autoprobe': 'true',
            'useragentplus': 'myuuid',
        }

    def test_dots_in_subsection_handled(self) -> None:
        """Subsection names with dots (URLs) are parsed correctly."""
        from liblore.node import _get_subsection_config

        result = subprocess.CompletedProcess[str](
            args=['git'],
            returncode=0,
            stdout='liblore.https://subspace.kernel.org.fallback\nhttps://mirror.example.com\x00',
        )
        with patch('liblore.node.subprocess.run', return_value=result):
            cfg = _get_subsection_config(
                'liblore',
                'https://subspace.kernel.org',
                multivals=['fallback'],
            )

        assert cfg == {
            'fallback': ['https://mirror.example.com'],
        }

    def test_no_matching_subsection(self) -> None:
        """Returns empty dict when no keys match the subsection."""
        from liblore.node import _get_subsection_config

        result = subprocess.CompletedProcess[str](
            args=['git'],
            returncode=1,
            stdout='',
        )
        with patch('liblore.node.subprocess.run', return_value=result):
            cfg = _get_subsection_config(
                'liblore',
                'https://nonexistent.example.com',
            )

        assert cfg == {}

    def test_git_not_installed(self) -> None:
        """Returns empty dict when git is not installed."""
        from liblore.node import _get_subsection_config

        with patch(
            'liblore.node.subprocess.run',
            side_effect=FileNotFoundError('git not found'),
        ):
            cfg = _get_subsection_config(
                'liblore',
                'https://lore.kernel.org',
            )

        assert cfg == {}


class TestFromGitConfigSubsections:
    """Tests for from_git_config() with [liblore "<origin>"] subsections."""

    def test_subsection_used_for_lore(self) -> None:
        """[liblore "https://lore.kernel.org"] is used when present."""
        subsection_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://tor.lore.kernel.org'],
            'autoprobe': 'true',
            'useragentplus': 'subsection-uuid',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value=subsection_cfg),
            patch('liblore.node._get_config_from_git', return_value={}) as mock_legacy,
        ):
            node = LoreNode.from_git_config()

        assert node._all_origins == [
            'https://tor.lore.kernel.org',
            'https://lore.kernel.org',
        ]
        assert node._auto_probe is True
        assert node._user_agent_plus == 'subsection-uuid'
        # [lore] should NOT be consulted when subsection has config
        mock_legacy.assert_not_called()

    def test_falls_back_to_lore_section(self) -> None:
        """Falls back to [lore] when no [liblore] subsection for lore.kernel.org."""
        legacy_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://sea.lore.kernel.org'],
            'autoprobe': 'true',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=legacy_cfg),
        ):
            node = LoreNode.from_git_config()

        assert node._all_origins == [
            'https://sea.lore.kernel.org',
            'https://lore.kernel.org',
        ]
        assert node._auto_probe is True

    def test_no_fallback_to_lore_for_non_lore_url(self) -> None:
        """Non-lore URLs do NOT fall back to [lore] section."""
        legacy_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://tor.lore.kernel.org'],
            'autoprobe': 'true',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch(
                'liblore.node._get_config_from_git', return_value=legacy_cfg
            ) as mock_legacy,
        ):
            node = LoreNode.from_git_config(
                url='https://subspace.kernel.org/_lists/helpdesk',
            )

        # Should have NO fallbacks — [lore] mirrors don't serve subspace
        assert node._all_origins == ['https://subspace.kernel.org']
        assert node._auto_probe is False
        # [lore] must not be consulted for non-lore URLs
        mock_legacy.assert_not_called()

    def test_non_lore_url_with_own_subsection(self) -> None:
        """Non-lore URLs use their own [liblore "<origin>"] section."""
        subsection_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://subspace-mirror.kernel.org'],
            'useragentplus': 'subspace-token',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value=subsection_cfg),
            patch('liblore.node._get_config_from_git') as mock_legacy,
        ):
            node = LoreNode.from_git_config(
                url='https://subspace.kernel.org/_lists/helpdesk',
            )

        assert node._all_origins == [
            'https://subspace-mirror.kernel.org',
            'https://subspace.kernel.org',
        ]
        assert node._user_agent_plus == 'subspace-token'
        mock_legacy.assert_not_called()

    def test_subsection_overrides_lore_section(self) -> None:
        """[liblore "https://lore.kernel.org"] takes precedence over [lore]."""
        subsection_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://preferred-mirror.example.com'],
            'useragentplus': 'new-uuid',
        }
        legacy_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://old-mirror.example.com'],
            'useragentplus': 'old-uuid',
        }

        with (
            patch('liblore.node._get_subsection_config', return_value=subsection_cfg),
            patch(
                'liblore.node._get_config_from_git', return_value=legacy_cfg
            ) as mock_legacy,
        ):
            node = LoreNode.from_git_config()

        # Subsection wins
        assert node._all_origins == [
            'https://preferred-mirror.example.com',
            'https://lore.kernel.org',
        ]
        assert node._user_agent_plus == 'new-uuid'
        # Legacy not even consulted
        mock_legacy.assert_not_called()

    def test_explicit_kwargs_override_subsection(self) -> None:
        """Explicit kwargs still take precedence over subsection config."""
        subsection_cfg: dict[str, str | list[str]] = {
            'fallback': ['https://from-config.example.com'],
            'autoprobe': 'true',
        }

        with patch('liblore.node._get_subsection_config', return_value=subsection_cfg):
            node = LoreNode.from_git_config(
                fallback_urls=['https://explicit.example.com'],
                auto_probe=False,
            )

        assert node._all_origins == [
            'https://explicit.example.com',
            'https://lore.kernel.org',
        ]
        assert node._auto_probe is False


# =====================================================================
# Public API: request()
# =====================================================================


class TestRequest:
    """Tests for the public request() method."""

    def test_delegates_to_private_request(self) -> None:
        with responses.RequestsMock() as rsps:
            """request() delegates to _request() with raise_on_error=True."""
            node = LoreNode('https://lore.kernel.org/all')
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )

            resp = node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
            assert resp.status_code == 200

    def test_failover_works(self) -> None:
        with responses.RequestsMock() as rsps:
            """First origin fails, second succeeds."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/manifest.js.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/manifest.js.gz',
                status=200,
            )

            resp = node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
            assert resp.status_code == 200
            assert len(rsps.calls) == 2

    def test_raises_remote_error_when_all_fail(self) -> None:
        with responses.RequestsMock() as rsps:
            """RemoteError raised when every origin fails."""
            node = LoreNode(
                'https://lore.kernel.org/all',
                fallback_urls=['http://mirror.local'],
            )
            rsps.add(
                responses.GET,
                'http://mirror.local/manifest.js.gz',
                body=requests.ConnectionError('refused'),
            )
            rsps.add(
                responses.GET,
                'https://lore.kernel.org/manifest.js.gz',
                body=requests.ConnectionError('refused'),
            )

            with pytest.raises(RemoteError, match='All hosts failed') as ei:
                node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
            assert ei.value.status_code is None

    def test_kwargs_forwarded(self) -> None:
        """Extra kwargs (e.g. timeout) are passed through."""
        node = LoreNode('https://lore.kernel.org/all')
        with patch.object(
            node, '_request', return_value=MagicMock(status_code=200)
        ) as mock_request:
            node.request(
                'GET',
                'https://lore.kernel.org/manifest.js.gz',
                timeout=30,
            )

        mock_request.assert_called_once_with(
            'GET',
            'https://lore.kernel.org/manifest.js.gz',
            raise_on_error=True,
            timeout=30,
        )

    # =====================================================================
    # Public API: user_agent_plus property
    # =====================================================================


class TestUserAgentPlusProperty:
    """Tests for the user_agent_plus read-only property."""

    def test_none_when_created_directly(self) -> None:
        """None when created via LoreNode() without git config."""
        node = LoreNode()
        assert node.user_agent_plus is None

    def test_set_via_from_git_config(self) -> None:
        """Populated when created via from_git_config() with the key set."""
        gitcfg: dict[str, str | list[str]] = {
            'useragentplus': 'my-tracking-uuid',
        }
        with patch('liblore.node._get_config_from_git', return_value=gitcfg):
            node = LoreNode.from_git_config()

        assert node.user_agent_plus == 'my-tracking-uuid'


# =====================================================================
# Public API: origins property
# =====================================================================


class TestOriginsProperty:
    """Tests for the origins read-only property."""

    def test_returns_fallbacks_plus_canonical(self) -> None:
        """Returns fallbacks followed by the canonical origin."""
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=['http://mirror.local', 'https://ams.lore.kernel.org'],
        )
        assert node.origins == [
            'http://mirror.local',
            'https://ams.lore.kernel.org',
            'https://lore.kernel.org',
        ]

    def test_returns_copy(self) -> None:
        """Mutating the returned list does not affect the node."""
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=['http://mirror.local'],
        )
        origins = node.origins
        origins.append('https://injected.example.com')
        assert 'https://injected.example.com' not in node.origins

    def test_reflects_reordering_after_probe(self) -> None:
        """After probe_origins(), list reflects the new order."""
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=['https://slow.example.com', 'https://fast.example.com'],
        )
        latencies = {
            'slow.example.com': 2.0,
            'fast.example.com': 0.1,
            'lore.kernel.org': 0.5,
        }

        def fake_probe_one(origin: str) -> tuple[str, float]:
            for host, lat in latencies.items():
                if host in origin:
                    return origin, lat
            return origin, float('inf')

        with patch.object(node, '_probe_one', side_effect=fake_probe_one):
            node.probe_origins()

        assert node.origins[0] == 'https://fast.example.com'


# =====================================================================
# Public API: canonical_origin property
# =====================================================================


class TestCanonicalOriginProperty:
    """Tests for the canonical_origin read-only property."""

    def test_matches_scheme_host(self) -> None:
        """Returns scheme://host from the URL passed to __init__."""
        node = LoreNode('https://lore.kernel.org/all')
        assert node.canonical_origin == 'https://lore.kernel.org'

    def test_no_path_component(self) -> None:
        """The origin has no path, even when the URL does."""
        node = LoreNode('https://lore.kernel.org/some/deep/path')
        assert node.canonical_origin == 'https://lore.kernel.org'
        assert '/some' not in node.canonical_origin

    def test_preserves_port(self) -> None:
        """Port number is included in the origin when present."""
        node = LoreNode('http://localhost:8080/inbox')
        assert node.canonical_origin == 'http://localhost:8080'


# =====================================================================
# Cancellation and request timeouts
# =====================================================================


class TestCancellation:
    """Tests for operation-scoped cancellation: cancel_active()/shutdown()."""

    def test_cancel_active_closes_owned_session(self) -> None:
        """cancel_active() closes an owned session and nulls it out."""
        node = LoreNode()
        session = node._get_session()  # owned
        with patch.object(session, 'close') as mock_close:
            node.cancel_active()
            mock_close.assert_called_once()
        assert node._session is None
        assert node._owns_session is False

    def test_shutdown_closes_owned_session(self) -> None:
        """shutdown() closes an owned session and nulls it out."""
        node = LoreNode()
        session = node._get_session()  # owned
        with patch.object(session, 'close') as mock_close:
            node.shutdown()
            mock_close.assert_called_once()
        assert node._session is None
        assert node._owns_session is False

    def test_cancel_active_leaves_injected_session(self) -> None:
        """cancel_active() must not close or drop an injected session."""
        node = LoreNode()
        mock_session = MagicMock()
        node.set_requests_session(mock_session)
        node.cancel_active()
        mock_session.close.assert_not_called()
        assert node._session is mock_session

    def test_new_operation_after_cancel_active_proceeds(self) -> None:
        """cancel_active() does not poison operations started later.

        This is the structural fix for the stale-cancel-flag bug class:
        there is no sticky flag, so a fresh operation needs no reset.
        """
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)
        node.cancel_active()
        resp = node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        assert resp.status_code == 200

    def test_cancel_active_mid_flight_does_not_failover(self) -> None:
        """A socket closed by cancel_active() cancels, without failover.

        The session being closed mid-flight surfaces as a ConnectionError,
        which would normally trigger failover to the next origin.  When the
        operation was cancelled, it must convert to OperationCancelledError
        instead and stop — no failover, no RemoteError.
        """
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=['https://mirror.example.com'],
        )
        mock_session = MagicMock()

        def boom(*_args: object, **_kwargs: object) -> object:
            # Simulate cancel_active() closing the socket during the read.
            node.cancel_active()
            raise requests.ConnectionError('socket closed')

        mock_session.get.side_effect = boom
        node.set_requests_session(mock_session)

        with pytest.raises(OperationCancelledError):
            node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        # Only the first origin was tried — no failover to the mirror.
        assert mock_session.get.call_count == 1

    def test_cancelled_operation_stays_cancelled(self) -> None:
        """Within one operation a cancel is sticky; outside it is not."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)

        with node._operation():
            node.cancel_active()
            # Every request for the rest of this operation raises...
            for _ in range(2):
                with pytest.raises(OperationCancelledError):
                    node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        # ...and none of them was dispatched over the wire.
        mock_session.get.assert_not_called()

        # A fresh operation proceeds normally.
        resp = node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        assert resp.status_code == 200

    def test_shutdown_refuses_new_operations(self) -> None:
        """After shutdown(), starting an operation raises immediately."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        node.set_requests_session(mock_session)
        assert node.is_shutdown is False
        node.shutdown()
        assert node.is_shutdown is True
        with pytest.raises(OperationCancelledError):
            node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        # Refused at operation entry — no HTTP call was attempted.
        mock_session.get.assert_not_called()

    def test_shutdown_mid_flight_does_not_failover(self) -> None:
        """A socket closed by shutdown() cancels, without failover."""
        node = LoreNode(
            'https://lore.kernel.org/all',
            fallback_urls=['https://mirror.example.com'],
        )
        mock_session = MagicMock()

        def boom(*_args: object, **_kwargs: object) -> object:
            node.shutdown()
            raise requests.ConnectionError('socket closed')

        mock_session.get.side_effect = boom
        node.set_requests_session(mock_session)

        with pytest.raises(OperationCancelledError):
            node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        assert mock_session.get.call_count == 1

    def test_batch_cancel_between_requests_stops_batch(
        self, sample_mbox: bytes
    ) -> None:
        """A cancel landing between batch items stops the whole batch.

        The batch methods are one operation, so a cancel_active() that
        arrives while no request is in flight (e.g. during the
        inter-item cooldown) must still stop the remaining items.
        """
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()

        def first_ok_then_cancelled(*_args: object, **_kwargs: object) -> object:
            # The cancel lands while item one is still being processed,
            # i.e. between the batch's HTTP requests.
            node.cancel_active()
            return MagicMock(status_code=200, content=gzip.compress(sample_mbox))

        mock_session.get.side_effect = first_ok_then_cancelled
        node.set_requests_session(mock_session)

        with pytest.raises(OperationCancelledError):
            node.batch_get_thread_by_msgid(['first@example.com', 'first@example.com'])
        # Item one completed; item two was never fetched.
        assert mock_session.get.call_count == 1

    def test_message_cancel_mid_flight_is_not_remote_error(self) -> None:
        """A cancel during get_message_by_msgid() raises OperationCancelledError.

        get_message_by_msgid() turns unexpected errors into RemoteError;
        a cancel must not be one of them, or callers that catch
        RemoteError would treat the cancel as a server failure.
        """
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()

        def boom(*_args: object, **_kwargs: object) -> object:
            node.cancel_active()
            raise requests.ConnectionError('socket closed')

        mock_session.get.side_effect = boom
        node.set_requests_session(mock_session)

        with pytest.raises(OperationCancelledError):
            node.get_message_by_msgid('first@example.com')

    def test_validate_cancel_mid_flight_is_not_remote_error(self) -> None:
        """A cancel during validate() raises OperationCancelledError."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()

        def boom(*_args: object, **_kwargs: object) -> object:
            node.cancel_active()
            raise requests.ConnectionError('socket closed')

        mock_session.head.side_effect = boom
        node.set_requests_session(mock_session)

        with pytest.raises(OperationCancelledError):
            node.validate()

    def test_nested_operations_join_the_outermost(self) -> None:
        """Nested _operation() scopes share the outer generation."""
        node = LoreNode()
        with node._operation():
            outer_generation = node._op_local.generation
            with node._operation():
                assert node._op_local.depth == 2
                assert node._op_local.generation == outer_generation
            assert node._op_local.depth == 1
        assert node._op_local.depth == 0
        assert node._op_local.generation is None

    def test_cancel_is_deprecated_alias_for_cancel_active(self) -> None:
        """cancel() warns and behaves exactly like cancel_active()."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)
        with pytest.warns(DeprecationWarning, match='cancel_active'):
            node.cancel()
        assert node._cancel_generation == 1
        # Not sticky: a fresh operation proceeds without any reset.
        resp = node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        assert resp.status_code == 200

    def test_reset_cancel_is_a_deprecated_noop(self) -> None:
        """reset_cancel() warns and changes no cancellation state."""
        node = LoreNode()
        with pytest.warns(DeprecationWarning, match='operation-scoped'):
            node.reset_cancel()
        assert node._cancel_generation == 0
        assert node.is_shutdown is False

    def test_default_timeout_injected(self) -> None:
        """A request without an explicit timeout gets the default."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)
        node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        _, kwargs = mock_session.get.call_args
        assert kwargs['timeout'] == (5.0, 30.0)

    def test_explicit_timeout_preserved(self) -> None:
        """An explicit per-call timeout overrides the default."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)
        node.request('GET', 'https://lore.kernel.org/manifest.js.gz', timeout=99)
        _, kwargs = mock_session.get.call_args
        assert kwargs['timeout'] == 99

    def test_custom_request_timeout_from_init(self) -> None:
        """request_timeout passed to __init__ is the injected default."""
        node = LoreNode('https://lore.kernel.org/all', request_timeout=12.0)
        mock_session = MagicMock()
        mock_session.get.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)
        node.request('GET', 'https://lore.kernel.org/manifest.js.gz')
        _, kwargs = mock_session.get.call_args
        assert kwargs['timeout'] == 12.0

    def test_resolve_via_head_passes_timeout(self) -> None:
        """_resolve_msgid_via_head() applies the request timeout."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        head_resp = MagicMock(status_code=404)
        head_resp.url = 'https://lore.kernel.org/q/'  # no redirect
        mock_session.head.return_value = head_resp
        node.set_requests_session(mock_session)
        node._resolve_msgid_via_head('q')
        _, kwargs = mock_session.head.call_args
        assert kwargs['timeout'] == (5.0, 30.0)

    def test_validate_passes_timeout(self) -> None:
        """validate() applies the request timeout to its HEAD call."""
        node = LoreNode('https://lore.kernel.org/all')
        mock_session = MagicMock()
        mock_session.head.return_value = MagicMock(status_code=200)
        node.set_requests_session(mock_session)
        node.validate()
        _, kwargs = mock_session.head.call_args
        assert kwargs['timeout'] == (5.0, 30.0)

    def test_from_git_config_single_float(self) -> None:
        """A single-float requesttimeout is parsed into a float."""
        gitcfg: dict[str, str | list[str]] = {'requesttimeout': '15'}
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()
        assert node._request_timeout == 15.0

    def test_from_git_config_connect_read_tuple(self) -> None:
        """A "connect,read" requesttimeout is parsed into a tuple."""
        gitcfg: dict[str, str | list[str]] = {'requesttimeout': '3,20'}
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()
        assert node._request_timeout == (3.0, 20.0)

    def test_from_git_config_invalid_ignored(self) -> None:
        """A non-numeric requesttimeout is silently ignored (default kept)."""
        gitcfg: dict[str, str | list[str]] = {'requesttimeout': 'notanumber'}
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=gitcfg),
        ):
            node = LoreNode.from_git_config()
        assert node._request_timeout == (5.0, 30.0)
