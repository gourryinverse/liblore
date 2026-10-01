# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for liblore.series."""

from __future__ import annotations

import json
from email.message import EmailMessage

import pytest
from conftest import MsgFactory

from liblore.series import (
    SCHEMA_VERSION,
    Identity,
    ThreadSummary,
    Trailer,
    email_matches,
    extract_trailers,
    group_by_thread,
    name_matches,
    parse_patch_subject,
    summarize_message,
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
        parsed = parse_patch_subject('Отв: [PATCH 1/2] usb: fix the thing')
        assert parsed.is_reply is True
        assert parsed.is_patch is True
        assert parsed.counter == 1

    def test_subsystem_is_not_a_reply_marker(self) -> None:
        # "mm:" looks like a two-letter reply marker, but it is the
        # subsystem; with no bracket after it, it stays in the subject.
        parsed = parse_patch_subject('mm: fix the widget')
        assert parsed.is_reply is False
        assert parsed.title == 'mm: fix the widget'
        parsed = parse_patch_subject('Re: net: fix the socket')
        assert parsed.is_reply is True
        assert parsed.title == 'net: fix the socket'

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
            'Reviewed-by: Bob <bob@example.com>\n' + PATCH_TAIL
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
        msg = make_msg(
            from_addr=('Bob', 'bob@example.com'),
            body='Thanks!\n\n    Acked-by: Bob <bob@example.com>\n',
        )
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
            'Signed-off-by: Alice <alice@example.com>\n' + PATCH_TAIL
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
        msg = make_msg(
            from_addr=('Bob', 'bob@example.com'),
            body='Reviewed-By: Bob <bob@example.com>\n',
        )
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
            + PATCH_TAIL
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
            + PATCH_TAIL
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
            + PATCH_TAIL
        )
        assert [t.name for t in extract_trailers(msg)] == ['Acked-by']

    def test_separator_without_a_diff_is_not_a_basement(
        self, make_msg: MsgFactory
    ) -> None:
        # A bare "---" in a free-form reply is just a visual separator, so
        # trailers below it still count.
        msg = make_msg(
            from_addr=('Bob', 'bob@example.com'),
            body='Looks good to me.\n---\nReviewed-by: Bob <bob@example.com>\n',
        )
        assert [t.name for t in extract_trailers(msg)] == ['Reviewed-by']

    def test_signature_block_is_ignored(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            from_addr=('Alice', 'alice@example.com'),
            body=(
                'usb: fix it\n\n'
                'Signed-off-by: Alice <alice@example.com>\n'
                '-- \n'
                'Cc: not-a-real-trailer@example.com\n'
            ),
        )
        assert [t.name for t in extract_trailers(msg)] == ['Signed-off-by']

    def test_reply_trailer_mid_body_is_found(self, make_msg: MsgFactory) -> None:
        # Reviewers put their tag wherever they like, so a reply is
        # scanned whole rather than only in its last paragraph.
        msg = make_msg(
            from_addr=('Bob', 'bob@example.com'),
            body=(
                'Looks good to me overall.\n\n'
                'Reviewed-by: Bob <bob@example.com>\n\n'
                'But please fix the typo in the comment.\n'
            ),
        )
        assert [(t.name, t.value) for t in extract_trailers(msg)] == [
            ('Reviewed-by', 'Bob <bob@example.com>'),
        ]

    def test_reply_quoted_trailer_still_skipped(self, make_msg: MsgFactory) -> None:
        # The quoted tag is Bob's own, so only the quoting excludes it.
        msg = make_msg(
            from_addr=('Bob', 'bob@example.com'),
            body=(
                'On Mon, Alice wrote:\n'
                '> Reviewed-by: Bob <bob@example.com>\n\n'
                'Acked-by: Bob <bob@example.com>\n'
            ),
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
            from_addr=('Bob', 'bob@example.com'),
            body=(
                'Here is what I applied:\n\n'
                'From: Alice <alice@example.com>\n'
                'Subject: [PATCH] usb: fix it\n'
                'base-commit: deadbeef1234\n\n'
                'Tested-by: Bob <bob@example.com>\n'
            ),
        )
        assert [t.name for t in extract_trailers(msg)] == ['Tested-by']


