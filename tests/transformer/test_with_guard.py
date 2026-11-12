"""Tests for the optional ``_with_`` guard policy.

The behavioural tests compare the events produced by ordinary CPython
against the events produced by the restricted compilation.  Apart from the
explicit ``_with_`` guard calls both executions must stay in lockstep.
"""

from RestrictedPython import RestrictingWithGuardNodeTransformer
from RestrictedPython import compile_restricted_exec
from RestrictedPython.Guards import guarded_unpack_sequence
from RestrictedPython.transformer import RestrictingNodeTransformer


class LoggingManager:
    """Context manager recording every lifecycle event."""

    def __init__(self, name, log, enter_error=None, exit_suppress=False,
                 enter_value='sentinel'):
        self.name = name
        self.log = log
        self.enter_error = enter_error
        self.exit_suppress = exit_suppress
        self.enter_value = enter_value

    def __enter__(self):
        self.log.append((self.name, 'enter'))
        if self.enter_error is not None:
            raise self.enter_error
        return self.enter_value

    def __exit__(self, exc_type, exc, tb):
        self.log.append(
            (self.name, 'exit', exc_type.__name__ if exc_type else None))
        return self.exit_suppress


def guarded_globals(log, with_guard, _write_=None, _getitem_=None):
    return {
        'log': log,
        'LoggingManager': LoggingManager,
        '_with_': with_guard,
        '_getiter_': lambda ob: ob,
        '_unpack_sequence_': guarded_unpack_sequence,
        '_getattr_': getattr,
        '_getitem_': _getitem_ or (lambda ob, index: ob[index]),
        '_write_': _write_ or (lambda ob: ob),
    }


def compile_guarded(source):
    result = compile_restricted_exec(
        source, policy=RestrictingWithGuardNodeTransformer)
    assert result.errors == (), result.errors
    assert result.code is not None
    return result.code


def run_guarded(source, log, with_guard, **kwargs):
    glb = guarded_globals(log, with_guard, **kwargs)
    exec(compile_guarded(source), glb)
    return glb


def run_plain(source, log):
    glb = {'log': log, 'LoggingManager': LoggingManager}
    exec(compile(source, '<plain>', 'exec'), glb)
    return glb


def manager_events(log):
    """Drop host guard calls, which only exist in the restricted run."""
    return [event for event in log if not (
        len(event) == 2 and event[1] == 'guard')]


def capture(runner):
    """Run ``runner(log)`` with a fresh log and return (events, outcome)."""
    log = []
    try:
        value = runner(log)
    except Exception as exc:  # noqa: BLE001 - the type itself is asserted
        return log, (type(exc).__name__, str(exc))
    return log, ('ok', value)


def _iter_traceback(tb):
    while tb is not None:
        yield tb
        tb = tb.tb_next


class RefusedByPolicy(Exception):
    pass


def recording_guard(log):
    def with_guard(manager):
        log.append((manager.name, 'guard'))
        return manager
    return with_guard


def refusing_guard(log, refused_name):
    def with_guard(manager):
        log.append((manager.name, 'guard'))
        if manager.name == refused_name:
            raise RefusedByPolicy(
                f'context manager {manager.name} is not allowed')
        return manager
    return with_guard


# ----------------------------------------------------------------------
# Two layered contexts, evaluation / binding order and guard calls
# ----------------------------------------------------------------------

TWO_LAYER = """
def run(first, second):
    with first as bound_a, second as bound_b:
        log.append(('body', bound_a, bound_b))
        return 'result'
"""


def test_two_layer_matches_plain_python_and_calls_guard_in_order():
    def plain_runner(log):
        first = LoggingManager('a', log)
        second = LoggingManager('b', log)
        return run_plain(TWO_LAYER, log)['run'](first, second)

    def guarded_runner(log):
        first = LoggingManager('a', log)
        second = LoggingManager('b', log)
        return run_guarded(
            TWO_LAYER, log, recording_guard(log))['run'](first, second)

    plain_log, plain_outcome = capture(plain_runner)
    guarded_log, guarded_outcome = capture(guarded_runner)

    assert guarded_outcome == plain_outcome == ('ok', 'result')
    assert manager_events(guarded_log) == manager_events(plain_log)
    assert manager_events(plain_log) == [
        ('a', 'enter'),
        ('b', 'enter'),
        ('body', 'sentinel', 'sentinel'),
        ('b', 'exit', None),
        ('a', 'exit', None),
    ]
    # Each object reaches the guard, and the guard runs before __enter__.
    assert guarded_log[:4] == [
        ('a', 'guard'),
        ('a', 'enter'),
        ('b', 'guard'),
        ('b', 'enter'),
    ]


