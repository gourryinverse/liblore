# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for liblore.series."""

from __future__ import annotations

import json

import pytest
from conftest import MsgFactory

from liblore.series import (
    ThreadSummary,
    Trailer,
    extract_trailers,
    group_by_thread,
    parse_patch_subject,
    summarize_thread,
    summarize_threads,
)
from liblore.utils import msg_get_author, msg_get_inbody_author

DATE1 = 'Mon, 15 Sep 2026 10:00:00 +0000'
DATE2 = 'Mon, 15 Sep 2026 11:00:00 +0000'
DATE3 = 'Mon, 15 Sep 2026 12:00:00 +0000'

# What turns a body into a real patch: the basement line plus a diff.
PATCH_TAIL = (
    '---\n'
    ' net/core.c | 2 +-\n'
    ' 1 file changed, 1 insertion(+), 1 deletion(-)\n'
    '--- a/net/core.c\n'
    '+++ b/net/core.c\n'
)


class TestParsePatchSubject:
    def test_plain_subject(self) -> None:
        parsed = parse_patch_subject('Just a question about USB')
        assert parsed.is_patch is False
        assert parsed.title == 'Just a question about USB'
        assert parsed.version == 1
        assert parsed.counter == 1
        assert parsed.counters_inferred is True

    def test_lone_patch(self) -> None:
        parsed = parse_patch_subject('[PATCH] usb: fix the thing')
        assert parsed.is_patch is True
        assert parsed.title == 'usb: fix the thing'
        assert parsed.counter == 1
        assert parsed.counters_inferred is True
        assert parsed.expected == 1
        assert parsed.version == 1

    def test_numbered_and_versioned(self) -> None:
        parsed = parse_patch_subject('[PATCH v3 2/7] usb: fix the thing')
        assert (parsed.counter, parsed.expected, parsed.version) == (2, 7, 3)
        assert parsed.title == 'usb: fix the thing'
        assert parsed.is_cover is False

    def test_cover_letter(self) -> None:
        parsed = parse_patch_subject('[PATCH v2 0/4] usb: a series')
        assert parsed.is_cover is True
        assert parsed.counter == 0

    def test_extra_prefixes_preserved_in_order(self) -> None:
        parsed = parse_patch_subject('[RFC PATCH net-next v3 2/7] net: thing')
        assert parsed.prefixes == ('RFC', 'net-next')
        assert parsed.version == 3
        assert parsed.is_patch is True

    def test_split_bracket_groups(self) -> None:
        parsed = parse_patch_subject('[RFC] [PATCH 1/2] subject here')
        assert parsed.is_patch is True
        assert parsed.prefixes == ('RFC',)
        assert (parsed.counter, parsed.expected) == (1, 2)
        assert parsed.title == 'subject here'

    def test_reply_is_flagged(self) -> None:
        parsed = parse_patch_subject('Re: [PATCH v3 2/7] usb: fix the thing')
        assert parsed.is_reply is True
        assert parsed.is_patch is True

    def test_nested_reply_marker(self) -> None:
        assert parse_patch_subject('Re: Re: [PATCH] x').is_reply is True
        assert parse_patch_subject('Re[2]: [PATCH] x').is_reply is True

    def test_spaced_counter(self) -> None:
        parsed = parse_patch_subject('[PATCH 2 / 7] spaced out')
        assert (parsed.counter, parsed.expected) == (2, 7)

    def test_version_is_case_insensitive(self) -> None:
        assert parse_patch_subject('[PATCH V4 1/2] x').version == 4

    def test_resend_is_a_prefix_not_a_version(self) -> None:
        parsed = parse_patch_subject('[PATCH RESEND 1/3] x')
        assert parsed.prefixes == ('RESEND',)
        assert parsed.version == 1

    def test_glued_version(self) -> None:
        # "[PATCHv3]" gets written without the space often enough that b4
        # repairs it before tokenising; so do we.
        parsed = parse_patch_subject('[PATCHv3 2/7] usb: fix the thing')
        assert parsed.is_patch is True
        assert parsed.version == 3
        assert parsed.counter == 2
        assert parsed.expected == 7
        assert parsed.prefixes == ()

    def test_punctuation_between_chunks(self) -> None:
        parsed = parse_patch_subject('[PATCH, v2, 1/2] usb: fix the thing')
        assert parsed.version == 2
        assert parsed.counter == 1
        assert parsed.prefixes == ()

    def test_foreign_reply_marker(self) -> None:
        # A reply marker in a language we do not know, sitting directly in
        # front of the bracket group.
        parsed = parse_patch_subject('Odp: [PATCH 1/2] usb: fix the thing')
        assert parsed.is_reply is True
        assert parsed.is_patch is True
        assert parsed.counter == 1

    def test_oversized_counter_is_not_a_counter(self) -> None:
        # Bounded to four digits, so a date cannot pose as "n/m".
        parsed = parse_patch_subject('[PATCH 12345/67890] usb: fix the thing')
        assert parsed.counters_inferred is True
        assert parsed.prefixes == ('12345/67890',)

    def test_nested_brackets_collapse(self) -> None:
        parsed = parse_patch_subject('[[PATCH v2 1/2]] usb: fix the thing')
        assert parsed.is_patch is True
        assert parsed.version == 2
        assert parsed.counter == 1

    def test_unbalanced_bracket_is_not_a_prefix(self) -> None:
        parsed = parse_patch_subject('[PATCH v2 usb: fix the thing')
        assert parsed.is_patch is False
        assert parsed.title == '[PATCH v2 usb: fix the thing'


