# liblore

A Python library for working with [public-inbox](https://public-inbox.org/)
servers, particularly [lore.kernel.org](https://lore.kernel.org/). It fetches
email threads, parses mbox files, summarises patch series, and provides
utilities for working with email messages from mailing list archives.

## Requirements

- Python 3.9 or newer
- `requests` >= 2.31
- `authheaders` >= 0.15 (optional, for DKIM/DMARC/ARC verification)

## Installation

Install from PyPI:

```shell
pip install liblore
```

To include optional email authentication support (DKIM, DMARC, ARC):

```shell
pip install liblore[auth]
```

Or install from source:

```shell
pip install .
```

## Quick Start

The main entry point is the `LoreNode` class. It connects to a public-inbox
endpoint and lets you fetch threads, search for messages, and work with raw
mbox data. Use it as a context manager so the underlying HTTP session is
cleaned up automatically:

```python
from liblore import LoreNode

with LoreNode('https://lore.kernel.org/all') as node:
    msgs = node.get_thread_by_msgid(
        '20250101-example@kernel.org',
        sort=True,
    )
    for msg in msgs:
        print(msg['Subject'])
```

If you omit the URL, it defaults to `https://lore.kernel.org/all`.

## API Reference

### LoreNode

```python
from liblore import LoreNode

node = LoreNode(url='https://lore.kernel.org/all')
```

#### Git Config Integration

The easiest way to create a `LoreNode` is via `from_git_config()`, which reads
settings from the `[lore]` section of your git config (repo, global, or
system). This gives you per-repository overrides for free -- a subsystem
maintainer with a local mirror just adds settings to their repo's
`.git/config`:

```python
with LoreNode.from_git_config() as node:
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
```

Supported git config keys:

```ini
[lore]
    # Origin URLs to try before the canonical URL (multi-valued, in order)
    fallback = https://tor.lore.kernel.org
    fallback = https://sea.lore.kernel.org

    # Auto-probe all origins on first request, reorder by latency
    autoprobe = true

    # Per-origin probe timeout in seconds (default: 5.0)
    probetimeout = 5.0

    # How long cached probe results stay valid, in seconds (default: 3600)
    probettl = 3600

    # Unique identifier appended to User-Agent (typically a UUID)
    useragentplus = 550e8400-e29b-41d4-a716-446655440000
```

For any other server, such as a local mirror, put the same keys in a
section named after its origin (`scheme://host[:port]`, with no path).
lore.kernel.org also reads this section first, and uses `[lore]` only when
it is missing:

```ini
[liblore "http://localhost:11043"]
    probetimeout = 2.0
```

All keys are optional. Missing keys, missing git, or any other failure is
silently ignored. Explicit keyword arguments to `from_git_config()` take
precedence over git config values.

#### Fallback URLs

lore.kernel.org uses geodns across multiple nodes. When a node is slow or
unavailable, `fallback_urls` lets you specify alternative servers to try
automatically:

```python
with LoreNode(
    'https://lore.kernel.org/all',
    fallback_urls=[
        'http://mymirror.local',
        'https://tor.lore.kernel.org',
        'https://sea.lore.kernel.org',
    ],
) as node:
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
```

Each fallback is an **origin prefix** (`scheme://host`). The path from the
primary URL is preserved automatically, so `https://lore.kernel.org/all/...`
becomes `http://mymirror.local/all/...`. This supports mixing `https` and
`http` schemes -- useful for local mirrors or `.onion` endpoints where TOR
provides the encryption layer.

On each HTTP request, origins are tried in order. Connection errors, timeouts,
and 5xx responses trigger a fall-through to the next origin. 4xx responses
(the resource genuinely doesn't exist) are returned immediately without
retrying. The `validate()` method intentionally skips fallback and checks only
the canonical URL.

Cache keys always use the canonical URL, so cache hits work regardless of
which mirror served the response.

#### Origin Probing

When you have multiple fallback URLs, `probe_origins()` finds the fastest
mirror by sending a concurrent `HEAD` request to `/manifest.js.gz` on each
origin:

```python
node = LoreNode(
    'https://lore.kernel.org/all',
    fallback_urls=['https://tor.lore.kernel.org', 'https://sea.lore.kernel.org'],
)

# Probe all origins concurrently and reorder by latency
results = node.probe_origins()
for origin, elapsed in results:
    print(f'{origin}: {elapsed * 1000:.0f}ms')
```

Unreachable origins are moved to the end rather than removed, so they can
recover on subsequent requests. Probe results are cached to `cache_dir` (when
set) for `probe_ttl` seconds (default 3600 = 1 hour). Pass `nocache=True` to
force a live probe even when cached results exist (the fresh results are still
written back to cache).

Set `auto_probe=True` to trigger probing transparently on the first request:

```python
with LoreNode(
    'https://lore.kernel.org/all',
    fallback_urls=['https://tor.lore.kernel.org'],
    auto_probe=True,
    cache_dir='/tmp/liblore-cache',
) as node:
    # First request probes, reorders, then fetches via the fastest mirror
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
```

#### Caching

LoreNode can optionally cache raw mbox bytes on disk. Pass `cache_dir` to
enable it:

```python
with LoreNode(cache_dir='/tmp/liblore-cache', cache_ttl=600) as node:
    # First call fetches from the network and writes a cache file
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
    # Second call reads from cache (if within TTL)
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
```

- `cache_dir` -- directory for cache files (`None` to disable, the default)
- `cache_ttl` -- time-to-live in seconds (default 600 = 10 minutes)

Caching is applied to `get_mbox_by_msgid`, `get_mbox_by_query`, and
`get_message_by_msgid`. Polling methods (`get_thread_updates_since`) are
intentionally not cached. TTL is checked on every read, so stale data is
never returned -- even in long-running processes.

Pass `nocache=True` to any cached method to bypass the cache for that call
(the response is still written back to refresh the entry). Call
`node.clear_cache()` to remove all cached entries.

#### Partial Mirrors

A *partial mirror* has only some of an archive: for example a
maintainer's local mirror of the lists they follow. Such a mirror is fast
on a bad connection, and it keeps working when lore.kernel.org is down.
But it doesn't have everything, so "not found" from it doesn't mean the
message doesn't exist.

A partial mirror says so in two response headers:

```
X-Archive-Coverage: partial; updated=1790000000
X-Archive-Upstream: https://lore.kernel.org/all/
```

LoreNode reads them on every answer. There is nothing to configure: point
the node at the mirror, and it learns the rest.

- **The mirror answers first.** When it doesn't have a thread or a message
  (a 404), the node asks the upstream archive. The upstream gets its own
  node, which uses your git config for its origin (fallbacks, probing,
  timeouts).
- **Searches stay local.** A search the mirror answers is final. The
  upstream archive is asked only when the mirror finds nothing. Set
  `partialsearch = upstream` to ask the upstream archive first, if you want
  every hit and don't mind the wait.
- **Polling stays local.** `get_thread_updates_since()` first asks the
  mirror whether it has the thread (a `HEAD` request). For a thread it has,
  its "nothing new" is final, and lore.kernel.org is not contacted. For a
  thread it doesn't have, the upstream archive answers.
- **Outages.** When the mirror can't be reached, the upstream archive
  answers. When the upstream can't be reached, the node doesn't try it
  again for `upstreamholdoff` seconds (default 300), so a command doesn't
  wait for a timeout on every call.
- **Remembered.** With `cache_dir` set, the node saves the upstream it
  learned (in a `*.lore.upstream` file, which `clear_cache()` keeps). So it
  still works the next time, even when the mirror is stopped.

Servers that don't send these headers, lore.kernel.org included, work
exactly as before.

**Where answers came from.** `node.last_source` tells you where the
answer to your last call (in this Python thread) came from:

```python
from liblore import LoreNode, Source

with LoreNode.from_git_config('http://localhost:11043/lore/all') as node:
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
    if node.last_source is Source.UPSTREAM:
        print(f'Not on your mirror, fetched from {node.upstream_url}')
```

- `Source.LOCAL` -- answered by this node (its server or its cache).
- `Source.UPSTREAM` -- answered by the upstream archive.
- `Source.DEGRADED` -- the upstream archive could not be reached, so only
  the mirror answered, and the answer may be incomplete. Only
  `partialsearch = upstream` can give this.

It is `None` before the first call and after a call that raised. A batch
reports the worst source of all its items. To get a source per item, call
the single-item method in your own loop. On a node that is not a partial
mirror, every answer is `LOCAL`.

**Polling with `last_as_of`.** A mirror is always a little behind the
archive it mirrors. Its "nothing new" is true only up to its last update.
If you poll with your own clock as the next `since`, a reply can fall into
that gap and never be reported. Instead, use `node.last_as_of`. It tells
you how far the last answer reaches:

```python
since = datetime.now(timezone.utc) - timedelta(days=1)
while True:
    updates = node.get_thread_updates_since(msgid, since)
    since = node.last_as_of or since
    # Polls overlap a little: skip messages you have already seen.
    ...
```

This works on every node, not only on partial mirrors. On a full archive,
`last_as_of` is the time the request was sent.

**When a mirror sends no `updated` time** (it has never finished an
update), its "nothing new" can't be trusted. Thread updates then come from
the upstream archive.

**Errors.** When the mirror doesn't have something and the upstream
archive can't be reached, you get `NotOnMirrorError`. It is a
`RemoteError`, so code that already handles `RemoteError` keeps working.
When the upstream archive answers "not found", you get a plain
`RemoteError` with `status_code == 404`, as from any other server.

**Trust.** A mirror shared over a VPN could name any host as its upstream,
and your queries would go there. So only `https://lore.kernel.org` is
trusted by default. Add others with `allowupstream` (multi-valued) in git
config, or `allowed_upstreams=[...]` in Python. An upstream that isn't
trusted is ignored, with one warning.

The User-Agent `plus` identifier of the mirror's node (passed to
`set_user_agent()`, or `useragentplus` in the mirror's git config) is not
sent to the upstream archive: it identifies you to one server's operator.
The upstream node uses the `useragentplus` that git config sets for its
own origin, if any. For lore.kernel.org, that is the one in `[lore]`.

**Caching.** The node caches only its own answers. Answers from the
upstream archive are cached by the upstream node, so the next call asks
the mirror again: by then it may have the thread. Answers that may be
incomplete (`DEGRADED`) are never cached. `clear_cache()` clears both.

The settings go in the git config section of the mirror's origin, and
`from_git_config()` reads them:

```ini
[liblore "http://localhost:11043"]
    partialsearch = upstream
    upstreamholdoff = 60
    allowupstream = https://mirror.example.com
```

| git config key    | `LoreNode` argument | Default                                          |
|-------------------|---------------------|--------------------------------------------------|
| `followupstream`  | `follow_upstream`   | `true`; `false` ignores the headers              |
| `partialsearch`   | `partial_search`    | `local`, or `upstream`                           |
| `upstreamholdoff` | `upstream_holdoff`  | `300` seconds; `0` asks the upstream every time  |
| `allowupstream`   | `allowed_upstreams` | none besides `https://lore.kernel.org`           |

#### Fetching Threads

**`node.get_thread_by_msgid(msgid, *, strict=True, sort=False, since=None)`**

Fetch a thread by its message ID. This is the highest-level method and the
one you will reach for most often.

- `strict` (default `True`) -- filter results to only messages that belong
  to the thread rooted at `msgid`. When a query returns messages from
  unrelated threads (common with broad date ranges), strict mode discards
  them.
- `sort` -- sort the returned messages by their `Received` header timestamp.
- `since` -- a date string appended as a `d:` filter. This uses
  public-inbox's approxidate syntax, so you can write things like
  `"20240115"`, `"2.weeks.ago"`, or `"last.month"`.

Returns a `list[EmailMessage]`. Raises `LookupError` if no messages match.

```python
with LoreNode() as node:
    # Fetch a thread, sorted by date, only looking at recent messages
    msgs = node.get_thread_by_msgid(
        '20250101-example@kernel.org',
        strict=True,
        sort=True,
        since='20250101',
    )
```

**`node.get_thread_updates_since(msgid, since, *, strict=True, sort=False)`**

Check whether a thread has new messages since a given point in time. This is
handy for polling use cases where you want to know if anything new has arrived.

- `since` -- a `datetime` object. Converted to a UTC epoch timestamp
  internally and matched against the server-set `Received` header (`rt:`
  prefix), which is more reliable than the client-set `Date` header.
- `strict` (default `True`) -- filter results to only messages belonging to
  the thread rooted at `msgid`.
- `sort` -- sort the returned messages by their `Received` header timestamp.

Returns a `list[EmailMessage]`. Returns an empty list (rather than raising)
when there are no updates.

```python
from datetime import datetime, timedelta, timezone

with LoreNode() as node:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    updates = node.get_thread_updates_since(
        '20250101-example@kernel.org',
        cutoff,
    )
    if updates:
        print(f'{len(updates)} new message(s)')
```

**`node.get_thread_by_query(query, *, full_threads=False)`**

Run a search query and return a deduplicated `list[EmailMessage]`. The query
uses public-inbox's
[Xapian search syntax](https://public-inbox.org/HOWTO#search), which supports
prefixes like `msgid:`, `s:` (subject), `f:` (from), `d:` (date range), and
more.

When `full_threads` is `True`, the server expands results to include the
full thread for every matching message. This is useful when searching by
patch-id or change-id and you need the complete surrounding thread, not just
the matching messages.

```python
with LoreNode() as node:
    # Find all messages from a sender in the last month
    msgs = node.get_thread_by_query('f:alice@example.com d:last.month..')

    # Search by patch-id and fetch the full threads
    msgs = node.get_thread_by_query('patchid:abc123', full_threads=True)
```

#### Batch Fetching

When you need to fetch multiple threads, the batch methods handle the loop for
you and add a 100 ms cooldown between requests so you're being a good citizen
to the server.

**`node.batch_get_thread_by_msgid(msgids, *, strict=True, sort=False, since=None)`**

Fetch threads for a list of message IDs. Calls `get_thread_by_msgid()` for
each one with a brief pause between requests. Returns a
`list[list[EmailMessage]]` in the same order as the input.

```python
with LoreNode() as node:
    threads = node.batch_get_thread_by_msgid(
        ['msg1@example.com', 'msg2@example.com', 'msg3@example.com'],
        sort=True,
        since='2.weeks.ago',
    )
    for thread in threads:
        print(f'Thread with {len(thread)} messages')
```

**`node.batch_get_thread_by_query(queries, *, full_threads=False)`**

Run multiple search queries. Same pattern -- calls `get_thread_by_query()` per
query with a 100 ms cooldown. Returns a `list[list[EmailMessage]]`.

```python
with LoreNode() as node:
    results = node.batch_get_thread_by_query(
        [
            's:fix f:alice@example.com',
            's:feature f:bob@example.com',
        ]
    )
```

#### Raw Mbox Access

These methods return raw mbox bytes rather than parsed messages. They are
useful when you need the unprocessed data, or when you want to feed the
output into your own parser.

**`node.get_mbox_by_msgid(msgid, *, nocache=False)`** -- fetch a thread's mbox
by message ID.

**`node.get_mbox_by_query(query, *, full_threads=False, nocache=False)`** --
run a search query and return the matching mbox. Pass `full_threads=True` to
expand results to include full threads.

```python
with LoreNode() as node:
    raw = node.get_mbox_by_msgid('20250101-example@kernel.org')
    with open('thread.mbox', 'wb') as f:
        f.write(raw)
```

#### Single Messages

**`node.get_message_by_msgid(msgid, *, nocache=False)`** -- fetch a single raw
message (bytes) by its message ID. Useful when you need exactly one message rather than an
entire thread.

#### Session Configuration

**`node.set_user_agent(app_name, version, plus=None)`** -- set a custom
`User-Agent` header. Being a good citizen of public infrastructure means
identifying your tool:

```python
node.set_user_agent('my-tool', '1.0')
# User-Agent: my-tool/1.0
```

The optional `plus` argument appends a unique identifier that server operators
can use to identify and prioritize known installations:

```python
node.set_user_agent('my-tool', '1.0', plus='550e8400-e29b-41d4')
# User-Agent: my-tool/1.0+550e8400-e29b-41d4
```

When `plus` is not provided and the node was created via `from_git_config()`,
the value of `lore.useragentplus` from git config is used automatically. This
way a single git config entry identifies the installation across all tools
using liblore:

```python
# With lore.useragentplus = myuuid in ~/.gitconfig:
node = LoreNode.from_git_config()
node.set_user_agent('korgalore', '0.7')
# User-Agent: korgalore/0.7+myuuid
```

**`node.set_requests_session(session)`** -- inject your own
`requests.Session`. Handy when you need custom timeouts, proxies, or
authentication. Note that the session's `User-Agent` is not overwritten
when you provide your own.

**`node.validate()`** -- check that the configured URL actually points to a
public-inbox server. Raises `RemoteError` if it does not.

**`node.close()`** -- close the HTTP session. Called automatically when
using `LoreNode` as a context manager.

#### Cancellation

Fetches can take a while, and interactive applications need a way to bail
out -- the user pressed Escape, or the whole application is exiting. Both
methods are thread-safe and are meant to be called from a different thread
than the one doing the fetching. Cancellation is *operation-scoped*: each
public method call is one operation, and cancelling affects only the
operations that are running at that moment.

**`node.cancel_active()`** -- cancel every operation currently in flight.
Each one raises `OperationCancelledError` (from a worker thread, this
surfaces in whatever way your framework reports worker exceptions) and stays
cancelled for the rest of its run, so a batch stops fully even when the
cancel arrives between its requests. Operations started afterwards are
unaffected -- there is no flag to reset before the next fetch.

```python
import threading
from liblore import LoreNode, OperationCancelledError

node = LoreNode()


def fetch() -> None:
    try:
        node.batch_get_thread_by_msgid(lots_of_msgids)
    except OperationCancelledError:
        print('fetch aborted')


worker = threading.Thread(target=fetch)
worker.start()
# ... user presses Escape:
node.cancel_active()
worker.join()

# The node is immediately usable again -- no reset needed:
msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
```

**`node.shutdown()`** -- cancel everything in flight *and* refuse every
operation started afterwards. This is the terminal state for application
exit: a worker racing against shutdown raises `OperationCancelledError`
immediately instead of opening a new connection and delaying the exit.
There is no way to undo a shutdown. The **`node.is_shutdown`** property
reports whether the node has been shut down.

Cancelling closes the node-owned HTTP session, so a thread blocked in a
socket read is interrupted right away rather than waiting for a timeout. A
session you injected with `set_requests_session()` is left untouched.

Versions before 0.9 used a sticky `cancel()`/`reset_cancel()` pair; both
still work but are deprecated. See `MIGRATIONS.md` for how to move over.

#### Message Authentication

LoreNode can optionally verify DKIM signatures, DMARC alignment, and ARC
chains on every message it retrieves. This requires the `authheaders` package
(install with `pip install liblore[auth]`).

```python
with LoreNode(add_auth_headers=True) as node:
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')
    for msg in msgs:
        print(msg['Authentication-Results'])
        # liblore; dkim=pass header.d=kernel.org; ...
```

When enabled, each returned `EmailMessage` gets an `Authentication-Results`
header added by the [authheaders](https://pypi.org/project/authheaders/)
library. SPF is not checked because archived messages don't carry the SMTP
transaction info (client IP, MAIL FROM, HELO) that SPF requires.

If `add_auth_headers=True` is set but `authheaders` is not installed, a
`LibloreError` is raised immediately on construction.

### How the API Layers Fit Together

The methods build on each other in layers, from raw bytes up to filtered,
sorted thread views:

```
get_mbox_by_msgid / get_mbox_by_query      ->  raw mbox bytes
        |
get_thread_by_query                        ->  split + dedupe -> list[EmailMessage]
        |
get_thread_by_msgid                        ->  strict + sort  -> list[EmailMessage]
        |
get_thread_updates_since                   ->  poll for new   -> list[EmailMessage]
        |
batch_get_thread_by_msgid / batch_get_...  ->  rate-limited loop -> list[list[EmailMessage]]
```

You can tap into whichever layer suits your needs. Need raw bytes for
archiving? Use the `get_mbox_*` methods. Need parsed messages with
deduplication? Use `get_thread_by_query`. Want the full convenience of
strict filtering and date sorting? Use `get_thread_by_msgid`. Need to
poll for new messages? Use `get_thread_updates_since`.

### Thread and Series Summaries

The `liblore.series` module answers one question: *what is in this thread?*
It is pure Python -- no network, no git, no subprocess -- so you hand it the
messages you already fetched and get back plain dataclasses you can filter,
cache or turn into JSON.

It reports facts and leaves the judgements to you. Whether a series arrived
complete, whether you have already applied it, whether two addresses belong
to the same human -- those are your calls. The summary gives you what you
need to make them.

```python
from liblore import LoreNode
from liblore.series import summarize_thread

with LoreNode() as node:
    msgs = node.get_thread_by_msgid('20250101-example@kernel.org')

summary = summarize_thread(msgs)

print(summary.subject)  # subject of the thread root
print(summary.count)  # how many messages are in the thread
print(summary.first, summary.last)  # oldest and newest timestamps
print(summary.is_patch_series)  # did anything carry a [PATCH ...] subject
print(summary.patch_count)  # how many patches were actually seen
print(summary.expected)  # how many the brackets claimed (the m in n/m)
print(summary.version)  # 3 for a [PATCH v3] series, 1 if unstated
print(summary.has_cover)  # was there a 0/m cover letter
print(summary.prefixes)  # extra brackets, e.g. ('RFC', 'net-next')
print(summary.author)  # who opened the thread, as (name, email)
print(summary.participants)  # everybody who sent something, author included
print(summary.repliers)  # everybody except the author
```

`patch_count` and `expected` are deliberately two fields. Comparing them is
how *you* decide whether a series arrived complete -- liblore will not
decide it for you, because "complete" means different things depending on
what you are about to do.

A lone patch with no `n/m` counter counts as a one-patch series. The
`[PATCH` bracket is what makes something a patch here, which is the same
rule b4 uses; a message carrying a diff under some other subject is not
treated as a submission.

#### Messages in Order

`messages` holds the same thread message by message, so you can ask
questions about *order* -- did the review land before or after the last
reply, which patch in the series was tagged, has this person said anything
since.

```python
for msg in summary.messages:
    print(msg.msgid, msg.author, msg.date)
    print(msg.patch.counter, 'of', msg.patch.expected)  # parsed subject
    print(msg.has_diff)  # did this message carry a diff
    print(msg.trailers)  # trailers this message offered
    print(msg.is_submission)  # a patch, and not a reply carrying its brackets
```

The thread root comes first and the rest follow oldest-first. Messages sent
within the same second -- which is every series sent by `git send-email` --
are ordered by their `n/m` counter rather than left in arrival order.

`msgids` runs parallel to `messages`: `msgids[i]` is always
`messages[i].msgid`, and both are `count` entries long. A message with no
Message-Id keeps its place as an empty string, so the two never drift apart.

```python
assert summary.msgids == tuple(m.msgid for m in summary.messages)
assert summary.msgids[0] == summary.msgid  # the thread root leads
```

You can also summarise a single message on its own:

```python
from liblore.series import summarize_message

one = summarize_message(msg)
```

#### Trailers

Every trailer seen anywhere in the thread is reported once, in the order it
first appeared, classified the way b4 classifies them:

```python
for trailer in summary.trailers:
    print(trailer.name)  # 'Reviewed-by', as written
    print(trailer.key)  # 'reviewed-by', for comparing
    print(trailer.value)  # 'Ann Submitter <ann@example.com>'
    print(trailer.type)  # 'person', 'utility' or 'unknown'
    print(trailer.addr)  # ('Ann Submitter', 'ann@example.com'), or None
    print(trailer.msgid)  # which message offered it first
    print(trailer.date)  # and when

summary.person_trailers  # endorsements: Reviewed-by, Acked-by, Tested-by ...
summary.utility_trailers  # bookkeeping: Fixes, Link, Closes ...
summary.trailer_names  # frozenset of every lowercased name seen
```

A reply's endorsement counts only if the sender signs it themselves: you may
tag on your own behalf, not on somebody else's. This is b4's rule, and it
keeps a quoted tag or a pasted device-tree snippet from being read as a
review. Patches are not subject to it -- a patch collects everybody's tags,
and the person sending it signs for them.

This is **not** a signature check. liblore does no DKIM by design, so a
trailer is reported as *claiming* to come from the person it names. If you
need proof, that is what b4's attestation is for.

#### Asking About People

Two spellings of one address are recognised -- an extended local part, and a
domain one label deeper -- which covers most of what a mailing list does to
an address:

```python
from liblore.series import email_matches, name_matches

email_matches('ann+kernel@example.com', 'ann@example.com')  # True
email_matches('ann@linux.intel.com', 'ann@intel.com')  # True
name_matches('Amara Okonkwo', 'Okonkwo, Amara')  # True
name_matches('Bob Reviewer (Arm)', 'Bob Reviewer')  # True
```

Knowing that two *unrelated* addresses belong to one person needs a
directory -- a MAINTAINERS file, a config entry, a mapping you keep
yourself -- so `Identity` is where you hand that in:

```python
from liblore.series import Identity

me = Identity(
    addrs=('ann@example.com', 'ann@corp.example.net'),
    names=('Ann Submitter',),
)

summary.messages_by(me)  # every message this person sent
summary.trailers_by(me)  # every trailer attributed to them
me.matches('ann@example.com')  # True
```

`messages_by()` and `trailers_by()` also take a bare address or a
`(name, email)` pair, in which case only that spelling is matched.

`participants` stays literal: one entry per distinct From address, so
somebody who wrote from two addresses appears twice. That is what the thread
contains, and collapsing them is your decision, not ours.

#### Many Threads at Once

A search result is a flat bag of messages from many threads.
`summarize_threads()` splits it up and summarises each one:

```python
from liblore.series import summarize_threads

with LoreNode() as node:
    msgs = node.get_thread_by_query('s:"net: fix"', months=1)

for summary in summarize_threads(msgs):
    print(summary.subject, summary.count)
```

Grouping walks the In-Reply-To and References headers, so a thread is found
even when its root is missing from the result. If you want the groups
without the summaries, use `group_by_thread()`, which returns a list of
message lists.

#### JSON

Both summaries serialise to plain JSON-safe dictionaries, stamped with a
schema version so stored records can be read back later:

```python
import json
from liblore.series import SCHEMA_VERSION

print(json.dumps(summary.to_dict(), indent=2))
print(SCHEMA_VERSION)
```

Timestamps come out as ISO 8601 strings, and the key set is fixed: a field
with nothing in it is present and empty rather than missing.

#### Parsing a Subject on Its Own

If all you have is a subject line, `parse_patch_subject()` decomposes the
brackets:

```python
from liblore.series import parse_patch_subject

parsed = parse_patch_subject('[PATCH RFC v3 2/5] net: fix the thing')
parsed.title  # 'net: fix the thing'
parsed.version  # 3
parsed.counter  # 2
parsed.expected  # 5
parsed.prefixes  # ('RFC',)
parsed.is_patch  # True
parsed.is_cover  # False -- 0/5 would be True
parsed.is_reply  # True for a 'Re:' subject
parsed.version_inferred  # False here: the subject said v3
parsed.counters_inferred  # False here: the subject said 2/5
```

The `*_inferred` flags let you tell a stated `1/1` from an assumed one,
which matters when a lone patch and a one-patch series need handling
differently.

### Utility Functions

The `liblore.utils` module provides lower-level helpers for parsing and
inspecting email messages.

#### Header Handling

```python
from liblore.utils import clean_header, get_clean_msgid

# Decode RFC 2047 encoded headers
decoded = clean_header('=?utf-8?q?Re=3A_Some_Subject?=')

# Extract a clean message ID (without angle brackets) from a message
msgid = get_clean_msgid(msg)  # reads Message-Id by default
msgid = get_clean_msgid(msg, 'In-Reply-To')  # or any other header
```

#### Parsing Messages

```python
from liblore.utils import parse_message

# Parse raw email bytes into an EmailMessage
msg = parse_message(raw_bytes)
```

#### Extracting Message Content

```python
from liblore.utils import (
    msg_get_subject,
    msg_get_author,
    msg_get_inbody_author,
    msg_get_payload,
    msg_get_recipients,
    strip_reply_prefixes,
)

# Get the decoded subject line
subject = msg_get_subject(msg)

# Strip [PATCH v3 2/5] and Re: prefixes to get the bare subject.
# A subsystem prefix such as "mm:" is part of the subject and stays.
bare = msg_get_subject(msg, strip_prefixes=True)

# Or remove only the reply markers from a subject line you already have
rest, is_reply = strip_reply_prefixes('Re: mm: fix the widget')
# -> ('mm: fix the widget', True)

# Get the author as a (name, email) tuple
name, addr = msg_get_author(msg)

# Lists that rewrite From to survive DMARC keep the original sender in
# X-Original-From, which is preferred over the rewritten header -- so
# this reports the person, not the mailing list.  Note that the header
# is unauthenticated: it says who a message claims to be from.

# Prefer a git-style in-body "From:" line, which is what git am would
# record as the author of a patch sent on somebody else's behalf
name, addr = msg_get_author(msg, use_inbody=True)

# Or read that in-body header on its own; None if the message has none
author = msg_get_inbody_author(msg)

# Get the plain-text body, stripping the signature
body = msg_get_payload(msg)

# Get the body without quoted lines or signature
body = msg_get_payload(msg, strip_quoted=True, strip_signature=True)

# Get all recipient addresses (To + Cc + From), as a lowercased set
recipients = msg_get_recipients(msg)
```

#### Patch Bodies and Trailers

A patch body has structure: in-body git headers at the top, the commit
message, a trailing block of `Name: value` trailers, then everything below
the `---` separator. `split_body_parts()` takes it apart, and both it and
`find_trailers()` are ports of b4's logic, so the corner cases b4 has
learned about are handled the same way.

```python
from liblore.utils import body_has_diff, find_trailers, split_body_parts

# Is this a patch or just a reply?
body_has_diff(body)

parts = split_body_parts(body)
parts.githeaders  # [('From', 'Ann Submitter <ann@example.com>'), ...]
parts.message  # the commit message, without the headers or trailers
parts.trailers  # [('Signed-off-by', 'Ann Submitter <ann@example.com>')]
parts.basement  # everything below the '---': diffstat and diff
parts.signature  # text below a conformant '-- ' marker

# Scan any block of text for trailers.  Returns the trailers it found and
# the lines it could not read as one.
trailers, others = find_trailers(text)

# In followup mode the rules are stricter, for scanning a reply that has
# no patch structure to lean on
trailers, others = find_trailers(reply_body, followup=True)
```

Trailers may be indented, since some maintainers send them that way, but one
behind a `>` quoting marker is somebody else's and does not count.

For most purposes you want `liblore.series` instead, which uses these and
gives you classified `Trailer` objects with the sender check already
applied.

#### Email Serialization

These functions replace Python's buggy `as_bytes()` with battle-tested
serialization that correctly handles RFC 2047 header encoding, line wrapping,
and non-ASCII display names.

```python
from liblore.utils import format_addrs, wrap_header, get_msg_as_bytes

# Format (name, email) pairs into an RFC 5322 address string
formatted = format_addrs(
    [
        ('', 'foo@example.com'),
        ('Foo Bar', 'bar@example.com'),
    ]
)
# -> 'foo@example.com, Foo Bar <bar@example.com>'

# Wrap and RFC 2047-encode a header for SMTP
hdr_bytes = wrap_header(('Subject', 'Hello world'))

# Serialize a full message to bytes with proper encoding
msg_bytes = get_msg_as_bytes(msg)  # \n line endings (dry-run)
msg_bytes = get_msg_as_bytes(msg, nl='\r\n')  # \r\n for SMTP
```

#### Sorting and Threading

```python
from liblore.utils import sort_msgs_by_received, get_strict_thread

# Sort messages by their Received timestamp (falls back to Date)
sorted_msgs = sort_msgs_by_received(msgs)

# Filter a list of messages to only those in a specific thread
thread = get_strict_thread(msgs, '20250101-example@kernel.org')

# Break the thread at msgid, ignoring its parent references
thread = get_strict_thread(msgs, msgid, noparent=True)
```

#### Thread Minimization

```python
from liblore.utils import minimize_thread

# Strip excessive quoting and non-essential headers for compact display
minimized = minimize_thread(msgs)

# Customize which headers to keep
minimized = minimize_thread(msgs, keep_headers=('From', 'Subject', 'Date'))

# Aggressively reduce long quotes to just the last paragraph
minimized = minimize_thread(msgs, reduce_quote_context=True)
```

`minimize_thread()` creates lightweight copies of each message: it keeps only
essential headers (From, To, Cc, Subject, Date, Message-ID, Reply-To,
In-Reply-To by default), strips multi-level quotes and trailing quoted blocks,
and drops messages that become empty after processing. Messages containing
diffs or diffstats are preserved as-is.

When `reduce_quote_context=True`, long quoted blocks preceding a reply are
trimmed to just the last paragraph, with earlier content replaced by a
`> [... skip NN lines ...]` marker.  This only applies when more than 5 lines
would be skipped.

#### Mbox Splitting

```python
from liblore.utils import split_mbox, split_and_dedupe

# Split mboxrd bytes into a list of EmailMessage objects
msgs = split_mbox(mbox_bytes)

# Split and deduplicate by Message-ID (first occurrence wins)
msgs = split_and_dedupe(mbox_bytes)
```

When you need raw message bytes without the cost of parsing, use the
`_as_bytes` variants:

```python
from liblore.utils import split_mbox_as_bytes, split_and_dedupe_as_bytes

# Split mboxrd bytes into a list of raw message byte strings
chunks = split_mbox_as_bytes(mbox_bytes)

# Split, deduplicate, and return raw bytes (no email parsing)
chunks = split_and_dedupe_as_bytes(mbox_bytes)
```

The `_as_bytes` functions perform mboxrd unescaping and (for dedupe)
Message-ID/List-Id extraction directly on raw bytes, so they skip the
email parser entirely. The regular `split_mbox` and `split_and_dedupe`
are thin wrappers that parse the results.

#### URL Helpers

```python
from liblore.utils import get_msgid_from_url

# Extract a message ID from a lore URL
msgid = get_msgid_from_url('https://lore.kernel.org/all/20250101-example@kernel.org/')
# -> "20250101-example@kernel.org"

# Also works with bare message IDs
msgid = get_msgid_from_url('<20250101-example@kernel.org>')
# -> "20250101-example@kernel.org"
```

### Exceptions

All exceptions inherit from `LibloreError`, so you can catch them broadly or
handle specific cases:

```python
from liblore import LibloreError, NotOnMirrorError, RemoteError, PublicInboxError

try:
    msgs = node.get_thread_by_msgid('nonexistent@example.com')
except NotOnMirrorError:
    # A partial mirror doesn't have it, and its upstream archive could not
    # be reached (see "Partial Mirrors").  Also a RemoteError.
    ...
except RemoteError as ex:
    # HTTP request failed (server error, network issue, etc.)
    if ex.status_code == 404:
        ...  # the server answered: it does not have this
    elif ex.status_code is None:
        ...  # no answer at all: connection error or timeout
    ...
except PublicInboxError:
    # Something went wrong with the public-inbox operation
    ...
except LibloreError:
    # Catch-all for any liblore error
    ...
```

## Development

Install with development dependencies:

```shell
uv sync --all-extras --all-groups
```

Run the test suite:

```shell
uv run --all-extras --all-groups pytest
```

Type checking:

```shell
uv run --all-extras --all-groups ty check
uv run --all-extras --all-groups mypy .
uv run --all-extras --all-groups pyright
```

Linting:

```shell
uv run --all-extras --all-groups ruff format --check
uv run --all-extras --all-groups ruff check
```

## Bug Reports

Send bug reports and patches to [tools@kernel.org](mailto:tools@kernel.org).

## Licence

GPL-2.0-or-later. See [LICENSES/GPL-2.0-or-later.txt](LICENSES/GPL-2.0-or-later.txt)
for the full text.

Copyright The Linux Foundation.