ORDER_WITH_SECOND_SEEING_FIRST_BINDING = """
def run(make_second):
    with LoggingManager('a', log) as first, make_second(first) as second:
        log.append(('body', first, second))
"""


def test_second_context_expression_is_evaluated_after_first_binding():
    log = []
    seen = []

    def make_second(first):
        seen.append(first)
        return LoggingManager('b', log, enter_value='second')

    glb = run_guarded(
        ORDER_WITH_SECOND_SEEING_FIRST_BINDING,
        log, recording_guard(log))
    glb['run'](make_second)

    # The second expression received the value bound by the first `as`.
    assert seen == ['sentinel']
    assert log == [
        ('a', 'guard'), ('a', 'enter'),
        ('b', 'guard'), ('b', 'enter'),
        ('body', 'sentinel', 'second'),
        ('b', 'exit', None), ('a', 'exit', None)]


# ----------------------------------------------------------------------
# Refusal happens before __enter__; already entered managers still exit
# ----------------------------------------------------------------------

REFUSAL = """
def run(first, refused):
    with first as bound_a, refused as bound_b:
        log.append(('body', bound_a, bound_b))
"""


def test_guard_refusal_happens_before_enter_and_cleans_up_entered_managers():
    def plain_runner(log):
        first = LoggingManager('a', log)
        refused = LoggingManager('b', log)

        # Simulate the policy refusal at the exact same expansion point.
        def plain_with_guard(manager):
            log.append((manager.name, 'guard'))
            if manager is refused:
                raise RefusedByPolicy(
                    'context manager b is not allowed')
            return manager

        with plain_with_guard(first) as bound_a, plain_with_guard(refused):
            log.append(('body', bound_a, None))

    def guarded_runner(log):
        first = LoggingManager('a', log)
        refused = LoggingManager('b', log)
        run_guarded(
            REFUSAL, log, refusing_guard(log, 'b'))['run'](first, refused)

    plain_log, plain_outcome = capture(plain_runner)
    guarded_log, guarded_outcome = capture(guarded_runner)

    assert guarded_outcome == plain_outcome
    assert guarded_outcome == (
        'RefusedByPolicy', 'context manager b is not allowed')
    assert manager_events(guarded_log) == manager_events(plain_log) == [
        ('a', 'enter'),
        ('a', 'exit', 'RefusedByPolicy'),
    ]
    assert guarded_log == [
        ('a', 'guard'),
        ('a', 'enter'),
        ('b', 'guard'),
        ('a', 'exit', 'RefusedByPolicy'),
    ]
    # The refused object's __enter__ / __exit__ never run; the entered
    # manager exits exactly once.
    assert ('b', 'enter') not in guarded_log
    assert sum(event == ('a', 'exit', 'RefusedByPolicy')
               for event in guarded_log) == 1


def test_refusal_of_first_manager_enters_nothing():
    source = """
def run(manager):
    with manager as value:
        log.append(('body', value))
"""

    def runner(log):
        manager = LoggingManager('a', log)
        run_guarded(
            source, log, refusing_guard(log, 'a'))['run'](manager)

    events, outcome = capture(runner)
    assert outcome == (
        'RefusedByPolicy', 'context manager a is not allowed')
    assert events == [('a', 'guard')]


# ----------------------------------------------------------------------
# Target binding failure (guarded unpacking) keeps the expansion order
# ----------------------------------------------------------------------

UNPACK_FAILURE = """
def run(first, second):
    with first as bound_a, second as (x, y):
        log.append(('body', bound_a, x, y))
"""


