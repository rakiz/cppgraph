"""Regression guard for the scip-clang patch bundle's inter-patch dependency.

The seventh patch (`enclosing-range-macro-on-v0.4.0.patch`) rewrites the
`saveDefinition(...)` arguments that the #504 patch
(`enclosing_range-on-v0.4.0.patch`) introduces, so it can only be applied on
top of it; it is order-independent with respect to the other five patches.
That dependency is declared in the patch's own header (`# Requires: <name>`)
and enforced here mechanically so it can never be silently lost: declared
requirements must name real patches, the requirement graph must stay acyclic,
every patch must stay wired into BOTH build consumers (the Linux Dockerfile
and the macOS script), and each consumer's application order must place a
required patch before the patch that requires it. The order/wiring checks
parse only ACTIVE instructions (Dockerfile COPY lines, macOS patch
assignments) — a patch named in a comment or prose is not applied and must
not count. The other five patches are independent — they declare no
requirements, and nothing here expects them to.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = REPO_ROOT / "scip-clang-patches"

MACRO_PATCH = "enclosing-range-macro-on-v0.4.0.patch"
ENCLOSING_RANGE_PATCH = "enclosing_range-on-v0.4.0.patch"

# Active-instruction matchers, one per build consumer. Each matches ONLY the
# line that actually applies a patch — a `COPY scip-clang-patches/<name>`
# Dockerfile instruction, a `<NAME>_PATCH="$(pwd)/scip-clang-patches/<name>"`
# assignment in the macOS script — never a comment or prose mention: a
# commented-out COPY must not satisfy wiring, and a patch named in a comment
# must not influence the parsed order.
_DOCKERFILE_PATCH_RE = re.compile(
    r"^[ \t]*COPY\s+scip-clang-patches/([A-Za-z0-9._-]+\.patch)", re.MULTILINE
)
_MACOS_PATCH_ASSIGN_RE = re.compile(
    r"^[ \t]*[A-Z_]*PATCH=.*scip-clang-patches/([A-Za-z0-9._-]+\.patch)", re.MULTILINE
)

CONSUMERS: dict[str, tuple[Path, re.Pattern[str]]] = {
    "docker/build-scip-clang-patched-linux/Dockerfile": (
        REPO_ROOT / "docker/build-scip-clang-patched-linux/Dockerfile",
        _DOCKERFILE_PATCH_RE,
    ),
    "scripts/build-scip-clang-patched-macos.sh": (
        REPO_ROOT / "scripts/build-scip-clang-patched-macos.sh",
        _MACOS_PATCH_ASSIGN_RE,
    ),
}

# The machine-readable dependency line inside a patch's `#` header. Keyword
# matched case-insensitively; the name is the rest of the line, so exactly one
# requirement per line (see parse_requires).
_REQUIRES_LINE_RE = re.compile(r"^#\s*Requires:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)


def patch_names() -> list[str]:
    """Every `*.patch` filename in scip-clang-patches/, sorted."""
    return sorted(p.name for p in PATCH_DIR.glob("*.patch"))


def patch_header(patch_name: str) -> str:
    """A patch's header: its leading `#` lines, before the first `diff --git`."""
    header_lines: list[str] = []
    for line in (PATCH_DIR / patch_name).read_text(encoding="utf-8").splitlines():
        if line.startswith("diff --git"):
            break
        if line.startswith("#"):
            header_lines.append(line)
    return "\n".join(header_lines)


def parse_requires(header: str) -> list[str]:
    """Patch filenames declared by `# Requires: <name>` lines in a patch header.

    The `Requires:` keyword is matched case-insensitively. One requirement per
    line by design: a patch with several dependencies writes one
    `# Requires:` line each, and a line listing several whitespace-separated
    names is rejected (matches nothing, no half-parsing) — silently taking the
    first name and dropping the rest would be exactly the lost-dependency
    failure this module exists to prevent.
    """
    return _REQUIRES_LINE_RE.findall(header)


