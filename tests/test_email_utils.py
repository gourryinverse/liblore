# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for liblore.email_utils."""

from __future__ import annotations

from email.message import EmailMessage

from conftest import MsgFactory

from liblore import emlpolicy
from liblore.utils import (
    msg_get_author,
    msg_get_payload,
    msg_get_recipients,
    msg_get_subject,
    sort_msgs_by_received,
)


class TestMsgGetSubject:
    def test_plain_subject(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='Just a plain subject')
        assert msg_get_subject(msg) == 'Just a plain subject'

    def test_no_strip(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='[PATCH v3 2/5] subsys: fix thing')
        assert msg_get_subject(msg) == '[PATCH v3 2/5] subsys: fix thing'

    def test_strip_patch_prefix(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='[PATCH v3 2/5] subsys: fix thing')
        assert msg_get_subject(msg, strip_prefixes=True) == 'subsys: fix thing'

    def test_strip_re_and_prefix(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='Re: [PATCH] Something cool')
        assert msg_get_subject(msg, strip_prefixes=True) == 'Something cool'

    def test_strip_multiple_prefixes(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='[RFC PATCH v2 0/3] [net-next] New feature')
        result = msg_get_subject(msg, strip_prefixes=True)
        assert result == 'New feature'

    def test_strip_aw_prefix(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='Aw: [PATCH] German reply')
        assert msg_get_subject(msg, strip_prefixes=True) == 'German reply'

    def test_no_subject(self) -> None:
        msg = EmailMessage(policy=emlpolicy)
        assert msg_get_subject(msg) == ''

    def test_strip_no_brackets(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='No brackets here')
        assert msg_get_subject(msg, strip_prefixes=True) == 'No brackets here'


class TestMsgGetAuthor:
    def test_normal_from(self, make_msg: MsgFactory) -> None:
        msg = make_msg(from_addr=('Jane Doe', 'jane@example.com'))
        name, addr = msg_get_author(msg)
        assert name == 'Jane Doe'
        assert addr == 'jane@example.com'

    def test_missing_from(self) -> None:
        msg = EmailMessage(policy=emlpolicy)
        name, addr = msg_get_author(msg)
        assert name == ''
        assert addr == 'missing@address.local'

    def test_empty_name(self) -> None:
        msg = EmailMessage(policy=emlpolicy)
        msg['From'] = 'noname@example.com'
        name, addr = msg_get_author(msg)
        assert name == ''
        assert addr == 'noname@example.com'


class TestMsgGetPayload:
    def test_plain_body(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='This is the body.\n')
        assert 'This is the body.' in msg_get_payload(msg)

    def test_strip_signature(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='Body text.\n-- \nMy Sig\n')
        result = msg_get_payload(msg, strip_signature=True)
        assert 'Body text.' in result
        assert 'My Sig' not in result

    def test_keep_signature(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='Body text.\n-- \nMy Sig\n')
        result = msg_get_payload(msg, strip_signature=False)
        assert 'My Sig' in result

    def test_strip_quoted(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='My reply.\n> Quoted line.\nMore text.\n')
        result = msg_get_payload(msg, strip_quoted=True)
        assert 'My reply.' in result
        assert 'Quoted line.' not in result
        assert 'More text.' in result

    def test_empty_body(self) -> None:
        msg = EmailMessage(policy=emlpolicy)
        assert msg_get_payload(msg) == ''

    def test_unknown_charset(self, make_msg: MsgFactory) -> None:
        """``charset="yes"`` is a real thing posted to the lists, and
        Python raises LookupError rather than returning replacement
        characters: the codec name is what cannot be resolved, so
        ``errors='replace'`` never gets a say."""
        msg = make_msg(body='Body text.\n')
        msg.set_param('charset', 'yes')
        assert 'Body text.' in msg_get_payload(msg)

    def test_unknown_charset_keeps_non_ascii(self, make_msg: MsgFactory) -> None:
        """Falling back must still decode what it can, rather than
        discarding the body or mangling every byte above ASCII."""
        msg = make_msg(body='Ceci coûte cher.\n')
        msg.set_param('charset', 'definitely-not-a-codec')
        assert 'coûte' in msg_get_payload(msg)


class TestMsgGetRecipients:
    def test_to_cc_from(self) -> None:
        msg = EmailMessage(policy=emlpolicy)
        msg['To'] = 'alice@example.com'
        msg['Cc'] = 'bob@example.com'
        msg['From'] = 'carol@example.com'
        recips = msg_get_recipients(msg)
        assert recips == {'alice@example.com', 'bob@example.com', 'carol@example.com'}

    def test_case_normalisation(self) -> None:
        msg = EmailMessage(policy=emlpolicy)
        msg['To'] = 'ALICE@EXAMPLE.COM'
        msg['From'] = 'alice@example.com'
        recips = msg_get_recipients(msg)
        assert recips == {'alice@example.com'}


class TestSortMsgsByReceived:
    def test_sorts_by_date(self, make_msg: MsgFactory) -> None:
        msg1 = make_msg(
            msgid='older@x.com',
            date='Mon, 01 Jan 2024 00:00:00 +0000',
        )
        msg2 = make_msg(
            msgid='newer@x.com',
            date='Tue, 02 Jan 2024 00:00:00 +0000',
        )
        # Feed them in reverse order
        result = sort_msgs_by_received([msg2, msg1])
        assert result[0]['Message-Id'] == '<older@x.com>'
        assert result[1]['Message-Id'] == '<newer@x.com>'

    def test_skips_dateless(self, make_msg: MsgFactory) -> None:
        msg = make_msg()
        # No Date header set at all — del it if make_msg added one
        if 'Date' in msg:
            del msg['Date']
        result = sort_msgs_by_received([msg])
        assert result == []