class TestSelfAttestedTrailers:
    """A reply's person trailer counts only when the sender signs it."""

    def _names(
        self, sender: tuple[str, str], body: str, make_msg: MsgFactory
    ) -> list[str]:
        return [t.name for t in extract_trailers(make_msg(from_addr=sender, body=body))]

    def test_tagging_somebody_else_is_dropped(self, make_msg: MsgFactory) -> None:
        assert (
            self._names(
                ('Bob', 'bob@example.com'),
                'Alice liked it too:\n\nReviewed-by: Alice <alice@example.com>\n',
                make_msg,
            )
            == []
        )

    def test_an_affiliation_does_not_break_attestation(
        self, make_msg: MsgFactory
    ) -> None:
        # The name carries the employer and the address is unrelated to
        # the one in the tag, so only the name can rescue this.
        assert self._names(
            ('Bob Reviewer (Arm)', 'bob@example.arm.com'),
            'lgtm\n\nReviewed-by: Bob Reviewer <bob@unrelated.example.org>\n',
            make_msg,
        ) == ['Reviewed-by']

    def test_an_inbody_from_does_not_name_the_sender(
        self, make_msg: MsgFactory
    ) -> None:
        # Who sent the mail is a header question.  An in-body From: is
        # body text the sender wrote, so letting it answer would let
        # anybody tag on anybody's behalf.
        assert (
            self._names(
                ('Mallory', 'mallory@example.net'),
                'From: Ann Submitter <ann@example.com>\n\nlgtm\n\n'
                'Reviewed-by: Ann Submitter <ann@example.com>\n',
                make_msg,
            )
            == []
        )

    def test_pasted_snippet_is_not_a_trailer(self, make_msg: MsgFactory) -> None:
        # A device tree snippet quoted in a reply reads as a trailer
        # whose value happens to look like an address.
        assert (
            self._names(
                ('David', 'david@example.com'),
                'How about this:\n\n\ti2c0: i2c@... {\n\t\t#size-cells = <0>;\n',
                make_msg,
            )
            == []
        )

    def test_extended_local_part_still_matches(self, make_msg: MsgFactory) -> None:
        assert self._names(
            ('Bob', 'bob+kernel@example.com'),
            'Acked-by: Bob <bob@example.com>\n',
            make_msg,
        ) == ['Acked-by']

    def test_subdomain_still_matches(self, make_msg: MsgFactory) -> None:
        assert self._names(
            ('Bob', 'bob@example.com'),
            'Acked-by: Bob <bob@linux.example.com>\n',
            make_msg,
        ) == ['Acked-by']

    def test_name_match_rescues_a_different_address(self, make_msg: MsgFactory) -> None:
        # Maintainers often post from one address and tag from another.
        assert self._names(
            ('Mikael Renberg', 'mrenberg@example.com'),
            'Acked-by: Mikael Renberg <mikael@oss.example.org>\n',
            make_msg,
        ) == ['Acked-by']

    def test_comma_reversed_name_matches(self, make_msg: MsgFactory) -> None:
        assert self._names(
            ('Okonkwo, Amara', 'aokonkwo@example.com'),
            'Reviewed-by: Amara Okonkwo <amara@other.example.org>\n',
            make_msg,
        ) == ['Reviewed-by']

    def test_comma_reversed_name_matches_either_way_round(
        self, make_msg: MsgFactory
    ) -> None:
        # Name matching is symmetric, so it does not matter which of the
        # two spellings is the comma-separated one.
        assert self._names(
            ('Amara Okonkwo', 'aokonkwo@example.com'),
            'Reviewed-by: Okonkwo, Amara <amara@other.example.org>\n',
            make_msg,
        ) == ['Reviewed-by']

    def test_utility_trailers_need_no_attestation(self, make_msg: MsgFactory) -> None:
        # There is no identity in a Fixes: or a Link:, so nothing to match.
        assert self._names(
            ('Bob', 'bob@example.com'),
            'This one:\n\nFixes: deadbeef1234 ("usb: break it")\n',
            make_msg,
        ) == ['Fixes']

    def test_a_patch_is_not_subject_to_the_rule(self, make_msg: MsgFactory) -> None:
        # A patch collects everybody's tags; the sender signs for them.
        assert self._names(
            ('Alice', 'alice@example.com'),
            'usb: fix it\n\nSigned-off-by: Alice <alice@example.com>\n'
            'Reviewed-by: Bob <bob@example.com>\n' + PATCH_TAIL,
            make_msg,
        ) == ['Signed-off-by', 'Reviewed-by']


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

    def test_summary_author_and_sender_may_differ(self, make_msg: MsgFactory) -> None:
        # A patch relayed by a co-maintainer: git am would record the
        # in-body author, while the sender is whoever posted it.  Both
        # answers are right, to two different questions.
        msg = make_msg(
            subject='[PATCH 1/1] usb: fix it',
            from_addr=('Relaying Maintainer', 'maint@example.com'),
            body='From: Ann Submitter <ann@example.com>\n\nusb: fix it\n\n'
            'Signed-off-by: Ann Submitter <ann@example.com>\n' + PATCH_TAIL,
        )
        assert summarize_message(msg).author == ('Ann Submitter', 'ann@example.com')
        assert msg_get_author(msg) == ('Relaying Maintainer', 'maint@example.com')

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


