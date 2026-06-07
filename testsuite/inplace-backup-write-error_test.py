#!/usr/bin/env python3
"""Regression test for the inplace-backup write-error corruption bug.

With `--inplace --backup` and a DELTA transfer to an existing basis file, the
backup file is not merely a safety copy: it is the read-only delta BASIS that
the receiver reads from while it overwrites the destination in place.
recv_generator must therefore secure that backup -- a complete, verified copy
of the original -- BEFORE any part of the transfer is committed to the wire.

The bug: the backup copy used to be streamed block-by-block, from inside
generate_and_send_sums() via an unchecked full_write(f_copy, ...), AFTER the
transfer had already been committed to the wire (write_ndx + the checksum
stream).  full_write() loops over short writes, so a short write is not a
corruption vector -- but a genuine write *error* (ENOSPC/EDQUOT/EFBIG) is: the
backup is left truncated, and because the sender's matched-block tokens were
derived from the FULL original, matched blocks past the truncated backup's EOF
are zero-filled and written into the destination in place.  By then the
receiver's overwrite is unstoppable.  Result on the buggy binary: the original
destination is corrupted in place (and, on a transient fault, a truncated junk
backup is silently left behind).

The fix restructures the delta path to mirror the whole-file path: a discrete,
verified copy_file(fname, backupptr, ...) runs BEFORE the ndx is committed.  On
a backup-write failure the partial copy is discarded, the file is skipped, and
the original destination is never opened for writing -- so it is left
byte-intact.

This test injects exactly that fault with an LD_PRELOAD shim
(backup-write-fail-preload.c) that fails write() to the backup-copy fd ONLY,
after a small prefix, leaving every other fd untouched.  It asserts the fixed
behaviour:

  * the original destination is PRESERVED byte-for-byte (the core guarantee),
  * rsync exits non-zero (the failure is surfaced, not hidden),
  * no truncated backup is left finalized as a valid "backed up" file, and
  * no "backed up" success is logged.

Against the pre-fix binary the same run corrupts the destination in place
(zero-filled past the truncated basis), so the preservation assertion FAILS
there -- i.e. the test has teeth against the actual corruption.

Mechanism note: an LD_PRELOAD write()-shim is used (rather than RLIMIT_FSIZE,
which would also cap the in-place write and not isolate the backup, or a tiny
full filesystem, which needs root here) so the fault hits the backup write and
nothing else.  The test SKIPs (77) if no C compiler is available to build the
shim, or if the platform lacks working LD_PRELOAD interposition.
"""

import os
import shutil
import subprocess

from rsyncfns import (
    FROMDIR, SCRATCHDIR, TODIR,
    makepath, rmtree, rsync_argv, test_fail, test_skipped,
)
from rsyncfns import SUITEDIR


SHIM_SRC = SUITEDIR / 'backup-write-fail-preload.c'
PREFIX_BYTES = 2048          # how much of the backup the shim lets through
BASIS_SIZE = 200 * 1024      # basis large enough to span many checksum blocks


def find_cc():
    for cand in (os.environ.get('CC'), 'cc', 'gcc', 'clang'):
        if cand and shutil.which(cand.split()[0]):
            return cand
    return None


def build_shim(cc):
    shim = SCRATCHDIR / 'backup-write-fail.so'
    cmd = [*cc.split(), '-shared', '-fPIC', '-O2',
           '-o', str(shim), str(SHIM_SRC), '-ldl']
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        test_skipped(f"could not build LD_PRELOAD shim:\n{proc.stderr}")
    return shim


