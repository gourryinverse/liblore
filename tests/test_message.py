# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for liblore.message."""

from __future__ import annotations

from email.message import EmailMessage

import pytest
from conftest import MsgFactory

from liblore.utils import _clean_msgid_raw, clean_header, get_clean_msgid, parse_message


class TestCleanHeader:
    def test_plain_header(self) -> None:
        assert clean_header('Hello World') == 'Hello World'

    def test_none_returns_empty(self) -> None:
        assert clean_header(None) == ''

    def test_rfc2047_encoded(self) -> None:
        encoded = '=?utf-8?q?Caf=C3=A9?='
        assert clean_header(encoded) == 'Caf\u00e9'

    def test_folded_whitespace(self) -> None:
        assert clean_header('Hello\n World') == 'Hello World'

    def test_encoded_address(self) -> None:
        # formataddr re-encodes Unicode names, so we just check
        # the address is preserved and the result is valid
        hdr = '=?utf-8?q?=C3=89ric?= <eric@example.com>'
        result = clean_header(hdr)
        assert 'eric@example.com' in result

    def test_special_chars_in_name(self) -> None:
        hdr = '=?utf-8?q?O=27Brien=2C_Joe?= <joe@example.com>'
        result = clean_header(hdr)
        assert 'joe@example.com' in result


