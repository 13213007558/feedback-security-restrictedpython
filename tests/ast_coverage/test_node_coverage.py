"""Node coverage tests for :class:`RestrictingNodeTransformer`.

These tests are the executable form of ``tests/ast_coverage/policy.py``:

* the interpreter's ``ast`` inventory is compared with the recorded
  decisions, so a new node without a strategy fails the build and points at
  the default-deny location;
* every recorded decision is proven with a small, natively valid program
  (a clean guarded compile for allowed nodes, a restricted-compilation error
  for denied and unhandled ones).
"""

import ast

import pytest

from RestrictedPython.transformer import RestrictingNodeTransformer
from tests.ast_coverage import policy


def test_every_interpreter_node_has_a_decision():
    """No node class may exist without an explicit recorded strategy."""
    report = policy.coverage_report()
    assert report.complete, report.missing_strategies()


def test_registry_has_no_duplicate_node_entries():
    """A node must have exactly one decision; duplicates would override."""
    entries = (
        [item.node for item in policy._DENIED]
        + [item.node for item in policy._UNHANDLED]
        + [item.node for item in policy._ALLOWED_GUARDED]
        + list(policy._ALLOWED_INHERENTLY_SAFE)
        + [item.node for item in policy._VERSION_POLICIES])
    assert len(entries) == len(set(entries))
    assert len(entries) == len(policy.REGISTRY)


def test_report_matches_transformer_visitor_methods():
    """The ``denied``/``unhandled`` split must reflect the real transformer.

    A node with a ``visit_`` method is never ``unhandled`` (generic_visit is
    only the fallback), and a node without one cannot be recorded as handled.
    """
    report = policy.coverage_report()
    visitors = {
        name[len('visit_'):]
        for name in dir(RestrictingNodeTransformer)
        if name.startswith('visit_')}

    assert not (set(report.unhandled) & visitors)
    for node in report.allowed + report.denied:
        assert node in visitors, (
            f'{node} is recorded as {node} but has no visit_ method')


def test_exempt_nodes_are_really_absent_from_parsed_trees():
    """Exemptions must not hide a node the parser can actually emit.

    A broad syntax corpus covers every grammar production the interpreter
    supports; none of the exempt classes may show up in the resulting trees,
    otherwise the exemption would be silently skipping a reachable node.
    """
    corpus = (
        'import os\n'
        'from os import path as os_path\n'
        'x: int = 1\n'
        'value = [i for i in range(3)]\n'
        'mapping = {k: v for k, v in items}\n'
        'values = {i for i in sequence}\n'
        'generator = (i for i in sequence)\n'
        'match command:\n'
        '    case 1 | 2:\n'
        '        pass\n'
        '    case [first, *rest]:\n'
        '        pass\n'
        '    case {"key": value}:\n'
        '        pass\n'
        '    case Point(x=0, y=y):\n'
        '        pass\n'
        '    case None as singleton:\n'
        '        pass\n'
        'def generic[T](*args, **kwargs) -> T:\n'
        '    return args[0]\n'
        'type Alias = int\n'
        'try:\n'
        '    pass\n'
        'except ValueError as error:\n'
        '    pass\n'
        'try:\n'
        '    pass\n'
        'except* ValueError as errors:\n'
        '    pass\n'
        'target = 1\n'
        'target += 1\n'
        'del target\n'
        'global counter\n'
        'identity = lambda value: value\n'
        'function(*arguments, **keywords)\n'
        'container[1:2], object_.attribute\n'
        'with manager() as (left, right):\n'
        '    pass\n'
        'for left, right in pairs:\n'
        '    pass\n'
        'while True:\n'
        '    break\n'
        'value = f"field={object_.attribute!r:>10}"\n'
        'raise ValueError()\n'
        'assert True\n'
        '@decorator\n'
        'class Sample(metaclass=Meta):\n'
        '    pass\n'
        'walrus = (item := 1)\n'
    )
    tree = ast.parse(corpus, type_comments=True)
    emitted = {type(node).__name__ for node in ast.walk(tree)}
    emitted |= {
        type(node).__name__ for node in tree.type_ignores}

    report = policy.coverage_report()
    leaked = emitted & set(report.exempt)
    assert not leaked, (
        f'exempt node(s) are parser-emitted and need a real NodePolicy: '
        f'{sorted(leaked)}')


