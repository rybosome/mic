"""Atomic replacement of individual evidence files; no multi-file transaction."""

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


def atomic_write(destination: Path, chunks: Iterable[str]) -> None:
    """Preserve the previous file and primary error if writing or cleanup fails."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, prefix=".mic-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            for chunk in chunks:
                stream.write(chunk)
        # Close before replacing, including on Windows.
        os.replace(temporary, destination)
    except BaseException as exc:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                exc.add_note(f"Temporary evidence cleanup also failed: {cleanup_error}")
        raise