def test_unpack_failure_matches_plain_python_and_exits_entered_managers():
    def make_managers(log):
        first = LoggingManager('a', log)
        second = LoggingManager('b', log, enter_value=(1,))  # too few items
        return first, second

    def plain_runner(log):
        first, second = make_managers(log)
        run_plain(UNPACK_FAILURE, log)['run'](first, second)

    def guarded_runner(log):
        first, second = make_managers(log)
        run_guarded(
            UNPACK_FAILURE, log, recording_guard(log))['run'](first, second)

    plain_log, plain_outcome = capture(plain_runner)
    guarded_log, guarded_outcome = capture(guarded_runner)

    assert guarded_outcome[0] == plain_outcome[0] == 'ValueError'
    assert manager_events(guarded_log) == manager_events(plain_log)
    assert manager_events(guarded_log) == [
        ('a', 'enter'),
        ('b', 'enter'),
        ('b', 'exit', 'ValueError'),
        ('a', 'exit', 'ValueError'),
    ]
    assert guarded_log == [
        ('a', 'guard'), ('a', 'enter'),
        ('b', 'guard'), ('b', 'enter'),
        ('b', 'exit', 'ValueError'),
        ('a', 'exit', 'ValueError'),
    ]


# ----------------------------------------------------------------------
# __enter__ raising behaves like plain Python
# ----------------------------------------------------------------------

ENTER_FAILURE = """
def run(first, broken):
    with first as bound_a, broken as bound_b:
        log.append(('body', bound_a, bound_b))
"""


def test_enter_error_matches_plain_python_and_exits_entered_managers():
    class BrokenEnter(Exception):
        pass

    def make_managers(log):
        first = LoggingManager('a', log)
        broken = LoggingManager('b', log, enter_error=BrokenEnter('boom'))
        return first, broken

    def plain_runner(log):
        first, broken = make_managers(log)
        run_plain(ENTER_FAILURE, log)['run'](first, broken)

    def guarded_runner(log):
        first, broken = make_managers(log)
        run_guarded(
            ENTER_FAILURE, log, recording_guard(log))['run'](first, broken)

    plain_log, plain_outcome = capture(plain_runner)
    guarded_log, guarded_outcome = capture(guarded_runner)

    assert guarded_outcome == plain_outcome == ('BrokenEnter', 'boom')
    assert manager_events(guarded_log) == manager_events(plain_log) == [
        ('a', 'enter'),
        ('b', 'enter'),
        ('a', 'exit', 'BrokenEnter'),
    ]
    assert guarded_log == [
        ('a', 'guard'), ('a', 'enter'),
        ('b', 'guard'), ('b', 'enter'),
        ('a', 'exit', 'BrokenEnter'),
    ]


# ----------------------------------------------------------------------
# __exit__ suppressing a body exception matches plain Python
# ----------------------------------------------------------------------

SUPPRESSED = """
def run(manager):
    with manager as value:
        log.append(('body', value))
        raise ValueError('ignored by __exit__')
    log.append(('after',))
"""


def test_suppressed_body_exception_matches_plain_python():
    def plain_runner(log):
        manager = LoggingManager('b', log, exit_suppress=True)
        run_plain(SUPPRESSED, log)['run'](manager)

    def guarded_runner(log):
        manager = LoggingManager('b', log, exit_suppress=True)
        run_guarded(
            SUPPRESSED, log, recording_guard(log))['run'](manager)

    plain_log, plain_outcome = capture(plain_runner)
    guarded_log, guarded_outcome = capture(guarded_runner)

    assert guarded_outcome == plain_outcome == ('ok', None)
    assert manager_events(guarded_log) == manager_events(plain_log) == [
        ('b', 'enter'),
        ('body', 'sentinel'),
        ('b', 'exit', 'ValueError'),
        ('after',),
    ]


SUPPRESSED_WITH_TWO_LAYERS = """
def run(first, second):
    with first, second:
        raise RuntimeError('body failure')
"""


