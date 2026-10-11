"""Lenses: trees of groups over the snapshot's files.

``directory`` (kind ``directory``): one group per directory, files in the group of
their own directory. Containment in a directory is a fact, so groups and
memberships are ``observed`` with high confidence.

``components`` (kind ``inferred``): Python projects (package manifests) and, inside
them, components derived from module names (rule ``arch.components.python_package``):
a module in a subpackage belongs to its top two package levels (``app.users.service``
-> ``app.users``); a module directly in a top-level package belongs to that package
(``app.db`` -> ``app``); a top-level module is its own component. Everything here is
``inferred``. A membership is justified by its group's rule and evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from xraymyrepo.cim import (
    REPOSITORY_KEY,
    Basis,
    Confidence,
    DerivedEvidence,
    Group,
    GroupMember,
    Lens,
    LensKind,
    NodeKind,
)
from xraymyrepo.cim.keys import decode_path, group_key, parent_path_key
from xraymyrepo.cim.nodes import ECOSYSTEM_LANGUAGES, Node, PackageAttributes
from xraymyrepo.cim.provenance import Provenance

from .index import SnapshotIndex
from .params import ALGORITHM

DIRECTORY_LENS = "directory"
COMPONENTS_LENS = "components"

RULE_DIRECTORY = "arch.lens.directory"
RULE_PROJECT = "arch.components.python_project"
RULE_COMPONENT = "arch.components.python_package"


@dataclass
class LensResult:
    lens: Lens
    groups: dict[str, Group] = field(default_factory=dict)
    members: dict[str, GroupMember] = field(default_factory=dict)
    """By file key."""

    def chain(self, file_key: str) -> list[str] | None:
        """Group keys from the lens root down to the file's group; None if unassigned."""
        member = self.members.get(file_key)
        if member is None:
            return None
        chain = []
        key: str | None = member.group_key
        while key is not None:
            chain.append(key)
            key = self.groups[key].parent_key
        return chain[::-1]


def _provenance(rule: str) -> Provenance:
    return Provenance(producer=ALGORITHM, rule=rule)


def _evidence(basis: Basis, rule: str, node_keys: Iterable[str]) -> DerivedEvidence:
    return DerivedEvidence(type="derived", basis=basis, provenance=_provenance(rule),
                           node_keys=tuple(node_keys))  # fmt: skip


# ---------------------------------------------------------------------------
# directory


def directory_lens(index: SnapshotIndex) -> LensResult:
    result = LensResult(Lens(name=DIRECTORY_LENS, kind=LensKind.DIRECTORY))

    def key(node_key: str) -> str:
        return group_key(DIRECTORY_LENS, node_key)

    for node in index.doc.nodes:
        if node.kind not in (NodeKind.REPOSITORY, NodeKind.DIRECTORY):
            continue
        result.groups[key(node.key)] = Group(
            key=key(node.key),
            parent_key=None if node.parent_key is None else key(node.parent_key),
            label=node.name,
            basis=Basis.OBSERVED,
            confidence=Confidence.HIGH,
            provenance=_provenance(RULE_DIRECTORY),
            evidence=(_evidence(Basis.OBSERVED, RULE_DIRECTORY, [node.key]),),
        )
    for file in index.files:
        assert file.parent_key is not None
        result.members[file.key] = GroupMember(
            group_key=key(file.parent_key), node_key=file.key,
            basis=Basis.OBSERVED, confidence=Confidence.HIGH,
        )  # fmt: skip
    return result


# ---------------------------------------------------------------------------
# components


@dataclass
class _Component:
    key: str
    name: str
    parent_key: str | None
    directory: str | None
    """Key of the component package's directory (None for a top-level module)."""
    files: list[Node] = field(default_factory=list)
    init_file: str | None = None


def _python_projects(index: SnapshotIndex) -> list[Node]:
    return [
        n for n in index.doc.nodes
        if isinstance(n.attributes, PackageAttributes)
        and "python" in ECOSYSTEM_LANGUAGES.get(n.attributes.ecosystem, frozenset())
    ]  # fmt: skip


def _root(project: Node) -> str:
    assert isinstance(project.attributes, PackageAttributes)
    return project.attributes.root_key


def _enclosing_project(file_key: str, projects: list[Node]) -> Node | None:
    """The project with the deepest root containing the file (projects are in key order,
    so ties resolve deterministically)."""
    enclosing = [p for p in projects
                 if _root(p) == REPOSITORY_KEY or file_key.startswith(_root(p) + "/")]  # fmt: skip
    return max(enclosing, key=lambda p: len(_root(p)), default=None)


def _ancestor_directory(file_key: str, levels: int) -> str:
    current = parent_path_key(file_key)
    for _ in range(levels):
        current = parent_path_key(current)
    return current


def components_lens(index: SnapshotIndex) -> LensResult:
    result = LensResult(Lens(name=COMPONENTS_LENS, kind=LensKind.INFERRED))
    projects = _python_projects(index)
    used_projects: dict[str, Node] = {}
    components: dict[str, _Component] = {}

    for file in index.component_scope:
        module = index.module_name(file)
        assert module is not None
        is_init = decode_path(file.key).rsplit("/", 1)[-1] == "__init__.py"
        package = module if is_init else module.rpartition(".")[0]
        segments = package.split(".") if package else []
        project = _enclosing_project(file.key, projects)
        scope = REPOSITORY_KEY if project is None else project.key
        if project is not None:
            used_projects[project.key] = project
        if segments:
            name = ".".join(segments[:2])
            directory = _ancestor_directory(file.key, len(segments) - len(name.split(".")))
        else:
            name, directory = module, None
        key = group_key(COMPONENTS_LENS, f"{scope}#{name}")
        component = components.setdefault(
            key,
            _Component(key, name, None if project is None else group_key(COMPONENTS_LENS, scope),
                       directory),
        )  # fmt: skip
        component.files.append(file)
        if is_init and module == name:
            component.init_file = file.key

    for project in sorted(used_projects.values(), key=lambda n: n.key):
        assert isinstance(project.attributes, PackageAttributes)
        key = group_key(COMPONENTS_LENS, project.key)
        result.groups[key] = Group(
            key=key,
            label=project.attributes.package_name,
            basis=Basis.INFERRED,
            confidence=Confidence.HIGH,
            provenance=_provenance(RULE_PROJECT),
            evidence=(_evidence(Basis.INFERRED, RULE_PROJECT,
                                [project.key, project.attributes.manifest_key]),),
        )  # fmt: skip

    for component in sorted(components.values(), key=lambda c: c.key):
        if component.directory is None:
            confidence = Confidence.LOW  # a lone top-level module, not a package
            cited = [component.files[0].key]
        else:
            regular = component.init_file is not None
            confidence = (
                Confidence.HIGH if regular and len(component.files) >= 2 else Confidence.MEDIUM
            )
            cited = [k for k in (component.directory, component.init_file)
                     if k is not None and k in index.nodes]  # fmt: skip
        result.groups[component.key] = Group(
            key=component.key,
            parent_key=component.parent_key,
            label=component.name,
            basis=Basis.INFERRED,
            confidence=confidence,
            provenance=_provenance(RULE_COMPONENT),
            evidence=(_evidence(Basis.INFERRED, RULE_COMPONENT, cited),),
        )
        for file in component.files:
            result.members[file.key] = GroupMember(
                group_key=component.key, node_key=file.key,
                basis=Basis.INFERRED, confidence=confidence,
            )  # fmt: skip
    return result
