# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2025-2026 The Linux Foundation
"""Message parsing, email utilities, threading, and mbox splitting."""

from __future__ import annotations

import datetime
import email.header
import email.parser
import email.quoprimime
import email.utils
import fnmatch
import logging
import re
import textwrap
import urllib.parse
from collections.abc import Sequence
from email.message import EmailMessage
from typing import NamedTuple

from liblore import emlpolicy

logger = logging.getLogger(__name__)

# Matches the "From " separator line that starts each message in an mbox.
# Mirrors git's is_from_line(): "From " + address + timestamp with a
# colon surrounded by digits (HH:MM:SS) and a year > 90.
_FROM_RE = re.compile(
    rb'^From \S+.*\d\d:\d\d:\d\d.*\d{4}',
)

# Matches one or more ">" followed by "From " — the mboxrd escaping
# that needs one level of ">" stripped.  Mirrors git's is_gtfrom().
_GTFROM_RE = re.compile(rb'^>+From ')


# =====================================================================
# Header and message-ID utilities
# =====================================================================


def clean_header(hdrval: str | None) -> str:
    """Decode an RFC 2047 encoded header and normalise whitespace.

    Handles encoded words, email addresses with encoded display names,
    and the CPython formataddr quoting bug (bpo-100900).
    """
    if hdrval is None:
        return ''

    if hdrval.find('=?') >= 0:
        # If the header contains email addresses, decode them individually
        if re.search(r'<\S+@\S+>', hdrval, flags=re.IGNORECASE | re.MULTILINE):
            newaddrs: list[str] = []
            for addr in email.utils.getaddresses([hdrval]):
                if addr[0].find('=?') >= 0:
                    addr = (clean_header(addr[0]), addr[1])
                # Work around https://github.com/python/cpython/issues/100900
                if re.search(r'[^\w\s]', addr[0]):
                    newaddrs.append(f'"{addr[0]}" <{addr[1]}>')
                else:
                    newaddrs.append(email.utils.formataddr(addr))
            return ', '.join(newaddrs)

        decoded = ''
        for hstr, hcs in email.header.decode_header(hdrval):
            if hcs is None:
                hcs = 'utf-8'
            try:
                decoded += hstr.decode(hcs, errors='replace')
            except LookupError:
                decoded += hstr.decode('utf-8', errors='replace')
            except (UnicodeDecodeError, AttributeError):
                decoded += hstr
    else:
        decoded = hdrval

    new_hdrval = re.sub(r'\n?\s+', ' ', decoded)
    return new_hdrval.strip()


def get_clean_msgid(msg: EmailMessage, header: str = 'Message-Id') -> str | None:
    """Extract a clean message ID (without angle brackets) from a header.

    Message-IDs are always ASCII and enclosed in angle brackets, and
    RFC 2047 forbids encoded words in msg-id headers (Message-Id,
    In-Reply-To, References).  We therefore extract straight from the
    raw value and deliberately avoid :func:`clean_header`, whose
    address-parsing path would mangle a bare message-ID when the header
    also carries an encoded-word comment (e.g. Gnus-style In-Reply-To).

    This mirrors :func:`_clean_msgid_raw`, which does the same on raw
    message bytes.
    """
    raw = msg.get(header)
    if raw:
        matches = re.search(r'<([^>]+)>', str(raw))
        if matches:
            msgid: str = matches.group(1)
            return msgid
    return None


def parse_message(raw: bytes) -> EmailMessage:
    """Parse raw email bytes into an EmailMessage using our policy."""
    msg: EmailMessage = email.parser.BytesParser(
        policy=emlpolicy,
    ).parsebytes(raw)
    return msg


# =====================================================================
# Email formatting and extraction
# =====================================================================


def msg_get_subject(msg: EmailMessage, strip_prefixes: bool = False) -> str:
    """Return the decoded Subject header.

    When *strip_prefixes* is ``True``, any leading ``[bracketed]``
    prefixes (e.g. ``[PATCH v3 2/5]``, ``[RFC]``) and ``Re:``/``Aw:``
    tags are removed, leaving just the bare subject text.
    """
    raw = msg.get('Subject', '')
    subject = clean_header(raw)
    if not strip_prefixes:
        return subject
    # Strip Re:/Aw:/Fwd: prefixes
    subject = re.sub(r'^(\w{2,3}:\s*)+', '', subject)
    # Strip all leading [bracketed] prefixes
    subject = re.sub(r'^(\s*\[[^]]*]\s*)+', '', subject)
    return subject.strip()


# Detecting real diff content, so a bare "---" in a free-form reply is
# not mistaken for a patch separator.  Both mirror b4, which has had the
# corner cases beaten out of it against the real lore corpus.
_RE_DIFF = re.compile(
    r'^(---.*\n\+\+\+|GIT binary patch|diff --git \w/\S+ \w/\S+)',
    flags=re.MULTILINE | re.IGNORECASE,
)
_RE_DIFFSTAT = re.compile(
    r'^\s*\d+ file.*\d+ (insertion|deletion)',
    flags=re.MULTILINE | re.IGNORECASE,
)

