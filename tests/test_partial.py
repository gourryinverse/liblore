# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for partial mirror support (X-Archive-Coverage / X-Archive-Upstream)."""

from __future__ import annotations

import gzip
import json
import logging
import os
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import requests
import responses

from liblore import (
    LibloreError,
    NotOnMirrorError,
    OperationCancelledError,
    RemoteError,
)
from liblore.node import (
    LoreNode,
    _LoreNodeInitKwargs,
    _normalize_url,
    _parse_coverage,
)

if TYPE_CHECKING:
    from typing_extensions import Unpack

LOCAL = 'http://127.0.0.1:11043/lore/all'
RAW = f'{LOCAL}/x%40example.com/raw'
UPSTREAM = 'https://lore.kernel.org/all'
PARTIAL = {
    'X-Archive-Coverage': 'partial',
    'X-Archive-Upstream': 'https://lore.kernel.org/all/',
}
# A partial mirror naming an upstream that is not trusted by default.
OTHER = {**PARTIAL, 'X-Archive-Upstream': 'https://mirror.example.com/all'}


@pytest.fixture(autouse=True)
def _reset_warnings() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Each test starts with no "untrusted upstream" or outage warning given yet."""
    with (
        patch('liblore.node._warned_untrusted', set[str]()),
        patch('liblore.node._warned_outage', set[tuple[str, str]]()),
    ):
        yield


def fetch(
    node: LoreNode,
    status: int = 200,
    headers: dict[str, str] | None = None,
    url: str = RAW,
) -> None:
    """Make *node* receive one response with *status* and *headers*."""
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, url, status=status, headers=headers or {})
        node.request('GET', url)


def upstream_files(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob('*.lore.upstream'))


# =====================================================================
# Helpers
# =====================================================================


class TestParseCoverage:
    def test_token_only(self) -> None:
        assert _parse_coverage('partial') == ('partial', {})

    def test_parameters(self) -> None:
        assert _parse_coverage(' Partial ; Updated = 1790000000 ; future ') == (
            'partial',
            {'updated': '1790000000'},
        )

    def test_full(self) -> None:
        assert _parse_coverage('full') == ('full', {})


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        'url,expected',
        [
            ('HTTPS://Lore.Kernel.org/all/', 'https://lore.kernel.org/all'),
            ('https://lore.kernel.org:443/all', 'https://lore.kernel.org/all'),
            ('http://127.0.0.1:80/lore/all', 'http://127.0.0.1/lore/all'),
            ('http://127.0.0.1:11043/lore/all/', 'http://127.0.0.1:11043/lore/all'),
            ('https://example.com:80/All', 'https://example.com:80/All'),
            ('http://[::1]:11043/lore', 'http://[::1]:11043/lore'),
        ],
    )
    def test_normalize(self, url: str, expected: str) -> None:
        assert _normalize_url(url) == expected

    @pytest.mark.parametrize(
        'url',
        ['/lore/all', 'lore.kernel.org', 'http://x:99999', 'https://u:p@example.com'],
    )
    def test_invalid(self, url: str) -> None:
        with pytest.raises(ValueError):
            _normalize_url(url)


# =====================================================================
# L1: learning that a node is partial
# =====================================================================


class TestLearnUpstream:
    def test_partial_learns_upstream(self) -> None:
        node = LoreNode(LOCAL)
        fetch(node, headers=PARTIAL)
        # The trailing / from the header is dropped.
        assert node._get_upstream_url() == UPSTREAM

    def test_no_headers_is_full(self) -> None:
        node = LoreNode(LOCAL)
        fetch(node)
        assert node._get_upstream_url() is None

    @pytest.mark.parametrize('coverage', ['full', 'complete', ''])
    def test_other_tokens_are_full(self, coverage: str) -> None:
        node = LoreNode(LOCAL)
        fetch(node, headers={**PARTIAL, 'X-Archive-Coverage': coverage})
        assert node._get_upstream_url() is None

    def test_upstream_without_partial_ignored(self) -> None:
        node = LoreNode(LOCAL)
        fetch(node, headers={'X-Archive-Upstream': UPSTREAM})
        assert node._get_upstream_url() is None

    def test_partial_without_upstream(self) -> None:
        node = LoreNode(LOCAL)
        fetch(node, headers={'X-Archive-Coverage': 'partial'})
        assert node._get_upstream_url() is None

    def test_token_case_and_parameters(self) -> None:
        node = LoreNode(LOCAL)
        headers = {**PARTIAL, 'X-Archive-Coverage': 'PARTIAL; updated=1790000000'}
        fetch(node, headers=headers)
        assert node._get_upstream_url() == UPSTREAM

    def test_learned_on_404(self) -> None:
        """A miss is the most likely first answer from a partial mirror."""
        node = LoreNode(LOCAL)
        fetch(node, status=404, headers=PARTIAL)
        assert node._get_upstream_url() == UPSTREAM

    def test_learned_on_5xx_with_headers(self) -> None:
        node = LoreNode(LOCAL)
        fetch(node, status=503, headers=PARTIAL)
        assert node._get_upstream_url() == UPSTREAM

    def test_learned_from_fallback_origin(self) -> None:
        node = LoreNode(
            'https://lore.example.org/all',
            fallback_urls=['http://127.0.0.1:11043'],
            allowed_upstreams=['https://lore.example.org'],
        )
        url = 'https://lore.example.org/all/x%40example.com/raw'
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                'http://127.0.0.1:11043/all/x%40example.com/raw',
                headers={
                    **PARTIAL,
                    'X-Archive-Upstream': 'https://lore.kernel.org/all',
                },
            )
            node.request('GET', url)
        assert node._get_upstream_url() == UPSTREAM

    @pytest.mark.parametrize(
        'value',
        [
            '/all/',
            'lore.kernel.org/all',
            'ftp://lore.kernel.org/all',
            'https://lore.kernel.org/all?x=1',
            'https://user@lore.kernel.org/all',
            'https://lore.kernel.org:99999/all',
        ],
    )
    def test_invalid_upstream_ignored(self, value: str) -> None:
        node = LoreNode(LOCAL)
        fetch(node, headers={**PARTIAL, 'X-Archive-Upstream': value})
        assert node._get_upstream_url() is None

    def test_self_reference_ignored(self) -> None:
        node = LoreNode(LOCAL, allowed_upstreams=['http://127.0.0.1:11043'])
        headers = {**PARTIAL, 'X-Archive-Upstream': 'HTTP://127.0.0.1:11043/lore/all/'}
        fetch(node, headers=headers)
        assert node._get_upstream_url() is None

    def test_normalized_upstream(self) -> None:
        node = LoreNode(LOCAL)
        headers = {**PARTIAL, 'X-Archive-Upstream': 'HTTPS://Lore.Kernel.org:443/all/'}
        fetch(node, headers=headers)
        assert node._get_upstream_url() == UPSTREAM

    def test_follow_upstream_off(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, follow_upstream=False, cache_dir=str(tmp_path))
        fetch(node, headers=PARTIAL)
        assert node._get_upstream_url() is None
        assert upstream_files(tmp_path) == []


class TestTrust:
    def test_untrusted_ignored(self) -> None:
        node = LoreNode(LOCAL)
        fetch(node, headers=OTHER)
        assert node._get_upstream_url() is None

    def test_untrusted_warns_once_per_process(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger='liblore.node'):
            for _ in range(2):
                node = LoreNode(LOCAL)
                fetch(node, headers=OTHER)
                fetch(node, headers=OTHER)
        warnings = [r for r in caplog.records if 'untrusted' in r.getMessage()]
        assert len(warnings) == 1
        assert 'https://mirror.example.com' in warnings[0].getMessage()

    def test_allowed_upstream_trusted(self) -> None:
        node = LoreNode(LOCAL, allowed_upstreams=['HTTPS://Mirror.Example.com:443/'])
        fetch(node, headers=OTHER)
        assert node._get_upstream_url() == 'https://mirror.example.com/all'

    def test_allowed_is_per_origin(self) -> None:
        """Trusting a host on one port does not trust it on another."""
        node = LoreNode(LOCAL, allowed_upstreams=['https://mirror.example.com:8443'])
        fetch(node, headers=OTHER)
        assert node._get_upstream_url() is None

    def test_plain_http_lore_not_trusted(self) -> None:
        """Only https://lore.kernel.org is built in, not http://."""
        node = LoreNode(LOCAL)
        headers = {**PARTIAL, 'X-Archive-Upstream': 'http://lore.kernel.org/all'}
        fetch(node, headers=headers)
        assert node._get_upstream_url() is None

    @pytest.mark.parametrize(
        'value',
        [
            'mirror.example.com',
            'ftp://mirror.example.com',
            'https://mirror.example.com/all',
            'https://user@mirror.example.com',
        ],
    )
    def test_invalid_allowed_upstream(self, value: str) -> None:
        with pytest.raises(LibloreError, match='Invalid allowed upstream'):
            LoreNode(LOCAL, allowed_upstreams=[value])


