"""Runnable example of the optional ``_with_`` guard policy.

Run it from the repository root with::

    .venv/bin/python docs/usage/with_guard_example.py

The example shows exactly where a refusal lands: the host guard is called
with the evaluated context object *before* its ``__enter__`` method runs.
"""

from RestrictedPython import RestrictingWithGuardNodeTransformer
from RestrictedPython import compile_restricted_exec
from RestrictedPython.Guards import safer_getattr


class PolicyDenied(PermissionError):
    """Raised by the host when a context manager is not admissible."""


class ManagedResource:
    """A context manager the host knows and trusts."""

    def __init__(self, name, trusted):
        self.name = name
        self.trusted = trusted
        self.events = []

    def __enter__(self):
        self.events.append(f'{self.name}.__enter__')
        return self

    def __exit__(self, exc_type, exc, tb):
        self.events.append(
            f'{self.name}.__exit__({exc_type.__name__ if exc_type else None})')
        # Do not suppress anything; the host wants the full event trail.
        return False


def host_with_guard(obj):
    """Admission check injected into the restricted globals as ``_with_``."""
    print(f'[host] _with_({obj!r})')
    if not isinstance(obj, ManagedResource) or not obj.trusted:
        raise PolicyDenied(
            f'{getattr(obj, "name", obj)!r} failed the context '
            'manager admission check')
    # The host trusts this class, so returning the object unchanged is fine
    # for this example. A real host could return a checked wrapper instead.
    return obj


RESTRICTED_SOURCE = """
def work(first, second):
    with first as entered_first, second as entered_second:
        log.append((entered_first.name, entered_second.name))
"""


def main():
    result = compile_restricted_exec(
        RESTRICTED_SOURCE, policy=RestrictingWithGuardNodeTransformer)
    if result.errors:
        raise SystemExit(result.errors)

    log = []
    restricted_globals = {
        '__builtins__': {},
        '_with_': host_with_guard,
        '_getattr_': safer_getattr,
        'log': log,
    }
    exec(result.code, restricted_globals)

    good = ManagedResource('good', trusted=True)
    bad = ManagedResource('bad', trusted=False)

    print('--- allowed run ---')
    restricted_globals['work'](
        good, ManagedResource('also-good', trusted=True))
    print('body log:', log)
    print('good events:', good.events)

    print('--- refused run ---')
    try:
        restricted_globals['work'](good, bad)
    except PolicyDenied as exc:
        print(f'[host] caught {type(exc).__name__}: {exc}')

    # The refused manager was never entered; the already entered manager was
    # exited exactly once while the refusal propagated.
    print('refused manager events:', bad.events)
    print('good events after refusal:', good.events)


if __name__ == '__main__':
    main()
