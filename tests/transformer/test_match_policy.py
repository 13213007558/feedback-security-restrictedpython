import ast

import pytest

from RestrictedPython import RestrictedMatchNodeTransformer
from RestrictedPython import compile_restricted_exec
from RestrictedPython.Guards import guarded_match_pattern
from RestrictedPython.Guards import safe_builtins


def _match_globals(**overrides):
    glb = {'__builtins__': safe_builtins.copy()}
    glb.update({
        '_getiter_': iter,
        '_getitem_': lambda ob, key: ob[key],
        '_getattr_': getattr,
        '_match_pattern_': guarded_match_pattern,
    })
    glb.update(overrides)
    return glb


def _compile_match(source):
    result = compile_restricted_exec(
        source, policy=RestrictedMatchNodeTransformer)
    assert result.errors == ()
    assert result.code is not None
    return result.code


def _run(source, function_name, *args, **glb_overrides):
    glb = _match_globals(**glb_overrides)
    exec(_compile_match(source), glb)
    return glb[function_name](*args)


def _native(source, function_name, *args):
    ns = {}
    exec(source, ns)
    return ns[function_name](*args)


def test_default_policy_rejects_match():
    """The base policy keeps rejecting the match syntax."""
    result = compile_restricted_exec(
        'match x:\n    case 1:\n        pass\n')
    assert result.errors == (
        'Line 1: Match statements are not allowed.',)
    assert result.code is None


def test_match_policy_leaves_no_match_ast_behind():
    """No raw Match/pattern node survives the transformation."""
    tree = ast.parse(
        'match x:\n'
        '    case [a, {"k": b}]:\n'
        '        pass\n')
    RestrictedMatchNodeTransformer().visit(tree)
    leftovers = [
        node for node in ast.walk(tree)
        if type(node).__name__.startswith('Match')
        or isinstance(node, ast.match_case)]
    assert leftovers == []


def test_constant_and_singleton_patterns():
    source = """
def f(x):
    match x:
        case None:
            return 'none'
        case True:
            return 'true'
        case False:
            return 'false'
        case 1:
            return 'one'
        case -1:
            return 'neg'
        case 's':
            return 'str'
        case _:
            return 'other'
"""
    for value, expected in [
        (None, 'none'), (True, 'true'), (False, 'false'),
        (1, 'one'), (-1, 'neg'), ('s', 'str'), (2, 'other'),
    ]:
        assert _run(source, 'f', value) == expected
        assert _run(source, 'f', value) == _native(
            source, 'f', value)


def test_value_pattern_uses_equality_not_identity():
    source = """
def f(x):
    match x:
        case 1:
            return 'one'
        case _:
            return 'other'
"""
    # ``1.0 == 1`` so a value pattern matches even though the objects
    # differ; singleton patterns below use identity instead.
    assert _run(source, 'f', 1.0) == 'one'
    assert _native(source, 'f', 1.0) == 'one'


def test_capture_and_wildcard():
    source = """
def f(x):
    match x:
        case [first, _]:
            return first
        case other:
            return other
"""
    assert _run(source, 'f', [9, 10]) == 9
    assert _run(source, 'f', 42) == 42


def test_or_pattern_binds_consistent_names():
    source = """
def f(x):
    match x:
        case [a] | (a,):
            return a
        case _:
            return 'no'
"""
    assert _run(source, 'f', [7]) == 7
    assert _run(source, 'f', (8,)) == 8
    assert _run(source, 'f', 99) == 'no'


def test_or_pattern_requires_same_bindings():
    result = compile_restricted_exec(
        'match x:\n    case a | b:\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == (
        'Line 2: All alternatives of an OR pattern must bind the '
        'same set of names.',)