# A trailer line, as b4 matches it: at the start of a line, optionally
# indented, because some maintainers do send their trailers with leading
# whitespace.  The name must begin with a word character, which is what
# keeps a quoted "> Reviewed-by:" out without having to parse quoting
# styles at all.
_RE_TRAILER_LINE = re.compile(r'^\s*(\w\S+):\s+(\S.*)$')

# An "[for the foo bits]" line continuing the trailer above it.
_RE_TRAILER_EXTINFO = re.compile(r'^\s*\[[^]]+]\s*$')

# Values that look like trailers but never are.
_TRAILER_IGNORES = frozenset(
    {'phone', 'mail', 'email', 'e-mail', 'prerequisite-message-id'}
)

# Only meaningful as in-body git headers, and only before any other text.
_TRAILER_HEADERS = frozenset({'subject', 'date', 'from'})

# Trailers whose value is a URL rather than a person.
_TRAILER_LINKS = frozenset({'link', 'buglink', 'closes'})

# Trailers that legitimately carry no email address.
_TRAILER_NONPERSON = _TRAILER_LINKS | {
    'fixes',
    'subject',
    'date',
    'obsoleted-by',
    'change-id',
    'base-commit',
}

_RE_VALUE_EMAIL = re.compile(r'\S+@\S+\.\S+')
_RE_VALUE_LINK = re.compile(r'https?://')


class BodyParts(NamedTuple):
    """The pieces a patch or reply body splits into.

    *githeaders* are the in-body ``From:``/``Subject:``/``Date:`` headers
    git recognises at the very start of a patch, *trailers* are the
    trailing ``Name: value`` block, *basement* is everything below the
    ``---`` separator (diffstat and diff), and *signature* is the text
    below a conformant ``-- `` marker.
    """

    githeaders: list[tuple[str, str]]
    message: str
    trailers: list[tuple[str, str]]
    basement: str
    signature: str


def find_trailers(
    text: str, followup: bool = False
) -> tuple[list[tuple[str, str]], list[str]]:
    """Find ``Name: value`` trailers in a block of text.

    A trailer starts at the beginning of a line and may be indented, since
    some maintainers send theirs with leading whitespace.  A quoted one
    is still excluded, because a quoting marker such as ``>`` is not a
    word character.

    With *followup* set, the much stricter rules b4 applies to replies
    are used: the name must be ASCII, the value must contain an email
    address unless the trailer is a kind that legitimately has none, and
    a URL is only allowed in a link trailer.  That strictness is what
    makes it safe to scan a whole reply rather than just its last
    paragraph, which is how a ``Reviewed-by:`` written in the middle of
    a reply still gets found.

    Returns ``(trailers, other_lines)``.  Mail clients like to wrap long
    trailers, so a ``Fixes:`` hash comment or a bare ``<address>`` that
    spilled onto the next line is rejoined before scanning.  Trailing
    ``# extra info`` is stripped from values, which also means the same
    trailer with and without a comment deduplicates to one entry.
    """
    # Ignore everything below the standard email signature marker.
    text = text.split('\n-- \n', 1)[0].strip() + '\n'
    # Fixes: abcd0123 (foo bar
    # baz quux)
    text = re.sub(
        r'^(\S+:\s+[\da-f]+\s+\([^)]+)\n([^\n]+\))', r'\1 \2', text, flags=re.MULTILINE
    )
    # Signed-off-by: Long Name
    # <email.here@example.com>
    text = re.sub(r'^(\S+:\s+[^<]+)\n(<[^>]+>)$', r'\1 \2', text, flags=re.MULTILINE)

    trailers: list[tuple[str, str]] = []
    others: list[str] = []
    was_trailer = False
    for line in text.split('\n'):
        line = line.strip('\r')
        match = _RE_TRAILER_LINE.match(line)
        if match:
            name, value = match.group(1), match.group(2)
            lname = name.lower()
            if lname in _TRAILER_IGNORES:
                continue
            if others and lname in _TRAILER_HEADERS:
                # A git header only counts before any other content.
                continue
            if followup:
                if not lname.isascii():
                    continue
                if (
                    not _RE_VALUE_EMAIL.search(value)
                    and lname not in _TRAILER_NONPERSON
                ):
                    # A person trailer has to name a person.
                    continue
                if _RE_VALUE_LINK.search(value) and lname not in _TRAILER_LINKS:
                    continue
            if lname == 'fixes' and not re.match(r'^[0-9a-f]{6,}', value):
                continue
            extinfo = re.search(r'(.*\S+)(\s+#[^#]+)$', value)
            if extinfo:
                value = extinfo.group(1)
            was_trailer = True
            trailers.append((name, value.strip()))
            continue
        if len(line) > 2 and was_trailer and _RE_TRAILER_EXTINFO.match(line):
            # Continuation of the trailer above, not new content.
            was_trailer = False
            continue
        was_trailer = False
        others.append(line)

    return trailers, others


