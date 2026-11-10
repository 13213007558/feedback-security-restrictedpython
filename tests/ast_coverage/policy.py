"""Maintainable AST node coverage for the restricting transformer.

Interpreter upgrades add nodes to :mod:`ast`.  RestrictedPython must never
become more permissive just because a ``visit_<Node>`` method was forgotten,
so this module answers three questions for *every* node class the running
interpreter exposes:

* ``allowed`` - an explicit visitor exists and compilable evidence shows the
  allowed path invoking an existing guard (or being inherently safe);
* ``denied`` - an explicit visitor rejects the node during restricted
  compilation, i. e. before any byte code is produced;
* ``unhandled`` - no visitor exists, so
  :meth:`RestrictingNodeTransformer.generic_visit` rejects the node by
  default.

The node list is taken from the interpreter at runtime instead of being a
hard-coded snapshot.  Version-specific nodes are registered with a version
condition and a reason; they are skipped (never silently counted as covered)
on interpreters that do not provide them.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import sys
import textwrap
from collections.abc import Iterable, Sequence

from RestrictedPython.transformer import RestrictingNodeTransformer


TRANSFORMER_PATH = (
    RestrictingNodeTransformer.generic_visit.__code__.co_filename)


class Policy(enum.Enum):
    """How RestrictedPython treats a concrete ``ast`` node class."""

    ALLOWED = 'allowed'
    DENIED = 'denied'
    UNHANDLED = 'unhandled'


@dataclasses.dataclass(frozen=True)
class VersionCondition:
    """Interpreter versions on which an entry applies.

    ``added_in`` / ``removed_in`` use ``(major, minor)`` tuples.  An entry
    without a condition applies to every supported interpreter.
    """

    added_in: tuple[int, int] | None = None
    removed_in: tuple[int, int] | None = None
    reason: str = ''

    def active(self, version: tuple[int, int] | None = None) -> bool:
        version = sys.version_info[:2] if version is None else version
        if self.added_in is not None and version < self.added_in:
            return False
        if self.removed_in is not None and version >= self.removed_in:
            return False
        return True


@dataclasses.dataclass(frozen=True)
class NodeEvidence:
    """A compilable program proving how a node is handled.

    ``source`` must be valid Python that parses natively on every
    interpreter the evidence is active for.  A program the native parser
    rejects is not evidence of a security decision; those cases are listed
    in :data:`NOT_PARSER_EMITTED` instead.

    By default the whole parsed program is transformed.  When
    ``visit_node`` names an AST class, the node of that class is located in
    the parsed tree and visited directly instead: this proves the policy of
    sub-nodes (e. g. match patterns) whose enclosing statement is rejected
    before the transformer would descend to them.
    """

    source: str
    # Names of guards the allowed path must inject, e. g. ``_getiter_``.
    guards: frozenset[str] = frozenset()
    # Substring expected in the restricted-compilation error messages.
    error: str | None = None
    condition: VersionCondition | None = None
    reason: str = ''
    # When set, visit only the first node of this class found in the tree.
    visit_node: str | None = None
    # Parse mode; eval/single produce Expression/Interactive root nodes.
    mode: str = 'exec'

    @property
    def dedented_source(self) -> str:
        return textwrap.dedent(self.source)


@dataclasses.dataclass(frozen=True)
class NodePolicy:
    """The recorded decision for one concrete ``ast`` node class."""

    node: str
    policy: Policy
    reason: str
    evidence: tuple[NodeEvidence, ...] = ()
    condition: VersionCondition | None = None


# Nodes the parser never instantiates.  Abstract ASDL base classes have no
# grammar production; compatibility stubs and deprecated constant aliases are
# either unused by the parser or turn into ``Constant``.  Keeping the reason
# next to the name avoids a permanent snapshot of *concrete* nodes while still
# explaining why the class is exempt from the compiled-program evidence.
NOT_PARSER_EMITTED: dict[str, VersionCondition] = {
    # Abstract ASDL base classes - only concrete subclasses appear in trees.
    'AST': VersionCondition(
        reason='root class, never instantiated by parse()'),
    'expr': VersionCondition(reason='abstract ASDL base class'),
    'stmt': VersionCondition(reason='abstract ASDL base class'),
    'mod': VersionCondition(reason='abstract ASDL base class'),
    'operator': VersionCondition(reason='abstract ASDL base class'),
    'boolop': VersionCondition(reason='abstract ASDL base class'),
    'unaryop': VersionCondition(reason='abstract ASDL base class'),
    'cmpop': VersionCondition(reason='abstract ASDL base class'),
    'expr_context': VersionCondition(
        reason='abstract ASDL base class; Load/Store/Del are concrete'),
    'slice': VersionCondition(reason='abstract ASDL base class'),
    'pattern': VersionCondition(reason='abstract ASDL base class'),
    'type_param': VersionCondition(reason='abstract ASDL base class'),
    'type_ignore': VersionCondition(reason='abstract ASDL base class'),
    'excepthandler': VersionCondition(
        reason='deprecated alias of the concrete ExceptHandler class'),
    # The parser emits Load/Store/Del; augmented contexts are reserved in the
    # ASDL but never produced (see RestrictingNodeTransformer.visit_Subscript).
    'AugLoad': VersionCondition(
        reason='reserved expr_context, never produced by the CPython parser'),
    'AugStore': VersionCondition(
        reason='reserved expr_context, never produced by the CPython parser'),
    # Pre-3.9 grammar nodes kept as compatibility classes until 3.13.
    'Index': VersionCondition(
        removed_in=(3, 13),
        reason='removed from the grammar in Python 3.9; the parser emits '
               'the expression directly, and the stub class is gone in 3.13'),
    'ExtSlice': VersionCondition(
        removed_in=(3, 13),
        reason='removed from the grammar in Python 3.9; the parser emits a '
               'Tuple of slices, and the stub class is gone in 3.13'),
    'Suite': VersionCondition(
        removed_in=(3, 13),
        reason='old single-mode node; Interactive carries statements '
               'directly and the stub class is gone in 3.13'),
    'FunctionType': VersionCondition(
        reason='only produced by the removed func_type parse mode; '
               'compile_restricted only uses exec/eval/single'),
    # Deprecated ``Constant`` aliases; constructing one yields ``Constant``.
    # The classes emit DeprecationWarning on 3.12/3.13 and are gone in 3.14.
    'Num': VersionCondition(
        removed_in=(3, 14),
        reason='deprecated alias; construction returns a Constant node'),
    'Str': VersionCondition(
        removed_in=(3, 14),
        reason='deprecated alias; construction returns a Constant node'),
    'Bytes': VersionCondition(
        removed_in=(3, 14),
        reason='deprecated alias; construction returns a Constant node'),
    'NameConstant': VersionCondition(
        removed_in=(3, 14),
        reason='deprecated alias; construction returns a Constant node'),
    'Ellipsis': VersionCondition(
        removed_in=(3, 14),
        reason='deprecated alias; construction returns a Constant node'),
    '_ast_Ellipsis': VersionCondition(
        removed_in=(3, 14),
        reason='private deprecated alias of Ellipsis; construction '
               'returns a Constant node'),
    # Empty forward-compatibility stub; arguments only become Param nodes in
    # 3.14.  On 3.12/3.13 the class exists but has no grammar production.
    'Param': VersionCondition(
        added_in=(3, 12), removed_in=(3, 14),
        reason='forward-compatibility stub until Python 3.14; the parser '
               'emits arg nodes on older versions'),
    # Module.type_ignores is only populated when parsing with
    # ``type_comments=True``, which compile_restricted never does, so the
    # node cannot occur in a tree the transformer receives.
    'TypeIgnore': VersionCondition(
        reason='only emitted with ast.parse(..., type_comments=True), a '
               'flag compile_restricted never passes'),
}


def _ev(source: str,
        guards: Sequence[str] = (),
        error: str | None = None,
        condition: VersionCondition | None = None,
        reason: str = '',
        visit_node: str | None = None,
        mode: str = 'exec') -> NodeEvidence:
    return NodeEvidence(
        source=source,
        guards=frozenset(guards),
        error=error,
        condition=condition,
        reason=reason,
        visit_node=visit_node,
        mode=mode,
    )


def _allowed(node: str, reason: str,
             evidence: Sequence[NodeEvidence]) -> NodePolicy:
    return NodePolicy(node, Policy.ALLOWED, reason, tuple(evidence))


def _denied(node: str, reason: str,
            evidence: Sequence[NodeEvidence]) -> NodePolicy:
    return NodePolicy(node, Policy.DENIED, reason, tuple(evidence))


def _unhandled(node: str, reason: str,
               evidence: Sequence[NodeEvidence]) -> NodePolicy:
    return NodePolicy(node, Policy.UNHANDLED, reason, tuple(evidence))


# Explicit rejections: a visitor exists and records an error, so restricted
# compilation fails and no byte code is returned.
_DENIED: tuple[NodePolicy, ...] = (
    _denied(
        'Nonlocal',
        'closure mutation would let restricted code rebind enclosing '
        'scopes; visit_Nonlocal calls not_allowed()',
        [_ev(
            'def outer():\n'
            '    message = "x"\n'
            '    def inner():\n'
            '        nonlocal message\n',
            error='Nonlocal statements are not allowed.')],
    ),
    _denied(
        'TryStar',
        'exception groups can escape the sandbox through the except* '
        'machinery; visit_TryStar calls not_allowed()',
        [_ev(
            'try:\n'
            '    pass\n'
            'except* ValueError as errors:\n'
            '    pass\n',
            error='TryStar statements are not allowed.')],
    ),
    _denied(
        'AsyncFunctionDef',
        'async syntax is denied; visit_AsyncFunctionDef calls '
        'not_allowed()',
        [_ev(
            'async def coroutine():\n'
            '    pass\n',
            error='AsyncFunctionDef statements are not allowed.')],
    ),
    _denied(
        'Await',
        'async syntax is denied; visit_Await calls not_allowed(). The '
        'node is visited directly so the proof is not masked by the '
        'AsyncFunctionDef rejection',
        [_ev(
            'async def coroutine():\n'
            '    await suspend()\n',
            error='Await statements are not allowed.',
            visit_node='Await')],
    ),
    _denied(
        'AsyncFor',
        'async syntax is denied; visit_AsyncFor calls not_allowed(). The '
        'node is visited directly so the proof is not masked by the '
        'AsyncFunctionDef rejection',
        [_ev(
            'async def coroutine():\n'
            '    async for item in stream:\n'
            '        pass\n',
            error='AsyncFor statements are not allowed.',
            visit_node='AsyncFor')],
    ),
    _denied(
        'AsyncWith',
        'async syntax is denied; visit_AsyncWith calls not_allowed(). The '
        'node is visited directly so the proof is not masked by the '
        'AsyncFunctionDef rejection',
        [_ev(
            'async def coroutine():\n'
            '    async with manager() as resource:\n'
            '        pass\n',
            error='AsyncWith statements are not allowed.',
            visit_node='AsyncWith')],
    ),
)


# Nodes without a visitor: generic_visit() warns and not_allowed() errors, so
# they are rejected at the restricted compilation stage by default.  The
# snippets are valid Python (they parse natively) which proves the rejection
# is RestrictedPython's decision rather than a syntax error.
_UNHANDLED: tuple[NodePolicy, ...] = (
    _unhandled(
        'AnnAssign',
        'annotated assignment has not been reviewed; rejected by '
        'generic_visit() until a policy is added',
        [_ev('count: int = 1\n',
             error='AnnAssign statements are not allowed.')],
    ),
    _unhandled(
        'Match',
        'structural pattern matching has not been reviewed; rejected by '
        'generic_visit(), which also covers every Match* sub-pattern',
        [_ev(
            'match command:\n'
            '    case ["go", direction]:\n'
            '        pass\n',
            error='Match statements are not allowed.')],
    ),
    _unhandled(
        'MatchValue',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case 1:\n'
            '        pass\n',
            error='MatchValue statements are not allowed.',
            visit_node='MatchValue')],
    ),
    _unhandled(
        'MatchSequence',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case [first, second]:\n'
            '        pass\n',
            error='MatchSequence statements are not allowed.',
            visit_node='MatchSequence')],
    ),
    _unhandled(
        'MatchMapping',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case {"key": value}:\n'
            '        pass\n',
            error='MatchMapping statements are not allowed.',
            visit_node='MatchMapping')],
    ),
    _unhandled(
        'MatchClass',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match event:\n'
            '    case Point(x=0, y=y):\n'
            '        pass\n',
            error='MatchClass statements are not allowed.',
            visit_node='MatchClass')],
    ),
    _unhandled(
        'MatchAs',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case [first, *rest] as whole:\n'
            '        pass\n',
            error='MatchAs statements are not allowed.',
            visit_node='MatchAs')],
    ),
    _unhandled(
        'MatchStar',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case [first, *rest]:\n'
            '        pass\n',
            error='MatchStar statements are not allowed.',
            visit_node='MatchStar')],
    ),
    _unhandled(
        'MatchOr',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case 1 | 2:\n'
            '        pass\n',
            error='MatchOr statements are not allowed.',
            visit_node='MatchOr')],
    ),
    _unhandled(
        'MatchSingleton',
        'sub-pattern of the unreviewed Match statement; visiting the '
        'pattern directly proves generic_visit() rejects it on its own',
        [_ev(
            'match command:\n'
            '    case None:\n'
            '        pass\n',
            error='MatchSingleton statements are not allowed.',
            visit_node='MatchSingleton')],
    ),
    _unhandled(
        'match_case',
        'case clause container of the unreviewed Match statement; '
        'visiting the clause directly proves generic_visit() rejects it',
        [_ev(
            'match command:\n'
            '    case other:\n'
            '        pass\n',
            error='match_case statements are not allowed.',
            visit_node='match_case')],
    ),
    _unhandled(
        'TypeAlias',
        'PEP 695 type statements have not been reviewed; rejected by '
        'generic_visit()',
        [_ev('type Point = tuple[float, float]\n',
             error='TypeAlias statements are not allowed.')],
    ),
    _unhandled(
        'TypeVar',
        'PEP 695 type parameters have not been reviewed; rejected by '
        'generic_visit()',
        [_ev(
            'def identity[T](value: T) -> T:\n'
            '    return value\n',
            error='TypeVar statements are not allowed.')],
    ),
    _unhandled(
        'TypeVarTuple',
        'PEP 695 type parameters have not been reviewed; rejected by '
        'generic_visit()',
        [_ev(
            'def flattened[*Types](*values: *Types):\n'
            '    return values\n',
            error='TypeVarTuple statements are not allowed.')],
    ),
    _unhandled(
        'ParamSpec',
        'PEP 695 type parameters have not been reviewed; rejected by '
        'generic_visit()',
        [_ev(
            'def wrap[**Params](func: '
        'typing.Callable[Params, object]):\n'
            '    return func\n',
            error='ParamSpec statements are not allowed.')],
    ),
)


# Allowed nodes whose visitor rewrites the tree through an existing guard.
# The evidence asserts both a clean restricted compile and that the named
# guard name actually appears in the transformed AST, so "allowed" never
# silently means "unchecked".
_ALLOWED_GUARDED: tuple[NodePolicy, ...] = (
    _allowed(
        'For',
        'iteration is wrapped in _getiter_ via guard_iter()',
        [_ev('for item in sequence:\n    pass\n',
             guards=('_getiter_',))],
    ),
    _allowed(
        'comprehension',
        'every comprehension generator is wrapped in _getiter_ via '
        'guard_iter()',
        [_ev('result = [item for item in sequence]\n',
             guards=('_getiter_',))],
    ),
    _allowed(
        'ListComp',
        'allowed; the comprehension node it contains is guard_iter() '
        'protected',
        [_ev('result = [item for item in sequence]\n',
             guards=('_getiter_',))],
    ),
    _allowed(
        'SetComp',
        'allowed; the comprehension node it contains is guard_iter() '
        'protected',
        [_ev('result = {item for item in sequence}\n',
             guards=('_getiter_',))],
    ),
    _allowed(
        'DictComp',
        'allowed; the comprehension node it contains is guard_iter() '
        'protected',
        [_ev('result = {item: item for item in sequence}\n',
             guards=('_getiter_',))],
    ),
    _allowed(
        'GeneratorExp',
        'generators are allowed; iteration is wrapped in _getiter_ and '
        'yielding itself needs no runtime guard',
        [_ev('result = list(item for item in sequence)\n',
             guards=('_getiter_',))],
    ),
    _allowed(
        'Yield',
        'generators are allowed; a yield inside a guarded function keeps '
        'the enclosing iteration guards',
        [_ev(
            'def generate():\n'
            '    for item in sequence:\n'
            '        yield item\n',
            guards=('_getiter_',))],
    ),
    _allowed(
        'YieldFrom',
        'yield from is allowed; the delegated iterator is still created '
        'inside guarded code',
        [_ev(
            'def generate():\n'
            '    yield from sequence\n',
            guards=())],
    ),
    _allowed(
        'Attribute',
        'attribute access is rewritten to _getattr_; stores/dels go '
        'through _write_',
        [_ev('value = obj.attribute\n', guards=('_getattr_',)),
         _ev('obj.attribute = value\n', guards=('_write_',))],
    ),
    _allowed(
        'Subscript',
        'loads are rewritten to _getitem_; stores/dels go through _write_',
        [_ev('value = container[0]\n', guards=('_getitem_',)),
         _ev('container[0] = value\n', guards=('_write_',))],
    ),
    _allowed(
        'Call',
        'star/keyword call forms are collapsed through _apply_; plain '
        'calls keep the usual name checks',
        [_ev('result = function(*arguments, **keywords)\n',
             guards=('_apply_',))],
    ),
    _allowed(
        'AugAssign',
        'name targets are rewritten through _inplacevar_; attribute and '
        'subscript targets are errors',
        [_ev('total += 1\n', guards=('_inplacevar_',))],
    ),
    _allowed(
        'Assign',
        'tuple unpacking is protected by _unpack_sequence_ with _getiter_',
        [_ev('first, second = pair\n',
             guards=('_unpack_sequence_', '_getiter_'))],
    ),
    _allowed(
        'With',
        'tuple targets in with statements are unpacked through '
        '_unpack_sequence_',
        [_ev(
        'with manager() as (first, second):\n'
            '    pass\n',
            guards=('_unpack_sequence_', '_getiter_'))],
    ),
    _allowed(
        'JoinedStr',
        'f-strings are allowed; each embedded expression is transformed, '
        'so attribute access still routes through _getattr_',
        [_ev("value = f'{obj.attribute}'\n",
             guards=('_getattr_',))],
    ),
    _allowed(
        'FormattedValue',
        'the replacement field of an f-string; the embedded expression is '
        'transformed like any other',
        [_ev("value = f'{obj.attribute}'\n",
             guards=('_getattr_',))],
    ),
    _allowed(
        'Name',
        'name loads are recorded in used_names and checked for protected '
        'prefixes; print/printed are rewritten',
        [_ev('value = printed\n', guards=('_print',))],
    ),
    _allowed(
        'Global',
        'global declarations are allowed; the referenced names still pass '
        'check_name on use',
        [_ev(
            'def function():\n'
            '    global counter\n'
            '    counter = 1\n',
            guards=())],
    ),
    _allowed(
        'FunctionDef',
        'function definitions are allowed after name and argument checks; '
        'the body is transformed normally',
        [_ev(
            'def function(items):\n'
            '    return [item for item in items]\n',
            guards=('_getiter_',))],
    ),
    _allowed(
        'Lambda',
        'lambdas are allowed after the same argument-name checks as def',
        [_ev('double = lambda value: value * 2\n', guards=())],
    ),
    _allowed(
        'ExceptHandler',
        'exception handlers are allowed; the bound name goes through '
        'check_name',
        [_ev(
            'try:\n'
            '    pass\n'
            'except ValueError as error:\n'
            '    pass\n',
            guards=())],
    ),
)


# Allowed nodes that are structural or purely syntactic: they introduce no
# new value, name, attribute or iteration, so there is no guard to invoke.
# The evidence still has to compile cleanly through the restricted compiler.
_ALLOWED_INHERENTLY_SAFE: dict[str, NodeEvidence] = {
    'Module': _ev('value = 1\n'),
    'Interactive': _ev('value = 1\n', mode='single'),
    'Expression': _ev('1 + 1\n', mode='eval'),
    'Constant': _ev('value = "text"\n'),
    'List': _ev('value = [1, 2]\n'),
    'Tuple': _ev('value = (1, 2)\n'),
    'Set': _ev('value = {1, 2}\n'),
    'Dict': _ev('value = {"key": "value"}\n'),
    'Expr': _ev('1 + 1\n'),
    'UnaryOp': _ev('value = -number\n'),
    'UAdd': _ev('value = +number\n'),
    'USub': _ev('value = -number\n'),
    'Not': _ev('value = not flag\n'),
    'Invert': _ev('value = ~number\n'),
    'BinOp': _ev('value = left + right\n'),
    'Add': _ev('value = left + right\n'),
    'Sub': _ev('value = left - right\n'),
    'Mult': _ev('value = left * right\n'),
    'Div': _ev('value = left / right\n'),
    'FloorDiv': _ev('value = left // right\n'),
    'Mod': _ev('value = left % right\n'),
    'Pow': _ev('value = left ** right\n'),
    'LShift': _ev('value = left << right\n'),
    'RShift': _ev('value = left >> right\n'),
    'BitOr': _ev('value = left | right\n'),
    'BitXor': _ev('value = left ^ right\n'),
    'BitAnd': _ev('value = left & right\n'),
    'MatMult': _ev('value = left @ right\n'),
    'BoolOp': _ev('value = left and right\n'),
    'And': _ev('value = left and right\n'),
    'Or': _ev('value = left or right\n'),
    'Compare': _ev('value = left < right\n'),
    'Eq': _ev('value = left == right\n'),
    'NotEq': _ev('value = left != right\n'),
    'Lt': _ev('value = left < right\n'),
    'LtE': _ev('value = left <= right\n'),
    'Gt': _ev('value = left > right\n'),
    'GtE': _ev('value = left >= right\n'),
    'Is': _ev('value = left is right\n'),
    'IsNot': _ev('value = left is not right\n'),
    'In': _ev('value = item in container\n'),
    'NotIn': _ev('value = item not in container\n'),
    'IfExp': _ev('value = item if flag else other\n'),
    'keyword': _ev('function(keyword=value)\n'),
    'Slice': _ev('value = container[1:3]\n'),
    'Starred': _ev('value = [*items, last]\n'),
    'Load': _ev('value = item\n'),
    'Store': _ev('item = 1\n'),
    'Del': _ev('del item\n'),
    'If': _ev('if flag:\n    pass\n'),
    'While': _ev('while flag:\n    break\n'),
    'Break': _ev('while flag:\n    break\n'),
    'Continue': _ev('while flag:\n    continue\n'),
    'Try': _ev(
        'try:\n'
        '    pass\n'
        'except ValueError:\n'
        '    pass\n'
        'finally:\n'
        '    pass\n'),
    'Raise': _ev('raise ValueError()\n'),
    'Assert': _ev('assert flag\n'),
    'Delete': _ev('del item\n'),
    'Pass': _ev('pass\n'),
    'Import': _ev('import os\n'),
    'ImportFrom': _ev('from os import path\n'),
    'alias': _ev('from os import path as os_path\n'),
    'withitem': _ev('with manager() as resource:\n    pass\n'),
    'arguments': _ev('def function(argument):\n    return argument\n'),
    'arg': _ev('def function(argument):\n    return argument\n'),
    'Return': _ev('def function():\n    return 1\n'),
    'ClassDef': _ev('class Sample:\n    pass\n'),
    'NamedExpr': _ev('value = (item := 1)\n'),
}


# Version-gated nodes.  The condition keeps the entry out of the active set on
# interpreters that lack the node, while preserving the reason for the policy.
_VERSION_POLICIES: tuple[NodePolicy, ...] = (
    NodePolicy(
        'TemplateStr', Policy.ALLOWED,
        't-strings only produce data; rendering logic is itself restricted '
        'code and is transformed normally',
        (
            _ev(
                'value = t"{item}"\n',
                guards=(),
                condition=VersionCondition(
                    added_in=(3, 14),
                    reason='t-strings were added in Python 3.14'),
            ),
        ),
        VersionCondition(added_in=(3, 14),
                         reason='t-strings were added in Python 3.14'),
    ),
    NodePolicy(
        'Interpolation', Policy.ALLOWED,
        'interpolations are the replacement fields of t-strings; the '
        'embedded expression is transformed normally',
        (
            _ev(
                'value = t"{obj.attribute}"\n',
                guards=('_getattr_',),
                condition=VersionCondition(
                    added_in=(3, 14),
                    reason='t-strings were added in Python 3.14'),
            ),
        ),
        VersionCondition(added_in=(3, 14),
                         reason='t-strings were added in Python 3.14'),
    ),
)


def interpreter_ast_nodes() -> dict[str, type[ast.AST]]:
    """AST node classes exposed by the running interpreter, keyed by name.

    The keys are the attribute names on :mod:`ast`, including deprecated
    alias names such as ``_ast_Ellipsis``.  Aliases that resolve to the same
    class object as a canonical name are still listed separately so the
    coverage check can explain each exposed name.
    """
    return {
        name: obj
        for name, obj in vars(ast).items()
        if isinstance(obj, type) and issubclass(obj, ast.AST)
    }


def _build_registry() -> dict[str, NodePolicy]:
    registry: dict[str, NodePolicy] = {
        policy.node: policy
        for policy in (_DENIED + _UNHANDLED + _ALLOWED_GUARDED)
    }
    for node, evidence in _ALLOWED_INHERENTLY_SAFE.items():
        registry[node] = _allowed(
            node,
            'structural/syntactic node that performs no name, attribute, '
            'call or iteration access of its own',
            (evidence,),
        )
    for policy in _VERSION_POLICIES:
        registry[policy.node] = policy
    return registry


REGISTRY: dict[str, NodePolicy] = _build_registry()


@dataclasses.dataclass(frozen=True)
class CoverageGap:
    """A node the running interpreter exposes without an active decision."""

    node: str
    reason: str
    transformer_line: int


@dataclasses.dataclass(frozen=True)
class CoverageReport:
    """Result of comparing the registry against the interpreter inventory."""

    allowed: tuple[str, ...]
    denied: tuple[str, ...]
    unhandled: tuple[str, ...]
    exempt: tuple[str, ...]
    gaps: tuple[CoverageGap, ...]

    @property
    def complete(self) -> bool:
        return not self.gaps

    def missing_strategies(self) -> str:
        lines = [
        'RestrictedPython AST node coverage is incomplete for '
            f'Python {sys.version_info.major}.{sys.version_info.minor}:',
        ]
        for gap in self.gaps:
            lines.append(
                f'  - {gap.node}: {gap.reason}; default rejection happens in '
                f'{TRANSFORMER_PATH}:{gap.transformer_line} '
                '(RestrictingNodeTransformer.generic_visit). Add a '
                'NodePolicy with compilable evidence to '
                'tests/ast_coverage/policy.py.')
        return '\n'.join(lines)


def _generic_visit_line() -> int:
    return RestrictingNodeTransformer.generic_visit.__code__.co_firstlineno


def coverage_report(
        version: tuple[int, int] | None = None) -> CoverageReport:
    """Diff the interpreter's AST classes against the recorded decisions."""

    version = sys.version_info[:2] if version is None else version
    nodes = interpreter_ast_nodes()
    generic_visit_line = _generic_visit_line()

    allowed: list[str] = []
    denied: list[str] = []
    unhandled: list[str] = []
    exempt: list[str] = []
    gaps: list[CoverageGap] = []

    for name in sorted(nodes):
        exemption = NOT_PARSER_EMITTED.get(name)
        if exemption is not None and exemption.active(version):
            exempt.append(name)
            continue

        decision = REGISTRY.get(name)
        if decision is None:
            gaps.append(CoverageGap(
                node=name,
                reason='no NodePolicy registered',
                transformer_line=generic_visit_line))
            continue
        if decision.condition is not None and not decision.condition.active(
                version):
            exempt.append(name)
            continue

        bucket = {
            Policy.ALLOWED: allowed,
            Policy.DENIED: denied,
            Policy.UNHANDLED: unhandled,
        }[decision.policy]
        bucket.append(name)

    return CoverageReport(
        tuple(allowed), tuple(denied), tuple(unhandled),
        tuple(exempt), tuple(gaps))