class TestMungedSender:
    """Lists that rewrite From for DMARC keep the sender elsewhere."""

    def _munged(self, make_msg: MsgFactory, body: str) -> EmailMessage:
        msg = make_msg(
            subject='Re: [PATCH 1/1] usb: fix it',
            from_addr=('Bob Reviewer via list', 'list@lists.example.org'),
            body=body,
        )
        msg['X-Original-From'] = 'Bob Reviewer <bob@example.com>'
        return msg

    def test_original_from_names_the_sender(self, make_msg: MsgFactory) -> None:
        msg = self._munged(make_msg, 'Looks fine.\n')
        assert msg_get_author(msg) == ('Bob Reviewer', 'bob@example.com')
        assert msg_get_author(msg, use_inbody=True) == (
            'Bob Reviewer',
            'bob@example.com',
        )

    def test_a_munged_reply_keeps_its_trailer(self, make_msg: MsgFactory) -> None:
        # Without X-Original-From the sender looks like the list itself,
        # and a perfectly good tag would be dropped as unattested.
        msg = self._munged(
            make_msg,
            'Looks fine.\n\nReviewed-by: Bob Reviewer <bob@example.com>\n',
        )
        assert [t.name for t in extract_trailers(msg)] == ['Reviewed-by']


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
            from_addr=('Bob', 'bob@example.com'),
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
            body='x\n\nSigned-off-by: Alice <alice@example.com>\n' + PATCH_TAIL,
        )
        replies = [
            make_msg(
                subject='Re: [PATCH] x',
                from_addr=('Bob', 'bob@example.com'),
                msgid=f'td{n}@x',
                in_reply_to='td1@x',
                date=DATE2,
                body='Acked-by: Bob <bob@example.com>\n',
            )
            for n in (2, 3)
        ]
        summary = summarize_thread([patch, *replies])
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

    def test_msgids_run_parallel_to_messages(self, make_msg: MsgFactory) -> None:
        root = make_msg(subject='[PATCH 1/1] x', msgid='p1@x', date=DATE1)
        reply = make_msg(
            subject='Re: [PATCH 1/1] x',
            msgid='p2@x',
            in_reply_to='p1@x',
            date=DATE2,
        )
        summary = summarize_thread([reply, root])
        assert summary.msgids == tuple(m.msgid for m in summary.messages)

    def test_a_message_with_no_msgid_keeps_its_place(
        self, make_msg: MsgFactory
    ) -> None:
        # Every message counts, so msgids stays the same length as
        # messages and holds an empty string where the id is missing.
        root = make_msg(subject='[PATCH 1/1] x', msgid='n1@x', date=DATE1)
        reply = make_msg(
            subject='Re: [PATCH 1/1] x',
            msgid='n2@x',
            in_reply_to='n1@x',
            date=DATE2,
        )
        del reply['Message-Id']
        summary = summarize_thread([root, reply])
        assert summary.count == 2
        assert summary.msgids == ('n1@x', '')
        assert summary.msgids == tuple(m.msgid for m in summary.messages)

    def test_a_root_with_no_msgid_still_leads(self, make_msg: MsgFactory) -> None:
        root = make_msg(subject='[PATCH 1/1] x', msgid='r1@x', date=DATE1)
        reply = make_msg(
            subject='Re: [PATCH 1/1] x',
            msgid='r2@x',
            in_reply_to='r1@x',
            date=DATE2,
        )
        del root['Message-Id']
        summary = summarize_thread([root, reply])
        assert summary.msgid == ''
        assert summary.msgids == ('', 'r2@x')
        assert summary.messages[0].subject == '[PATCH 1/1] x'
        assert summary.msgids == tuple(m.msgid for m in summary.messages)


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
                'msgid': 'j2@x',
                'date': '2026-09-15T11:00:00+00:00',
            }
        ]
        assert payload['msgids'] == ['j1@x', 'j2@x']
        assert payload['schema'] == SCHEMA_VERSION
        assert payload['author'] == {
            'name': 'Test User',
            'email': 'test@example.com',
        }
        assert [m['msgid'] for m in payload['messages']] == ['j1@x', 'j2@x']
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
        # Every field is serialised, plus the schema version, which is a
        # property of the shape rather than of any one summary.
        assert set(plain) == {
            f.name for f in ThreadSummary.__dataclass_fields__.values()
        } | {'schema'}

    def test_message_key_set_is_fixed(self, make_msg: MsgFactory) -> None:
        plain = summarize_message(make_msg(msgid='km1@x'))
        patched = summarize_message(
            make_msg(subject='[PATCH v2 2/3] x', msgid='km2@x', date=DATE1)
        )
        assert plain.to_dict().keys() == patched.to_dict().keys()
        assert json.loads(json.dumps(patched.to_dict())) == patched.to_dict()


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