def body_has_diff(body: str) -> bool:
    """Whether a body carries actual diff content, as opposed to quoting it."""
    return bool(_RE_DIFF.search(body) or _RE_DIFFSTAT.search(body))


def split_body_parts(body: str) -> BodyParts:
    """Split a message body into git headers, message, trailers and diff.

    Trailers are taken from the *last* paragraph of the message and
    in-body git headers from the *first*, which is what keeps an
    ordinary commit subject such as ``usb: fix the thing`` from being
    read as a trailer named ``usb`` -- it sits in the first paragraph,
    not the trailing block.
    """
    body = body.replace('\r', '').strip('\n')
    signature = ''
    sparts = body.rsplit('\n-- \n', 1)
    if len(sparts) > 1:
        signature = sparts[1]
        body = sparts[0]

    basement = ''
    parts = [body]
    # Only split on "---" for messages that actually carry a diff; in a
    # free-form reply it is just a visual separator.
    if body_has_diff(body):
        parts = re.split(r'^---\s*\n', body, maxsplit=1, flags=re.MULTILINE)
        if len(parts) == 2:
            basement = parts[1]
        elif body.find('\ndiff ') >= 0:
            parts = body.split('\ndiff ', 1)
            if len(parts) == 2:
                parts[1] = 'diff ' + parts[1]
                basement = parts[1]
        elif body.find('\n--- a/') >= 0:
            # Patches generated by some really peculiar tools.
            parts = body.split('\n--- a/', 1)
            if len(parts) == 2:
                parts[1] = '--- a/' + parts[1]
                basement = parts[1]

    paragraphs = parts[0].strip('\n').split('\n\n')

    githeaders: list[tuple[str, str]] = []
    mparts: list[str] = []
    head, other = find_trailers(paragraphs[0])
    if [line for line in other if line.strip()]:
        # Not every line was a header, so it is ordinary content.
        mparts.append(paragraphs[0])
    else:
        githeaders = head

    trailers, nlines = find_trailers(paragraphs[-1])

    if len(paragraphs) == 1:
        if githeaders == trailers:
            # A message that consists of nothing but trailers.
            githeaders = []
        return BodyParts(githeaders, '\n'.join(nlines), trailers, basement, signature)

    if len(paragraphs) > 2:
        mparts += paragraphs[1:-1]
    if nlines:
        mparts.append('\n'.join(nlines))

    return BodyParts(githeaders, '\n\n'.join(mparts), trailers, basement, signature)


def msg_get_inbody_author(msg: EmailMessage) -> tuple[str, str] | None:
    """Return ``(name, email)`` from a git-style in-body ``From:`` line.

    ``git mailinfo`` lets a patch name its real author by starting the
    body with a ``From:`` line, which overrides the header.  This is how
    a patch keeps correct authorship when it is relayed through a
    remailer or a mailing list that rewrites the ``From:`` header, so
    the header alone cannot be trusted to name the author.

    Returns ``None`` when there is no in-body ``From:``.
    """
    parts = split_body_parts(msg_get_payload(msg, strip_signature=False))
    for name, value in parts.githeaders:
        if name.lower() == 'from':
            addrs = email.utils.getaddresses([clean_header(value)])
            if addrs and addrs[0][1]:
                return addrs[0]
    return None


def msg_get_author(msg: EmailMessage, use_inbody: bool = False) -> tuple[str, str]:
    """Return ``(name, email)`` for whoever sent the message.

    Lists that rewrite From to survive DMARC keep the original sender in
    ``X-Original-From``, which is preferred over the rewritten header
    so that the person is reported rather than the mailing list.  The
    header is unauthenticated -- anybody can set it, and liblore does no
    DKIM by design -- so this reports who the message claims to be
    from.  Code that needs proof wants b4's attestation, which checks
    the header against a signature before believing it.

    When *use_inbody* is ``True``, a git-style in-body ``From:`` line
    takes precedence over both, matching what ``git am`` would record
    as the author.  See :func:`msg_get_inbody_author`.
    """
    if use_inbody:
        inbody = msg_get_inbody_author(msg)
        if inbody is not None:
            return inbody
    author = ('', 'missing@address.local')
    for header in ('x-original-from', 'from'):
        fh = msg.get(header)
        if fh:
            addrs = email.utils.getaddresses([clean_header(fh)])
            if addrs and addrs[0][1]:
                return addrs[0]
    return author


