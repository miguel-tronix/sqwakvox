"""Automated MIT Design Structure Matrix (DSM) architecture guard tests.

Computes the exact internal import graph of ``src/sqwakvox`` via Python's AST
(covering both top-level static imports and lazy in-function imports, while
excluding ``if TYPE_CHECKING:`` blocks) and asserts key structural invariants:

1. Zero dependency cycles (no Strongly Connected Components of size > 1).
2. Headless core/domain/backend modules never import the Textual view layer.
3. Domain-agnostic core modules never hard-import concrete domain packages.
4. Views and presenters stay decoupled from heavyweight ``AppController``.
5. Package-wide Propagation Cost stays below the architectural ceiling.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"
PKG_ROOT = SRC_ROOT / "sqwakvox"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _collect_import_graph() -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(modules, internal_edges, external_top_imports)`` for runtime imports."""
    py_files = sorted(p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in str(p))
    mod_paths = {_module_name(p): p for p in py_files if _module_name(p) != "sqwakvox"}
    mod_set = set(mod_paths)

    def resolve_internal(name: str) -> str | None:
        if name in mod_set:
            return name
        parts = name.split(".")
        while len(parts) > 1:
            parts.pop()
            cand = ".".join(parts)
            if cand in mod_set:
                return cand
        return None

    internal: dict[str, set[str]] = defaultdict(set)
    external: dict[str, set[str]] = defaultdict(set)

    class _Visitor(ast.NodeVisitor):
        def __init__(self, cur: str, is_pkg: bool) -> None:
            self.cur = cur
            self.is_pkg = is_pkg
            self.in_tc = 0

        def _record_internal(self, target: str | None) -> None:
            if target and target != self.cur and not self.in_tc:
                internal[self.cur].add(target)

        def visit_If(self, node: ast.If) -> None:
            is_tc = (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING") or (
                isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING"
            )
            if is_tc:
                self.in_tc += 1
                for stmt in node.body:
                    self.visit(stmt)
                self.in_tc -= 1
                for stmt in node.orelse:
                    self.visit(stmt)
            else:
                self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            if self.in_tc:
                return
            for alias in node.names:
                if alias.name.startswith("sqwakvox"):
                    self._record_internal(resolve_internal(alias.name))
                else:
                    external[self.cur].add(alias.name.split(".")[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if self.in_tc:
                return
            if node.level > 0:
                pkg = self.cur.split(".") if self.is_pkg else self.cur.split(".")[:-1]
                up = node.level - 1
                base = pkg[: len(pkg) - up] if up > 0 else pkg
                full_base = ".".join(base + ([node.module] if node.module else []))
            else:
                full_base = node.module or ""
            if not full_base:
                return
            if full_base.startswith("sqwakvox"):
                for alias in node.names:
                    cand = f"{full_base}.{alias.name}"
                    self._record_internal(cand if cand in mod_set else resolve_internal(full_base))
            else:
                external[self.cur].add(full_base.split(".")[0])

    for mod, path in mod_paths.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _Visitor(mod, path.name == "__init__.py").visit(tree)

    for mod in mod_set:
        internal.setdefault(mod, set())
        external.setdefault(mod, set())

    return mod_set, dict(internal), dict(external)


def _tarjan_cycles(nodes: set[str], edges: dict[str, set[str]]) -> list[list[str]]:
    """Return all Strongly Connected Components with size > 1."""
    counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cycles: list[list[str]] = []

    def strongconnect(v: str) -> None:
        index[v] = lowlink[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in sorted(edges.get(v, set())):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])
        if lowlink[v] == index[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.append(sorted(comp))

    for node in sorted(nodes):
        if node not in index:
            strongconnect(node)
    return cycles


def _propagation_cost(nodes: set[str], edges: dict[str, set[str]]) -> float:
    """Compute MacCormack et al. Propagation Cost (inclusive of diagonal)."""
    ordered = sorted(nodes)
    n = len(ordered)
    idx = {m: i for i, m in enumerate(ordered)}
    reach = [[i == j for j in range(n)] for i in range(n)]
    for u in ordered:
        for v in edges.get(u, set()):
            if v in idx:
                reach[idx[u]][idx[v]] = True
    for k in range(n):
        for i in range(n):
            if reach[i][k]:
                for j in range(n):
                    if reach[k][j]:
                        reach[i][j] = True
    return sum(sum(row) for row in reach) / (n * n)


def test_no_dependency_cycles() -> None:
    """The runtime import graph (static + lazy) must be strictly acyclic."""
    modules, internal, _ = _collect_import_graph()
    cycles = _tarjan_cycles(modules, internal)
    assert cycles == [], f"Dependency cycle(s) detected: {cycles}"


def test_headless_modules_do_not_import_textual_or_renderer() -> None:
    """Domain, backend, and headless session modules must stay UI-free."""
    modules, internal, external = _collect_import_graph()
    headless = [
        m
        for m in sorted(modules)
        if m.startswith(("sqwakvox.domains", "sqwakvox.backend"))
        or m
        in {
            "sqwakvox.formatting",
            "sqwakvox.models",
            "sqwakvox.guardrails",
            "sqwakvox.controller",
            "sqwakvox.agent",
            "sqwakvox.presenter",
            "sqwakvox.doc_session",
        }
    ]
    for mod in headless:
        assert "sqwakvox.renderer" not in internal[mod], f"{mod} must not import sqwakvox.renderer"
        assert "textual" not in external[mod], f"{mod} must not import textual"


def test_generic_core_does_not_import_concrete_domains() -> None:
    """Domain-agnostic core modules must dispatch via the domain registry."""
    _, internal, _ = _collect_import_graph()
    generic_core = (
        "sqwakvox.controller",
        "sqwakvox.agent",
        "sqwakvox.backend.tasks",
        "sqwakvox.presenter",
    )
    for mod in generic_core:
        concrete = [
            dep
            for dep in internal[mod]
            if dep == "sqwakvox.domains.financial" or dep.startswith("sqwakvox.domains.swe")
        ]
        assert concrete == [], f"{mod} hard-imports concrete domain(s): {concrete}"


def test_presenters_and_views_do_not_import_controller() -> None:
    """Views, sessions, and presenters use models DTOs, not AppController."""
    _, internal, _ = _collect_import_graph()
    for mod in ("sqwakvox.presenter", "sqwakvox.doc_session", "sqwakvox.app"):
        assert "sqwakvox.controller" not in internal[mod], (
            f"{mod} must import shared DTOs from sqwakvox.models, not sqwakvox.controller"
        )


def test_propagation_cost_within_budget() -> None:
    """Guard against accidental coupling spikes across the package."""
    modules, internal, _ = _collect_import_graph()
    pc = _propagation_cost(modules, internal)
    assert pc <= 0.245, f"Propagation Cost {pc:.1%} exceeded 24.5% ceiling"
