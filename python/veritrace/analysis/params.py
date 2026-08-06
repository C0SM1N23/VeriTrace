"""Parameter inspector — §8.11c.

Small, cheap, and it closes a real class: a parameter that propagates wrongly
through the hierarchy while nobody notices. pyslang hands over the *elaborated*
AST, so the final value of every parameter in every instance is a fact, not an
inference — including whether that instance overrode it or took the default.

§8.11c's rule for what deserves a warning: a parameter left at its default
while the parent has one with a similar name and a different value. That is
what 90% of parameterisation bugs look like — `top` sets `DATA_W = 32`, the
child's `DATA_W` quietly stays at 8, and the design builds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from veritrace.analysis.findings import Finding, Group, Severity

CHECK = "parameter_default"


@dataclass(slots=True)
class ParamNode:
    """One level of the tree §8.11c draws."""

    path: str
    module: str
    #: name -> (value, overridden, inherited-and-differs)
    params: dict[str, tuple[str, bool, bool]] = field(default_factory=dict)
    children: list["ParamNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "module": self.module,
            "params": [
                {"name": n, "value": v, "overridden": o, "shadowed": s}
                for n, (v, o, s) in sorted(self.params.items())
            ],
            "children": [c.to_dict() for c in self.children],
        }


def _shadowed(
    name: str, value: str, overridden: bool, ancestors: list[dict[str, Any]]
) -> bool:
    """§8.11c's rule: default-valued here, but an ancestor has the same name
    with a different value. The names are compared exactly — a fuzzy match
    would turn `ADDR_W` and `DATA_W` into a warning and destroy the signal."""
    if overridden:
        return False
    return any(
        name in scope and scope[name].value != value for scope in ancestors
    )


def tree(elaboration: Any) -> ParamNode | None:
    """The parameter hierarchy, each level marked with what it inherited."""
    instances: dict[str, str] = elaboration.instances
    params = elaboration.parameters
    if not instances:
        return None

    roots = sorted(instances, key=lambda p: (p.count("."), p))
    nodes: dict[str, ParamNode] = {}
    for path in roots:
        ancestors = [params.get(a, {}) for a in _ancestors(path) if a in params]
        node = ParamNode(path=path, module=instances[path])
        for name, pv in sorted(params.get(path, {}).items()):
            node.params[name] = (
                pv.value,
                pv.overridden,
                _shadowed(name, pv.value, pv.overridden, ancestors),
            )
        nodes[path] = node
        parent = path.rsplit(".", 1)[0] if "." in path else None
        if parent is not None and parent in nodes:
            nodes[parent].children.append(node)
    return nodes[roots[0]]


def _ancestors(path: str) -> list[str]:
    """Enclosing instance paths, nearest first."""
    parts = path.split(".")
    return [".".join(parts[:i]) for i in range(len(parts) - 1, 0, -1)]


def scan(elaboration: Any, config: Any = None) -> Iterator[Finding]:
    """One finding per parameter left on a default the parent contradicts."""
    params = elaboration.parameters
    for path in sorted(params):
        ancestors = [params.get(a, {}) for a in _ancestors(path) if a in params]
        if not ancestors:
            continue
        for name, pv in sorted(params[path].items()):
            if not _shadowed(name, pv.value, pv.overridden, ancestors):
                continue
            outer = next(s[name].value for s in ancestors if name in s)
            full = f"{path}.{name}"
            if config is not None and config.is_ignored(full):
                continue
            yield Finding(
                group=Group.PARAMETERS,
                severity=Severity.WARN,
                check=CHECK,
                title=f"{name} = {pv.value} left at its default",
                signal=full,
                loc=pv.loc,
                detail=f"an enclosing scope sets {name} = {outer}; this instance did not override it",
                # Parameters are elaboration-time constants: there is no trace
                # value to explain, so no `[why]` rather than one that errors.
                why=None,
            )
