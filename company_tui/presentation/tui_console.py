import asyncio
from collections.abc import Awaitable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TypeVar

from textual import events, work
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import RichLog

from company_tui.application.app import Application
from company_tui.application.updates import UpdateWatch
from company_tui.domain import naming
from company_tui.domain.capability import Capability
from company_tui.domain.deployment import (
    DeploymentEnvironment,
    DeploymentGroup,
    DeploymentOperation,
    DeploymentProject,
)
from company_tui.domain.options import Option, OptionValue
from company_tui.domain.recent import RecentPathsPort
from company_tui.domain.script_config import (
    ScriptAction,
    ScriptCatalogue,
    ScriptSection,
    ScriptUpdate,
)
from company_tui.domain.session_memory import SessionMemory
from company_tui.domain.template_pack import (
    ScaffoldTarget,
    ScaffoldTargetOption,
    TemplatePack,
)
from company_tui.domain.updates import NOTHING_TO_REPORT, UpdateReport
from company_tui.infrastructure.terminal_window import restore_terminal_interaction
from company_tui.infrastructure.window_shape import (
    DEFAULT_PLAN,
    AppWindow,
    SizeGuard,
    WindowPlan,
    cramped,
    current_window,
    window_plan,
)
from company_tui.presentation.branding import (
    APP_NAME,
    APP_TAGLINE,
    APP_THEME,
    APP_VERSION,
    PEAK_ART,
)
from company_tui.presentation.card import MenuEntry
from company_tui.presentation.chrome import AppFooter, AppFrame, AppHeader, BusyLine
from company_tui.presentation.path_screen import PathScreen
from company_tui.presentation.run_screen import RunScreen
from company_tui.presentation.screens import (
    TRAIL_SEPARATOR,
    CardMenuScreen,
    ConfirmScreen,
    ContinueScreen,
    InputScreen,
    InstallingScreen,
    RunsScreen,
    SplashScreen,
)
from company_tui.presentation.session import (
    CURRENT_SESSION,
    RunSession,
    SessionRegistry,
    Workflow,
    current_session,
)
from company_tui.presentation.ui import RefreshRunner

TRAIL_STEPS = (
    "capability",
    "scaffold_target",
    "template_pack",
    "script_section",
    "script_action",
    "deployment_project",
    "deployment_environment",
    "deployment_group",
    "deployment_operation",
)
"""Every menu a workflow can walk, in the order it walks them.

ORDER IS LOAD-BEARING: `_enter_step` forgets a step and everything AFTER it in
this tuple, so a step listed too late leaves its own successors standing when the
user goes back. And a step MISSING from here is worse than misplaced -
`TRAIL_STEPS.index` raises, the exception is caught as a failed run, and the
workflow dies into the session log while the menu loop calmly puts the top menu
back. Which is what "script_section" did: Scripts opened, vanished, and left a
card menu that looked like nothing had been asked for."""

SCRIPT_UPDATE_ANSWERS: tuple[tuple[str, str, str, str], ...] = (
    (
        ScriptUpdate.RECLONE.value,
        "Re-clone",
        "Replace the copy in the store",
        "Deletes the store copy and clones it again — anything edited there goes with it.",
    ),
    (
        ScriptUpdate.KEEP.value,
        "Keep this copy",
        "Carry on with what is installed",
        "Runs the version in the store. Asked again next time you come through here.",
    ),
    (
        ScriptUpdate.SILENCE.value,
        "Stop asking",
        "Keep it, and never check again",
        "Written to your settings, where it can be turned back on. Skips the check entirely.",
    ),
)
"""Key, name, card line and focus line for each way out of a stale store.

A menu because there are three, and because two of them are only safe to take
once you know what the third costs: re-cloning is the one step here that throws
away work, and it says so on the card rather than after the fact."""

RAW_OUTPUT_PREFIX = "  "
"""Marks a line as verbatim output from a subprocess rather than the toolbox
talking. Reads as indentation in the plain console, and as dimmed process output
in the run panel."""

QUIT_CONFIRM = f"Quit {APP_NAME}?"
QUIT_DETAIL = (
    "Are you sure you want to close the toolbox?\nNothing is running right now."
)
QUIT_WITH_RUNS = (
    "Are you sure you want to close the toolbox?\n"
    "{count} still going — every one of them will be stopped."
)
"""What quitting costs, spelled out rather than left to the title.

Two versions because they are two different prices: with nothing going, quitting
loses a menu position, and with N runs going it is N directories abandoned
half-written. The title asks the same question either way.
"""

QUIT_ANSWER = f"QUIT {APP_NAME}"

CLOSE_TAB_CONFIRM = "Close this tab?"
CLOSE_TAB_DETAIL = "'{name}' is still running.\nClosing the tab stops it."
CLOSE_TAB_ANSWER = "CLOSE TAB"

BUSY_DELAY = 0.25
"""How long a step has to take before the app says it is working on it.

Every step is a screen popped and another pushed, and most of them are over
before the frame in between has been drawn. Announcing those would be a mark
that appears and vanishes at every choice the user makes — motion that says
nothing and reads as flicker. Waited out rather than measured beforehand,
because whether reading a stack's scripts is instant or is a network round trip
depends on what is in the store and on what settings say to check."""

INSTALL_PAUSE = 1.2
"""How long the installing screen is held before the app actually goes.

`docs/decisions/0004` says arming comes first and quitting follows immediately,
because what has been armed is waiting for exactly that. This does not break
that rule so much as pay a fixed, named price against it: the waiter allows 120
seconds and this spends one of them.

A floor rather than a delay. Shutting down writes the tabs and takes every run
down, which for a run mid-`npm install` is a subprocess tree and covers this on
its own - but with nothing running it is instant, and a message that appears and
disappears inside one frame is worse than no message at all: the user sees a
flicker and cannot say what it was."""

WINDOW_POLL_INTERVAL = 0.2
"""How often the window is measured.

There is no event for it: the window is resized by the window manager, and what
reaches the app is a new cell grid — which the same drag produces several of, and
a drag onto a display at another scale produces none of, while changing the size
in pixels. So it is asked for rather than waited for.

Five times a second, because the poll is also where the end of a drag is noticed
(`SizeGuard.hold`), and a window that snaps back a whole second after the button
came up reads as the app arguing rather than as a floor. A tick is two reads
against state the input system and the window manager already hold, and nothing
is drawn unless the measurement changed.

What must not follow from a faster poll is a resize per tick. The version of this
that held the window to a floor *and* a ratio ran ten times a second and asked
every time, and see `infrastructure/window_shape.py` for what that cost — the cap
and the wait-for-the-button-up are both there.
"""

WINDOW_NOTICE = "▲ window {width}x{height} → {floor_width}x{floor_height}"
"""Said in the header while the window is smaller than the layout wants.

The size it is against the size it should be, because "too small" without a
number leaves the user dragging an edge and guessing whether they are there yet.
"""

UPDATE_MARK = "▲"
"""The glyph on the update badge. Geometric Shapes, like every other mark drawn
here — see `tests/test_glyphs.py` for why a plausible-looking emoji is not an
option, and `domain/updates.py` for why the domain formats the notice around a
mark it is handed rather than holding one."""

