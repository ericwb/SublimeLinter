from __future__ import annotations
import sublime

from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_EXCEPTION
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import chain, count
from functools import lru_cache, partial
import hashlib
import logging
import multiprocessing
import os
import time
import threading
import traceback

from . import events, linter as linter_module, persist, style, util

from typing import Callable, Iterator, TypeVar
from typing_extensions import TypeAlias
from .persist import LintError
from .elect import LinterInfo
from typing_extensions import ParamSpec
Linter = linter_module.Linter
LinterSettings = linter_module.LinterSettings

T = TypeVar('T')
P = ParamSpec('P')
LintResult: TypeAlias[list] = "list[LintError]"
Task = Callable[[], T]
ViewChangedFn = Callable[[], bool]
FileName = str
LinterName = str
ViewContext = linter_module.ViewContext


@dataclass(frozen=True)
class LintJob:
    linter_name: LinterName
    ctx: ViewContext
    tasks: list[Task[LintResult]]


logger = logging.getLogger(__name__)

MAX_CONCURRENT_TASKS = multiprocessing.cpu_count() or 1
orchestrator = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)


task_count = count(start=1)
counter_lock = threading.Lock()


def lint_view(
    linters: list[LinterInfo],
    view: sublime.View,
    view_has_changed: ViewChangedFn,
    sink: Callable[[LinterName, LintResult], None]
) -> None:
    """Lint the given view.

    This is the top level lint dispatcher. It falls through.
    """
    lint_jobs = [
        LintJob(linter.name, linter.context, tasks)
        for linter in linters
        if (tasks := list(tasks_per_linter(view, view_has_changed, linter)))
    ]
    warn_excessive_tasks(lint_jobs)

    for job in lint_jobs:
        # Explicitly catch all unhandled errors because we fire-and-forget!
        orchestrator.submit(print_all_exceptions(run_job), job, sink)


def tasks_per_linter(
    view: sublime.View,
    view_has_changed: ViewChangedFn,
    linter_info: LinterInfo
) -> Iterator[Task[LintResult]]:
    for region in linter_info.regions:
        linter = linter_info.klass(view, linter_info.settings)
        code = view.substr(region)
        offsets = view.rowcol(region.begin()) + (region.begin(),)

        task = partial(execute_lint_task, linter, code, offsets, view_has_changed)
        executor = partial(modify_thread_name, linter_info, task)
        yield executor


def modify_thread_name(linter_info: LinterInfo, sink: Callable[[], T]) -> T:
    original_name = threading.current_thread().name
    # We 'name' our threads, for logging purposes.
    threading.current_thread().name = make_good_task_name(linter_info)
    try:
        return sink()
    finally:
        threading.current_thread().name = original_name


def make_good_task_name(linter: LinterInfo) -> str:
    with counter_lock:
        task_number = next(task_count)

    return 'LintTask|{}|{}|{}|{}'.format(
        task_number,
        linter.name,
        linter.context["short_canonical_filename"],
        linter.context["view_id"]
    )


def execute_lint_task(
    linter: Linter,
    code: str,
    offsets: tuple,
    view_has_changed: ViewChangedFn
) -> LintResult:
    try:
        errors = linter.lint(code, view_has_changed)
        finalize_errors(linter, errors, offsets)
        return errors
    except linter_module.TransientError:
        # For `TransientError`s we want to omit calling the `sink` at all.
        # Raise to abort in `run_job`.
        raise
    except linter_module.PermanentError:
        return []  # Empty list here to clear old errors
    except Exception:
        linter.notify_failure()
        # Log while multi-threaded to get a nicer log message
        logger.exception('Unhandled exception:\n', extra={'demote': True})
        return []  # Empty list here to clear old errors