def msg_get_payload(
    msg: EmailMessage,
    strip_quoted: bool = False,
    strip_signature: bool = True,
) -> str:
    """Return the plain-text body of a message.

    Optionally strips the email signature (``-- `` marker) and/or
    quoted lines (lines starting with ``> ``).
    """
    mp = msg.get_body(preferencelist=('plain',))
    if mp is None:
        return ''
    bbody = mp.get_payload(decode=True)
    if bbody is None:
        return ''
    cs = mp.get_content_charset()
    if not cs:
        cs = 'utf-8'
    if isinstance(bbody, bytes):
        try:
            cpay = bbody.decode(cs, errors='replace')
        except LookupError:
            # A charset Python has no codec for: ``charset="yes"`` and
            # friends do turn up on the lists.  ``errors='replace'`` is no
            # help here, because it is the codec name that fails to
            # resolve, before any byte is looked at.  Fall back to utf-8
            # and hope for the best, the way b4 does for message bodies
            # and :func:`clean_header` already does for headers.
            cpay = bbody.decode('utf-8', errors='replace')
    else:
        cpay = str(bbody)

    if strip_signature:
        chunks = cpay.rsplit('\n-- \n', maxsplit=1)
        cbody = chunks[0]
    else:
        cbody = cpay

    if not strip_quoted:
        return cbody

    stripped: list[str] = []
    for line in cbody.splitlines():
        if not line.startswith('> '):
            stripped.append(line)
    return '\n'.join(stripped)


# Adapted from email._parseaddr — RFC 5322 specials that require quoting
_QSPECIALS = re.compile(r'[()<>@,:;.\"\[\]]')


def format_addrs(pairs: list[tuple[str, str]], clean: bool = True) -> str:
    """Format ``(name, email)`` pairs into an RFC 5322 address string.

    Works around CPython issue #100900 where ``formataddr`` mis-quotes
    display names containing RFC 5322 special characters.  When *clean*
    is ``True`` (the default), RFC 2047 encoded names are decoded first
    via :func:`clean_header`.
    """
    addrs: list[str] = []
    for name, addr in pairs:
        if not name or name == addr:
            addrs.append(addr)
            continue
        if clean:
            name = clean_header(name)
        # Work around https://github.com/python/cpython/issues/100900
        if (
            not name.startswith('=?')
            and not name.startswith('"')
            and _QSPECIALS.search(name)
        ):
            addrs.append(f'"{email.utils.quote(name)}" <{addr}>')
            continue
        if name.isascii():
            addrs.append(email.utils.formataddr((name, addr)))
        else:
            # Avoid email.utils.formataddr re-encoding unicode to RFC 2047
            addrs.append(f'{name} <{addr}>')
    return ', '.join(addrs)


def wrap_header(hdr: tuple[str, str], width: int = 75, nl: str = '\n') -> bytes:
    """Wrap and RFC 2047-encode a single header for outbound email.

    Address headers (To, Cc, From, X-Original-From) are parsed and each
    address is encoded individually.  Non-address headers are
    word-wrapped (ASCII) or RFC 2047 quoted-printable encoded (non-ASCII).

    *nl* controls the line ending: ``'\\n'`` for dry-run display,
    ``'\\r\\n'`` for SMTP transmission.
    """
    hname, hval = hdr
    _parts: list[str] = []
    if hname.lower() in ('to', 'cc', 'from', 'x-original-from'):
        _parts.append(f'{hname}: ')
        first = True
        for addr in email.utils.getaddresses([hval]):
            if not addr[0].isascii():
                enc_name = email.quoprimime.header_encode(
                    addr[0].encode(), charset='utf-8'
                )
                qp = format_addrs([(enc_name, addr[1])], clean=False)
            else:
                qp = format_addrs([addr], clean=False)
            if first:
                _parts[-1] += qp
                first = False
                continue
            if len(_parts[-1] + ', ' + qp) > width:
                _parts[-1] += ', '
                _parts.append(qp)
                continue
            _parts[-1] += ', ' + qp
    else:
        hdata = f'{hname}: {hval}'
        if hval.isascii():
            if len(hdata) <= width:
                return hdata.encode()
            # Trick: replace ': ' with ':_' so textwrap doesn't break there
            hdata = hdata.replace(': ', ':_', 1)
            wrapped = textwrap.wrap(
                hdata,
                break_long_words=False,
                break_on_hyphens=False,
                subsequent_indent=' ',
                width=width,
            )
            return nl.join(wrapped).replace(':_', ': ', 1).encode()

        # Non-ASCII: encode as RFC 2047 quoted-printable
        qp = f'{hname}: ' + email.quoprimime.header_encode(
            hval.encode(), charset='utf-8'
        )
        if len(qp) <= width:
            return qp.encode()

        while len(qp) > width:
            wrapat = width - 2
            if len(_parts):
                wrapat -= 1
            # Don't break in the middle of a =XX escape sequence
            while '=' in qp[wrapat - 2 : wrapat]:
                wrapat -= 1
            _parts.append(qp[:wrapat] + '?=')
            qp = '=?utf-8?q?' + qp[wrapat:]
        _parts.append(qp)
    return f'{nl} '.join(_parts).encode()


