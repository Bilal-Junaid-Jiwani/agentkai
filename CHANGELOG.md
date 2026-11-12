# Changelog

All notable changes to agentkai are documented here. Versioning follows
[SemVer](https://semver.org/): MINOR for features, PATCH for fixes.

## [0.6.28] - 2026-11-12

### Fixed
- **Exec blocklist: disk-destroying tools that format / partition
  nothing slipped past unchecked.** `cryptsetup erase` wipes the
  LUKS header, `nvme format` / `nvme sanitize` erase the whole
  namespace, `hdparm --security-erase` / `--security-erase-enhanced`
  wipe every sector in firmware, and `badblocks -w` writes test
  patterns over every block — none of them formats or partitions,
  so none of them matched the format / partition / volume checks
  added in 0.6.27. They are now blocked whenever their target is a
  raw device (including `..` climbs, by-id symlinks, and
  `/dev/mapper/` nodes, via the existing device-spelling check).
  Precision is preserved: each tool keeps its read-only form
  (`nvme list` / `smart-log` / `id-ctrl`, `hdparm -I`, `badblocks`
  without `-w`, `cryptsetup luksDump` / `reencrypt`), `hdparm`'s
  password management (`--security-set-pass` / `--security-unlock`)
  stays allowed — it locks the drive but destroys no data — and all
  of them stay allowed on non-device targets such as image files.
  New test pins the blocked spellings (21 cases) and the allowed
  ones (17 cases).

## [0.6.27] - 2026-11-11

### Fixed
- **Exec blocklist: format / partition / volume tools on raw devices
  passed unchecked.** Only `mkfs` was caught (by a regex), so
  `mke2fs`, `mkswap`, `fdisk`, `parted`, `sgdisk`, `sfdisk`,
  `cryptsetup luksFormat`, `pvcreate` / `vgcreate` and their kin all
  slipped past the blocklist. A new token-level check
  (`_format_device_reason`, with a shared `_split_tool_args` option
  parser) now blocks each of them — `mke2fs`, `mkswap`, `fdisk`,
  `parted` (any destructive subcommand, plus a bare interactive
  invocation), `sgdisk` (zap / clear / convert / delete / new /
  load-backup / replicate / expert, plus a bare invocation and a
  backup file aimed at a device), `sfdisk` (`--delete` and a bare
  stdin-script invocation), `cryptsetup` (`luksFormat`,
  `luksHeaderRestore`, `luksRemoveKey`, `luksKillSlot`), `pvcreate` /
  `pvremove`, `vgcreate` / `vgremove` / `vgextend` / `vgreduce`,
  `lvremove`, `mdadm --create`, and `zpool create` — whenever their
  target is a raw device (including `..` climbs, by-id symlinks, and
  `/dev/mapper/` nodes, via the existing device-spelling check).
  Precision is preserved: each tool keeps its read-only form
  (`fdisk -l`, `sfdisk -d`, `parted print`, `sgdisk -p`,
  `pvdisplay` / `vgdisplay`, `cryptsetup luksDump`, the `mke2fs -n`
  and `zpool create -n` dry runs), and all of them stay allowed on
  non-device targets such as image files. New test pins the blocked
  spellings (42 cases) and the allowed ones (39 cases).

## [0.6.26] - 2026-11-10

### Fixed
- **Exec blocklist: `..` climbs back onto a raw device slipped past
  the `>` redirect patterns.** The shell resolves `..` segments
  lexically, so `cat x > /dev/../dev/sda`,
  `cat x > /dev/mapper/../sda`, and
  `cat x > /dev/foo/../../dev/sda` write `/dev/sda` exactly like the
  canonical spelling — but the redirect patterns only admitted dot /
  empty segments after `dev/`, so every climb spelling went
  unchecked, even though the `dd of=` pattern (which only needs the
  `/dev/` prefix) and the copy / wipe token checks (which normpath
  the target) already blocked their own `..` spellings. The
  redirect patterns now cover both climb forms — climb then re-enter
  a `dev/` directory, and one or more `<dir>/../` pairs landing back
  in `/dev` — for all three device families (disk names,
  `mem` / `kmem` / `port`, by-*/mapper paths), with one quote
  admitted at each joint of the climb the way the quoted spellings
  already are, and the brace re-check covers brace spellings of the
  climb too. Precision is preserved: a climb that lands outside
  `/dev` (`> /dev/../sda` is a plain file in `/`,
  `> /dev/disk/../../sda` likewise) stays allowed, as does a climb
  that lands on a non-device (`> /dev/a/b/../sda` is `/dev/a/sda`,
  `> /dev/../dev/null` is `/dev/null`). New test pins the blocked
  spellings and the allowed ones.

## [0.6.25] - 2026-11-09

### Fixed
- **Exec blocklist: device-mapper / loop / RAID / ramdisk /
  network-block / stable-symlink spellings of a raw device slipped
  past the copy / wipe checks and the redirect patterns.** The copy
  and wipe token checks and the `>` redirect patterns only knew the
  `sd` / `hd` / `vd`, `nvme`, `mmcblk`, and `mem` / `kmem` / `port`
  name families, so the same destruction spelled through a
  device-mapper node (`cp /dev/zero /dev/dm-0`,
  `tee /dev/mapper/vg-lv`), a loop device (`cp /dev/zero
  /dev/loop0`), a RAID array (`tee /dev/md0`), a ramdisk (`shred
  /dev/ram0`), or a network block device (`cp /dev/zero /dev/nbd0`,
  `/dev/rbd0`) wrote the disk exactly like the canonical spelling —
  and the stable symlinks under `/dev/disk/by-*/`
  (`cp /dev/zero /dev/disk/by-id/ata-…`, `by-path`, `by-partuuid`,
  `by-label`, `by-uuid`, `by-partlabel`) did the same, since their
  names are arbitrary and no fixed name list can cover them. The
  redirect patterns never knew `/dev/mmcblk0` either, even though
  the copy / wipe checks did. The token checks now cover the new
  name families (literals, globs against the representative-name
  list, and brace expansions) plus any entry under a `by-*` or
  `mapper` directory — those directories hold only device nodes, so
  any name there is a device — and the redirect patterns cover the
  new families and the by-*/mapper paths, with one quote admitted at
  each joint of the path the way the quoted spellings already are.
  Precision is preserved: the by-* and mapper directories themselves
  are not devices, a bare name with no number (`/dev/loop`,
  `/dev/md`, `/dev/dm-`) stays allowed, a glob that cannot match a
  device (`/dev/ttyS[0-9]`, `/dev/pts/*`, `/tmp/*`) stays allowed,
  and a brace group whose first expansion is a plain directory
  (`> /dev/{disk,mapper}/x` redirects to `/dev/disk/x` — a redirect
  target is a single word) stays allowed. Correction to the 0.6.24
  pin: `cp /dev/zero /dev/disk/by-id/*` was listed as allowed there,
  but the glob expands to device symlinks — every expansion is a
  device, so `tee` / `shred` on it write every disk; it is blocked
  now. New test pins the blocked spellings and the allowed ones.

## [0.6.24] - 2026-11-08

### Fixed
- **Exec blocklist: glob / brace spellings of a raw device slipped
  past the copy / wipe checks and the redirect patterns.** The shell
  expands globs and braces before acting, so `cp /dev/zero
  /dev/sd[a]`, `tee /dev/sd?`, `shred /dev/nvme[0-9]n1`, and `tee
  /dev/{sda,sdb}` write a raw device exactly like the canonical
  spelling — but the literal `_RAW_DEVICE` match in the
  `cp`/`mv`/`tee`/`install` and `shred`/`wipefs`/`blkdiscard` token
  checks never saw them, and a brace group at the start of a
  redirect target (`cat x > /dev/{sda,sdb}`, `>>` likewise) never
  matched the literal device-name position in the redirect patterns.
  The token checks now expand braces first and match glob spellings
  segment-wise against the representative device names (a glob must
  have exactly the `/dev/<name>` shape and `*` never crosses a `/`,
  the way the shell's glob works), and the redirect patterns are
  re-checked against the command's first brace-expansion — a
  redirect target is a single word, so only its first expansion can
  be the target. Precision is preserved: a glob that cannot match a
  device (`/dev/ttyS[0-9]`, `/dev/pts/*`, `/tmp/*`) stays allowed,
  as does a brace group with no comma (`{sda}` is literal in the
  shell) and a harmless first alternative (`{null,zero}`,
  `{null,sda}`). New test pins the blocked spellings and the
  allowed ones.

## [0.6.23] - 2026-11-07

### Fixed
- **Exec blocklist: quoted spellings of a raw device slipped past
  the redirect / `dd of=` scan.** The shell strips quotes before
  acting, so `cat x > "/dev/sda"`, `cat x > /dev/"sda"`, and
  `dd if=/dev/zero of="/dev/sda"` write `/dev/sda` exactly like the
  unquoted spelling — but the `>` / `dd of=` patterns matched the
  literal `/dev/` prefix, so every quoted spelling went unchecked,
  including `$'/dev/sda'` (which `_expand_ansi_c_quoting` re-wraps
  in quotes, so the pattern still saw a leading quote) and quotes
  inside the device name (`/dev/s"d"a`) or before it (`/"dev/sda"`).
  The three patterns now admit one quote at each joint of the device
  path (after `>` / `of=`, after each `/`, after `dev/`, inside the
  device name), and the `mem`/`kmem`/`port` name keeps its
  word-boundary behaviour via a lookahead after an optional trailing
  quote. Precision is preserved: a quoted harmless path
  (`"/tmp/out.txt"`), a quoted harmless device (`"/dev/null"`), and
  `/dev/memory` quoted or not stay allowed, as does a quoted `>`
  operator (`echo '>' /dev/sda`). New test pins the blocked
  spellings and the allowed ones.

## [0.6.22] - 2026-11-06

### Fixed
- **Run event stream never closed when the agent raised (issue #1).**
  When the agent factory or `agent.run` raised (for example a client
  that fails to initialise), `_run_agent` appended an `error` event
  and marked the run `error`, but never appended `run_finished` —
  unlike the other failure paths (missing key, non-done result).
  `_event_stream` only stops on `run_finished`, so
  `/api/events?run_id=` kept sending pings until the browser
  disconnected, and the run detail view stayed on "running" with the
  timeline following forever. The `except` path now appends
  `run_finished` with status `error` before finishing the run,
  matching the other error paths; replaying a finished run also ends
  immediately. New test pins it: a raising factory still yields
  `error` then `run_finished` (error) and the SSE stream terminates.

## [0.6.21] - 2026-11-05

### Fixed
- **Exec blocklist: dot-segment / doubled-slash spellings of a raw
  device slipped past the scan.** The shell resolves `//`, `/./`
  and `/../` before acting, so `cp /dev/zero /dev/./sda`,
  `cp /dev/zero //dev/sda`, and `cp /dev/zero /dev/sda/.` write
  `/dev/sda` exactly like the canonical spelling — but the raw-device
  match was literal, so the same write spelled as `cp`/`mv`/`tee`/
  `install` or as `shred`/`wipefs`/`blkdiscard` of the device went
  unchecked, including inside `bash -c` payloads and with `-t` /
  `--target-directory` destinations. The same spellings slipped past
  the redirect and `dd` patterns too: `> /dev/./sda`,
  `> //dev/sda`, `> /dev//sda`, `dd of=//dev/sda`, and the `>|`
  clobber redirect (`>| /dev/sda`) matched no pattern. Device
  candidates are now normalized (dot segments and repeated
  separators collapsed, leading `//` folded) before the device
  match, and the redirect/`dd` patterns admit dot/empty segments
  and `>|`. Two precision notes: a spaced redirect to a memory
  device (`cat x > /dev/mem`) also slipped past, because the
  word-boundary in that pattern required the `>` to be glued to a
  word — the boundary now applies only to the `sha256sum`
  alternative; and dot segments that resolve elsewhere stay
  allowed (`/dev/./null`, `/dev/../tmp/x`, `> /dev/../sda` — a
  plain file in `/`, not a device). New tests pin the blocked
  spellings and the allowed ones.

## [0.6.20] - 2026-11-04

### Fixed
- **Exec blocklist: raw-device destruction spelled as `install`, and
  device-wipe commands aimed at a raw device, slipped past the scan.**
  The scan blocked the same destruction spelled as `dd of=/dev/...`,
  a `> /dev/...` redirect, or a `cp`/`mv`/`tee` copy — but `install`
  copies its source onto its destination exactly like `cp`, so
  `install /dev/zero /dev/sda` overwrote the disk unchecked, and
  `shred /dev/sda`, `wipefs -a /dev/sda`, and `blkdiscard /dev/sda`
  matched no pattern or token check at all. The raw-device check now
  covers `install` with `cp`'s destination rules (last operand, or
  `-t`/`--target-directory`, including attached and clustered
  spellings; `install`'s value-taking long options `--mode`/`--owner`/
  `--group`/`--strip-program` are skipped so they are not mistaken
  for operands), and a new check blocks `shred`/`wipefs`/`blkdiscard`
  whose target is a raw device (`/dev/sd*`, `/dev/hd*`, `/dev/vd*`,
  `/dev/nvme*n*p*`, `/dev/mmcblk*`, `/dev/mem`, `/dev/kmem`,
  `/dev/port`). Precision is preserved: installing to a safe path,
  a safe `-t` target, or a harmless device (`/dev/null`,
  `/dev/zero`), and wiping an ordinary file (`shred /tmp/file`,
  `wipefs /tmp/disk.img`) stay allowed, and both new checks rescan
  inside `bash -c`/`eval` payloads. New tests pin the blocked
  spellings and the allowed ones.

## [0.6.19] - 2026-11-03

### Fixed
- **Exec blocklist: writing to a raw disk or memory device spelled as
  an ordinary file copy slipped past the scan.** The scan blocked the
  same write spelled as `dd if=/dev/zero of=/dev/sda` or as a
  redirect (`cat /dev/zero > /dev/sda`), but `cp /dev/zero /dev/sda`,
  `mv x /dev/sda`, and `tee /dev/sda` matched no pattern or token
  check — and neither did the same writes inside `bash -c '...'` /
  `eval '...'` payloads, which the nested-shell rescan only catches
  if the inner scan does. A new token-level check now blocks
  `cp`/`mv`/`tee` whose write destination is a raw device
  (`/dev/sd*`, `/dev/hd*`, `/dev/vd*`, `/dev/nvme*n*p*`,
  `/dev/mmcblk*`, `/dev/mem`, `/dev/kmem`, `/dev/port`), with
  `cp`/`mv` destination rules matching the shell (`--` ends flags,
  `-t`/`--target-directory` names the destination, including
  attached and clustered spellings; `tee` writes to every operand).
  Precision is preserved: only the destination counts, so copying a
  device *to* a safe path (`cp /dev/sda /tmp/backup`), a safe
  `-t` target (`cp -t /tmp /dev/sda`), and harmless devices
  (`/dev/null`, `/dev/zero`, `/dev/full`) stay allowed. New tests
  pin the blocked spellings and the allowed ones.

## [0.6.18] - 2026-11-02

### Fixed
- **Exec blocklist: nested-shell payloads spelled without a standalone
  `-c` word, and backtick command substitution, slipped past the
  scan.** The nested-shell check only recognized a standalone `-c`
  token immediately after the shell word, but the shell's option
  parser also runs an attached argument (`sh -c'rm -rf /'` — shlex
  merges it into the single token `-crm -rf /`), options before `-c`
  (`bash --norc -c '...'`, `bash -o posix -c '...'`,
  `bash --rcfile foo -c '...'`), and short-option clusters
  (`sh -ec '...'`, `sh -xc'...'`) — and backtick substitution
  (`echo \`rm -rf /\``) tokenizes with `` `rm `` and `` /` `` as
  words, which neither the command nor the path checks recognized.
  The scan now walks shell options the way getopt does (long options,
  value-taking options, clusters where `-o` consumes its argument and
  `-c` takes the rest of the cluster or the following words), and
  splits backticks into their own tokens, with the backtick as a
  separator so a following word is not mistaken for the substituted
  command's argument. Precision is preserved: `sh -c'echo hi'`,
  option-preceded benign payloads, `bash script.sh -c '...'` (where
  `-c` is the script's argument, not the shell's), `sh -- -c '...'`
  (option parsing ended), `echo \`date\``, and
  `echo \`rm -rf /tmp\` /` (the trailing `/` belongs to echo, not
  to rm) stay allowed. New tests pin the blocked spellings and the
  allowed ones.

## [0.6.17] - 2026-11-01

### Fixed
- **Exec blocklist: shell operators glued to a command or path slipped
  past the scan.** The shell splits on `;`, `&`, `|`, `(` and `)` whether
  or not whitespace surrounds them, but `shlex` splits on whitespace
  only — so `rm -rf /; echo done` tokenized with `/;` as one word,
  `(rm -rf /)` with `(rm` and `/)`, and `echo hi;rm -rf /` with `hi;rm`,
  and none of them matched the command or path checks; `find /
  -delete; echo done` slipped too, since `-delete;` is not `-delete`.
  The scan now splits glued operators into their own tokens (which the
  existing separator handling already treats as command boundaries),
  and nested-shell payload extraction rejoins grouped payloads such as
  `bash -c '(rm -rf /)'`. Precision is preserved: `rm -rf /tmp; ...`,
  `/tmp/*`, `/home/shared`, `./build`, plain `echo a;b`, and `find /
  ; -delete` (where `;` makes `-delete` a separate command, not a find
  flag) stay allowed. New tests pin the blocked spellings and the
  allowed ones.

## [0.6.16] - 2026-10-31

### Fixed
- **Exec blocklist: permission-nuke spellings of `chmod 777 /`
  slipped past the 0.6.15 pattern.** Numeric modes are any octal run
  ending in 777, but the pattern only matched `777`/`0777`, so
  `chmod 00777 /` (leading zeros) and `chmod 4777 /`, `7777 /`,
  `1777 /`, `07777 /` (setuid/setgid/sticky bit) were allowed — and
  every symbolic spelling of the same mode was allowed too:
  `chmod a+rwx /`, `a=rwx /`, `u=rwx,g=rwx,o=rwx /`, `go+rwx /`,
  `o+rwx /`, and plain `o+w /` all leave `/` world-writable. The
  numeric pattern now matches any octal run ending in 777, and a new
  token-level check blocks symbolic modes whose end state leaves `/`
  world-writable (clauses apply in order, so `a+rwx,o-w /` takes the
  write bit back off and stays allowed). Scope is unchanged: only
  `/` itself is guarded, and `777`-class modes on `/tmp`/`/home`,
  non-777 modes on `/`, owner/group-only grants, other-read-only
  grants, and the umask-masked bare `+rwx /` (other gains r-x, not
  w, like the allowed `755 /`) stay allowed. New tests pin the
  blocked spellings and the allowed ones.

## [0.6.15] - 2026-10-30

### Fixed
- **Exec blocklist: the `chmod 777 /` permission nuke slipped past
  the scan in its most natural spelling.** The pattern required
  trailing whitespace after `/`, so `chmod -R 777 /` at the end of a
  command and `chmod -R 777 /; ...` before the next command were
  allowed, as were `--recursive` and combined `-Rf` flag spellings.
  The pattern now accepts any flag spellings before the mode and
  matches `/` at end of command or before a shell separator too.
  Precision is preserved: `777` on a non-root dir (`/tmp`, `/home`)
  and non-`777` modes on `/` stay allowed. New tests pin the blocked
  spellings and the allowed ones.
- **Health-summary tests no longer age out of their own window.**
  The fixtures used fixed September dates, which fell outside the
  30-day summary window as the calendar moved, failing two tests for
  no code reason. Fixture dates are now relative to today, with the
  same expected aggregates.

## [0.6.14] - 2026-10-29

### Fixed
- **Exec blocklist: ANSI-C-quoted spellings can no longer wipe `/`
  or a home directory.** The shell decodes `$'...'` escapes before
  acting, so `rm -rf $'/home'` is `rm -rf /home`, `$'\x2fhome'`
  (hex escape) and `$'\057home'` (octal escape) spell the same path,
  `/$'home'` and `$'/'home` build it across a quoted segment, and
  `/home/$'..'` resolves to `/` — all of these slipped past the old
  check, which saw only the shlex-mangled tokens (`$/home`,
  `$\x2fhome`, `/$home`) that no normalizer recognizes. The scan now
  decodes unquoted `$'...'` segments (and `$"..."` locale strings)
  on the raw command before tokenizing, re-wrapping each decoded
  segment in single quotes so it stays one word; nested payloads
  written as `bash -c "rm -rf \$'/home'"` are caught too, since the
  scan now accounts for the outer shell stripping `\$` inside double
  quotes (which shlex does not). The download-piped-to-shell pattern
  gets the same treatment: `curl ... | $'sh'` / `| 'sh'` are now
  blocked. Precision is preserved: a quoted non-guarded path
  (`$'/tmp/foo'`), a quoted subdir of a home dir, a quoted relative
  path, a quoted non-shell piped command, and the single-quote
  concatenation lookalike (`bash -c 'rm -rf $'/home''`, which the
  shell reads as the literal word `$/home`) stay allowed. New tests
  pin the blocked spellings and the allowed ones.

## [0.6.13] - 2026-10-28

### Fixed
- **Exec blocklist: brace-sequence, `${HOME}` substring, and
  suffix-removal spellings can no longer wipe `/` or a home
  directory.** The shell expands these before acting, so
  `/hom{e..f}` is `/home`, `/home/hat{c..d}h` includes a home dir,
  `${HOME:0:5}` is `/home` for a `/home/<user>` home,
  `${HOME:0}` is home itself, `${HOME%/*}` removes to `/home`, and
  the space form `${HOME: -11}` (one word in the shell, two tokens
  under `shlex`) substring-expands to home itself — all of these
  slipped past the old check, which only understood comma braces and
  the `<op>word` parameter forms. `_expand_braces` now expands
  `{a..z}` / `{1..10}` sequences (with optional `..step`, zero-step
  meaning step 1, and the shell's zero-padding), `_normalize_path_spelling`
  now expands `${HOME:offset[:length]}` substrings and `${HOME#/%pat}`
  pattern removal, and `_is_dangerous` rejoins tokens while a
  `${...}` group is still open so spaced expansions tokenize as the
  shell sees them. Precision is preserved: `${HOME#*/}` /
  `${HOME:1}` are relative paths, `${HOME%%/*}` expands to empty (no
  argument at all, not `/`), `${HOME%/*}/shared` is a sibling of the
  homes, and brace sequences over non-guarded dirs (`/tmp/{a..c}`)
  stay allowed. New tests pin the blocked spellings and the allowed
  ones.

## [0.6.12] - 2026-10-27

### Fixed
- **Exec blocklist: glob and brace spellings can no longer wipe `/`
  or a home directory.** The shell expands globs and braces before
  acting, so `rm -rf /hom?` is `rm -rf /home`, `/h*` matches
  `/home`, `/home/?*` / `/home/[a-z]*` match every home, and
  `/{,}` / `/home/{,}` brace-expand to `/` / `/home` — all of
  these slipped past the old check, which only understood literal
  paths and bare `*` chains. `_is_root_or_home` now brace-expands
  the token and walks glob segments against the guarded set: a
  pattern matching a guarded dir's name is blocked, a broad glob
  (`*`, `?*`, `[!.]*`) directly under a guarded dir is blocked as
  a wipe of that dir, and a glob chain under a guarded dir stays
  blocked. Precision is preserved: a glob under a named subdir
  (`$HOME/project/*`), a pattern matching no guarded name
  (`/*.pyc`, `$HOME/*.log`), and a named tail under a glob-matched
  parent (`/hom?/shared`, a sibling of the homes) stay allowed.
  `find ... -delete` and nested-shell payloads over these spellings
  are covered through the same check. New tests pin the blocked
  spellings and the allowed ones.

## [0.6.11] - 2026-10-26

### Fixed
- **Exec blocklist: the directory holding the homes, and star chains
  over guarded dirs, can no longer wipe every home at once.**
  `_is_root_or_home` guarded `/` and each account home (plus their
  `/*` forms), but `rm -rf /home` — spelled directly or as
  `$HOME/..` / `~/..` — slipped through, even though deleting the
  container destroys every home inside it (and `~root/..` was already
  blocked, since its parent is `/`). The parent of each home now
  counts as guarded too, as do pure-star chains under a guarded dir:
  `/home/*` (all homes at once), `/home/*/*`, `/*/*`, and
  `$HOME/*/*`. Precision is preserved: a sibling of the homes
  (`$HOME/../shared`) and a star under a named subdir
  (`$HOME/project/*`) stay allowed. New tests pin the blocked
  spellings and the allowed ones; one old precision pin (`~/..//`)
  moved to the blocked set, since it resolves to the home container.
  `find ... -delete` and nested-shell payloads over these paths are
  covered through the same check.

## [0.6.10] - 2026-10-25

### Fixed
- **Exec blocklist: tilde-user and `${HOME<op>word}` spellings can no
  longer bypass the home-directory guard.** The blocklist expanded only
  bare `~` / `$HOME` / `${HOME}`, but the shell expands more spellings
  of the same paths: `~user` resolves to that account's home (so
  `rm -rf ~root` / `rm -rf ~<current-user>` wiped a home directory
  while the checker normalized it to the nonsense path `<home>user`),
  and `${HOME:-word}` / `${HOME-word}` expand to `$HOME` when it is set
  (with `${HOME:=word}` / `${HOME:?word}` yielding `$HOME` too, and
  `${HOME:+word}` yielding `word`). `_normalize_path_spelling` now
  expands tilde-user via `pwd` lookup and all of these parameter
  expansions before comparing, and the protected set is every account's
  home directory, not just the current user's — so `find ~root
  -delete` and nested-shell payloads are caught as well. Near-misses
  stay allowed: unknown usernames (left literal by the shell),
  `${HOME:+/tmp/foo}` (the word, not home), and subdirs of a home dir.
  New regression tests pin both the blocked spellings and the allowed
  ones.

## [0.6.9] - 2026-10-24

### Fixed
- **Failed forced skill reinstalls no longer delete the working skill.**
  `install_skill(..., force=True)` removed the existing skill *before*
  attempting the new install, so any failure (failed git clone, missing
  `SKILL.md`, pin mismatch) left the user with no skill at all. The
  existing install is now moved aside during the reinstall and restored
  unchanged on failure; the backup is cleaned up on success. New
  regression tests pin both the restore path and the success path.
- **Honest scope note version drift.** `README.md` and `docs/index.md`
  said "early-stage software (v0.6.6)" — two releases behind; both now
  read v0.6.9.

## [0.6.8] - 2026-10-23

### Fixed
- **Exec blocklist: dot-segment path spellings can no longer bypass it.**
  The blocklist matched ``rm`` targets against literal spellings of ``/``
  and the home directory, but the shell resolves dot segments before
  acting — so ``rm -rf /./`` and ``rm -rf /../`` (and the same spellings
  behind ``sudo``, nested ``bash -c '...'`` payloads, and ``find`` search
  roots) slipped straight through while doing exactly what ``rm -rf /``
  does. ``_is_root_or_home`` now normalizes each target the way the shell
  resolves it (``~``/``$HOME``/``${HOME}`` expanded, ``//``/``/./``/``/../``
  collapsed via POSIX normalization) before comparing, so the obfuscated
  spellings are caught too — without flagging near-misses like
  ``rm -rf $HOME/../shared`` (the parent of home, not home). New
  regression tests pin both the blocked spellings and the allowed ones.

## [0.6.7] - 2026-10-22

### Fixed
- **Browser tools are now thread-affine.** `_run_tool_guarded` ran every
  tool call on a fresh worker thread, but the persistent `BrowserSession`
  drives Playwright's sync API, which must be used from the thread that
  created it — so browser calls could fail or deadlock when a new tool
  call landed on a different thread than the one that started Chromium.
  `Tool` gained a `thread_affinity` flag; all browser tools set it, and
  the agent loop routes those calls through a dedicated per-tool worker
  thread instead of a fresh thread per call.
- **MCP SSE transport rejects cross-origin endpoints.** The transport
  took its POST endpoint from the server's `endpoint` event via
  `urljoin` with no origin check, so a compromised MCP server could
  redirect all JSON-RPC traffic (tool names, arguments, results) to an
  arbitrary host. The announced endpoint must now share scheme, host,
  and port with the SSE stream URL; a mismatch fails closed with
  `MCPError`.
- **Context compaction summary is no longer `role: "user"`.** Phase-2
  budget compaction inserted a summary of *untrusted tool output* as a
  user message — a prompt-injection foothold, since the model treats
  user-role content as instructions. The summary is now `role: "system"`
  and explicitly labeled as compacted tool output, never new user
  instructions.

### Verified and refuted (Gemini Argon audit claims checked in source)
- Skill manifest "bypass": refuted — `_verify_user_skill` is fail-closed
  (unreadable/invalid/non-object manifest or missing `content_sha256`
  refuses the load) and runs on every user-skill load.
- Dashboard token issue: refuted — per-launch random token, localhost-only
  bind, token-gated middleware with constant-time comparison, never
  logged, printed exactly once at startup.
- Missing dependency floors: refuted — every runtime dependency in
  `pyproject.toml` carries a `>=` floor.
- Approval cancellation/skip: refuted — approvals fail closed: timeout
  denies, unknown run returns 404, `action_id` mismatch returns 400; no
  cancel-to-allow path exists.

## [0.6.6] - 2026-10-22

### Fixed
- **Critical: `agentkai run` works again on a fresh install.** Releases
  through 0.6.5 declared `litellm>=1.60` with no upper bound and never
  declared `mcp`, but LiteLLM's tool-calling path imports `mcp.types`
  at call time — so a clean `pip install agentkai` produced an
  install where every run died with "all models failed ... No module
  named 'mcp'". `mcp>=2.2,<3` is now a declared dependency (matching
  LiteLLM's own requirement for that code path) and LiteLLM is capped
  at `<2.0`. `agentkai doctor` gained a `litellm` check that probes
  exactly these imports, so a partial install is diagnosable up front
  instead of at first run. New `scripts/smoke_install.py` installs the
  built wheel into a clean venv and proves the run path reaches the
  model stage — the class of check that would have caught this.
- **Fallback-chain errors tell the truth.** When every model in a chain
  failed, only the LAST provider's error was surfaced — a bad Anthropic
  key on the primary showed up as "OpenAI credentials missing" from the
  tail of the chain. The error now lists each model's own failure in
  chain order (primary first), each collapsed to one line.
- **`exec` blocklist hardening (still a blocklist, not a sandbox).**
  The destructive-command scan now matches `rm` by basename (`/bin/rm
  -rf /` slipped past), treats `$HOME`/`${HOME}` like `~`, recurses
  into nested `sh -c '...'` / `eval` payloads, and blocks
  `find / -delete` and download-piped-to-shell one-liners
  (`curl ... | sh`). Docs and the tool description now state plainly
  that the command itself is not sandboxed, the permission gate is the
  real protection, and a container/VM is the right home for untrusted
  or unattended use. Regression tests cover every named bypass plus
  near-miss commands that must stay allowed (`rm -rf ./build`,
  `curl ... | sha256sum`).
- **Scheduler refuses garbage schedules at add time.** `Scheduler.add`
  never validated, and the CLI path didn't either — a job with schedule
  `"not a cron"` was happily stored and could never fire. `add()` now
  validates cron and `@at:` schedules (the dashboard API already did),
  and the CLI reports a clean `bad schedule ...` error with exit 2.
- **Docs caught up with the code.** README and the docs index still
  called the software "v0.3.0"; both now say v0.6.6, and the doctor /
  scheduler / models / tools pages describe the checks, validation,
  error format, and exec safety model as they actually are.

## [0.6.5] - 2026-10-21

### Fixed
- **Security: `transcribe_audio` input files are now confined to the
  agent workspace.** The tool accepted an unconstrained `audio_path`,
  so a model could point it at any audio file on the host (e.g. another
  application's recordings) and have the transcript fed back to it. Input
  paths are now confined exactly like the file/shell and media tools:
  relative paths resolve against `<home>/workspace`, absolute paths must
  already live inside it, and `..` traversal or symlink escapes are
  refused with a clear error (surfaced as `ERROR: ...` by the tool).
  The gateway hook `preprocess_voice_note` is unchanged: channels
  download voice notes themselves and call it on the downloaded file.
  Eight new tests in `tests/test_voice.py` cover absolute-path,
  traversal, tilde, and symlink escapes, inside-workspace acceptance
  (absolute and relative), and missing-file rejection.

## [0.6.4] - 2026-10-20

### Fixed
- **Security: `speak_text` output paths are now confined to the agent's
  voice directory.** The low-risk `speak_text` tool accepted an
  unconstrained `out_path`, so a model could point it at any
  process-writable file (e.g. `~/.bashrc`) and have it overwritten with
  synthesized audio bytes. Output paths are now confined exactly like
  the media tools: relative paths resolve against `<home>/voice`,
  absolute paths must already live inside it, and `..` traversal or
  symlink escapes are refused with a clear error (surfaced as
  `ERROR: ...` by the tool). The default output location is unchanged
  (`<home>/voice/tts-<stamp>-<id>.wav`). Seven new tests in
  `tests/test_voice.py` cover absolute-path, traversal, tilde, and
  symlink escapes plus inside-voice-dir acceptance and the default path.

## [0.6.3] - 2026-10-17

### Fixed
- **Security: `edit_image` input files are now confined to the agent
  workspace.** The `image` and `mask` arguments were resolved against the
  raw filesystem, so a model could point the tool at an arbitrary file
  (e.g. `~/.ssh/id_rsa`, `/etc/passwd`) and have its bytes exfiltrated to
  the remote image-editing provider. Input paths are now confined exactly
  like the file/shell tools: relative paths resolve against
  `home/workspace`, absolute paths must already live inside it, and `..`
  traversal or symlink escapes are refused with a clear error. Six new
  tests in `tests/test_media.py` cover absolute-path, traversal, symlink,
  and mask escapes plus inside-workspace acceptance.

## [0.6.2] - 2026-10-16

### Fixed
- **Permission gate: the in-memory audit log no longer grows without
  bound.** `PermissionGate._audit` appended one entry per tool decision
  for the whole process lifetime — and gateway agents (plus their gates)
  live as long as the gateway runs, so a long-lived gateway leaked memory
  on every tool call (verified: 50,000 decisions → 50,000 in-memory
  entries). The in-memory `audit_log()` now keeps the most recent
  `AUDIT_CAP = 2000` decisions (oldest-first eviction, same window as the
  WhatsApp bridge's seen-id cache); when `audit_path` is configured, the
  JSONL file remains the durable, complete record of every decision. Two
  new tests in `tests/test_core.py` verify the bound/eviction and that the
  JSONL file still records everything.

## [0.6.1] - 2026-10-15

### Fixed
- **WhatsApp bridge: the seen-message-id dedup cache no longer grows
  without bound.** `WhatsAppBridge._seen` collected every sidecar event id
  it ever dispatched, so a gateway left running for weeks would keep the
  full history in memory forever. The cache is now capped at
  `SEEN_CAP = 5000` ids with oldest-first eviction. Eviction is safe
  because the sidecar only returns events at or after the `since`
  watermark, so a re-sent evicted id simply gets re-dispatched — the
  correct fallback. A new test in `tests/test_channels.py` verifies the
  bound, oldest-first eviction, and that within-window dedup still works.

## [0.6.0] - 2026-10-14

### Added
- **Browser request gate: the non-public-host SSRF boundary now applies
  to every request Chromium makes, not just the initial navigation.**
  0.5.6 closed the navigation-time gap and documented the remaining
  limitation honestly — redirect hops, subresources, XHR/fetch, and
  websockets fired by a loaded page were followed by Chromium itself with
  no re-validation, so a prompt-injected page could have steered the
  browser at `http://169.254.169.254/latest/meta-data/`, `127.0.0.1`, or
  `192.168.x.x` and read the response into the page (and into the
  agent's context). `BrowserSession` now registers a context-wide
  Playwright route handler (`**/*`, covering all tabs including ones
  opened later) that enforces the same `_assert_public_host()` boundary
  as `fetch_url` hop-by-hop: requests to non-public or unresolvable
  hosts are aborted, as are `file://` requests (a page can never read
  the local disk, even via iframe/subresource). Harmless schemes
  (`data:`, `blob:`, `about:`, `chrome:`, ...) pass through untouched,
  and the handler fails closed — an unexpected error aborts the
  request rather than letting it through. Eleven new tests in
  `tests/test_browser.py` cover registration, the public-host allow
  path (http/https and wss), and aborts for link-local metadata IP,
  loopback, private, and unresolvable hosts, `file://` URLs, ws-to-loopback,
  harmless schemes, and the fail-closed path. Playwright's route handler
  fires per redirect hop, so this supersedes the 0.5.6 known-limitation
  note.

## [0.5.6] - 2026-10-13

### Fixed
- **Browser navigation now refuses non-public hosts (SSRF gap closed).**
  `fetch_url` has long refused hosts that resolve to loopback, private,
  link-local, or otherwise non-public addresses, but the real-Chromium
  browser tool's URL validation only checked the scheme and rejected
  credentials-in-URL — so `browser_goto http://169.254.169.254/...`
  (the cloud metadata endpoint), `http://127.0.0.1:8501/`, or
  `http://192.168.1.10/...` loaded happily (verified with a repro:
  all three passed validation while `fetch_url` refused them). A
  prompt-injected page could have steered the browser at internal
  services or metadata URLs and the response would have landed in the
  agent context. The non-public-host check is now extracted from
  `tools._validate_url` into a shared `_assert_public_host()` helper and
  enforced by `browser._validate_url`, so `browser_goto` (and `new_tab`
  with a URL, which routes through the same gate) refuses unresolvable
  hosts and every non-public address class with the same messages as
  `fetch_url`. Six new tests in `tests/test_browser.py` cover private,
  loopback, and link-local (metadata IP) refusals, unresolvable hosts,
  and the public-host allow path; the suite's DNS is now fully mocked
  so browser navigation tests never touch real DNS. Known limitation,
  unchanged: redirects *within* an already-loaded page are followed by
  Chromium itself and are not re-validated hop-by-hop the way
  `fetch_url` does.

## [0.5.5] - 2026-10-12

### Fixed
- **`--pin` on skill installs now actually checks out the pinned commit.**
  The 0.5.2 supply-chain hardening added `--pin`/`pin` to pin a remote
  skill install to a commit SHA, but the implementation only compared
  `git clone --depth 1`'s HEAD against the pin afterwards and never
  checked the pin out — so pinning to any non-HEAD commit always failed
  with "pin mismatch" (verified with a repro against a real 3-commit
  git repo), and the feature's whole point (install the exact commit
  you reviewed) was silently broken. `install_skill()` now fetches the
  pinned commit and `git checkout --detach`s it before recording
  `commit_sha` in the manifest; the pin must be a full 40-hex SHA
  (required for fetch-by-SHA, and the strict shape also rules out
  option injection through the pin value), and an unfetchable commit
  fails the install with the half-cloned directory cleaned up, as
  before. Four new tests in `tests/test_skills.py` cover pinning to an
  older commit (checked-out tree, manifest SHA, and clean load),
  pinning to HEAD, invalid pin formats, and unfetchable commits.

## [0.5.4] - 2026-10-11

### Fixed
- **Media `out_path` can no longer escape the media directory.** The
  `generate_image`, `generate_video`, and `edit_image` tools joined a
  relative `out_path` to `~/.agentkai/media/` but never checked the
  result, so a path like `../../pwned.png` wrote the generated file
  *outside* the media dir (verified with a repro: a stub provider wrote
  three files outside the dir). The traversal guard in `generate_image`
  was dead code — after joining, the path was always absolute, so the
  check could never fire — and the other two functions had no guard at
  all, quietly bypassing the filesystem sandbox the file/shell tools
  enforce. All three now resolve the path through one helper that
  collapses `..` segments and symlinks and refuses anything landing
  outside the media dir; relative subdirectories (e.g.
  `campaigns/hero.png`) still work and get their parents created, and
  absolute paths are accepted only inside the media dir. Seven new tests
  in `tests/test_media.py` cover `../` traversal, absolute-path escape,
  symlink smuggling, and the legitimate cases.

## [0.5.3] - 2026-10-10

### Fixed
- **Skill manifest verification now fails closed.** The 0.5.2 integrity
  check silently skipped verification whenever an installed user skill's
  `manifest.json` was unreadable, invalid JSON, or missing its
  `content_sha256` — so corrupting the manifest switched the drift
  check off and let tampered `tools.py` code execute (verified with a
  repro: a marker-dropping `tools.py` ran despite a destroyed
  manifest). `SkillLoader.load()` now refuses such skills with a
  `SkillError` before any skill code runs, and `agentkai skills list`
  flags the broken manifest instead of showing the skill as healthy.
  Skills with no manifest at all (manually copied directories) keep
  loading as before. Note: skills installed by agentkai ≤ 0.5.1 carry
  a manifest without an integrity hash and must be reinstalled once
  (`agentkai skills install <source> --force`) to stamp one. Five new
  tests in `tests/test_skills.py` cover the corrupt, hash-less, and
  non-object manifest refusals (tampered code never executes), the
  manifest-less manual-copy path, and the `skills list` flagging.

## [0.5.2] - 2026-10-09

### Fixed
- **Skill installer supply-chain hardening.** Installing a third-party
  skill previously cloned any git URL with no commit pin or integrity
  hash, and the skill's `tools.py` then executed unsandboxed inside the
  agentkai process on load — so a tampered or swapped repo could run
  arbitrary code. Installs now record `commit_sha` (git installs) and a
  `content_sha256` (SHA-256 over sorted relative paths + file bytes,
  excluding `manifest.json`) in `manifest.json`; `SkillLoader.load()`
  recomputes the hash for `source == "user"` skills and raises
  `SkillError` on drift *before* `tools.py` executes. Remote/git
  installs additionally require explicit trust (`install_skill(...,
  trust_remote=True)`, CLI `--trust-remote`) with the resolved URL
  shown, and support `--pin <commit>` to pin a clone to a commit.
  Local-directory installs are unchanged; bundled skills are
  unaffected. Skills without a manifest (manually copied dirs) keep
  loading as before. No sandbox is introduced — this closes the
  silent-tamper hole, not the trust decision itself. Four new tests in
  `tests/test_skills.py` cover the recorded hash, drift refusal
  (tampered module-level code never executes), the trust gate, and pin
  mismatch cleanup.

## [0.5.1] - 2026-10-08

### Fixed
- The `exec` tool now kills the whole process group when a command times
  out, as its own `start_new_session` comment always intended.
  Previously `subprocess.run` killed only the shell itself, so anything
  the command had backgrounded (e.g. `sleep 60 & wait`) silently
  survived the timeout and kept running on the user's machine. The
  timeout path now SIGKILLs the process group and reaps it, returning
  the same `{exit_code: -1, "ERROR: timed out …"}` result as before.
  Regression test in `tests/test_tools.py` backgrounds a child, records
  its PID, and asserts it is gone after the timeout.

## [0.5.0] - 2026-10-07

### Added
- `agentkai runs list` / `agentkai runs show RUN_ID`: inspect past runs
  from the terminal, without opening the dashboard. `list` shows
  newest-first run summaries (status, model, tools used) with `--limit`
  and `--json`; `show` replays one run's `events.jsonl` in order
  (run start, LLM messages, tool calls/results, approvals, run end),
  also with `--json`. Both honor `$AGENTKAI_HOME` like the rest of the
  CLI. Until now the event log was only reachable through the dashboard
  API or by reading the JSONL files by hand, even though
  `events.list_runs`/`summarize`/`replay` have been public API since
  0.1.0. Six new tests in `tests/test_runs_cli.py` seed real event logs
  and cover empty state, ordering, `--limit`, both `--json` modes, and
  the unknown-run error (exit 1).

## [0.4.1] - 2026-10-06

### Fixed
- `LLMClient.generate` now retries the **whole** fallback chain, not just
  the first fallback. Previously the retry loop bailed out after the first
  fallback failed (`attempt == 0` only), so a request could raise
  "all models failed" without ever trying the remaining models in the
  chain — contradicting the documented fallback behavior in
  `docs/models.md`. The final error now also names every model that was
  actually tried. Three regression tests in `tests/test_core.py` cover the
  full-chain retry, the exhausted-chain error, and the no-fallback fast
  path.

## [0.4.0] - 2026-10-05

### Added
- `agentkai doctor`: read-only environment diagnostics. Checks the agent
  home, `config.yaml` (fails on unparseable YAML — the config loader
  itself silently ignores it), provider API-key status for every model
  alias (warns when the default model's key is missing), the scheduler
  SQLite database (job counts plus failures in the last 24h; fails on a
  corrupt/unreadable DB), the memory tree, the built-in tool registry
  (fails on unknown risk levels), image media providers (OpenAI key,
  local Stable Diffusion server, or an honest warning), and the bundled
  dashboard static assets (fails on missing `index.html`, which would
  mean broken packaging). Prints `[OK]`/`[WARN]`/`[FAIL]` per check,
  exits 1 on any failure; `--json` for a machine-readable report.
  13 new tests in `tests/test_doctor.py`; README CLI table,
  `docs/quickstart.md`, and `docs/troubleshooting.md` document it.

## [0.3.0] - 2026-10-04

### Added
- `PUT /api/memory/{name}` (closes the API.md roadmap item): edit any
  memory file from the dashboard or API — body `{content}`, same
  root-level name rules as the GET (400 on path traversal), 1 MiB cap.
  The previous version is kept as a `<name>.bak` sidecar via atomic
  rename; response is `{name, created, backup, bytes}` (201 on create,
  200 on update). The dashboard Memory page gained an Edit/Save UI that
  surfaces the backup filename.
- `GET /api/tools` (closes the API.md roadmap item): lists the built-in
  tool registry as `{tools: [{name, description, risk}]}` via a new
  `Registry.describe()` accessor (MCP-attached remote tools are per-run
  and intentionally not listed). The dashboard has a matching Tools page
  with per-tool risk pills.
- `Memory.replace(name, text) -> (created, backup)`: atomic replace with
  `.bak` sidecar; backs the PUT endpoint.
- `create_app()` accepts an injectable `Memory` (tests back new
  endpoints with a temp memory root).

### Changed
- `src/agentkai/dashboard/API.md`: memory editing and the tool registry
  moved from "Roadmap" into the documented endpoint contract; roadmap
  is empty.
- `docs/dashboard.md`: corrected the stale "stubbed run simulation"
  note (the chat page drives the real agent loop) and documented the
  new pages.

## [0.2.0] - 2026-10-01

### Added
- Dashboard scheduler job management (the `API.md` roadmap item):
  - `POST /api/scheduler/jobs` — create or update a job (201 on create,
    200 on update). Validates the schedule (5-field cron, or `@at:<ISO>`
    for one-shot jobs) and requires a `prompt` or `command` for
    `cron`/`at` job types, mirroring `agentkai scheduler add`.
  - `DELETE /api/scheduler/jobs/{name}` — remove a job (404 if unknown).
  - `POST /api/scheduler/jobs/{name}/run` — run a job now regardless of
    schedule (404 if unknown, 400 if disabled); outcome is recorded in
    the job's run history.
  - The dashboard Scheduler page got a matching UI: an add-job form plus
    per-row "Run now" / "Delete" actions.
  - `GET /api/scheduler/jobs` now also returns `prompt`, `model_alias`,
    `job_type` and `last_run` for each job.
- `create_app()` accepts an injectable `Scheduler` (tests back new
  endpoints with a temp SQLite store).

### Changed
- `src/agentkai/dashboard/API.md`: scheduler job endpoints moved from
  "Roadmap" into the documented endpoint contract; roadmap now lists
  memory editing and the tool registry.
- `docs/dashboard.md`: notes scheduler job management as shipped in v0.2.0.

## [0.1.0] - 2026-09-29

First public release.

- ReAct agent loop, multi-provider model layer (Claude/Gemini/GPT/Ollama/
  GLM via LiteLLM), safe shell/fs/web tools, MCP client, markdown
  memory, subagents, SQLite scheduler, messaging gateway
  (Telegram/Discord/WebChat/WhatsApp), real Chromium browser tool,
  12 bundled skills, voice STT/TTS, media generation, devices, goals,
  widgets, and a token-secured localhost dashboard.
- 357 tests green (4 skipped); real-Chromium verification 13/13.
