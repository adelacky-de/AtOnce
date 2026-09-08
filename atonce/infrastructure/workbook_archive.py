"""Recoverable archive handling for an edited returned XLSX before downstream refresh."""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


class WorkbookArchiveError(RuntimeError):
    pass


@dataclass
class WorkbookArchive:
    original_path: str
    archive_path: str
    staged: bool = False

    def stage(self) -> None:
        source = Path(self.original_path)
        archive = Path(self.archive_path)
        if self.staged:
            raise WorkbookArchiveError("Returned workbook archive was already staged.")
        if not source.is_file():
            raise WorkbookArchiveError(f"Returned workbook no longer exists: {source}")
        if archive.exists():
            raise WorkbookArchiveError(f"Archive target already exists: {archive}")
        os.replace(str(source), str(archive))
        self.staged = True

    def preserve_original_copy(self) -> None:
        """Restore a byte-identical working copy while retaining audit evidence.

        Selective propagation may intentionally skip the returned workbook's own
        delivery edge. In that case AtOnce must not make the linked workbook
        disappear merely because audit evidence is required. The staged archive
        remains immutable evidence while this method recreates the original path
        byte-for-byte. It refuses to overwrite any file created in the meantime.
        """

        if not self.staged:
            raise WorkbookArchiveError("Returned workbook archive was not staged.")
        source = Path(self.original_path)
        archive = Path(self.archive_path)
        if source.exists():
            raise WorkbookArchiveError(
                f"Cannot preserve skipped returned workbook because target already exists: {source}"
            )
        if not archive.is_file():
            raise WorkbookArchiveError(f"Returned workbook archive is missing: {archive}")
        try:
            shutil.copy2(str(archive), str(source))
        except Exception as exc:
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
            raise WorkbookArchiveError(
                f"Could not preserve skipped returned workbook at its original path: {exc}"
            ) from exc

    def restore(self) -> None:
        if not self.staged:
            return
        source = Path(self.original_path)
        archive = Path(self.archive_path)
        if source.exists():
            raise WorkbookArchiveError(
                f"Cannot restore returned workbook because target already exists: {source}"
            )
        if not archive.is_file():
            raise WorkbookArchiveError(f"Returned workbook archive is missing: {archive}")
        os.replace(str(archive), str(source))
        self.staged = False

    def keep(self) -> str:
        if not self.staged or not Path(self.archive_path).is_file():
            raise WorkbookArchiveError("Returned workbook archive was not preserved successfully.")
        return self.archive_path


def plan_workbook_archive(path: str, sync_id: str, revision_number: int) -> WorkbookArchive:
    source = Path(path).expanduser()
    if not source.is_file():
        raise WorkbookArchiveError(f"Returned workbook does not exist: {source}")
    suffix = source.suffix or ".xlsx"
    stem = source.stem
    candidate = source.with_name(
        f".{stem}.atonce-returned-rev{revision_number}-{sync_id[:12]}{suffix}"
    )
    return WorkbookArchive(str(source), str(candidate))
