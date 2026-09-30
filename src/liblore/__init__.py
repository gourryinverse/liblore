# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2025-2026 The Linux Foundation
"""liblore — shared library for public-inbox / lore.kernel.org access."""

from __future__ import annotations

import email.charset
import email.policy
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


class PublicInboxError(LibloreError):
    """Raised when a public-inbox operation fails."""


class OperationCancelledError(LibloreError):
    """Raised when an in-flight or pending request was cancelled by the caller."""


from liblore.node import LoreNode

__all__ = [
    'LibloreError',
    'LoreNode',
    'OperationCancelledError',
    'PublicInboxError',
    'RemoteError',
    '__version__',
    'emlpolicy',
]
