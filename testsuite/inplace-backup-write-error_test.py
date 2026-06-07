#!/usr/bin/env python3
"""Regression test for the inplace-backup write-error silent-failure bug.

With `--inplace --backup` and a DELTA transfer to an existing basis file,
recv_generator opens the backup target (f_copy) and copies the basis into it,
block by block, from generate_and_send_sums() via full_write(f_copy, ...).
Before the fix that write's return value was DISCARDED.

full_write() loops over short writes, so a short write is not a corruption
vector -- but a genuine write *error* (ENOSPC/EDQUOT/EFBIG) is: the backup is
left truncated, yet the cleanup path still finalizes it (set_file_attrs + the
"backed up X to X~" log) AND the receiver overwrites the original in place.
Result on the buggy binary: rsync exits 0, reports a successful backup, and the
only surviving copy of the old data is a truncated fragment -- silent data loss.

This test injects exactly that fault with an LD_PRELOAD shim
(backup-write-fail-preload.c) that fails write() to the backup-copy fd ONLY,
after a small prefix, leaving every other fd (notably the in-place rewrite of
the original) untouched. It then asserts the fixed behaviour:

  * rsync exits non-zero (the failure is surfaced, not hidden), and
  * no truncated backup is left finalized as a valid "backed up" file.

Against the pre-fix binary the same run exits 0 and leaves a truncated file~,
so this test FAILS there -- i.e. it has teeth.

Mechanism note: an LD_PRELOAD write()-shim is used (rather than RLIMIT_FSIZE,
which would also cap the in-place write and not isolate the backup, or a tiny
full filesystem, which needs root here) so the fault hits the backup write and
nothing else. The test SKIPs (77) if no C compiler is available to build the
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
    # --no-whole-file -- via the delta path that exercises f_copy. Distinct
    # mtimes ensure rsync does not quick-check the pair as already-identical.
    dst.write_bytes(b'O' * BASIS_SIZE)
    src.write_bytes(b'N' * BASIS_SIZE)
    os.utime(dst, (1577836800, 1577836800))   # 2020-01-01
    os.utime(src, (1749254400, 1749254400))    # 2025-06-07

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

    # --- the silent-failure signature --------------------------------------
    # Buggy binary: exit 0, "backed up ... to file~" logged, and a TRUNCATED
    # file~ left behind finalized as valid (only the prefix the shim allowed).
    # Fixed binary: the write error is surfaced (non-zero exit) and the
    # truncated backup is discarded rather than finalized as a valid backup.
    failures = []

    if proc.returncode == 0:
        failures.append(
            "rsync exited 0 despite a failed backup-copy write -- the "
            "failure is silent (expected a non-zero exit surfacing it)")

    if 'backed up' in output and proc.returncode == 0:
        failures.append(
            "rsync logged 'backed up ...' for a backup whose copy failed")

    if backup.exists():
        sz = backup.stat().st_size
        if sz < BASIS_SIZE:
            failures.append(
                f"a truncated backup ({sz} of {BASIS_SIZE} bytes) was left "
                f"finalized as a valid 'backed up' file instead of being "
                f"discarded -- corrupt backup silently accepted as good")

    if failures:
        test_fail("inplace-backup write-error failure not surfaced:\n  - "
                  + "\n  - ".join(failures))

    # Positive end-state checks for the fixed binary.
    if backup.exists():
        test_fail(f"fixed path should have discarded the truncated backup, "
                  f"but {backup} still exists "
                  f"({backup.stat().st_size} bytes)")

    print("OK: backup-copy write error surfaced (exit "
          f"{proc.returncode}) and no corrupt backup finalized")


main()