def requirement_graph() -> dict[str, list[str]]:
    """Every patch mapped to the patches its header requires (empty list = independent)."""
    return {name: parse_requires(patch_header(name)) for name in patch_names()}


def parse_application_order(consumer_text: str, instruction_re: re.Pattern[str]) -> list[str]:
    """Patch filenames a build consumer applies, in application order.

    `instruction_re` must match ONLY the consumer's active application
    instruction — the `COPY scip-clang-patches/<name>` line for the Dockerfile,
    the `<NAME>_PATCH="$(pwd)/scip-clang-patches/<name>"` assignment for the
    macOS script — never a comment or prose. A patch mentioned only in a
    comment is not applied and counts for neither wiring nor order. The first
    appearance among active instructions is the application position.
    """
    first_seen: dict[str, int] = {}
    for match in instruction_re.finditer(consumer_text):
        first_seen.setdefault(match.group(1), len(first_seen))
    return list(first_seen)


def _order_violations(order: list[str], requires: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(required, dependent) pairs whose application order breaks a requirement.

    A pair is a violation when the dependent patch is applied before the patch
    it requires, or when the required patch is not applied by this consumer at
    all. A dependent patch absent from `order` is simply not applied here and
    imposes no order. Pure helper over in-memory inputs — the synthetic
    negative controls below cover it, so the live checks are known to bite.
    """
    position = {name: index for index, name in enumerate(order)}
    violations: list[tuple[str, str]] = []
    for dependent, required_names in requires.items():
        if dependent not in position:
            continue
        for required in required_names:
            if required not in position or position[required] > position[dependent]:
                violations.append((required, dependent))
    return violations


def _find_cycle(requires: dict[str, list[str]]) -> list[str] | None:
    """One dependency cycle as a closed node list, or None if the graph is acyclic."""
    _IN_STACK, _DONE = 1, 2
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = _IN_STACK
        path.append(node)
        for required in requires.get(node, ()):
            if state.get(required) == _IN_STACK:
                cycle_start = path.index(required)
                return [*path[cycle_start:], required]
            if state.get(required) != _DONE:
                found = visit(required)
                if found is not None:
                    return found
        path.pop()
        state[node] = _DONE
        return None

    for node in sorted(requires):
        if state.get(node) != _DONE:
            found = visit(node)
            if found is not None:
                return found
    return None


def test_requires_lines_name_existing_patches():
    """Every `# Requires: <name>` must name a patch that exists in the bundle."""
    graph = requirement_graph()
    for patch, required_names in graph.items():
        for required in required_names:
            assert required in patch_names(), (
                f"{patch} requires {required}, which does not exist in {PATCH_DIR}"
            )


def test_macro_patch_declares_its_dependency():
    """The seventh patch must keep declaring its #504 dependency in its header.

    Losing that `# Requires:` line is exactly the silent-loss failure this
    module exists to prevent, so the declaration itself is pinned, not just
    whatever it happens to point at.
    """
    assert parse_requires(patch_header(MACRO_PATCH)) == [ENCLOSING_RANGE_PATCH]


def test_requirement_graph_is_acyclic():
    """Declared requirements must not form a cycle (which could never be applied)."""
    graph = requirement_graph()
    cycle = _find_cycle(graph)
    assert cycle is None, f"requirement graph has a cycle: {' -> '.join(cycle)}"


def test_every_patch_is_wired_into_both_build_consumers():
    """Every patch must be applied by both build paths, not just shipped.

    Wiring means an ACTIVE application instruction, so a commented-out COPY
    (or a prose mention) does not satisfy it — see the negative controls below.
    """
    for consumer, (path, instruction_re) in CONSUMERS.items():
        applied = set(parse_application_order(path.read_text(encoding="utf-8"), instruction_re))
        missing = sorted(set(patch_names()) - applied)
        assert not missing, (
            f"{consumer} has no active instruction applying: {missing} — "
            "a patch shipped but never applied by this build path"
        )


def test_application_order_respects_requirements():
    """In each consumer, a required patch must be applied before its dependent."""
    graph = requirement_graph()
    for consumer, (path, instruction_re) in CONSUMERS.items():
        order = parse_application_order(path.read_text(encoding="utf-8"), instruction_re)
        violations = _order_violations(order, graph)
        assert violations == [], (
            f"{consumer} applies patches in an order that breaks requirements: "
            + ", ".join(f"{dep} before {req}" for req, dep in violations)
        )


def test_order_violations_catches_dependent_applied_before_its_requirement():
    """Negative control: the live order check must flag a genuinely bad order."""
    requires = {"b.patch": ["a.patch"]}
    assert _order_violations(["a.patch", "b.patch"], requires) == []
    assert _order_violations(["b.patch", "a.patch"], requires) == [("a.patch", "b.patch")]


def test_order_violations_catches_missing_requirement():
    """Negative control: not applying a required patch is a violation too."""
    assert _order_violations(["b.patch"], {"b.patch": ["a.patch"]}) == [("a.patch", "b.patch")]


def test_order_violations_accepts_independent_patches():
    """Negative control: independent patches in any order are never violations."""
    requires = {"a.patch": [], "b.patch": []}
    assert _order_violations(["b.patch", "a.patch"], requires) == []


def test_find_cycle_finds_a_two_patch_cycle():
    """Negative control: the cycle checker must flag an actual cycle."""
    requires = {"a.patch": ["b.patch"], "b.patch": ["a.patch"]}
    assert _find_cycle(requires) == ["a.patch", "b.patch", "a.patch"]


def test_find_cycle_accepts_acyclic_requirements():
    """Negative control: a diamond of requirements is acyclic."""
    requires = {
        "a.patch": [],
        "b.patch": ["a.patch"],
        "c.patch": ["a.patch"],
        "d.patch": ["b.patch", "c.patch"],
    }
    assert _find_cycle(requires) is None


def test_dockerfile_order_ignores_commented_copies():
    """Negative control: a COPY inside a comment must not register as applied.

    With raw-text scanning, a comment naming b.patch before a.patch's real
    line would invert the parsed order and could hide a real violation.
    """
    text = "\n".join(
        [
            "# COPY scip-clang-patches/b.patch /tmp/b.patch",
            "COPY scip-clang-patches/a.patch /tmp/a.patch",
            "COPY scip-clang-patches/b.patch /tmp/b.patch",
        ]
    )
    assert parse_application_order(text, _DOCKERFILE_PATCH_RE) == ["a.patch", "b.patch"]


def test_dockerfile_commented_out_copy_does_not_satisfy_wiring():
    """Negative control: a commented-out COPY means the patch is NOT wired in."""
    text = "# COPY scip-clang-patches/a.patch /tmp/a.patch"
    assert parse_application_order(text, _DOCKERFILE_PATCH_RE) == []


def test_macos_script_ignores_commented_assignments():
    """Negative control: same comment rule for the macOS script's assignments."""
    text = "\n".join(
        [
            '# KIND_PATCH="$(pwd)/scip-clang-patches/zzz.patch"',
            'RW_PATCH="$(pwd)/scip-clang-patches/yyy.patch"',
        ]
    )
    assert parse_application_order(text, _MACOS_PATCH_ASSIGN_RE) == ["yyy.patch"]


def test_parse_requires_matches_keyword_case_insensitively():
    """Negative control: the `Requires:` keyword is case-insensitive."""
    assert parse_requires("# requires: a.patch") == ["a.patch"]


def test_parse_requires_rejects_multi_name_lines():
    """One requirement per `# Requires:` line; a multi-name line is rejected."""
    assert parse_requires("# Requires: a.patch b.patch") == []
    assert parse_requires("# Requires: a.patch\n# Requires: b.patch") == [
        "a.patch",
        "b.patch",
    ]