def test_inner_manager_suppresses_exception_outer_still_sees_none():
    def plain_runner(log):
        first = LoggingManager('a', log)
        second = LoggingManager('b', log, exit_suppress=True)
        run_plain(SUPPRESSED_WITH_TWO_LAYERS, log)['run'](first, second)

    def guarded_runner(log):
        first = LoggingManager('a', log)
        second = LoggingManager('b', log, exit_suppress=True)
        run_guarded(
            SUPPRESSED_WITH_TWO_LAYERS, log,
            recording_guard(log))['run'](first, second)

    plain_log, plain_outcome = capture(plain_runner)
    guarded_log, guarded_outcome = capture(guarded_runner)

    assert guarded_outcome == plain_outcome == ('ok', None)
    assert manager_events(guarded_log) == manager_events(plain_log) == [
        ('a', 'enter'),
        ('b', 'enter'),
        ('b', 'exit', 'RuntimeError'),
        ('a', 'exit', None),
    ]


# ----------------------------------------------------------------------
# return / break / continue unwind the stack exactly like CPython
# ----------------------------------------------------------------------

CONTROL_FLOW = """
def run(kind, first, second):
    for turn in (1, 2, 3):
        with first as a, second as b:
            log.append(('body', kind, turn, a, b))
            if kind == 'return':
                return 'returned'
            if kind == 'break':
                break
            continue
        log.append(('between iterations', turn))
    log.append(('after loop',))
"""


def test_return_break_continue_match_plain_python():
    for kind in ('return', 'break', 'continue'):
        def plain_runner(log, kind=kind):
            first = LoggingManager('a', log)
            second = LoggingManager('b', log)
            return run_plain(CONTROL_FLOW, log)['run'](
                kind, first, second)

        def guarded_runner(log, kind=kind):
            first = LoggingManager('a', log)
            second = LoggingManager('b', log)
            return run_guarded(
                CONTROL_FLOW, log, recording_guard(log))['run'](
                kind, first, second)

        plain_log, plain_outcome = capture(plain_runner)
        guarded_log, guarded_outcome = capture(guarded_runner)

        assert guarded_outcome == plain_outcome
        assert manager_events(guarded_log) == manager_events(plain_log)

    events, outcome = capture(
        lambda log: run_guarded(
            CONTROL_FLOW, log, recording_guard(log))['run'](
            'return', LoggingManager('a', log), LoggingManager('b', log)))
    assert outcome == ('ok', 'returned')
    assert manager_events(events) == [
        ('a', 'enter'),
        ('b', 'enter'),
        ('body', 'return', 1, 'sentinel', 'sentinel'),
        ('b', 'exit', None), ('a', 'exit', None)]


# ----------------------------------------------------------------------
# Repeated execution must not leak temporary names or manager state
# ----------------------------------------------------------------------

REPEATED_UNPACK = """
def run(manager):
    with manager as (a, b):
        log.append(('body', a, b))
"""


def test_same_code_runs_repeatedly_without_leaking_tmp_names():
    log = []
    glb = run_guarded(REPEATED_UNPACK, log, recording_guard(log))
    manager = LoggingManager('a', log, enter_value=(1, 2))

    glb['run'](manager)
    glb['run'](LoggingManager('a', log, enter_value=(3, 4)))
    glb['run'](LoggingManager('a', log, enter_value=(5, 6)))

    assert manager_events(log) == [
        ('a', 'enter'), ('body', 1, 2),
        ('a', 'exit', None),
        ('a', 'enter'), ('body', 3, 4),
        ('a', 'exit', None),
        ('a', 'enter'), ('body', 5, 6),
        ('a', 'exit', None),
    ]
    # The temporary names created for unpacking are deleted again and are
    # not visible in the execution namespace.
    assert not any(name.startswith('_tmp') for name in glb)


REPEATED_REFUSAL = """
def run(manager):
    with manager as value:
        log.append(('body', value))
"""


