"""Guarded augmented assignment of attributes, items and slices.

The default :class:`RestrictingNodeTransformer` keeps rejecting
``object.attr += value`` and ``object[key] += value`` at compile time.
Trusted policies opt in with ``allow_guarded_augassign = True``; the
statements are then rewritten to calls of the ``_inplace_aug_attr_`` /
``_inplace_aug_item_`` policy guards.

Guard contract::

    _inplace_aug_attr_(object, name, getter, value, operator)
    _inplace_aug_item_(object, key, getter, value, operator)

``getter`` is a zero argument callable.  A guard must authorize the
write before calling the getter, so a denied write guarantees that the
in-place operator never runs.
"""

import operator

import pytest

from RestrictedPython import compile_restricted_exec
from RestrictedPython.Guards import _guarded_inplace_aug_attr
from RestrictedPython.Guards import _guarded_inplace_aug_item
from RestrictedPython.Guards import _INPLACE_OPERATORS
from RestrictedPython.Guards import full_write_guard
from RestrictedPython.Guards import safe_builtins
from RestrictedPython.Guards import safer_getattr
from RestrictedPython.transformer import RestrictingNodeTransformer


class TrustedPolicy(RestrictingNodeTransformer):
    allow_guarded_augassign = True


def trusted_exec(source, glb):
    result = compile_restricted_exec(source, policy=TrustedPolicy)
    assert result.errors == (), result.errors
    assert result.code is not None
    exec(result.code, glb)


def base_globals(**overrides):
    glb = {
        '__builtins__': safe_builtins.copy(),
        '_getattr_': safer_getattr,
        '_getitem_': lambda ob, key: ob[key],
        '_write_': full_write_guard,
        '_inplace_aug_attr_': _guarded_inplace_aug_attr(full_write_guard),
        '_inplace_aug_item_': _guarded_inplace_aug_item(full_write_guard),
    }
    glb.update(overrides)
    return glb


# ---------------------------------------------------------------------
# Compilation / guard call contract
# ---------------------------------------------------------------------


def test_guarded_attribute_augassign_calls_guard(mocker):
    captured = {}

    def guard_impl(ob, name, getter, value, op):
        captured['args'] = (ob, name, getter, value, op)
        captured['current'] = getter()
        setattr(ob, name, captured['current'] + value)

    guard = mocker.stub()
    guard.side_effect = guard_impl
    target = mocker.stub()
    target.value = 1
    getattr_guard = mocker.stub()
    getattr_guard.side_effect = getattr
    glb = {
        '_getattr_': getattr_guard,
        '_inplace_aug_attr_': guard,
        'target': target,
    }

    trusted_exec('target.value += 4', glb)

    ob, name, getter, value, op = captured['args']
    assert ob is target
    assert name == 'value'
    assert callable(getter)
    assert captured['current'] == 1
    assert value == 4
    assert op == '+='
    assert target.value == 5
    getattr_guard.assert_called_once_with(target, 'value')


def test_guarded_subscript_augassign_calls_guard(mocker):
    captured = {}

    def guard_impl(ob, key, getter, value, op):
        captured['args'] = (ob, key, getter, value, op)
        captured['current'] = getter()
        ob[key] = captured['current'] + value

    guard = mocker.stub()
    guard.side_effect = guard_impl
    container = {'key': 1}
    getitem_guard = mocker.stub()
    getitem_guard.side_effect = lambda ob, key: ob[key]
    glb = {
        '_getitem_': getitem_guard,
        '_inplace_aug_item_': guard,
        'container': container,
        'key': 'key',
    }

    trusted_exec('container[key] += 4', glb)

    ob, key, getter, value, op = captured['args']
    assert ob is container
    assert key == 'key'
    assert callable(getter)
    assert captured['current'] == 1
    assert value == 4
    assert op == '+='
    assert container == {'key': 5}
    getitem_guard.assert_called_once_with(container, 'key')


