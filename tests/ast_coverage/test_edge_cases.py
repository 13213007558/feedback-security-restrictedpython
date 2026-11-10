"""Evidence for the security-sensitive language boundaries.

Each test first proves the snippet is valid Python for the native compiler
(so a rejection cannot be dismissed as a syntax error), then demonstrates
either:

* rejection during *restricted* compilation - ``compile_restricted_exec``
  returns errors and no code object; or
* an allowed path that really routes through an existing guard.
"""

import ast

from RestrictedPython import compile_restricted_exec
from RestrictedPython.transformer import RestrictingNodeTransformer


def _is_natively_valid(source):
    """Parse without restrictions; return True if the language allows it."""
    ast.parse(source)
    return True


def _transformed_guard_names(source):
    tree = ast.parse(source)
    transformer = RestrictingNodeTransformer()
    transformer.visit(tree)
    assert not transformer.errors, transformer.errors
    return {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


# Scopes
# ======

def test_nonlocal_is_natively_valid_but_restricted_compile_fails():
    source = (
        'def outer():\n'
        '    message = "outer"\n'
        '    def inner():\n'
        '        nonlocal message\n'
        '        message = "inner"\n')
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.code is None
    assert result.errors == (
        'Line 4: Nonlocal statements are not allowed.',)


def test_global_is_allowed_and_name_checks_still_apply():
    source = (
        'def use_global():\n'
        '    global counter\n'
        '    counter = counter + 1\n')
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.errors == ()
    assert result.code is not None
    assert result.used_names == {'counter': True}


def test_underscore_name_in_scope_is_rejected_not_silently_allowed():
    source = 'def function():\n    _secret = 1\n'
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.code is None
    assert result.errors == (
        'Line 2: "_secret" is an invalid variable name because it '
        'starts with "_"',)


# Formatted strings
# =================

def test_fstring_allowed_path_keeps_attribute_guard():
    source = "value = f'name={user.name}'\n"
    assert _is_natively_valid(source)

    guard_names = _transformed_guard_names(source)
    assert '_getattr_' in guard_names

    result = compile_restricted_exec(source)
    assert result.errors == ()
    assert result.code is not None


def test_fstring_private_attribute_is_rejected_at_compile_stage():
    source = "value = f'{user._secret}'\n"
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.code is None
    assert result.errors == (
        'Line 1: "_secret" is an invalid attribute name because it '
        'starts with "_".',)


# Generators
# ==========

def test_generator_expression_routes_iteration_through_getiter():
    source = 'total = sum(item for item in items)\n'
    assert _is_natively_valid(source)

    guard_names = _transformed_guard_names(source)
    assert '_getiter_' in guard_names


def test_generator_function_compiles_and_runs_under_guard():
    source = (
        'def generate(items):\n'
        '    for item in items:\n'
        '        yield item * 2\n')
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.errors == ()
    assert result.code is not None
    namespace = {'_getiter_': iter}
    exec(result.code, namespace)
    assert list(namespace['generate']([1, 2])) == [2, 4]


def test_yield_from_is_allowed():
    source = (
        'def chain(items):\n'
        '    yield from items\n')
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.errors == ()
    assert result.code is not None


# Exception groups
# ================

def test_exception_group_syntax_is_natively_valid_but_restricted():
    source = (
        'try:\n'
        '    pass\n'
        'except* ValueError as errors:\n'
        '    pass\n')
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.code is None
    assert result.errors == (
        'Line 1: TryStar statements are not allowed.',)


def test_plain_except_still_allowed():
    source = (
        'try:\n'
        '    pass\n'
        'except ValueError as error:\n'
        '    pass\n')
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.errors == ()
    assert result.code is not None


# Async syntax
# ============

def test_async_function_is_rejected_at_restricted_compile():
    source = 'async def coroutine():\n    pass\n'
    assert _is_natively_valid(source)

    result = compile_restricted_exec(source)
    assert result.code is None
    assert result.errors == (
        'Line 1: AsyncFunctionDef statements are not allowed.',)


def test_await_rejection_is_not_masked_by_async_function_rejection():
    """The Await visitor must reject even if AsyncFunctionDef were allowed."""
    source = (
        'async def coroutine():\n'
        '    await suspend()\n')
    assert _is_natively_valid(source)

    tree = ast.parse(source)
    await_node = next(
        node for node in ast.walk(tree) if isinstance(node, ast.Await))
    transformer = RestrictingNodeTransformer()
    transformer.visit(await_node)
    assert transformer.errors == [
        'Line 2: Await statements are not allowed.']


def test_async_for_and_async_with_have_their_own_rejections():
    for_source = (
        'async def coroutine():\n'
        '    async for item in stream:\n'
        '        pass\n')
    with_source = (
        'async def coroutine():\n'
        '    async with manager() as resource:\n'
        '        pass\n')
    for source, node_type, message in (
            (for_source, ast.AsyncFor,
             'AsyncFor statements are not allowed.'),
            (with_source, ast.AsyncWith,
             'AsyncWith statements are not allowed.')):
        assert _is_natively_valid(source)
        tree = ast.parse(source)
        target = next(
            node for node in ast.walk(tree)
            if isinstance(node, node_type))
        transformer = RestrictingNodeTransformer()
        transformer.visit(target)
        assert transformer.errors == [f'Line 2: {message}']


# Guards on iteration and unpacking boundaries
# ============================================

def test_for_loop_iteration_is_guarded():
    assert '_getiter_' in _transformed_guard_names(
        'for item in items:\n    pass\n')


def test_tuple_unpacking_is_guarded():
    guard_names = _transformed_guard_names('first, second = pair\n')
    assert {'_getiter_', '_unpack_sequence_'} <= guard_names


def test_guarded_unpacking_really_runs_through_getiter():
    calls = []

    def recording_getiter(iterable):
        calls.append(iterable)
        return iter(iterable)

    result = compile_restricted_exec(
        'first, second = pair\n')
    assert result.errors == ()
    namespace = {
        'pair': (1, 2),
        '_getiter_': recording_getiter,
        '_unpack_sequence_': lambda it, spec, gi: list(gi(it)),
    }
    exec(result.code, namespace)
    assert namespace['first'] == 1
    assert calls == [(1, 2)]