def get_msg_as_bytes(msg: EmailMessage, nl: str = '\n') -> bytes:
    """Serialize a message with proper header encoding for SMTP.

    Uses :func:`wrap_header` for RFC 2047 encoding and line wrapping
    instead of Python's buggy ``as_bytes()``.

    *nl* controls the line ending: ``'\\n'`` for dry-run display,
    ``'\\r\\n'`` for SMTP transmission.
    """
    bdata = b''
    for hname, hval in msg.items():
        bdata += wrap_header((hname, str(hval)), nl=nl) + nl.encode()
    bdata += nl.encode()
    payload = msg.get_payload(decode=True)
    if not isinstance(payload, bytes):
        payload = str(msg.get_payload() or '').encode('utf-8', errors='replace')
    for bline in payload.split(b'\n'):
        bdata += re.sub(rb'[\r\n]*$', b'', bline) + nl.encode()
    return bdata


def msg_get_recipients(msg: EmailMessage) -> set[str]:
    """Return the set of lowercased email addresses from To, Cc, and From."""
    pairs: list[tuple[str, str]] = []
    if msg.get('To'):
        pairs += email.utils.getaddresses([str(x) for x in msg.get_all('to', [])])
    if msg.get('Cc'):
        pairs += email.utils.getaddresses([str(x) for x in msg.get_all('cc', [])])
    if msg.get('From'):
        pairs += email.utils.getaddresses([str(x) for x in msg.get_all('from', [])])
    return {x[1].lower() for x in pairs}


def msg_get_date(msg: EmailMessage) -> datetime.datetime | None:
    """Return the message timestamp, preferring Received over Date.

    Uses the latest ``Received`` header timestamp when one is present,
    because those are stamped by the receiving servers rather than by
    the sender, and falls back to the client-set ``Date`` header.
    Returns ``None`` when neither yields a parsable timestamp.
    """
    latest_rdt: datetime.datetime | None = None
    for rhdr in msg.get_all('Received', []):
        # Received headers generally end with "; datetimeinfo"
        chunks = str(rhdr).rsplit(';', maxsplit=1)
        if len(chunks) < 2:
            continue
        rdate = chunks[1].strip()
        try:
            rdt = email.utils.parsedate_to_datetime(rdate)
        except (ValueError, TypeError):
            continue
        if not latest_rdt or latest_rdt < rdt:
            latest_rdt = rdt
    if not latest_rdt and msg.get('Date'):
        try:
            latest_rdt = email.utils.parsedate_to_datetime(str(msg.get('Date')))
        except (ValueError, TypeError):
            pass
    return latest_rdt


def sort_msgs_by_received(msgs: list[EmailMessage]) -> list[EmailMessage]:
    """Sort messages by latest Received header timestamp, falling back to Date."""
    tosort: list[tuple[datetime.datetime, EmailMessage]] = []
    for msg in msgs:
        latest_rdt = msg_get_date(msg)
        if not latest_rdt:
            logger.debug('Message without a date: %s!', msg.get('Message-ID'))
            continue
        tosort.append((latest_rdt, msg))

    return [msg for _rdt, msg in sorted(tosort, key=lambda x: x[0])]