def test_guarded_slice_augassign_passes_slice_object(mocker):
    captured = {}

    def guard_impl(ob, key, getter, value, op):
        captured['args'] = (ob, key, getter, value, op)
        captured['current'] = getter()
        ob[key] = captured['current'] + value

    guard = mocker.stub()
    guard.side_effect = guard_impl
    container = [1, 2, 3, 4]
    glb = {
        '_getitem_': lambda ob, key: ob[key],
        '_inplace_aug_item_': guard,
        'container': container,
    }

    trusted_exec('container[1:3] += [9]', glb)

    ob, key, getter, value, op = captured['args']
    assert ob is container
    assert key == slice(1, 3, None)
    assert captured['current'] == [2, 3]
    assert value == [9]
    assert op == '+='


def test_guarded_slice_with_step_passes_slice_object(mocker):
    guard = mocker.stub()
    guard.side_effect = lambda ob, key, getter, value, op: getter()
    container = [1, 2, 3, 4, 5, 6]
    glb = {
        '_getitem_': lambda ob, key: ob[key],
        '_inplace_aug_item_': guard,
        'container': container,
    }

    trusted_exec('container[1:6:2] += [9]', glb)

    assert guard.call_args.args[1] == slice(1, 6, 2)


def test_guarded_extended_slice_passes_tuple_of_indexes(mocker):
    guard = mocker.stub()
    guard.side_effect = lambda ob, key, getter, value, op: getter()

    class Multi:
        def __getitem__(self, key):
            return key

    glb = {
        '_getitem_': lambda ob, key: ob[key],
        '_inplace_aug_item_': guard,
        'multi': Multi(),
    }

    trusted_exec('multi[0, 1:2, :3] += [9]', glb)

    assert guard.call_args.args[1] == (
        0, slice(1, 2, None), slice(None, 3))


def test_getter_is_lazy_and_not_called_before_guard(mocker):
    events = []
    guard = mocker.stub()

    def guard_impl(ob, name, getter, value, op):
        events.append('guard')
        # The getter must only be invoked after the guard took control.
        assert events.count('getter') == 0
        current = getter()
        events.append('iadd')
        setattr(ob, name, current + value)

    guard.side_effect = guard_impl
    target = type('Target', (), {'value': 1})()
    glb = {
        '_getattr_': lambda ob, name: (events.append('getter'),
                                       getattr(ob, name))[1],
        '_inplace_aug_attr_': guard,
        'target': target,
    }

    trusted_exec('target.value += 4', glb)

    assert events == ['guard', 'getter', 'iadd']
    assert target.value == 5


def test_attribute_augassign_evaluation_contract():
    log = []

    class Target:
        _guarded_writes = True

        def __init__(self):
            # Bypass the tracing __setattr__ during construction.
            object.__setattr__(self, 'value', 1)

        def __getattribute__(self, name):
            if name == 'value':
                log.append('getattr')
            return object.__getattribute__(self, name)

        def __setattr__(self, name, value):
            if name == 'value':
                log.append('setattr')
            object.__setattr__(self, name, value)

    def write_guard(ob):
        log.append('write')
        return ob

    def attr_guard(ob, name, getter, value, op):
        writable = write_guard(ob)
        current = getter()
        log.append('iadd')
        setattr(writable, name, current + value)

    glb = {
        '_getattr_': lambda ob, name: getattr(ob, name),
        '_inplace_aug_attr_': attr_guard,
        'target': Target(),
    }

    trusted_exec('target.value += 9', glb)

    assert log == ['write', 'getattr', 'iadd', 'setattr']


def test_subscript_augassign_evaluation_contract():
    log = []

    class Container:
        _guarded_writes = True

        def __init__(self):
            self.items = {'key': 1}

        def __getitem__(self, key):
            log.append('getitem')
            return self.items[key]

        def __setitem__(self, key, value):
            log.append('setitem')
            self.items[key] = value

    def write_guard(ob):
        log.append('write')
        return ob

    def item_guard(ob, key, getter, value, op):
        writable = write_guard(ob)
        current = getter()
        log.append('iadd')
        writable[key] = current + value

    glb = {
        '_getitem_': lambda ob, key: ob[key],
        '_inplace_aug_item_': item_guard,
        'container': Container(),
        'key': 'key',
    }

    trusted_exec('container[key] += 9', glb)

    assert log == ['write', 'getitem', 'iadd', 'setitem']


