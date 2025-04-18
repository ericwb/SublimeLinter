from __future__ import annotations
import sublime
import sublime_plugin

from itertools import dropwhile, takewhile

from .lint import persist, util
from .lint.util import flash

from typing_extensions import Literal


Direction = Literal['next', 'previous']


class sublime_linter_goto_error(sublime_plugin.TextCommand):
    def run(
        self,
        edit: sublime.Edit,
        direction: Direction = 'next',
        count: int = 1,
        wrap: bool = False
    ) -> None:
        goto(self.view, direction, count, wrap)


def goto(view: sublime.View, direction: Direction, count: int, wrap: bool) -> None:
    filename = util.canonical_filename(view)
    errors = persist.file_errors.get(filename)
    if not errors:
        flash(view, 'No problems')
        return

    cursor = view.sel()[0].begin()

    # Filter regions under the cursor, bc we don't want to jump to them.
    # Also filter duplicate start positions.
    all_jump_positions = sorted({
        error['region'].begin()
        for error in errors
        if not error['region'].contains(cursor)})

    # Edge case: Since we filtered, it is possible we get here with nothing
    # left. That is the case if we sit on the last remaining error, where we
    # don't have anything to jump to and even `wrap` becomes a no-op.
    if len(all_jump_positions) == 0:
        flash(view, 'No more problems')
        return

    def before_current_pos(pos):
        return pos < cursor

    next_positions = dropwhile(before_current_pos, all_jump_positions)
    previous_positions = takewhile(before_current_pos, all_jump_positions)

    reverse = direction == 'previous'
    jump_positions = list(previous_positions if reverse else next_positions)
    if reverse:
        jump_positions = list(reversed(jump_positions))

    if not jump_positions:
        if wrap:
            point = all_jump_positions[-1] if reverse else all_jump_positions[0]
            flash(
                view,
                'Jumped to {} problem'.format('last' if reverse else 'first'))
        else:
            flash(
                view,
                'No more problems {}'.format('above' if reverse else 'below'))
            return
    elif len(jump_positions) <= count:
        # If we cannot jump wide enough, do not wrap, but jump as wide as
        # possible to reduce disorientation.
        point = jump_positions[-1]
    else:
        point = jump_positions[count - 1]

    move_to(view, point)


class sublime_linter_move_cursor(sublime_plugin.TextCommand):
    # We ensure `on_selection_modified` handlers run by using a `TextCommand`.
    # See: https://github.com/SublimeLinter/SublimeLinter/pull/867
    # and https://github.com/SublimeTextIssues/Core/issues/485#issuecomment-337480388
    def run(self, edit, point):
        self.view.sel().clear()
        self.view.sel().add(point)
        self.view.show(point)


def move_to(view: sublime.View, point: int) -> None:
    add_selection_to_jump_history(view)
    view.run_command('sublime_linter_move_cursor', {'point': point})


def add_selection_to_jump_history(view):
    view.run_command("add_jump_record", {
        "selection": [(r.a, r.b) for r in view.sel()]
    })