def active_evidence(
        policy: NodePolicy,
        version: tuple[int, int] | None = None
        ) -> tuple[NodeEvidence, ...]:
    """Evidence programs applicable on the running interpreter."""
    version = sys.version_info[:2] if version is None else version
    return tuple(
        evidence for evidence in policy.evidence
        if evidence.condition is None or evidence.condition.active(version))


@dataclasses.dataclass(frozen=True)
class EvidenceFailure(Exception):
    """A registered evidence program does not prove its claim."""

    node: str
    detail: str

    def __str__(self) -> str:
        return f'evidence for {self.node}: {self.detail}'


def _guard_names(tree: ast.AST) -> frozenset[str]:
    # Guard helpers are the underscore-wrapped names injected by the
    # transformer.  The ``printed`` rewrite additionally emits a call to the
    # injected ``_print`` closure (no trailing underscore), include it too.
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    return frozenset(
        name for name in names
        if (name.startswith('_') and name.endswith('_')) or name == '_print')


def _transform(source: str,
               visit_node: str | None = None,
               mode: str = 'exec'
               ) -> tuple[ast.AST, RestrictingNodeTransformer]:
    """Parse with the native parser then run the restricting transformer.

    With ``visit_node`` set, only the first node of that class is visited,
    which isolates the policy of a sub-node whose parent is rejected first.
    """
    tree = ast.parse(source, mode=mode)
    transformer = RestrictingNodeTransformer()
    if visit_node is None:
        transformer.visit(tree)
        return tree, transformer

    target_class = getattr(ast, visit_node)
    target = next(
        (node for node in ast.walk(tree)
         if isinstance(node, target_class)),
        None)
    if target is None:
        raise EvidenceFailure(
            visit_node,
            f'no {visit_node} node found in program {source!r}')
    transformer.visit(target)
    return target, transformer


