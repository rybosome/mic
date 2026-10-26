"""Streaming CLI progress: one line per finished case, plus a live status line.

Progress is advisory and goes to stderr; the final summary stays the source of truth.
State is bounded by concurrency, not dataset size: a case is held only until its last
admitted trial finishes, and nothing beyond scores, statuses, exception type names,
scorer names, and case labels is ever shown.
"""

import os
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import astuple, dataclass, field
from typing import TextIO

from .._runtime.aggregation import Moments, TrialObservation
from .._runtime.capture import Echo, Origin
from .._runtime.progress import RunObserver, RunPlan, TrialError
from ..results import Failure, SourceSummary

_ERASE_LINE = "\r\x1b[2K"
# A trial is called out as slow only past both an absolute and a relative threshold.
_SLOW_SECONDS = 10.0
_SLOW_FACTOR = 3.0
_LABEL_WIDTH = 32
# Application output without a newline is held until it completes, up to this size.
_PARTIAL_LIMIT = 4096


@dataclass(frozen=True)
class Glyphs:
    completed: str
    task_failed: str
    scoring_failed: str
    cancelled: str
    rejected: str
    separator: str
    ellipsis: str
    dash: str
    spinner: str
    rule: str


UNICODE = Glyphs("✓", "✗", "!", "·", "-", " · ", "…", "–", "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "│")
ASCII = Glyphs("+", "x", "!", ".", "-", " | ", "...", "-", "|/-\\", "|")


def glyphs_for(stream: TextIO) -> Glyphs:
    try:
        "".join(astuple(UNICODE)).encode(stream.encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return ASCII
    return UNICODE


def text_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in ("W", "F")
        else 1
        for char in text
    )


def fit(text: str, width: int, ellipsis: str) -> str:
    """Truncate to a terminal column budget so a status line can never wrap."""
    if text_width(text) <= width:
        return text
    budget = max(width - text_width(ellipsis), 0)
    kept: list[str] = []
    used = 0
    for char in text:
        used += text_width(char)
        if used > budget:
            break
        kept.append(char)
    return "".join(kept) + ellipsis


def printable(text: str) -> str:
    """Case labels are dataset content; never let them move the cursor or emit escapes."""
    return "".join(char if char.isprintable() else "?" for char in text)


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return clock(seconds)


def clock(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def _number(value: float) -> str:
    return format(value, ".3g")


@dataclass
class _Case:
    label: str | None
    trials: dict[tuple[str, int], TrialObservation] = field(
        default_factory=dict[tuple[str, int], TrialObservation]
    )
    errors: dict[tuple[str, int], TrialError] = field(
        default_factory=dict[tuple[str, int], TrialError]
    )


@dataclass(frozen=True)
class _Source:
    name: str
    # (evaluation, trials per case) in execution order.
    tasks: tuple[tuple[str, int], ...]
    scorers: dict[str, tuple[str, ...]]

    @property
    def trials(self) -> int:
        return sum(trials for _, trials in self.tasks)


class ProgressState(RunObserver):
    """Turns observer notifications into finished lines and a status summary.

    Observer calls arrive on the event loop thread while a display thread renders, so
    every method takes the same lock.
    """

    def __init__(
        self,
        *,
        glyphs: Glyphs = UNICODE,
        color: bool = False,
        every_case: bool = True,
        scores: bool = False,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.glyphs = glyphs
        self._color = color
        self._every_case = every_case
        self._scores = scores
        self._now = now
        self._lock = threading.Lock()
        self._started = now()
        self._plan: RunPlan | None = None
        self._sources: dict[str, _Source] = {}
        self._reading: str | None = None
        self._reading_rows = 0
        self._sources_done = 0
        self._seen = self._accepted = self._rejected = 0
        self._finished = self._failed = 0
        self._latency = Moments()
        self._running: dict[tuple[str, str, int, int], float] = {}
        self._cases: dict[tuple[str, int], _Case] = {}
        self._lines: list[str] = []
        self._cancelling = False
        self._finishing: tuple[str, ...] | None = None

    # RunObserver -----------------------------------------------------------------

    def planned(self, run: RunPlan) -> None:
        with self._lock:
            self._plan = run
            self._started = self._now()
            tasks = {task.name: task for task in run.tasks}
            self._sources = {
                source.source_id: _Source(
                    source.name,
                    tuple((name, tasks[name].trials) for name in source.tasks),
                    {name: tasks[name].scorers for name in source.tasks},
                )
                for source in run.sources
            }
            self._lines.append(self._header(run))

    def source_started(self, source_id: str) -> None:
        with self._lock:
            self._reading = source_id
            self._reading_rows = 0

    def case_accepted(self, source_id: str, row_index: int, label: str | None) -> None:
        with self._lock:
            self._seen += 1
            self._accepted += 1
            if source_id == self._reading:
                self._reading_rows += 1
            self._cases[(source_id, row_index)] = _Case(label)

    def record_rejected(self, source_id: str, row_index: int, error: Failure) -> None:
        with self._lock:
            self._seen += 1
            self._rejected += 1
            if source_id == self._reading:
                self._reading_rows += 1
            source = self._sources[source_id]
            marks = self.glyphs.rejected * source.trials
            self._lines.append(
                f"  {marks}  {'':>6}  {self._identity(source_id, row_index, None)}"
                f"{self.glyphs.separator}rejected ({error.type})"
            )

    def trial_started(self, task: str, source_id: str, row_index: int, trial: int) -> None:
        with self._lock:
            self._running[(task, source_id, row_index, trial)] = self._now()

    def trial_finished(
        self,
        task: str,
        source_id: str,
        row_index: int,
        trial: int,
        observation: TrialObservation,
        errors: tuple[TrialError, ...],
    ) -> None:
        with self._lock:
            self._running.pop((task, source_id, row_index, trial), None)
            self._finished += 1
            if observation.status in ("task_failed", "scoring_failed"):
                self._failed += 1
            if observation.total_ms is not None and observation.status != "cancelled":
                self._latency.add(observation.total_ms)
            case = self._cases.setdefault((source_id, row_index), _Case(None))
            case.trials[(task, trial)] = observation
            if errors:
                case.errors[(task, trial)] = errors[0]
            if len(case.trials) == self._sources[source_id].trials:
                del self._cases[(source_id, row_index)]
                self._emit(source_id, row_index, case)

    def source_finished(self, source_id: str, summary: SourceSummary) -> None:
        with self._lock:
            self._sources_done += 1
            if self._reading == source_id:
                self._reading = None

    def cancelling(self) -> None:
        with self._lock:
            self._cancelling = True

    def finishing(self, sinks: tuple[str, ...]) -> None:
        with self._lock:
            self._finishing = sinks

    # Application output ----------------------------------------------------------

    def output_lines(self, origin: Origin | None, text: str) -> list[str]:
        """Prefix captured output with where it came from; it can never move the cursor."""
        with self._lock:
            prefix = self._origin(origin)
        lines: list[str] = []
        for line in text.removesuffix("\n").split("\n"):
            # Keep what a terminal would show after a carriage return (progress bars).
            shown = printable(line.rsplit("\r", 1)[-1].expandtabs())
            lines.append(f"  {prefix} {self.glyphs.rule} {shown}")
        return lines

    # Rendering -------------------------------------------------------------------

    def drain(self) -> list[str]:
        with self._lock:
            lines, self._lines = self._lines, []
            return lines

    def close(self) -> list[str]:
        """Emit cases that will never complete (e.g. admission stopped) and drain."""
        with self._lock:
            for (source_id, row_index), case in sorted(self._cases.items()):
                self._emit(source_id, row_index, case, force=True)
            self._cases.clear()
            lines, self._lines = self._lines, []
            return lines

    @property
    def phase(self) -> str:
        with self._lock:
            return (
                "cancelling"
                if self._cancelling
                else "finishing"
                if self._finishing is not None
                else "running"
            )

    def status(self) -> str:
        """The live status text, without spinner or elapsed time."""
        with self._lock:
            return self._status(self._now())

    def footer(self, width: int) -> str:
        with self._lock:
            now = self._now()
            elapsed = now - self._started
            spinner = self.glyphs.spinner[int(elapsed * 10) % len(self.glyphs.spinner)]
            line = f"{spinner} {self._status(now)}{self.glyphs.separator}{clock(elapsed)}"
            return fit(line, width, self.glyphs.ellipsis)

    # Internals (lock held) -------------------------------------------------------

    def _header(self, run: RunPlan) -> str:
        names = [task.name for task in run.tasks]
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        parts = [f"mic run {run.run_id[:8]}", printable(shown)]
        trials = {task.trials for task in run.tasks}
        if len(trials) == 1:
            count = trials.pop()
            parts.append(f"{count} trial{'' if count == 1 else 's'}/case")
        parts.append(f"concurrency {run.concurrency}")
        timeouts = {task.timeout for task in run.tasks}
        if len(timeouts) == 1 and (timeout := timeouts.pop()) is not None:
            parts.append(f"timeout {timeout:g}s")
        if run.limits.row_count is not None:
            parts.append(f"limit {run.limits.row_count}")
        return self.glyphs.separator.join(parts)

    def _status(self, now: float) -> str:
        sep, ellipsis = self.glyphs.separator, self.glyphs.ellipsis
        if self._cancelling:
            waiting = len(self._running)
            if not waiting:
                return f"cancelling{ellipsis}"
            rows = sorted({row for _, _, row, _ in self._running})
            shown = ", ".join(str(row) for row in rows[:5]) + (
                f", {ellipsis}" if len(rows) > 5 else ""
            )
            trials = "trial" if waiting == 1 else "trials"
            label = "row" if len(rows) == 1 else "rows"
            return (
                f"cancelling{sep}waiting for {waiting} running {trials} to return ({label} {shown})"
            )
        if self._finishing is not None:
            names = ", ".join(printable(name) for name in self._finishing)
            return f"finishing sinks ({names}){ellipsis}" if names else f"finishing{ellipsis}"
        if self._plan is None:
            return f"starting{ellipsis}"
        parts: list[str] = []
        if self._reading is not None and self._reading_rows == 0:
            parts.append(f"reading {printable(self._sources[self._reading].name)}{ellipsis}")
        if self._seen or self._finished:
            limit = self._plan.limits.row_count
            if limit is not None:
                parts.append(f"{self._seen}/{limit} records")
            else:
                done = self._sources_done == len(self._sources)
                plural = "" if self._accepted == 1 else "s"
                parts.append(f"{self._accepted} case{plural}{' read' if done else ''}")
            if self._rejected:
                parts.append(f"{self._rejected} rejected")
            parts.append(f"{self._finished} trials done")
            parts.append(f"{len(self._running)}/{self._plan.concurrency} running")
            parts.append(f"{self._failed} failed")
            elapsed = now - self._started
            if self._finished and elapsed >= 1:
                parts.append(f"{self._finished / elapsed:.1f}/s")
            slow = self._slowest(now)
            if slow is not None:
                parts.append(slow)
        return sep.join(parts) or f"starting{ellipsis}"

    def _slowest(self, now: float) -> str | None:
        if not self._running:
            return None
        (task, source_id, row, _), started = min(self._running.items(), key=lambda item: item[1])
        age = now - started
        typical = self._latency.mean / 1000 if self._latency.count else 0.0
        if age < _SLOW_SECONDS or age < typical * _SLOW_FACTOR:
            return None
        source = self._sources[source_id]
        who = f"{printable(task)} " if len(source.tasks) > 1 else ""
        return f"{who}{self._identity(source_id, row, None)} running {clock(age)}"

    def _identity(self, source_id: str, row: int, label: str | None) -> str:
        prefix = f"{printable(self._sources[source_id].name)} " if len(self._sources) > 1 else ""
        text = f"{prefix}row {row}"
        if label is not None:
            text += f" ({fit(printable(label), _LABEL_WIDTH, self.glyphs.ellipsis)})"
        return text

    def _origin(self, origin: Origin | None) -> str:
        source = None if origin is None else self._sources.get(origin.source_id)
        if origin is None or source is None:
            return "mic"
        parts = [self._identity(origin.source_id, origin.row_index, None)]
        if len(source.tasks) > 1:
            parts.insert(0, printable(origin.evaluation))
        if dict(source.tasks).get(origin.evaluation, 1) > 1:
            parts.append(f"trial {origin.trial}")
        if origin.scorer is not None:
            parts.append(printable(origin.scorer))
        return self.glyphs.separator.join(parts)

    def _paint(self, text: str, code: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self._color else text

    def _mark(self, observation: TrialObservation | None) -> str:
        glyphs = self.glyphs
        if observation is None:
            return glyphs.cancelled
        match observation.status:
            case "completed":
                return glyphs.completed
            case "task_failed":
                return self._paint(glyphs.task_failed, "31")
            case "scoring_failed":
                return self._paint(glyphs.scoring_failed, "33")
            case "cancelled":
                return glyphs.cancelled

    def _emit(self, source_id: str, row: int, case: _Case, *, force: bool = False) -> None:
        failed = any(o.status in ("task_failed", "scoring_failed") for o in case.trials.values())
        if not (self._every_case or failed or force):
            return
        source = self._sources[source_id]
        multi = len(source.tasks) > 1
        groups: list[str] = []
        details: list[str] = []
        for task, trials in source.tasks:
            marks = "".join(self._mark(case.trials.get((task, t))) for t in range(1, trials + 1))
            groups.append(f"{printable(task)} {marks}" if multi else marks)
            if self._scores:
                details.append(self._task_scores(task, source.scorers[task], case, multi))
            details.extend(self._task_problems(task, trials, case, multi))
        totals = [o.total_ms for o in case.trials.values() if o.total_ms is not None]
        took = duration(max(totals) / 1000) if totals else self.glyphs.dash
        identity = self._identity(source_id, row, case.label)
        if force and len(case.trials) < source.trials:
            details.append("not run")
        tail = "".join(self.glyphs.separator + detail for detail in details if detail)
        self._lines.append(f"  {' '.join(groups)}  {took:>6}  {identity}{tail}")

    def _task_scores(self, task: str, scorers: tuple[str, ...], case: _Case, multi: bool) -> str:
        values: dict[str, list[float]] = {name: [] for name in scorers}
        for (name, _), observation in case.trials.items():
            if name == task:
                for metric, value in observation.scores.items():
                    if value is not None and metric in values:
                        values[metric].append(value)
        shown = " ".join(
            f"{printable(metric)}="
            + (_number(sum(found) / len(found)) if found else self.glyphs.dash)
            for metric, found in values.items()
        )
        return f"{printable(task)} {shown}" if multi and shown else shown

    def _task_problems(self, task: str, trials: int, case: _Case, multi: bool) -> list[str]:
        """Group failed and cancelled trials, e.g. ``trials 1,3 task_failed (TimeoutError)``."""
        grouped: dict[tuple[str, str | None, str | None], list[int]] = {}
        for trial in range(1, trials + 1):
            observation = case.trials.get((task, trial))
            if observation is None or observation.status == "completed":
                continue
            error = case.errors.get((task, trial))
            key = (
                (observation.status, None, None)
                if observation.status == "cancelled"
                else (
                    observation.status,
                    error.type if error else "unknown error",
                    error.scorer if error else None,
                )
            )
            grouped.setdefault(key, []).append(trial)
        prefix = f"{printable(task)} " if multi else ""
        return [
            f"{prefix}trial{'' if len(found) == 1 else 's'} {','.join(map(str, found))} {status}"
            + (f" in {printable(scorer)}" if scorer else "")
            + (f" ({printable(kind)})" if kind else "")
            for (status, kind, scorer), found in grouped.items()
        ]


class _Display:
    """Renders on a background thread so status stays current even if the loop stalls."""

    interval: float

    def __init__(self, state: ProgressState, stream: TextIO) -> None:
        self.state = state
        self.stream = stream
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._broken = False

    def start(self) -> None:
        self._render()
        self._thread = threading.Thread(target=self._loop, name="mic-progress", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._render(final=True)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._render()

    def echo(self, origin: Origin | None, stream: str, text: str) -> None:
        """Show captured application output above the progress, on the progress stream."""
        lines = self.state.output_lines(origin, text)
        with self._lock:
            if self._broken:
                return
            try:
                self.clear()
                # Progress lines already due (the header, finished cases) keep their order.
                lines = self.state.drain() + lines
                self.stream.write("".join(line + "\n" for line in lines))
                self.stream.flush()
            except Exception:
                self._broken = True

    def clear(self) -> None:
        """Remove anything that must not share a line with other output."""

    def _render(self, *, final: bool = False) -> None:
        if self._broken:
            return
        try:
            with self._lock:
                self.render(final=final)
        except Exception:
            # A closed or broken stderr must not take the run down with it.
            self._broken = True

    def render(self, *, final: bool) -> None:
        raise NotImplementedError


class LiveDisplay(_Display):
    """Case lines scroll above a single status line that is redrawn in place."""

    interval = 0.1

    def __init__(self, state: ProgressState, stream: TextIO) -> None:
        super().__init__(state, stream)
        self._drawn = False
        self._proxies: list[tuple[str, _Interleaved]] = []

    def start(self) -> None:
        # Application prints would otherwise land in the middle of the status line.
        for name in ("stdout", "stderr"):
            current: TextIO = getattr(sys, name)
            if current is self.stream or _isatty(current):
                proxy = _Interleaved(self, current)
                self._proxies.append((name, proxy))
                setattr(sys, name, proxy)
        super().start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        for name, proxy in self._proxies:
            if getattr(sys, name) is proxy:
                setattr(sys, name, proxy.target)
            proxy.release()
        self._render(final=True)

    def clear(self) -> None:
        self.erase()

    def erase(self) -> None:
        """Remove the status line before other output; the next tick redraws it."""
        if self._drawn:
            self.stream.write(_ERASE_LINE)
            self.stream.flush()
            self._drawn = False

    def render(self, *, final: bool) -> None:
        lines = self.state.close() if final else self.state.drain()
        footer = None if final else self.state.footer(_columns(self.stream) - 1)
        if not lines and footer is None and not self._drawn:
            return
        parts = [_ERASE_LINE] if self._drawn else []
        parts.extend(line + "\n" for line in lines)
        if footer is not None:
            parts.append(footer)
        self.stream.write("".join(parts))
        self.stream.flush()
        self._drawn = footer is not None


class PlainDisplay(_Display):
    """Non-interactive output: finished lines as they happen plus a periodic status line."""

    interval = 0.5

    def __init__(self, state: ProgressState, stream: TextIO, *, heartbeat: float = 30.0) -> None:
        super().__init__(state, stream)
        self._heartbeat = heartbeat
        self._last = time.monotonic()
        self._phase: str = "running"

    def render(self, *, final: bool) -> None:
        lines = self.state.close() if final else self.state.drain()
        now = time.monotonic()
        phase = self.state.phase
        if not final and (now - self._last >= self._heartbeat or phase != self._phase):
            lines.append(f"mic: {self.state.status()}")
            self._last, self._phase = now, phase
        if lines:
            self.stream.write("".join(line + "\n" for line in lines))
            self.stream.flush()


class _Interleaved:
    """A stdout/stderr stand-in that keeps application output off the status line."""

    def __init__(self, display: LiveDisplay, target: TextIO) -> None:
        self.display = display
        self.target = target
        self._partial = ""

    def write(self, text: str) -> int:
        with self.display._lock:  # pyright: ignore[reportPrivateUsage]
            self._partial += text
            if "\n" in self._partial or len(self._partial) > _PARTIAL_LIMIT:
                head, newline, rest = self._partial.rpartition("\n")
                complete, self._partial = (head + newline, rest) if newline else (self._partial, "")
                self.display.erase()
                self.target.write(complete)
                self.target.flush()
        return len(text)

    def flush(self) -> None:
        with self.display._lock:  # pyright: ignore[reportPrivateUsage]
            self.target.flush()

    def release(self) -> None:
        """Write any unterminated output on its own line before the display finishes."""
        with self.display._lock:  # pyright: ignore[reportPrivateUsage]
            if self._partial:
                self.display.erase()
                self.target.write(self._partial + "\n")
                self.target.flush()
                self._partial = ""

    def isatty(self) -> bool:
        return _isatty(self.target)

    def __getattr__(self, name: str) -> object:
        return getattr(self.target, name)


def _isatty(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except Exception:
        return False


def _columns(stream: TextIO) -> int:
    try:
        return int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        pass
    try:
        return os.get_terminal_size(stream.fileno()).columns
    except (AttributeError, OSError, ValueError):
        return 80


def _enable_ansi(stream: TextIO) -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt

        kernel32 = ctypes.windll.kernel32
        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def interactive(stream: TextIO) -> bool:
    return _isatty(stream) and os.environ.get("TERM") != "dumb" and _enable_ansi(stream)


@contextmanager
def progress(
    stream: TextIO, *, quiet: bool, verbose: bool, show_output: bool = False
) -> Generator[tuple[RunObserver | None, Echo | None]]:
    """Show progress on ``stream`` for the duration of a run; always restores the terminal.

    Yields the observer and, with ``show_output``, an echo for captured application output.
    """
    if quiet:
        yield None, None
        return
    live = interactive(stream)
    state = ProgressState(
        glyphs=glyphs_for(stream),
        color=live and not os.environ.get("NO_COLOR"),
        every_case=live or verbose,
        scores=verbose,
    )
    display: _Display = LiveDisplay(state, stream) if live else PlainDisplay(state, stream)
    display.start()
    try:
        yield state, display.echo if show_output else None
    finally:
        display.close()
