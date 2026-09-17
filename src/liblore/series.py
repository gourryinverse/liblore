# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2025-2026 The Linux Foundation
"""Descriptive summaries of mail threads and patch series.

This module answers *what is in this thread* and nothing more.  It
reports facts -- how many messages, when they arrived, who took part,
what the ``[PATCH]`` brackets said, which trailers appeared -- and
leaves every judgement to the caller.  Deciding whether a series is
complete, whether it has been applied, or what to do about it is
deliberately somebody else's job.

Everything here is pure: no network, no git, no subprocess.  Feed it
messages, get back frozen dataclasses that serialise cleanly via
:meth:`ThreadSummary.to_dict`.

Summaries nest: a :class:`ThreadSummary` carries a
:class:`MessageSummary` per message, and each of those carries the
trailers that message offered.  Aggregate questions ("who reviewed
this?") are answered from the thread's own fields; ordered ones ("did
my reply arrive after that review?") need the per-message records,
which is why both are reported and not just the aggregates.
"""

from __future__ import annotations

import datetime
import email.utils
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from liblore.utils import (
    body_has_diff,
    clean_header,
    find_trailers,
    format_addrs,
    get_clean_msgid,
    msg_get_author,
    msg_get_date,
    msg_get_payload,
    msg_get_subject,
    split_body_parts,
)

# Version of the serialised shape produced by to_dict().  Consumers that
# persist or transport these records -- the MCP server first among them --
# pin against it, so bump it whenever a key changes meaning or leaves.
# Adding a key is not a bump; every key is always present, so a reader
# that does not know a new one can ignore it.
SCHEMA_VERSION = 1

# Leading Re:/Aw:/Fwd: style reply markers, including the "Re[2]:" form.
_RE_REPLY = re.compile(
    r'^\s*(?:(?:re|aw|fwd|fw|sv|vs|antw)\s*(?:\[\d+])?\s*:\s*)+',
    re.IGNORECASE,
)

# b4's catch-all for a reply marker in a language we do not know: two or
# three letters and a colon, directly in front of the bracket group.
_RE_REPLY_FOREIGN = re.compile(r'^\w{2,3}:\s*\[')

# One leading [bracketed] prefix group.
_RE_BRACKET = re.compile(r'^\s*\[([^]]*)]\s*')

# "[PATCHv3]" and "[RFC PATCHv2]" -- written without the space often
# enough that it is worth repairing before tokenising.
_RE_GLUED_VERSION = re.compile(r'(patch)(v\d+)', re.IGNORECASE)

# A "v3" style version token.
_RE_VERSION = re.compile(r'^v(\d+)$', re.IGNORECASE)

# An "n/m" counter token, after whitespace around the slash is removed.
# Bounded, so a date or a version range cannot pose as a counter.
_RE_COUNTER = re.compile(r'^(\d{1,4})/(\d{1,4})$')

# Trailers that record a fact about the patch rather than a person's
# endorsement of it.  Taken from b4, which has the better-researched
# vocabulary here.
_UTILITY_TRAILERS = frozenset(
    {
        'base-commit',
        'based-on',
        'buglink',
        'change-id',
        'closes',
        'fixes',
        'link',
        'message-id',
        'obsoleted-by',
    }
)

# A "Name <address>" trailer value, parsed by hand: email.utils.parseaddr
# applies RFC 2822 semantics that mangle display names holding quotes,
# commas or parentheses, and a trailer is plain text, not a header.
_RE_TRAILER_ADDR = re.compile(r'^(.*?)\s*<([^>]+)>\s*$')

# Inserted by mail clients that back-convert from HTML mail.
_RE_MAILTO = re.compile(r'<mailto:[^>]+>')

# A value that names an email address somewhere inside it.
_RE_HAS_ADDR = re.compile(r'\S+@\S+\.\S+')

# An extended local part, as in foo+kernel@example.com.
_RE_EXTENDED_LOCAL = re.compile(r'\+[^@]+@')

# Patch and commit metadata that is not review feedback.  A reply that
# quotes a patch's own headers should not be read as having offered
# them, so these are dropped from replies (b4 does the same).
_REPLY_IGNORES = frozenset(
    {
        'author',
        'base-commit',
        'cc',
        'change-id',
        'date',
        'from',
        'message-id',
        'subject',
        'subscribe',
        'to',
        'unsubscribe',
    }
)


