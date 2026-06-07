/* LD_PRELOAD write()-failure shim for the inplace-backup regression test
 * (testsuite/inplace-backup-write-error_test.py).
 *
 * Interposes open()/openat() to identify the inplace backup-copy file
 * descriptor -- the destination whose path ends in the backup suffix (default
 * "~") -- then makes write() to THAT fd fail with ENOSPC once a small prefix
 * has been written.  This models a genuine mid-copy write error (a full disk)
 * on the backup target ONLY: every other fd, including the in-place rewrite of
 * the original file, is left untouched.  That isolation is the point -- it
 * exercises the unchecked full_write(f_copy, ...) in generator.c without
 * perturbing the rest of the transfer (unlike RLIMIT_FSIZE, which would also
 * cap the in-place write, or a full filesystem, which needs root here).
 *
 * Note rsync's full_write() loops over short writes, so a short write is not a
 * corruption vector; only a real write() *error* is, which is what this shim
 * injects.
 *
 * Tunable via environment:
 *   BWF_SUFFIX  backup-name suffix to target (default "~")
 *   BWF_PREFIX  bytes to let through before failing (default 2048)
 *
 * Self-contained: no rsync headers; links only against libc + libdl.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/types.h>

#define MAX_FDS 4096

static int marked[MAX_FDS];          /* 1 if this fd is the backup-copy fd */
static long long written[MAX_FDS];   /* bytes allowed through so far */

static const char *bwf_suffix(void) {
    const char *s = getenv("BWF_SUFFIX");
    return (s && *s) ? s : "~";
}

static long bwf_prefix(void) {
    const char *s = getenv("BWF_PREFIX");
    return (s && *s) ? atol(s) : 2048;
}

static int path_is_backup(const char *path) {
    if (!path) return 0;
    const char *suf = bwf_suffix();
    size_t pl = strlen(path), sl = strlen(suf);
    return pl >= sl && memcmp(path + pl - sl, suf, sl) == 0;
}

static void mark_if_backup(int fd, const char *path) {
    if (fd >= 0 && fd < MAX_FDS && path_is_backup(path)) {
        marked[fd] = 1;
        written[fd] = 0;
    }
}

typedef int (*open_fn)(const char *, int, ...);
typedef int (*openat_fn)(int, const char *, int, ...);
typedef ssize_t (*write_fn)(int, const void *, size_t);
typedef int (*close_fn)(int);

static int do_open(open_fn real, const char *path, int flags, va_list ap) {
    mode_t mode = 0;
    if (flags & O_CREAT) mode = va_arg(ap, mode_t);
    int fd = real(path, flags, mode);
    mark_if_backup(fd, path);
    return fd;
}

int open(const char *path, int flags, ...) {
    static open_fn real;
    if (!real) real = (open_fn)dlsym(RTLD_NEXT, "open");
    va_list ap; va_start(ap, flags);
    int fd = do_open(real, path, flags, ap);
    va_end(ap);
    return fd;
}

int open64(const char *path, int flags, ...) {
    static open_fn real;
    if (!real) real = (open_fn)dlsym(RTLD_NEXT, "open64");
    va_list ap; va_start(ap, flags);
    int fd = do_open(real, path, flags, ap);
    va_end(ap);
    return fd;
}

int openat(int dirfd, const char *path, int flags, ...) {
    static openat_fn real;
    if (!real) real = (openat_fn)dlsym(RTLD_NEXT, "openat");
    mode_t mode = 0;
    va_list ap; va_start(ap, flags);
    if (flags & O_CREAT) mode = va_arg(ap, mode_t);
    va_end(ap);
    int fd = real(dirfd, path, flags, mode);
    mark_if_backup(fd, path);
    return fd;
}

ssize_t write(int fd, const void *buf, size_t count) {
    static write_fn real;
    if (!real) real = (write_fn)dlsym(RTLD_NEXT, "write");
    if (fd >= 0 && fd < MAX_FDS && marked[fd]) {
        long pfx = bwf_prefix();
        if (written[fd] >= pfx) {
            errno = ENOSPC;
            return -1;
        }
        size_t room = (size_t)(pfx - written[fd]);
        if (count > room) count = room;
        ssize_t n = real(fd, buf, count);
        if (n > 0) written[fd] += n;
        return n;
    }
    return real(fd, buf, count);
}

int close(int fd) {
    static close_fn real;
    if (!real) real = (close_fn)dlsym(RTLD_NEXT, "close");
    if (fd >= 0 && fd < MAX_FDS) { marked[fd] = 0; written[fd] = 0; }
    return real(fd);
}
