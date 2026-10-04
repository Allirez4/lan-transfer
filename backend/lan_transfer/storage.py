"""Portable path checks and same-volume, no-replace publication."""
import hashlib
import os
from pathlib import Path
import re
import stat
import unicodedata


def filesystem_name(path: Path):
    """Detect Windows volume limits, including mounted volumes; unknown elsewhere."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    root = ctypes.create_unicode_buffer(32768)
    name = ctypes.create_unicode_buffer(256)
    api.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    if not api.GetVolumePathNameW(str(path.absolute()), root, len(root)):
        return None
    if not api.GetVolumeInformationW(root.value, None, 0, None, None, None, name, len(name)):
        return None
    return name.value


def check_file_size(size, filesystem):
    if filesystem and filesystem.upper() in ('FAT', 'FAT32') and size >= 2**32:
        raise ValueError('FAT32 cannot store a file of 4 GiB or larger. Choose an NTFS, exFAT or other large-file volume.')


def relative_path(value: str) -> str:
    value = unicodedata.normalize("NFC", value)
    parts = value.split("/")
    if not value or len(value) > 16000 or "\\" in value or any(
        not p or p in (".", "..") or p[-1:] in (" ", ".")
        or any(ord(c) < 32 or c in '<>:"|?*' for c in p)
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", p, re.I)
        for p in parts
    ):
        raise ValueError("Unsafe path: use relative names without traversal, device names or Windows-reserved characters")
    return value


def is_link(path: Path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def confined(root: Path, name: str, mkdir=False, normalize=True) -> Path:
    validated = relative_path(name)
    name = validated if normalize else name
    if any(is_link(path) for path in [root, *root.parents]):
        raise ValueError("Storage/share root must not be a symlink or junction")
    current = root
    for part in name.split("/"):
        current = current / part
        if is_link(current):
            raise ValueError("Symlinks and junctions are not supported")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes its allowed root")
    if mkdir:
        current.parent.mkdir(parents=True, exist_ok=True)
    return current


def fsync_dir(path: Path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish(source: Path, destination: Path):
    if os.name == "nt":
        os.rename(source, destination)  # Windows rename fails if destination exists.
    else:
        os.link(source, destination)  # Atomic exclusive destination creation.
        fsync_dir(destination.parent)
        source.unlink()
    fsync_dir(destination.parent)
    fsync_dir(source.parent)


def digest_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def version(path: Path):
    s = path.stat()
    if not stat.S_ISREG(s.st_mode):
        raise ValueError("Source is not an ordinary file")
    return hashlib.sha256(f"{s.st_dev}:{s.st_ino}:{s.st_size}:{s.st_mtime_ns}:{s.st_ctime_ns}".encode()).hexdigest()


class ProcessLock:
    def __init__(self, path):
        self.file = open(path, "a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another LAN Transfer server is already using this storage directory") from None

    def close(self):
        self.file.close()
