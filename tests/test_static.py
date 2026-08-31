"""The page, checked the way the Python is checked.

The frontend used to be one 4,200-line file inside a single closure, where
every name was reachable from everywhere and nothing said what depended on
what. Three bugs came out of that in one week: a handler left behind after its
markup was deleted, a variable lost when a block was replaced, and a module
reaching into another one's rendering. None of them were visible until the
page was loaded in a browser.

Splitting it into ES modules turned all three into things a test can see, so
these are those tests. They parse rather than execute - no Node, no browser -
and they assert the structure the split was for:

* every element the script reaches for exists in the markup;
* every import resolves to a module that exports that name;
* the dependency graph stays a DAG, so no cycles creep back in;
* the assets the page asks the browser to fetch are actually there.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "context_orchestration" / "web" / "static"
INDEX = STATIC / "index.html"
JS = STATIC / "js"
CSS = STATIC / "css"


@pytest.fixture(scope="module")
def markup() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def modules() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(JS.glob("*.js"))}


# -- the split itself ------------------------------------------------------


def test_the_page_is_no_longer_one_file(markup):
    """Markup, style and behaviour are separate files, and stay separate."""
    assert "<style>" not in markup, "CSS has crept back into the document"
    # One script tag, the module entry point. Plus the inline theme guard,
    # which must stay inline: a module is deferred and would flash.
    assert markup.count("<script") == 2
    assert '<script type="module" src="/static/js/main.js"></script>' in markup
    assert len(markup.splitlines()) < 1500


def test_every_asset_the_page_asks_for_exists(markup):
    for href in re.findall(r'(?:href|src)="(/static/[^"]+)"', markup):
        assert (STATIC / href[len("/static/"):]).is_file(), href


def test_no_module_is_larger_than_it_should_be(modules):
    """A file this size is the problem the split was solving."""
    for name, src in modules.items():
        assert len(src.splitlines()) < 800, f"{name} is growing back"


# -- element references ----------------------------------------------------
#
# The bug this catches: a handler kept a `$("lvSwitch")` after the element was
# deleted, and the page threw on every load for two commits.

# Built at runtime by seqBuild(), one per stage, so they are never in markup.
RUNTIME_IDS = {"sq-compile", "sq-handover", "sq-work", "sq-reconcile",
               "sq-commit", "sq-handoff"}


def test_every_element_the_script_reaches_for_exists(markup, modules):
    present = set(re.findall(r'\sid="([^"]+)"', markup)) | RUNTIME_IDS
    orphans = []
    for name, src in modules.items():
        for ident in re.findall(r'\$\("([^"]+)"\)', src):
            if ident not in present:
                orphans.append(f"{name} -> #{ident}")
    assert not orphans, "script reaches for elements the markup does not have: " + ", ".join(orphans)


def test_the_stage_ids_the_markup_lacks_are_the_ones_built_at_runtime(modules):
    """Guards the allowlist above against becoming a place to hide things."""
    built = set(re.findall(r'\{ k: "([a-z]+)"', modules["live.js"]))
    assert {"sq-" + k for k in built} == RUNTIME_IDS


# -- imports and exports ---------------------------------------------------
#
# The bug this catches: `budget.js` read a value it never imported, and
# `live.js` called a function defined in `run.js`. Both threw at runtime.


def imports_of(src: str) -> list[tuple[str, list[str]]]:
    out = []
    for names, target in re.findall(r'import\s*\{([^}]*)\}\s*from\s*"\./([^"]+)"', src):
        out.append((target, [n.strip() for n in names.split(",") if n.strip()]))
    return out


def exports_of(src: str) -> set[str]:
    names = set(re.findall(r"export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([\w$]+)", src))
    for group in re.findall(r"export\s*\{([^}]*)\}", src):
        names |= {n.strip().split()[-1] for n in group.split(",") if n.strip()}
    return names


def test_every_import_resolves(modules):
    problems = []
    for name, src in modules.items():
        for target, wanted in imports_of(src):
            if target not in modules:
                problems.append(f"{name} imports from missing {target}")
                continue
            available = exports_of(modules[target])
            for want in wanted:
                if want not in available:
                    problems.append(f"{name} wants {want} from {target}, which does not export it")
    assert not problems, "; ".join(problems)


def test_nothing_is_exported_that_nobody_imports(modules):
    """An export with no importer is either dead code or a missing call."""
    wanted = {
        want
        for src in modules.values()
        for _target, names in imports_of(src)
        for want in names
    }
    unused = []
    for name, src in modules.items():
        if name == "main.js":
            continue  # the entry point exports nothing
        for exported in exports_of(src):
            if exported not in wanted:
                unused.append(f"{name}:{exported}")
    assert not unused, "exported but never imported: " + ", ".join(sorted(unused))


def test_the_dependency_graph_has_no_cycles(modules):
    """A cycle is how shared mutable state sneaks back in."""
    graph = {name: {t for t, _ in imports_of(src)} for name, src in modules.items()}
    seen: set[str] = set()
    stack: list[str] = []

    def walk(node: str) -> None:
        if node in stack:
            raise AssertionError("import cycle: " + " -> ".join(stack + [node]))
        if node in seen:
            return
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            walk(nxt)
        stack.pop()
        seen.add(node)

    for module in sorted(graph):
        walk(module)


def top_level_statements(src: str):
    """Yield lines that begin a statement at the top level of a module.

    Depth has to be tracked rather than inferred from indentation: a module
    ends a multi-line array or object literal at column zero, and that closing
    bracket is a continuation, not a new statement.
    """
    depth = 0
    for raw in src.split("\n"):
        bare = re.sub(r"""\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`[^`]*`|//.*$""", "", raw)
        if depth == 0 and raw[:1] not in ("", " ", "\t", "}", "]", ")", "/", "*"):
            yield raw
        depth += sum(bare.count(c) for c in "{[(") - sum(bare.count(c) for c in "}])")
        depth = max(depth, 0)


def test_only_the_entry_point_has_side_effects_at_import_time(modules):
    """Importing a module must not touch the page; main.js does the wiring.

    Otherwise the page depends on the order the modules happen to evaluate
    in, which is the coupling the split existed to remove.
    """
    offenders = []
    for name, src in modules.items():
        if name == "main.js":
            continue
        body = re.sub(r"^\s*(?:import\b|export\s*\{).*$", "", src, flags=re.M)
        for line in top_level_statements(body):
            if re.match(r"(?:export\s+)?(?:function|const|let|var|class)\b", line):
                continue
            offenders.append(f"{name}: {line.strip()[:60]}")
    assert not offenders, "side effects at import time: " + "; ".join(offenders)


# -- widths ----------------------------------------------------------------


def test_content_is_not_boxed_into_competing_widths():
    """One container width governs the page.

    There were eight different caps stacked inside one container, so every
    section started and ended somewhere different. The only max-widths left
    are the ones that protect rather than constrain.
    """
    allowed = {"max-width: 100%", "max-width: min(24rem, calc(100vw - 2.5rem))"}
    stray = []
    for sheet in sorted(CSS.glob("*.css")):
        for i, line in enumerate(sheet.read_text(encoding="utf-8").split("\n"), 1):
            if "max-width" not in line or line.lstrip().startswith("@media"):
                continue
            for cap in re.findall(r"max-width:\s*[^;]+", line):
                if cap.strip() not in allowed:
                    stray.append(f"{sheet.name}:{i} {cap.strip()}")
    assert not stray, "content width caps are back: " + "; ".join(stray)


def test_the_page_declares_one_content_width():
    base = (CSS / "base.css").read_text(encoding="utf-8")
    assert ".wrap { width: min(1460px, 95vw);" in base
    assert ".pg-section .wrap" not in base, "the playground has its own width again"
