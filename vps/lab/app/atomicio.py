"""Atomic file writes for everything the Lab keeps on its volumes (#152, #180).

`open(path, "w")` truncates first and writes after: a crash, a full disk or a reader (the
nightly backup) in between sees an empty or half file, and a crash loses the file outright.
Every Lab writer goes through here instead: the bytes go to a unique temp file in the SAME
directory, are fsynced, `os.replace`d over the target, and the directory is fsynced, so a
reader sees the old file or the new one, never a mix, and a power cut keeps one of them.
"""
import contextlib
import json
import os
import tempfile


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def atomic_open(path, mode="w", **kw):
    """`with atomic_open(p, "w") as f: ...` — the target is replaced only if the block succeeds."""
    if mode not in ("w", "wb"):
        raise ValueError(f"atomic_open writes whole files only (got mode {mode!r})")
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, **kw) as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    _fsync_dir(d)


def write_json(path, obj, **dump_kw):
    with atomic_open(path, "w") as f:
        json.dump(obj, f, **dump_kw)


def write_bytes(path, data: bytes):
    with atomic_open(path, "wb") as f:
        f.write(data)


def copy_file(src, dest):
    """shutil.copy2 semantics (bytes + mtime/mode), atomically at `dest`."""
    import shutil
    with open(src, "rb") as s, atomic_open(dest, "wb") as f:
        shutil.copyfileobj(s, f)
    shutil.copystat(src, dest)


def tmp_name(path):
    """A not-yet-existing temp path beside `path`, for tools that write the file themselves
    (ogr2ogr refuses an existing output). Pair with `commit`."""
    d, b = os.path.split(os.path.abspath(path))
    return os.path.join(d, f".{b}.{os.getpid()}.{os.urandom(4).hex()}.tmp")


def commit(tmp, path):
    """Make a finished temp file the target, durably (fsync, replace, fsync dir)."""
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(os.path.abspath(path)))