def test_sequence_patterns_with_star():
    source = """
def f(x):
    match x:
        case [a, *middle, last]:
            return (a, middle, last)
        case [*all_items]:
            return ('all', all_items)
        case _:
            return 'no'
"""
    cases = [
        ([1, 2, 3, 4], (1, [2, 3], 4)),
        ([1, 2], (1, [], 2)),
        ([9], ('all', [9])),
    ]
    for value, expected in cases:
        assert _run(source, 'f', value) == expected
        assert _native(source, 'f', value) == expected


def test_sequence_type_gate_matches_native():
    source = """
def f(x):
    match x:
        case [a]:
            return a
        case _:
            return 'no'
"""
    # Strings and ranges are sequences but do not match a sequence
    # pattern in native Python either.
    assert _run(source, 'f', 'ab') == 'no'
    assert _native(source, 'f', 'ab') == 'no'


def test_mapping_patterns_with_rest():
    source = """
def f(x):
    match x:
        case {'a': a, **rest}:
            return (a, rest)
        case {}:
            return 'empty'
        case _:
            return 'no'
"""
    assert _run(source, 'f', {'a': 1, 'b': 2}) == (1, {'b': 2})
    assert _run(source, 'f', {'a': 1}) == (1, {})
    # An empty mapping pattern matches any mapping, mirroring native.
    assert _run(source, 'f', {}) == 'empty'
    assert _run(source, 'f', {'b': 2}) == 'empty'
    assert _native(source, 'f', {'b': 2}) == 'empty'


def test_nested_patterns():
    source = """
def f(x):
    match x:
        case [[a, b], {'k': v}]:
            return (a, b, v)
        case _:
            return 'no'
"""
    assert _run(source, 'f', [[1, 2], {'k': 3}]) == (1, 2, 3)
    assert _run(source, 'f', [[1, 2], {}]) == 'no'


def test_guard_failure_keeps_bindings_like_native():
    # CPython commits the bindings as soon as a pattern matches and does
    # not roll them back when the guard is false.
    source = """
def f(x):
    match x:
        case [a] if False:
            return 'never'
        case _:
            return a
"""
    assert _run(source, 'f', [5]) == 5
    assert _native(source, 'f', [5]) == 5


def test_guard_failure_falls_through_to_next_case():
    source = """
def f(x):
    match x:
        case [a, b] if a == b:
            return ('eq', a)
        case [a, b]:
            return ('neq', a, b)
        case _:
            return 'no'
"""
    assert _run(source, 'f', [1, 1]) == ('eq', 1)
    assert _run(source, 'f', [1, 2]) == ('neq', 1, 2)
    assert _run(source, 'f', 99) == 'no'


def test_subject_is_evaluated_once():
    source = """
def f(data):
    calls = []
    def subject():
        calls.append(1)
        return data
    match subject():
        case [a]:
            return (len(calls), a)
        case _:
            return (len(calls), 'wild')
"""
    assert _run(source, 'f', [5]) == (1, 5)
    assert _run(source, 'f', 9) == (1, 'wild')


def test_break_and_continue_inside_match():
    source = """
def f(data):
    total = 0
    for item in data:
        match item:
            case ('add', value):
                total += value
            case ('stop',):
                break
            case _:
                continue
    return total
"""
    glb = _match_globals(_inplacevar_=lambda op, x, y: x + y)
    glb2 = glb
    exec(_compile_match(source), glb2)
    assert glb2['f'](
        [('add', 1), ('skip',), ('add', 2), ('stop',), ('add', 9)]
    ) == 3


def test_class_patterns_are_rejected():
    result = compile_restricted_exec(
        'match x:\n    case Point(a, b):\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == (
        'Line 2: Class patterns (case Class(...)) are not allowed in '
        'restricted pattern matching.',)


