from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, TypeVar

from company_tui.domain.capability import Capability
from company_tui.domain.deployment import (
    DeploymentEnvironment,
    DeploymentGroup,
    DeploymentOperation,
    DeploymentProject,
)
from company_tui.domain.options import Option, OptionValue, RefreshOutcome
from company_tui.domain.script_config import (
    ScriptAction,
    ScriptCatalogue,
    ScriptSection,
    ScriptUpdate,
)
from company_tui.domain.template_pack import (
    ScaffoldTarget,
    ScaffoldTargetOption,
    TemplatePack,
)

T = TypeVar("T")

Workflow = Callable[[], Awaitable[None]]

RefreshRunner = Callable[[Option, bool], Awaitable[RefreshOutcome]]
"""Runs one option's refresh, previewing when the second argument is True.

Supplied by whoever opened the panel rather than owned by it: acting means
running a subprocess and re-reading a document, and a screen has neither. The
preview/act split is what lets the panel ask before something is deleted."""


class Ui(Protocol):
    def write(self, message: str = "") -> None: ...

    def error(self, message: str) -> None: ...

    async def start_run(self, workflow: Workflow, label: str) -> None:
        """Run one workflow, and hand the menu back when it lets go of the screen.

        Handed over as a callable rather than awaited by the caller, because a
        presentation that runs several at once needs its own task per run and
        needs to be able to start the same workflow again for a second tab.
        Returning does not mean the workflow finished — only that it is no
        longer the thing on screen.
        """
        ...

    async def open_run_panel(
        self,
        title: str,
        options: Sequence[Option],
        trail: Sequence[str] = (),
        refresh: "RefreshRunner | None" = None,
        preview: "Callable[[dict[str, OptionValue]], str] | None" = None,
        subtitle: str = "",
    ) -> dict[str, OptionValue] | None:
        """Collect every flag up front, then stay open while the work runs.

        Returns the filled-in values, or `None` if the user backed out. Until
        `close_run_panel`, `write`/`ask`/`confirm` report into the same panel, so
        a workflow narrates itself without knowing where the output lands.
        """
        ...

    async def install_update(self, installer: str, version: str) -> str:
        """Install `installer` and leave, or say why not.

        Returns a problem to report, or `""` - and on `""` the app is already on its
        way out, so the caller should stop narrating rather than carry on.

        HERE RATHER THAN IN THE CAPABILITY, because installing means shutting the app
        down, and shutting it down cancels every run - including the one that asked.
        A capability doing this directly would be a worker cancelling itself and then
        awaiting its own completion. So the capability asks, and the thing that owns
        the screen does it from a worker of its own.

        A presentation with no app to close returns a problem. `list` and `doctor`
        print and exit; neither is somewhere to replace the program from.
        """
        ...

    async def working(self, label: str, work: Awaitable[T]) -> T:
        """Do `work` while saying what is being waited for.

        `run_in_panel`'s counterpart for a step with no panel: a workflow
        between two menus has been popped off one screen and cannot build the
        next until this answers, so nothing on screen is about it. Fine for a
        step that takes no time, and indistinguishable from a hang for one that
        goes to the network.

        The result is the work's own, and a presentation with nowhere to put a
        label is free to ignore it and simply await.
        """
        ...

    async def run_in_panel(self, work: Awaitable[T]) -> T | None:
        """Do the panel's work, returning `None` if it did not produce a result.

        The console owns the running task so the panel's Stop can cancel it —
        and only it. Everything the work is waiting on, a subprocess included,
        unwinds through the usual cancellation path.

        An unhandled error is reported rather than raised: it is shown where the
        run was being narrated, and `panel_failure` then says what it was, so a
        broken workflow costs the user a message instead of the whole session.
        """
        ...

    def panel_failure(self) -> str:
        """Why the last run in the panel broke, or `""` if it was stopped."""
        ...

    async def close_run_panel(self, message: str = "", ok: bool = True) -> bool:
        """Report how the run ended, and say whether another one is wanted.

        `True` means the panel is back at its form with the finished run above
        it, ready to be filled in again — doing the same thing twice should not
        mean walking back out through the menu to get here.
        """
        ...

    async def ask(self, prompt: str) -> str: ...

    async def confirm(self, prompt: str) -> bool: ...

    async def choose_capability(
        self, capabilities: Sequence[Capability], preselect: str = ""
    ) -> Capability | None:
        """Choose what to do, or take `preselect` as already chosen.

        A launcher that exists to run one kind of work opens on it rather than on a
        menu whose first job is to be dismissed. It is answered through the same
        recording a real choice makes, so the breadcrumb, the session's scope and
        the tab it lands in are identical either way - the alternative was starting
        a run with no step behind it, which is a tab belonging to nowhere.

        Only the first call honours it. Backing out of that capability then lands on
        the menu, so nothing is unreachable.
        """
        ...

    async def choose_scaffold_target(
        self, options: Sequence[ScaffoldTargetOption], notice: str = ""
    ) -> ScaffoldTarget | None: ...

    async def choose_template_pack(
        self, packs: Sequence[TemplatePack], notice: str = ""
    ) -> TemplatePack | None:
        """Ask again, saying why if the last answer led nowhere.

        A workflow that sends the user back has something to tell them, and the
        place to tell them is the menu they land on. Writing it to the console
        instead would put it behind whatever screen comes next.
        """
        ...

    async def choose_folder(self, start: str = "", prompt: str = "") -> str | None:
        """Ask which directory to work in, or `None` if the user backed out.

        A workflow that can only ever act on the directory it was started in is
        one somebody has to restart in the right place. Separate from the PATH
        field on a form, which asks where a single run should point: this asks
        which project the workflow is about at all.
        """
        ...

    async def choose_script_section(
        self, sections: Sequence[ScriptSection], notice: str = ""
    ) -> ScriptSection | None:
        """Pick which group of a document's actions to look at.

        A menu above the workflow menu, for a repository that declares more than
        one section. It is the document's own grouping rather than one invented
        here: the sections were named by whoever wrote the config, and a card per
        section is the only arrangement that cannot disagree with them.
        """
        ...

    async def choose_script_action(
        self, actions: Sequence[ScriptAction], notice: str = ""
    ) -> ScriptAction | None:
        """Pick one of the workflows a script repository declares.

        A menu rather than a field on the form, because this is the choice of
        which workflow to run and the form is what that workflow asks for. It
        is also the only menu here whose entries are read off a disk, so its
        cards carry their own one-line detail instead of looking one up.
        """
        ...

    async def choose_script_update(
        self, catalogue: ScriptCatalogue
    ) -> ScriptUpdate | None:
        """Ask what to do about a store that has fallen behind its repository.

        Three answers, so a menu rather than a dialog: `ConfirmScreen` asks
        about one irreversible thing and offers exactly two, and "stop asking"
        is neither of them. Backing out is a fourth answer that costs nothing —
        Esc puts the stack menu back with nothing decided.
        """
        ...

    async def choose_deployment_project(
        self, projects: Sequence[DeploymentProject], notice: str = ""
    ) -> DeploymentProject | None: ...

    async def choose_deployment_environment(
        self, environments: Sequence[DeploymentEnvironment], notice: str = ""
    ) -> DeploymentEnvironment | None: ...

    async def choose_deployment_group(
        self, groups: Sequence[DeploymentGroup], notice: str = ""
    ) -> DeploymentGroup | None: ...

    async def choose_deployment_operation(
        self, operations: Sequence[DeploymentOperation], notice: str = ""
    ) -> DeploymentOperation | None: ...

    async def pause(self) -> None: ...