class TestForget:
    def learned(self) -> LoreNode:
        node = LoreNode(LOCAL)
        fetch(node, headers=PARTIAL)
        assert node._get_upstream_url() == UPSTREAM
        return node

    @pytest.mark.parametrize('status', [200, 302, 404])
    def test_forgotten_without_headers(self, status: int) -> None:
        """The server changed its mind (e.g. ARCHIVE_UPSTREAM='')."""
        node = self.learned()
        fetch(node, status=status)
        assert node._get_upstream_url() is None

    @pytest.mark.parametrize('status', [500, 502, 503, 401, 403])
    def test_kept_without_headers(self, status: int) -> None:
        """A proxy in front of the mirror may answer without the headers."""
        node = self.learned()
        fetch(node, status=status)
        assert node._get_upstream_url() == UPSTREAM

    def test_forgotten_when_upstream_becomes_untrusted(self) -> None:
        node = self.learned()
        fetch(node, headers=OTHER)
        assert node._get_upstream_url() is None


# =====================================================================
# L1: remembering the upstream across runs
# =====================================================================


class TestSavedUpstream:
    def test_saved(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        fetch(node, headers=PARTIAL)
        (path,) = upstream_files(tmp_path)
        assert json.loads(path.read_text()) == {'url': LOCAL, 'upstream': UPSTREAM}
        assert list(tmp_path.glob('*.tmp')) == []

    def test_loaded_without_any_request(self, tmp_path: Path) -> None:
        """This is what lets a stopped mirror fall back to the upstream."""
        fetch(LoreNode(LOCAL, cache_dir=str(tmp_path)), headers=PARTIAL)
        # conftest blocks the network: this node sends no request at all.
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        assert node._get_upstream_url() == UPSTREAM

    def test_keyed_by_url(self, tmp_path: Path) -> None:
        fetch(LoreNode(LOCAL, cache_dir=str(tmp_path)), headers=PARTIAL)
        other = LoreNode('http://127.0.0.1:11043/lore/tools', cache_dir=str(tmp_path))
        assert other._get_upstream_url() is None

    def test_survives_clear_cache(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        fetch(node, headers=PARTIAL)
        node.clear_cache()
        assert len(upstream_files(tmp_path)) == 1

    def test_deleted_when_forgotten(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        fetch(node, headers=PARTIAL)
        fetch(node, status=404)
        assert upstream_files(tmp_path) == []
        assert LoreNode(LOCAL, cache_dir=str(tmp_path))._get_upstream_url() is None

    def test_kept_on_5xx_without_headers(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        fetch(node, headers=PARTIAL)
        fetch(node, status=503)
        assert len(upstream_files(tmp_path)) == 1

    def test_written_only_on_change(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        with patch('liblore.node.os.replace', wraps=os.replace) as replace:
            for _ in range(3):
                fetch(node, headers=PARTIAL)
        assert replace.call_count == 1

    def test_file_from_earlier_run_not_rewritten(self, tmp_path: Path) -> None:
        """The saved file is loaded before the first answer is compared."""
        fetch(LoreNode(LOCAL, cache_dir=str(tmp_path)), headers=PARTIAL)
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        with patch('liblore.node.os.replace', wraps=os.replace) as replace:
            fetch(node, headers=PARTIAL)
        assert replace.call_count == 0

    def test_saved_upstream_checked_for_trust(self, tmp_path: Path) -> None:
        """Removing a host from allowupstream stops a saved file using it."""
        allowed = ['https://mirror.example.com']
        node = LoreNode(LOCAL, cache_dir=str(tmp_path), allowed_upstreams=allowed)
        fetch(node, headers=OTHER)
        assert len(upstream_files(tmp_path)) == 1
        assert LoreNode(LOCAL, cache_dir=str(tmp_path))._get_upstream_url() is None

    @pytest.mark.parametrize(
        'content',
        [
            b'not json',
            b'\xff\xfe',
            b'["a list"]',
            json.dumps({'url': LOCAL}).encode(),
            json.dumps({'url': LOCAL, 'upstream': 42}).encode(),
            json.dumps({'url': 'http://elsewhere/all', 'upstream': UPSTREAM}).encode(),
        ],
    )
    def test_bad_file_ignored(self, tmp_path: Path, content: bytes) -> None:
        fetch(LoreNode(LOCAL, cache_dir=str(tmp_path)), headers=PARTIAL)
        (path,) = upstream_files(tmp_path)
        path.write_bytes(content)
        assert LoreNode(LOCAL, cache_dir=str(tmp_path))._get_upstream_url() is None

    def test_no_cache_dir(self) -> None:
        """Without cache_dir the upstream is learned again in each process."""
        node = LoreNode(LOCAL)
        fetch(node, headers=PARTIAL)
        assert node._get_upstream_url() == UPSTREAM
        assert LoreNode(LOCAL)._get_upstream_url() is None


# =====================================================================
# L8: git config
# =====================================================================


class TestGitConfig:
    def from_config(self, cfg: dict[str, str | list[str]]) -> LoreNode:
        with (
            patch('liblore.node._get_subsection_config', return_value=cfg),
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            return LoreNode.from_git_config(LOCAL)

    def test_defaults(self) -> None:
        node = self.from_config({})
        assert node._follow_upstream is True
        assert node._trusted_upstreams == {'https://lore.kernel.org'}

    @pytest.mark.parametrize('value', ['false', 'no', 'off', '0'])
    def test_followupstream_off(self, value: str) -> None:
        assert self.from_config({'followupstream': value})._follow_upstream is False

    def test_invalid_followupstream_keeps_default(self) -> None:
        node = self.from_config({'followupstream': 'maybe'})
        assert node._follow_upstream is True

    def test_allowupstream(self) -> None:
        node = self.from_config(
            {'allowupstream': ['https://a.example.com', 'http://b.example.com:8080']}
        )
        assert node._trusted_upstreams == {
            'https://lore.kernel.org',
            'https://a.example.com',
            'http://b.example.com:8080',
        }

    def test_multivalued_key_requested(self) -> None:
        with (
            patch('liblore.node._get_subsection_config', return_value={}) as sub,
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            LoreNode.from_git_config(LOCAL)
        assert 'allowupstream' in sub.call_args.kwargs['multivals']

    @pytest.mark.parametrize(
        ('value', 'mode'),
        [('local', 'local'), ('upstream', 'upstream'), (' UPSTREAM ', 'upstream')],
    )
    def test_partialsearch(self, value: str, mode: str) -> None:
        assert self.from_config({'partialsearch': value})._partial_search == mode

    def test_partialsearch_default(self) -> None:
        assert self.from_config({})._partial_search == 'local'

    def test_invalid_partialsearch_keeps_default(self) -> None:
        node = self.from_config({'partialsearch': 'sideways'})
        assert node._partial_search == 'local'

    def test_invalid_partial_search_argument(self) -> None:
        with pytest.raises(LibloreError, match='partial_search'):
            LoreNode(LOCAL, partial_search='sideways')

    def test_explicit_kwargs_win(self) -> None:
        cfg: dict[str, str | list[str]] = {
            'followupstream': 'false',
            'allowupstream': ['https://a.example.com'],
        }
        with (
            patch('liblore.node._get_subsection_config', return_value=cfg),
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            node = LoreNode.from_git_config(
                LOCAL, follow_upstream=True, allowed_upstreams=[]
            )
        assert node._follow_upstream is True
        assert node._trusted_upstreams == {'https://lore.kernel.org'}


# =====================================================================
# L2 + L7: the upstream node and its lifecycle
# =====================================================================

# What the user's [lore] section says about lore.kernel.org.
LORE_CFG: dict[str, str | list[str]] = {
    'fallback': ['https://tor.example.com'],
    'useragentplus': 'secret',
}


class TestUpstreamNode:
    @pytest.fixture(autouse=True)
    def _git_config(self) -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value=LORE_CFG),
        ):
            yield

    def partial(
        self,
        tmp_path: Path,
        *,
        follow_upstream: bool = True,
        allowed_upstreams: list[str] | None = None,
    ) -> LoreNode:
        """Return a node that has learned it is a partial mirror."""
        node = LoreNode(
            LOCAL,
            cache_dir=str(tmp_path),
            cache_ttl=60,
            follow_upstream=follow_upstream,
            allowed_upstreams=allowed_upstreams,
        )
        fetch(node, headers=PARTIAL)
        return node

    def upstream(self, node: LoreNode) -> LoreNode:
        up = node._get_upstream_node()
        assert up is not None
        return up

    def test_none_for_full_node(self, tmp_path: Path) -> None:
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        fetch(node)
        assert node._get_upstream_node() is None

    def test_none_when_not_following(self, tmp_path: Path) -> None:
        assert (
            self.partial(tmp_path, follow_upstream=False)._get_upstream_node() is None
        )

    def test_built_lazily_and_reused(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        assert node._upstream_node is None
        up = self.upstream(node)
        assert up.url == UPSTREAM
        assert node._get_upstream_node() is up

    def test_one_hop_only(self, tmp_path: Path) -> None:
        up = self.upstream(self.partial(tmp_path))
        assert up._follow_upstream is False

    def test_shares_cache_settings(self, tmp_path: Path) -> None:
        up = self.upstream(self.partial(tmp_path))
        assert up._cache_dir == str(tmp_path)
        assert up._cache_ttl == 60

    def test_no_auth_headers(self, tmp_path: Path) -> None:
        # This node authenticates what it returns; doing it twice would
        # add a second Authentication-Results header.
        node = self.partial(tmp_path)
        with patch.object(
            LoreNode, 'from_git_config', wraps=LoreNode.from_git_config
        ) as build:
            up = self.upstream(node)
        assert build.call_args.kwargs['add_auth_headers'] is False
        assert up._authenticate_message is None

    def test_uses_git_config_for_upstream(self, tmp_path: Path) -> None:
        up = self.upstream(self.partial(tmp_path))
        assert 'https://tor.example.com' in up._all_origins

    def test_shares_injected_session(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        session = requests.Session()
        node.set_requests_session(session)
        up = self.upstream(node)
        assert up._session is session
        assert up._owns_session is False

    def test_session_injected_later_is_shared(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        up = self.upstream(node)
        session = requests.Session()
        node.set_requests_session(session)
        assert up._session is session

    def test_own_session_not_shared(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        node._get_session()
        up = self.upstream(node)
        assert up._session is None

    def test_user_agent_without_plus(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        node.set_user_agent('b4', '1.0', 'mine')
        up = self.upstream(node)
        # The upstream's own useragentplus from git config, never ours.
        assert up._user_agent == 'b4/1.0+secret'
        assert node._user_agent == 'b4/1.0+mine'

    def test_user_agent_set_later(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        up = self.upstream(node)
        node.set_user_agent('b4', '2.0', 'mine')
        assert up._user_agent == 'b4/2.0+secret'

    def test_new_url_keeps_old_node(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path, allowed_upstreams=['https://mirror.example.com'])
        old = self.upstream(node)
        fetch(node, headers=OTHER)
        new = self.upstream(node)
        assert new is not old
        assert new.url == 'https://mirror.example.com/all'
        assert node._upstream_nodes() == [old, new]

    def test_cancel_active_propagates(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path, allowed_upstreams=['https://mirror.example.com'])
        old = self.upstream(node)
        fetch(node, headers=OTHER)
        new = self.upstream(node)
        gens = (old._cancel_generation, new._cancel_generation)
        node.cancel_active()
        assert old._cancel_generation == gens[0] + 1
        assert new._cancel_generation == gens[1] + 1
        assert not new.is_shutdown

    def test_shutdown_propagates(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path, allowed_upstreams=['https://mirror.example.com'])
        old = self.upstream(node)
        fetch(node, headers=OTHER)
        new = self.upstream(node)
        node.shutdown()
        assert old.is_shutdown
        assert new.is_shutdown

    def test_no_upstream_after_shutdown(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        node.shutdown()
        assert node._get_upstream_node() is None
        assert node._upstream_node is None

    def test_shutdown_during_build(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        real = LoreNode.from_git_config

        def racing(url: str, **kwargs: Unpack[_LoreNodeInitKwargs]) -> LoreNode:
            built = real(url, **kwargs)
            node.shutdown()
            return built

        with patch.object(LoreNode, 'from_git_config', side_effect=racing):
            up = node._get_upstream_node()
        assert up is not None
        assert up.is_shutdown

    def test_close_closes_all(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path, allowed_upstreams=['https://mirror.example.com'])
        old = self.upstream(node)
        fetch(node, headers=OTHER)
        new = self.upstream(node)
        with (
            patch.object(old, 'close') as old_close,
            patch.object(new, 'close') as new_close,
        ):
            node.close()
        old_close.assert_called_once_with()
        new_close.assert_called_once_with()
        assert node._upstream_nodes() == []

    def test_context_manager_closes_upstream(self, tmp_path: Path) -> None:
        with self.partial(tmp_path) as node:
            up = self.upstream(node)
            up._get_session()
        assert up._session is None

    def test_rebuilt_after_close(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        old = self.upstream(node)
        node.close()
        assert self.upstream(node) is not old

    def test_built_once_across_threads(self, tmp_path: Path) -> None:
        node = self.partial(tmp_path)
        barrier = threading.Barrier(8)
        found: list[LoreNode | None] = []

        def worker() -> None:
            barrier.wait()
            found.append(node._get_upstream_node())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(found) == 8
        assert len({id(up) for up in found}) == 1
        assert node._upstream_nodes() == [found[0]]

    def test_build_does_not_block_cancel(self, tmp_path: Path) -> None:
        # The build runs git; a cancel (from a signal handler, say)
        # must not wait for it.
        node = self.partial(tmp_path)
        building = threading.Event()
        release = threading.Event()
        real_from_git_config = LoreNode.from_git_config

        def slow_from_git_config(
            url: str, **kwargs: Unpack[_LoreNodeInitKwargs]
        ) -> LoreNode:
            building.set()
            assert release.wait(5)
            return real_from_git_config(url, **kwargs)

        builder = threading.Thread(target=node._get_upstream_node)
        with patch.object(LoreNode, 'from_git_config', slow_from_git_config):
            builder.start()
            try:
                assert building.wait(5)
                cancelled = threading.Thread(target=node.cancel_active)
                cancelled.start()
                cancelled.join(2)
                assert not cancelled.is_alive(), 'cancel_active() waited for the build'
            finally:
                release.set()
                builder.join(5)
        assert node._upstream_node is not None

    def test_build_failure_remembered(self, tmp_path: Path) -> None:
        # Bad git config for the upstream: say so every time, but read
        # the config once.
        node = self.partial(tmp_path)
        boom = LibloreError('bad config')
        with patch.object(
            LoreNode, 'from_git_config', side_effect=boom
        ) as from_git_config:
            for _ in range(2):
                with pytest.raises(LibloreError, match='bad config'):
                    node._get_upstream_node()
            assert from_git_config.call_count == 1
        # A miss then fails the same way, and the user sees why.
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.GET,
                f'{LOCAL}/x%40example.com/t.mbox.gz',
                status=404,
                headers=PARTIAL,
            )
            with pytest.raises(LibloreError, match='bad config'):
                node.get_mbox_by_msgid('x@example.com')


# =====================================================================
# L3 + L9: a local miss goes upstream
# =====================================================================

MSGID = 'first@example.com'
Q = 'first%40example.com'
LOCAL_MBOX = f'{LOCAL}/{Q}/t.mbox.gz'
UP_MBOX = f'{UPSTREAM}/{Q}/t.mbox.gz'
LOCAL_RAW = f'{LOCAL}/{Q}/raw'
UP_RAW = f'{UPSTREAM}/{Q}/raw'
LOCAL_QUERY = f'{LOCAL}/?x=m&q=test'
UP_QUERY = f'{UPSTREAM}/?x=m&q=test'
SINCE = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
LOCAL_SINCE = f'{LOCAL}/{Q}/?x=m&q=rt%3A1705320000..'
UP_SINCE = f'{UPSTREAM}/{Q}/?x=m&q=rt%3A1705320000..'
# The HEAD redirect lookup of a full node: bare origin, no base path.
REDIRECT_HEAD = f'http://127.0.0.1:11043/{Q}/'


LOCAL_MSG = (
    b'From x Mon Jan  1 00:00:00 2024\n'
    b'From: Carol <carol@example.com>\n'
    b'Subject: Re: First message\n'
    b'Message-Id: <local@example.com>\n'
    b'In-Reply-To: <first@example.com>\n'
    b'\n'
    b'Hello.\n'
)


def gz(data: bytes) -> bytes:
    return gzip.compress(data)


def urls(rsps: responses.RequestsMock) -> list[tuple[str, str | None]]:
    """Every request sent, as ``(method, url)``, in order."""
    return [(c.request.method or '', c.request.url) for c in rsps.calls]


class PartialNode:
    """Shared setup: no git config, and a node on the local mirror."""

    @pytest.fixture(autouse=True)
    def _git_config(self) -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
        with (
            patch('liblore.node._get_subsection_config', return_value={}),
            patch('liblore.node._get_config_from_git', return_value={}),
        ):
            yield

    def node(
        self,
        tmp_path: Path,
        *,
        learned: bool = True,
        follow_upstream: bool = True,
        partial_search: str = 'local',
    ) -> LoreNode:
        """Return a node, by default one that knows it is partial."""
        node = LoreNode(
            LOCAL,
            cache_dir=str(tmp_path),
            follow_upstream=follow_upstream,
            partial_search=partial_search,
        )
        if learned:
            fetch(node, headers=PARTIAL)
        return node


class TestMissRules(PartialNode):
    # --- get_mbox_by_msgid / get_thread_by_msgid ---------------------

    def test_mbox_hit_is_local(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, body=gz(sample_mbox), headers=PARTIAL)
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox
            assert urls(rsps) == [('GET', LOCAL_MBOX)]

    def test_mbox_miss_goes_upstream(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox
            # No HEAD redirect lookup anywhere (L9).
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('GET', UP_MBOX)]

    def test_mbox_miss_learned_from_same_answer(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('GET', UP_MBOX)]

    def test_mbox_upstream_answer_cached_upstream_only(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            node.get_mbox_by_msgid(MSGID)
            # Local is asked again (the mirror may have it by now), and
            # the upstream node answers from its own cache.
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox
            assert urls(rsps) == [
                ('GET', LOCAL_MBOX),
                ('GET', UP_MBOX),
                ('GET', LOCAL_MBOX),
            ]

    def test_mbox_nocache_reaches_upstream(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            node.get_mbox_by_msgid(MSGID)
            node.get_mbox_by_msgid(MSGID, nocache=True)
            assert urls(rsps).count(('GET', UP_MBOX)) == 2

    def test_mbox_upstream_404_is_final(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, status=404)
            # The upstream is a full node: it does its own redirect
            # lookup, as it would for any other caller.
            rsps.add(responses.HEAD, f'https://lore.kernel.org/{Q}/', status=404)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_msgid(MSGID)
            assert exc.value.status_code == 404
            assert not isinstance(exc.value, NotOnMirrorError)
            assert len(rsps.calls) == 3

    def test_full_node_keeps_redirect_lookup(self, tmp_path: Path) -> None:
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404)
            rsps.add(responses.HEAD, REDIRECT_HEAD, status=404)
            with pytest.raises(RemoteError):
                node.get_mbox_by_msgid(MSGID)
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('HEAD', REDIRECT_HEAD)]

    def test_not_following_keeps_redirect_lookup(self, tmp_path: Path) -> None:
        node = self.node(tmp_path, learned=False, follow_upstream=False)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.HEAD, REDIRECT_HEAD, status=404)
            with pytest.raises(RemoteError):
                node.get_mbox_by_msgid(MSGID)
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('HEAD', REDIRECT_HEAD)]

    def test_thread_miss_goes_upstream(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            msgs = node.get_thread_by_msgid(MSGID)
            assert [m['Message-Id'] for m in msgs] == [
                '<first@example.com>',
                '<second@example.com>',
            ]

    def test_batch_per_item(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path)
        other = f'{LOCAL}/second%40example.com/t.mbox.gz'
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            rsps.add(responses.GET, other, body=gz(sample_mbox), headers=PARTIAL)
            with patch('liblore.node.time.sleep'):
                results = node.batch_get_thread_by_msgid(
                    [MSGID, 'second@example.com'], strict=False
                )
            assert len(results) == 2
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('GET', UP_MBOX), ('GET', other)]

    def test_no_fallback_after_cancel(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)

        def cancel_then_404(
            _req: object,
        ) -> tuple[int, dict[str, str], bytes]:
            node.cancel_active()
            return 404, PARTIAL, b''

        with responses.RequestsMock() as rsps:
            rsps.add_callback(responses.GET, LOCAL_MBOX, callback=cancel_then_404)
            with pytest.raises(OperationCancelledError):
                node.get_mbox_by_msgid(MSGID)
            assert urls(rsps) == [('GET', LOCAL_MBOX)]

    def test_upstream_uses_its_fallbacks(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path)
        tor = 'https://tor.lore.kernel.org/all/first%40example.com/t.mbox.gz'
        cfg: dict[str, str | list[str]] = {'fallback': ['https://tor.lore.kernel.org']}
        with (
            patch('liblore.node._get_config_from_git', return_value=cfg),
            responses.RequestsMock() as rsps,
        ):
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, tor, status=503)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('GET', tor), ('GET', UP_MBOX)]

    def test_one_hop_only(self, tmp_path: Path) -> None:
        # The upstream says it is partial too, and names a trusted
        # upstream of its own: that one is not asked.
        node = self.node(tmp_path)
        chained = {**PARTIAL, 'X-Archive-Upstream': 'https://lore.kernel.org/lkml'}
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_MBOX, status=404, headers=chained)
            rsps.add(responses.HEAD, f'https://lore.kernel.org/{Q}/', status=404)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_msgid(MSGID)
            assert exc.value.status_code == 404
            assert len(rsps.calls) == 3

    # --- get_message_by_msgid ----------------------------------------

    def test_raw_hit_is_local(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_RAW, body=b'local', headers=PARTIAL)
            assert node.get_message_by_msgid(MSGID) == b'local'
            assert len(rsps.calls) == 1

    def test_raw_miss_goes_upstream(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_RAW, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_RAW, body=b'upstream')
            assert node.get_message_by_msgid(MSGID) == b'upstream'
            assert urls(rsps) == [('GET', LOCAL_RAW), ('GET', UP_RAW)]

    def test_raw_upstream_404_is_final(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_RAW, status=404, headers=PARTIAL)
            rsps.add(responses.GET, UP_RAW, status=404)
            with pytest.raises(RemoteError) as exc:
                node.get_message_by_msgid(MSGID)
            assert exc.value.status_code == 404
            assert not isinstance(exc.value, NotOnMirrorError)

    def test_raw_other_4xx_not_a_miss(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_RAW, status=403, headers=PARTIAL)
            with pytest.raises(RemoteError) as exc:
                node.get_message_by_msgid(MSGID)
            assert exc.value.status_code == 403
            assert len(rsps.calls) == 1

    # --- get_mbox_by_query -------------------------------------------

    def test_search_goes_upstream_first(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path, partial_search='upstream')
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, UP_QUERY, body=gz(sample_mbox))
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', UP_QUERY)]

    def test_search_upstream_404_is_final(self, tmp_path: Path) -> None:
        # lore.kernel.org answers "no results" with a 404, not an empty 200.
        node = self.node(tmp_path, partial_search='upstream')
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, UP_QUERY, status=404)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_query('test')
            assert exc.value.status_code == 404
            assert not isinstance(exc.value, NotOnMirrorError)
            assert urls(rsps) == [('POST', UP_QUERY)]

    def test_search_learned_from_same_answer(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path, learned=False, partial_search='upstream')
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_QUERY, body=gz(b'partial'), headers=PARTIAL)
            rsps.add(responses.POST, UP_QUERY, body=gz(sample_mbox))
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', LOCAL_QUERY), ('POST', UP_QUERY)]
        # The mirror's partial answer was not cached.
        key = node._cache_key('mbox_by_query', 'test', 'False')
        assert node._cache_read(key) is None

    def test_search_default_is_local_first(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        # The mirror answers without the upstream archive, which is the
        # point of having a mirror: fast, and working while lore is down.
        node = LoreNode(LOCAL, cache_dir=str(tmp_path))
        fetch(node, headers=PARTIAL)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_QUERY, body=gz(sample_mbox), headers=PARTIAL)
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', LOCAL_QUERY)]

    def test_search_local_mode_hit(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path, partial_search='local')
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_QUERY, body=gz(sample_mbox), headers=PARTIAL)
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', LOCAL_QUERY)]

    @pytest.mark.parametrize(('status', 'body'), [(404, b''), (200, gz(b''))])
    def test_search_local_mode_miss(
        self, tmp_path: Path, sample_mbox: bytes, status: int, body: bytes
    ) -> None:
        node = self.node(tmp_path, partial_search='local')
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST, LOCAL_QUERY, status=status, body=body, headers=PARTIAL
            )
            rsps.add(responses.POST, UP_QUERY, body=gz(sample_mbox))
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', LOCAL_QUERY), ('POST', UP_QUERY)]

    def test_thread_by_query_upstream(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, UP_QUERY, body=gz(sample_mbox))
            msgs = node.get_thread_by_query('test')
            assert len(msgs) == 2

    # --- _fetch_thread_since: the known-thread check -----------------

    def test_known_thread_searched_locally(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, headers=PARTIAL)
            rsps.add(responses.POST, LOCAL_SINCE, body=gz(sample_mbox), headers=PARTIAL)
            msgs = node.get_thread_updates_since(MSGID, SINCE)
            assert len(msgs) == 2
            # The check comes before the search.
            assert urls(rsps) == [('HEAD', LOCAL_RAW), ('POST', LOCAL_SINCE)]

    def test_known_thread_via_redirect(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.HEAD,
                LOCAL_RAW,
                status=302,
                headers={**PARTIAL, 'Location': f'{LOCAL}/elsewhere/raw'},
            )
            rsps.add(responses.POST, LOCAL_SINCE, body=gz(sample_mbox), headers=PARTIAL)
            assert len(node.get_thread_updates_since(MSGID, SINCE)) == 2
            # The redirect was not followed.
            assert urls(rsps) == [('HEAD', LOCAL_RAW), ('POST', LOCAL_SINCE)]

    @pytest.mark.parametrize(('status', 'body'), [(404, b''), (200, gz(b''))])
    def test_known_thread_nothing_new_is_final(
        self, tmp_path: Path, status: int, body: bytes
    ) -> None:
        # public-inbox answers 404 "No results found" for a known thread
        # with no hits.
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, headers=PARTIAL)
            rsps.add(
                responses.POST, LOCAL_SINCE, status=status, body=body, headers=PARTIAL
            )
            assert node.get_thread_updates_since(MSGID, SINCE) == []
            assert urls(rsps) == [('HEAD', LOCAL_RAW), ('POST', LOCAL_SINCE)]

    def test_unknown_thread_goes_upstream(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        # For an unknown message-id public-inbox drops the thread limit
        # and searches the whole archive, so the mirror is never searched.
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, status=404, headers=PARTIAL)
            rsps.add(responses.POST, UP_SINCE, body=gz(sample_mbox))
            msgs = node.get_thread_updates_since(MSGID, SINCE)
            assert len(msgs) == 2
            assert urls(rsps) == [('HEAD', LOCAL_RAW), ('POST', UP_SINCE)]

    def test_unknown_thread_nothing_new_upstream(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, status=404, headers=PARTIAL)
            rsps.add(responses.POST, UP_SINCE, status=404)
            assert node.get_thread_updates_since(MSGID, SINCE) == []

    def test_thread_by_msgid_since(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path)
        up = f'{UPSTREAM}/{Q}/?x=m&q=dt%3A20240101..'
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, status=404, headers=PARTIAL)
            rsps.add(responses.POST, up, body=gz(sample_mbox))
            msgs = node.get_thread_by_msgid(MSGID, since='20240101')
            assert len(msgs) == 2

    @pytest.mark.parametrize('head_status', [200, 404])
    def test_check_after_learning_from_search(
        self, tmp_path: Path, sample_mbox: bytes, head_status: int
    ) -> None:
        # A node that learns it is partial from the search answer itself
        # can't trust that answer without the check.
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_SINCE, body=gz(LOCAL_MSG), headers=PARTIAL)
            rsps.add(responses.HEAD, LOCAL_RAW, status=head_status, headers=PARTIAL)
            if head_status == 404:
                rsps.add(responses.POST, UP_SINCE, body=gz(sample_mbox))
            msgs = node._fetch_thread_since(MSGID, 'rt:1705320000..')
            sent = urls(rsps)
        if head_status == 404:
            # Unknown here: the mirror's answer was a whole-archive search.
            assert len(msgs) == 2
            assert sent[-1] == ('POST', UP_SINCE)
        else:
            assert [m['Message-Id'] for m in msgs] == ['<local@example.com>']
            assert sent == [('POST', LOCAL_SINCE), ('HEAD', LOCAL_RAW)]

    def test_full_node_sends_no_check(self, tmp_path: Path, sample_mbox: bytes) -> None:
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_SINCE, body=gz(sample_mbox))
            assert len(node.get_thread_updates_since(MSGID, SINCE)) == 2
            assert urls(rsps) == [('POST', LOCAL_SINCE)]

    # --- local only --------------------------------------------------

    def test_validate_is_local_only(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, f'{LOCAL}/_/text/help/', status=404)
            with pytest.raises(RemoteError):
                node.validate()
            assert len(rsps.calls) == 1


