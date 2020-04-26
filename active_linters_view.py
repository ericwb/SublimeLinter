from collections import defaultdict
from functools import partial
import threading

import sublime
import sublime_plugin

from .lint import events, persist, util


MYPY = False
if MYPY:
    from typing import DefaultDict, Dict, Hashable, Iterator, List, Set
    from mypy_extensions import TypedDict

    FileName = str
    LinterName = str
    State_ = TypedDict('State_', {
        'assigned_linters_per_file': DefaultDict[FileName, Set[LinterName]],
        'failed_linters_per_file': DefaultDict[FileName, Set[LinterName]],
        'problems_per_file': DefaultDict[FileName, Dict[LinterName, str]],
        'linters_per_file_memo': DefaultDict[FileName, Set[LinterName]],
        'running': DefaultDict[sublime.BufferId, int],
        'expanded_ok': Set[sublime.BufferId],
    })


STATUS_ACTIVE_KEY = 'sublime_linter_status_active'

State = {
    'assigned_linters_per_file': defaultdict(set),
    'failed_linters_per_file': defaultdict(set),
    'problems_per_file': defaultdict(dict),
    'linters_per_file_memo': defaultdict(set),
    'running': defaultdict(int),
    'expanded_ok': set(),
}  # type: State_


def plugin_unloaded():
    events.off(redraw_file)
    events.off(on_begin_linting)
    events.off(on_finished_linting)

    for window in sublime.windows():
        for view in window.views():
            view.erase_status(STATUS_ACTIVE_KEY)


@events.on(events.LINT_START)
def on_begin_linting(buffer_id):
    # type: (sublime.BufferId) -> None
    State['running'][buffer_id] += 1


@events.on(events.LINT_END)
def on_finished_linting(buffer_id, **kwargs):
    if State['running'][buffer_id] <= 1:
        State['running'].pop(buffer_id)
    else:
        State['running'][buffer_id] -= 1

    if buffer_id in State['expanded_ok']:
        # Prolong "expanded" state
        show_expanded_ok(buffer_id)


def on_first_activate(view):
    # type: (sublime.View) -> None
    if not util.is_lintable(view):
        return

    show_expanded_ok(view.buffer_id())
    filename = util.get_filename(view)
    draw(view, State['problems_per_file'][filename])


def on_assigned_linters_changed(filename, bid):
    # type: (FileName, sublime.BufferId) -> None
    show_expanded_ok(bid)
    redraw_file_(filename, State['problems_per_file'][filename])


def show_expanded_ok(bid):
    # type: (sublime.BufferId) -> None
    State['expanded_ok'].add(bid)
    sublime.set_timeout(throttled_on_args(_unset_expanded_ok, bid), 3000)


def _unset_expanded_ok(bid):
    # type: (sublime.BufferId) -> None
    # Keep expanded if linters are running to minimize redraws
    if State['running'].get(bid, 0) > 0:
        return

    State['expanded_ok'].discard(bid)
    redraw_buffer_(bid)


class sublime_linter_assigned(sublime_plugin.WindowCommand):
    def run(self, filename, buffer_id, linter_names):
        # type: (FileName, sublime.BufferId, List[LinterName]) -> None
        State['assigned_linters_per_file'][filename] = set(linter_names)
        State['failed_linters_per_file'][filename] = set()

        if not distinct_pair(filename, linter_names):
            on_assigned_linters_changed(filename, buffer_id)


class sublime_linter_unassigned(sublime_plugin.WindowCommand):
    def run(self, filename, linter_name):
        State['assigned_linters_per_file'][filename].discard(linter_name)
        State['failed_linters_per_file'][filename].discard(linter_name)


class sublime_linter_failed(sublime_plugin.WindowCommand):
    def run(self, filename, linter_name):
        State['failed_linters_per_file'][filename].add(linter_name)