# =====================================================================
# Thread filtering
# =====================================================================
def get_strict_thread(
    msgs: list[EmailMessage],
    msgid: str,
    noparent: bool = False,
) -> list[EmailMessage] | None:
    """Walk the reference graph and return only messages belonging to a thread.

    Starting from *msgid*, follows In-Reply-To and References headers to
    collect all messages that are part of the same thread.  Messages
    outside the thread are discarded.

    When *noparent* is ``True``, the parent references of *msgid* are
    ignored, effectively breaking the thread at that point.  This is
    useful for standalone patches posted in the middle of an unrelated
    discussion.

    Returns ``None`` when no messages match.
    """
    # Build msgid -> message map
    msgid_map: dict[str, EmailMessage] = {}
    for msg in msgs:
        c_msgid = get_clean_msgid(msg)
        if c_msgid is not None:
            msgid_map[c_msgid] = msg

    want: set[str] = {msgid}
    ignore: set[str] = set()
    got: set[str] = set()
    seen: set[str] = set()
    maybe: dict[str, set[str]] = {}
    strict: list[EmailMessage] = []

    while True:
        for c_msgid, msg in msgid_map.items():
            if c_msgid in ignore:
                continue
            seen.add(c_msgid)
            if c_msgid in got:
                continue

            refs: set[str] = set()
            raw_refs: list[str] = []
            for hdr_name in ('in-reply-to', 'references'):
                for hval in msg.get_all(hdr_name, []):
                    raw_refs += re.findall(r'<([^>]+)>', str(hval))

            # Some tooling emits messages that reference their own Message-ID.
            # A self-reference is never meaningful for threading, so drop it
            # before it can be mistaken for a parent.
            if c_msgid in raw_refs:
                logger.debug('Ignoring self-reference in %s', c_msgid)
                raw_refs = [ref for ref in raw_refs if ref != c_msgid]

            # If noparent, pretend our target message has no references
            if noparent and msgid == c_msgid:
                logger.info('Breaking thread to remove parents of %s', msgid)
                ignore = set(raw_refs)
                raw_refs = []

            for ref in set(raw_refs):
                if ref in ignore:
                    continue
                if ref in got or ref in want:
                    want.add(c_msgid)
                elif len(ref):
                    refs.add(ref)
                    if c_msgid not in want:
                        if ref not in maybe:
                            maybe[ref] = set()
                        maybe[ref].add(c_msgid)

            if c_msgid in want:
                strict.append(msg)
                got.add(c_msgid)
                want.update(refs)
                want.discard(c_msgid)
                if c_msgid in maybe:
                    want.update(maybe[c_msgid])
                    maybe.pop(c_msgid)
                for ref in refs:
                    if ref in maybe:
                        want.update(maybe[ref])
                        maybe.pop(ref)

        # Remove entries that are missing or already collected
        for c_msgid in set(want):
            if c_msgid not in seen or c_msgid in got:
                want.remove(c_msgid)
        if not want:
            break

    if not strict:
        return None

    if len(msgs) > len(strict):
        logger.debug(
            'Reduced thread to requested matches only (%s->%s)',
            len(msgs),
            len(strict),
        )

    return strict


# =====================================================================
# Thread minimization
# =====================================================================

# Detect diff and diffstat content — messages containing these are
# preserved verbatim by minimize_thread().
DIFF_RE = re.compile(r'^diff\s', re.MULTILINE)
DIFFSTAT_RE = re.compile(r'^\s*\d+ file.*changed', re.MULTILINE)

# Default set of headers to keep when minimizing messages.
MINIMIZE_KEEP_HEADERS: tuple[str, ...] = (
    'From',
    'To',
    'Cc',
    'Subject',
    'Date',
    'Message-ID',
    'Reply-To',
    'In-Reply-To',
)


def minimize_thread(
    msgs: list[EmailMessage],
    keep_headers: Sequence[str] | None = None,
    reduce_quote_context: bool = False,
) -> list[EmailMessage]:
    """Reduce thread messages to essential headers and clean up excessive quoting.

    Creates lightweight copies of each message, keeping only the headers
    listed in *keep_headers* (defaults to :data:`MINIMIZE_KEEP_HEADERS`).

    For non-patch messages the body is processed to remove:
    - Multi-level quotes (``>>`` and deeper)
    - Empty quote-only lines (bare ``>``)
    - Trailing quoted blocks at the bottom of the message

    When *reduce_quote_context* is ``True``, quoted blocks that precede
    a reply are further reduced to just the last paragraph, with
    earlier paragraphs replaced by a ``> [... skip NN lines ...]``
    marker.  This only kicks in when more than 5 lines would be
    skipped, so short quotes are left untouched.

    Compliant email signatures (``-- `` marker) are split off before
    quote processing and re-appended afterwards, so that a trailing
    quoted block followed only by a signature is correctly recognised
    as a bottom quote and dropped.

    Messages containing diffs or diffstats are preserved as-is.
    Messages that become empty after minimization are dropped.
    """
    if keep_headers is None:
        keep_headers = MINIMIZE_KEEP_HEADERS

    mmsgs: list[EmailMessage] = []
    for msg in msgs:
        mmsg = EmailMessage()
        for hdr in keep_headers:
            val = clean_header(msg[hdr])
            if val:
                mmsg[hdr] = val

        body = msg_get_payload(msg, strip_signature=False)

        if (
            not re.search(r'^>', body, re.MULTILINE)
            or DIFF_RE.search(body)
            or DIFFSTAT_RE.search(body)
        ):
            mmsg.set_payload(body, charset='utf-8')
            mmsgs.append(mmsg)
            continue

        # Split off the signature before quote processing so trailing
        # quoted blocks before the sig are recognised as bottom quotes.
        sig = ''
        sig_parts = body.rsplit('\n-- \n', maxsplit=1)
        if len(sig_parts) == 2:
            body = sig_parts[0]
            sig = '\n-- \n' + sig_parts[1]

        # Split lines into alternating quoted / unquoted chunks
        chunks: list[tuple[bool, list[str]]] = []
        chunk: list[str] = []
        current: bool | None = None
        for line in body.rstrip().splitlines():
            quoted = line.startswith('>')
            if current is None:
                current = quoted
            if current == quoted:
                if quoted and re.search(r'^>\s*>', line):
                    # Strip multi-level quotes
                    continue
                if quoted and not chunk and line.strip() == '>':
                    # Strip leading empty quote lines
                    continue
                chunk.append(line)
                continue

            # Flush previous chunk, trimming trailing empty quote lines
            if current:
                while chunk and chunk[-1].strip() == '>':
                    chunk.pop(-1)
            if chunk:
                chunks.append((current, chunk))
            chunk = []
            if not (quoted and line.strip() == '>'):
                chunk.append(line)
            current = quoted

        if current is None:
            current = False

        # Don't append trailing quoted chunks
        if chunk and not current:
            chunks.append((current, chunk))

        # Reduce long quoted blocks to just the last paragraph
        if reduce_quote_context:
            for idx, (is_quoted, qchunk) in enumerate(chunks):
                if not is_quoted:
                    continue
                # Only reduce quoted chunks followed by an unquoted reply
                if idx + 1 >= len(chunks) or chunks[idx + 1][0]:
                    continue
                # Find the last paragraph boundary (empty quote line)
                last_break = -1
                for li, line in enumerate(qchunk):
                    if line.strip() in ('>', ''):
                        last_break = li
                if last_break <= 0:
                    continue
                skipped = last_break
                if skipped <= 5:
                    continue
                last_para = qchunk[last_break + 1 :]
                chunks[idx] = (
                    True,
                    [
                        f'> [... skip {skipped} lines ...]',
                        '>',
                    ]
                    + last_para,
                )

        parts: list[str] = []
        for _quoted, chunk in chunks:
            parts.append('\n'.join(chunk))
        body = '\n'.join(parts).strip() + '\n'

        if not body.strip():
            continue

        # Re-append the signature
        if sig:
            body = body.rstrip('\n') + sig

        mmsg.set_payload(body, charset='utf-8')
        mmsgs.append(mmsg)

    return mmsgs


