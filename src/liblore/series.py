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
    """

    name: str
    value: str
    type: str = 'unknown'
    addr: tuple[str, str] | None = None

    @classmethod
    def parse(cls, name: str, value: str) -> Trailer:
        """Classify a raw ``name``/``value`` pair into a trailer.

        A person trailer's value is normalised while we are here, so
        that a stray space inside the brackets or a ``<mailto:>`` tag
        left behind by an HTML mail client does not make two copies of
        the same endorsement look like two different ones.
        """
        if name.lower() in _UTILITY_TRAILERS or '://' in value:
            return cls(name=name, value=value, type='utility')
        if not _RE_HAS_ADDR.search(value):
            return cls(name=name, value=value)
        value = _RE_MAILTO.sub('', value).strip()
        match = _RE_TRAILER_ADDR.match(value)
        if match:
            addr = (match.group(1).strip(), match.group(2).strip())
        else:
            addr = email.utils.parseaddr(value)
        if not addr[0] and not addr[1]:
            return cls(name=name, value=value)
        return cls(
            name=name,
            value=format_addrs([addr], clean=False),
            type='person',
            addr=addr,
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
class ThreadSummary:
    """Descriptive facts about a single thread.

    ``msgids`` lists every message in the thread, the root first and
    the rest oldest-first, so a caller can go back to any individual
    message without re-deriving the grouping.

    The patch fields describe what the submissions in the thread *said
    about themselves*.  In particular ``patch_count`` is how many
    patches were seen and ``expected`` is how many the brackets claimed
    there would be; comparing the two is the caller's business, not
    ours.
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
    participants: tuple[tuple[str, str], ...] = ()
    trailers: tuple[Trailer, ...] = ()

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

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this summary.

        The key set is fixed and every key is always present, so a
        consumer never has to probe for optional fields.
        """
        return {
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
            'participants': [
                {'name': name, 'email': addr} for name, addr in self.participants
            ],
            'trailers': [
                {
                    'name': trailer.name,
                    'value': trailer.value,
                    'type': trailer.type,
                    'email': trailer.addr[1] if trailer.addr else None,
                }
                for trailer in self.trailers
            ],
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

    A trailer may be indented -- some maintainers send them that way --
    but one behind a ``>`` quoting marker is a copy of a trailer
    somebody wrote elsewhere and does not count here.
    """
    body = msg_get_payload(msg, strip_signature=False)
    if body_has_diff(body):
        found = split_body_parts(body).trailers
    else:
        found, _others = find_trailers(body, followup=True)
        found = [(n, v) for n, v in found if n.lower() not in _REPLY_IGNORES]
    return [Trailer.parse(name, value) for name, value in found]


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


def _collect_msgids(
    root: EmailMessage, ordered: Sequence[EmailMessage]
) -> tuple[str, ...]:
    """List the thread's message-ids, root first and the rest in order.

    The root leads even when it is not the oldest message: a cover
    letter can carry a timestamp a second later than the first patch,
    and a caller reading ``msgids[0]`` should get the same message as
    ``msgid``.
    """
    found = [msgid for msgid in (get_clean_msgid(msg) for msg in ordered) if msgid]
    root_msgid = get_clean_msgid(root)
    if root_msgid and root_msgid in found:
        found.remove(root_msgid)
        found.insert(0, root_msgid)
    return tuple(found)


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
    dates = [d for d in (msg_get_date(msg) for msg in ordered) if d is not None]

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

    for msg in ordered:
        name, addr = msg_get_author(msg, use_inbody=True)
        if addr.lower() not in seen_addrs:
            seen_addrs.add(addr.lower())
            participants.append((name, addr))

        for trailer in extract_trailers(msg):
            dedupe = (trailer.key, trailer.value)
            if dedupe not in seen_trailers:
                seen_trailers.add(dedupe)
                trailers.append(trailer)

        parsed = parse_patch_subject(msg_get_subject(msg))
        # A "Re:" carries the brackets of whatever it replies to, so it
        # must not be counted as another submission.
        if not parsed.is_patch or parsed.is_reply:
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
        msgids=_collect_msgids(root, ordered),
        first=min(dates) if dates else None,
        last=max(dates) if dates else None,
        is_patch_series=is_patch_series,
        patch_count=patch_count,
        expected=expected,
        version=version,
        has_cover=has_cover,
        prefixes=tuple(prefixes),
        participants=tuple(participants),
        trailers=tuple(trailers),
    )


def summarize_threads(msgs: Iterable[EmailMessage]) -> list[ThreadSummary]:
    """Group a flat bag of messages into threads and summarise each one.

    This is the entry point for a query result, which mixes messages
    from many threads together.  Summaries come back ordered by their
    thread's earliest message.
    """
    return [summarize_thread(thread) for thread in group_by_thread(list(msgs))]