def test_object_and_key_and_rhs_evaluated_once():
    calls = []

    class Container:
        _guarded_writes = True

        def __init__(self):
            self.items = {'key': 0}

        def __getitem__(self, key):
            return self.items[key]

        def __setitem__(self, key, value):
            self.items[key] = value

    def make_container():
        calls.append('object')
        return Container()

    def make_key():
        calls.append('key')
        return 'key'

    def make_rhs():
        calls.append('rhs')
        return 7

    glb = base_globals(
        make_container=make_container,
        make_key=make_key,
        make_rhs=make_rhs)

    trusted_exec('make_container().items[make_key()] += make_rhs()', glb)

    assert calls == ['object', 'key', 'rhs']


def test_attribute_object_expression_evaluated_once():
    calls = []

    class Target:
        _guarded_writes = True

        def __init__(self):
            self.value = 0

    def make_target():
        calls.append('object')
        return Target()

    glb = base_globals(make_target=make_target)

    trusted_exec('make_target().value += 1', glb)

    assert calls == ['object']


# ---------------------------------------------------------------------
# Default policy keeps rejecting
# ---------------------------------------------------------------------


def test_default_policy_rejects_attribute_augassign():
    result = compile_restricted_exec('a.a += 1')
    assert result.errors == (
        'Line 1: Augmented assignment of attributes is not allowed.',)


def test_default_policy_rejects_subscript_augassign():
    result = compile_restricted_exec('a[a] += 1')
    assert result.errors == (
        'Line 1: Augmented assignment of object items and slices is not '
        'allowed.',)


def test_default_policy_rejects_slice_augassign():
    result = compile_restricted_exec('a[x:y] += 1')
    assert result.errors == (
        'Line 1: Augmented assignment of object items and slices is not '
        'allowed.',)


def test_default_policy_rejects_slice_with_step_augassign():
    result = compile_restricted_exec('a[x:y:z] += 1')
    assert result.errors == (
        'Line 1: Augmented assignment of object items and slices is not '
        'allowed.',)


def test_simple_name_augassign_unchanged(mocker):
    _inplacevar_ = mocker.stub()
    _inplacevar_.side_effect = lambda op, val, expr: val + expr
    glb = {'_inplacevar_': _inplacevar_, 'a': 1, 'x': 1, 'z': 0}

    trusted_exec('a += x + z', glb)

    assert glb['a'] == 2
    _inplacevar_.assert_called_once_with('+=', 1, 1)


def test_simple_name_augassign_does_not_need_new_guards():
    # The simple name path must not depend on the new guards at all.
    glb = {'_inplacevar_': lambda op, val, expr: val + expr, 'a': 40}

    trusted_exec('a += 2', glb)

    assert glb['a'] == 42


# ---------------------------------------------------------------------
# Missing guards fail loudly instead of unguarded access
# ---------------------------------------------------------------------


def test_missing_attribute_guard_fails_with_name_error():
    target = type('Target', (), {'_guarded_writes': True, 'value': 1})()
    glb = {'__builtins__': safe_builtins, '_getattr_': safer_getattr,
           'target': target}

    with pytest.raises(NameError, match='_inplace_aug_attr_'):
        trusted_exec('target.value += 1', glb)


def test_missing_subscript_guard_fails_with_name_error():
    glb = {
        '__builtins__': safe_builtins,
        '_getitem_': lambda ob, key: ob[key],
        'container': [1],
    }

    with pytest.raises(NameError, match='_inplace_aug_item_'):
        trusted_exec('container[0] += 1', glb)


def test_no_tmp_variables_remain_after_success():
    glb = base_globals(container={'key': 1})

    trusted_exec('container["key"] += 1', glb)

    assert not [name for name in glb if name.startswith('_tmp')]


def test_no_tmp_variables_remain_after_failure():
    class Broken:
        def __iadd__(self, other):
            raise ValueError('boom')

    target = type(
        'Target', (), {'_guarded_writes': True, 'value': Broken()})()
    glb = base_globals(target=target)

    with pytest.raises(ValueError, match='boom'):
        trusted_exec('target.value += 1', glb)

    assert not [name for name in glb if name.startswith('_tmp')]


