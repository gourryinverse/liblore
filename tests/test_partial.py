# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for partial mirror support (X-Archive-Coverage / X-Archive-Upstream)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import responses

from liblore import LibloreError
from liblore.node import LoreNode, _normalize_url, _parse_coverage

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
    """Each test starts with no "untrusted upstream" warning given yet."""
    with patch('liblore.node._warned_untrusted', set[str]()):
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