def _isoformat(when: datetime.datetime | None) -> str | None:
    """Render a timestamp for JSON, passing ``None`` straight through."""
    return when.isoformat() if when is not None else None


@dataclass(frozen=True)
class Trailer:
    """A ``Name: value`` trailer line seen somewhere in a thread.

    *type* is one of ``person`` (an endorsement by somebody, with *addr*
    holding their parsed name and email), ``utility`` (a fact about the
    patch, such as ``Fixes:`` or ``base-commit:``) or ``unknown`` (a
    trailer we could make no sense of).  The split follows b4's.

    *msgid* and *date* say which message offered this trailer and when.
    Without them a trailer can only be placed in a thread, not in its
    timeline, and "was this reviewed before or after the last reply?"
    has no answer.  Both are ``None`` for a trailer parsed on its own,
    outside any message.
    """

    name: str
    value: str
    type: str = 'unknown'
    addr: tuple[str, str] | None = None
    msgid: str | None = None
    date: datetime.datetime | None = None

    @classmethod
    def parse(
        cls,
        name: str,
        value: str,
        *,
        msgid: str | None = None,
        date: datetime.datetime | None = None,
    ) -> Trailer:
        """Classify a raw ``name``/``value`` pair into a trailer.

        A person trailer's value is normalised while we are here, so
        that a stray space inside the brackets or a ``<mailto:>`` tag
        left behind by an HTML mail client does not make two copies of
        the same endorsement look like two different ones.

        *msgid* and *date* are recorded as given; this does not go
        looking for them, since the caller is the one holding the
        message.
        """
        origin: dict[str, Any] = {'msgid': msgid, 'date': date}
        if name.lower() in _UTILITY_TRAILERS or '://' in value:
            return cls(name=name, value=value, type='utility', **origin)
        if not _RE_HAS_ADDR.search(value):
            return cls(name=name, value=value, **origin)
        value = _RE_MAILTO.sub('', value).strip()
        match = _RE_TRAILER_ADDR.match(value)
        if match:
            addr = (match.group(1).strip(), match.group(2).strip())
        else:
            addr = email.utils.parseaddr(value)
        if not addr[0] and not addr[1]:
            return cls(name=name, value=value, **origin)
        return cls(
            name=name,
            value=format_addrs([addr], clean=False),
            type='person',
            addr=addr,
            **origin,
        )

    @property
    def key(self) -> str:
        """The trailer name lowercased, for case-insensitive matching."""
        return self.name.lower()

    @property
    def is_utility(self) -> bool:
        """Whether this records a fact rather than somebody's endorsement."""
        return self.type == 'utility'

    @property
    def is_person(self) -> bool:
        """Whether this is an endorsement by an identifiable person."""
        return self.type == 'person'

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this trailer."""
        return {
            'name': self.name,
            'value': self.value,
            'type': self.type,
            'email': self.addr[1] if self.addr else None,
            'msgid': self.msgid,
            'date': _isoformat(self.date),
        }


@dataclass(frozen=True)
class PatchSubject:
    """What a subject line's ``[PATCH ...]`` brackets decompose into."""

    title: str
    version: int = 1
    counter: int = 1
    expected: int = 1
    prefixes: tuple[str, ...] = ()
    is_patch: bool = False
    is_reply: bool = False
    version_inferred: bool = True
    counters_inferred: bool = True

    @property
    def is_cover(self) -> bool:
        """Whether this is a ``0/n`` cover letter."""
        return self.is_patch and not self.counters_inferred and self.counter == 0