def verify_evidence(node: str, policy: NodePolicy,
                    evidence: NodeEvidence) -> None:
    """Compile the program and check it demonstrates the recorded policy.

    The program is first parsed without restrictions: a native syntax error
    would mean the snippet proves nothing about RestrictedPython.
    """
    source = evidence.dedented_source
    try:
        ast.parse(source, mode=evidence.mode)
    except SyntaxError as exc:
        raise EvidenceFailure(
            node,
            f'program is not valid Python, so it cannot be security '
            f'evidence: {exc.msg!r}') from exc

    tree, transformer = _transform(
        source, evidence.visit_node, evidence.mode)
    has_errors = bool(transformer.errors)

    if policy.policy is Policy.ALLOWED:
        if has_errors:
            raise EvidenceFailure(
                node,
                f'expected a clean compile but got {transformer.errors!r}')
        injected = _guard_names(tree)
        missing = evidence.guards - injected
        if missing:
            raise EvidenceFailure(
                node,
                f'allowed path must invoke guard(s) {sorted(missing)}, '
                f'transformed tree only contains {sorted(injected)!r}')
        if evidence.error is not None:
            raise EvidenceFailure(
                node, 'allowed evidence must not declare an expected error')
        # Only whole programs can be compiled to byte code; an isolated
        # sub-node is proven by the clean transformation above.
        if evidence.visit_node is None:
            compile(tree, '<coverage>', evidence.mode)
        return

    expected_error = evidence.error
    if expected_error is None:
        raise EvidenceFailure(
            node,
            f'{policy.policy.value} evidence must state the expected error')
    if not any(expected_error in message
               for message in transformer.errors):
        raise EvidenceFailure(
            node,
            f'expected an error containing {expected_error!r}, got '
            f'{transformer.errors!r}')
    # Reaching this point means the transformer recorded an error, which
    # prevents byte code generation in compile._compile_restricted_mode.
    if policy.policy is Policy.DENIED:
        return
    if policy.policy is Policy.UNHANDLED:
        unknown_warning = (
            f'{node} statement is not known to RestrictedPython')
        if not any(unknown_warning in warning
                   for warning in transformer.warnings):
            raise EvidenceFailure(
                node,
                'unhandled node must be caught by generic_visit; expected '
                f'warning {unknown_warning!r}, got '
                f'{transformer.warnings!r}')


def iter_active_policies(
        version: tuple[int, int] | None = None
) -> Iterable[tuple[NodePolicy, tuple[NodeEvidence, ...]]]:
    """All registry decisions with evidence applicable to this interpreter."""
    version = sys.version_info[:2] if version is None else version
    for name in sorted(REGISTRY):
        policy = REGISTRY[name]
        if policy.condition is not None and not policy.condition.active(
                version):
            continue
        evidence = active_evidence(policy, version)
        if evidence:
            yield policy, evidence
