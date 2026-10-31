"""Explicit disposable qualification binding; ordinary developer runs are unchanged.

The updater admits/creates the private run root. This reader proves its identity,
protected-root separation and environment again before shell work or runner I/O.
It is not a sandbox for arbitrary test code and grants no installation authority.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import uuid


class BoundScratch:
    def __init__(self, binding: Path, repo: Path):
        self.binding = Path(binding)
        self.repo = Path(repo)
        self.payload = self._read()
        data = json.loads(self.payload)
        if (not isinstance(data, dict) or data.get("schema") != "hermes.test-scratch.v1"
                or set(data) != {"schema", "run_root", "device", "inode", "protected_roots"}
                or not isinstance(data["run_root"], str)
                or type(data["device"]) is not int or type(data["inode"]) is not int
                or not isinstance(data["protected_roots"], list) or not data["protected_roots"]
                or not all(isinstance(p, str) for p in data["protected_roots"])):
            raise ValueError("scratch binding: invalid schema")
        self.root = Path(data["run_root"])
        self.identity = (data["device"], data["inode"])
        self.protected = [Path(p) for p in data["protected_roots"]]
        self.scratch = self.root / "tmp/pytest"
        self.attempts: dict[str, tuple[int, int]] = {}
        self.validate()

    def _read(self) -> bytes:
        with os.fdopen(os.open(self.binding, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("scratch binding: unowned or writable binding")
            return stream.read()

    @staticmethod
    def _canonical(path: Path) -> None:
        if not path.is_absolute() or path != path.resolve():
            raise ValueError(f"scratch binding: noncanonical or linked path: {path}")

    def validate(self) -> None:
        self._canonical(self.root)
        info = self.root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or (info.st_dev, info.st_ino) != self.identity):
            raise ValueError("scratch binding: private run-root identity changed")
        if self._read() != self.payload:
            raise ValueError("scratch binding: binding changed")
        expected = self.root / "user/.hermes/runner-binding.json"
        if self.binding != expected:
            raise ValueError("scratch binding: binding is not owned by this run")
        for path in (self.binding, self.repo, self.root / "user", self.root / "user/.hermes",
                     self.root / "tmp", self.scratch, *self.protected):
            self._canonical(path)
        if not self.repo.is_relative_to(self.root):
            raise ValueError("scratch binding: checkout is outside the admitted run root")
        for protected in self.protected:
            if self.root.is_relative_to(protected) or protected.is_relative_to(self.root):
                raise ValueError("scratch binding: run root overlaps a protected destination")
        for name, expected in (("HOME", self.root / "user"),
                               ("HERMES_HOME", self.root / "user/.hermes"),
                               ("TMPDIR", self.root / "tmp")):
            if os.environ.get(name) != str(expected) or not expected.is_dir():
                raise ValueError(f"scratch binding: {name} is not bound to this run")

    @contextmanager
    def _scratch_fd(self, *, create: bool):
        self.validate()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = os.open(self.root, flags)
        try:
            info = os.fstat(root)
            if (info.st_dev, info.st_ino) != self.identity:
                raise ValueError("scratch binding: run root replaced")
            tmp = os.open("tmp", flags, dir_fd=root)
            try:
                if create:
                    try:
                        os.mkdir("pytest", mode=0o700, dir_fd=tmp)
                    except FileExistsError:
                        pass
                scratch = os.open("pytest", flags, dir_fd=tmp)
                try:
                    yield scratch
                finally:
                    os.close(scratch)
            finally:
                os.close(tmp)
        finally:
            os.close(root)

    def new_attempt(self) -> str:
        name = "r-" + uuid.uuid4().hex[:8]
        with self._scratch_fd(create=True) as scratch:
            os.mkdir(name, mode=0o700, dir_fd=scratch)
            info = os.stat(name, dir_fd=scratch, follow_symlinks=False)
            self.attempts[name] = (info.st_dev, info.st_ino)
        self.validate()
        return str(self.scratch / name)

    def cleanup(self, attempt: str | Path, kill=None) -> None:
        attempt = Path(attempt)
        self._canonical(attempt)
        if attempt.parent != self.scratch or attempt.name not in self.attempts:
            raise ValueError("scratch binding: cleanup does not own this attempt")
        with self._scratch_fd(create=False) as scratch:
            info = os.stat(attempt.name, dir_fd=scratch, follow_symlinks=False)
            if (not stat.S_ISDIR(info.st_mode)
                    or (info.st_dev, info.st_ino) != self.attempts[attempt.name]):
                raise ValueError("scratch binding: cleanup attempt replaced")
            if kill is not None:
                kill(str(attempt))
            # Repair readonly fixture directories through owned descriptors;
            # never chmod a link target (including a protected external home).
            os.chmod(attempt.name, 0o700, dir_fd=scratch, follow_symlinks=False)
            for _path, directories, _files, parent in os.fwalk(
                    attempt.name, dir_fd=scratch, follow_symlinks=False):
                for name in directories:
                    if not stat.S_ISLNK(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
                        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
            shutil.rmtree(attempt.name, dir_fd=scratch)
            del self.attempts[attempt.name]


def configure(binding: str | None, repo: Path) -> BoundScratch | None:
    """No fallback from a requested/marked qualification to shared scratch."""
    marker = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "runner-binding.json"
    if binding is None:
        if marker.exists() or marker.is_symlink():
            raise ValueError("scratch binding: qualification requires explicit --scratch-binding")
        return None
    if os.name != "posix":
        raise ValueError("scratch binding: this qualification requires POSIX directory custody")
    try:
        return BoundScratch(Path(binding), repo)
    except (OSError, ValueError, TypeError) as error:
        raise ValueError(f"scratch binding: {error}") from error
