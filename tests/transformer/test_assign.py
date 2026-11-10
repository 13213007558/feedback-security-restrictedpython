import ast
import dis

import pytest

from RestrictedPython.Guards import guarded_unpack_sequence
from RestrictedPython.transformer import RestrictingNodeTransformer
from tests.helper import restricted_exec


def test_RestrictingNodeTransformer__visit_Assign__1(mocker):
    src = "orig = (a, (x, z)) = (c, d) = g"

    _getiter_ = mocker.stub()
    _getiter_.side_effect = lambda it: it

    glb = {
        '_getiter_': _getiter_,
        '_unpack_sequence_': guarded_unpack_sequence,
        'g': (1, (2, 3)),
    }

    restricted_exec(src, glb)
    assert glb['a'] == 1
    assert glb['x'] == 2
    assert glb['z'] == 3
    assert glb['c'] == 1
    assert glb['d'] == (2, 3)
    assert glb['orig'] == (1, (2, 3))
    assert _getiter_.call_count == 3
    _getiter_.assert_any_call((1, (2, 3)))
    _getiter_.assert_any_call((2, 3))
    _getiter_.reset_mock()


def test_RestrictingNodeTransformer__visit_Assign__2(
        mocker):
    src = "a, *d, (c, *e), x  = (1, 2, 3, (4, 3, 4), 5)"

    _getiter_ = mocker.stub()
    _getiter_.side_effect = lambda it: it

    glb = {
        '_getiter_': _getiter_,
        '_unpack_sequence_': guarded_unpack_sequence
    }

    restricted_exec(src, glb)
    assert glb['a'] == 1
    assert glb['d'] == [2, 3]
    assert glb['c'] == 4
    assert glb['e'] == [3, 4]
    assert glb['x'] == 5

    _getiter_.assert_has_calls([
        mocker.call((1, 2, 3, (4, 3, 4), 5)),
        mocker.call((4, 3, 4))])


def _guarded_globals(mocker, extra=None):
    _getiter_ = mocker.stub()
    _getiter_.side_effect = lambda it: iter(it)
    glb = {
        '__builtins__': {},
        '_getiter_': _getiter_,
        '_unpack_sequence_': guarded_unpack_sequence,
    }
    if extra:
        glb.update(extra)
    return glb, _getiter_


def test_RestrictingNodeTransformer__visit_Assign__rhs_evaluated_once(mocker):
    """Multi target assignment evaluates the right hand side exactly once."""
    calls = []

    def make():
        calls.append(1)
        return (7, 8)

    glb, _getiter_ = _guarded_globals(mocker, {'make': make})
    restricted_exec("(a, b) = (c, d) = make()", glb)
    assert len(calls) == 1
    assert (glb['a'], glb['b'], glb['c'], glb['d']) == (7, 8, 7, 8)


def test_RestrictingNodeTransformer__visit_Assign__name_target(mocker):
    """A name target receives the single, shared right hand side value."""
    calls = []

    def make():
        calls.append(1)
        return (9, 9)

    glb, _getiter_ = _guarded_globals(mocker, {'make': make})
    restricted_exec("x = (a, b) = make()", glb)
    assert len(calls) == 1
    assert glb['x'] == (9, 9)
    assert (glb['a'], glb['b']) == (9, 9)


def test_RestrictingNodeTransformer__visit_Assign__one_shot_iterator(mocker):
    """A one shot iterator is shared, matching native Python semantics."""
    def make():
        return iter([7, 8])

    glb, _getiter_ = _guarded_globals(mocker, {'make': make})
    restricted_exec("x = (a, b) = make()", glb)
    assert (glb['a'], glb['b']) == (7, 8)
    # The plain name target keeps the iterator object itself.
    assert hasattr(glb['x'], '__next__')

    glb, _getiter_ = _guarded_globals(
        mocker, {'make': lambda: iter([7, 8])})
    with pytest.raises(ValueError):
        # Native Python raises here as well: the exhausted iterator cannot
        # feed a second tuple target.
        restricted_exec("(a, b) = (c, d) = make()", glb)


def test_RestrictingNodeTransformer__visit_Assign__failure_state(mocker):
    """Rightmost target is filled first; a later failure keeps its state."""
    glb, _getiter_ = _guarded_globals(mocker, {'g': (1, 2, 3)})
    with pytest.raises(ValueError):
        restricted_exec("(a, b) = (c, d, e) = g", glb)
    assert (glb['c'], glb['d'], glb['e']) == (1, 2, 3)
    # The temporary variable is removed by the ``finally`` cleanup.
    assert not any(name.startswith('_tmp') for name in glb)

    glb, _getiter_ = _guarded_globals(mocker, {'g': (1,)})
    with pytest.raises(ValueError):
        restricted_exec("(a, b) = (c, d) = g", glb)
    assert all(name not in glb for name in ('a', 'b', 'c', 'd'))
    assert not any(name.startswith('_tmp') for name in glb)


def test_RestrictingNodeTransformer__gen_unpack_spec__is_constant_tuple():
    """The unpack spec is a tuple literal the compiler can constant fold."""
    tree = ast.parse('a, (b, c) = g')
    transformer = RestrictingNodeTransformer()
    transformed = transformer.visit(tree)
    ast.fix_missing_locations(transformed)
    call = transformed.body[0].value
    spec = call.args[1]
    assert isinstance(spec, ast.Tuple)
    min_len, childs = spec.elts
    assert isinstance(min_len, ast.Constant) and min_len.value == 2
    assert isinstance(childs, ast.Tuple)
    index, nested_spec = childs.elts[0].elts
    assert index.value == 1
    assert isinstance(nested_spec, ast.Tuple)
    assert nested_spec.elts[0].value == 2

    # The whole spec ends up as a single code constant, no dict is built.
    code = compile(transformed, '<spec>', 'exec')
    opnames = [
        instruction.opname
        for instruction in dis.get_instructions(code)]
    assert 'BUILD_CONST_KEY_MAP' not in opnames
    assert all(not isinstance(const, dict) for const in code.co_consts)