class TestGetCleanMsgid:
    def test_extracts_msgid(self, make_msg: MsgFactory) -> None:
        msg = make_msg(msgid='test123@example.com')
        assert get_clean_msgid(msg) == 'test123@example.com'

    def test_missing_header(self) -> None:
        msg = EmailMessage()
        assert get_clean_msgid(msg) is None

    def test_custom_header(self, make_msg: MsgFactory) -> None:
        msg = make_msg(in_reply_to='parent@example.com')
        assert get_clean_msgid(msg, 'In-Reply-To') == 'parent@example.com'

    def test_in_reply_to_with_encoded_comment(self) -> None:
        """Gnus appends an RFC 2047 encoded comment after the message-ID.

        The encoded-word comment must not send us down clean_header's
        address-parsing path, which would destroy the message-ID.
        """
        msg = EmailMessage()
        msg['In-Reply-To'] = (
            '<patch-v1-1-abc123@example.com>'
            ' (=?utf-8?Q?=22Marc-Andr=C3=A9?= Lureau"\'s'
            ' message of "Mon, 20 Jul 2026 11:55:30 +0400")'
        )
        assert get_clean_msgid(msg, 'In-Reply-To') == 'patch-v1-1-abc123@example.com'

    def test_in_reply_to_with_encoded_comment_parsed(self) -> None:
        """Same case, but via a message parsed under the modern policy."""
        raw = (
            b'From: x@example.com\r\n'
            b'Message-Id: <child@example.com>\r\n'
            b'In-Reply-To: <patch-v1-1-abc123@example.com>'
            b' (=?utf-8?Q?=22Marc-Andr=C3=A9?= Lureau"\'s'
            b' message of "Mon, 20 Jul 2026 11:55:30 +0400")\r\n'
            b'\r\nbody\r\n'
        )
        msg = parse_message(raw)
        assert get_clean_msgid(msg, 'In-Reply-To') == 'patch-v1-1-abc123@example.com'

    # --- Non-compliant / malformed headers ---------------------------------
    # These document how extraction copes with headers that violate
    # RFC 5322.  The contract is deliberately simple: we return the
    # contents of the first ``<...>`` pair, or None when there isn't one.

    def test_missing_angle_brackets(self) -> None:
        """A bare address without angle brackets yields no message-ID."""
        msg = EmailMessage()
        msg['Message-Id'] = 'bare@example.com'
        assert get_clean_msgid(msg) is None

    def test_empty_angle_brackets(self) -> None:
        """An empty ``<>`` has nothing to extract."""
        msg = EmailMessage()
        msg['In-Reply-To'] = '<>'
        assert get_clean_msgid(msg, 'In-Reply-To') is None

    def test_no_at_sign_inside_brackets(self) -> None:
        """We trust the angle brackets and do not require an ``@``."""
        msg = EmailMessage()
        msg['Message-Id'] = '<garbage-no-at>'
        assert get_clean_msgid(msg) == 'garbage-no-at'

    def test_leading_comment_before_id(self) -> None:
        """A comment preceding the message-ID must not confuse extraction."""
        msg = EmailMessage()
        msg['In-Reply-To'] = '(sent earlier) <parent@example.com>'
        assert get_clean_msgid(msg, 'In-Reply-To') == 'parent@example.com'

    def test_multiple_ids_returns_first(self) -> None:
        """Some broken clients cram several IDs into In-Reply-To."""
        msg = EmailMessage()
        msg['In-Reply-To'] = '<first@example.com> <second@example.com>'
        assert get_clean_msgid(msg, 'In-Reply-To') == 'first@example.com'

    def test_rfc2047_encoded_msgid(self) -> None:
        """A nonconformant fully RFC 2047-encoded ``<foo@bar>``.

        RFC 2047 forbids encoded words in msg-id headers, but if one
        shows up the modern EmailPolicy decodes it on read, so we still
        recover the ID -- no clean_header fallback required.
        """
        msg = EmailMessage()
        msg['Message-Id'] = '=?utf-8?q?=3Cfoo=40bar=3E?='
        assert get_clean_msgid(msg) == 'foo@bar'

    def test_rfc2047_encoded_msgid_parsed(self) -> None:
        """Same nonconformant case, parsed under the real policy."""
        raw = (
            b'From: x@example.com\r\n'
            b'Message-Id: =?utf-8?q?=3Cfoo=40bar=3E?=\r\n'
            b'\r\nbody\r\n'
        )
        msg = parse_message(raw)
        assert get_clean_msgid(msg) == 'foo@bar'

    # --- Characters the header parser chokes on -------------------------
    # The policy's Message-ID parser reads "(" as a comment and stops at a
    # stray quote, so these must come from the raw source value instead.

    @pytest.mark.parametrize(
        'msgid',
        [
            'x$(id)@x',
            'a(b)c@x',
            'a"b@x',
            '"quoted local"@x',
            'a/b?c@x',
        ],
    )
    def test_parser_unfriendly_characters(self, msgid: str) -> None:
        raw = f'From: x@example.com\r\nMessage-ID: <{msgid}>\r\n\r\nbody\r\n'
        msg = parse_message(raw.encode())
        assert get_clean_msgid(msg) == msgid

    def test_message_id_matches_in_reply_to(self) -> None:
        """A reply must point at the exact ID its parent reports."""
        raw = (
            b'From: x@example.com\r\n'
            b'Message-ID: <x$(id)@x>\r\n'
            b'In-Reply-To: <x$(id)@x>\r\n'
            b'\r\nbody\r\n'
        )
        msg = parse_message(raw)
        assert get_clean_msgid(msg) == get_clean_msgid(msg, 'In-Reply-To')
        assert get_clean_msgid(msg) == _clean_msgid_raw(raw)

    def test_header_name_is_case_insensitive(self) -> None:
        raw = b'From: x@example.com\r\nmessage-id: <a(b)c@x>\r\n\r\nbody\r\n'
        msg = parse_message(raw)
        assert get_clean_msgid(msg, 'Message-Id') == 'a(b)c@x'

    def test_first_header_wins(self) -> None:
        raw = (
            b'From: x@example.com\r\n'
            b'Message-ID: <first(1)@x>\r\n'
            b'Message-ID: <second@x>\r\n'
            b'\r\nbody\r\n'
        )
        msg = parse_message(raw)
        assert get_clean_msgid(msg) == 'first(1)@x'

    def test_folded_header(self) -> None:
        raw = b'From: x@example.com\r\nMessage-ID:\r\n <a(b)c@x>\r\n\r\nbody\r\n'
        msg = parse_message(raw)
        assert get_clean_msgid(msg) == 'a(b)c@x'

    def test_list_id(self) -> None:
        """List-Id is read the same way when picking among duplicates."""
        raw = (
            b'From: x@example.com\r\n'
            b'List-Id: Kernel list <linux-kernel.vger.kernel.org>\r\n'
            b'\r\nbody\r\n'
        )
        msg = parse_message(raw)
        assert get_clean_msgid(msg, 'List-Id') == 'linux-kernel.vger.kernel.org'


class TestParseMessage:
    def test_roundtrip(self) -> None:
        raw = (
            b'From: test@example.com\r\n'
            b'Subject: Test\r\n'
            b'Message-Id: <test@example.com>\r\n'
            b'\r\n'
            b'Body text\r\n'
        )
        msg = parse_message(raw)
        assert isinstance(msg, EmailMessage)
        assert msg['Subject'] == 'Test'
        assert msg['From'] == 'test@example.com'