ME = ('Me Maintainer', 'me@example.com')
SASHIKO = ('Sashiko Tanaka', 'sashiko@example.com')
ANN = ('Ann Submitter', 'ann@example.com')


class TestMessageSummaries:
    def test_messages_match_msgids_root_first(self, make_msg: MsgFactory) -> None:
        # The cover letter is stamped a second after patch 1/2, so date
        # order and thread order genuinely differ here.
        cover = make_msg(subject='[PATCH 0/2] set', msgid='c0@x', date=DATE2)
        one = make_msg(
            subject='[PATCH 1/2] a', msgid='c1@x', in_reply_to='c0@x', date=DATE1
        )
        summary = summarize_thread([one, cover])
        assert summary.msgids == ('c0@x', 'c1@x')
        assert [m.msgid for m in summary.messages] == list(summary.msgids)
        assert summary.messages[0].patch.is_cover is True

    def test_message_carries_author_date_and_patch(self, make_msg: MsgFactory) -> None:
        msg = make_msg(
            subject='[PATCH v3 2/5] thing',
            from_addr=ANN,
            msgid='m1@x',
            date=DATE1,
            body='Body\n' + PATCH_TAIL,
        )
        summary = summarize_message(msg)
        assert summary.author == ANN
        assert summary.date is not None
        assert summary.has_diff is True
        assert summary.is_submission is True
        assert (summary.patch.version, summary.patch.counter) == (3, 2)

    def test_reply_carrying_brackets_is_not_a_submission(
        self, make_msg: MsgFactory
    ) -> None:
        reply = make_msg(subject='Re: [PATCH 1/1] a', msgid='r1@x', date=DATE2)
        assert summarize_message(reply).is_submission is False

    def test_trailer_records_the_message_that_offered_it(
        self, make_msg: MsgFactory
    ) -> None:
        patch = make_msg(
            subject='[PATCH 1/1] a', from_addr=ANN, msgid='p1@x', date=DATE1
        )
        review = make_msg(
            subject='Re: [PATCH 1/1] a',
            from_addr=SASHIKO,
            msgid='p2@x',
            in_reply_to='p1@x',
            date=DATE2,
            body='Reviewed-by: Sashiko Tanaka <sashiko@example.com>\n',
        )
        summary = summarize_thread([patch, review])
        (trailer,) = [t for t in summary.trailers if t.key == 'reviewed-by']
        assert trailer.msgid == 'p2@x'
        assert trailer.date is not None
        # And the same trailer is on the message that made it.
        offered = summary.messages_by('sashiko@example.com')
        assert [t.key for t in offered[0].trailers] == ['reviewed-by']