def test_repeated_execution_after_refusal_keeps_working():
    log = []
    allowed = []

    def with_guard(manager):
        log.append((manager.name, 'guard'))
        if not allowed:
            raise RefusedByPolicy('not allowed yet')
        return manager

    glb = run_guarded(REPEATED_REFUSAL, log, with_guard)

    try:
        glb['run'](LoggingManager('a', log))
    except RefusedByPolicy as exc:
        outcome = (type(exc).__name__, str(exc))
    else:  # pragma: no cover
        raise AssertionError('first call should have been refused')
    assert outcome == ('RefusedByPolicy', 'not allowed yet')
    assert log == [('a', 'guard')]

    allowed.append(True)
    glb['run'](LoggingManager('a', log))
    assert log == [
        ('a', 'guard'),
        ('a', 'guard'), ('a', 'enter'),
        ('body', 'sentinel'), ('a', 'exit', None)]


# ----------------------------------------------------------------------
# The guard may return a host controlled replacement manager
# ----------------------------------------------------------------------

SUBSTITUTION = """
def run(manager):
    with manager as value:
        log.append(('body', value))
"""


def test_guard_may_return_a_replacement_manager():
    log = []
    replacement = LoggingManager('replacement', log, enter_value='wrapped')

    def with_guard(manager):
        log.append(('guard', manager.name))
        return replacement

    glb = run_guarded(SUBSTITUTION, log, with_guard)
    glb['run'](LoggingManager('original', log))

    assert log == [
        ('guard', 'original'),
        ('replacement', 'enter'),
        ('body', 'wrapped'),
        ('replacement', 'exit', None),
    ]


# ----------------------------------------------------------------------
# Attribute and subscript `as` targets keep the write protection
# ----------------------------------------------------------------------

WRITE_TARGETS = """
def assign_attr(holder, manager):
    with manager as holder.value:
        pass

def assign_item(holder, manager):
    with manager as holder['key']:
        pass
"""


def test_attribute_and_subscript_targets_still_use_write_guard(mocker):
    _write_ = mocker.stub()
    _write_.side_effect = lambda ob: ob
    _getitem_ = mocker.stub()
    _getitem_.side_effect = lambda ob, index: ob[index]

    log = []
    glb = run_guarded(
        WRITE_TARGETS, log, recording_guard(log),
        _write_=_write_, _getitem_=_getitem_)

    class Holder:
        pass

    holder = Holder()
    manager = LoggingManager('a', log, enter_value='bound')
    glb['assign_attr'](holder, manager)
    assert holder.value == 'bound'

    mapping = {}
    glb['assign_item'](mapping, LoggingManager('b', log, enter_value='item'))
    assert mapping == {'key': 'item'}

    _write_.assert_has_calls([mocker.call(holder), mocker.call(mapping)])


def test_refusing_write_guard_blocks_the_as_binding():
    class WriteDenied(Exception):
        pass

    def deny_write(ob):
        raise WriteDenied('writing is not allowed')

    log = []
    glb = run_guarded(
        WRITE_TARGETS, log, recording_guard(log), _write_=deny_write)

    class Holder:
        pass

    try:
        glb['assign_attr'](Holder(), LoggingManager('a', log))
    except WriteDenied as exc:
        outcome = (type(exc).__name__, str(exc))
    else:  # pragma: no cover
        raise AssertionError('write guard did not refuse the binding')
    assert outcome == ('WriteDenied', 'writing is not allowed')
    # The manager was entered and is still exited when binding fails.
    assert log == [
        ('a', 'guard'), ('a', 'enter'),
        ('a', 'exit', 'WriteDenied')]


# ----------------------------------------------------------------------
# User code cannot shadow the guard or collide with temporary names
# ----------------------------------------------------------------------

SHADOW_WITH_GUARD = """
def run():
    _with_ = 'hijacked'
    with m as value:
        return value
"""


def test_user_cannot_define_the_with_guard_name():
    result = compile_restricted_exec(
        SHADOW_WITH_GUARD, policy=RestrictingWithGuardNodeTransformer)
    assert result.errors == (
        'Line 3: "_with_" is an invalid variable name because it '
        'starts with "_"',)
    assert result.code is None


TMP_NAME_COLLISION = """
def run():
    _tmp0 = 'user value'
    with m as (a, b):
        return a, b, _tmp0
"""


