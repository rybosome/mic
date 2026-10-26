"""Opt-in capture of application stdout/stderr, attributed to the trial that wrote it.

Only Python-level writes through ``sys.stdout``/``sys.stderr`` are seen. Writes to the
underlying file descriptors (C extensions, subprocesses) and handlers that kept a
reference to an original stream bypass capture. Attribution follows ``contextvars``, so
threads started by application code without copying the context count as run-level.
"""

import io
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from ..errors import ConfigurationError
from ..models import JsonObject
from ..results import Failure, failure
from .validation import dumps

OUTPUT_FILE = "output.jsonl"
SCHEMA_VERSION = "mic-output-v1"
# Recording stops here so chatty application code cannot fill the disk; echo continues.
MAX_RECORDED_BYTES = 64 * 1024 * 1024
# Output without a newline is held until it completes, up to this size.
_PARTIAL_LIMIT = 4096


@dataclass(frozen=True)
class Origin:
    run_id: str
    evaluation: str
    source_id: str
    row_index: int
    trial: int
    scorer: str | None = None


# Receives complete captured text as (origin, stream name, text); None origin is run-level.
Echo = Callable[[Origin | None, str, str], None]

_origin: ContextVar[Origin | None] = ContextVar("mic_output_origin", default=None)
_install = threading.Lock()
_active: "OutputCapture | None" = None


@contextmanager
def attributed(origin: Origin) -> Generator[None]:
    """Attribute writes in this context to ``origin``; held partial lines flush on exit."""
    token = _origin.set(origin)
    try:
        yield
    finally:
        _origin.reset(token)
        if (capture := _active) is not None and capture.run_id == origin.run_id:
            capture.flush(origin)


@contextmanager
def scoring(name: str) -> Generator[None]:
    current = _origin.get()
    if current is None:
        yield
        return
    if (capture := _active) is not None and capture.run_id == current.run_id:
        capture.flush(current)  # keep a task's unterminated line ahead of scorer output
    with attributed(replace(current, scorer=name)):
        yield


class OutputCapture:
    """Replaces the process's ``sys.stdout``/``sys.stderr`` for the duration of one run.

    Captured output is never passed to the original streams. Complete lines are recorded
    to ``output.jsonl`` once a directory is attached, and handed to ``echo`` if given.
    """

    def __init__(self, run_id: str, *, record: bool, echo: Echo | None = None) -> None:
        self.run_id = run_id
        self.error: Failure | None = None
        self._echo = echo
        # RLock plus a guard: a write made while emitting (e.g. by a failing echo) is
        # dropped instead of deadlocking or reordering.
        self._lock = threading.RLock()
        self._emitting = False
        self._buffers: dict[tuple[Origin | None, str], str] = {}
        self._pending: list[str] | None = [] if record else None
        self._file: TextIO | None = None
        self._recorded = 0
        self._truncated = False
        self._streams: list[tuple[str, TextIO, _CapturedStream]] = []
        self._closed = False

    def install(self) -> None:
        global _active
        with _install:
            if _active is not None:
                raise ConfigurationError("Only one run at a time can capture output")
            _active = self
            for name in ("stdout", "stderr"):
                original: TextIO = getattr(sys, name)
                stream = _CapturedStream(self, name, original)
                self._streams.append((name, original, stream))
                setattr(sys, name, stream)

    def record_to(self, path: Path) -> None:
        """Start recording; output captured before this point is written first."""
        with self._lock:
            pending, self._pending = self._pending, None
            if pending is None or self._closed:
                return
            try:
                self._file = path.open("x", encoding="utf-8", errors="backslashreplace")
                self._file.write("".join(pending))
                self._file.flush()
            except Exception as exc:
                self._fail(exc)

    @property
    def closed(self) -> bool:
        return self._closed

    def feed(self, origin: Origin | None, stream: str, text: str) -> None:
        with self._lock:
            if self._emitting:
                return
            key = (origin, stream)
            buffered = self._buffers.pop(key, "") + text
            head, newline, rest = buffered.rpartition("\n")
            if newline:
                complete = head + newline
            elif len(buffered) >= _PARTIAL_LIMIT:
                complete, rest = buffered, ""
            else:
                complete = ""
            if rest:
                self._buffers[key] = rest
            if complete:
                self._emit(origin, stream, complete)

    def flush(self, origin: Origin | None) -> None:
        """Emit held partial lines for ``origin`` as they are."""
        with self._lock:
            for stream in ("stdout", "stderr"):
                text = self._buffers.pop((origin, stream), "")
                if text:
                    self._emit(origin, stream, text)

    def close(self) -> None:
        """Restore the original streams, emit held partial lines, and close the record."""
        global _active
        with _install:
            if self._closed:
                return
            for name, original, stream in self._streams:
                if getattr(sys, name) is stream:
                    setattr(sys, name, original)
            if _active is self:
                _active = None
        with self._lock:
            for origin, stream in list(self._buffers):
                text = self._buffers.pop((origin, stream))
                self._emit(origin, stream, text)
            self._closed = True
            self._echo = None
            self._pending = None
            if self._file is not None:
                try:
                    self._file.close()
                except Exception as exc:
                    self._fail(exc)
                self._file = None

    def _emit(self, origin: Origin | None, stream: str, text: str) -> None:
        self._emitting = True
        try:
            if self._pending is not None or self._file is not None:
                self._record(origin, stream, text)
            if self._echo is not None:
                try:
                    self._echo(origin, stream, text)
                except Exception:
                    self._echo = None  # showing output is advisory and never fails a run
        finally:
            self._emitting = False

    def _record(self, origin: Origin | None, stream: str, text: str) -> None:
        if self._truncated:
            return
        size = len(text.encode("utf-8", "backslashreplace"))
        if self._recorded + size > MAX_RECORDED_BYTES:
            self._truncated = True
            line = _line({"type": "truncated", "limit_bytes": MAX_RECORDED_BYTES})
        else:
            self._recorded += size
            line = _line(
                {
                    "type": "output",
                    "stream": stream,
                    "evaluation": None if origin is None else origin.evaluation,
                    "source_id": None if origin is None else origin.source_id,
                    "row_index": None if origin is None else origin.row_index,
                    "trial": None if origin is None else origin.trial,
                    "scorer": None if origin is None else origin.scorer,
                    "text": text,
                }
            )
        if self._pending is not None:
            self._pending.append(line)
            return
        if self._file is None:
            return
        try:
            self._file.write(line)
            self._file.flush()
        except Exception as exc:
            self._fail(exc)

    def _fail(self, exc: Exception) -> None:
        self.error = failure("output", exc)
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        self._file = None


def _line(record: JsonObject) -> str:
    return dumps({"schema_version": SCHEMA_VERSION, **record}) + "\n"


class _CapturedStream(io.TextIOBase):
    """A text stream that routes writes to the active capture, never to the terminal."""

    def __init__(self, capture: OutputCapture, name: str, original: TextIO) -> None:
        super().__init__()
        self._capture = capture
        self._name = name
        self._original = original

    def write(self, text: str) -> int:
        if not isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        origin = _origin.get()
        if self._capture.closed or (origin is not None and origin.run_id != self._capture.run_id):
            # Kept references after the run, and concurrent runs that do not capture,
            # write normally.
            return self._original.write(text)
        self._capture.feed(origin, self._name, text)
        return len(text)

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass  # application code closing sys.stdout must not end the capture

    @property
    def encoding(self) -> str:  # pyright: ignore[reportIncompatibleVariableOverride]
        return getattr(self._original, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str | None:  # pyright: ignore[reportIncompatibleVariableOverride]
        return getattr(self._original, "errors", None)
