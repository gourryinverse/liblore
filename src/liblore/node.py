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
import subprocess
import threading
import time
import urllib.parse
import warnings
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

import requests

from liblore import LibloreError, OperationCancelledError, RemoteError
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


class _LoreNodeInitKwargs(TypedDict, total=False):
    fallback_urls: list[str] | None
    auto_probe: bool
    probe_timeout: float
    probe_ttl: int
    request_timeout: tuple[float, float] | float | None
    add_auth_headers: bool
    cache_dir: str | None
    cache_ttl: int


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
            multivals=['fallback'],
        )
        if not gitcfg and parsed.netloc == 'lore.kernel.org':
            gitcfg = _get_config_from_git(r'^lore\.', multivals=['fallback'])

        if 'fallback_urls' not in kwargs:
            fallbacks = gitcfg.get('fallback')
            if isinstance(fallbacks, list) and fallbacks:
                kwargs['fallback_urls'] = fallbacks

        if 'auto_probe' not in kwargs:
            val = gitcfg.get('autoprobe')
            if isinstance(val, str):
                kwargs['auto_probe'] = val.lower() == 'true'

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

    def set_requests_session(self, session: requests.Session) -> None:
        """Inject a pre-existing :class:`requests.Session`.

        The session's User-Agent header is **not** overwritten — the
        caller owns it.
        """
        self._session = session
        self._owns_session = False
        logger.debug('Using caller-provided requests session')

    def _get_session(self) -> requests.Session:
        """Return the session, creating one if needed."""
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({'User-Agent': self._user_agent})
            self._owns_session = True
        return self._session

    def close(self) -> None:
        """Close the session if we created it (not externally injected)."""
        if self._session is not None and self._owns_session:
            self._session.close()
        self._session = None
        self._owns_session = False

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
        tmp_path = path + '.tmp'
        try:
            with open(tmp_path, 'wb') as f:
                f.write(json.dumps(origins).encode())
            os.replace(tmp_path, path)
        except OSError:
            logger.debug('Failed to write probe cache: %s', path)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

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
        tmp_path = path + '.tmp'
        try:
            with open(tmp_path, 'wb') as f:
                f.write(data)
            os.replace(tmp_path, path)
        except OSError:
            logger.debug('Failed to write cache file: %s', path)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def clear_cache(self) -> None:
        """Remove all cache files from *cache_dir*."""
        if self._cache_dir is None:
            return
        for entry in os.listdir(self._cache_dir):
            if entry.endswith('.lore.cache'):
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
        """
        key = self._cache_key('mbox_by_msgid', msgid)
        if not nocache:
            cached = self._cache_read(key)
            if cached is not None:
                return cached

        qmsgid = urllib.parse.quote_plus(msgid)
        mbox_url = f'{self._url}/{qmsgid}/t.mbox.gz'
        resp = self._request('GET', mbox_url)
        if resp.status_code == 404:
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
        failed HEAD response if no redirect was found).
        """
        head_url = f'{self._canonical_origin}/{qmsgid}/'
        session = self._get_session()
        logger.debug('Trying HEAD %s for redirect discovery', head_url)
        head_resp = session.head(
            head_url,
            allow_redirects=True,
            timeout=self._request_timeout,
        )
        if head_resp.status_code == 200 and head_resp.url != head_url:
            # Redirected — use the resolved location
            resolved = head_resp.url.rstrip('/')
            mbox_url = f'{resolved}/t.mbox.gz'
            logger.debug('Resolved via redirect: %s', mbox_url)
            return self._request('GET', mbox_url)
        return head_resp

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
        """
        key = self._cache_key('mbox_by_query', query, str(full_threads))
        if not nocache:
            cached = self._cache_read(key)
            if cached is not None:
                return cached

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

        self._cache_write(key, t_mbox)
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
        """
        qmsgid = urllib.parse.quote_plus(msgid)
        query = urllib.parse.quote_plus(query_fragment)
        search_url = f'{self._url}/{qmsgid}/?x=m&q={query}'
        resp = self._request('POST', search_url, raise_on_error=False, data='')
        if resp.status_code != 200:
            return []
        t_mbox = gzip.decompress(resp.content)
        resp.close()
        if not t_mbox:
            return []
        return split_and_dedupe(t_mbox)

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
        """Fetch a single raw email message by message ID."""
        key = self._cache_key('message_by_msgid', msgid)
        if not nocache:
            cached = self._cache_read(key)
            if cached is not None:
                return cached

        raw_url = f'{self._url}/{urllib.parse.quote_plus(msgid)}/raw'
        try:
            response = self._request('GET', raw_url)
            response.raise_for_status()
            data: bytes = response.content
        except RemoteError:
            raise
        except Exception as ex:
            status = None
            if isinstance(ex, requests.HTTPError) and ex.response is not None:
                status = ex.response.status_code
            raise RemoteError(
                f'Failed to fetch message from {raw_url}: {ex}',
                status_code=status,
            ) from ex

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
            raise RemoteError(
                f'Failed to reach public-inbox at {help_url}: {ex}'
            ) from ex
        if resp.status_code != 200:
            raise RemoteError(
                f'URL does not appear to be a public-inbox server '
                f'(HEAD {help_url} returned {resp.status_code})',
                status_code=resp.status_code,
            )