def _evidence_cases():
    cases = []
    seen: dict[str, int] = {}
    for node_policy, evidences in policy.iter_active_policies():
        for evidence in evidences:
            count = seen.get(node_policy.node, 0)
            seen[node_policy.node] = count + 1
            cases.append(
                pytest.param(
                    node_policy, evidence,
                    id=f'{node_policy.node}-{count}'))
    return cases


@pytest.mark.parametrize('node_policy, evidence', _evidence_cases())
def test_node_evidence(node_policy, evidence):
    """Each recorded decision is demonstrated by a compilable program."""
    policy.verify_evidence(node_policy.node, node_policy, evidence)


def test_native_syntax_error_is_not_accepted_as_evidence():
    """A program the native parser rejects proves nothing about security."""
    broken = policy.NodeEvidence(source='def def def\n')
    node_policy = policy.NodePolicy(
        'Module', policy.Policy.ALLOWED, 'test', (broken,))
    with pytest.raises(policy.EvidenceFailure, match='not valid Python'):
        policy.verify_evidence('Module', node_policy, broken)


def test_allowed_evidence_without_guard_fails_when_guard_claimed():
    """A claimed guard that is not injected is a test failure, not a pass."""
    fake = policy.NodeEvidence(
        source='value = 1\n', guards=frozenset(('_getiter_',)))
    node_policy = policy.NodePolicy(
        'Module', policy.Policy.ALLOWED, 'test', (fake,))
    with pytest.raises(policy.EvidenceFailure, match='_getiter_'):
        policy.verify_evidence('Module', node_policy, fake)


def test_missing_strategy_report_names_node_and_default_deny_location():
    """When a node is unregistered the report must be actionable."""
    report = policy.CoverageReport(
        allowed=(), denied=(), unhandled=(), exempt=(),
        gaps=(policy.CoverageGap(
            node='FutureNode',
            reason='no NodePolicy registered',
            transformer_line=RestrictingNodeTransformer
            .generic_visit.__code__.co_firstlineno),))
    message = report.missing_strategies()
    assert 'FutureNode' in message
    assert 'no NodePolicy registered' in message
    assert 'generic_visit' in message
    assert policy.TRANSFORMER_PATH in message
    assert not report.complete


def test_an_interpreter_node_without_strategy_fails_the_build(monkeypatch):
    """A simulated interpreter upgrade must surface as a coverage gap.

    Unknown nodes are still rejected at runtime by generic_visit; the build
    failure makes the missing decision explicit instead of trusting silence.
    """

    class FutureStatement(ast.AST):
        _fields = ()

    monkeypatch.setattr(ast, 'FutureStatement', FutureStatement, raising=False)
    report = policy.coverage_report()
    assert not report.complete
    gap, = report.gaps
    assert gap.node == 'FutureStatement'
    assert gap.transformer_line == (
        RestrictingNodeTransformer.generic_visit.__code__.co_firstlineno)

    # The runtime safety net independently rejects the unknown node.
    transformer = RestrictingNodeTransformer()
    transformer.visit(FutureStatement())
    assert transformer.errors == [
        'Line None: FutureStatement statements are not allowed.']
    assert transformer.warnings == [
        'Line None: FutureStatement statement is not known to '
        'RestrictedPython']


def test_version_gated_node_is_exempt_before_its_minimum_version(
        monkeypatch):
    """A node that exists early but is gated to a future version is exempt."""

    class PreviewNode(ast.AST):
        _fields = ()

    monkeypatch.setattr(ast, 'PreviewNode', PreviewNode, raising=False)
    monkeypatch.setitem(
        policy.REGISTRY,
        'PreviewNode',
        policy.NodePolicy(
            node='PreviewNode',
            policy=policy.Policy.ALLOWED,
            reason='preview feature',
            condition=policy.VersionCondition(
                added_in=(3, 20),
                reason='not part of any released grammar yet')))

    report = policy.coverage_report(version=(3, 12))
    assert report.complete
    assert 'PreviewNode' in report.exempt

    # Once the minimum version is reached the same entry becomes a decision.
    future_report = policy.coverage_report(version=(3, 20))
    assert 'PreviewNode' in future_report.allowed