class TestAuthorAndRepliers:
    def test_author_is_the_thread_root(self, make_msg: MsgFactory) -> None:
        patch = make_msg(
            subject='[PATCH 1/1] a', from_addr=ANN, msgid='a1@x', date=DATE1
        )
        reply = make_msg(
            subject='Re: [PATCH 1/1] a',
            from_addr=SASHIKO,
            msgid='a2@x',
            in_reply_to='a1@x',
            date=DATE2,
        )
        summary = summarize_thread([patch, reply])
        assert summary.author == ANN
        assert summary.repliers == (SASHIKO,)

    def test_author_answering_their_own_thread_is_not_a_replier(
        self, make_msg: MsgFactory
    ) -> None:
        # The whole point of keeping author apart from participants: Ann
        # is in participants twice over and still has replied to nobody.
        patch = make_msg(
            subject='[PATCH 1/1] a', from_addr=ANN, msgid='b1@x', date=DATE1
        )
        followup = make_msg(
            subject='Re: [PATCH 1/1] a',
            from_addr=ANN,
            msgid='b2@x',
            in_reply_to='b1@x',
            date=DATE2,
        )
        summary = summarize_thread([patch, followup])
        assert summary.participants == (ANN,)
        assert summary.repliers == ()

    def test_messages_by_matches_addresses(self, make_msg: MsgFactory) -> None:
        patch = make_msg(
            subject='[PATCH 1/1] a', from_addr=ANN, msgid='d1@x', date=DATE1
        )
        reply = make_msg(
            subject='Re: [PATCH 1/1] a',
            from_addr=('Ann Submitter', 'ANN@example.com'),
            msgid='d2@x',
            in_reply_to='d1@x',
            date=DATE2,
        )
        summary = summarize_thread([patch, reply])
        assert len(summary.messages_by('ann@EXAMPLE.com')) == 2
        assert summary.messages_by('ann@elsewhere.org') == ()


def _reviewed_by_awaiting_reply(
    summaries: list[ThreadSummary], reviewer: Identity, me: Identity
) -> list[str]:
    """The example query, written the way a caller would write it.

    "Threads reviewed by *reviewer* that have not yet had a reply from
    *me*" -- meaning I have sent nothing in the thread since the review
    landed.  Every judgement here is the caller's; the module only
    supplies the facts it leans on, and answers both "who is this?"
    questions the same way.
    """
    hits: list[str] = []
    for summary in summaries:
        reviews = [
            trailer.date
            for trailer in summary.trailers_by(reviewer)
            if trailer.key == 'reviewed-by' and trailer.date is not None
        ]
        if not reviews:
            continue
        latest = max(reviews)
        mine = summary.messages_by(me)
        if any(m.date is not None and m.date > latest for m in mine):
            continue
        hits.append(summary.msgid)
    return hits


