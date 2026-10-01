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

Questions about people go through :class:`Identity`, so that every
caller answers "is this the same person?" the same way.  Knowing that
two unrelated addresses belong to one human needs a directory, which is
the caller's to supply; what this module knows on its own is documented
on :func:`email_matches` and :func:`name_matches`.
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
    strip_reply_prefixes,
)

# Version of the serialised shape produced by to_dict().  Consumers that
# persist or transport these records -- the MCP server first among them --
# pin against it, so bump it whenever a key changes meaning or leaves.
# Adding a key is not a bump; every key is always present, so a reader
# that does not know a new one can ignore it.
SCHEMA_VERSION = 1

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

# One word of a display name, punctuation and spacing left out.
_RE_NAME_WORD = re.compile(r'\w+')

# An aside in a display name, as in "Bob Reviewer (Arm)" -- usually the
# employer, sometimes a nickname, never part of the name itself.
_RE_NAME_ASIDE = re.compile(r'\([^)]*\)')

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


def email_matches(one: str, two: str) -> bool:
    """Whether two addresses name the same mailbox.

    Beyond a case-insensitive comparison this forgives the two ways one
    mailbox routinely appears under two spellings on a mailing list: an
    extended local part, so ``foo+kernel@example.com`` matches
    ``foo@example.com``, and a domain that is one label deeper than the
    other, so ``foo@linux.intel.com`` matches ``foo@intel.com``.

    It does not attempt to know that two genuinely different addresses
    belong to one person -- that needs a directory, which is a caller's
    to supply.  :class:`Identity` is how to hand one in.
    """
    one, two = one.lower(), two.lower()
    if one == two:
        return True
    if '@' not in one or '@' not in two:
        return False
    one = _RE_EXTENDED_LOCAL.sub('@', one)
    two = _RE_EXTENDED_LOCAL.sub('@', two)
    if one == two:
        return True
    olocal, odomain = one.split('@', maxsplit=1)
    tlocal, tdomain = two.split('@', maxsplit=1)
    if olocal != tlocal:
        return False
    return abs(odomain.count('.') - tdomain.count('.')) == 1 and (
        odomain.endswith(f'.{tdomain}') or tdomain.endswith(f'.{odomain}')
    )


def _clean_name(name: str) -> str:
    """Lowercase a display name and drop anything in parentheses.

    Kernel mail routinely carries the sender's employer in the From
    line -- "Bob Reviewer (Arm)" -- while the tag they sign is written
    plainly, or the other way round.  The aside is not part of anybody's
    name, so it plays no part in comparing two of them.
    """
    return ' '.join(_RE_NAME_ASIDE.sub(' ', name).lower().split())


def _name_parts_match(whole: str, listed: str) -> bool:
    """Whether *listed*, written "Surname, Given", names *whole*'s person.

    Every word of *listed* has to be a whole word of *whole*, so
    "Okonkwo, Amara" matches "Amara Okonkwo" but "Smith, Alex" does not
    match "Alexander Smithson".  Extra words on the *whole* side are
    fine: a middle name or an initial does not stop the match.
    """
    chunks = [chunk.strip() for chunk in listed.split(',') if chunk.strip()]
    if len(chunks) < 2:
        return False
    words = _RE_NAME_WORD.findall(listed)
    if not words:
        return False
    return set(words) <= set(_RE_NAME_WORD.findall(whole))


def name_matches(one: str, two: str) -> bool:
    """Whether two display names are the same person's, as far as we can tell.

    Case and surrounding space are ignored, an aside in parentheses is
    dropped so "Bob Reviewer (Arm)" is still Bob Reviewer, and a name
    written "Okonkwo, Amara" matches the same name written out
    straight.  Unlike the comparison b4 makes internally, this is
    symmetric: it does not matter which of the two is the
    comma-separated one.

    Two people who share a name are indistinguishable here, so this is a
    weaker signal than :func:`email_matches` and should not be the only
    thing a decision rests on.  An empty name matches nothing, not even
    another empty one.
    """
    one, two = _clean_name(one), _clean_name(two)
    if not one or not two:
        return False
    if one == two:
        return True
    return _name_parts_match(one, two) or _name_parts_match(two, one)