# =====================================================================
# L4: unreachable nodes
# =====================================================================

LOCAL_HOST = '127.0.0.1:11043'


def add_down(
    rsps: responses.RequestsMock, method: str, url: str, status: int | None
) -> None:
    """Make *url* unreachable: a connection error, or a *status* 5xx."""
    if status is None:
        rsps.add(method, url, body=requests.ConnectionError('Connection refused'))
    else:
        rsps.add(method, url, status=status)


def warnings_about(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and text in r.getMessage()
    ]


# A connection error (no status at all) and a 5xx both mean "unreachable".
DOWN = pytest.mark.parametrize('status', [None, 503])


class TestUnreachable(PartialNode):
    # --- a local miss, and the upstream is unreachable ---------------

    @DOWN
    def test_mbox_miss_upstream_down(self, tmp_path: Path, status: int | None) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            add_down(rsps, responses.GET, UP_MBOX, status)
            with pytest.raises(NotOnMirrorError) as exc:
                node.get_mbox_by_msgid(MSGID)
        # Callers that catch RemoteError keep working.
        assert isinstance(exc.value, RemoteError)
        assert exc.value.status_code == status
        msg = str(exc.value)
        assert MSGID in msg
        assert LOCAL_HOST in msg
        assert 'lore.kernel.org' in msg

    @DOWN
    def test_raw_miss_upstream_down(self, tmp_path: Path, status: int | None) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_RAW, status=404, headers=PARTIAL)
            add_down(rsps, responses.GET, UP_RAW, status)
            with pytest.raises(NotOnMirrorError) as exc:
                node.get_message_by_msgid(MSGID)
        assert exc.value.status_code == status

    def test_thread_miss_upstream_down(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.GET, LOCAL_MBOX, status=404, headers=PARTIAL)
            add_down(rsps, responses.GET, UP_MBOX, None)
            with pytest.raises(NotOnMirrorError):
                node.get_thread_by_msgid(MSGID)

    # --- the local mirror is unreachable -----------------------------

    @DOWN
    def test_mbox_local_down_uses_upstream(
        self, tmp_path: Path, sample_mbox: bytes, status: int | None
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.GET, LOCAL_MBOX, status)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox
            assert urls(rsps) == [('GET', LOCAL_MBOX), ('GET', UP_MBOX)]

    @DOWN
    def test_raw_local_down_uses_upstream(
        self, tmp_path: Path, status: int | None
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.GET, LOCAL_RAW, status)
            rsps.add(responses.GET, UP_RAW, body=LOCAL_MSG)
            assert node.get_message_by_msgid(MSGID) == LOCAL_MSG
            assert urls(rsps) == [('GET', LOCAL_RAW), ('GET', UP_RAW)]

    def test_local_down_warns_once(
        self, tmp_path: Path, sample_mbox: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        node = self.node(tmp_path)
        with (
            caplog.at_level(logging.WARNING, logger='liblore.node'),
            responses.RequestsMock() as rsps,
        ):
            add_down(rsps, responses.GET, LOCAL_MBOX, None)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            node.get_mbox_by_msgid(MSGID)
            node.get_mbox_by_msgid(MSGID)
            # Local is tried every time: it may be back.
            assert urls(rsps).count(('GET', LOCAL_MBOX)) == 2
        warned = warnings_about(caplog, 'unreachable')
        assert len(warned) == 1
        assert LOCAL_HOST in warned[0]
        assert 'lore.kernel.org' in warned[0]

    def test_local_down_saved_upstream(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        # An earlier run learned the upstream; a stopped mirror sends no
        # headers to learn from.
        self.node(tmp_path)
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.GET, LOCAL_MBOX, None)
            rsps.add(responses.GET, UP_MBOX, body=gz(sample_mbox))
            assert node.get_mbox_by_msgid(MSGID) == sample_mbox

    @DOWN
    def test_local_down_no_upstream(self, tmp_path: Path, status: int | None) -> None:
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.GET, LOCAL_MBOX, status)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_msgid(MSGID)
            assert urls(rsps) == [('GET', LOCAL_MBOX)]
        assert not isinstance(exc.value, NotOnMirrorError)
        assert exc.value.status_code == status

    def test_both_down(self, tmp_path: Path) -> None:
        # Nothing says the thread is missing locally, so this is no
        # NotOnMirrorError: the upstream's error is raised as it is.
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.GET, LOCAL_MBOX, None)
            add_down(rsps, responses.GET, UP_MBOX, 503)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_msgid(MSGID)
        assert not isinstance(exc.value, NotOnMirrorError)
        assert exc.value.status_code == 503

    # --- whole-archive search ----------------------------------------

    @DOWN
    def test_search_upstream_down_is_degraded(
        self,
        tmp_path: Path,
        sample_mbox: bytes,
        caplog: pytest.LogCaptureFixture,
        status: int | None,
    ) -> None:
        node = self.node(tmp_path, partial_search='upstream')
        with (
            caplog.at_level(logging.WARNING, logger='liblore.node'),
            responses.RequestsMock() as rsps,
        ):
            add_down(rsps, responses.POST, UP_QUERY, status)
            rsps.add(responses.POST, LOCAL_QUERY, body=gz(sample_mbox), headers=PARTIAL)
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', UP_QUERY), ('POST', LOCAL_QUERY)]
        # The answer may be incomplete: never cached.
        key = node._cache_key('mbox_by_query', 'test', 'False')
        assert node._cache_read(key) is None
        assert len(warnings_about(caplog, 'incomplete')) == 1

    def test_search_learned_then_upstream_down(
        self, tmp_path: Path, sample_mbox: bytes
    ) -> None:
        # The mirror's own answer says it is partial, and then the
        # upstream can't be reached: the answer is degraded, not cached.
        node = self.node(tmp_path, learned=False, partial_search='upstream')
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_QUERY, body=gz(sample_mbox), headers=PARTIAL)
            add_down(rsps, responses.POST, UP_QUERY, None)
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', LOCAL_QUERY), ('POST', UP_QUERY)]
        key = node._cache_key('mbox_by_query', 'test', 'False')
        assert node._cache_read(key) is None

    @pytest.mark.parametrize(('status', 'body'), [(404, b''), (200, gz(b''))])
    def test_search_upstream_down_nothing_local(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        status: int,
        body: bytes,
    ) -> None:
        # A partial mirror finding nothing says nothing about the archive.
        node = self.node(tmp_path)
        with (
            caplog.at_level(logging.WARNING, logger='liblore.node'),
            responses.RequestsMock() as rsps,
        ):
            add_down(rsps, responses.POST, UP_QUERY, None)
            rsps.add(
                responses.POST, LOCAL_QUERY, status=status, body=body, headers=PARTIAL
            )
            with pytest.raises(NotOnMirrorError) as exc:
                node.get_mbox_by_query('test')
        assert "No results for 'test'" in str(exc.value)
        # Nothing was returned, so nothing is "incomplete".
        assert not warnings_about(caplog, 'incomplete')

    def test_search_both_down(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.POST, UP_QUERY, None)
            add_down(rsps, responses.POST, LOCAL_QUERY, None)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_query('test')
        assert not isinstance(exc.value, NotOnMirrorError)

    def test_search_cancel_is_not_degraded(self, tmp_path: Path) -> None:
        node = self.node(tmp_path, partial_search='upstream')

        def cancel_then_fail(_req: object) -> tuple[int, dict[str, str], bytes]:
            node.cancel_active()
            raise requests.ConnectionError('Connection aborted')

        with responses.RequestsMock() as rsps:
            rsps.add_callback(responses.POST, UP_QUERY, callback=cancel_then_fail)
            with pytest.raises(OperationCancelledError):
                node.get_mbox_by_query('test')
            # No local search after the cancel.
            assert urls(rsps) == [('POST', UP_QUERY)]

    @pytest.mark.parametrize(('status', 'body'), [(404, b''), (200, gz(b''))])
    def test_search_local_mode_miss_upstream_down(
        self, tmp_path: Path, status: int, body: bytes
    ) -> None:
        node = self.node(tmp_path, partial_search='local')
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST, LOCAL_QUERY, status=status, body=body, headers=PARTIAL
            )
            add_down(rsps, responses.POST, UP_QUERY, None)
            with pytest.raises(NotOnMirrorError):
                node.get_mbox_by_query('test')
            assert urls(rsps) == [('POST', LOCAL_QUERY), ('POST', UP_QUERY)]

    @DOWN
    def test_search_local_mode_local_down_uses_upstream(
        self, tmp_path: Path, sample_mbox: bytes, status: int | None
    ) -> None:
        node = self.node(tmp_path, partial_search='local')
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.POST, LOCAL_QUERY, status)
            rsps.add(responses.POST, UP_QUERY, body=gz(sample_mbox))
            assert node.get_mbox_by_query('test') == sample_mbox
            assert urls(rsps) == [('POST', LOCAL_QUERY), ('POST', UP_QUERY)]

    def test_search_local_mode_other_4xx_is_raised(self, tmp_path: Path) -> None:
        # Only "no results" is a miss; a bad query is a bad query anywhere.
        node = self.node(tmp_path, partial_search='local')
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_QUERY, status=400, headers=PARTIAL)
            with pytest.raises(RemoteError) as exc:
                node.get_mbox_by_query('test')
            assert urls(rsps) == [('POST', LOCAL_QUERY)]
        assert exc.value.status_code == 400

    # --- thread updates ----------------------------------------------

    @DOWN
    def test_since_check_down_uses_upstream(
        self, tmp_path: Path, sample_mbox: bytes, status: int | None
    ) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.HEAD, LOCAL_RAW, status)
            rsps.add(responses.POST, UP_SINCE, body=gz(sample_mbox))
            assert len(node.get_thread_updates_since(MSGID, SINCE)) == 2
            assert urls(rsps) == [('HEAD', LOCAL_RAW), ('POST', UP_SINCE)]

    @DOWN
    def test_since_search_down_uses_upstream(
        self, tmp_path: Path, sample_mbox: bytes, status: int | None
    ) -> None:
        # The mirror knows the thread, then fails on the search itself.
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, headers=PARTIAL)
            add_down(rsps, responses.POST, LOCAL_SINCE, status)
            rsps.add(responses.POST, UP_SINCE, body=gz(sample_mbox))
            assert len(node.get_thread_updates_since(MSGID, SINCE)) == 2
            assert urls(rsps) == [
                ('HEAD', LOCAL_RAW),
                ('POST', LOCAL_SINCE),
                ('POST', UP_SINCE),
            ]

    @DOWN
    def test_since_unknown_thread_upstream_down(
        self, tmp_path: Path, status: int | None
    ) -> None:
        # "Nothing new" would be a guess.
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.HEAD, LOCAL_RAW, status=404, headers=PARTIAL)
            add_down(rsps, responses.POST, UP_SINCE, status)
            with pytest.raises(NotOnMirrorError) as exc:
                node.get_thread_updates_since(MSGID, SINCE)
        assert MSGID in str(exc.value)

    def test_since_both_down(self, tmp_path: Path) -> None:
        node = self.node(tmp_path)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.HEAD, LOCAL_RAW, None)
            add_down(rsps, responses.POST, UP_SINCE, None)
            with pytest.raises(RemoteError) as exc:
                node.get_thread_updates_since(MSGID, SINCE)
        assert not isinstance(exc.value, NotOnMirrorError)

    def test_full_node_since_5xx_is_nothing_new(self, tmp_path: Path) -> None:
        # Unchanged: a full node has always read a 5xx as "nothing new".
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, LOCAL_SINCE, status=503)
            assert node.get_thread_updates_since(MSGID, SINCE) == []

    def test_full_node_since_connection_error_raises(self, tmp_path: Path) -> None:
        node = self.node(tmp_path, learned=False)
        with responses.RequestsMock() as rsps:
            add_down(rsps, responses.POST, LOCAL_SINCE, None)
            with pytest.raises(RemoteError) as exc:
                node.get_thread_updates_since(MSGID, SINCE)
        assert not isinstance(exc.value, NotOnMirrorError)