@events.on(events.LINT_RESULT)
def redraw_file(filename, linter_name, errors, **kwargs):
    # type: (FileName, LinterName, List[persist.LintError], object) -> None
    problems = State['problems_per_file'][filename]
    if linter_name in State['failed_linters_per_file'][filename]:
        problems[linter_name] = '?'
    elif linter_name in State['assigned_linters_per_file'][filename] or errors:
        if linter_name not in State['assigned_linters_per_file'][filename]:
            State['assigned_linters_per_file'][filename].add(linter_name)

        counts = count_problems(errors)
        if sum(counts.values()) == 0:
            problems[linter_name] = ''
        else:
            sorted_keys = (
                tuple(sorted(counts.keys() - {'w', 'e'}))
                + ('w', 'e')
            )
            parts = ' '.join(
                "{}:{}".format(error_type, counts[error_type])
                for error_type in sorted_keys
                if error_type in counts and counts[error_type] > 0
            )
            problems[linter_name] = '({})'.format(parts)
    else:
        problems.pop(linter_name, None)

    sublime.set_timeout(lambda: redraw_file_(filename, problems))


def count_problems(errors):
    # type: (List[persist.LintError]) -> Dict[str, int]
    counters = defaultdict(int)  # type: DefaultDict[str, int]
    for error in errors:
        error_type = error['error_type']
        counters[error_type[0]] += 1

    return counters


def redraw_file_(filename, problems):
    for view in views_into_file(filename):
        draw(view, problems)


def redraw_buffer_(buffer_id):
    for view in views_with_buffer_id(buffer_id):
        filename = util.get_filename(view)
        draw(view, State['problems_per_file'][filename])


def views_into_file(filename):
    # type: (FileName) -> Iterator[sublime.View]
    return (view for view in all_views() if util.get_filename(view) == filename)


def views_with_buffer_id(bid):
    # type: (sublime.BufferId) -> Iterator[sublime.View]
    return (view for view in all_views() if view.buffer_id() == bid)


def all_views():
    # type: () -> Iterator[sublime.View]
    return (
        view
        for window in sublime.windows()
        for view in window.views()
    )


def draw(view, problems):
    if persist.settings.get('statusbar.show_active_linters'):
        bid = view.buffer_id()
        previous = State['linters_per_file_memo'][bid]
        current = State['linters_per_file_memo'][bid] = set(problems.keys())
        if current != previous:
            show_expanded_ok(bid)

        if (
            problems.keys()
            and bid not in State['expanded_ok']
            and all(part == '' for part in problems.values())
        ):
            message = 'ok'
            view.set_status(STATUS_ACTIVE_KEY, message)
            return

        message = ' '.join(
            '{}{}'.format(linter_name, summary)
            for linter_name, summary in sorted(problems.items(), key=by_severity)
        )
        view.set_status(STATUS_ACTIVE_KEY, message)
    else:
        view.erase_status(STATUS_ACTIVE_KEY)


def by_severity(item):
    linter_name, summary = item
    if summary == '':
        return (0, linter_name)
    elif summary[0] == '?':
        return (2, linter_name)
    return (1, linter_name)


THROTTLER_TOKENS = {}
THROTTLER_LOCK = threading.Lock()


def throttled_on_args(fn, *args, **kwargs):
    key = (fn,) + args
    action = partial(fn, *args, **kwargs)
    with THROTTLER_LOCK:
        THROTTLER_TOKENS[key] = action

    def program():
        with THROTTLER_LOCK:
            ok = THROTTLER_TOKENS[key] == action
        if ok:
            action()

    return program


ACTIVATED_VIEWS = set()


class OnFirstActivate(sublime_plugin.EventListener):
    def on_activated(self, view):
        # type: (sublime.View) -> None
        vid = view.id()
        if vid in ACTIVATED_VIEWS:
            return

        ACTIVATED_VIEWS.add(vid)
        on_first_activate(view)

    def on_close(self, view):
        # type: (sublime.View) -> None
        ACTIVATED_VIEWS.discard(view.id())


PAIRS = {}  # type: Dict[Hashable, object]


def distinct_pair(key, val):
    # type: (Hashable, object) -> bool
    previous = PAIRS.get(key)
    current = PAIRS[key] = val
    return current == previous
