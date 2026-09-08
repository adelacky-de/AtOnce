"""Recoverable multi-file replacement for downstream outputs."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List
from uuid import uuid4


class OutputTransactionError(RuntimeError):
    """Raised when staged outputs cannot be installed safely."""


@dataclass(frozen=True)
class StagedOutput:
    staged_path: str
    target_path: str


def sqlite_sidecar_paths(path: str) -> List[Path]:
    """Return transient SQLite journal paths associated with one database path."""

    base = Path(path)
    return [
        Path(f"{base}-wal"),
        Path(f"{base}-shm"),
        Path(f"{base}-journal"),
    ]


def cleanup_staging_artifacts(path: str) -> None:
    """Remove a staging path and transient SQLite sidecars on a best-effort basis.

    Staging artifacts are never authoritative outputs. This helper is used only
    after a stage has either been moved into its final destination or abandoned.
    """

    primary = Path(path)
    shapefile_sidecars = [primary.with_suffix(suffix) for suffix in (".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj")]
    for candidate in [primary, *shapefile_sidecars, *sqlite_sidecar_paths(path)]:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            # A stale staging artifact is preferable to deleting any final output.
            pass


class OutputTransaction:
    """Install staged files while retaining enough state to roll back.

    Existing targets are moved to same-directory backups before replacement. The
    caller must call ``finalize`` only after durable revision metadata is saved;
    otherwise ``rollback`` restores the previous outputs.
    """

    def __init__(self, outputs: Iterable[StagedOutput], obsolete_targets: Iterable[str] = ()):
        self.outputs = list(outputs)
        self.obsolete_targets = [str(path) for path in obsolete_targets]
        self.backups: Dict[str, str] = {}
        self.installed: List[str] = []
        self._prepared = False

    def install(self) -> None:
        if self._prepared:
            raise OutputTransactionError("Output transaction was already used.")
        self._prepared = True

        try:
            for output in self.outputs:
                staged = Path(output.staged_path)
                if not staged.is_file():
                    raise OutputTransactionError(
                        f"Staged output is missing before install: {staged}"
                    )

            # Back up the complete old target set before installing any new member.
            backup_targets = [Path(output.target_path) for output in self.outputs]
            backup_targets.extend(Path(path) for path in self.obsolete_targets)
            seen_targets = set()
            for target in backup_targets:
                if str(target) in seen_targets:
                    continue
                seen_targets.add(str(target))
                if target.exists():
                    backup = target.with_name(
                        f".{target.name}.atonce-backup-{uuid4().hex}"
                    )
                    os.replace(str(target), str(backup))
                    self.backups[str(target)] = str(backup)

            for output in self.outputs:
                staged = Path(output.staged_path)
                target = Path(output.target_path)
                os.replace(str(staged), str(target))
                self.installed.append(str(target))
        except Exception as exc:
            self.rollback()
            if isinstance(exc, OutputTransactionError):
                raise
            raise OutputTransactionError(f"Could not install downstream outputs: {exc}") from exc

    def finalize(self) -> None:
        for backup in self.backups.values():
            try:
                Path(backup).unlink(missing_ok=True)
            except OSError:
                # A stale backup is safer than deleting a current output.
                pass
        for obsolete in self.obsolete_targets:
            try:
                Path(obsolete).unlink(missing_ok=True)
            except OSError:
                # Never make a successful revision fail because an obsolete sidecar
                # is already absent or held by an external reader.
                pass
        for output in self.outputs:
            cleanup_staging_artifacts(output.staged_path)
        self.backups.clear()
        self.installed.clear()

    def rollback(self) -> None:
        for target in reversed(self.installed):
            try:
                Path(target).unlink(missing_ok=True)
            except OSError:
                pass

        for target, backup in self.backups.items():
            backup_path = Path(backup)
            if backup_path.exists():
                try:
                    os.replace(str(backup_path), target)
                except OSError:
                    pass

        for output in self.outputs:
            cleanup_staging_artifacts(output.staged_path)

        self.installed.clear()
        self.backups.clear()


def staging_path_for(target_path: str) -> str:
    """Return a unique same-directory staging path preserving the target suffix."""

    target = Path(target_path)
    suffix = "".join(target.suffixes) or target.suffix
    stem = target.name[: -len(suffix)] if suffix else target.name
    return str(target.with_name(f".{stem}.atonce-stage-{uuid4().hex}{suffix}"))