def finalize_errors(
    linter: Linter,
    errors: list[LintError],
    offsets: tuple[int, ...]
) -> None:
    linter_name = linter.name
    view = linter.view
    eof = view.size()
    view_filename = util.canonical_filename(view)
    line_offset, col_offset, pt_offset = offsets

    for error in errors:
        belongs_to_main_file = (
            os.path.normcase(error['filename']) == os.path.normcase(view_filename)
        )

        region, line, start = error['region'], error['line'], error['start']
        offending_text = error['offending_text']
        if belongs_to_main_file:  # offsets are for the main file only
            if line == 0:
                start += col_offset
            line += line_offset
            region = sublime.Region(region.a + pt_offset, region.b + pt_offset)
            # If only parts of a file are linted, the virtual view inside
            # the linter can "think" it has an error on eof when it is
            # actually on the end of the linted *part* of the file only.
            # Check here, and maybe undo.
            if region.empty() and region.a != eof:
                region.b += 1
                offending_text = view.substr(region)

        error.update({
            'linter': linter_name,
            'line': line,
            'start': start,
            'region': region,
            'offending_text': offending_text,
        })

        error.update({
            'uid': make_error_uid(error),
            'priority': style.get_value('priority', error, 0),
        })


PROPERTIES_FOR_UID = (
    'filename', 'linter', 'line', 'start', 'error_type', 'code', 'msg',
)


def make_error_uid(error: LintError) -> str:
    return hashlib.sha256(
        ''.join(
            str(error[k])  # type: ignore
            for k in PROPERTIES_FOR_UID
        )
        .encode('utf-8')
    ).hexdigest()


def warn_excessive_tasks(jobs: list[LintJob]) -> None:
    total_tasks = sum(len(job.tasks) for job in jobs)
    if total_tasks > 4:
        details = ", ".join(
            "{}x {}".format(len(job.tasks), job.linter_name)
            for job in jobs
        )
        excess_warning(
            "'{}' puts in total {}(!) tasks on the queue:  {}."
            .format(jobs[0].ctx["short_canonical_filename"], total_tasks, details)
        )
    else:
        for job in jobs:
            if len(job.tasks) > 3:
                excess_warning(
                    "'{}' puts {} {} tasks on the queue."
                    .format(job.ctx["short_canonical_filename"], len(job.tasks), job.linter_name)
                )


@lru_cache(4)
def excess_warning(msg: str) -> None:
    logger.warning(msg)


def print_all_exceptions(fn: Callable[P, T]) -> Callable[P, T]:
    def inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
    return inner


def run_job(job: LintJob, sink: Callable[[LinterName, LintResult], None]) -> None:
    with broadcast_lint_runtime(job), remember_runtime(job):
        try:
            results = run_concurrently(job.tasks, executor=executor)
        except linter_module.TransientError:
            return  # ABORT
        except Exception:
            traceback.print_exc()
            return  # ABORT

    errors = list(chain.from_iterable(results))  # flatten and consume

    # We don't want to guarantee that our consumers/views are thread aware.
    # So we merge here into Sublime's shared worker thread. Sublime guarantees
    # here to execute all scheduled tasks ordered and sequentially.
    sublime.set_timeout_async(lambda: sink(job.linter_name, errors))


def run_concurrently(tasks: list[Task[T]], executor: ThreadPoolExecutor) -> list[T]:
    work = [executor.submit(task) for task in tasks]
    done, not_done = wait(work, return_when=FIRST_EXCEPTION)

    for future in not_done:
        future.cancel()

    return [future.result() for future in done]


global_lock = threading.RLock()
elapsed_runtimes = deque([0.6] * 3, maxlen=10)
MIN_DEBOUNCE_DELAY = 0.0005
MAX_AUTOMATIC_DELAY = 2.0


def get_delay() -> float:
    """Return the delay between a lint request and when it will be processed."""
    runtimes = sorted(elapsed_runtimes)
    middle = runtimes[len(runtimes) // 2]
    return max(
        max(MIN_DEBOUNCE_DELAY, float(persist.settings.get('delay'))),
        min(MAX_AUTOMATIC_DELAY, middle / 2)
    )


@contextmanager
def remember_runtime(job: LintJob) -> Iterator[None]:
    start_time = time.perf_counter()
    yield
    end_time = time.perf_counter()
    runtime = end_time - start_time
    with global_lock:
        elapsed_runtimes.append(runtime)

    logger.info(
        "Linting '{}' with {} took {:.2f}s"
        .format(job.ctx["short_canonical_filename"], job.linter_name, runtime)
    )


@contextmanager
def broadcast_lint_runtime(job: LintJob) -> Iterator[None]:
    payload = {'filename': job.ctx["canonical_filename"], 'linter_name': job.linter_name}
    events.broadcast(events.LINT_START, payload)
    try:
        yield
    finally:
        events.broadcast(events.LINT_END, payload)
