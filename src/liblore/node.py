# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2025-2026 The Linux Foundation
"""LoreNode — primary API for interacting with public-inbox servers."""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
import gzip
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import warnings
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

import requests

from liblore import (
    LibloreError,
    NotOnMirrorError,
    OperationCancelledError,
    RemoteError,
)
from liblore.utils import (
    get_strict_thread,
    sort_msgs_by_received,
    split_and_dedupe,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from typing import TypeVar

    from typing_extensions import ParamSpec, Self, Unpack

    _P = ParamSpec('_P')
    _R = TypeVar('_R')
    _T = TypeVar('_T')


class _LoreNodeInitKwargs(TypedDict, total=False):
    fallback_urls: list[str] | None
    auto_probe: bool
    probe_timeout: float
    probe_ttl: int
    request_timeout: tuple[float, float] | float | None
    add_auth_headers: bool
    cache_dir: str | None
    cache_ttl: int
    follow_upstream: bool
    partial_search: str
    upstream_holdoff: float
    allowed_upstreams: list[str] | None


class _AuthenticateMessage(Protocol):
    def __call__(
        self,
        msg: bytes,
        authserv_id: str,
        *,
        dkim: bool = ...,
        dmarc: bool = ...,
        arc: bool = ...,
        spf: bool = ...,
    ) -> str: ...


def _get_config_from_git(
    regexp: str,
    multivals: list[str] | None = None,
) -> dict[str, str | list[str]]:
    """Read git config keys matching *regexp* in one shot.

    Uses ``git config -z --get-regexp`` to fetch all matching keys
    with NUL-separated output (safe for values containing newlines).

    Single-valued keys are stored as strings.  Keys listed in
    *multivals* are collected into lists.

    Returns an empty dict on any failure (git not installed, not in a
    repo, no matching keys, etc.).
    """
    if multivals is None:
        multivals = []
    try:
        result = subprocess.run(
            ['git', 'config', '-z', '--get-regexp', regexp],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return {}
    except Exception:  # noqa: BLE001
        return {}

    config: dict[str, str | list[str]] = {}
    for entry in result.stdout.split('\x00'):
        if not entry:
            continue
        if '\n' in entry:
            key, value = entry.split('\n', 1)
        else:
            key, value = entry, 'true'
        # Extract the last component: "lore.fallback" → "fallback"
        cfgkey = key.rsplit('.', maxsplit=1)[-1].lower()
        if cfgkey in multivals:
            existing = config.get(cfgkey)
            if not isinstance(existing, list):
                existing = []
                config[cfgkey] = existing
            existing.append(value)
        else:
            config[cfgkey] = value

    return config


def _get_subsection_config(
    section: str,
    subsection: str,
    multivals: list[str] | None = None,
) -> dict[str, str | list[str]]:
    """Read git config keys from ``[section "subsection"]``.

    Like :func:`_get_config_from_git`, but handles subsections whose
    names contain dots (e.g. URLs).  The variable name is extracted by
    stripping the known ``section.subsection.`` prefix rather than
    splitting on the last dot.

    Returns an empty dict on any failure.
    """
    if multivals is None:
        multivals = []
    prefix = f'{section}.{subsection}.'
    escaped = re.escape(prefix)
    try:
        result = subprocess.run(
            ['git', 'config', '-z', '--get-regexp', f'^{escaped}'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return {}
    except Exception:  # noqa: BLE001
        return {}

    config: dict[str, str | list[str]] = {}
    for entry in result.stdout.split('\x00'):
        if not entry:
            continue
        if '\n' in entry:
            key, value = entry.split('\n', 1)
        else:
            key, value = entry, 'true'
        varname = key[len(prefix) :].lower()
        if varname in multivals:
            existing = config.get(varname)
            if not isinstance(existing, list):
                existing = []
                config[varname] = existing
            existing.append(value)
        else:
            config[varname] = value

    return config


def _git_bool(value: str) -> bool | None:
    """Read a git config boolean the way git does.

    ``true``/``yes``/``on`` and ``false``/``no``/``off`` in any case, an
    empty value (false), or an integer (true unless 0).  A bare key with
    no ``=`` is already turned into ``'true'`` by the config readers.

    Returns ``None`` for anything else, so the caller keeps its default.
    """
    val = value.strip().lower()
    if val in ('true', 'yes', 'on'):
        return True
    if val in ('false', 'no', 'off', ''):
        return False
    try:
        return int(val) != 0
    except ValueError:
        return None


# Upstreams followed without being listed in allowupstream (section 2.4 of
# the partial mirrors spec).
_BUILTIN_UPSTREAMS = frozenset({'https://lore.kernel.org'})
# Where a partial node sends whole-archive searches first.
_PARTIAL_SEARCH_MODES = ('local', 'upstream')

# Hosts already warned about, so each untrusted upstream is logged once
# per process, not once per response.
_warned_untrusted: set[str] = set()
# Outages already warned about, as (kind, node URL).
_warned_outage: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _warn_outage_once(kind: str, url: str, msg: str, *args: object) -> None:
    """Log warning *msg* the first time this process sees *kind* for *url*."""
    with _warned_lock:
        first = (kind, url) not in _warned_outage
        _warned_outage.add((kind, url))
    if first:
        logger.warning(msg, *args)


def _unreachable(ex: RemoteError) -> bool:
    """Whether *ex* means the server gave no usable answer.

    That is no answer at all (connection error, timeout), or a 5xx: a
    mirror answering 503 while it restarts is as good as a stopped one.
    """
    return ex.status_code is None or ex.status_code >= 500


class _HeldOffError(RemoteError):
    """The upstream was not asked: it was unreachable *ago* seconds ago."""

    def __init__(self, netloc: str, ago: float) -> None:
        super().__init__(
            f'{netloc} was unreachable {ago:.0f} s ago, not trying it again yet'
        )
        self.netloc = netloc
        self.ago = ago


class _Miss(Exception):
    """This partial mirror doesn't have what was asked (see :meth:`LoreNode._answer`).

    *upstream* is the node to ask instead.  *answer* is what the mirror
    did answer, when there was an answer worth keeping should the
    upstream turn out to be unreachable: a whole-archive search finds
    some of the hits, or none.  *missing* starts the
    :class:`~liblore.NotOnMirrorError` message, when the default
    ``'<what> is not'`` doesn't read well.
    """

    def __init__(
        self,
        upstream: LoreNode,
        answer: object = None,
        *,
        missing: str | None = None,
    ) -> None:
        super().__init__(f'not on the mirror, asking {upstream.url}')
        self.upstream = upstream
        self.answer = answer
        self.missing = missing


def _normalize_url(url: str) -> str:
    """Normalize *url* for comparison.

    Lower-cases the scheme and host, drops the default port (``:80`` for
    http, ``:443`` for https) and any trailing ``/``.  The path keeps
    its case.  Raises :class:`ValueError` for a URL without a host, with
    an invalid port, or with user info (which would otherwise be lost).
    """
    parsed = urllib.parse.urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if not scheme or not host:
        raise ValueError(f'not an absolute URL: {url!r}')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f'URL has user info: {url!r}')
    if ':' in host:  # IPv6 literal: urlsplit() dropped the brackets
        host = f'[{host}]'
    port = parsed.port
    if port is not None and (scheme, port) not in (('http', 80), ('https', 443)):
        host = f'{host}:{port}'
    return urllib.parse.urlunsplit(
        (scheme, host, parsed.path.rstrip('/'), parsed.query, parsed.fragment)
    )


def _url_origin(url: str) -> str:
    """Return ``scheme://host[:port]`` of an already normalized *url*."""
    parsed = urllib.parse.urlsplit(url)
    return f'{parsed.scheme}://{parsed.netloc}'


def _parse_coverage(value: str) -> tuple[str, dict[str, str]]:
    """Split an ``X-Archive-Coverage`` value into its token and parameters.

    ``'partial; updated=1790000000'`` gives
    ``('partial', {'updated': '1790000000'})``.  Names are lower-cased;
    a parameter without ``=`` is skipped.
    """
    token, *parts = value.split(';')
    params: dict[str, str] = {}
    for part in parts:
        name, sep, val = part.partition('=')
        if sep:
            params[name.strip().lower()] = val.strip()
    return token.strip().lower(), params


def _header(resp: requests.Response, name: str) -> str | None:
    """Return response header *name*, or ``None`` when it is not a string."""
    value: object = resp.headers.get(name)
    return value if isinstance(value, str) else None


def _operation_scope(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run the decorated LoreNode method as one logical operation.

    Everything the method does — however many HTTP requests it takes —
    shares a single cancellation scope, so a :meth:`LoreNode.cancel_active`
    that lands anywhere in the middle (including between requests) stops
    the whole thing.  See :meth:`LoreNode._operation`.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        node = cast('LoreNode', args[0])
        with node._operation():  # pyright: ignore[reportPrivateUsage]
            return func(*args, **kwargs)

    return wrapper


# A temporary file _write_atomic() left behind when its process was
# killed.  Versions before 0.10 named it <file>.tmp.
_ORPHAN_TMP_RE = re.compile(r'.+\.lore\.(?:cache|upstream)(?:\.[0-9a-f]{16})?\.tmp')
# A temporary file younger than this may still be in use by a running
# process, so clear_cache() leaves it alone.
_ORPHAN_TMP_AGE = 3600


def _write_atomic(path: str, data: bytes) -> None:
    """Write *data* to *path* so that readers see the old or the new file.

    The data goes to a temporary file next to *path*, which then
    replaces it.  The temporary name is unique, so processes that write
    the same file at the same time don't write into each other's file.
    Unlike :func:`tempfile.mkstemp`, the file gets the same mode as
    :func:`open` would give it (``0o666`` minus the umask).

    Raises :class:`OSError`; the temporary file is removed first.
    """
    tmp_path = f'{path}.{secrets.token_hex(8)}.tmp'
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _orphan_tmp(dirname: str, entry: str, now: float) -> bool:
    """Whether *entry* in *dirname* is a stale temporary file of ours."""
    if not _ORPHAN_TMP_RE.fullmatch(entry):
        return False
    try:
        mtime = os.stat(os.path.join(dirname, entry)).st_mtime
    except OSError:
        return False
    return now - mtime > _ORPHAN_TMP_AGE


class LoreNode:
    """A connection to a single public-inbox endpoint.

    Encapsulates HTTP session management and all operations against a
    public-inbox server.  Use as a context manager for automatic
    resource cleanup::

        with LoreNode('https://lore.kernel.org/all') as node:
            msgs = node.get_thread_by_msgid('test@example.com')
    """

    def __init__(
        self,
        url: str = 'https://lore.kernel.org/all',
        *,
        fallback_urls: list[str] | None = None,
        auto_probe: bool = False,
        probe_timeout: float = 5.0,
        probe_ttl: int = 3600,
        request_timeout: tuple[float, float] | float | None = (5.0, 30.0),
        add_auth_headers: bool = False,
        cache_dir: str | None = None,
        cache_ttl: int = 600,
        follow_upstream: bool = True,
        partial_search: str = 'local',
        upstream_holdoff: float = 300.0,
        allowed_upstreams: list[str] | None = None,
    ) -> None:
        self._url = url.rstrip('/')
        self._session: requests.Session | None = None
        self._owns_session = False
        self._request_timeout = request_timeout
        self._cancel_lock = threading.Lock()
        self._cancel_generation = 0
        self._shutdown_event = threading.Event()
        self._op_local = threading.local()
        self._user_agent_plus: str | None = None
        self._user_agent: str = f'liblore/{__import__("liblore").__version__}'
        self._cache_dir = cache_dir
        self._cache_ttl = cache_ttl
        self._auto_probe = auto_probe
        self._probe_timeout = probe_timeout
        self._probe_ttl = probe_ttl
        self._probe_done = False

        # Parse the canonical URL into origin (scheme+host) and path suffix
        parsed = urllib.parse.urlparse(self._url)
        self._canonical_origin = f'{parsed.scheme}://{parsed.netloc}'

        # Build the ordered list of origins to try
        self._all_origins: list[str] = []
        for fb in fallback_urls or []:
            fb = fb.rstrip('/')
            fb_parsed = urllib.parse.urlparse(fb)
            if not fb_parsed.scheme or not fb_parsed.netloc:
                raise LibloreError(
                    f'Invalid fallback URL (expected scheme://host): {fb!r}'
                )
            if fb_parsed.path and fb_parsed.path != '/':
                raise LibloreError(
                    f'Fallback URL must be a scheme://host origin, '
                    f'not a full path: {fb!r}. '
                    f'The path from the primary URL is preserved '
                    f'automatically.'
                )
            self._all_origins.append(fb)
        self._all_origins.append(self._canonical_origin)

        # Partial mirrors: the upstream archive learned from the
        # X-Archive-* response headers (see _learn_upstream).
        self._follow_upstream = follow_upstream
        if partial_search not in _PARTIAL_SEARCH_MODES:
            raise LibloreError(
                f'Invalid partial_search (expected local or upstream): '
                f'{partial_search!r}'
            )
        self._partial_search = partial_search
        # Also rejects NaN, which would hold the upstream off forever.
        if not upstream_holdoff >= 0:
            raise LibloreError(
                f'Invalid upstream_holdoff (expected 0 or more seconds): '
                f'{upstream_holdoff!r}'
            )
        self._upstream_holdoff = upstream_holdoff
        trusted = set(_BUILTIN_UPSTREAMS)
        for au in allowed_upstreams or []:
            try:
                norm = _normalize_url(au)
            except ValueError:
                norm = ''
            au_parsed = urllib.parse.urlsplit(norm)
            if (
                au_parsed.scheme not in ('http', 'https')
                or au_parsed.path
                or au_parsed.query
                or au_parsed.fragment
            ):
                raise LibloreError(
                    f'Invalid allowed upstream (expected scheme://host): {au!r}'
                )
            trusted.add(norm)
        self._trusted_upstreams = frozenset(trusted)
        try:
            self._norm_url = _normalize_url(self._url)
        except ValueError:
            self._norm_url = self._url
        # Re-entrant: _get_upstream_node() holds it while it calls
        # _get_upstream_url(), and cancel_active() may run from a signal
        # handler on a thread that holds it.
        self._upstream_lock = threading.RLock()
        self._upstream_url: str | None = None
        self._upstream_loaded = False
        # The node for _upstream_url, built on first use.  Nodes for an
        # earlier upstream URL are kept until close(): another thread
        # may still be using them.
        self._upstream_node: LoreNode | None = None
        self._old_upstream_nodes: list[LoreNode] = []
        # Held while an upstream node is built, so that two threads
        # don't build two.  Not _upstream_lock: the build runs git, and
        # cancel_active() must not wait for that.
        self._upstream_build_lock = threading.Lock()
        # The upstream URL whose node could not be built, and why.
        # Guarded by _upstream_lock.
        self._upstream_build_failed: tuple[str, LibloreError] | None = None
        # The upstream URL and time.monotonic() of its last unreachable
        # answer, while it is held off.  Guarded by _upstream_lock.
        self._holdoff_since: tuple[str, float] | None = None
        # set_user_agent() app and version, passed on to upstream nodes.
        self._user_agent_app: tuple[str, str] | None = None

        if cache_dir is not None:
            os.makedirs(cache_dir, exist_ok=True)
        self._authenticate_message: _AuthenticateMessage | None = None
        if add_auth_headers:
            try:
                import authheaders

                self._authenticate_message = authheaders.authenticate_message
            except ImportError:
                raise LibloreError(
                    'authheaders library is required for add_auth_headers. '
                    'Install with: pip install liblore[auth]'
                )

    # -----------------------------------------------------------------
    # Git config integration
    # -----------------------------------------------------------------

    @classmethod
    def from_git_config(
        cls,
        url: str = 'https://lore.kernel.org/all',
        **kwargs: Unpack[_LoreNodeInitKwargs],
    ) -> LoreNode:
        """Create a :class:`LoreNode` using settings from git config.

        Looks for per-origin configuration in a ``[liblore "<origin>"]``
        subsection first, then falls back to ``[lore]`` for
        lore.kernel.org URLs.  This allows different public-inbox
        servers to have independent mirror and probe settings.

        Supported keys (in each section):

        ``fallback``
            Multi-valued.  Each value is an origin URL prefix
            (``scheme://host``) to try before the canonical URL.
            Tried in the order listed.

        ``autoprobe``
            Boolean.  When ``true``, automatically probe all origins
            on the first request and reorder by latency.

        ``probetimeout``
            Float (seconds).  Per-origin timeout for probes.
            Defaults to 5.0.

        ``probettl``
            Integer (seconds).  How long cached probe results stay
            valid.  Defaults to 3600.

        ``requesttimeout``
            Float (seconds), or ``connect,read`` for separate connect
            and read timeouts.  Applied to all outgoing HTTP requests.
            Defaults to ``5.0,30.0``.

        ``useragentplus``
            String.  A unique identifier appended to the User-Agent
            header as ``app/version+IDENTIFIER``.  Used by server
            operators to identify and prioritize known installations.
            Typically a UUID.  Applied automatically when
            :meth:`set_user_agent` is called without an explicit
            *plus* argument.

        ``followupstream``
            Boolean.  When ``true`` (the default), a server that says it
            is a partial mirror (``X-Archive-Coverage: partial``) is
            remembered together with the upstream archive it names in
            ``X-Archive-Upstream``.  ``false`` ignores those headers.
            Something the partial mirror doesn't have is then fetched
            from the upstream archive.

        ``partialsearch``
            ``local`` (the default) or ``upstream``.  Where a partial
            mirror sends a whole-archive search first.  By default the
            mirror answers, and the upstream archive is asked only when
            the mirror finds nothing, so searches stay fast and keep
            working while the upstream archive is down.  A partial
            mirror can't know what it is missing, so ``upstream`` asks
            the upstream archive first, for users who want every hit.

        ``upstreamholdoff``
            Seconds (default 300).  After the upstream archive of a
            partial mirror can't be reached, it is not asked again for
            this long, so a command doesn't wait for a timeout on every
            call.  ``0`` asks it every time.

        ``allowupstream``
            Multi-valued.  Each value is an origin (``scheme://host``)
            to trust as an upstream archive, besides the built-in
            ``https://lore.kernel.org``.  An upstream that is not
            trusted is ignored, with one warning per process.

        Lookup order:

        1. ``[liblore "<origin>"]`` — per-origin subsection
        2. ``[lore]`` — only for ``lore.kernel.org`` URLs (backwards
           compatibility)

        Any keyword argument passed explicitly takes precedence over
        the git config value.  Failures reading git config (git not
        installed, not in a repo, keys missing) are silently ignored.

        Example git config::

            # Per-origin configuration (recommended)
            [liblore "https://lore.kernel.org"]
                fallback = https://tor.lore.kernel.org
                fallback = https://sea.lore.kernel.org
                autoprobe = true
                useragentplus = 550e8400-e29b-41d4-a716-446655440000

            [liblore "https://subspace.kernel.org"]
                fallback = https://subspace-mirror.kernel.org

            # Legacy shorthand for [liblore "https://lore.kernel.org"]
            [lore]
                fallback = https://tor.lore.kernel.org
                autoprobe = true

        Example usage::

            with LoreNode.from_git_config() as node:
                msgs = node.get_thread_by_msgid('test@example.com')
        """
        parsed = urllib.parse.urlparse(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'

        # Try [liblore "<origin>"] first, fall back to [lore] for
        # lore.kernel.org.
        gitcfg = _get_subsection_config(
            'liblore',
            origin,
            multivals=['fallback', 'allowupstream'],
        )
        if not gitcfg and parsed.netloc == 'lore.kernel.org':
            gitcfg = _get_config_from_git(
                r'^lore\.', multivals=['fallback', 'allowupstream']
            )

        if 'fallback_urls' not in kwargs:
            fallbacks = gitcfg.get('fallback')
            if isinstance(fallbacks, list) and fallbacks:
                kwargs['fallback_urls'] = fallbacks

        if 'auto_probe' not in kwargs:
            val = gitcfg.get('autoprobe')
            if isinstance(val, str):
                flag = _git_bool(val)
                if flag is None:
                    logger.debug('Ignoring invalid autoprobe value: %r', val)
                else:
                    kwargs['auto_probe'] = flag

        if 'probe_timeout' not in kwargs:
            val = gitcfg.get('probetimeout')
            if isinstance(val, str):
                try:
                    kwargs['probe_timeout'] = float(val)
                except ValueError:
                    pass

        if 'probe_ttl' not in kwargs:
            val = gitcfg.get('probettl')
            if isinstance(val, str):
                try:
                    kwargs['probe_ttl'] = int(val)
                except ValueError:
                    pass

        if 'request_timeout' not in kwargs:
            val = gitcfg.get('requesttimeout')
            if isinstance(val, str):
                try:
                    if ',' in val:
                        connect, read = val.split(',', 1)
                        kwargs['request_timeout'] = (
                            float(connect),
                            float(read),
                        )
                    else:
                        kwargs['request_timeout'] = float(val)
                except ValueError:
                    pass

        if 'follow_upstream' not in kwargs:
            val = gitcfg.get('followupstream')
            if isinstance(val, str):
                flag = _git_bool(val)
                if flag is None:
                    logger.debug('Ignoring invalid followupstream value: %r', val)
                else:
                    kwargs['follow_upstream'] = flag

        if 'partial_search' not in kwargs:
            val = gitcfg.get('partialsearch')
            if isinstance(val, str):
                mode = val.strip().lower()
                if mode in _PARTIAL_SEARCH_MODES:
                    kwargs['partial_search'] = mode
                else:
                    logger.debug('Ignoring invalid partialsearch value: %r', val)

        if 'upstream_holdoff' not in kwargs:
            val = gitcfg.get('upstreamholdoff')
            if isinstance(val, str):
                try:
                    holdoff = float(val)
                except ValueError:
                    holdoff = -1.0
                if holdoff >= 0:
                    kwargs['upstream_holdoff'] = holdoff
                else:
                    logger.debug('Ignoring invalid upstreamholdoff value: %r', val)

        if 'allowed_upstreams' not in kwargs:
            allowed = gitcfg.get('allowupstream')
            if isinstance(allowed, list) and allowed:
                kwargs['allowed_upstreams'] = allowed

        node = cls(url, **kwargs)

        val = gitcfg.get('useragentplus')
        if isinstance(val, str) and val:
            node._user_agent_plus = val

        return node

    # -----------------------------------------------------------------
    # Session management
    # -----------------------------------------------------------------

    @property
    def user_agent_plus(self) -> str | None:
        """The ``lore.useragentplus`` value from git config, or None.

        Only populated when the node was created via
        :meth:`from_git_config`.  Downstream projects can use this to
        include the same tracking identifier in their own user-agent
        strings (e.g. for git HTTP or CLI tools).
        """
        return self._user_agent_plus

    def set_user_agent(
        self, app_name: str, version: str, plus: str | None = None
    ) -> None:
        """Set the User-Agent to ``app_name/version`` (optionally ``+plus``).

        When *plus* is not provided, the value from ``lore.useragentplus``
        in git config is used (if it was loaded via :meth:`from_git_config`).
        """
        self._user_agent = f'{app_name}/{version}'
        effective_plus = plus or self._user_agent_plus
        if effective_plus:
            self._user_agent += f'+{effective_plus}'
        logger.debug('Set user-agent to: %s', self._user_agent)
        if self._session is not None and self._owns_session:
            self._session.headers.update({'User-Agent': self._user_agent})
        # The upstream gets no plus: it identifies this installation to
        # this server's operator, and must not go to another host just
        # because a mirror named it.
        with self._upstream_lock:
            self._user_agent_app = (app_name, version)
        for node in self._upstream_nodes():
            node.set_user_agent(app_name, version)

    def set_requests_session(self, session: requests.Session) -> None:
        """Inject a pre-existing :class:`requests.Session`.

        The session's User-Agent header is **not** overwritten — the
        caller owns it.
        """
        self._session = session
        self._owns_session = False
        logger.debug('Using caller-provided requests session')
        for node in self._upstream_nodes():
            node.set_requests_session(session)

    def _get_session(self) -> requests.Session:
        """Return the session, creating one if needed."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({'User-Agent': self._user_agent})
            self._owns_session = True
        return self._session

    def close(self) -> None:
        """Close the session if we created it (not externally injected).

        Also closes the upstream nodes of a partial mirror.
        """
        if self._session is not None and self._owns_session:
            self._session.close()
        self._session = None
        self._owns_session = False
        with self._upstream_lock:
            nodes = self._upstream_nodes()
            self._upstream_node = None
            self._old_upstream_nodes = []
        for node in nodes:
            node.close()

    def _abort_owned_session(self) -> None:
        """Close and drop the owned session so blocked socket reads raise.

        An injected session (``set_requests_session``) is intentionally
        *not* closed — the caller owns its lifecycle.
        """
        if self._session is not None and self._owns_session:
            try:
                self._session.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug('Ignoring error closing owned session: %s', exc)
            self._session = None
            self._owns_session = False

    @contextlib.contextmanager
    def _operation(self) -> Generator[None, None, None]:
        """Delimit one logical operation for cancellation purposes.

        On outermost entry, refuses to start when the node is shut down
        and snapshots the current cancellation generation for this
        thread.  :meth:`cancel_active` bumps the generation, so an
        operation that was in flight when it landed sees the mismatch
        at its next request and stays cancelled for the rest of its
        run, while operations started afterwards proceed normally.
        Re-entrant per thread: nested scopes join the outermost
        operation.
        """
        depth = getattr(self._op_local, 'depth', 0)
        if depth == 0:
            if self._shutdown_event.is_set():
                raise OperationCancelledError('Node is shut down')
            self._op_local.generation = self._cancel_generation
        self._op_local.depth = depth + 1
        try:
            yield
        finally:
            self._op_local.depth = depth
            if depth == 0:
                self._op_local.generation = None

    def _cancelled(self) -> bool:
        """Whether the current thread's operation has been cancelled."""
        if self._shutdown_event.is_set():
            return True
        generation: int | None = getattr(self._op_local, 'generation', None)
        if generation is None:
            return False
        return generation != self._cancel_generation

    def cancel_active(self) -> None:
        """Cancel all operations currently in flight.

        Closes the owned session so a thread blocked in a socket read
        raises immediately, and marks every operation currently in
        progress as cancelled: each raises
        :class:`~liblore.OperationCancelledError` at its next request
        and stays cancelled for the rest of its run.  Operations
        started after this call are unaffected — there is no sticky
        flag to reset.  Thread-safe; safe to call from a thread other
        than the ones issuing requests.
        """
        with self._cancel_lock:
            self._cancel_generation += 1
        self._abort_owned_session()
        for node in self._upstream_nodes():
            node.cancel_active()

    def shutdown(self) -> None:
        """Cancel active operations and refuse all new ones.

        The terminal state for application exit: everything in flight
        is cancelled as in :meth:`cancel_active`, and any operation
        started afterwards raises
        :class:`~liblore.OperationCancelledError` immediately, so
        racing workers fail fast instead of opening new connections.
        Cannot be undone.  Thread-safe.
        """
        self._shutdown_event.set()
        self._abort_owned_session()
        for node in self._upstream_nodes():
            node.shutdown()

    @property
    def is_shutdown(self) -> bool:
        """Whether :meth:`shutdown` has been called."""
        return self._shutdown_event.is_set()

    def cancel(self) -> None:
        """Deprecated alias for :meth:`cancel_active`.

        .. deprecated:: 0.9
           Cancellation is operation-scoped and no longer sticky; use
           :meth:`cancel_active`, or :meth:`shutdown` on application
           exit.
        """
        warnings.warn(
            'cancel() is deprecated; use cancel_active() or shutdown()',
            DeprecationWarning,
            stacklevel=2,
        )
        self.cancel_active()

    def reset_cancel(self) -> None:
        """Deprecated no-op.

        .. deprecated:: 0.9
           Cancellation is operation-scoped: :meth:`cancel_active`
           only affects operations already in flight, so there is no
           sticky flag to reset before starting a new one.
        """
        warnings.warn(
            'reset_cancel() is deprecated and does nothing; '
            'cancellation is operation-scoped and needs no reset',
            DeprecationWarning,
            stacklevel=2,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def url(self) -> str:
        """The base URL of this public-inbox endpoint."""
        return self._url

    @property
    def hostname(self) -> str:
        """The hostname extracted from the URL, for logging and display."""
        return urllib.parse.urlparse(self._url).hostname or self._url

    @property
    def _netloc(self) -> str:
        """``host[:port]`` of the URL, for messages."""
        return urllib.parse.urlsplit(self._url).netloc or self._url

    @property
    def origins(self) -> list[str]:
        """All configured origins in current probe order.

        The list includes fallback origins followed by the canonical
        origin.  After :meth:`probe_origins` runs, the list is reordered
        fastest-first.  Returns a copy to prevent external mutation.
        """
        return list(self._all_origins)

    @property
    def canonical_origin(self) -> str:
        """The scheme://host origin extracted from the primary URL.

        This is the origin that ``request()`` URLs must use — the
        failover mechanism rewrites this portion when trying fallback
        origins.
        """
        return self._canonical_origin

    # -----------------------------------------------------------------
    # URL fallback
    # -----------------------------------------------------------------

    def _rewrite_url(self, url: str, origin: str) -> str:
        """Replace the canonical origin in *url* with *origin*."""
        return origin + url[len(self._canonical_origin) :]

    def _probe_cache_key(self) -> str:
        """Cache key for probe results, stable regardless of current order."""
        origins = '\0'.join(sorted(self._all_origins))
        return hashlib.sha256(
            f'probe\0{origins}'.encode(),
        ).hexdigest()

    def _probe_cache_read(self) -> list[str] | None:
        """Return cached origin order if fresh, or None."""
        if self._cache_dir is None:
            return None
        key = self._probe_cache_key()
        path = os.path.join(self._cache_dir, f'{key}.lore.cache')
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        age = int(time.time() - st.st_mtime)
        if age > self._probe_ttl:
            logger.debug('Probe cache expired (%ds > %ds)', age, self._probe_ttl)
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        with open(path, 'rb') as f:
            try:
                origins: list[str] = json.loads(f.read())
            except (json.JSONDecodeError, ValueError):
                return None
        # Validate: must contain exactly the same origins we know about
        if set(origins) != set(self._all_origins):
            return None
        logger.debug('Probe cache hit (%ds old)', age)
        return origins

    def _probe_cache_write(self, origins: list[str]) -> None:
        """Write probe results to cache."""
        if self._cache_dir is None:
            return
        key = self._probe_cache_key()
        path = os.path.join(self._cache_dir, f'{key}.lore.cache')
        try:
            _write_atomic(path, json.dumps(origins).encode())
        except OSError:
            logger.debug('Failed to write probe cache: %s', path)

    # -----------------------------------------------------------------
    # Partial mirrors
    # -----------------------------------------------------------------

    def _upstream_path(self) -> str | None:
        """Path of the file that remembers the learned upstream, or None.

        The suffix is not ``.lore.cache``, so :meth:`clear_cache` keeps
        it: the saved upstream is what lets a stopped mirror fall back.
        """
        if self._cache_dir is None:
            return None
        key = hashlib.sha256(f'upstream\0{self._url}'.encode()).hexdigest()
        return os.path.join(self._cache_dir, f'{key}.lore.upstream')

    def _usable_upstream(self, value: str) -> str | None:
        """Return upstream URL *value*, normalized, if this node may follow it.

        It must be an absolute http or https URL, must not be this node
        itself, and its origin must be trusted: built in, or listed in
        *allowed_upstreams*.  An untrusted origin is warned about once
        per process.
        """
        try:
            norm = _normalize_url(value)
        except ValueError:
            norm = ''
        parsed = urllib.parse.urlsplit(norm)
        if parsed.scheme not in ('http', 'https') or parsed.query or parsed.fragment:
            logger.debug('Ignoring invalid upstream URL: %r', value)
            return None
        if norm == self._norm_url:
            return None
        origin = _url_origin(norm)
        if origin not in self._trusted_upstreams:
            with _warned_lock:
                first = origin not in _warned_untrusted
                _warned_untrusted.add(origin)
            if first:
                logger.warning(
                    'Not following untrusted upstream %s named by %s '
                    '(add it to allowupstream to trust it)',
                    origin,
                    self._canonical_origin,
                )
            return None
        return norm

    def _load_upstream(self) -> None:
        """Read the saved upstream, once.  The caller holds _upstream_lock."""
        if self._upstream_loaded:
            return
        self._upstream_loaded = True
        path = self._upstream_path()
        if path is None:
            return
        try:
            with open(path, 'rb') as f:
                data: object = json.loads(f.read())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.debug('Ignoring unreadable upstream file: %s', path)
            return
        if not isinstance(data, dict):
            return
        saved = cast('dict[str, object]', data)
        upstream = saved.get('upstream')
        if saved.get('url') != self._url or not isinstance(upstream, str):
            return
        # Check it again: allowupstream may have changed since it was saved.
        self._upstream_url = self._usable_upstream(upstream)

    def _save_upstream(self) -> None:
        """Write or delete the saved upstream.  The caller holds _upstream_lock."""
        path = self._upstream_path()
        if path is None:
            return
        if self._upstream_url is None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                logger.debug('Failed to remove upstream file: %s', path)
            return
        data = {'url': self._url, 'upstream': self._upstream_url}
        try:
            _write_atomic(path, json.dumps(data).encode())
        except OSError:
            logger.debug('Failed to write upstream file: %s', path)

    def _learn_upstream(self, resp: requests.Response) -> None:
        """Learn from *resp* whether this node is a partial mirror.

        A partial mirror sends ``X-Archive-Coverage: partial`` and names
        the archive it mirrors in ``X-Archive-Upstream``.  A usable
        upstream is remembered.  Any other answer forgets it, but only
        for a 2xx, 3xx or 404: a 5xx without the headers may come from a
        proxy in front of the mirror, not from the mirror.
        """
        if not self._follow_upstream:
            return
        upstream = None
        coverage = _header(resp, 'X-Archive-Coverage')
        if coverage is not None and _parse_coverage(coverage)[0] == 'partial':
            value = _header(resp, 'X-Archive-Upstream')
            if value is not None:
                upstream = self._usable_upstream(value)
        status = resp.status_code
        if upstream is None and not (200 <= status < 400 or status == 404):
            return
        with self._upstream_lock:
            self._load_upstream()
            if upstream == self._upstream_url:
                return
            if upstream is not None:
                logger.debug('%s is a partial mirror of %s', self._url, upstream)
            else:
                logger.debug('%s is no longer a partial mirror', self._url)
            self._upstream_url = upstream
            self._save_upstream()

    def _get_upstream_url(self) -> str | None:
        """Return the learned upstream archive URL, or None for a full node."""
        if not self._follow_upstream:
            return None
        with self._upstream_lock:
            self._load_upstream()
            return self._upstream_url

    def _upstream_nodes(self) -> list[LoreNode]:
        """Return every upstream node built so far, current one last."""
        with self._upstream_lock:
            nodes = list(self._old_upstream_nodes)
            if self._upstream_node is not None:
                nodes.append(self._upstream_node)
            return nodes

    def _get_upstream_node(self) -> LoreNode | None:
        """Return the node for the learned upstream, building it on first use.

        Returns ``None`` for a full node, and once this node is shut
        down: there is no upstream fallback after :meth:`shutdown`.

        The upstream node reads the user's git config for its own
        origin, so its fallbacks, probing, timeouts and
        ``useragentplus`` apply.  It never follows an upstream itself
        (one hop only), and adds no authentication headers: this node
        does that, once, for the messages it returns.

        Raises :class:`~liblore.LibloreError` when that git config
        can't be used.  The error is remembered, so the config is not
        read again for the same upstream.
        """
        if self._shutdown_event.is_set():
            return None
        with self._upstream_lock:
            url = self._get_upstream_url()
            if url is None:
                return None
            node = self._upstream_node
            if node is not None and node.url == url:
                return node
        with self._upstream_build_lock:
            with self._upstream_lock:
                # Another thread may have built it while this one waited.
                node = self._upstream_node
                if node is not None and node.url == url:
                    return node
                failed = self._upstream_build_failed
            if failed is not None and failed[0] == url:
                raise failed[1].with_traceback(None)
            try:
                node = LoreNode.from_git_config(
                    url,
                    follow_upstream=False,
                    cache_dir=self._cache_dir,
                    cache_ttl=self._cache_ttl,
                    add_auth_headers=False,
                )
            except LibloreError as ex:
                with self._upstream_lock:
                    self._upstream_build_failed = (url, ex)
                raise
            if self._session is not None and not self._owns_session:
                node.set_requests_session(self._session)
            with self._upstream_lock:
                if self._user_agent_app is not None:
                    node.set_user_agent(*self._user_agent_app)
                if self._shutdown_event.is_set():
                    # shutdown() ran while the node was being built.
                    node.shutdown()
                if self._upstream_node is not None:
                    self._old_upstream_nodes.append(self._upstream_node)
                self._upstream_node = node
            logger.debug('Built upstream node for %s', url)
            return node

    def _fallback_node(self) -> LoreNode | None:
        """Return the upstream node to retry a local miss on, or ``None``.

        Raises :class:`OperationCancelledError` when this node's
        operation was cancelled: never fall back after a cancel.
        """
        upstream = self._get_upstream_node()
        if upstream is not None and self._cancelled():
            raise OperationCancelledError('Request cancelled')
        return upstream

    def _outage_node(self, ex: RemoteError) -> LoreNode | None:
        """Return the upstream node to use because this node failed with *ex*.

        Returns ``None`` when *ex* is a real answer (see
        :func:`_unreachable`) or no upstream is known.  The upstream
        may come from the saved file of an earlier run: an unreachable
        node sends no headers to learn from.
        """
        if not _unreachable(ex):
            return None
        upstream = self._fallback_node()
        if upstream is not None:
            _warn_outage_once(
                'local',
                self._url,
                'Local mirror %s unreachable (%s), using %s',
                self._netloc,
                ex,
                upstream._netloc,
            )
        return upstream

    def _warn_degraded(self, upstream: LoreNode, ex: RemoteError) -> None:
        _warn_outage_once(
            'degraded',
            upstream.url,
            '%s unreachable (%s), searching the local mirror %s only; '
            'results may be incomplete',
            upstream._netloc,
            ex,
            self._netloc,
        )

    def _not_on_mirror(
        self, upstream: LoreNode, missing: str, ex: RemoteError
    ) -> NotOnMirrorError:
        """Build the error for *missing* locally with *upstream* failing with *ex*.

        *missing* reads as the start of a sentence, for example
        ``'<msgid> is not'``.
        """
        if isinstance(ex, _HeldOffError):
            why = f'{ex.netloc} was unreachable {ex.ago:.0f} s ago'
        else:
            why = f'{upstream._netloc} could not be reached: {ex}'
        return NotOnMirrorError(
            f'{missing} on the local mirror ({self._netloc}), and {why}',
            status_code=ex.status_code,
        )

    def _ask_upstream(self, upstream: LoreNode, remote: Callable[[LoreNode], _T]) -> _T:
        """Return ``remote(upstream)``, unless *upstream* is held off.

        After *upstream* is unreachable, it is not asked again for
        ``upstream_holdoff`` seconds: every request would wait for the
        timeout.  Meanwhile this raises an unreachable
        :class:`RemoteError` at once.  A real answer, an error included,
        ends the hold-off; a cancel changes nothing.
        """
        ago = self._held_off_for(upstream)
        if ago is not None:
            logger.debug('Not asking %s, unreachable %.0f s ago', upstream.url, ago)
            raise _HeldOffError(upstream._netloc, ago)
        try:
            result = remote(upstream)
        except RemoteError as ex:
            if _unreachable(ex):
                self._start_holdoff(upstream, ex)
            else:
                self._end_holdoff(upstream)
            raise
        self._end_holdoff(upstream)
        return result

    def _held_off_for(self, upstream: LoreNode) -> float | None:
        """Seconds since *upstream* was unreachable, while it is held off."""
        with self._upstream_lock:
            since = self._holdoff_since
        if since is None or since[0] != upstream.url:
            return None
        ago = time.monotonic() - since[1]
        if ago >= self._upstream_holdoff:
            return None
        return ago

    def _start_holdoff(self, upstream: LoreNode, ex: RemoteError) -> None:
        if self._upstream_holdoff <= 0:
            return
        with self._upstream_lock:
            # Another thread's request may have started it already.
            if self._held_off_for(upstream) is not None:
                return
            self._holdoff_since = (upstream.url, time.monotonic())
        logger.warning(
            '%s unreachable (%s), not trying it again for %.0f s',
            upstream._netloc,
            ex,
            self._upstream_holdoff,
        )

    def _end_holdoff(self, upstream: LoreNode) -> None:
        with self._upstream_lock:
            since = self._holdoff_since
            if since is None or since[0] != upstream.url:
                return
            self._holdoff_since = None
        logger.debug('%s answered, hold-off ended', upstream.url)

    def _answer(
        self,
        what: str,
        local: Callable[[], _T],
        remote: Callable[[LoreNode], _T],
        *,
        upstream_first: bool = False,
        on_outage: Callable[[RemoteError], _T] | None = None,
    ) -> _T:
        """Answer from this node, or from the upstream archive.

        This is the one place the rules of section 4.5 of the partial
        mirrors spec live.  *what* names the thing asked for, for log
        and error messages.

        *local* asks this node.  It returns its answer; or it raises
        :class:`_Miss` when this partial mirror doesn't have *what*; or
        :class:`~liblore.RemoteError` when the node failed.  *remote*
        asks the upstream node the same thing.

        A miss goes to the upstream.  When the upstream is unreachable
        too, the answer the mirror did give is returned if there is
        one, and :class:`~liblore.NotOnMirrorError` is raised if not.

        When this node is unreachable and an upstream is known, the
        upstream answers.  With no upstream, *on_outage* answers for
        the node when given, and the error is raised when not.

        With *upstream_first*, the upstream is asked before this node,
        and this node only when the upstream is unreachable.
        """
        if upstream_first:
            upstream = self._fallback_node()
            if upstream is not None:
                logger.debug('Asking %s first for %s', upstream.url, what)
                try:
                    return self._ask_upstream(upstream, remote)
                except RemoteError as ex:
                    if not _unreachable(ex):
                        raise
                    return self._degraded(upstream, what, local, ex)
        try:
            return local()
        except _Miss as miss:
            return self._after_miss(miss, what, remote)
        except RemoteError as ex:
            upstream = self._outage_node(ex)
            if upstream is None:
                if on_outage is not None and _unreachable(ex):
                    return on_outage(ex)
                raise
            try:
                return self._ask_upstream(upstream, remote)
            except _HeldOffError as held:
                # Say which failure the caller is looking at: the
                # hold-off alone reads as if the mirror had answered.
                raise RemoteError(
                    f'{what}: the local mirror ({self._netloc}) is unreachable '
                    f'({ex}), and {held}'
                ) from held

    def _after_miss(
        self, miss: _Miss, what: str, remote: Callable[[LoreNode], _T]
    ) -> _T:
        """Ask the upstream for *what*, which this partial mirror doesn't have.

        An unreachable upstream leaves the mirror's own answer, if
        *miss* carries one, as the degraded answer; otherwise it raises
        :class:`~liblore.NotOnMirrorError`.  The upstream's real
        answers, a 404 included, are raised as they are.
        """
        upstream = miss.upstream
        logger.debug('%s not on %s, asking %s', what, self._url, upstream.url)
        try:
            return self._ask_upstream(upstream, remote)
        except RemoteError as ex:
            if not _unreachable(ex):
                raise
            if not miss.answer:
                missing = miss.missing or f'{what} is not'
                raise self._not_on_mirror(upstream, missing, ex) from ex
            self._warn_degraded(upstream, ex)
            return cast('_T', miss.answer)

    def _degraded(
        self,
        upstream: LoreNode,
        what: str,
        local: Callable[[], _T],
        upstream_error: RemoteError,
    ) -> _T:
        """Ask this partial mirror for *what* because *upstream* is unreachable.

        The answer may be incomplete, so the caller must not cache it.
        When the mirror has nothing, that says nothing about the
        archive: raise :class:`~liblore.NotOnMirrorError` instead.
        """
        missing = None
        try:
            answer = local()
        except _Miss as miss:
            answer, missing = cast('_T', miss.answer), miss.missing
        if not answer:
            missing = missing or f'{what} is not'
            raise self._not_on_mirror(
                upstream, missing, upstream_error
            ) from upstream_error
        self._warn_degraded(upstream, upstream_error)
        return answer

    def _known_locally(self, msgid: str) -> bool:
        """Ask this node whether it has the message *msgid*.

        A thread-scoped search for a message-id the server doesn't
        know searches the whole archive instead (public-inbox drops
        the thread limit), so a partial node checks before searching.
        Raises :class:`RemoteError` when this node is unreachable.
        """
        raw_url = f'{self._url}/{urllib.parse.quote_plus(msgid)}/raw'
        resp = self._request('HEAD', raw_url, allow_redirects=False)
        resp.close()
        if resp.status_code >= 500:
            raise RemoteError(
                f'Server returned an error: {resp.status_code}',
                status_code=resp.status_code,
            )
        return resp.status_code != 404

    def _probe_one(self, origin: str) -> tuple[str, float]:
        """HEAD manifest.js.gz on *origin*, return (origin, elapsed).

        Uses a throwaway session to be thread-safe.  Returns
        ``float('inf')`` for unreachable or erroring origins.
        """
        url = f'{origin}/manifest.js.gz'
        try:
            start = time.monotonic()
            resp = requests.head(
                url,
                headers={'User-Agent': self._user_agent},
                timeout=self._probe_timeout,
            )
            elapsed = time.monotonic() - start
            if resp.status_code >= 400:
                logger.debug('Probe %s returned %d', origin, resp.status_code)
                return origin, float('inf')
            logger.debug('Probe %s: %.3fs', origin, elapsed)
            return origin, elapsed
        except Exception as exc:  # noqa: BLE001
            logger.debug('Probe %s failed: %s', origin, exc)
            return origin, float('inf')

    @_operation_scope
    def probe_origins(self, nocache: bool = False) -> list[tuple[str, float]]:
        """Probe all origins concurrently and reorder by response time.

        Sends a ``HEAD`` request to ``/manifest.js.gz`` on each origin
        (a lightweight resource present on all public-inbox instances)
        and sorts :attr:`_all_origins` fastest-first.  Unreachable
        origins are moved to the end rather than removed, so they can
        recover on subsequent requests.

        Results are cached to *cache_dir* (when set) for *probe_ttl*
        seconds so repeated calls are cheap.  Pass *nocache=True* to
        skip the cache and always perform a live probe (results are
        still written to cache afterward).

        Returns a list of ``(origin, elapsed_seconds)`` pairs in the
        new order.  Unreachable origins have ``float('inf')`` as their
        elapsed time.
        """
        if len(self._all_origins) <= 1:
            self._probe_done = True
            return [(self._all_origins[0], 0.0)] if self._all_origins else []

        # Try cached results first
        if not nocache:
            cached = self._probe_cache_read()
            if cached is not None:
                self._all_origins = cached
                self._probe_done = True
                return [(o, 0.0) for o in cached]

        # Probe all origins concurrently
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self._all_origins),
        ) as pool:
            results = list(pool.map(self._probe_one, self._all_origins))

        results.sort(key=lambda x: x[1])
        self._all_origins = [origin for origin, _ in results]
        self._probe_done = True

        # Log the results
        for origin, elapsed in results:
            if elapsed == float('inf'):
                logger.info('Probe result: %s — unreachable', origin)
            else:
                logger.info('Probe result: %s — %.3fs', origin, elapsed)

        self._probe_cache_write(self._all_origins)
        return results

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> requests.Response:
        """Execute an HTTP request with origin failover.

        The *url* must use the canonical origin (the URL passed to
        ``__init__``).  On retriable failures — connection errors,
        timeouts, and 5xx responses — each configured fallback origin is
        tried in order.  4xx responses are returned immediately (not
        retriable).

        Raises :class:`RemoteError` when every origin has been exhausted.

        Any extra *kwargs* are forwarded to the underlying
        ``requests.Session`` method (e.g. ``timeout``, ``headers``).
        """
        return self._request(method, url, raise_on_error=True, **kwargs)

    @_operation_scope
    def _request(
        self,
        method: str,
        url: str,
        *,
        raise_on_error: bool = True,
        **kwargs: object,
    ) -> requests.Response:
        """Execute an HTTP request with fallback URL rotation.

        Tries each origin in :attr:`_all_origins`.  On retriable
        failures (connection errors, timeouts, 5xx responses), logs a
        warning and tries the next origin.

        When *raise_on_error* is ``False``, returns the last response
        even when every origin failed (used by
        :meth:`_fetch_thread_since` which returns ``[]`` on error).
        """
        if self._auto_probe and not self._probe_done:
            self.probe_origins()

        # Always apply a timeout so a stalled socket cannot hang forever,
        # but let an explicit per-call timeout override the default.
        kwargs.setdefault('timeout', self._request_timeout)

        session = self._get_session()
        last_exc: Exception | None = None
        last_resp: requests.Response | None = None

        for origin in self._all_origins:
            if self._cancelled():
                raise OperationCancelledError('Request cancelled')
            request_url = self._rewrite_url(url, origin)
            logger.debug('Trying %s %s', method, request_url)
            try:
                resp: requests.Response = getattr(
                    session,
                    method.lower(),
                )(request_url, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                # cancel_active() and shutdown() close the session
                # mid-flight, which surfaces here as a ConnectionError.
                # Distinguish that from a real origin failure: if the
                # caller cancelled, do NOT fail over.
                if self._cancelled():
                    raise OperationCancelledError('Request cancelled') from exc
                logger.warning(
                    'Request to %s failed (%s), trying next host',
                    origin,
                    exc,
                )
                last_exc = exc
                continue

            self._learn_upstream(resp)
            if resp.status_code >= 500:
                logger.warning(
                    'Request to %s returned %d, trying next host',
                    origin,
                    resp.status_code,
                )
                last_resp = resp
                continue

            # Success or 4xx — not retriable
            return resp

        # All origins exhausted
        if not raise_on_error and last_resp is not None:
            return last_resp

        if last_exc is not None:
            raise RemoteError(f'All hosts failed for {url}: {last_exc}') from last_exc

        # last_resp must be a 5xx from the final origin
        assert last_resp is not None
        return last_resp

    # -----------------------------------------------------------------
    # Cache
    # -----------------------------------------------------------------

    def _cache_key(self, namespace: str, *parts: str) -> str:
        """Build a hex cache key from a namespace and variable parts."""
        canonical = '\0'.join([self._url, namespace] + list(parts))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _cache_read(self, key: str) -> bytes | None:
        """Return cached bytes if fresh, or None."""
        if self._cache_dir is None:
            return None
        path = os.path.join(self._cache_dir, f'{key}.lore.cache')
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        age = int(time.time() - st.st_mtime)
        if age > self._cache_ttl:
            logger.debug(
                'Cache expired (%ds > %ds): %s', age, self._cache_ttl, key[:12]
            )
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        logger.debug('Cache hit (%ds old): %s', age, key[:12])
        with open(path, 'rb') as f:
            return f.read()

    def _cache_write(self, key: str, data: bytes) -> None:
        """Write data to cache. Errors are logged but never raised."""
        if self._cache_dir is None:
            return
        path = os.path.join(self._cache_dir, f'{key}.lore.cache')
        try:
            _write_atomic(path, data)
        except OSError:
            logger.debug('Failed to write cache file: %s', path)

    def clear_cache(self) -> None:
        """Remove all cache files from *cache_dir*.

        Temporary files left by a killed process are removed too, once
        they are old enough that no running write can still own them.
        """
        if self._cache_dir is None:
            return
        now = time.time()
        for entry in os.listdir(self._cache_dir):
            if entry.endswith('.lore.cache') or _orphan_tmp(
                self._cache_dir, entry, now
            ):
                try:
                    os.unlink(os.path.join(self._cache_dir, entry))
                except OSError:
                    pass

    # -----------------------------------------------------------------
    # Authentication
    # -----------------------------------------------------------------

    def _authenticate_msgs(self, msgs: list[EmailMessage]) -> None:
        """Add Authentication-Results headers via authheaders."""
        if self._authenticate_message is None:
            return
        for msg in msgs:
            msg_bytes = msg.as_bytes()
            auth_result = self._authenticate_message(
                msg_bytes,
                'liblore',
                dkim=True,
                dmarc=True,
                arc=True,
                spf=False,
            )
            if auth_result:
                # authheaders returns the full header line, so strip
                # the name prefix before adding to the message.
                msg['Authentication-Results'] = auth_result.removeprefix(
                    'Authentication-Results: '
                )

    # -----------------------------------------------------------------
    # Primary API — raw mbox
    # -----------------------------------------------------------------

    @_operation_scope
    def get_mbox_by_msgid(self, msgid: str, *, nocache: bool = False) -> bytes:
        """Fetch a thread mbox by message ID and return the raw bytes.

        On a 404, falls back to a HEAD request against the bare server
        origin to discover the correct list path via redirect.  This
        handles public-inbox servers where the configured URL does not
        include the archive path (e.g. ``https://lore.kernel.org``
        instead of ``https://lore.kernel.org/all``).

        On a partial mirror, a thread the mirror doesn't have comes from
        the upstream archive.  :class:`~liblore.NotOnMirrorError` means
        the upstream could not be reached either.
        """
        key = self._cache_key('mbox_by_msgid', msgid)
        if not nocache:
            cached = self._cache_read(key)
            if cached is not None:
                return cached

        # The upstream node caches its own answers, under its own keys.
        # A named function, not a lambda: mypy can't infer a lambda
        # that calls this decorated method from inside its own body.
        def remote(upstream: LoreNode) -> bytes:
            return upstream.get_mbox_by_msgid(msgid, nocache=nocache)

        return self._answer(
            msgid, lambda: self._local_mbox_by_msgid(msgid, key), remote
        )

    def _local_mbox_by_msgid(self, msgid: str, key: str) -> bytes:
        """Fetch the thread mbox from this node, for :meth:`get_mbox_by_msgid`.

        Raises :class:`_Miss` when this partial mirror doesn't have the
        thread.
        """
        qmsgid = urllib.parse.quote_plus(msgid)
        mbox_url = f'{self._url}/{qmsgid}/t.mbox.gz'
        resp = self._request('GET', mbox_url)
        if resp.status_code == 404:
            upstream = self._fallback_node()
            if upstream is not None:
                # Skip the redirect lookup below: it drops the base
                # path, and the upstream archive has the whole thing.
                resp.close()
                raise _Miss(upstream)
            # The message may live under a different list path.  Try a
            # HEAD against the bare origin and follow redirects to
            # discover the canonical location.
            resp = self._resolve_msgid_via_head(qmsgid)
        if resp.status_code != 200:
            raise RemoteError(
                f'Server returned an error: {resp.status_code}',
                status_code=resp.status_code,
            )
        t_mbox = gzip.decompress(resp.content)
        resp.close()

        self._cache_write(key, t_mbox)
        return t_mbox

    def _resolve_msgid_via_head(self, qmsgid: str) -> requests.Response:
        """HEAD the bare origin to discover the list path, then GET the mbox.

        Public-inbox servers redirect ``/{msgid}/`` to
        ``/{list}/{msgid}/`` when the message-id is found.  This method
        follows that redirect and fetches the mbox from the discovered
        location.  Returns the response from the final GET (or the
        failed HEAD response if the message-id wasn't found).

        Both requests go through :meth:`_request`, so they fail over,
        honour a cancel, and raise :class:`RemoteError` when no host
        answers.
        """
        head_url = f'{self._canonical_origin}/{qmsgid}/'
        logger.debug('Trying HEAD %s for redirect discovery', head_url)
        head_resp = self._request('HEAD', head_url, allow_redirects=True)
        if head_resp.status_code != 200:
            return head_resp
        # The answer may come from a fallback origin, and _request()
        # expects URLs on the canonical one, so keep only the path.
        # Without a redirect this is head_url's own path.
        resolved = urllib.parse.urlsplit(head_resp.url).path.rstrip('/')
        mbox_url = f'{self._canonical_origin}{resolved}/t.mbox.gz'
        logger.debug('Resolved via redirect: %s', mbox_url)
        return self._request('GET', mbox_url)

    @_operation_scope
    def get_mbox_by_query(
        self,
        query: str,
        *,
        full_threads: bool = False,
        nocache: bool = False,
    ) -> bytes:
        """POST a search query and return the raw mbox bytes.

        When *full_threads* is ``True``, the server expands results to
        include the full thread for every matching message (public-inbox
        ``t=1`` parameter).

        On a partial mirror the mirror answers, and the upstream archive
        is asked only when the mirror finds nothing.  A hit the mirror
        doesn't have is then missed.  With ``partial_search='upstream'``
        the upstream archive is asked first, and the mirror only when
        the upstream archive can't be reached.
        """

        # A named function, not a lambda: mypy can't infer a lambda
        # that calls this decorated method from inside its own body.
        def remote(upstream: LoreNode) -> bytes:
            return upstream.get_mbox_by_query(
                query, full_threads=full_threads, nocache=nocache
            )

        key = self._cache_key('mbox_by_query', query, str(full_threads))
        upstream_first = self._partial_search == 'upstream'

        def local() -> bytes:
            if not nocache:
                cached = self._cache_read(key)
                if cached is not None:
                    return cached
            try:
                t_mbox = self._local_search(query, full_threads)
            except RemoteError as ex:
                # This answer may be the first to say the node is partial.
                upstream = self._fallback_node()
                if upstream is None or ex.status_code != 404:
                    raise
                raise _Miss(upstream, missing=f'No results for {query!r}') from ex
            upstream = self._fallback_node()
            if upstream is not None and (upstream_first or not t_mbox):
                # The mirror's answer, which may be incomplete: never
                # cached.
                raise _Miss(upstream, t_mbox, missing=f'No results for {query!r}')
            self._cache_write(key, t_mbox)
            return t_mbox

        return self._answer(
            f'Search {query!r}', local, remote, upstream_first=upstream_first
        )

    def _local_search(self, query: str, full_threads: bool) -> bytes:
        """POST a whole-archive search to this node and return the mbox.

        Raises :class:`RemoteError` for any answer but a 200.
        """
        t_param = '&t=1' if full_threads else ''
        query_url = (
            self._url + '/?x=m' + t_param + '&q=' + urllib.parse.quote_plus(query)
        )
        resp = self._request('POST', query_url, data='x=m')
        if resp.status_code != 200:
            raise RemoteError(
                f'Server returned an error: {resp.status_code}',
                status_code=resp.status_code,
            )
        t_mbox = gzip.decompress(resp.content)
        resp.close()
        return t_mbox

    # -----------------------------------------------------------------
    # Primary API — parsed messages
    # -----------------------------------------------------------------

    @_operation_scope
    def get_thread_by_msgid(
        self,
        msgid: str,
        *,
        strict: bool = True,
        sort: bool = False,
        since: str | None = None,
    ) -> list[EmailMessage]:
        """Fetch a thread from the public-inbox server.

        When *since* is given (a ``YYYYMMDDHHMMSS`` date string), only
        thread messages with a ``Date:`` header after that timestamp are
        returned.  This uses the per-message search endpoint
        (``/{msgid}/?x=m&q=dt:{since}..``) which scopes the query to
        the thread identified by *msgid* and filters individual messages
        by date.

        Without *since*, the full thread mbox is fetched via
        ``/{msgid}/t.mbox.gz``.

        When *strict* is ``True`` (default), the results are filtered
        to only messages belonging to the thread rooted at *msgid*.

        When *sort* is ``True``, messages are sorted by Received date.

        Raises :class:`LookupError` when no messages match.
        """
        if since:
            msgs = self._fetch_thread_since(msgid, f'dt:{since}..')
            if not msgs:
                raise LookupError(f'No messages found for msgid={msgid} since={since}')
        else:
            # Full thread: GET /{msgid}/t.mbox.gz
            t_mbox = self.get_mbox_by_msgid(msgid)
            if not t_mbox:
                raise LookupError(f'No messages found for msgid={msgid}')
            msgs = split_and_dedupe(t_mbox)

        if strict:
            strict_msgs = get_strict_thread(msgs, msgid)
            if not isinstance(strict_msgs, list) or not len(strict_msgs):
                raise LookupError(f'No messages found for msgid={msgid}')
            msgs = strict_msgs

        if sort:
            msgs = sort_msgs_by_received(msgs)

        self._authenticate_msgs(msgs)
        return msgs

    def _fetch_thread_since(
        self,
        msgid: str,
        query_fragment: str,
    ) -> list[EmailMessage]:
        """Fetch thread messages matching a date-range query fragment.

        *query_fragment* is a ready-to-use search term such as
        ``dt:20240101..`` or ``rt:1704067200..``.

        Uses the per-message search endpoint: the ``/{msgid}/`` path
        scopes the query to the thread.  POSTs with an empty body so
        that thread expansion is NOT triggered — only messages matching
        the range are returned.

        Returns an empty list when the server finds no matches.

        On a partial mirror, :meth:`_known_locally` runs first.  For a
        thread the mirror doesn't have, the search would cover the
        whole mirror instead of one thread, so it goes to the upstream
        archive and the mirror is not searched.  For a thread the
        mirror has, its answer is final, even when it is "nothing new".

        When the partial mirror is unreachable, the upstream archive
        answers.  When the mirror doesn't have the thread and the
        upstream is unreachable, :class:`~liblore.NotOnMirrorError` is
        raised: "nothing new" would be a guess.
        """

        def remote(upstream: LoreNode) -> list[EmailMessage]:
            return self._search_thread_upstream(upstream, msgid, query_fragment)

        def local() -> list[EmailMessage]:
            upstream = self._fallback_node()
            if upstream is not None and not self._known_locally(msgid):
                raise _Miss(upstream)
            msgs = self._search_thread(msgid, query_fragment)
            if upstream is None:
                # This answer may be the first to say the node is
                # partial, and then it can't be trusted without the
                # check.
                upstream = self._fallback_node()
                if upstream is not None and not self._known_locally(msgid):
                    raise _Miss(upstream)
            return msgs

        def on_outage(ex: RemoteError) -> list[EmailMessage]:
            if ex.status_code is None:
                raise ex
            # Every origin answered 5xx: a full node has always reported
            # that as "nothing new".
            return []

        return self._answer(f'Thread {msgid}', local, remote, on_outage=on_outage)

    def _search_thread(self, msgid: str, query_fragment: str) -> list[EmailMessage]:
        """Run the thread-scoped search on this node.

        Returns an empty list when the server finds no matches.  Raises
        :class:`RemoteError` when the server is unreachable, 5xx
        included.
        """
        qmsgid = urllib.parse.quote_plus(msgid)
        query = urllib.parse.quote_plus(query_fragment)
        search_url = f'{self._url}/{qmsgid}/?x=m&q={query}'
        resp = self._request('POST', search_url, raise_on_error=False, data='')
        if resp.status_code >= 500:
            resp.close()
            raise RemoteError(
                f'Server returned an error: {resp.status_code}',
                status_code=resp.status_code,
            )
        if resp.status_code != 200:
            return []
        t_mbox = gzip.decompress(resp.content)
        resp.close()
        if not t_mbox:
            return []
        return split_and_dedupe(t_mbox)

    def _search_thread_upstream(
        self,
        upstream: LoreNode,
        msgid: str,
        query_fragment: str,
    ) -> list[EmailMessage]:
        """Run :meth:`_search_thread` on *upstream*, as one operation.

        The upstream is a full archive, so it needs no known-thread
        check, and its 5xx answers raise instead of reading as
        "nothing new".
        """
        with upstream._operation():
            return upstream._search_thread(msgid, query_fragment)

    @_operation_scope
    def get_thread_updates_since(
        self,
        msgid: str,
        since: datetime,
        *,
        strict: bool = True,
        sort: bool = False,
    ) -> list[EmailMessage]:
        """Check a thread for messages newer than *since*.

        Uses the ``rt:`` (Received-date) search prefix, which filters
        by the server-set ``Received:`` header rather than the
        client-set ``Date:`` header, making it more reliable.
        Accepts a :class:`~datetime.datetime` (converted to a UTC
        epoch timestamp internally) and returns an empty list when
        there are no updates, making it easy to poll::

            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            updates = node.get_thread_updates_since(msgid, cutoff)
            if updates:
                print(f'{len(updates)} new message(s)')

        When *strict* is ``True`` (default), results are filtered to
        only messages belonging to the thread rooted at *msgid*.

        When *sort* is ``True``, messages are sorted by Received date.
        """
        epoch = int(since.astimezone(timezone.utc).timestamp())
        msgs = self._fetch_thread_since(msgid, f'rt:{epoch}..')

        if strict and msgs:
            strict_msgs = get_strict_thread(msgs, msgid)
            if isinstance(strict_msgs, list) and len(strict_msgs):
                msgs = strict_msgs
            else:
                return []

        if sort and msgs:
            msgs = sort_msgs_by_received(msgs)

        self._authenticate_msgs(msgs)
        return msgs

    @_operation_scope
    def get_thread_by_query(
        self,
        query: str,
        *,
        full_threads: bool = False,
    ) -> list[EmailMessage]:
        """POST a search query and return deduplicated messages.

        When *full_threads* is ``True``, the server expands results to
        include the full thread for every matching message.
        """
        t_mbox = self.get_mbox_by_query(query, full_threads=full_threads)
        if not t_mbox:
            raise LookupError(f'No results for query: {query}')
        msgs = split_and_dedupe(t_mbox)
        self._authenticate_msgs(msgs)
        return msgs

    @_operation_scope
    def get_message_by_msgid(self, msgid: str, *, nocache: bool = False) -> bytes:
        """Fetch a single raw email message by message ID.

        On a partial mirror, a message the mirror doesn't have comes
        from the upstream archive.  :class:`~liblore.NotOnMirrorError`
        means the upstream could not be reached either.
        """
        key = self._cache_key('message_by_msgid', msgid)
        if not nocache:
            cached = self._cache_read(key)
            if cached is not None:
                return cached

        # A named function, not a lambda: mypy can't infer a lambda
        # that calls this decorated method from inside its own body.
        def remote(upstream: LoreNode) -> bytes:
            return upstream.get_message_by_msgid(msgid, nocache=nocache)

        return self._answer(
            msgid, lambda: self._local_message_by_msgid(msgid, key), remote
        )

    def _local_message_by_msgid(self, msgid: str, key: str) -> bytes:
        """Fetch the raw message from this node, for :meth:`get_message_by_msgid`.

        Raises :class:`_Miss` when this partial mirror doesn't have the
        message.
        """
        raw_url = f'{self._url}/{urllib.parse.quote_plus(msgid)}/raw'

        def failed(ex: Exception) -> RemoteError:
            status = None
            if isinstance(ex, requests.HTTPError) and ex.response is not None:
                status = ex.response.status_code
            return RemoteError(
                f'Failed to fetch message from {raw_url}: {ex}',
                status_code=status,
            )

        try:
            response = self._request('GET', raw_url)
        except LibloreError:
            # RemoteError, and OperationCancelledError: a cancel is not
            # a server failure.
            raise
        except Exception as ex:
            raise failed(ex) from ex

        if response.status_code == 404:
            upstream = self._fallback_node()
            if upstream is not None:
                response.close()
                raise _Miss(upstream)

        try:
            response.raise_for_status()
            data: bytes = response.content
        except Exception as ex:
            raise failed(ex) from ex

        self._cache_write(key, data)
        return data

    # -----------------------------------------------------------------
    # Batch API
    # -----------------------------------------------------------------

    @_operation_scope
    def batch_get_thread_by_msgid(
        self,
        msgids: list[str],
        *,
        strict: bool = True,
        sort: bool = False,
        since: str | None = None,
    ) -> list[list[EmailMessage]]:
        """Fetch threads for multiple message IDs with rate limiting.

        Calls :meth:`get_thread_by_msgid` for each *msgid* with a
        100 ms cooldown between requests (no sleep before the first).

        Returns a list of results in the same order as the input.
        """
        results: list[list[EmailMessage]] = []
        for i, msgid in enumerate(msgids):
            if i > 0:
                time.sleep(0.1)
            results.append(
                self.get_thread_by_msgid(msgid, strict=strict, sort=sort, since=since)
            )
        return results

    @_operation_scope
    def batch_get_thread_by_query(
        self,
        queries: list[str],
        *,
        full_threads: bool = False,
    ) -> list[list[EmailMessage]]:
        """Run multiple queries with rate limiting.

        Calls :meth:`get_thread_by_query` for each query with a
        100 ms cooldown between requests (no sleep before the first).

        When *full_threads* is ``True``, the server expands results to
        include the full thread for every matching message.

        Returns a list of results in the same order as the input.
        """
        results: list[list[EmailMessage]] = []
        for i, query in enumerate(queries):
            if i > 0:
                time.sleep(0.1)
            results.append(self.get_thread_by_query(query, full_threads=full_threads))
        return results

    @_operation_scope
    def validate(self) -> None:
        """Validate the URL as a public-inbox endpoint.

        Sends a HEAD request to ``{url}/_/text/help/`` and checks for a
        200 response.

        Raises :class:`~liblore.RemoteError` when the URL does not
        appear to be a public-inbox server.
        """
        help_url = f'{self._url}/_/text/help/'
        logger.debug('Validating public-inbox URL: %s', help_url)
        try:
            session = self._get_session()
            resp = session.head(help_url, timeout=self._request_timeout)
        except Exception as ex:
            # cancel_active() and shutdown() close the session mid-flight.
            if self._cancelled():
                raise OperationCancelledError('Request cancelled') from ex
            raise RemoteError(
                f'Failed to reach public-inbox at {help_url}: {ex}'
            ) from ex
        if resp.status_code != 200:
            raise RemoteError(
                f'URL does not appear to be a public-inbox server '
                f'(HEAD {help_url} returned {resp.status_code})',
                status_code=resp.status_code,
            )
