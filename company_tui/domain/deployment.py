"""The deployment catalog the TUI reads, and the project registry beside it.

The admin repository owns the catalog. The TUI only reads it; it never writes a
project, an operation or a credential. Production execution is deliberately not
part of this module: the broker owns that boundary.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGISTRY_ENV = "IKAIKA_DEPLOYMENT_PROJECTS"

ENVIRONMENTS: dict[str, tuple[str, str]] = {
    "local": ("Local workspace", "This checkout and its local databases."),
    "test": ("Test Server", "The shared Test Server and its TEST services."),
    "prod": ("Production", "The live portal. Requires broker approval."),
}

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_.-]+)\}")


class DeploymentError(RuntimeError):
    """The registry or catalog cannot be read."""


@dataclass(frozen=True, slots=True)
class DeploymentEnvironment:
    key: str
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class DeploymentProject:
    id: str
    name: str
    description: str
    root: Path
    catalog: Path
    environments: tuple[DeploymentEnvironment, ...]
    icon: str = ""
    order: int = 0


@dataclass(frozen=True, slots=True)
class DeploymentGroup:
    id: str
    label: str
    description: str
    icon: str = ""
    order: int = 0


@dataclass(frozen=True, slots=True)
class DeploymentParameter:
    name: str
    label: str
    kind: str = "text"
    choices: tuple[str, ...] = ()
    default: str = ""
    required: bool = False


@dataclass(frozen=True, slots=True)
class DeploymentRun:
    type: str
    path: str = ""
    command: str = ""
    args: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentOperation:
    id: str
    label: str
    group: str
    description: str
    environments: tuple[str, ...]
    risk: str
    auth: str
    parameters: tuple[DeploymentParameter, ...]
    run: DeploymentRun


@dataclass(frozen=True, slots=True)
class DeploymentCatalog:
    groups: tuple[DeploymentGroup, ...]
    operations: tuple[DeploymentOperation, ...]

    def groups_for(self, environment: str) -> tuple[DeploymentGroup, ...]:
        present = {operation.group for operation in self.operations if environment in operation.environments}
        return tuple(
            sorted(
                (group for group in self.groups if group.id in present),
                key=lambda group: (group.order, group.label),
            )
        )

    def operations_for(self, group: str, environment: str) -> tuple[DeploymentOperation, ...]:
        return tuple(
            operation
            for operation in self.operations
            if operation.group == group and environment in operation.environments
        )


@dataclass(frozen=True, slots=True)
class DeploymentRegistry:
    path: Path
    projects: tuple[DeploymentProject, ...]

    def project(self, project_id: str) -> DeploymentProject | None:
        return next((project for project in self.projects if project.id == project_id), None)


def find_registry(start: Path | None = None) -> Path:
    """Find deployment/projects.json from a checkout or a sibling admin repo."""
    declared = os.environ.get(REGISTRY_ENV, "").strip()
    if declared:
        path = Path(declared).expanduser()
        if path.is_file():
            return path.resolve()
        raise DeploymentError(f"{REGISTRY_ENV} points at {path}, which is not a file.")

    here = (start or Path.cwd()).resolve()
    for base in (here, *here.parents):
        for candidate in (
            base / "deployment" / "projects.json",
            base / "ikaika_admin" / "deployment" / "projects.json",
        ):
            if candidate.is_file():
                return candidate.resolve()
    raise DeploymentError(
        "No deployment/projects.json was found. Run the TUI from the admin checkout, "
        f"or set {REGISTRY_ENV}."
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise DeploymentError(f"{path}: {error}") from error
    except json.JSONDecodeError as error:
        raise DeploymentError(f"{path}: {error}") from error


def _text(value: Any, field: str, where: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentError(f"{where}: {field} must be a non-empty string.")
    return value.strip()


def _texts(value: Any, field: str, where: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise DeploymentError(f"{where}: {field} must be a list of non-empty strings.")
    return tuple(item.strip() for item in value)


def load_registry(path: Path) -> DeploymentRegistry:
    document = _read_json(path)
    if not isinstance(document, dict):
        raise DeploymentError(f"{path}: the registry must be a JSON object.")
    projects_document = document.get("projects")
    if not isinstance(projects_document, list) or not projects_document:
        raise DeploymentError(f"{path}: projects must be a non-empty list.")

    admin_root = path.parent.parent
    projects: list[DeploymentProject] = []
    seen: set[str] = set()
    for item in projects_document:
        if not isinstance(item, dict):
            raise DeploymentError(f"{path}: each project must be an object.")
        project_id = _text(item.get("id"), "id", path)
        if project_id in seen:
            raise DeploymentError(f"{path}: duplicate project id {project_id}.")
        seen.add(project_id)
        root_value = _text(item.get("root"), "root", path)
        catalog_value = _text(item.get("catalog"), "catalog", path)
        root = (admin_root / root_value).resolve()
        catalog_path = (admin_root / catalog_value).resolve()
        if not root.is_dir():
            raise DeploymentError(f"{path}: project {project_id} root does not exist: {root}")
        if not catalog_path.is_file():
            raise DeploymentError(f"{path}: project {project_id} catalog does not exist: {catalog_path}")
        environments = tuple(
            DeploymentEnvironment(key=key, label=ENVIRONMENTS[key][0], description=ENVIRONMENTS[key][1])
            for key in _texts(item.get("environments"), "environments", path)
            if key in ENVIRONMENTS
        )
        if not environments:
            raise DeploymentError(f"{path}: project {project_id} has no valid environments.")
        projects.append(
            DeploymentProject(
                id=project_id,
                name=_text(item.get("name"), "name", path),
                description=_text(item.get("description"), "description", path),
                root=root,
                catalog=catalog_path,
                environments=environments,
                icon=str(item.get("icon", "") or ""),
                order=int(item.get("order", 0) or 0),
            )
        )

    return DeploymentRegistry(path=path.resolve(), projects=tuple(sorted(projects, key=lambda project: (project.order, project.name))))


def load_catalog(path: Path) -> DeploymentCatalog:
    document = _read_json(path)
    if not isinstance(document, dict):
        raise DeploymentError(f"{path}: the catalog must be a JSON object.")
    groups_document = document.get("groups")
    operations_document = document.get("operations")
    if not isinstance(groups_document, list) or not isinstance(operations_document, list):
        raise DeploymentError(f"{path}: groups and operations must be lists.")

    groups: list[DeploymentGroup] = []
    seen_groups: set[str] = set()
    for item in groups_document:
        if not isinstance(item, dict):
            raise DeploymentError(f"{path}: each group must be an object.")
        group_id = _text(item.get("id"), "id", path)
        if group_id in seen_groups:
            raise DeploymentError(f"{path}: duplicate group id {group_id}.")
        seen_groups.add(group_id)
        groups.append(
            DeploymentGroup(
                id=group_id,
                label=_text(item.get("label"), "label", path),
                description=_text(item.get("description"), "description", path),
                icon=str(item.get("icon", "") or ""),
                order=int(item.get("order", 0) or 0),
            )
        )

    operations: list[DeploymentOperation] = []
    seen_operations: set[str] = set()
    for item in operations_document:
        if not isinstance(item, dict):
            raise DeploymentError(f"{path}: each operation must be an object.")
        operation_id = _text(item.get("id"), "id", path)
        if operation_id in seen_operations:
            raise DeploymentError(f"{path}: duplicate operation id {operation_id}.")
        seen_operations.add(operation_id)
        group = _text(item.get("group"), "group", path)
        if group not in seen_groups:
            raise DeploymentError(f"{path}: operation {operation_id} uses unknown group {group}.")
        risk = _text(item.get("risk"), "risk", path)
        if risk not in {"read", "write", "destructive"}:
            raise DeploymentError(f"{path}: operation {operation_id} has invalid risk {risk}.")
        auth = str(item.get("auth", "none") or "none")
        if auth not in {"none", "prodGate"}:
            raise DeploymentError(f"{path}: operation {operation_id} has invalid auth {auth}.")
        parameters = tuple(
            DeploymentParameter(
                name=_text(parameter.get("name"), "parameters.name", path),
                label=_text(parameter.get("label"), "parameters.label", path),
                kind=str(parameter.get("type", "text") or "text"),
                choices=tuple(str(choice) for choice in parameter.get("choices", []) or []),
                default=str(parameter.get("default", "") or ""),
                required=bool(parameter.get("required", False)),
            )
            for parameter in (item.get("parameters", []) or [])
            if isinstance(parameter, dict)
        )
        run = item.get("run")
        if not isinstance(run, dict):
            raise DeploymentError(f"{path}: operation {operation_id} has no run object.")
        operations.append(
            DeploymentOperation(
                id=operation_id,
                label=_text(item.get("label"), "label", path),
                group=group,
                description=_text(item.get("description"), "description", path),
                environments=_texts(item.get("environments"), "environments", path),
                risk=risk,
                auth=auth,
                parameters=parameters,
                run=DeploymentRun(
                    type=_text(run.get("type"), "run.type", path),
                    path=str(run.get("path", "") or ""),
                    command=str(run.get("command", "") or ""),
                    args=tuple(str(argument) for argument in (run.get("args", []) or [])),
                ),
            )
        )

    return DeploymentCatalog(groups=tuple(groups), operations=tuple(operations))


def resolve_argument(value: str, environment: str, values: dict[str, str], value_file: str = "") -> str:
    """Resolve the manifest's placeholders without invoking a shell."""
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name == "target":
            return environment
        if name == "valueFile":
            return value_file
        if name in values:
            return values[name]
        raise DeploymentError(f"Unknown operation placeholder: {match.group(0)}")

    return _PLACEHOLDER.sub(replace, value)


__all__ = [
    "DeploymentCatalog",
    "DeploymentEnvironment",
    "DeploymentError",
    "DeploymentGroup",
    "DeploymentOperation",
    "DeploymentParameter",
    "DeploymentProject",
    "DeploymentRegistry",
    "DeploymentRun",
    "ENVIRONMENTS",
    "REGISTRY_ENV",
    "find_registry",
    "load_catalog",
    "load_registry",
    "resolve_argument",
]