# ---------------------------------------------------------------------
# Denial proves the in-place operator is never called
# ---------------------------------------------------------------------


class TracingValue:
    def __init__(self, log):
        self.log = log

    def __iadd__(self, other):
        self.log.append('iadd')
        return self


class PermissionDenied(Exception):
    """Raised by a write guard that decides up front that writes are off."""


def denying_write_guard(ob):
    raise PermissionDenied('object is read-only')


def denying_globals(**overrides):
    return base_globals(
        _write_=denying_write_guard,
        _inplace_aug_attr_=_guarded_inplace_aug_attr(denying_write_guard),
        _inplace_aug_item_=_guarded_inplace_aug_item(denying_write_guard),
        **overrides)


def test_denied_attribute_write_never_calls_iadd():
    log = []

    class ReadOnly:
        def __init__(self):
            self.value = TracingValue(log)

    target = ReadOnly()
    glb = denying_globals(target=target)

    with pytest.raises(PermissionDenied):
        trusted_exec('target.value += 1', glb)

    assert log == []


def test_denied_item_write_never_calls_iadd():
    log = []

    class ReadOnly:
        def __init__(self):
            self.items = {'key': TracingValue(log)}

        def __getitem__(self, key):
            return self.items[key]

    target = ReadOnly()
    glb = denying_globals(target=target)

    with pytest.raises(PermissionDenied):
        trusted_exec('target["key"] += 1', glb)

    assert log == []


def test_denied_write_keeps_business_state():
    class ReadOnly:
        def __init__(self):
            self.value = [1]

    target = ReadOnly()
    glb = denying_globals(target=target)

    with pytest.raises(PermissionDenied):
        trusted_exec('target.value += [2]', glb)

    assert target.value == [1]


def test_denied_write_never_calls_getter():
    events = []

    class ReadOnly:
        def __init__(self):
            self.value = 1

    def attr_guard(ob, name, getter, value, op):
        events.append('guard')
        denying_write_guard(ob)  # raises before the getter is touched
        raise AssertionError('unreachable')  # pragma: no cover

    glb = {
        '_getattr_': lambda ob, name: (events.append('getter'),
                                       getattr(ob, name))[1],
        '_inplace_aug_attr_': attr_guard,
        'target': ReadOnly(),
    }

    with pytest.raises(PermissionDenied):
        trusted_exec('target.value += 2', glb)

    assert events == ['guard']


def test_denied_item_write_never_calls_getter():
    events = []

    class ReadOnly:
        def __getitem__(self, key):
            events.append('getter')
            return 1

    def item_guard(ob, key, getter, value, op):
        events.append('guard')
        denying_write_guard(ob)
        raise AssertionError('unreachable')  # pragma: no cover

    glb = {
        '_getitem_': lambda ob, key: ob[key],
        '_inplace_aug_item_': item_guard,
        'target': ReadOnly(),
    }

    with pytest.raises(PermissionDenied):
        trusted_exec('target["key"] += 2', glb)

    assert events == ['guard']


# ---------------------------------------------------------------------
# Descriptor ordering
# ---------------------------------------------------------------------


def test_descriptor_get_before_set():
    log = []

    class Descriptor:
        def __get__(self, instance, owner):
            log.append('get')
            return instance._value

        def __set__(self, instance, value):
            log.append('set')
            instance._value = value

    class Holder:
        value = Descriptor()
        _guarded_writes = True

        def __init__(self):
            self._value = 10

    glb = base_globals(holder=Holder())

    trusted_exec('holder.value += 5', glb)

    assert log == ['get', 'set']
    assert glb['holder']._value == 15


# ---------------------------------------------------------------------
# Reference guard parity with native Python
# ---------------------------------------------------------------------


ALL_OPERATORS = [
    ('+=', operator.iadd),
    ('-=', operator.isub),
    ('*=', operator.imul),
    ('/=', operator.itruediv),
    ('%=', operator.imod),
    ('**=', operator.ipow),
    ('<<=', operator.ilshift),
    ('>>=', operator.irshift),
    ('|=', operator.ior),
    ('^=', operator.ixor),
    ('&=', operator.iand),
    ('//=', operator.ifloordiv),
]