class TestExtractTrailers:
    def test_column_zero_trailers_are_kept(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body='Some text\n\nSigned-off-by: Alice <alice@example.com>\n'
            'Reviewed-by: Bob <bob@example.com>\n'
        )
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Signed-off-by', 'Alice <alice@example.com>'),
            ('Reviewed-by', 'Bob <bob@example.com>'),
        ]

    def test_quoted_trailer_is_skipped(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='> Reviewed-by: Bob <bob@example.com>\n\nI agree.\n')
        assert extract_trailers(msg) == []

    def test_indented_trailer_is_kept(self, make_msg: MsgFactory) -> None:
        # Some maintainers send their trailers with leading whitespace.
        msg = make_msg(body='Thanks!\n\n    Acked-by: Bob <bob@example.com>\n')
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Acked-by', 'Bob <bob@example.com>'),
        ]

    def test_diff_content_produces_no_trailers(self, make_msg: MsgFactory) -> None:
        # Everything below the "---" separator is the basement, so no
        # amount of diff content can produce a trailer.
        msg = make_msg(
            body=(
                'usb: fix it\n\n'
                'Signed-off-by: Alice <alice@example.com>\n'
                '---\n'
                ' drivers/usb/core.c | 2 +-\n'
                '--- a/drivers/usb/core.c\n'
                '+++ b/drivers/usb/core.c\n'
                '@@ -1,3 +1,3 @@\n'
                '-\tint foo: 1;\n'
                '+\tint bar: 2;\n'
                ' context: line\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Signed-off-by']

    def test_commit_subject_is_not_a_trailer(self, make_msg: MsgFactory) -> None:
        # 62% of recent linux.git subjects match the bare trailer shape,
        # so the unhyphenated subsystem prefix must not be picked up.
        msg = make_msg(
            body='usb: fix the thing\n\nSome explanation.\n\n'
            'Signed-off-by: Alice <alice@example.com>\n'
        )
        assert [t.name for t in extract_trailers(msg)] == ['Signed-off-by']

    def test_unhyphenated_trailers_are_kept(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body=(
                'net: fix it\n\n'
                'Fixes: deadbeef1234 ("net: break it")\n'
                'Cc: stable@vger.kernel.org\n'
                'Link: https://lore.kernel.org/r/x@y\n'
                '---\n'
                ' net/core.c | 2 +-\n'
                ' 1 file changed, 1 insertion(+), 1 deletion(-)\n'
                '--- a/net/core.c\n'
                '+++ b/net/core.c\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Fixes', 'Cc', 'Link']

    def test_trailer_needs_a_value(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='Reviewed-by:\nAcked-by: \n')
        assert extract_trailers(msg) == []

    def test_key_is_lowercased(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='Reviewed-By: Bob <bob@example.com>\n')
        assert extract_trailers(msg)[0].key == 'reviewed-by'

    def test_wrapped_fixes_trailer_is_rejoined(self, make_msg: MsgFactory) -> None:
        # Mail clients love to wrap a long Fixes: line; b4 stitches it
        # back together before scanning.
        msg = make_msg(
            body=(
                'usb: fix it\n\n'
                'Fixes: 1234abcd5678 ("usb: introduce the thing\n'
                'that later broke")\n'
            )
        )
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Fixes', '1234abcd5678 ("usb: introduce the thing that later broke")'),
        ]

    def test_wrapped_signoff_address_is_rejoined(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body=('usb: fix it\n\nSigned-off-by: Alice Longname\n<alice@example.com>\n')
        )
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Signed-off-by', 'Alice Longname <alice@example.com>'),
        ]

    def test_contact_details_are_not_trailers(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body=(
                'usb: fix it\n\n'
                'Signed-off-by: Alice <alice@example.com>\n'
                'Phone: +1 555 0100\n'
                'E-mail: alice@example.com\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Signed-off-by']

    def test_fixes_value_must_look_like_a_hash(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='usb: fix it\n\nFixes: the thing I broke earlier\n')
        assert extract_trailers(msg) == []

    def test_extinfo_comment_is_stripped(self, make_msg: MsgFactory) -> None:
        # Stripping the comment also means the same trailer with and
        # without one deduplicates to a single entry.
        msg = make_msg(
            body=(
                'usb: fix it\n\n'
                'Cc: stable@vger.kernel.org # 5.10+\n'
                '---\n'
                ' net/core.c | 2 +-\n'
                ' 1 file changed, 1 insertion(+), 1 deletion(-)\n'
                '--- a/net/core.c\n'
                '+++ b/net/core.c\n'
            )
        )
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Cc', 'stable@vger.kernel.org'),
        ]

    def test_extinfo_continuation_is_not_a_trailer(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body=(
                'usb: fix it\n\nAcked-by: Bob <bob@example.com>\n[for the usb bits]\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Acked-by']

    def test_separator_without_a_diff_is_not_a_basement(
        self, make_msg: MsgFactory
    ) -> None:
        # A bare "---" in a free-form reply is just a visual separator, so
        # trailers below it still count.
        msg = make_msg(
            body=('Looks good to me.\n---\nReviewed-by: Bob <bob@example.com>\n')
        )
        assert [t.name for t in extract_trailers(msg)] == ['Reviewed-by']

    def test_signature_block_is_ignored(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body=(
                'usb: fix it\n\n'
                'Signed-off-by: Alice <alice@example.com>\n'
                '-- \n'
                'Cc: not-a-real-trailer@example.com\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Signed-off-by']

    def test_reply_trailer_mid_body_is_found(self, make_msg: MsgFactory) -> None:
        # Reviewers put their tag wherever they like, so a reply is
        # scanned whole rather than only in its last paragraph.
        msg = make_msg(
            body=(
                'Looks good to me overall.\n\n'
                'Reviewed-by: Bob <bob@example.com>\n\n'
                'But please fix the typo in the comment.\n'
            )
        )
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Reviewed-by', 'Bob <bob@example.com>'),
        ]

    def test_reply_quoted_trailer_still_skipped(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body=(
                'On Mon, Alice wrote:\n'
                '> Signed-off-by: Alice <alice@example.com>\n\n'
                'Acked-by: Bob <bob@example.com>\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Acked-by']

    def test_reply_person_trailer_needs_an_address(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='Nice.\n\nAssisted-by: LLM\n\nBut fix the typo.\n')
        assert extract_trailers(msg) == []

    def test_reply_url_only_allowed_in_link_trailers(
        self, make_msg: MsgFactory
    ) -> None:
        msg = make_msg(
            body=(
                'See below.\n\n'
                'Link: https://lore.kernel.org/r/x@y\n'
                'Reviewed-by: https://example.com/not-a-person\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Link']

    def test_reply_patch_metadata_is_not_feedback(self, make_msg: MsgFactory) -> None:
        # Quoting a patch's own headers is not the same as offering them.
        msg = make_msg(
            body=(
                'Here is what I applied:\n\n'
                'From: Alice <alice@example.com>\n'
                'Subject: [PATCH] usb: fix it\n'
                'base-commit: deadbeef1234\n\n'
                'Tested-by: Bob <bob@example.com>\n'
            )
        )
        assert [t.name for t in extract_trailers(msg)] == ['Tested-by']


class TestInbodyAuthor:
    def test_inbody_from_overrides_header(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            from_addr=('Mailing List', 'list@vger.kernel.org'),
            body='From: Real Author <real@example.com>\n\nusb: fix it\n',
        )
        assert msg_get_inbody_author(msg) == ('Real Author', 'real@example.com')
        assert msg_get_author(msg) == ('Mailing List', 'list@vger.kernel.org')
        assert msg_get_author(msg, use_inbody=True) == (
            'Real Author',
            'real@example.com',
        )

    def test_no_inbody_from(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='usb: fix it\n\nSome commit message.\n')
        assert msg_get_inbody_author(msg) is None

    def test_commit_subject_is_not_a_header(self, make_msg: MsgFactory) -> None:
        # "usb: fix it" matches "Name: value" shape but is not a known
        # header, so scanning must stop rather than walk into the body.
        msg = make_msg(body='usb: fix it\n\nFrom: Not The Author <no@example.com>\n')
        assert msg_get_inbody_author(msg) is None

    def test_block_ends_at_blank_line(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            body='Subject: [PATCH] x\n\nBody text\nFrom: Late <late@example.com>\n'
        )
        assert msg_get_inbody_author(msg) is None

    def test_leading_blank_lines_are_allowed(self, make_msg: MsgFactory) -> None:
        msg = make_msg(body='\n\nFrom: Real <real@example.com>\n\nbody\n')
        assert msg_get_inbody_author(msg) == ('Real', 'real@example.com')


class TestGroupByThread:
    def test_splits_two_threads(self, make_msg: MsgFactory) -> None:
        a1 = make_msg(subject='A', msgid='a1@x', date=DATE1)
        a2 = make_msg(subject='Re: A', msgid='a2@x', in_reply_to='a1@x', date=DATE2)
        b1 = make_msg(subject='B', msgid='b1@x', date=DATE3)
        threads = group_by_thread([a2, b1, a1])
        assert [[m['Message-Id'] for m in t] for t in threads] == [
            ['<a1@x>', '<a2@x>'],
            ['<b1@x>'],
        ]

    def test_groups_via_references_when_root_is_missing(
        self, make_msg: MsgFactory
    ) -> None:
        # Two siblings whose parent is not in the bag still belong together.
        c1 = make_msg(msgid='c1@x', references=['root@x'], date=DATE1)
        c2 = make_msg(msgid='c2@x', references=['root@x'], date=DATE2)
        assert len(group_by_thread([c1, c2])) == 1

    def test_duplicate_msgids_are_collapsed(self, make_msg: MsgFactory) -> None:
        one = make_msg(msgid='dup@x', date=DATE1)
        two = make_msg(msgid='dup@x', date=DATE1)
        threads = group_by_thread([one, two])
        assert len(threads) == 1
        assert len(threads[0]) == 1

    def test_empty_input(self) -> None:
        assert group_by_thread([]) == []


class TestSummarizeThread:
    def test_rejects_empty_thread(self) -> None:
        with pytest.raises(ValueError, match='empty thread'):
            summarize_thread([])

    def test_plain_discussion(self, make_msg: MsgFactory) -> None:
        first = make_msg(
            subject='Question about USB',
            from_addr=('Alice', 'alice@example.com'),
            msgid='q1@x',
            date=DATE1,
        )
        reply = make_msg(
            subject='Re: Question about USB',
            from_addr=('Bob', 'bob@example.com'),
            msgid='q2@x',
            in_reply_to='q1@x',
            date=DATE2,
        )
        summary = summarize_thread([first, reply])
        assert summary.msgid == 'q1@x'
        assert summary.subject == 'Question about USB'
        assert summary.count == 2
        assert summary.is_patch_series is False
        assert summary.patch_count == 0
        assert summary.expected == 0
        assert summary.participants == (
            ('Alice', 'alice@example.com'),
            ('Bob', 'bob@example.com'),
        )

    def test_lone_patch_is_a_one_patch_series(self, make_msg: MsgFactory) -> None:
        msg = make_msg(subject='[PATCH] usb: fix it', msgid='p1@x', date=DATE1)
        summary = summarize_thread([msg])
        assert summary.is_patch_series is True
        assert summary.patch_count == 1
        assert summary.expected == 1
        assert summary.has_cover is False

    def test_complete_series_with_cover(self, make_msg: MsgFactory) -> None:
        msgs = [
            make_msg(subject='[PATCH v2 0/3] usb: a series', msgid='s0@x', date=DATE1)
        ]
        msgs += [
            make_msg(
                subject=f'[PATCH v2 {n}/3] usb: part {n}',
                msgid=f's{n}@x',
                in_reply_to='s0@x',
                date=DATE1,
            )
            for n in (1, 2, 3)
        ]
        summary = summarize_thread(msgs)
        assert summary.is_patch_series is True
        assert summary.has_cover is True
        assert summary.patch_count == 3
        assert summary.expected == 3
        assert summary.version == 2

    def test_incomplete_series_is_reported_not_judged(
        self, make_msg: MsgFactory
    ) -> None:
        msgs = [
            make_msg(subject=f'[PATCH {n}/7] x', msgid=f'i{n}@x', date=DATE1)
            for n in (1, 2)
        ]
        summary = summarize_thread(msgs)
        assert (summary.patch_count, summary.expected) == (2, 7)

    def test_replies_do_not_inflate_patch_count(self, make_msg: MsgFactory) -> None:
        patch = make_msg(subject='[PATCH 1/1] usb: fix it', msgid='r1@x', date=DATE1)
        reply = make_msg(
            subject='Re: [PATCH 1/1] usb: fix it',
            msgid='r2@x',
            in_reply_to='r1@x',
            date=DATE2,
            body='Reviewed-by: Bob <bob@example.com>\n',
        )
        summary = summarize_thread([patch, reply])
        assert summary.count == 2
        assert summary.patch_count == 1
        assert summary.trailer_names == {'reviewed-by'}

    def test_resent_patch_counts_once(self, make_msg: MsgFactory) -> None:
        first = make_msg(subject='[PATCH 1/2] x', msgid='d1@x', date=DATE1)
        again = make_msg(subject='[PATCH 1/2] x', msgid='d2@x', date=DATE2)
        other = make_msg(subject='[PATCH 2/2] y', msgid='d3@x', date=DATE3)
        assert summarize_thread([first, again, other]).patch_count == 2

    def test_timestamps_span_the_thread(self, make_msg: MsgFactory) -> None:
        first = make_msg(msgid='t1@x', date=DATE1)
        last = make_msg(msgid='t2@x', in_reply_to='t1@x', date=DATE3)
        summary = summarize_thread([last, first])
        assert summary.first is not None and summary.last is not None
        assert summary.first.hour == 10
        assert summary.last.hour == 12

    def test_undated_messages_do_not_crash(self, make_msg: MsgFactory) -> None:
        summary = summarize_thread([make_msg(msgid='u1@x')])
        assert summary.first is None
        assert summary.last is None

    def test_participants_deduped_and_ordered(self, make_msg: MsgFactory) -> None:
        one = make_msg(
            from_addr=('Alice', 'alice@example.com'), msgid='x1@x', date=DATE1
        )
        two = make_msg(from_addr=('Bob', 'bob@example.com'), msgid='x2@x', date=DATE2)
        three = make_msg(
            from_addr=('Alice', 'Alice@Example.com'), msgid='x3@x', date=DATE3
        )
        summary = summarize_thread([three, one, two])
        assert summary.participants == (
            ('Alice', 'alice@example.com'),
            ('Bob', 'bob@example.com'),
        )

    def test_participant_uses_inbody_from(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            subject='[PATCH] usb: fix it',
            from_addr=('Remailer', 'list@vger.kernel.org'),
            msgid='rm@x',
            date=DATE1,
            body='From: Real Author <real@example.com>\n\nusb: fix it\n',
        )
        assert summarize_thread([msg]).participants == (
            ('Real Author', 'real@example.com'),
        )

    def test_trailers_are_deduped_across_the_thread(self, make_msg: MsgFactory) -> None:
        patch = make_msg(
            subject='[PATCH] x',
            msgid='td1@x',
            date=DATE1,
            body='x\n\nSigned-off-by: Alice <alice@example.com>\n',
        )
        reply = make_msg(
            subject='Re: [PATCH] x',
            msgid='td2@x',
            in_reply_to='td1@x',
            date=DATE2,
            body='Signed-off-by: Alice <alice@example.com>\n'
            'Acked-by: Bob <bob@example.com>\n',
        )
        summary = summarize_thread([patch, reply])
        assert [(t.name, t.value) for t in summary.trailers] == [
            ('Signed-off-by', 'Alice <alice@example.com>'),
            ('Acked-by', 'Bob <bob@example.com>'),
        ]

    def test_prefixes_collected_in_order(self, make_msg: MsgFactory) -> None:
        msgs = [
            make_msg(subject='[RFC PATCH net-next 1/2] a', msgid='pf1@x', date=DATE1),
            make_msg(subject='[RFC PATCH net-next 2/2] b', msgid='pf2@x', date=DATE2),
        ]
        assert summarize_thread(msgs).prefixes == ('RFC', 'net-next')


class TestTrailerTypes:
    """b4 sorts trailers into people, utilities and everything else."""

    def _trailers(self, body: str, make_msg: MsgFactory) -> dict[str, Trailer]:
        patch = make_msg(
            subject='[PATCH 1/1] x',
            msgid='tt1@x',
            date=DATE1,
            body=body + PATCH_TAIL,
        )
        return {t.key: t for t in summarize_thread([patch]).trailers}

    def test_person_trailer_carries_the_address(self, make_msg: MsgFactory) -> None:
        found = self._trailers('Signed-off-by: Alice <alice@example.com>\n', make_msg)
        trailer = found['signed-off-by']
        assert trailer.type == 'person'
        assert trailer.is_person and not trailer.is_utility
        assert trailer.addr == ('Alice', 'alice@example.com')

    def test_stray_whitespace_in_address_is_normalised(
        self, make_msg: MsgFactory
    ) -> None:
        found = self._trailers(
            'Tested-by: Fuad Tabba < fuad.tabba@linux.dev>\n', make_msg
        )
        assert found['tested-by'].value == 'Fuad Tabba <fuad.tabba@linux.dev>'

    def test_mailto_wrapper_is_stripped(self, make_msg: MsgFactory) -> None:
        # Some clients round-trip through HTML and mangle the address.
        found = self._trailers(
            'Reviewed-by: Bob <bob@example.com> <mailto:bob@example.com>\n',
            make_msg,
        )
        assert found['reviewed-by'].value == 'Bob <bob@example.com>'
        assert found['reviewed-by'].addr == ('Bob', 'bob@example.com')

    def test_known_utility_names_are_utilities(self, make_msg: MsgFactory) -> None:
        found = self._trailers(
            'Fixes: deadbeef1234 ("thing: do it")\n'
            'Link: https://lore.kernel.org/r/foo@bar\n',
            make_msg,
        )
        assert found['fixes'].type == 'utility'
        assert found['link'].is_utility
        assert found['link'].addr is None

    def test_any_url_value_is_a_utility(self, make_msg: MsgFactory) -> None:
        found = self._trailers('Suggested-by: https://example.com/x\n', make_msg)
        assert found['suggested-by'].type == 'utility'

    def test_addressless_trailer_stays_unknown(self, make_msg: MsgFactory) -> None:
        found = self._trailers('Suggested-by: Somebody Nice\n', make_msg)
        trailer = found['suggested-by']
        assert trailer.type == 'unknown'
        assert not trailer.is_person and not trailer.is_utility
        assert trailer.addr is None

    def test_summary_splits_people_from_utilities(self, make_msg: MsgFactory) -> None:
        patch = make_msg(
            subject='[PATCH 1/1] x',
            msgid='ts1@x',
            date=DATE1,
            body='Fixes: deadbeef1234 ("thing: do it")\n'
            'Signed-off-by: Alice <alice@example.com>\n'
            'Suggested-by: Somebody Nice\n' + PATCH_TAIL,
        )
        summary = summarize_thread([patch])
        assert [t.name for t in summary.person_trailers] == ['Signed-off-by']
        assert [t.name for t in summary.utility_trailers] == ['Fixes']
        # The unknown one is still in the full list, just in neither bucket.
        assert len(summary.trailers) == 3


class TestMsgids:
    def test_msgids_are_in_date_order(self, make_msg: MsgFactory) -> None:
        root = make_msg(subject='[PATCH 1/1] x', msgid='m1@x', date=DATE1)
        late = make_msg(
            subject='Re: [PATCH 1/1] x',
            msgid='m3@x',
            in_reply_to='m1@x',
            date=DATE3,
        )
        early = make_msg(
            subject='Re: [PATCH 1/1] x',
            msgid='m2@x',
            in_reply_to='m1@x',
            date=DATE2,
        )
        summary = summarize_thread([late, root, early])
        assert summary.msgids == ('m1@x', 'm2@x', 'm3@x')
        assert len(summary.msgids) == summary.count
        assert summary.msgids[0] == summary.msgid

    def test_root_leads_even_when_it_is_not_the_oldest(
        self, make_msg: MsgFactory
    ) -> None:
        # A cover letter can be stamped a second after its first patch.
        cover = make_msg(subject='[PATCH 0/1] x', msgid='c0@x', date=DATE2)
        patch = make_msg(
            subject='[PATCH 1/1] x', msgid='c1@x', in_reply_to='c0@x', date=DATE1
        )
        summary = summarize_thread([cover, patch])
        assert summary.msgids == ('c0@x', 'c1@x')
        assert summary.msgids[0] == summary.msgid

    def test_series_sent_in_the_same_second_keeps_its_order(
        self, make_msg: MsgFactory
    ) -> None:
        # git send-email stamps a whole series within one second, so the
        # n/m counter is what actually orders it.
        msgs = [
            make_msg(subject='[PATCH 3/3] c', msgid='s3@x', date=DATE1),
            make_msg(subject='[PATCH 1/3] a', msgid='s1@x', date=DATE1),
            make_msg(subject='[PATCH 2/3] b', msgid='s2@x', date=DATE1),
        ]
        assert summarize_thread(msgs).msgids == ('s1@x', 's2@x', 's3@x')


class TestSerialization:
    def test_to_dict_is_json_safe(self, make_msg: MsgFactory) -> None:
        patch = make_msg(subject='[PATCH v2 1/1] x', msgid='j1@x', date=DATE1)
        reply = make_msg(
            subject='Re: [PATCH v2 1/1] x',
            from_addr=('Bob', 'bob@example.com'),
            msgid='j2@x',
            in_reply_to='j1@x',
            date=DATE2,
            body='Reviewed-by: Bob <bob@example.com>\n',
        )
        payload = summarize_thread([patch, reply]).to_dict()
        # Must survive a round trip through JSON unchanged.
        assert json.loads(json.dumps(payload)) == payload
        assert payload['trailers'] == [
            {
                'name': 'Reviewed-by',
                'value': 'Bob <bob@example.com>',
                'type': 'person',
                'email': 'bob@example.com',
            }
        ]
        assert payload['msgids'] == ['j1@x', 'j2@x']
        assert payload['participants'][0] == {
            'name': 'Test User',
            'email': 'test@example.com',
        }
        assert payload['first'].startswith('2026-09-15T10:00:00')

    def test_key_set_is_fixed(self, make_msg: MsgFactory) -> None:
        # Every key is always present, so consumers never probe.
        plain = summarize_thread([make_msg(msgid='k1@x')]).to_dict()
        patched = summarize_thread(
            [make_msg(subject='[PATCH 1/1] x', msgid='k2@x', date=DATE1)]
        ).to_dict()
        assert plain.keys() == patched.keys()
        assert set(plain) == {
            f.name for f in ThreadSummary.__dataclass_fields__.values()
        }


class TestSummarizeThreads:
    def test_end_to_end_query_shape(self, make_msg: MsgFactory) -> None:
        """The MCP query: complete series, reviewed, from a mixed bag."""
        reviewed = [
            make_msg(subject='[PATCH 1/2] good', msgid='g1@x', date=DATE1),
            make_msg(
                subject='[PATCH 2/2] good',
                msgid='g2@x',
                in_reply_to='g1@x',
                date=DATE1,
            ),
            make_msg(
                subject='Re: [PATCH 1/2] good',
                from_addr=('Bob', 'bob@example.com'),
                msgid='g3@x',
                in_reply_to='g1@x',
                date=DATE2,
                body='Reviewed-by: Bob <bob@example.com>\n',
            ),
        ]
        incomplete = [make_msg(subject='[PATCH 1/5] partial', msgid='h1@x', date=DATE1)]
        chatter = [make_msg(subject='Just chatting', msgid='n1@x', date=DATE3)]

        summaries = summarize_threads(reviewed + incomplete + chatter)
        assert len(summaries) == 3

        wanted = {'reviewed-by', 'acked-by'}
        hits = [
            s
            for s in summaries
            if s.is_patch_series
            and s.patch_count == s.expected
            and bool(wanted & s.trailer_names)
        ]
        assert [s.msgid for s in hits] == ['g1@x']