# =====================================================================
# Mbox splitting and deduplication
# =====================================================================
def _is_from_line(line: bytes) -> bool:
    """Check whether *line* is an mbox ``From `` separator."""
    return len(line) >= 20 and _FROM_RE.match(line) is not None


def _get_raw_header(raw: bytes, name: str) -> str | None:
    """Extract a header value from raw message bytes without full parsing.

    Performs a case-insensitive scan of the header block (up to the
    first blank line).  Handles folded headers (continuation lines
    starting with whitespace).
    """
    prefix = name.lower().encode() + b':'
    hdr_end = raw.find(b'\n\n')
    header_block = raw[:hdr_end] if hdr_end >= 0 else raw

    value: bytes | None = None
    for line in header_block.split(b'\n'):
        if value is not None and line and line[:1] in (b' ', b'\t'):
            value += b' ' + line.strip()
            continue
        if value is not None:
            break
        if line.lower().startswith(prefix):
            value = line[len(prefix) :].strip()

    if value is not None:
        decoded: str = value.decode('ascii', errors='replace')
        return decoded
    return None


def _clean_msgid_raw(raw: bytes, header: str = 'message-id') -> str | None:
    """Extract a clean message ID (without angle brackets) from raw bytes."""
    val = _get_raw_header(raw, header)
    if val:
        match = re.search(r'<([^>]+)>', val)
        if match:
            msgid: str = match.group(1)
            return msgid
    return None


def split_mbox_as_bytes(mbox_bytes: bytes) -> list[bytes]:
    """Split mboxrd bytes into a list of raw message byte strings.

    Handles the mboxrd variant used by public-inbox: for every body
    line that matches ``>+From ``, one leading ``>`` is stripped.
    """
    if not mbox_bytes:
        return []

    msgs: list[bytes] = []
    current: list[bytes] = []
    in_header = False

    for raw_line in mbox_bytes.split(b'\n'):
        line = raw_line + b'\n'

        if _is_from_line(line):
            # Flush the previous message
            if current:
                msgs.append(b''.join(current))
                current = []
            in_header = True
            continue

        if in_header and line == b'\n':
            # End of headers, start of body
            in_header = False
            current.append(line)
            continue

        if not in_header and _GTFROM_RE.match(line):
            # Undo one level of mboxrd >From escaping
            line = line[1:]

        current.append(line)

    # Flush the last message
    if current:
        msgs.append(b''.join(current))

    return msgs


def split_mbox(mbox_bytes: bytes) -> list[EmailMessage]:
    """Split mboxrd bytes into a list of parsed EmailMessage objects.

    Handles the mboxrd variant used by public-inbox: for every body
    line that matches ``>+From ``, one leading ``>`` is stripped.
    """
    return [parse_message(raw) for raw in split_mbox_as_bytes(mbox_bytes)]


