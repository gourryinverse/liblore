# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2025-2026 The Linux Foundation
"""liblore — shared library for public-inbox / lore.kernel.org access."""

from __future__ import annotations

import email.charset
import email.policy
import enum
from email.message import EmailMessage

__version__ = '0.9.0'

# Email policy used for parsing and serialising messages
emlpolicy = email.policy.EmailPolicy(
    utf8=True,
    cte_type='8bit',
    max_line_length=None,
    message_factory=EmailMessage,
)

# Disable base64 encoding for utf-8 content
email.charset.add_charset('utf-8', email.charset.SHORTEST, None, 'utf-8')


class LibloreError(Exception):
    """Base exception for all liblore errors."""


class RemoteError(LibloreError):
    """Raised when a remote HTTP request fails.

    :attr:`status_code` is the HTTP status the server answered with, or
    ``None`` when no answer arrived at all (connection error, timeout).
    It lets callers tell "the server says it doesn't have this" (404)
    apart from "the server could not be reached".
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotOnMirrorError(RemoteError):
    """A partial mirror doesn't have it, and its upstream could not be reached.

    Raised only by a node that knows it is a partial mirror
    (``X-Archive-Coverage: partial``).  The thing asked for may well
    exist: the mirror doesn't have it, and the upstream archive that
    would have it did not answer.  :attr:`status_code` is the
    upstream's status, or ``None`` when it did not answer at all.
    """


class Source(enum.Enum):
    """Where an answer came from, as :attr:`LoreNode.last_source` reports it.

    ``LOCAL``
        Answered by this node: its server, or its cache.  On a node
        that is not a partial mirror, such as lore.kernel.org itself,
        every answer is ``LOCAL``: the word means "this node", not "a
        local mirror".
    ``UPSTREAM``
        Answered by the upstream archive of a partial mirror, because
        the mirror didn't have it or could not be reached.
    ``DEGRADED``
        Answered by a partial mirror because its upstream archive could
        not be reached, so it may be incomplete.  Only upstream-first
        searches (``partial_search='upstream'``) give it.
    """

    LOCAL = 'local'
    UPSTREAM = 'upstream'
    DEGRADED = 'degraded'


class PublicInboxError(LibloreError):
    """Raised when a public-inbox operation fails."""


class OperationCancelledError(LibloreError):
    """Raised when an in-flight or pending request was cancelled by the caller."""


from liblore.node import LoreNode

__all__ = [
    'LibloreError',
    'LoreNode',
    'NotOnMirrorError',
    'OperationCancelledError',
    'PublicInboxError',
    'RemoteError',
    'Source',
    '__version__',
    'emlpolicy',
]