@dataclass(frozen=True)
class Identity:
    """One person, and the names and addresses they are known by.

    Everything in this module that asks "is this the same person?" has
    to answer it the same way, or two callers looking at one thread will
    disagree about who reviewed it.  This is that one way, and it is the
    seam where a caller's own knowledge goes: liblore can tell that
    ``foo+list@example.com`` and ``foo@example.com`` are one mailbox, but
    only a directory -- a MAINTAINERS file, a config entry, a mapping the
    maintainer wrote by hand -- knows that two unrelated addresses belong
    to one human.  Build an Identity from what that directory says.

    A match on any one address or any one name is a match.  Address
    matching is the strong signal; see :func:`name_matches` for why the
    name half is weaker.
    """

    addrs: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    @classmethod
    def from_addr(cls, addr: tuple[str, str]) -> Identity:
        """Build an identity from one parsed ``(name, email)`` pair."""
        name, email_addr = addr
        return cls(
            addrs=(email_addr,) if email_addr else (),
            names=(name,) if name else (),
        )

    def matches_addr(self, addr: str) -> bool:
        """Whether *addr* is one of this person's mailboxes."""
        return any(email_matches(addr, known) for known in self.addrs)

    def matches_name(self, name: str) -> bool:
        """Whether *name* is one of the names this person goes by."""
        return any(name_matches(name, known) for known in self.names)

    def matches(self, who: str | tuple[str, str] | Identity) -> bool:
        """Whether *who* is this person.

        Accepts a bare address, a parsed ``(name, email)`` pair, or
        another identity.  A pair matches on either half, so a
        participant whose address we have never seen is still recognised
        by name -- and vice versa, which is the usual case.
        """
        if isinstance(who, Identity):
            return any(self.matches_addr(a) for a in who.addrs) or any(
                self.matches_name(n) for n in who.names
            )
        if isinstance(who, str):
            return self.matches_addr(who)
        name, addr = who
        return self.matches_addr(addr) or self.matches_name(name)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view of this identity."""
        return {'addrs': list(self.addrs), 'names': list(self.names)}


def _as_identity(who: str | tuple[str, str] | Identity) -> Identity:
    """Coerce the several ways of naming a person into an Identity."""
    if isinstance(who, Identity):
        return who
    if isinstance(who, str):
        return Identity(addrs=(who,))
    return Identity.from_addr(who)


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

    ``participants`` is literal: it holds one entry per distinct From
    address, so somebody who wrote from two addresses appears twice.
    That is what the thread contains, and deciding that two addresses
    are one person is the caller's to do -- see :class:`Identity`.

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

        The author is dropped however they spelled themselves, so
        somebody who posted the series from one address and answered a
        review from another is still not counted as having replied to
        it -- they are the thread's author either way.  Which of the
        remaining people count as a real answer is, as ever, the
        caller's call.
        """
        if self.author is None:
            return self.participants
        person = Identity.from_addr(self.author)
        return tuple(other for other in self.participants if not person.matches(other))

    def messages_by(
        self, who: str | tuple[str, str] | Identity
    ) -> tuple[MessageSummary, ...]:
        """Messages sent by *who*, in thread order.

        *who* may be a bare address, a parsed ``(name, email)`` pair or
        an :class:`Identity`.  A bare address is compared as an address
        only, so pass an Identity when one person's several unrelated
        addresses have to count as one.
        """
        person = _as_identity(who)
        return tuple(
            message for message in self.messages if person.matches(message.author)
        )

    def trailers_by(self, who: str | tuple[str, str] | Identity) -> tuple[Trailer, ...]:
        """Trailers naming *who*, in the order they were first offered.

        These are the endorsements attributed *to* that person, which is
        not the same as the trailers carried by messages they sent: a
        submitter collects a reviewer's tag into the next revision, and
        the tag is still the reviewer's.
        """
        person = _as_identity(who)
        return tuple(
            trailer
            for trailer in self.trailers
            if trailer.addr is not None and person.matches(trailer.addr)
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

    remainder, is_reply = strip_reply_prefixes(remainder)

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


def _is_self_attested(trailer: Trailer, author: tuple[str, str]) -> bool:
    """Whether a reply's person trailer was offered by the sender.

    b4 accepts a follow-up trailer only from the person it names: you
    may tag on your own behalf, not on somebody else's.  Trailers that
    name nobody -- ``Fixes:``, ``Link:`` and the like -- are not subject
    to this, since there is no identity to compare.

    *author* is expected to come from the message headers.  This is a
    check on who sent the mail, and nothing a sender writes in the body
    may answer that.  It is also not a signature check: liblore does no
    DKIM, so this says a message claims to come from the person it tags,
    not that it provably does.
    """
    if trailer.addr is None:
        return True
    return Identity.from_addr(author).matches(trailer.addr)


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
    # Who sent the mail is a question only the headers can answer.  An
    # in-body From: says who wrote a patch, which is a different
    # question and is body text the sender chose, so it takes no part in
    # deciding whether they may sign a tag.
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
