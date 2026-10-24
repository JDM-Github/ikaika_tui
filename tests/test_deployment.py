import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from company_tui.capabilities.deploy import DeployCapability, command_for
from company_tui.domain.deployment import (
    DeploymentError,
    DeploymentOperation,
    DeploymentParameter,
    DeploymentProject,
    DeploymentRun,
    find_registry,
    load_catalog,
    load_registry,
    resolve_argument,
)
from company_tui.domain.ports import ProcessResult


class RegistryTest(unittest.TestCase):
    def test_loads_projects_and_catalogs_from_the_admin_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "deployment").mkdir()
            (root / "deployment" / "catalog.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "groups": [
                            {
                                "id": "Status",
                                "label": "Status & health",
                                "description": "Read-only checks.",
                                "icon": "status",
                                "order": 10,
                            }
                        ],
                        "operations": [
                            {
                                "id": "status.health",
                                "label": "Health",
                                "group": "Status",
                                "description": "Read the health endpoint.",
                                "environments": ["test"],
                                "risk": "read",
                                "auth": "none",
                                "run": {
                                    "type": "script",
                                    "path": "deployment/check.ps1",
                                    "args": ["-Target", "{target}"],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "deployment" / "projects.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "projects": [
                            {
                                "id": "admin",
                                "name": "IKAIKA Admin",
                                "description": "The admin API.",
                                "root": ".",
                                "catalog": "deployment/catalog.json",
                                "environments": ["test"],
                                "icon": "admin",
                                "order": 10,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            registry = load_registry(root / "deployment" / "projects.json")
            self.assertEqual(["IKAIKA Admin"], [project.name for project in registry.projects])
            self.assertEqual(["test"], [environment.key for environment in registry.projects[0].environments])
            catalog = load_catalog(registry.projects[0].catalog)
            self.assertEqual(["Status & health"], [group.label for group in catalog.groups_for("test")])
            self.assertEqual(["Health"], [operation.label for operation in catalog.operations_for("Status", "test")])

    def test_finds_the_admin_registry_from_the_tui_checkout(self):
        found = find_registry(Path(__file__).resolve().parents[1])
        self.assertTrue(found.is_file())
        self.assertEqual("projects.json", found.name)


class CommandTest(unittest.TestCase):
    def project(self) -> DeploymentProject:
        return DeploymentProject(
            id="admin",
            name="IKAIKA Admin",
            description="The admin API.",
            root=Path("C:/IKAIKA/ikaika_admin"),
            catalog=Path("C:/IKAIKA/ikaika_admin/deployment/operations.json"),
            environments=(),
        )

    def test_resolves_target_and_arguments(self):
        operation = DeploymentOperation(
            id="status.health",
            label="Health",
            group="Status",
            description="Read it.",
            environments=("test",),
            risk="read",
            auth="none",
            parameters=(),
            run=DeploymentRun(
                type="script",
                path="deployment/check-health.ps1",
                args=("-Target", "{target}"),
            ),
        )
        command = command_for(self.project(), operation, "test", {})
        self.assertEqual("-Target", command[-2])
        self.assertEqual("test", command[-1])
        self.assertIn("check-health.ps1", command[3])

    def test_a_portal_operation_uses_the_sibling_portal_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "admin" / "deployment").mkdir(parents=True)
            (root / "ikaika-portal" / "deployment").mkdir(parents=True)
            (root / "ikaika-portal" / "script.ps1").write_text("", encoding="utf-8")
            project = DeploymentProject(
                id="admin",
                name="IKAIKA Admin",
                description="The admin API.",
                root=root / "admin",
                catalog=root / "admin" / "deployment" / "operations.json",
                environments=(),
            )
            operation = DeploymentOperation(
                id="deploy.portal.test",
                label="Deploy portal",
                group="Deploy",
                description="Deploy it.",
                environments=("test",),
                risk="write",
                auth="none",
                parameters=(),
                run=DeploymentRun(
                    type="script",
                    path="deployment/deploy.ps1",
                    repo="portal",
                ),
            )
            command = command_for(project, operation, "test", {})
            self.assertIn(str(root / "ikaika-portal" / "deployment" / "deploy.ps1"), command)

    def test_resolves_a_value_file(self):
        operation = DeploymentOperation(
            id="switch.test",
            label="Switch",
            group="Switches",
            description="Set it.",
            environments=("test",),
            risk="write",
            auth="none",
            parameters=(
                DeploymentParameter(name="value", label="Value", choices=("true", "false")),
            ),
            run=DeploymentRun(
                type="script",
                path="deployment/set-env.ps1",
                args=("-ValueFile", "{valueFile}"),
            ),
        )
        command = command_for(self.project(), operation, "test", {"value": "false"}, "C:/tmp/value.txt")
        self.assertEqual("C:/tmp/value.txt", command[-1])

    def test_refuses_an_unknown_placeholder(self):
        with self.assertRaises(DeploymentError):
            resolve_argument("{missing}", "test", {})


class FakeConsole:
    def __init__(self, project, environment, group, operation):
        self.project = project
        self.environment = environment
        self.group = group
        self.operation = operation
        self.lines = []
        self.commands = []

    def write(self, message=""):
        self.lines.append(message)

    def error(self, message):
        self.lines.append(message)

    async def choose_deployment_project(self, projects, notice=""):
        return self.project

    async def choose_deployment_environment(self, environments, notice=""):
        return self.environment

    async def choose_deployment_group(self, groups, notice=""):
        return self.group

    async def choose_deployment_operation(self, operations, notice=""):
        return self.operation

    async def confirm(self, prompt):
        return True

    async def open_run_panel(self, title, options, trail=(), refresh=None, preview=None, subtitle=""):
        return {}

    async def run_in_panel(self, work):
        return await work

    async def close_run_panel(self, message="", ok=True):
        return False

    def panel_failure(self):
        return ""


class FakeRunner:
    def __init__(self):
        self.calls = []

    async def stream(self, command, on_output, cwd=None):
        self.calls.append((command, cwd))
        on_output("ok")
        return ProcessResult(exit_code=0, stdout="ok", stderr="")


class DeployCapabilityTest(unittest.TestCase):
    def test_runs_a_test_operation_through_the_project_script(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "deployment").mkdir()
            (root / "deployment" / "catalog.json").write_text(
                json.dumps(
                    {
                        "groups": [
                            {
                                "id": "Status",
                                "label": "Status",
                                "description": "Read-only.",
                                "icon": "status",
                                "order": 10,
                            }
                        ],
                        "operations": [
                            {
                                "id": "status.health",
                                "label": "Health",
                                "group": "Status",
                                "description": "Read it.",
                                "environments": ["test"],
                                "risk": "read",
                                "auth": "none",
                                "run": {
                                    "type": "script",
                                    "path": "deployment/check.ps1",
                                    "args": ["-Target", "{target}"],
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (root / "deployment" / "projects.json").write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "admin",
                                "name": "IKAIKA Admin",
                                "description": "The admin API.",
                                "root": ".",
                                "catalog": "deployment/catalog.json",
                                "environments": ["test"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = load_registry(root / "deployment" / "projects.json")
            project = registry.projects[0]
            catalog = load_catalog(project.catalog)
            environment = project.environments[0]
            group = catalog.groups_for("test")[0]
            operation = catalog.operations_for(group.id, "test")[0]
            console = FakeConsole(project, environment, group, operation)
            runner = FakeRunner()
            capability = DeployCapability(
                console=console,
                process_runner=runner,
                registry_path=root / "deployment" / "projects.json",
            )
            self.assertEqual(0, asyncio.run(capability.execute()))
            self.assertEqual(1, len(runner.calls))
            self.assertEqual("test", runner.calls[0][0][-1])
            self.assertEqual(project.root, runner.calls[0][1])


if __name__ == "__main__":
    unittest.main()
