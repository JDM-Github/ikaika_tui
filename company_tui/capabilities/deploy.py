"""Run a project's deployment operations from the TUI.

Local and Test operations run through the project's own PowerShell scripts. The
production path is deliberately blocked here: it belongs to the approval broker,
which holds the production credentials.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from company_tui.domain.capability import CANCELLED, Capability, CapabilityInfo
from company_tui.domain.deployment import (
    DeploymentError,
    DeploymentEnvironment,
    DeploymentGroup,
    DeploymentOperation,
    DeploymentProject,
    DeploymentRegistry,
    find_registry,
    load_catalog,
    load_registry,
    resolve_argument,
)
from company_tui.domain.options import Option, OptionKind, OptionValue
from company_tui.domain.ports import ProcessRunner
from company_tui.domain.template_pack import PackActionResult
from company_tui.presentation.ui import Ui

RAW_OUTPUT_PREFIX = "  "
STOPPED_MESSAGE = "Stopped before it finished."
FAILED = 1


def _pwsh() -> str:
    return shutil.which("pwsh") or "pwsh"


def _run_root(project: DeploymentProject, run) -> Path:
    if run.repo == "portal":
        for name in ("ikaika-portal", "ikaika_portal_frontend_rewrite"):
            candidate = project.root.parent / name
            if (candidate / "script.ps1").is_file():
                return candidate
        return project.root.parent / "ikaika-portal"
    return project.root


def command_for(
    project: DeploymentProject,
    operation: DeploymentOperation,
    environment: str,
    values: dict[str, str],
    value_file: str = "",
) -> tuple[str, ...]:
    """Build the exact command for one operation, without a shell."""
    run = operation.run
    root = _run_root(project, run)
    if run.type == "script":
        if not run.path:
            raise DeploymentError(f"{operation.id} has no script path.")
        executable = root / run.path
        prefix = (_pwsh(), "-NoProfile", "-File", str(executable))
    elif run.type == "toolkit":
        if not run.command:
            raise DeploymentError(f"{operation.id} has no toolkit command.")
        prefix = (_pwsh(), "-NoProfile", "-File", str(root / "script.ps1"), run.command)
    elif run.type == "internal":
        raise DeploymentError(f"{operation.id} is an internal operation.")
    else:
        raise DeploymentError(f"{operation.id} has an unsupported run type: {run.type}")

    arguments = tuple(
        resolve_argument(argument, environment, values, value_file)
        for argument in run.args
    )
    return (*prefix, *arguments)


class DeployCapability(Capability):
    def __init__(
        self,
        console: Ui,
        process_runner: ProcessRunner,
        registry_path: Path | None = None,
    ) -> None:
        self._console = console
        self._process_runner = process_runner
        self._registry_path = registry_path
        self._state_cache: dict[tuple[str, str], dict] = {}

    @property
    def info(self) -> CapabilityInfo:
        return CapabilityInfo(
            key="deploy",
            name="Deploy",
            description="Run a project's deployment operations for Local or Test",
        )

    async def execute(self) -> int:
        try:
            registry = load_registry(self._registry_path or find_registry())
        except DeploymentError as error:
            self._console.error(str(error))
            return FAILED

        project = await self._console.choose_deployment_project(registry.projects)
        if project is None:
            return CANCELLED

        try:
            catalog = load_catalog(project.catalog)
        except DeploymentError as error:
            self._console.error(str(error))
            return FAILED

        environment = await self._console.choose_deployment_environment(project.environments)
        if environment is None:
            return CANCELLED

        if environment.key == "prod":
            self._console.write(
                "Production is not available from this TUI yet. "
                "It moves to the approval broker in a later phase; use DevTools for now."
            )
            return CANCELLED

        groups = catalog.groups_for(environment.key)
        if not groups:
            self._console.write(
                f"{project.name} has no {environment.label} operations in its catalog."
            )
            return CANCELLED

        while True:
            group = await self._console.choose_deployment_group(groups)
            if group is None:
                return CANCELLED

            operations = catalog.operations_for(group.id, environment.key)
            operation = await self._console.choose_deployment_operation(operations)
            if operation is None:
                continue

            if operation.risk == "destructive":
                if not await self._console.confirm(
                    f"{operation.label} is destructive. Continue?"
                ):
                    continue

            options = self._options(project, environment, operation)
            note = await self._state_note(project, environment, operation)
            subtitle = operation.description
            if note:
                subtitle = f"{subtitle}\nCurrent: {note}"
            values = await self._console.open_run_panel(
                operation.label,
                options,
                trail=(project.name, environment.label, group.label),
                subtitle=subtitle,
                preview=lambda values, p=project, o=operation, e=environment: self._preview(
                    p, o, e, values
                ),
            )
            if values is None:
                continue

            result = await self._console.run_in_panel(
                self._run(project, operation, environment.key, values)
            )
            if result is None:
                failure = self._console.panel_failure()
                if await self._console.close_run_panel(failure or STOPPED_MESSAGE, ok=False):
                    continue
                return FAILED if failure else CANCELLED

            if await self._console.close_run_panel(
                result.message, ok=result.exit_code == 0
            ):
                continue
            return result.exit_code

    def _preview(
        self,
        project: DeploymentProject,
        operation: DeploymentOperation,
        environment: DeploymentEnvironment,
        values: dict[str, OptionValue],
    ) -> str:
        if operation.run.type == "internal":
            return "Read the local DevTools audit log."
        try:
            command = command_for(
                project,
                operation,
                environment.key,
                {key: str(value) for key, value in values.items()},
                "<temporary value file>",
            )
        except DeploymentError as error:
            return str(error)
        return shlex.join(command)

    async def _switch_state(self, project: DeploymentProject, environment: str) -> dict:
        key = (project.id, environment)
        if key in self._state_cache:
            return self._state_cache[key]
        script = project.root / "deployment" / "switch-state.ps1"
        if not script.is_file():
            self._state_cache[key] = {}
            return {}
        result = await self._process_runner.capture(
            (_pwsh(), "-NoProfile", "-File", str(script), "-Target", environment, "-Json"),
            project.root,
        )
        if result.exit_code != 0:
            self._state_cache[key] = {}
            return {}
        try:
            state = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError):
            state = {}
        self._state_cache[key] = state if isinstance(state, dict) else {}
        return self._state_cache[key]

    async def _state_note(
        self,
        project: DeploymentProject,
        environment: DeploymentEnvironment,
        operation: DeploymentOperation,
    ) -> str:
        if project.id != "admin" or environment.key not in {"local", "test"}:
            return ""
        state = await self._switch_state(project, environment.key)
        if not state:
            return ""
        airtable = "ON" if state.get("airtableSync", {}).get("enabled") else "OFF"
        reminders = "ON" if state.get("reportReminders", {}).get("enabled") else "OFF"
        maintenance = "ON" if state.get("maintenance", {}).get("enabled") else "OFF"
        if operation.id.startswith("switch.airtable_sync."):
            return f"Airtable sync is {airtable}"
        if operation.id.startswith("switch.report_reminders."):
            return f"Report reminders are {reminders}"
        if operation.id.startswith("maintenance."):
            return f"Maintenance mode is {maintenance}"
        if operation.id == "status.switches":
            return f"Airtable sync {airtable}; reminders {reminders}; maintenance {maintenance}"
        return ""

    @staticmethod
    def _options(
        project: DeploymentProject,
        environment: DeploymentEnvironment,
        operation: DeploymentOperation,
    ) -> tuple[Option, ...]:
        options: list[Option] = [
            Option(
                key="target",
                label="Target",
                kind=OptionKind.INFO,
                default=f"{project.name} / {environment.label}",
            ),
            Option(
                key="risk",
                label="Risk",
                kind=OptionKind.INFO,
                default=operation.risk.upper(),
            ),
        ]
        for parameter in operation.parameters:
            kind = OptionKind.CHOICE if parameter.choices else OptionKind.TEXT
            default = parameter.default or (parameter.choices[0] if parameter.choices else "")
            options.append(
                Option(
                    key=parameter.name,
                    label=parameter.label,
                    kind=kind,
                    choices=parameter.choices,
                    default=default,
                    required=parameter.required,
                )
            )
        return tuple(options)

    def _audit(self, project: DeploymentProject) -> PackActionResult:
        path = project.root / ".ikaika_scripts_configs" / "logs" / "deployment-audit.jsonl"
        if not path.is_file():
            self._console.write(f"No audit log yet at {path}.")
            return PackActionResult(available=True, message="No audit entries.")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-200:]
        except OSError as error:
            self._console.error(str(error))
            return PackActionResult(available=True, message=str(error), exit_code=1)
        for line in lines:
            self._console.write(line)
        return PackActionResult(available=True, message=f"{len(lines)} audit entries.")

    async def _run(
        self,
        project: DeploymentProject,
        operation: DeploymentOperation,
        environment: str,
        values: dict[str, OptionValue],
    ) -> PackActionResult:
        if operation.run.type == "internal":
            if operation.id == "status.audit":
                return self._audit(project)
            self._console.write(f"{operation.label} is not available in the TUI yet.")
            return PackActionResult(available=True, message=f"{operation.label} is not available yet.")

        value_file = ""
        temporary: str | None = None
        try:
            if "{valueFile}" in operation.run.args:
                descriptor, temporary = tempfile.mkstemp(
                    prefix="ikaika-operation-", suffix=".txt"
                )
                os.close(descriptor)
                Path(temporary).write_text(str(values.get("value", "")), encoding="utf-8")
                value_file = temporary

            command = command_for(
                project,
                operation,
                environment,
                {key: str(value) for key, value in values.items()},
                value_file,
            )
            self._console.write("$ " + shlex.join(command))
            result = await self._process_runner.stream(
                command,
                lambda line: self._console.write(f"{RAW_OUTPUT_PREFIX}{line}"),
                _run_root(project, operation.run),
            )
            return PackActionResult(
                available=True,
                message=f"{operation.label} finished.",
                exit_code=result.exit_code,
            )
        except (DeploymentError, OSError) as error:
            self._console.error(str(error))
            return PackActionResult(available=True, message=str(error), exit_code=1)
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


__all__ = ["DeployCapability", "command_for"]
