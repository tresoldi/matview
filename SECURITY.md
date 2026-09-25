# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: the **Security** tab of
<https://github.com/tresoldi/matview>, then **Report a vulnerability**. That opens a
private advisory rather than a public issue.

Please do not open a public issue for something exploitable. There is one maintainer, so
expect an acknowledgement within a week rather than within a day.

## Supported versions

The most recent release. matview is 0.x: fixes go into the next release rather than into
patches of older ones.

## What matview trusts, which is what you need to know before using it

None of the following is a vulnerability. They are design positions, and a report that
one of them is exploitable will be answered with this section — but each is a real thing
to weigh, and two of them are easy to get wrong.

**Recipes are code, and registering one runs it.** `@recipe` registers a Python function
that matview calls. A *foreign* recipe is a TOML manifest naming an executable, which
matview launches with a JSON job file. Pointing matview at either is equivalent to
running that code yourself; a manifest from an untrusted source is as dangerous as a
script from one.

**`--recipes MODULE` imports code by design.** The CLI takes it for the operations that
genuinely need a recipe resolved. Every metadata command — `info`, `ls`, `show`, `stats`,
`verify`, `lineage`, `impact`, `cost` — works without it, so inspecting an unfamiliar
database never requires importing anything.

**A cache database is input, and decoding it runs a parser over bytes you did not
choose.** matview never uses pickle, anywhere, which is a hard rule of the design rather
than a preference: a payload cannot by itself execute code. But `cat` decodes a payload
using the codec named on the recipe row, and the Arrow and `.npy` decoders are
third-party parsers being handed attacker-controlled bytes. Treat a database from
somewhere you do not trust as you would any untrusted file, and prefer the metadata
commands, which never decode a payload.

**Digests detect change, not tampering.** Fingerprints and payload digests are
BLAKE2b-256 and unkeyed. They exist so that matview knows what is stale, and `verify`
catches corruption and accidental edits. They are not a signature: anyone who can write
to the database file can recompute them, and matview has no notion of a trusted writer.
SQLite's own file permissions are the access control.

**Nothing here opens a socket.** The core makes no network connections, runs no server
and has no remote backend — v1 leaves that seam unimplemented deliberately. The only
process matview starts is a foreign recipe's executable, and only when a recipe declares
one.
