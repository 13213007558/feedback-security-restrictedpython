import pytest

from RestrictedPython.Guards import guarded_match_pattern


def _identity_iter(ob):
    return iter(ob)


def _identity_item(ob, key):
    return ob[key]


def _match(subject, spec):
    return guarded_match_pattern(
        subject, spec, _identity_iter, _identity_item, getattr)


def test_wildcard_always_matches():
    assert _match(42, ('wildcard',)) == {}
    assert _match(None, ('wildcard',)) == {}


def test_capture_binds_subject():
    assert _match(42, ('capture', 'x')) == {'x': 42}


def test_underscore_capture_does_not_bind():
    assert _match(42, ('capture', '_')) == {}


def test_value_pattern_uses_equality():
    assert _match(1, ('value', 1)) == {}
    assert _match(1.0, ('value', 1)) == {}
    assert _match(2, ('value', 1)) is None


def test_singleton_pattern_uses_identity():
    assert _match(None, ('singleton', None)) == {}
    # ``True == 1`` but identity comparison keeps the patterns distinct.
    assert _match(1, ('singleton', True)) is None
    assert _match(True, ('singleton', True)) == {}


def test_as_pattern_binds_whole_subject():
    assert _match(
        [1], ('as', ('sequence', [('capture', 'a')]), 'whole')
    ) == {'a': 1, 'whole': [1]}


def test_or_returns_first_matching_alternative():
    spec = ('or', [
        ('value', 1),
        ('capture', 'x'),
    ])
    assert _match(1, spec) == {}
    assert _match(2, spec) == {'x': 2}


def test_or_failed_alternative_does_not_leak_bindings():
    spec = ('or', [
        ('sequence', [('capture', 'a'), ('capture', 'b')]),
        ('sequence', [('capture', 'a')]),
    ])
    # The first alternative needs two elements and fails; its partial
    # capture must never reach the result.
    assert _match([1], spec) == {'a': 1}


def test_or_all_alternatives_fail():
    spec = ('or', [('value', 1), ('value', 2)])
    assert _match(3, spec) is None


def test_sequence_requires_exact_length_without_star():
    spec = ('sequence', [('capture', 'a'), ('capture', 'b')])
    assert _match([1, 2], spec) == {'a': 1, 'b': 2}
    assert _match([1], spec) is None
    assert _match([1, 2, 3], spec) is None


def test_sequence_rejects_non_list_tuple():
    spec = ('sequence', [('wildcard',)])
    assert _match('ab', spec) is None
    assert _match(range(2), spec) is None
    assert _match([1], spec) == {}
    assert _match((1,), spec) == {}


def test_sequence_star_captures_remainder():
    spec = ('sequence', [
        ('capture', 'a'), ('star', 'rest'), ('capture', 'z')])
    assert _match([1, 2, 3, 4], spec) == {
        'a': 1, 'rest': [2, 3], 'z': 4}
    assert _match([1, 2], spec) == {'a': 1, 'rest': [], 'z': 2}
    assert _match([1], spec) is None


def test_unnamed_star_does_not_bind():
    spec = ('sequence', [('capture', 'a'), ('star', None)])
    assert _match([1, 2, 3], spec) == {'a': 1}


def test_mapping_requires_present_keys():
    spec = ('mapping', ['a'], [('capture', 'v')], None)
    assert _match({'a': 1}, spec) == {'v': 1}
    assert _match({'b': 2}, spec) is None


def test_mapping_rest_collects_leftover_items():
    spec = ('mapping', ['a'], [('capture', 'v')], 'rest')
    assert _match({'a': 1, 'b': 2, 'c': 3}, spec) == {
        'v': 1, 'rest': {'b': 2, 'c': 3}}
    assert _match({'a': 1}, spec) == {'v': 1, 'rest': {}}


def test_mapping_rejects_non_dict():
    spec = ('mapping', [], [], None)
    assert _match({}, spec) == {}

    class PlainMapping:
        def __getitem__(self, key):
            return 1

    assert _match(PlainMapping(), spec) is None


def test_nested_matching_through_guards():
    spec = ('sequence', [
        ('mapping', ['k'], [('capture', 'v')], None)])
    assert _match([{'k': 7}], spec) == {'v': 7}
    assert _match([{}], spec) is None


def test_iteration_guard_is_used_for_sequences():
    seen = []

    def rec_iter(ob):
        seen.append(ob)
        return iter(ob)

    result = guarded_match_pattern(
        [1, 2], ('sequence', [('wildcard',), ('wildcard',)]),
        rec_iter, _identity_item, getattr)
    assert result == {}
    assert seen == [[1, 2]]


def test_subscript_guard_is_used_for_mapping_values():
    seen = []

    def rec_item(ob, key):
        seen.append(key)
        return ob[key]

    result = guarded_match_pattern(
        {'a': 1}, ('mapping', ['a'], [('capture', 'v')], None),
        _identity_iter, rec_item, getattr)
    assert result == {'v': 1}
    assert seen == ['a']


def test_denying_iter_guard_blocks_sequence_match():
    def deny_iter(ob):
        raise PermissionError

    with pytest.raises(PermissionError):
        guarded_match_pattern(
            [1], ('sequence', [('capture', 'a')]),
            deny_iter, _identity_item, getattr)


def test_denying_subscript_guard_blocks_mapping_match():
    def deny_item(ob, key):
        raise PermissionError

    with pytest.raises(PermissionError):
        guarded_match_pattern(
            {'a': 1}, ('mapping', ['a'], [('capture', 'v')], None),
            _identity_iter, deny_item, getattr)


def test_unknown_spec_tag_raises_value_error():
    with pytest.raises(ValueError):
        _match(1, ('bogus',))