def main():
    if not SHIM_SRC.is_file():
        test_skipped(f"shim source missing: {SHIM_SRC}")
    cc = find_cc()
    if not cc:
        test_skipped("no C compiler available to build the LD_PRELOAD shim")
    shim = build_shim(cc)

    rmtree(FROMDIR)
    rmtree(TODIR)
    makepath(FROMDIR, TODIR)

    src = FROMDIR / 'file'
    dst = TODIR / 'file'
    backup = TODIR / 'file~'

    # Basis (old) content is all 'O'; source (new) content is all 'N'. They
    # differ everywhere, so the file is genuinely transferred and -- with
    # --no-whole-file -- via the delta path that exercises the backup basis.
    # Distinct mtimes ensure rsync does not quick-check the pair as identical.
    dst.write_bytes(b'O' * BASIS_SIZE)
    src.write_bytes(b'N' * BASIS_SIZE)
    os.utime(dst, (1577836800, 1577836800))   # 2020-01-01
    os.utime(src, (1749254400, 1749254400))    # 2025-06-07

    # Snapshot the original destination bytes. The core guarantee is that a
    # failed backup leaves THIS untouched.
    pre = dst.read_bytes()

    env = os.environ.copy()
    env['LD_PRELOAD'] = str(shim)
    env['BWF_SUFFIX'] = '~'
    env['BWF_PREFIX'] = str(PREFIX_BYTES)

    argv = rsync_argv('-a', '--inplace', '--backup', '--no-whole-file',
                      '--info=backup1', f'{src}', f'{TODIR}/')
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=60)
    except subprocess.TimeoutExpired:
        test_fail("rsync hung under the backup-write fault (protocol deadlock)")

    output = proc.stdout + proc.stderr
    print(output, end='')

    # Confirm the fault shim actually fired: if rsync wrote the WHOLE backup
    # (basis-sized, all 'O'), the interposition didn't take effect and the test
    # would be vacuous -- skip rather than pass on a no-op.
    if backup.exists():
        data = backup.read_bytes()
        if len(data) == BASIS_SIZE and data == b'O' * BASIS_SIZE:
            test_skipped("LD_PRELOAD write-shim did not interpose the backup "
                         "write (full backup written); cannot inject the fault")

    failures = []

    # --- the core guarantee: the original destination is preserved ----------
    # Buggy binary: the destination is overwritten in place and zero-filled
    # past the truncated basis, so post != pre -- the original is lost.
    # Fixed binary: the file is skipped before any write to the destination,
    # so it is byte-for-byte unchanged.
    post = dst.read_bytes()
    if post != pre:
        failures.append(
            "original destination was modified under a failed backup -- the "
            "preservation guarantee is broken (the destination must be left "
            "byte-intact when the backup basis cannot be created)")

    # --- failure must be surfaced, not silent -------------------------------
    if proc.returncode == 0:
        failures.append(
            "rsync exited 0 despite a failed backup-copy write -- the "
            "failure is silent (expected a non-zero exit surfacing it)")

    if 'backed up' in output:
        failures.append(
            "rsync logged 'backed up ...' for a backup whose copy failed")

    # --- no truncated backup left finalized ---------------------------------
    if backup.exists():
        sz = backup.stat().st_size
        failures.append(
            f"a partial backup ({sz} of {BASIS_SIZE} bytes) was left behind "
            f"instead of being discarded -- the failed copy must not survive")

    # --- POSITIVE CONTROL: the success path actually transfers and backs up --
    # The fault scenario above only proves the failure branch is safe.  It does
    # NOT prove the success branch works: a mutant that makes the backup block
    # skip the file UNCONDITIONALLY (even when copy_file succeeds) would pass
    # every assertion above, because they all hold trivially when nothing is
    # transferred.  Re-run the SAME --inplace --backup --no-whole-file delta on
    # a fresh dest/source pair WITHOUT the fault shim and assert the transfer
    # really happened and a valid backup was made.
    rmtree(FROMDIR)
    rmtree(TODIR)
    makepath(FROMDIR, TODIR)

    src.write_bytes(b'N' * BASIS_SIZE)
    dst.write_bytes(b'O' * BASIS_SIZE)
    os.utime(dst, (1577836800, 1577836800))   # 2020-01-01
    os.utime(src, (1749254400, 1749254400))    # 2025-06-07

    orig_dst = dst.read_bytes()                 # the bytes the backup must hold

    argv = rsync_argv('-a', '--inplace', '--backup', '--no-whole-file',
                      '--info=backup1', f'{src}', f'{TODIR}/')
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    output = proc.stdout + proc.stderr
    print(output, end='')

    if proc.returncode != 0:
        failures.append(
            f"the no-fault control transfer failed (exit {proc.returncode}) -- "
            "the --inplace --backup success path is broken")
    # (a) the destination must now equal the SOURCE -- the transfer happened.
    if dst.read_bytes() != b'N' * BASIS_SIZE:
        failures.append(
            "the no-fault control left the destination != source -- the "
            "--inplace --backup delta transfer did not actually run (a mutant "
            "that skips the file unconditionally would land here)")
    # (b) the backup must EXIST and equal the ORIGINAL destination content.
    if not backup.exists():
        failures.append(
            "the no-fault control produced no backup file -- the success path "
            "must create the delta-basis backup")
    elif backup.read_bytes() != orig_dst:
        failures.append(
            "the no-fault control's backup does not equal the original "
            "destination content -- the backup is not a valid copy")

    if failures:
        test_fail("inplace-backup write-error not handled safely:\n  - "
                  + "\n  - ".join(failures))

    print("OK: original destination preserved byte-intact, backup-copy write "
          f"error surfaced (exit {proc.returncode}), and no corrupt backup "
          "finalized; success-path control transferred the file and made a "
          "valid backup")


main()