def test_nested_class_pattern_is_rejected():
    result = compile_restricted_exec(
        'match x:\n    case [Point(1)]:\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert 'Class patterns' in result.errors[0]


def test_invalid_capture_name_is_rejected():
    result = compile_restricted_exec(
        'match x:\n    case _secret:\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == (
        'Line 2: "_secret" is an invalid variable name because it '
        'starts with "_"',)


def test_duplicate_bindings_are_rejected():
    result = compile_restricted_exec(
        'match x:\n    case [a, a]:\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == (
        "Line 2: multiple assignments to name 'a' in pattern.",)


def test_duplicate_mapping_key_is_rejected():
    result = compile_restricted_exec(
        "match x:\n    case {'a': 1, 'a': 2}:\n        pass\n",
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == (
        "Line 2: mapping pattern checks duplicate key ('a').",)


def test_two_stars_are_rejected():
    result = compile_restricted_exec(
        'match x:\n    case [*a, *b]:\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == (
        'Line 2: A sequence pattern may contain at most one starred '
        'pattern.',)


def test_error_locations_point_to_the_pattern():
    result = compile_restricted_exec(
        'x = 1\ny = 2\nmatch z:\n    case _bad:\n        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors[0].startswith('Line 4:')


def test_getiter_guard_protects_sequence_matching():
    def deny_iter(ob):
        raise PermissionError('iteration denied')

    glb = _match_globals(_getiter_=deny_iter)
    exec(_compile_match(
        'def f(x):\n'
        '    match x:\n'
        '        case [a, b]:\n'
        '            return (a, b)\n'
        '        case _:\n'
        '            return "no"\n'), glb)
    with pytest.raises(PermissionError):
        glb['f']([1, 2])


def test_getitem_guard_protects_mapping_matching():
    def deny_item(ob, key):
        raise PermissionError(f'access to {key!r} denied')

    glb = _match_globals(_getitem_=deny_item)
    exec(_compile_match(
        'def f(x):\n'
        '    match x:\n'
        '        case {"secret": value}:\n'
        '            return value\n'
        '        case _:\n'
        '            return "no"\n'), glb)
    with pytest.raises(PermissionError):
        glb['f']({'secret': 1})


def test_getattr_guard_protects_value_pattern():
    class Config:
        mode = 'fast'

    def deny_attr(ob, name):
        raise PermissionError(name)

    source = (
        'def f(x, cfg):\n'
        '    match x:\n'
        '        case cfg.mode:\n'
        '            return "match"\n'
        '        case _:\n'
        '            return "no"\n')
    glb = _match_globals(_getattr_=deny_attr)
    exec(_compile_match(source), glb)
    with pytest.raises(PermissionError):
        glb['f']('fast', Config())


def test_guard_calls_are_recorded():
    iter_calls = []
    item_calls = []

    def rec_iter(ob):
        iter_calls.append(ob)
        return iter(ob)

    def rec_item(ob, key):
        item_calls.append((ob, key))
        return ob[key]

    glb = _match_globals(
        _getiter_=rec_iter, _getitem_=rec_item)
    exec(_compile_match(
        'def f(x):\n'
        '    match x:\n'
        '        case [a, *rest]:\n'
        '            return (a, rest)\n'
        '        case {"k": value, **extra}:\n'
        '            return (value, extra)\n'
        '        case _:\n'
        '            return "no"\n'), glb)
    assert glb['f']([1, 2, 3]) == (1, [2, 3])
    assert iter_calls == [[1, 2, 3]]
    assert glb['f']({'k': 9, 'z': 1}) == (9, {'z': 1})
    assert item_calls[-1] == ({'k': 9, 'z': 1}, 'k')


def test_as_pattern_binds_whole_and_inner():
    source = """
def f(x):
    match x:
        case [a, b] as whole:
            return (whole, a, b)
        case _:
            return 'no'
"""
    assert _run(source, 'f', [1, 2]) == ([1, 2], 1, 2)
    assert _run(source, 'f', 99) == 'no'


def test_leading_star_pattern():
    source = """
def f(x):
    match x:
        case [*head, last]:
            return (head, last)
        case _:
            return 'no'
"""
    assert _run(source, 'f', [1, 2, 3]) == ([1, 2], 3)
    assert _run(source, 'f', [9]) == ([], 9)
    assert _native(source, 'f', [1, 2, 3]) == ([1, 2], 3)


def test_empty_case_body_with_guard_compiles():
    # A guarded case without statements still has to produce valid
    # ``if`` AST (an explicit ``pass`` is inserted).
    code = _compile_match(
        'def f(x):\n'
        '    match x:\n'
        '        case a if a > 0:\n'
        '            pass\n'
        '        case _:\n'
        '            return "no"\n')
    glb = _match_globals()
    exec(code, glb)
    assert glb['f'](-1) == 'no'
    assert glb['f'](1) is None


def test_nested_match_is_flattened_into_statements():
    source = """
def f(x):
    match x:
        case [inner]:
            match inner:
                case {'k': value}:
                    return value
                case _:
                    return 'inner-wild'
        case _:
            return 'outer-wild'
"""
    assert _run(source, 'f', [{'k': 9}]) == 9
    assert _run(source, 'f', [5]) == 'inner-wild'
    assert _run(source, 'f', 1) == 'outer-wild'


def test_module_level_match_with_guard():
    source = (
        'match point:\n'
        '    case [x] if x > 0:\n'
        '        result = ("pos", x)\n'
        '    case [x]:\n'
        '        result = ("other", x)\n')
    glb = _match_globals(point=[5])
    exec(_compile_match(source), glb)
    assert glb['result'] == ('pos', 5)


def test_mapping_nested_value_mismatch_continues():
    source = """
def f(x):
    match x:
        case {'k': 1}:
            return 'one'
        case _:
            return 'other'
"""
    assert _run(source, 'f', {'k': 2}) == 'other'
    assert _run(source, 'f', {'k': 1}) == 'one'


def test_mapping_star_rest_iteration_guard():
    def deny_iter(ob):
        raise PermissionError

    glb = _match_globals(_getiter_=deny_iter)
    exec(_compile_match(
        'def f(x):\n'
        '    match x:\n'
        '        case {"a": a, **rest}:\n'
        '            return (a, rest)\n'), glb)
    with pytest.raises(PermissionError):
        glb['f']({'a': 1, 'b': 2})


def test_grouped_pattern_without_capture_name():
    # Parentheses around a pattern without an ``as`` name just group it.
    source = """
def f(x):
    match x:
        case (a):
            return a
"""
    assert _run(source, 'f', 42) == 42


def test_repeated_underscore_is_not_a_duplicate_binding():
    # The wildcard ``_`` may appear several times without triggering the
    # duplicate-binding error and never creates a binding.
    source = """
def f(x):
    match x:
        case [_, _]:
            return 'pair'
        case _:
            return 'other'
"""
    assert _run(source, 'f', [1, 2]) == 'pair'
    glb = _match_globals()
    exec(_compile_match(source), glb)
    glb['f']([1, 2])
    assert '_' not in glb


def test_guard_with_only_pass_inserts_valid_body():
    # A guarded case whose body is a single ``pass`` still has to emit
    # valid ``if`` AST; the transformer reuses that ``pass`` and must not
    # produce an empty body.
    tree = ast.parse(
        'def f(x):\n'
        '    match x:\n'
        '        case value if value > 0:\n'
        '            pass\n'
        '        case _:\n'
        '            return "no"\n')
    RestrictedMatchNodeTransformer().visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            assert node.body
    compile(tree, '<string>', 'exec')


def test_duplicate_check_skips_underscore_directly():
    # Exercise the wildcard skip inside the duplicate binding check for
    # a mapping value pattern as well.
    result = compile_restricted_exec(
        'match x:\n'
        '    case {"a": _, "b": _}:\n'
        '        pass\n',
        policy=RestrictedMatchNodeTransformer)
    assert result.errors == ()