UPDATE_CONFIRM = "Install the update?"
UPDATE_DETAIL = (
    "{version} replaces {installed}.\n"
    "The toolbox will close, update itself, and reopen."
)
UPDATE_ANSWER = "INSTALL AND RESTART"
"""What saying yes costs, spelled out.

It closes the app — which is not what "install" implies on its own, and is the
part somebody with a run going needs to know before they press it. The quit
confirmation still happens underneath this one when runs are live, so the count
is named there rather than repeated here.
"""

UPDATE_DECLINED = "Left alone - nothing was installed."

UPDATE_RUNS = "\n{count} other run{s} will be stopped."
"""Added to the install question when something else is going.

The run doing the asking is not counted: it is about to end either way, and naming it
turns the question into the app arguing with the button just pressed.
"""
NO_INSTALLER_AT = "There is no installer at {path} any more."

NOTHING_TO_INSTALL = "Nothing to install - no newer build has been downloaded."
"""Ctrl+U pressed with no badge up.

The key is only ever advertised on the badge, so a press without one is somebody
guessing - and a deliberate keypress that does nothing at all reads as a broken
key. One line where a workflow without a panel says things.
"""

UPDATE_FAILED = "The update could not be started: {problem}"
"""Said in the activity log rather than in a dialog.

A handover that could not be armed leaves the app exactly where it was, which is
not a state anybody needs a modal about — and the app is still perfectly usable,
which a dialog would imply it is not.
"""

T = TypeVar("T")