class AttrHolder:
    _guarded_writes = True

    def __init__(self, value):
        self.value = value


@pytest.mark.parametrize('symbol, native', ALL_OPERATORS)
def test_attribute_augassign_matches_native(symbol, native):
    native_holder = AttrHolder(24)
    restricted_holder = AttrHolder(24)

    native_holder.value = native(native_holder.value, 3)
    glb = base_globals(holder=restricted_holder)
    trusted_exec(f'holder.value {symbol} 3', glb)

    assert restricted_holder.value == native_holder.value


@pytest.mark.parametrize('symbol, native', ALL_OPERATORS)
def test_item_augassign_matches_native(symbol, native):
    native_container = {'key': 24}
    restricted_container = {'key': 24}

    native_container['key'] = native(native_container['key'], 3)
    glb = base_globals(container=restricted_container)
    trusted_exec(f'container["key"] {symbol} 3', glb)

    assert restricted_container == native_container


def test_matmul_attribute_augassign_matches_native():
    class Vector:
        def __init__(self, size):
            self.size = size

        def __imatmul__(self, other):
            return Vector(self.size * other.size)

    class Holder:
        _guarded_writes = True

        def __init__(self, vector):
            self.vector = vector

    native_holder = Holder(Vector(6))
    restricted_holder = Holder(Vector(6))

    native_holder.vector @= Vector(7)
    glb = base_globals(
        holder=restricted_holder, other=Vector(7))
    trusted_exec('holder.vector @= other', glb)

    assert restricted_holder.vector.size == native_holder.vector.size == 42


def test_list_item_and_slice_augassign_matches_native():
    native_list = [1, 2, 3, 4]
    restricted_list = [1, 2, 3, 4]

    native_list[0] += 10
    native_list[1:3] += [9]

    glb = base_globals(container=restricted_list)
    trusted_exec('container[0] += 10\ncontainer[1:3] += [9]', glb)

    assert restricted_list == native_list == [11, 2, 3, 9, 4]


def test_iadd_returning_new_object_is_written_back():
    class NewObject:
        def __iadd__(self, other):
            return 'replacement'

    class Holder:
        _guarded_writes = True

        def __init__(self):
            self.value = NewObject()

    holder = Holder()
    glb = base_globals(holder=holder)

    trusted_exec('holder.value += 1', glb)

    assert holder.value == 'replacement'


def test_reference_guards_authorize_before_getter(mocker):
    log = []
    write_guard = mocker.stub()

    class Writable:
        _guarded_writes = True

        def __init__(self):
            self.value = 1

        def __getitem__(self, key):
            log.append('getitem')
            return 1

        def __setitem__(self, key, value):
            log.append('setitem')

    writable = Writable()
    write_guard.side_effect = lambda ob: (log.append('authorize'), ob)[1]

    attr_guard = _guarded_inplace_aug_attr(write_guard)
    item_guard = _guarded_inplace_aug_item(write_guard)

    attr_guard(writable, 'value', lambda: getattr(writable, 'value'), 2, '+=')
    item_guard(writable, 'key', lambda: writable['key'], 2, '+=')

    assert log == ['authorize', 'authorize', 'getitem', 'setitem']
    assert write_guard.call_args_list == [mocker.call(writable),
                                          mocker.call(writable)]


def test_reference_guard_denial_skips_getter_and_operator(mocker):
    write_guard = mocker.stub()
    write_guard.side_effect = TypeError('read-only')
    getter = mocker.stub()

    attr_guard = _guarded_inplace_aug_attr(write_guard)
    with pytest.raises(TypeError, match='read-only'):
        attr_guard(object(), 'value', getter, 1, '+=')
    getter.assert_not_called()

    item_guard = _guarded_inplace_aug_item(write_guard)
    with pytest.raises(TypeError, match='read-only'):
        item_guard(object(), 'key', getter, 1, '+=')
    getter.assert_not_called()


def test_inplace_operator_table_covers_all_operators():
    assert set(_INPLACE_OPERATORS) == {
        '+=', '-=', '*=', '/=', '%=', '**=', '<<=', '>>=',
        '|=', '^=', '&=', '//=', '@=',
    }