class TestReviewedAwaitingReplyQuery:
    def _corpus(self, make_msg: MsgFactory) -> list[ThreadSummary]:
        def review(msgid: str, parent: str) -> EmailMessage:
            return make_msg(
                subject=f'Re: [PATCH 1/1] {parent}',
                from_addr=SASHIKO,
                msgid=msgid,
                in_reply_to=parent,
                date=DATE2,
                body='Reviewed-by: Sashiko Tanaka <sashiko@example.com>\n',
            )

        msgs = [
            # Reviewed, no reply from me -- the answer.
            make_msg(subject='[PATCH 1/1] t1', from_addr=ANN, msgid='t1', date=DATE1),
            review('t1r', 't1'),
            # Reviewed, and I answered afterwards.
            make_msg(subject='[PATCH 1/1] t2', from_addr=ANN, msgid='t2', date=DATE1),
            review('t2r', 't2'),
            make_msg(
                subject='Re: [PATCH 1/1] t2',
                from_addr=ME,
                msgid='t2m',
                in_reply_to='t2',
                date=DATE3,
                body='Applied, thanks.\n',
            ),
            # My own series, reviewed, not answered by me since.
            make_msg(subject='[PATCH 1/1] t3', from_addr=ME, msgid='t3', date=DATE1),
            review('t3r', 't3'),
            # Reviewed by somebody else entirely.
            make_msg(subject='[PATCH 1/1] t4', from_addr=ANN, msgid='t4', date=DATE1),
            make_msg(
                subject='Re: [PATCH 1/1] t4',
                from_addr=('Bob Other', 'bob@example.com'),
                msgid='t4r',
                in_reply_to='t4',
                date=DATE2,
                body='Reviewed-by: Bob Other <bob@example.com>\n',
            ),
        ]
        return summarize_threads(msgs)

    def test_finds_only_the_unanswered_reviewed_threads(
        self, make_msg: MsgFactory
    ) -> None:
        hits = _reviewed_by_awaiting_reply(
            self._corpus(make_msg), Identity.from_addr(SASHIKO), Identity.from_addr(ME)
        )
        # t2 is answered; t4 is not Sashiko's.  t3 is mine and I have
        # still not replied to the review, which is why being in
        # participants cannot stand in for having replied.
        assert sorted(hits) == ['t1', 't3']

    def test_a_reply_before_the_review_does_not_count(
        self, make_msg: MsgFactory
    ) -> None:
        msgs = [
            make_msg(subject='[PATCH 1/1] e1', from_addr=ANN, msgid='e1', date=DATE1),
            make_msg(
                subject='Re: [PATCH 1/1] e1',
                from_addr=ME,
                msgid='e1m',
                in_reply_to='e1',
                date=DATE2,
                body='Looks reasonable.\n',
            ),
            make_msg(
                subject='Re: [PATCH 1/1] e1',
                from_addr=SASHIKO,
                msgid='e1r',
                in_reply_to='e1',
                date=DATE3,
                body='Reviewed-by: Sashiko Tanaka <sashiko@example.com>\n',
            ),
        ]
        hits = _reviewed_by_awaiting_reply(
            summarize_threads(msgs), Identity.from_addr(SASHIKO), Identity.from_addr(ME)
        )
        assert hits == ['e1']


class TestEmailMatches:
    def test_identical_and_case_insensitive(self) -> None:
        assert email_matches('Bob@Example.COM', 'bob@example.com') is True

    def test_extended_local_part(self) -> None:
        assert email_matches('foo+kernel@example.com', 'foo@example.com') is True

    def test_one_level_of_subdomain(self) -> None:
        assert email_matches('foo@linux.intel.com', 'foo@intel.com') is True

    def test_two_levels_is_too_far(self) -> None:
        assert email_matches('foo@a.b.intel.com', 'foo@intel.com') is False

    def test_different_local_parts_never_match(self) -> None:
        assert email_matches('foo@intel.com', 'bar@intel.com') is False

    def test_unrelated_domains_do_not_match(self) -> None:
        assert email_matches('foo@example.com', 'foo@example.org') is False

    def test_a_non_address_does_not_crash(self) -> None:
        assert email_matches('not-an-address', 'foo@example.com') is False


class TestNameMatches:
    def test_exact_ignoring_case_and_space(self) -> None:
        assert name_matches('  Ann Submitter ', 'ann submitter') is True

    def test_comma_form_matches_either_direction(self) -> None:
        straight, comma = 'Amara Okonkwo', 'Okonkwo, Amara'
        assert name_matches(straight, comma) is True
        assert name_matches(comma, straight) is True

    def test_empty_matches_nothing(self) -> None:
        assert name_matches('', '') is False
        assert name_matches('', 'Ann Submitter') is False

    def test_different_people_do_not_match(self) -> None:
        assert name_matches('Ann Submitter', 'Bob Other') is False

    def test_comma_form_matches_whole_words_only(self) -> None:
        # A shorter name that merely spells the start of a longer one is
        # a different person, however the comma falls.
        assert name_matches('Alexander Smithson', 'Smith, Alex') is False
        assert name_matches('Smith, Alex', 'Alexander Smithson') is False
        assert name_matches('Bob Ngyuen', 'Ng, Bo') is False

    def test_a_middle_name_does_not_stop_the_match(self) -> None:
        assert name_matches('Amara C. Okonkwo', 'Okonkwo, Amara') is True

    def test_an_aside_in_parentheses_is_ignored(self) -> None:
        # Kernel senders often carry their employer in the From line.
        assert name_matches('Bob Reviewer (Arm)', 'Bob Reviewer') is True
        assert name_matches('Bob Reviewer', 'Bob Reviewer (Arm)') is True
        assert name_matches('Reviewer, Bob (Arm)', 'Bob Reviewer') is True
        assert name_matches('Bob Reviewer (Arm)', 'Alice Other') is False

    def test_a_name_that_is_only_an_aside_matches_nothing(self) -> None:
        assert name_matches('(Arm)', 'Bob Reviewer') is False
        assert name_matches('(Arm)', '(Arm)') is False