class TuiConsole(App):
    """The interactive front end, and the owner of every run in flight.

    One workflow used to own one screen and one console. Sessions turn that
    around: the console holds every run, each with its own log, task and form,
    and the run panel becomes a view onto whichever one is selected. Output
    finds its way home through `CURRENT_SESSION` rather than through "the panel
    that happens to be open", which is the only routing rule that survives there
    being more than one.
    """

    TITLE = f"{PEAK_ART}  {APP_NAME} — {APP_TAGLINE}"
    SUB_TITLE = APP_VERSION
    BINDINGS = [
        ("ctrl+q", "leave", "Quit"),
        # The key everyone reaches for out of habit, so it means what they
        # expect rather than nothing. It is checked after the screen's own copy
        # binding, which yields when there is no selection to copy.
        ("ctrl+c", "leave", "Quit"),
        ("ctrl+b", "resume_runs", "Runs"),
        # Does nothing at all until there is an installer downloaded and waiting,
        # which is why it is not in any footer: a hint for a key that is dead most
        # of the time is a control that lies. The badge carries the key instead,
        # and the badge only exists when the key does something.
        ("ctrl+u", "install_update", "Update"),
    ]

    CSS = """
    Screen {
        align: center middle;
    }

    #output {
        width: 100%;
        height: 1fr;
        margin: 1 2;
        border: round $primary-lighten-1;
        border-title-color: $accent;
        padding: 0 1;
        background: $surface;
        scrollbar-size-vertical: 1;
    }

    /* Every workflow step pops one screen before pushing the next, and this is
       what shows in between. Hidden rather than removed so the header and footer
       stay exactly where they are: an empty frame for a frame reads as the same
       surface, an empty bordered box reads as somewhere else. */
    #output.-quiet {
        visibility: hidden;
    }
    """

    def __init__(
        self,
        workspace_label: str | None = None,
        memory: SessionMemory | None = None,
        workspace: str = "",
        watch: UpdateWatch | None = None,
        recent: RecentPathsPort | None = None,
    ) -> None:
        super().__init__()
        self.application: Application | None = None
        self.start_capability = ""
        """A capability to open on instead of the menu. See Ui.choose_capability."""
        self.workspace_label = workspace_label
        self._memory = memory
        self._recent = recent
        """The directories picked before, or None where there is nothing to remember
        with. Kept under the user's home, which an installer update cannot reach."""
        self._watch = watch
        """The launch-time update check, or None where there is nothing to check
        with — the plain console has no chrome to put a badge in, and a test pilot
        has no business asking GitHub anything."""
        self._workspace = workspace
        """Which project's tabs these are. One machine holds several, and the
        tabs of one are not the tabs of another."""
        self.result_code = 0
        # Choice already made at each step, for navigation that has not become a
        # session yet. Once it has, the session carries its own.
        self._steps: dict[str, str] = {}
        self._sessions = SessionRegistry(on_change=self._session_changed)
        self._panel: RunScreen | None = None
        self._attached: RunSession | None = None
        # Set while nothing is claiming the screen, which is what lets the menu
        # loop come back round.
        self._foreground_free = asyncio.Event()
        self._foreground_free.set()
        self._attachment = asyncio.Event()
        self._result_acknowledged = False
        self._window: AppWindow | None = None
        self._window_watch: Timer | None = None
        self._window_notice = ""
        self._update_notice = ""
        self._update: UpdateReport = NOTHING_TO_REPORT
        """What the launch check found. Empty until it has answered, and empty
        forever if update checking is not configured."""
        self._plan: WindowPlan = DEFAULT_PLAN
        self._guard: SizeGuard | None = None
        self._leaving = False
        """Set once the app is on its way out, so the menu loop stops asking.

        Stopping the runs frees the screen, which reads to the loop that started
        them as a workflow letting go — and it comes back round and pushes
        another menu into an app that is already unmounting."""

    def compose(self) -> ComposeResult:
        with AppFrame():
            yield AppHeader()
            yield BusyLine(id="busy")
            yield RichLog(id="output", wrap=True, markup=True, classes="-quiet")
            yield AppFooter([("Ctrl+Q", "Quit")])

    def on_mount(self) -> None:
        self.register_theme(APP_THEME)
        self.theme = naming.APP_SLUG
        output = self.query_one("#output", RichLog)
        output.border_title = "Activity"
        # The window is opened at a size, measured, and put back onto the floor
        # once a drag that took it under one has ended — and never asked for a
        # cell grid, which is the app telling the terminal what shape to be and a
        # fight the terminal wins: its idea of the grid and ours disagree until
        # the pointer lands somewhere other than where it points.
        self._watch_window_shape()
        self._recall()
        # Before `_start`, and deliberately not awaited: it is a worker, so the
        # first paint does not wait for a network round trip. A launch that opened
        # on a blank frame while GitHub was slow would be this feature costing
        # more than it is worth.
        self._look_for_update()
        self._start()

    # -------------------------------------------------------------- the tabs

    def _recall(self) -> None:
        """Put back the tabs the last run of the app left open.

        Before the workflow starts, so a menu walked straight into finds its
        strip already populated. Nothing is resumed and no menu is skipped: a
        restored tab has no workflow behind it, which is what lets the first
        workflow to arrive in that context pick it up rather than open a second
        tab beside it.
        """
        if self._memory is None:
            return
        for remembered in self._memory.remembered(self._workspace):
            self._sessions.restore(remembered, _nothing)

    def _remember(self) -> None:
        """Write the tabs down, at the points where there is something new.

        Not on every line of output: a build prints tens of thousands of them
        and a file rewritten per line is the same mistake as a window resized
        per frame. Called instead when a run ends, a tab is renamed or closed,
        and when the app does — which between them covers everything the user
        typed.
        """
        if self._memory is None:
            return
        self._memory.remember(self._workspace, self._sessions.remembered())

    # ------------------------------------------------------------- the window

    def _watch_window_shape(self) -> None:
        """Open the window at its size, then start reporting on it.

        Nothing to measure or to size without a real terminal in front of one:
        under a test pilot the only window this process could find is the one the
        test runner happens to be sitting in, and resizing that would be a unit
        test rearranging the developer's desk.

        The plan is read once. Which screen the window is on can change, but a
        floor that moved under a window the user had already settled would resize
        it for having been dragged onto another display.
        """
        if self._driver is None or self.is_headless:
            return
        self._window = current_window()
        if self._window is None:
            return
        self._plan = window_plan()
        self._guard = SizeGuard(self._window, self._plan)
        self._guard.open_at_start_size()
        self._check_window_shape()
        self._window_watch = self.set_interval(WINDOW_POLL_INTERVAL, self._check_window_shape)

    def _check_window_shape(self) -> None:
        window = self._window
        if window is None:
            return
        state = window.state()
        if state is None:
            # The window is gone — every later reading would name a handle that
            # is either dead or, once Windows reuses it, somebody else's.
            self._window = None
            self._guard = None
            self._stop_watching_window()
            self._say_about_window("")
            return
        if self._guard is not None and self._guard.hold(state):
            # It just asked for a size, so this measurement is already history.
            # The next tick reads the answer, and says something about it only if
            # the window did not get there.
            return
        if not cramped(state, self._plan):
            self._say_about_window("")
            return
        self._say_about_window(
            WINDOW_NOTICE.format(
                width=state.width,
                height=state.height,
                floor_width=self._plan.min_width,
                floor_height=self._plan.min_height,
            )
        )

    # ------------------------------------------------------------- updating

    @work
    async def _look_for_update(self) -> None:
        """Ask, once, in the background, and say nothing unless there is an answer.

        Every failure is silence. Offline, rate-limited, a repository that does not
        exist, a tag that does not parse — none of it is the user's problem at the
        moment they opened a terminal, and all of it is reported properly by the
        manual check, which is somebody actually asking. `UpdateWatch.look` never
        raises, so this cannot take the mount path down.
        """
        if self._watch is None:
            return
        report = await self._watch.look()
        self._update = report
        self._say_about_update(report.notice(UPDATE_MARK))

    def _say_about_update(self, notice: str) -> None:
        """Put the notice in every header, and only when it has changed.

        The same shape as `_say_about_window` for the same reason: a header
        composed after this ran reads the value back off the app, so a notice
        pushed only on change would be lost by the next screen.
        """
        if notice == self._update_notice:
            return
        self._update_notice = notice
        for screen in self.screen_stack:
            for header in screen.query(AppHeader):
                header.show_update(notice)

    @property
    def update_notice(self) -> str:
        """What a header composed after the fact should show. See `AppHeader`."""
        return self._update_notice

    async def install_update(self, installer: str, version: str) -> str:
        """`Ui.install_update`. Ask, then hand over from a worker of this app's own.

        The worker matters. `_shut_down` cancels every session, and this is called from
        inside one - so the confirmation and the handover run on the app rather than on
        the caller, and the caller's cancellation is then just part of leaving.

        The path is checked before anything is asked. A dialog offering to install a
        file that is not there is a dialog whose only outcome is an error.

        ONE QUESTION, NOT TWO
        =====================
        This asked the quit confirmation underneath the install one, the way `Ctrl+U`
        does. From a keypress that is right - the runs it names are somebody's real
        work. From inside a run panel it is not: the card IS a run, so the second
        dialog asked whether to stop the very run doing the asking. Answering no to
        that - which is the sane answer to "quit with a run going" when you asked to
        install, not to quit - abandoned the install with nothing to say why.
        So the cost of the OTHER runs is named in the one question, and the run that
        is asking is left out of the count.
        """
        if self._watch is None:
            return "update installing is not configured"
        target = Path(installer)
        if not target.is_file():
            return NO_INSTALLER_AT.format(path=installer)

        detail = UPDATE_DETAIL.format(version=version, installed=APP_VERSION)
        session = current_session()
        others = [live for live in self._sessions.live() if live is not session]
        if others:
            detail += UPDATE_RUNS.format(
                count=len(others), s="" if len(others) == 1 else "s"
            )

        answer = await self.push_screen_wait(
            ConfirmScreen(
                UPDATE_CONFIRM,
                self.trail_label(),
                detail=detail,
                confirm=UPDATE_ANSWER,
            )
        )
        if not answer:
            return UPDATE_DECLINED

        problem = self._watch.arm(target)
        if problem:
            return problem
        # From here the app is leaving, and the caller is one of the runs that leaving
        # cancels. Started as a worker so that cancellation cannot take the shutdown
        # with it.
        self._leave_for_update(version)
        return ""

    @work
    async def _leave_for_update(self, version: str) -> None:
        await self._leaving_for_update(version)

    async def _leaving_for_update(self, version: str) -> None:
        """Say what is happening, hold it long enough to be read, then go.

        Both ways of installing end here - the card's own dialog and `Ctrl+U` on
        the badge - so there is one answer to what leaving for an update looks
        like rather than two that can drift.

        The screen goes up BEFORE the shutdown rather than after it. Shutting
        down is the part that takes the time: it writes the tabs down and then
        takes every run with it, and a run mid-`npm install` is a subprocess tree
        to kill. Announced afterwards, the message would appear once there was
        nothing left to wait for, which is the wrong end of the pause entirely.
        """
        await self.push_screen(InstallingScreen(version))
        await asyncio.sleep(INSTALL_PAUSE)
        await self._shut_down()
        self.exit()

    def action_install_update(self) -> None:
        self._install_update()

    @work
    async def _install_update(self) -> None:
        """Hand the machine over to the installer, then get out of its way.

        The order is the whole thing. `hand_over` arms a process that is waiting
        for THIS process to exit before it starts the installer — so arming has to
        come first, and quitting has to follow immediately. Reversed, there is
        nothing left to arm it; skipped, the installer deletes the directory it is
        running from. See `infrastructure/handover.py`.

        ONE QUESTION, NOT TWO
        =====================
        This asked `_confirm_quit` underneath the install one, so an install with
        runs going put up a second dialog asking whether to quit. Two dialogs for
        one decision is a menu to get through rather than a question to answer,
        and the second one asks about quitting when what was pressed was install
        - so the sane answer to "quit with runs going" abandoned the install, and
        abandoned it SILENTLY, with the app sitting back on the menu looking like
        the key had done nothing. `Ui.install_update` already folds the cost of
        the runs into its one question; this is the same shape, arrived at for
        the same reason, and the two now agree.
        """
        if self._watch is None:
            return
        if not self._update.waiting:
            # Pressed without a badge, or pressed after the manual card
            # downloaded something during this launch. `look` answers out of the
            # state file without a request when an installer is already fetched,
            # so this is not a second network call in the common case.
            report = await self._watch.look()
            self._update = report
            self._say_about_update(report.notice(UPDATE_MARK))
        if not self._update.waiting:
            self.write(NOTHING_TO_INSTALL)
            return
        detail = UPDATE_DETAIL.format(
            version=self._update.available.text or self._update.tag,
            installed=APP_VERSION,
        )
        live = self._sessions.live()
        if live:
            detail += UPDATE_RUNS.format(count=len(live), s="" if len(live) == 1 else "s")
        answer = await self.push_screen_wait(
            ConfirmScreen(
                UPDATE_CONFIRM,
                self.trail_label(),
                detail=detail,
                confirm=UPDATE_ANSWER,
                key=AppHeader.UPDATE_HINT,
            )
        )
        if not answer:
            return
        problem = self._watch.hand_over(self._update)
        if problem:
            # Nothing was armed, so nothing is waiting for this process and the app
            # carries on. Said where a workflow without a panel says things. Nothing
            # is announced either - the installing screen goes up only once there is
            # genuinely something waiting for this process to end.
            self.write(UPDATE_FAILED.format(problem=problem))
            return
        await self._leaving_for_update(self._update.available.text or self._update.tag)

    def _stop_watching_window(self) -> None:
        if self._window_watch is not None:
            self._window_watch.stop()
            self._window_watch = None

    def _say_about_window(self, notice: str) -> None:
        """Put the notice in every header, and only when it has changed.

        The guard is the point of the whole arrangement: a window nobody is
        dragging is measured once a second and nothing is drawn at all.
        """
        if notice == self._window_notice:
            return
        self._window_notice = notice
        for screen in self.screen_stack:
            for header in screen.query(AppHeader):
                header.show_window(notice)

    @property
    def window_notice(self) -> str:
        """What a header composed after the fact should show. See `AppHeader`."""
        return self._window_notice

    def _write_to_terminal(self, data: str) -> None:
        """Send an escape sequence the way Textual sends its own.

        Never `sys.stdout`. The driver owns the output stream and on Windows
        writes it from a background thread, so anything written straight to
        stdout can land in the middle of a frame and splice itself into another
        escape sequence — which is how a working screen turns into one whose
        contents no longer sit where it thinks they do.
        """
        driver = self._driver
        if driver is None:
            return
        driver.write(data)
        driver.flush()

    def _clear_pointer_state(self) -> None:
        """Discard pointer state that a focus change can strand mid-gesture."""
        self.capture_mouse(None)
        # Textual otherwise keeps the old hover target until another mouse event.
        # Clearing it makes the first movement after returning emit Enter again.
        self._set_mouse_over(None, None)
        self._end_text_selection()

    def _end_text_selection(self) -> None:
        """Abandon a drag-selection rather than let it outlive the window.

        A press with no matching release — the window went away between them —
        leaves the screen convinced a drag is still in progress. It then treats
        every later movement as extending that selection: the highlight follows
        the pointer, scrollable content auto-scrolls under it, and the press
        that should have chosen something goes into ending the drag instead.
        `clear_selection` alone is not enough; it forgets the selection but
        leaves the drag itself armed.
        """
        for screen in self.screen_stack:
            screen.clear_selection()
            screen._selecting = False
            screen._mouse_down_offset = None

    def on_app_blur(self, event: events.AppBlur) -> None:
        self._clear_pointer_state()

    def on_app_focus(self, event: events.AppFocus) -> None:
        # Some Windows terminals stop forwarding mouse movement/clicks while the
        # window is inactive and don't reliably restore those modes themselves.
        restore_terminal_interaction(self._write_to_terminal)
        self._clear_pointer_state()
        # Repaint rather than re-layout: the terminal may have been redrawn from
        # under us, so every cell is suspect, not just the geometry.
        self.call_after_refresh(self._repaint_everything)

    def _repaint_everything(self) -> None:
        for screen in self.screen_stack:
            screen.refresh(repaint=True, layout=True)

    @work
    async def _start(self) -> None:
        await self.push_screen_wait(SplashScreen())
        assert self.application is not None
        self.result_code = await self.application.run(self.start_capability)
        await self._shut_down()
        self.exit()

    # ------------------------------------------------------------- the output

    def write(self, message: str = "") -> None:
        session = current_session()
        if session is not None and session.panel_open:
            session.write(message, marker=self._marker_for(message))
            return
        self._output().write(message)

    def _output(self) -> RichLog:
        """The activity log, shown from the first thing written into it."""
        log = self.query_one("#output", RichLog)
        log.remove_class("-quiet")
        return log

    def _quiet_output(self) -> None:
        """Nothing to look at again, ready for the next workflow.

        The activity log is where a workflow with no panel of its own reports,
        and the user reads it at the pause that follows. After that it is a line
        from a run that is over — and left standing it rides along behind every
        later transition, which is the one thing the gap between screens exists
        not to do.
        """
        for log in self.query("#output").results(RichLog):
            log.clear()
            log.add_class("-quiet")

    @staticmethod
    def _marker_for(message: str) -> str:
        """Two conventions, both readable in the plain console too: a line
        indented by `RAW_OUTPUT_PREFIX` is verbatim process output, and a line
        ending in an ellipsis is a step about to happen."""
        if message.startswith(RAW_OUTPUT_PREFIX):
            return "output"
        return "step" if message.endswith("...") else "plain"

    def error(self, message: str) -> None:
        session = current_session()
        if session is not None and session.panel_open:
            session.write(message, marker="error")
            return
        self._output().write(f"[bold red]Error: {message}[/bold red]")

    async def ask(self, prompt: str) -> str:
        session = current_session()
        if session is not None and session.panel_open:
            # Blocks this session and nothing else: a hidden run waiting on an
            # answer badges its own tab rather than stopping the app.
            return await session.prompt(prompt)
        await self._claim_screen(session)
        return await self.push_screen_wait(InputScreen(prompt, self.trail_label()))

    async def confirm(self, prompt: str) -> bool:
        session = current_session()
        if session is not None and session.panel_open:
            answer = await session.prompt(f"{prompt} (y/N)")
            return answer.strip().lower() in ("y", "yes")
        await self._claim_screen(session)
        return await self.push_screen_wait(ConfirmScreen(prompt, self.trail_label()))

    # --------------------------------------------- the gap between screens

    async def working(self, label: str, work: Awaitable[T]) -> T:
        """Do `work` with the app saying so, for a step that has no panel.

        A workflow between two menus has nowhere of its own to narrate into:
        the screen it was started from has been popped and the one it is
        heading for cannot be built until this answers. What shows meanwhile is
        the app's own frame — which is the right thing for the tenth of a
        second most steps take, and the wrong thing entirely for a step that
        goes to the network and comes back four seconds later, because an empty
        frame that stays put is exactly what a wedged one looks like.

        Only announced once it has outlasted `BUSY_DELAY`, so nothing flickers
        on a step that was never slow. The work itself is untouched either way,
        and a cancel reaches it rather than orphaning it.
        """
        task = asyncio.ensure_future(work)
        try:
            done, _pending = await asyncio.wait({task}, timeout=BUSY_DELAY)
            if task in done:
                return task.result()
            self._show_busy(label)
            try:
                return await task
            finally:
                self._show_busy("")
        except asyncio.CancelledError:
            # `asyncio.wait` hands the cancel on without touching what it was
            # waiting for, and a fetch left running past the workflow that
            # wanted it is a subprocess nothing will ever reap.
            task.cancel()
            raise

    def _show_busy(self, label: str) -> None:
        for line in self.query("#busy").results(BusyLine):
            line.show(label)

    # ------------------------------------------------------------- the panel

    @property
    def sessions(self) -> SessionRegistry:
        """Every run there is, whichever context made it."""
        return self._sessions

    async def start_run(self, workflow: Workflow, label: str) -> None:
        session = self._sessions.create(label, workflow, steps=dict(self._steps))
        self._foreground_free.clear()
        self._spawn(session)
        await self._foreground_free.wait()

    def _spawn(self, session: RunSession) -> None:
        async def run() -> None:
            # Set inside the task, so it belongs to this task's context and to
            # every task created from it — which is every `write`, `ask` and
            # subprocess the workflow goes on to make.
            CURRENT_SESSION.set(session)
            try:
                await session.workflow()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._here(session).report_failure(error)
            finally:
                self._retire(self._here(session))

        session.foreground = True
        # A worker, not a bare task: a session started from the tab strip has no
        # worker above it to inherit, and a workflow that cannot push a screen
        # is one that cannot ask a question or show a menu.
        session.task = self.run_worker(
            run(), name=session.name, group="session", exit_on_error=False
        )
        self._settle()

    @staticmethod
    def _here(started_in: RunSession) -> RunSession:
        """The session a workflow is using now, which is not always the one it
        started in — walking to another context moves it to that context's."""
        return current_session() or started_in

    def _retire(self, session: RunSession) -> None:
        """The workflow is over. Whether the tab goes with it depends.

        A tab something was run in stays, freed rather than removed: it holds
        the output, the time it happened and the form it was sent, and walking
        back into the stack is how you get to it. One that only ever showed a
        menu or a form nobody sent has nothing to come back for.
        """
        session.panel_open = False
        # Nothing is driving it any more, which is what lets a workflow that
        # walks back in here pick it up instead of opening a tab beside it.
        session.task = None
        self._remember()
        self._background(session)
        if not session.ran:
            self._sessions.remove(session)
        if self._attached is session:
            self._detach()
        else:
            self._settle()
            self._refresh_panel()

    async def open_run_panel(
        self,
        title: str,
        options: Sequence[Option],
        trail: Sequence[str] = (),
        refresh: RefreshRunner | None = None,
        preview: object = None,
        subtitle: str = "",
    ) -> dict[str, OptionValue] | None:
        session = current_session()
        if session is None:
            # No session means no task of our making — a capability driven
            # straight from the CLI rather than off the menu. It still gets a
            # form; having no worker behind it is what `_retire` reads to know
            # nothing will ever come along and clear its tab.
            session = self._sessions.create(title, _nothing, steps=dict(self._steps))
            CURRENT_SESSION.set(session)
        else:
            session = self._relocate(session)
            if session is None:
                # The context is busy and the user is now watching it. Reads
                # to the workflow as a step backwards, which puts its own menu
                # up once the screen comes free.
                return None
        # A panel already open is one the user asked to keep for another run, so
        # it collects the next set of values rather than being replaced.
        if not session.panel_open:
            session.load(
                title,
                options,
                trail or tuple(session.steps.values()),
                refresh,
                preview,
                subtitle,
            )
        self._attach(session)

        values = await session.wait_for_values()
        if values is None:
            session.panel_open = False
            if self._attached is session:
                self._detach()
            return None
        return values

    def _relocate(self, session: RunSession) -> RunSession | None:
        """The session for where the workflow is now, which may not be this one.

        A session belongs to one context for the whole of its life, so a
        workflow that has walked to another stack carries on in a session
        there rather than dragging this one along. Dragging it along is what
        put two stacks' runs in one terminal, and it is why the tab a user left
        going in React Native was the same tab that greeted them in Flutter.

        The context is only fully known here, at the panel — a menu deeper than
        the last one answered has not been asked yet — so this is where the
        move happens rather than at each step. What is left behind is dropped:
        nothing ever ran in it, and a tab nobody can reach from the strip they
        walked to is litter.

        Walking into a stack that already has a tab nothing is driving picks
        that tab up rather than opening one beside it. That is what makes going
        back into React Native mean going back to the React Native run — its
        output still above, the time it happened still in it, and the form
        still holding what it was sent.
        """
        if session.scope == session.place:
            return session

        free = self._free_at(session.place)
        busy = tuple(s for s in self._sessions.visible(session.place) if s.panel_open)
        if free is None and busy:
            # Every tab here is mid-run. There is nothing to fill in, so the
            # user is put on the run that is already going rather than handed a
            # second tab beside it, and this workflow waits for the screen —
            # which it gets back as the menu it came from when they leave.
            #
            # Only a tab with a form on it counts: one whose own workflow has
            # walked off to a menu has nothing to show, and attaching it put up
            # a run panel with an empty configuration pane.
            self._attach(busy[0])
            return None

        moved = free or self._sessions.create(
            session.base,
            session.workflow,
            steps=dict(session.steps),
            preset=dict(session.preset),
        )
        moved.steps = dict(session.steps)
        moved.answers = dict(session.answers)
        moved.preset = dict(session.preset)
        moved.foreground = session.foreground
        # Handed over, not shared. A tab something has already been run in is
        # kept rather than removed, and one kept still wanting the screen is a
        # claim with no workflow left to release it: `_retire` only ever reaches
        # the session the workflow ended up in, so the menu loop would wait on
        # this one for the rest of the app's life — the empty frame with nothing
        # but the quit chord on it.
        session.foreground = False
        self._teach(session.place, session.workflow)
        # The worker follows the workflow. Left on the tab behind, Stop and
        # quit would reach for this run through a session it has walked out of.
        moved.task, session.task = session.task, None
        if not session.ran:
            self._sessions.remove(session)
        CURRENT_SESSION.set(moved)
        return moved

    def _teach(self, place: tuple[str, ...], workflow: Workflow) -> None:
        """Hand this context's workflow to every tab in it that is idle.

        A tab restored from the last run of the app has no workflow — a callable
        is not something a JSON file can hold — and the strip's "+" is "another
        one of these", which needs one. Every tab in a context leads to the same
        capability, so the first workflow to walk in can say what it is for all
        of them; a tab with a run in it is left alone, because its own workflow
        is the one that is running.
        """
        for tab in self._sessions.visible(place):
            if tab.task is None:
                tab.workflow = workflow

    def _free_at(self, place: tuple[str, ...]) -> RunSession | None:
        """A tab in this context with no workflow behind it, if there is one.

        Oldest first, so coming back twice lands on the same tab rather than
        wandering through them.
        """
        return next(
            (s for s in self._sessions.visible(place) if s.task is None), None
        )

    async def run_in_panel(self, work: Awaitable[T]) -> T | None:
        session = current_session()
        if session is None or not session.panel_open:
            return await work

        task = asyncio.ensure_future(work)
        session.work = task
        session.changed()
        try:
            return await task
        except asyncio.CancelledError:
            # Stop cancels this task and nothing else. Anything else cancelling
            # it is the app shutting down, which has to keep unwinding — and
            # take the work down with it.
            task.cancel()
            if not session.stop_requested:
                raise
            return None
        except Exception as error:
            # The panel reports it and the user reads it there. Letting it out
            # would kill the worker running the workflow, take its output with
            # it, and lose everything that led up to the failure.
            session.report_failure(error)
            return None
        finally:
            session.work = None
            session.changed()

    def panel_failure(self) -> str:
        session = current_session()
        return session.failure if session is not None else ""

    async def close_run_panel(self, message: str = "", ok: bool = True) -> bool:
        session = current_session()
        if session is None or not session.panel_open:
            return False

        session.finish(message, ok)
        if await session.wait_for_next():
            session.reset()
            self._refresh_panel()
            return True

        session.panel_open = False
        session.result_acknowledged = True
        if session.task is None:
            # Nothing is going to run out and retire this one, so closing its
            # panel is the end of it — otherwise it sits in the strip forever
            # as a run that never finishes.
            self._retire(session)
            return False
        if self._attached is session:
            self._detach()
        else:
            self._refresh_panel()
        return False

    # ---------------------------------------------------------- attach/detach

    def _attach(self, session: RunSession) -> None:
        previous = self._attached
        if previous is not None and previous is not session:
            # Whatever it was doing carries on; it just stops being the thing
            # holding the screen.
            self._background(previous)
        self._attached = session
        session.foreground = True

        panel = self._panel
        if panel is not None and panel in self.screen_stack:
            panel.show(session)
        else:
            self._panel = RunScreen(session, self._sessions)
            self.push_screen(self._panel)

        self._signal_attachment()
        self._settle()
        self._refresh_badges()

    def _detach(self) -> None:
        """Take the panel off the screen, and nothing more.

        Deliberately not the same thing as backgrounding the session. A run
        whose panel closes because the user backed out of the form is still
        mid-workflow and about to put a menu of its own up; freeing the menu
        loop here would push a second one over the top of it, which is two
        owners of one screen stack.
        """
        self._attached = None
        panel = self._panel
        if panel is not None and self.screen is panel:
            self.pop_screen()
            self._panel = None
        self._signal_attachment()
        self._settle()
        self._refresh_badges()

    def _background(self, session: RunSession) -> None:
        """This run no longer wants the screen, which is what frees the menu."""
        session.foreground = False
        self._settle()

    def _signal_attachment(self) -> None:
        self._attachment.set()
        self._attachment = asyncio.Event()

    def _settle(self) -> None:
        busy = self._attached is not None or any(
            session.foreground for session in self._sessions.all()
        )
        if busy:
            self._foreground_free.clear()
        else:
            self._foreground_free.set()

    async def _claim_screen(self, session: RunSession | None) -> None:
        """Hold until a screen pushed from here would land on top."""
        while self._attached is not None and self._attached is not session:
            await self._attachment.wait()
        if self._attached is session and session is not None:
            self._detach()

    def _refresh_panel(self) -> None:
        if self._panel is not None and self._attached is not None:
            self._panel.render_state()
        self._refresh_badges()

    def _session_changed(self, session: RunSession | None) -> None:
        self._refresh_panel()

    @property
    def runs_summary(self) -> str:
        return self._sessions.summary()

    def _refresh_badges(self) -> None:
        summary = self._sessions.summary()
        for screen in self.screen_stack:
            for header in screen.query(AppHeader):
                header.show_runs(summary)
            if isinstance(screen, CardMenuScreen):
                screen.show_runs(summary)
                screen.show_counts(self._sessions.running_under)

    # ------------------------------------------------- what the panel reports

    def on_run_screen_chosen(self, message: RunScreen.Chosen) -> None:
        self._attach(message.session)

    def on_run_screen_detached(self, message: RunScreen.Detached) -> None:
        # The one gesture that means "leave it running": the run gives up the
        # screen, and the menu it came from comes back.
        session = self._attached
        self._detach()
        if session is None:
            return
        # Before backgrounding, not after: freeing the foreground with nothing
        # else holding it releases the menu loop, and the top menu it puts up
        # would land under whatever is opened here a moment later. And only
        # when nothing else is already waiting for the screen — a workflow that
        # walked in on this run is holding its own menu ready, and a second one
        # would land on top of it.
        if not any(s.foreground for s in self._sessions.all() if s is not session):
            self._resume_behind(session)
        self._background(session)

    def _resume_behind(self, session: RunSession) -> RunSession | None:
        """Put the menu this run was started from back up, still one step in.

        Esc means one step back, not "out to the top" — the run carries on in
        its own tab, and what the user lands on is the menu that chose it, so
        starting another is picking a different stack rather than walking in
        from the front again. A workflow with no menu behind its panel has no
        step to go back to, and leaving is the whole of what Esc can mean there.
        """
        steps = dict(session.steps)
        if len(steps) < 2:
            return None
        answers = dict(session.answers)
        last = next(reversed(steps))
        steps.pop(last)
        answers.pop(last, None)
        resumed = self._sessions.create(
            session.base, session.workflow, steps=steps, preset=answers
        )
        self._spawn(resumed)
        return resumed

    def on_run_screen_added(self, message: RunScreen.Added) -> None:
        parent = self._attached
        if parent is None:
            return
        # Another one of these, not another walk through the same three menus:
        # the sibling inherits what every step already answered.
        session = self._sessions.create(
            parent.base,
            parent.workflow,
            steps=dict(parent.steps),
            preset=dict(parent.answers),
        )
        self._spawn(session)
        self._attach(session)

    def on_run_screen_closed(self, message: RunScreen.Closed) -> None:
        self._close_session(message.session)

    def on_run_screen_renamed(self, message: RunScreen.Renamed) -> None:
        self._rename_session(message.session)

    @work
    async def _close_session(self, session: RunSession) -> None:
        if session.status.live and not await self.push_screen_wait(
            ConfirmScreen(
                CLOSE_TAB_CONFIRM,
                self.trail_label(),
                detail=CLOSE_TAB_DETAIL.format(name=session.name),
                confirm=CLOSE_TAB_ANSWER,
            )
        ):
            return
        # The neighbour takes the screen first, so the panel is handed over
        # rather than torn down and rebuilt — and so unwinding the closed run
        # cannot find itself still attached and pop the panel out from under it.
        if self._attached is session:
            neighbour = self._neighbour(session)
            if neighbour is not None:
                self._attach(neighbour)
        await self._stop_run(session)
        if self._attached is session:
            self._detach()
        self._remember()

    def _neighbour(self, session: RunSession) -> RunSession | None:
        shown = self._sessions.visible(session.scope)
        rest = [other for other in shown if other is not session]
        return rest[0] if rest else None

    @work
    async def _rename_session(self, session: RunSession) -> None:
        name = await self.push_screen_wait(
            InputScreen(f"Rename '{session.name}' to", self.trail_label())
        )
        self._sessions.rename(session, name)
        self._refresh_panel()
        self._remember()

    # ------------------------------------------------------- coming back to it

    def action_resume_runs(self) -> None:
        """Back into whatever is still going, from wherever the user is."""
        if isinstance(self.screen, ModalScreen) or self._attached is not None:
            return
        self._pick_run()

    @work
    async def _pick_run(self) -> None:
        """Show everything in flight and go to whichever one is chosen.

        Each context keeps its own strip, so no strip can list them all — and
        going straight to whichever run seemed most urgent was a guess made on
        the user's behalf about which one they meant.
        """
        waiting = self._resumable()
        if not waiting:
            return
        session = await self.push_screen_wait(RunsScreen(waiting))
        if session is not None and session in self._sessions.all():
            self._attach(session)

    def _resumable(self) -> tuple[RunSession, ...]:
        """Runs there is something to go back to, most wanting attention first.

        Only sessions with a form on them. A workflow part-way through its
        menus owns a session too, and it has nothing to show — going to one
        puts up a run panel with an empty configuration pane.
        """
        order = ("asking", "failed", "done", "running", "idle")
        return tuple(
            sorted(
                (s for s in self._sessions.all() if s.panel_open),
                key=lambda s: order.index(s.status.value),
            )
        )

    # -------------------------------------------------------------- shutdown

    def action_leave(self) -> None:
        self._leave()

    @work
    async def _leave(self) -> None:
        if not await self._confirm_quit():
            return
        await self._shut_down()
        self.exit()

    async def _confirm_quit(self, deliberate: bool = True) -> bool:
        """Nothing leaves the app on a keystroke that could have meant something else.

        Quitting on top of live runs is N directories abandoned rather than one,
        so that is always named before anything is thrown away. With nothing
        going, only the quit chords go straight out: Esc is a navigation key, and
        the press that walks out of the last menu is one further than the user
        was aiming — mash it and the app should still be there.
        """
        live = self._sessions.live()
        if live:
            count = f"{len(live)} run{'s are' if len(live) > 1 else ' is'}"
            return await self._ask_quit(QUIT_WITH_RUNS.format(count=count))
        if deliberate:
            return True
        return await self._ask_quit(QUIT_DETAIL)

    async def _ask_quit(self, detail: str) -> bool:
        return await self.push_screen_wait(
            ConfirmScreen(
                QUIT_CONFIRM,
                self.trail_label(),
                detail=detail,
                confirm=QUIT_ANSWER,
            )
        )

    async def _shut_down(self) -> None:
        """Write the tabs down, then take the runs down — in that order.

        Stopping a run removes its session from the registry, so tabs written
        down afterwards are no tabs at all, and "no tabs" is how this store says
        the project was closed out. Written first, the record is of the app as
        the user left it: every form as filled in, every name as given, and a
        run that was going logged up to the moment it was killed.
        """
        self._leaving = True
        self._remember()
        await self._stop_every_run()

    async def _stop_every_run(self) -> None:
        """N tasks to unwind and N subprocesses to kill, not one of each.

        The single-run path already did this correctly; it only had to fan out.
        """
        for session in self._sessions.all():
            await self._stop_run(session)

    async def _stop_run(self, session: RunSession) -> None:
        """Take the whole workflow down, not just the slow thing inside it.

        Deliberately not `request_stop`: that is the user saying "stop this run"
        and the workflow is meant to survive it and report the stop. Here the
        workflow itself is going away, so the cancellation has to travel all the
        way out rather than being caught and recovered from.
        """
        worker = session.task
        if worker is not None and not worker.is_finished:
            worker.cancel()
            with suppress(Exception):
                await worker.wait()
        self._sessions.remove(session)

    # ---------------------------------------------------------------- menus

    def trail_label(self) -> str:
        return TRAIL_SEPARATOR.join(self._current_steps().values())

    def _current_steps(self) -> dict[str, str]:
        session = current_session()
        return session.steps if session is not None else self._steps

    def _enter_step(self, step: str) -> tuple[str, ...]:
        """Forget this step and everything downstream, then return what precedes it.

        Re-asking a step means its old answer is being replaced, so the trail
        corrects itself without the caller having to say it went backwards.
        """
        steps = self._current_steps()
        for later in TRAIL_STEPS[TRAIL_STEPS.index(step) :]:
            steps.pop(later, None)
        return tuple(steps.values())

    def _record(self, step: str, name: str, chosen: object) -> None:
        steps = self._current_steps()
        steps[step] = name
        session = current_session()
        if session is not None:
            session.answers[step] = chosen

    def _entry(
        self, trail: tuple[str, ...], key: str, name: str, description: str
    ) -> MenuEntry:
        """One card, told what is still going on behind it.

        Where the card leads is where the menu stands plus the card's own name,
        which is exactly the context a run started there would belong to. With
        a strip per context that is the only thing telling the user a run they
        left is down this way — the strip they are looking at will not.
        """
        return MenuEntry(key, name, description, self._sessions.running_under((*trail, name)))

    def _replay(self, step: str) -> object | None:
        """The answer a sibling session inherited for this step, if it has one."""
        session = current_session()
        if session is None:
            return None
        return session.preset.pop(step, None)

    async def choose_capability(
        self,
        capabilities: Sequence[Capability],
        preselect: str = "",
    ) -> Capability | None:
        chosen = next((c for c in capabilities if c.info.key == preselect), None)
        if chosen is not None:
            # Recorded exactly as a real choice is, so the breadcrumb, the session's
            # scope and the tab this lands in are the same either way. No screen is
            # claimed because nothing has taken one yet - this only ever answers the
            # first call.
            self._enter_step("capability")
            self._record("capability", chosen.info.name, chosen)
            return chosen
        if self._leaving:
            # Nothing left to choose: the loop asking is on its way out, and a
            # menu pushed now would be mounted into a screen stack being torn
            # down. Reads to the loop as "the user walked out of the top menu",
            # which is what has just happened.
            return None
        session = current_session()
        # A workflow that was stopped never reached its pause, so the flag it
        # left behind is cleared here rather than swallowing the next one.
        self._result_acknowledged = False
        self._quiet_output()
        if session is not None:
            session.result_acknowledged = False
        await self._claim_screen(session)
        trail = self._enter_step("capability")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "What would you like to do?",
                [
                    self._entry(
                        trail, item.info.key, item.info.name, item.info.description
                    )
                    for item in capabilities
                ],
                subtitle="Choose a workflow to get started.",
                back_label="Quit",
                trail=trail,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            # Leaving here is leaving for good, so anything still going gets
            # named before it is thrown away rather than after — and so does
            # leaving at all, because Esc got here by walking rather than by
            # being aimed.
            if await self._confirm_quit(deliberate=False):
                return None
            return await self.choose_capability(capabilities)
        self._record("capability", capabilities[index].info.name, capabilities[index])
        return capabilities[index]

    async def choose_scaffold_target(
        self,
        options: Sequence[ScaffoldTargetOption],
        notice: str = "",
    ) -> ScaffoldTarget | None:
        inherited = self._replay("scaffold_target")
        match = next((o for o in options if o.target is inherited), None)
        if match is not None:
            self._record("scaffold_target", match.name, match.target)
            return match.target

        await self._claim_screen(current_session())
        trail = self._enter_step("scaffold_target")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "What do you want to scaffold?",
                [
                    self._entry(
                        trail, option.target.value, option.name, option.description
                    )
                    for option in options
                ],
                subtitle="Pick what the template pack should generate.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record(
            "scaffold_target", options[index].name, options[index].target
        )
        return options[index].target

    async def choose_template_pack(
        self,
        packs: Sequence[TemplatePack],
        notice: str = "",
    ) -> TemplatePack | None:
        inherited = self._replay("template_pack")
        if inherited in packs:
            pack = next(p for p in packs if p is inherited)
            self._record("template_pack", pack.info.name, pack)
            return pack

        await self._claim_screen(current_session())
        trail = self._enter_step("template_pack")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose a stack",
                [
                    self._entry(
                        trail, pack.info.key, pack.info.name, pack.info.description
                    )
                    for pack in packs
                ],
                subtitle="Every stack ships as a versioned template pack.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record("template_pack", packs[index].info.name, packs[index])
        return packs[index]

    async def choose_folder(self, start: str = "", prompt: str = "") -> str | None:
        # No step recorded: a folder is not one of the menus a run's breadcrumb is
        # made of, and putting it in TRAIL_STEPS would make going back to a menu
        # forget it.
        await self._claim_screen(current_session())
        chosen = await self.push_screen_wait(
            PathScreen(
                start,
                prompt or "Choose a project",
                recent=self._recent.recent() if self._recent is not None else (),
            )
        )
        # Recorded on the way out, and only for an answer: a dialog somebody escaped
        # out of said nothing about where they work, and a history that filled up with
        # cancelled navigation would be a history of nothing.
        if chosen and self._recent is not None:
            self._recent.remember(chosen)
        return chosen

    async def choose_script_section(
        self,
        sections: Sequence[ScriptSection],
        notice: str = "",
    ) -> ScriptSection | None:
        inherited = self._replay("script_section")
        if isinstance(inherited, ScriptSection):
            # Matched by key rather than by identity: the document is read off the
            # disk again on the way back in, so a sibling tab repeating this step is
            # holding an equal section, not the same one.
            match = next((s for s in sections if s.key == inherited.key), None)
            if match is not None:
                self._record("script_section", match.name, match)
                return match

        await self._claim_screen(current_session())
        trail = self._enter_step("script_section")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose a group",
                [
                    # No detail passed: CardMenuScreen falls through to hints.py for a
                    # key it knows, which is every domain this toolkit ships, and to the
                    # card's own description for one it does not.
                    self._entry(trail, section.key, section.name, section.summary)
                    for section in sections
                ],
                subtitle="Read from this project's own script config.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record("script_section", sections[index].name, sections[index])
        return sections[index]

    async def choose_script_action(
        self,
        actions: Sequence[ScriptAction],
        notice: str = "",
    ) -> ScriptAction | None:
        inherited = self._replay("script_action")
        if isinstance(inherited, ScriptAction):
            # Matched by identifier rather than by identity: the catalogue is
            # read off the disk again on the way back in, so a sibling tab
            # repeating this step is holding an equal action, not the same one.
            match = next(
                (a for a in actions if a.identifier == inherited.identifier), None
            )
            if match is not None:
                self._record("script_action", match.name, match)
                return match

        await self._claim_screen(current_session())
        trail = self._enter_step("script_action")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose a workflow",
                [
                    MenuEntry(
                        action.reference,
                        action.name,
                        action.summary,
                        self._sessions.running_under((*trail, action.name)),
                        action.detail,
                    )
                    for action in actions
                ],
                subtitle="Read from this stack's script repository.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record("script_action", actions[index].name, actions[index])
        return actions[index]

    async def choose_deployment_project(
        self, projects: Sequence[DeploymentProject], notice: str = ""
    ) -> DeploymentProject | None:
        await self._claim_screen(current_session())
        trail = self._enter_step("deployment_project")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose a project",
                [
                    MenuEntry(
                        project.id,
                        project.name,
                        project.description,
                        detail=project.description,
                    )
                    for project in projects
                ],
                subtitle="Choose the project whose deployment catalog you want.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record("deployment_project", projects[index].name, projects[index])
        return projects[index]

    async def choose_deployment_environment(
        self, environments: Sequence[DeploymentEnvironment], notice: str = ""
    ) -> DeploymentEnvironment | None:
        await self._claim_screen(current_session())
        trail = self._enter_step("deployment_environment")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose an environment",
                [
                    MenuEntry(
                        environment.key,
                        environment.label,
                        environment.description,
                        detail=environment.description,
                    )
                    for environment in environments
                ],
                subtitle="Local and Test run here. Production uses the approval broker.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record(
            "deployment_environment", environments[index].label, environments[index]
        )
        return environments[index]

    async def choose_deployment_group(
        self, groups: Sequence[DeploymentGroup], notice: str = ""
    ) -> DeploymentGroup | None:
        await self._claim_screen(current_session())
        trail = self._enter_step("deployment_group")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose an operation group",
                [
                    MenuEntry(
                        group.id,
                        group.label,
                        group.description,
                        detail=group.description,
                    )
                    for group in groups
                ],
                subtitle="The catalog groups operations by what they change.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record("deployment_group", groups[index].label, groups[index])
        return groups[index]

    async def choose_deployment_operation(
        self, operations: Sequence[DeploymentOperation], notice: str = ""
    ) -> DeploymentOperation | None:
        await self._claim_screen(current_session())
        trail = self._enter_step("deployment_operation")
        index = await self.push_screen_wait(
            CardMenuScreen(
                "Choose an operation",
                [
                    MenuEntry(
                        operation.id,
                        operation.label,
                        operation.description,
                        self._sessions.running_under((*trail, operation.label)),
                        f"[{operation.risk.upper()}] {operation.description}",
                    )
                    for operation in operations
                ],
                subtitle="Read from the project's deployment catalog.",
                trail=trail,
                notice=notice,
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        self._record(
            "deployment_operation", operations[index].label, operations[index]
        )
        return operations[index]

    async def choose_script_update(
        self, catalogue: ScriptCatalogue
    ) -> ScriptUpdate | None:
        await self._claim_screen(current_session())
        trail = tuple(self._current_steps().values())
        answers = SCRIPT_UPDATE_ANSWERS
        index = await self.push_screen_wait(
            CardMenuScreen(
                "These scripts are not the published ones",
                [
                    MenuEntry(key, name, description, detail=detail)
                    for key, name, description, detail in answers
                ],
                subtitle=(
                    f"Running {catalogue.installed or 'an unnamed version'}; "
                    f"{catalogue.available} is published."
                ),
                trail=trail,
                notice=f"Store: {catalogue.location}",
                runs=self._sessions.summary(),
            )
        )
        if index is None:
            return None
        return ScriptUpdate(answers[index][0])

    async def pause(self) -> None:
        session = current_session()
        if session is not None:
            if session.result_acknowledged:
                session.result_acknowledged = False
                return
        elif self._result_acknowledged:
            self._result_acknowledged = False
            return
        await self._claim_screen(session)
        await self.push_screen_wait(ContinueScreen())


async def _nothing() -> None:
    """A session with no workflow of its own behind it."""
    return
