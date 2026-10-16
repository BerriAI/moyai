"""Immutable packaged skill files for Harness's separate catalog workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic_ai.workspaces import FileEntry, WorkspaceReadOnlyError
from pydantic_ai_harness import Skills

from .contracts import RunScope


@dataclass(frozen=True, slots=True)
class BundledSkillWorkspace:
    root: str
    files: tuple[tuple[str, bytes], ...]
    entries: tuple[FileEntry, ...]

    @classmethod
    def capture(cls, source: Path, destination: str) -> BundledSkillWorkspace:
        # Capture before constructing the agent: workflow replay must do no disk I/O.
        if not source.is_dir():
            raise NotADirectoryError(source)
        root = PurePosixPath(destination)
        if not root.is_absolute():
            raise ValueError("The bundled skill catalog needs an absolute destination.")
        files = tuple(
            (str(root / path.parent.name / path.name), path.read_bytes())
            for path in sorted(source.glob("*/SKILL.md")) if path.is_file()
        )
        directories = (root, *(PurePosixPath(path).parent for path, _ in files))
        entries = (
            *(FileEntry(name=path.name, path=str(path), is_dir=True, size=None) for path in directories),
            *(FileEntry(name=PurePosixPath(path).name, path=path, is_dir=False, size=len(content))
              for path, content in files),
        )
        return cls(str(root), files, entries)

    @property
    def ref(self) -> None:
        return None

    async def working_dir(self) -> str:
        return self.root

    async def stat(self, path: str) -> FileEntry:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise FileNotFoundError(path)

    async def list_dir(self, path: str) -> tuple[FileEntry, ...]:
        if not (await self.stat(path)).is_dir:
            raise NotADirectoryError(path)
        return tuple(entry for entry in self.entries if str(PurePosixPath(entry.path).parent) == path)

    async def read_bytes(self, path: str) -> bytes:
        if (await self.stat(path)).is_dir:
            raise IsADirectoryError(path)
        return next(content for name, content in self.files if name == path)

    async def exists(self, path: str) -> bool:
        return any(entry.path == path for entry in self.entries)

    async def write_bytes(self, path: str, data: bytes) -> None:
        raise WorkspaceReadOnlyError("Bundled skill catalogs are immutable.")

    async def make_dir(self, path: str) -> None:
        raise WorkspaceReadOnlyError("Bundled skill catalogs are immutable.")

    async def remove(self, path: str) -> None:
        raise WorkspaceReadOnlyError("Bundled skill catalogs are immutable.")


def bundled_skill_catalog(source: Path, destination: str) -> Skills[RunScope]:
    return Skills(destination, workspace=BundledSkillWorkspace.capture(source, destination))