class TestIdentity:
    def test_matches_a_bare_address(self) -> None:
        person = Identity(addrs=('ann@example.com',))
        assert person.matches('ANN@example.com') is True
        assert person.matches('bob@example.com') is False

    def test_a_bare_address_never_matches_on_name(self) -> None:
        # A string is an address, so a name cannot sneak in through it.
        person = Identity(names=('Ann Submitter',))
        assert person.matches('Ann Submitter') is False

    def test_matches_a_pair_on_either_half(self) -> None:
        person = Identity(addrs=('ann@example.com',), names=('Ann Submitter',))
        assert person.matches(('Someone Else', 'ann@example.com')) is True
        assert person.matches(('Ann Submitter', 'ann@elsewhere.org')) is True
        assert person.matches(('Bob Other', 'bob@example.com')) is False

    def test_spans_several_unrelated_addresses(self) -> None:
        # The whole point: only a caller's directory knows these two
        # addresses are one human, and this is where it says so.
        person = Identity(addrs=('ann@example.com', 'ann@corp.invalid'))
        assert person.matches('ann@corp.invalid') is True

    def test_matches_another_identity(self) -> None:
        one = Identity(addrs=('ann@example.com',))
        two = Identity(addrs=('ann+lists@example.com',))
        assert one.matches(two) is True
        assert one.matches(Identity(addrs=('bob@example.com',))) is False

    def test_from_addr_drops_empty_halves(self) -> None:
        person = Identity.from_addr(('', 'ann@example.com'))
        assert person.names == ()
        assert person.addrs == ('ann@example.com',)

    def test_to_dict_is_json_safe(self) -> None:
        person = Identity(addrs=('ann@example.com',), names=('Ann Submitter',))
        payload = person.to_dict()
        assert json.loads(json.dumps(payload)) == payload


class TestIdentityOnThreads:
    def _thread(self, make_msg: MsgFactory) -> ThreadSummary:
        patch = make_msg(
            subject='[PATCH 1/1] a', from_addr=ANN, msgid='i1@x', date=DATE1
        )
        review = make_msg(
            subject='Re: [PATCH 1/1] a',
            from_addr=SASHIKO,
            msgid='i2@x',
            in_reply_to='i1@x',
            date=DATE2,
            body='Reviewed-by: Sashiko Tanaka <sashiko@example.com>\n',
        )
        # Same person as ANN, writing from an address a directory would
        # have to know about -- but under the same display name.
        again = make_msg(
            subject='Re: [PATCH 1/1] a',
            from_addr=('Ann Submitter', 'ann@corp.invalid'),
            msgid='i3@x',
            in_reply_to='i1@x',
            date=DATE3,
        )
        return summarize_thread([patch, review, again])

    def test_messages_by_accepts_an_identity(self, make_msg: MsgFactory) -> None:
        summary = self._thread(make_msg)
        assert len(summary.messages_by('ann@example.com')) == 1
        both = Identity(addrs=('ann@example.com', 'ann@corp.invalid'))
        assert len(summary.messages_by(both)) == 2

    def test_repliers_drops_the_author_under_another_address(
        self, make_msg: MsgFactory
    ) -> None:
        summary = self._thread(make_msg)
        # Ann spoke twice from two addresses; both are participants, and
        # neither counts as somebody having replied to her.
        assert len(summary.participants) == 3
        assert summary.repliers == (SASHIKO,)

    def test_trailers_by_attributes_to_the_named_person(
        self, make_msg: MsgFactory
    ) -> None:
        summary = self._thread(make_msg)
        assert [t.key for t in summary.trailers_by('sashiko@example.com')] == [
            'reviewed-by'
        ]
        assert summary.trailers_by('ann@example.com') == ()

    def test_trailers_by_matches_a_name_when_the_address_differs(
        self, make_msg: MsgFactory
    ) -> None:
        summary = self._thread(make_msg)
        by_name = Identity(names=('Sashiko Tanaka',))
        assert [t.key for t in summary.trailers_by(by_name)] == ['reviewed-by']