# Default List-Id preference order.  Sources listed first are less
# likely to mangle messages (e.g. rewrite From: or add footers), so
# they are preferred when the same message arrives from multiple lists.
DEFAULT_LISTID_PREFERENCE: list[str] = [
    '*.feeds.kernel.org',
    '*.linux.dev',
    '*.kernel.org',
    '*',
]


def _listid_preference_index(
    listid: str | None,
    listid_preference: list[str],
) -> int:
    """Return the preference index for a List-Id value.

    Lower is better.  Falls back to the ``*`` wildcard position, or
    one past the end if no wildcard is present.
    """
    if listid:
        for idx, pattern in enumerate(listid_preference):
            if fnmatch.fnmatch(listid, pattern):
                return idx
    try:
        return listid_preference.index('*')
    except ValueError:
        return len(listid_preference)


def get_preferred_duplicate(
    msg1: EmailMessage,
    msg2: EmailMessage,
    listid_preference: list[str] | None = None,
) -> EmailMessage:
    """Return the preferred copy of a duplicate message.

    When the same message (same Message-ID) arrives from multiple
    mailing lists, some lists mangle it (Mailman 2 rewrites From:,
    adds footers, etc.).  This function picks the copy from the
    source least likely to mangle, based on the ``List-Id`` header
    and the *listid_preference* glob patterns.

    Patterns earlier in the list are preferred.  The default order
    matches b4's ``listid-preference`` setting.
    """
    if listid_preference is None:
        listid_preference = DEFAULT_LISTID_PREFERENCE

    idx1 = _listid_preference_index(get_clean_msgid(msg1, 'List-Id'), listid_preference)
    idx2 = _listid_preference_index(get_clean_msgid(msg2, 'List-Id'), listid_preference)
    if idx1 <= idx2:
        logger.debug(
            'Picked duplicate from preferred source: %s',
            get_clean_msgid(msg1, 'List-Id'),
        )
        return msg1
    logger.debug(
        'Picked duplicate from preferred source: %s', get_clean_msgid(msg2, 'List-Id')
    )
    return msg2


def split_and_dedupe_as_bytes(
    mbox_bytes: bytes,
    listid_preference: list[str] | None = None,
) -> list[bytes]:
    """Split mboxrd bytes and deduplicate by Message-ID, returning raw bytes.

    Like :func:`split_and_dedupe` but avoids parsing messages into
    :class:`~email.message.EmailMessage` objects.  Header inspection
    is done directly on raw bytes.

    When duplicate Message-IDs are found, the copy from the
    preferred source is kept (based on ``List-Id`` header matching
    against *listid_preference* patterns).  Pass an empty list to
    disable preference logic and keep the first occurrence.

    Messages without a Message-ID are silently dropped.
    """
    if listid_preference is None:
        listid_preference = DEFAULT_LISTID_PREFERENCE
    use_preference = len(listid_preference) > 0

    chunks = split_mbox_as_bytes(mbox_bytes)
    deduped: dict[str, bytes] = {}
    for raw in chunks:
        msgid = _clean_msgid_raw(raw)
        if msgid is None:
            logger.debug(
                'No message-id found, ignoring %s',
                _get_raw_header(raw, 'subject') or '(no subject)',
            )
            continue
        if msgid in deduped:
            if use_preference:
                idx_old = _listid_preference_index(
                    _clean_msgid_raw(deduped[msgid], 'list-id'),
                    listid_preference,
                )
                idx_new = _listid_preference_index(
                    _clean_msgid_raw(raw, 'list-id'),
                    listid_preference,
                )
                if idx_new < idx_old:
                    deduped[msgid] = raw
            else:
                logger.debug('Dropping duplicate message-id: %s', msgid)
            continue
        deduped[msgid] = raw
    return list(deduped.values())


def split_and_dedupe(
    mbox_bytes: bytes,
    listid_preference: list[str] | None = None,
) -> list[EmailMessage]:
    """Split mboxrd bytes and deduplicate by Message-ID.

    When duplicate Message-IDs are found, the copy from the
    preferred source is kept (based on ``List-Id`` header matching
    against *listid_preference* patterns).  Pass an empty list to
    disable preference logic and keep the first occurrence.

    Messages without a Message-ID are silently dropped.
    """
    return [
        parse_message(raw)
        for raw in split_and_dedupe_as_bytes(mbox_bytes, listid_preference)
    ]


# =====================================================================
# Pure helpers
# =====================================================================


def get_msgid_from_url(url: str) -> str:
    """Extract a message ID from a lore.kernel.org URL.

    If the input is already a bare message ID (no ``://``), it is
    returned with angle brackets stripped.
    """
    if '://' in url:
        matches = re.search(
            r'^https?://[^@]+/([^/]+(?:@|%40)[^/]+)', url, re.IGNORECASE
        )
        if matches:
            return urllib.parse.unquote(matches.groups()[0])
    return url.strip('<>')