def test_user_cannot_collide_with_generated_temporary_names():
    result = compile_restricted_exec(
        TMP_NAME_COLLISION, policy=RestrictingWithGuardNodeTransformer)
    assert all('_tmp0' in error for error in result.errors)
    assert result.code is None


# ----------------------------------------------------------------------
# The strategy is opt-in; the default policy stays unchanged
# ----------------------------------------------------------------------

DEFAULT_POLICY_SOURCE = """
def run(manager):
    with manager as value:
        return value
"""


def test_default_policy_does_not_insert_a_with_guard():
    result = compile_restricted_exec(DEFAULT_POLICY_SOURCE)
    assert result.errors == ()
    assert '_with_' not in result.used_names

    # No ``_with_`` global is needed to run the default compilation.
    log = []
    glb = {'log': log, 'LoggingManager': LoggingManager}
    exec(result.code, glb)
    manager = LoggingManager('a', log)
    assert glb['run'](manager) == 'sentinel'
    assert log == [('a', 'enter'), ('a', 'exit', None)]


def test_guarded_policy_records_the_guard_name_as_used():
    result = compile_restricted_exec(
        DEFAULT_POLICY_SOURCE, policy=RestrictingWithGuardNodeTransformer)
    assert result.errors == ()
    assert result.used_names['_with_'] is True


# ----------------------------------------------------------------------
# async with stays forbidden and error locations point to user source
# ----------------------------------------------------------------------

ASYNC_WITH = """
async def run(con):
    async with con.transaction() as tx:
        return tx
"""


class _AsyncAllowingTransformer(RestrictingWithGuardNodeTransformer):
    """Allow ``async def`` so the inner ``async with`` is still rejected."""

    def visit_AsyncFunctionDef(self, node):
        return self.node_contents_visit(node)


def test_async_with_remains_forbidden_under_the_guard_policy():
    result = compile_restricted_exec(
        ASYNC_WITH, policy=_AsyncAllowingTransformer)
    assert result.errors == (
        'Line 3: AsyncWith statements are not allowed.',)
    assert result.code is None


GUARD_ON_SPECIFIC_LINE = """
x = 1
y = 2
def run(manager):
    with manager as value:
        return value
"""


def test_guard_call_keeps_the_user_source_location():
    result = compile_restricted_exec(
        GUARD_ON_SPECIFIC_LINE,
        policy=RestrictingWithGuardNodeTransformer)
    assert result.errors == ()

    def with_guard(manager):
        raise RefusedByPolicy('refused from line 5')

    log = []
    glb = guarded_globals(log, with_guard)
    exec(result.code, glb)
    try:
        glb['run'](LoggingManager('a', log))
    except RefusedByPolicy:
        import sys
        tb = sys.exc_info()[2]
        # Locate the frame that belongs to the compiled user source; the
        # guard call keeps the location of the user's ``with`` statement.
        user_frames = [
            frame for frame in _iter_traceback(tb)
            if frame.tb_frame.f_code.co_filename == '<string>']
        assert user_frames, 'no user source frame found in traceback chain'
        user_frame = user_frames[0]
        assert user_frame.tb_lineno == 5
    else:  # pragma: no cover
        raise AssertionError('RefusedByPolicy was not raised')


def test_syntax_error_locations_still_point_to_user_source():
    source = """
x = 1
with m as:
    pass
"""
    result = compile_restricted_exec(
        source, policy=RestrictingWithGuardNodeTransformer)
    assert result.code is None
    assert result.errors[0].startswith('Line 3: SyntaxError:')


def test_private_attribute_rules_remain_in_effect_under_guard_policy():
    source = """
def run(manager):
    with manager as value:
        return value._secret
"""
    result = compile_restricted_exec(
        source, policy=RestrictingWithGuardNodeTransformer)
    assert result.errors == (
        'Line 4: "_secret" is an invalid attribute name because it '
        'starts with "_".',)
    assert result.code is None


def test_base_transformer_constructor_keeps_guard_disabled_by_default():
    # Subclassing the base transformer without the keyword keeps the old
    # behaviour for external policies.
    transformer = RestrictingNodeTransformer()
    assert transformer.with_guard is False