@dataclass(frozen=True)
class MessageSummary:
    """What one message is, who sent it, and when.

    A thread's aggregate fields say *that* something happened somewhere
    in it.  These say *where*, which is what any question about order
    needs: whose message came last, whether a review landed before or
    after a reroll, which patch in a series a tag was attached to.

    *author* is the sender, preferring an in-body ``From:`` when the
    message carries one, so a patch sent on somebody's behalf is
    attributed to whoever wrote it.
    """

    msgid: str
    subject: str
    author: tuple[str, str]
    date: datetime.datetime | None
    patch: PatchSubject
    has_diff: bool = False
    trailers: tuple[Trailer, ...] = ()

    @property
    def is_submission(self) -> bool:
        """Whether this message is a patch rather than a reply to one.

        A ``Re:`` carries the brackets of whatever it answers, so the
        subject alone cannot tell the two apart; this applies the rule
        that it is a submission only when it is not a reply.
        """
        return self.patch.is_patch and not self.patch.is_reply

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this message."""
        return {
            'msgid': self.msgid,
            'subject': self.subject,
            'author': {'name': self.author[0], 'email': self.author[1]},
            'date': _isoformat(self.date),
            'is_submission': self.is_submission,
            'is_reply': self.patch.is_reply,
            'is_cover': self.patch.is_cover,
            'counter': self.patch.counter,
            'expected': self.patch.expected,
            'version': self.patch.version,
            'has_diff': self.has_diff,
            'trailers': [trailer.to_dict() for trailer in self.trailers],
        }


@dataclass(frozen=True)
class ThreadSummary:
    """Descriptive facts about a single thread.

    ``msgids`` lists every message in the thread, the root first and
    the rest oldest-first, so a caller can go back to any individual
    message without re-deriving the grouping.  It runs parallel to
    ``messages``: ``msgids[i]`` is ``messages[i].msgid``, and both are
    ``count`` long.  A message that carries no Message-Id -- rare, but
    it happens -- still holds its place, as an empty string.

    The patch fields describe what the submissions in the thread *said
    about themselves*.  In particular ``patch_count`` is how many
    patches were seen and ``expected`` is how many the brackets claimed
    there would be; comparing the two is the caller's business, not
    ours.

    ``author`` is who opened the thread and ``participants`` is
    everybody who spoke in it, the author included.  The two are worth
    keeping apart: being in ``participants`` says you sent something,
    not that you answered anybody, and for the person who posted the
    series in the first place those are very different claims.

    ``messages`` holds the same thread message by message, in the order
    of ``msgids``.  The aggregate fields are all derivable from it and
    are kept because they are what most callers actually want.
    """

    msgid: str
    subject: str
    count: int
    first: datetime.datetime | None
    last: datetime.datetime | None
    msgids: tuple[str, ...] = ()
    is_patch_series: bool = False
    patch_count: int = 0
    expected: int = 0
    version: int = 1
    has_cover: bool = False
    prefixes: tuple[str, ...] = ()
    author: tuple[str, str] | None = None
    participants: tuple[tuple[str, str], ...] = ()
    trailers: tuple[Trailer, ...] = ()
    messages: tuple[MessageSummary, ...] = ()

    @property
    def trailer_names(self) -> frozenset[str]:
        """Every trailer name seen in the thread, lowercased."""
        return frozenset(trailer.key for trailer in self.trailers)

    @property
    def person_trailers(self) -> tuple[Trailer, ...]:
        """Trailers that are somebody's endorsement of the patches."""
        return tuple(trailer for trailer in self.trailers if trailer.is_person)

    @property
    def utility_trailers(self) -> tuple[Trailer, ...]:
        """Trailers that record a fact, such as ``Fixes:`` or ``Link:``."""
        return tuple(trailer for trailer in self.trailers if trailer.is_utility)

    @property
    def repliers(self) -> tuple[tuple[str, str], ...]:
        """Participants other than the author, in the order they spoke.

        The author is dropped by address, so somebody who posted the
        series and then answered a review is still not counted as
        having replied to it -- they are the thread's author either
        way.  Which of the remaining people count as a real answer is,
        as ever, the caller's call.
        """
        if self.author is None:
            return self.participants
        return tuple(
            person
            for person in self.participants
            if person[1].lower() != self.author[1].lower()
        )

    def messages_by(self, addr: str) -> tuple[MessageSummary, ...]:
        """Messages sent from *addr*, in thread order.

        Matched on the address alone, case-insensitively, and exactly:
        resolving one person's several addresses to one identity needs a
        directory this module does not have and should not guess at.
        """
        wanted = addr.lower()
        return tuple(
            message for message in self.messages if message.author[1].lower() == wanted
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this summary.

        The key set is fixed and every key is always present, so a
        consumer never has to probe for optional fields.  ``schema`` is
        :data:`SCHEMA_VERSION`, for a consumer that stores these.
        """
        return {
            'schema': SCHEMA_VERSION,
            'msgid': self.msgid,
            'subject': self.subject,
            'count': self.count,
            'msgids': list(self.msgids),
            'first': _isoformat(self.first),
            'last': _isoformat(self.last),
            'is_patch_series': self.is_patch_series,
            'patch_count': self.patch_count,
            'expected': self.expected,
            'version': self.version,
            'has_cover': self.has_cover,
            'prefixes': list(self.prefixes),
            'author': (
                None
                if self.author is None
                else {'name': self.author[0], 'email': self.author[1]}
            ),
            'participants': [
                {'name': name, 'email': addr} for name, addr in self.participants
            ],
            'trailers': [trailer.to_dict() for trailer in self.trailers],
            'messages': [message.to_dict() for message in self.messages],
        }


def parse_patch_subject(subject: str) -> PatchSubject:
    """Decompose a subject line into its bracketed prefix parts.

    Handles the shapes that show up on kernel lists: ``[PATCH]``,
    ``[PATCH v3 2/7]``, ``[RFC PATCH net-next 0/4]``, the spaceless
    ``[PATCHv3]``, split groups like ``[RFC] [PATCH 1/2]``, nested or
    unbalanced brackets, and any of those behind a ``Re:``.  Tokens that
    are not a ``PATCH`` marker, a version, or an ``n/m`` counter are kept
    in *prefixes* in the order they appeared.

    An unnumbered ``[PATCH]`` reports ``counter=1``, ``expected=1`` with
    ``counters_inferred`` set, since a lone patch is a one-patch series;
    the flags are there for a caller that needs to tell a stated ``1/1``
    from an assumed one.

    The corner cases here follow b4's ``LoreSubject``, which has had them
    beaten out of it against the real lore corpus.
    """
    remainder = re.sub(r'\s+', ' ', clean_header(subject)).strip()

    remainder, count = _RE_REPLY.subn('', remainder)
    is_reply = count > 0
    if not is_reply and _RE_REPLY_FOREIGN.match(remainder):
        # A reply marker in some language we do not recognise; keep the
        # bracket group it was sitting in front of.
        remainder = _RE_REPLY_FOREIGN.sub('[', remainder, count=1)
        is_reply = True

    version = 1
    counter = 1
    expected = 1
    version_inferred = True
    counters_inferred = True
    prefixes: list[str] = []
    is_patch = False

    while True:
        match = _RE_BRACKET.match(remainder)
        if not match:
            break
        remainder = remainder[match.end() :]
        group = match.group(1)
        # Collapse a nested or unbalanced group such as "[[PATCH] v2" down
        # to its innermost content before tokenising it.
        while '[' in group:
            group = group[group.rindex('[') + 1 :]
        # "[PATCHv3]" -> "[PATCH v3]".
        group = _RE_GLUED_VERSION.sub(r'\1 \2', group)
        # Normalise "2 / 7" to "2/7" so the counter survives tokenising.
        group = re.sub(r'\s*/\s*', '/', group)
        for token in group.split():
            # Subject prefixes get separated by stray punctuation often
            # enough to be worth cleaning up.
            token = token.strip(',;')
            if not token:
                continue
            lowered = token.lower()
            if lowered.find('patch') == 0:
                is_patch = True
                continue
            vmatch = _RE_VERSION.match(token)
            if vmatch:
                version = int(vmatch.group(1))
                version_inferred = False
                continue
            cmatch = _RE_COUNTER.match(token)
            if cmatch:
                counter = int(cmatch.group(1))
                expected = int(cmatch.group(2))
                counters_inferred = False
                continue
            # Anything else -- "RFC", "net-next", "RESEND" -- is a
            # prefix, kept verbatim in the order it appeared.
            prefixes.append(token)

    return PatchSubject(
        title=remainder.strip(),
        version=version,
        counter=counter,
        expected=expected,
        prefixes=tuple(prefixes),
        is_patch=is_patch,
        is_reply=is_reply,
        version_inferred=version_inferred,
        counters_inferred=counters_inferred,
    )


def _email_eq(one: str, two: str) -> bool:
    """Whether two addresses name the same mailbox, as b4 compares them."""
    one, two = one.lower(), two.lower()
    if one == two:
        return True
    if '@' not in one or '@' not in two:
        return False
    # An extended local part is still the same mailbox, so
    # foo+kernel@example.com matches foo@example.com.
    one = _RE_EXTENDED_LOCAL.sub('@', one)
    two = _RE_EXTENDED_LOCAL.sub('@', two)
    if one == two:
        return True
    # One domain may be a subdomain of the other, which covers the
    # foo@linux.intel.com and foo@intel.com pairing.
    olocal, odomain = one.split('@', maxsplit=1)
    tlocal, tdomain = two.split('@', maxsplit=1)
    if olocal != tlocal:
        return False
    return abs(odomain.count('.') - tdomain.count('.')) == 1 and (
        odomain.endswith(f'.{tdomain}') or tdomain.endswith(f'.{odomain}')
    )


def _name_eq(tname: str, fromname: str) -> bool:
    """Whether a trailer's name is the same name as the sender's."""
    tname, fromname = tname.lower().strip(), fromname.lower().strip()
    if not tname or not fromname:
        return False
    if tname == fromname:
        return True
    # A From: written "Okonkwo, Amara" still matches a trailer
    # signed "Amara Okonkwo".
    chunks = [chunk.strip() for chunk in fromname.split(',') if chunk.strip()]
    if len(chunks) < 2:
        return False
    return all(chunk in tname for chunk in chunks)


def _is_self_attested(trailer: Trailer, author: tuple[str, str]) -> bool:
    """Whether a reply's person trailer was offered by the sender.

    b4 accepts a follow-up trailer only from the person it names: you
    may tag on your own behalf, not on somebody else's.  Trailers that
    name nobody -- ``Fixes:``, ``Link:`` and the like -- are not subject
    to this, since there is no identity to compare.
    """
    if trailer.addr is None:
        return True
    return _email_eq(trailer.addr[1], author[1]) or _name_eq(trailer.addr[0], author[0])


def extract_trailers(msg: EmailMessage) -> list[Trailer]:
    """Return every trailer line found in a message body, in order.

    How a message is read depends on what it is, which is what b4 does
    too.

    A message carrying a diff is a patch, so its trailers are taken from
    the last paragraph of the message proper -- above the ``---``, below
    the commit message.  That is what keeps an ordinary commit subject
    such as ``usb: fix the thing`` from being read as a trailer named
    ``usb``: it lives in the first paragraph, not the trailing block.

    Anything else is a reply, and reviewers put their tags wherever they
    like, so the whole body is scanned.  The stricter follow-up rules
    apply there -- a person trailer must name a person, a URL only
    belongs in a link trailer -- and patch metadata such as ``From:`` or
    ``base-commit:`` is dropped, since a reply quoting a patch's headers
    has not thereby offered them.

    A reply's person trailer counts only when the sender named himself
    or herself in it, which is how b4 keeps a quoted or pasted address
    from being read as a fresh endorsement.

    A trailer may be indented -- some maintainers send them that way --
    but one behind a ``>`` quoting marker is a copy of a trailer
    somebody wrote elsewhere and does not count here.
    """
    return _read_trailers(msg)[0]


def _read_trailers(msg: EmailMessage) -> tuple[list[Trailer], bool]:
    """Return a message's trailers and whether it carried a diff.

    The two come out together because deciding which is which is the
    same decision: reading the body once answers both, and a caller that
    wants both should not pay to parse it twice.
    """
    body = msg_get_payload(msg, strip_signature=False)
    msgid = get_clean_msgid(msg)
    date = msg_get_date(msg)
    if body_has_diff(body):
        found = split_body_parts(body).trailers
        return [
            Trailer.parse(name, value, msgid=msgid, date=date) for name, value in found
        ], True

    found, _others = find_trailers(body, followup=True)
    found = [(n, v) for n, v in found if n.lower() not in _REPLY_IGNORES]
    author = msg_get_author(msg)
    return [
        trailer
        for trailer in (
            Trailer.parse(name, value, msgid=msgid, date=date) for name, value in found
        )
        if _is_self_attested(trailer, author)
    ], False


def summarize_message(msg: EmailMessage) -> MessageSummary:
    """Summarise a single message on its own terms.

    Useful by itself, and what :func:`summarize_thread` builds its
    ``messages`` out of, so the two can never disagree about what a
    given message is.
    """
    subject = msg_get_subject(msg)
    trailers, has_diff = _read_trailers(msg)
    return MessageSummary(
        msgid=get_clean_msgid(msg) or '',
        subject=subject,
        author=msg_get_author(msg, use_inbody=True),
        date=msg_get_date(msg),
        patch=parse_patch_subject(subject),
        has_diff=has_diff,
        trailers=tuple(trailers),
    )


def _thread_parents(msg: EmailMessage) -> set[str]:
    """Return the message IDs this message claims as ancestors."""
    parents: set[str] = set()
    for header in ('In-Reply-To', 'References'):
        raw = msg.get(header)
        if not raw:
            continue
        parents.update(re.findall(r'<([^>]+)>', str(raw)))
    own = get_clean_msgid(msg)
    if own is not None:
        # A message that references itself is not its own ancestor.
        parents.discard(own)
    return parents


def group_by_thread(msgs: Sequence[EmailMessage]) -> list[list[EmailMessage]]:
    """Partition a flat bag of messages into threads.

    A query against a public-inbox server returns messages from many
    different threads at once, so something has to split them up before
    they can be summarised.  Messages are grouped into connected
    components of the reference graph, which keeps a thread together
    even when its root is missing from the input.

    Each returned thread is sorted oldest first, and the threads
    themselves are ordered by their earliest message.
    """
    ids: list[str] = []
    by_id: dict[str, EmailMessage] = {}
    for msg in msgs:
        msgid = get_clean_msgid(msg)
        if msgid is None or msgid in by_id:
            continue
        ids.append(msgid)
        by_id[msgid] = msg

    # Referenced ancestors join the graph even when the message itself is
    # absent from the input, so two replies whose common parent was not
    # returned by the query still land in the same thread.
    parent: dict[str, str] = {msgid: msgid for msgid in ids}
    for msg in by_id.values():
        for ancestor in _thread_parents(msg):
            parent.setdefault(ancestor, ancestor)

    def find(msgid: str) -> str:
        while parent[msgid] != msgid:
            parent[msgid] = parent[parent[msgid]]
            msgid = parent[msgid]
        return msgid

    def union(left: str, right: str) -> None:
        lroot, rroot = find(left), find(right)
        if lroot != rroot:
            parent[rroot] = lroot

    for msgid in ids:
        for ancestor in _thread_parents(by_id[msgid]):
            union(msgid, ancestor)

    groups: dict[str, list[EmailMessage]] = {}
    for msgid in ids:
        groups.setdefault(find(msgid), []).append(by_id[msgid])

    threads = [_sort_by_date(group) for group in groups.values()]
    threads.sort(key=lambda thread: _sort_key(_first_date(thread)))
    return threads


def _first_date(msgs: Sequence[EmailMessage]) -> datetime.datetime | None:
    dates = [d for d in (msg_get_date(msg) for msg in msgs) if d is not None]
    return min(dates) if dates else None


def _sort_key(when: datetime.datetime | None) -> datetime.datetime:
    """Sort undated items last, and make naive/aware datetimes comparable."""
    if when is None:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    if when.tzinfo is None:
        return when.replace(tzinfo=datetime.timezone.utc)
    return when


def _patch_order(msg: EmailMessage) -> int:
    """Where a patch sits in its series, for breaking ties on the date.

    ``git send-email`` stamps a whole series within the same second, so
    dates alone leave the patches in whatever order they arrived in.
    """
    parsed = parse_patch_subject(msg_get_subject(msg))
    if not parsed.is_patch or parsed.is_reply or parsed.counters_inferred:
        return -1
    return parsed.counter


def _sort_by_date(msgs: Sequence[EmailMessage]) -> list[EmailMessage]:
    return sorted(
        msgs, key=lambda msg: (_sort_key(msg_get_date(msg)), _patch_order(msg))
    )


def _pick_root(msgs: Sequence[EmailMessage]) -> EmailMessage:
    """Return the thread root: the oldest message with no parent present."""
    present = {msgid for msgid in (get_clean_msgid(msg) for msg in msgs) if msgid}
    for msg in msgs:
        if not _thread_parents(msg) & present:
            return msg
    return msgs[0]


def summarize_thread(msgs: Sequence[EmailMessage]) -> ThreadSummary:
    """Summarise one thread's worth of messages.

    The input is expected to be a single thread; use
    :func:`group_by_thread` first if it might hold more than one.
    Messages are considered in date order, so participants come back in
    the order they first spoke.
    """
    if not msgs:
        raise ValueError('Cannot summarize an empty thread')

    ordered = _sort_by_date(msgs)
    root = _pick_root(ordered)
    summaries = [summarize_message(msg) for msg in ordered]

    # The aggregates below are accumulated in date order, so participants
    # still come back in the order they first spoke.  Only the reported
    # messages tuple is re-ordered root first: a cover letter can be
    # stamped a second after the first patch, and messages[0] should be
    # the root.  Moved by position rather than by message-id, so a
    # message that has none is not dropped on the way.
    in_thread_order = list(summaries)
    root_at = next(i for i, msg in enumerate(ordered) if msg is root)
    in_thread_order.insert(0, in_thread_order.pop(root_at))

    dates = [s.date for s in summaries if s.date is not None]

    participants: list[tuple[str, str]] = []
    seen_addrs: set[str] = set()
    trailers: list[Trailer] = []
    seen_trailers: set[tuple[str, str]] = set()

    counters: set[int] = set()
    unnumbered = 0
    prefixes: list[str] = []
    seen_prefixes: set[str] = set()
    version = 1
    expected = 0
    is_patch_series = False
    has_cover = False

    for summary in summaries:
        name, addr = summary.author
        if addr.lower() not in seen_addrs:
            seen_addrs.add(addr.lower())
            participants.append((name, addr))

        # Deduped on name and value, so the thread-level tuple keeps one
        # copy of each distinct endorsement -- carrying the message-id
        # and date of the first message to offer it.  Every occurrence is
        # still on the message that made it, in messages[].
        for trailer in summary.trailers:
            dedupe = (trailer.key, trailer.value)
            if dedupe not in seen_trailers:
                seen_trailers.add(dedupe)
                trailers.append(trailer)

        parsed = summary.patch
        # A "Re:" carries the brackets of whatever it replies to, so it
        # must not be counted as another submission.
        if not summary.is_submission:
            continue

        is_patch_series = True
        version = max(version, parsed.version)
        expected = max(expected, parsed.expected)
        if parsed.is_cover:
            has_cover = True
        elif parsed.counters_inferred:
            # Nothing said "n/m", so this patch can only count as itself.
            unnumbered += 1
        else:
            counters.add(parsed.counter)
        for prefix in parsed.prefixes:
            if prefix.lower() not in seen_prefixes:
                seen_prefixes.add(prefix.lower())
                prefixes.append(prefix)

    patch_count = len(counters) + unnumbered

    return ThreadSummary(
        msgid=get_clean_msgid(root) or '',
        subject=msg_get_subject(root),
        count=len(ordered),
        msgids=tuple(summary.msgid for summary in in_thread_order),
        first=min(dates) if dates else None,
        last=max(dates) if dates else None,
        is_patch_series=is_patch_series,
        patch_count=patch_count,
        expected=expected,
        version=version,
        has_cover=has_cover,
        prefixes=tuple(prefixes),
        author=in_thread_order[0].author,
        participants=tuple(participants),
        trailers=tuple(trailers),
        messages=tuple(in_thread_order),
    )


def summarize_threads(msgs: Iterable[EmailMessage]) -> list[ThreadSummary]:
    """Group a flat bag of messages into threads and summarise each one.

    This is the entry point for a query result, which mixes messages
    from many threads together.  Summaries come back ordered by their
    thread's earliest message.
    """
    return [summarize_thread(thread) for thread in group_by_thread(list(msgs))]
