"""Repeatable micro benchmark for guarded (nested) unpacking.

Covers the hot paths of policy scripts rather than integer addition only:
flat and nested tuple targets inside loops, starred targets, nested
assignment unpacking and chained multi-target assignment.

Run with the project interpreter, e.g.::

    .venv/bin/python benchmarks/unpack_benchmark.py
    .venv/bin/python benchmarks/unpack_benchmark.py --mode trace
    .venv/bin/python benchmarks/unpack_benchmark.py --mode alloc

``--mode trace`` prints the guard call trail for tiny inputs.
``--mode alloc`` reports the tracemalloc peak of one invocation.
"""

from __future__ import annotations

import argparse
import timeit
import tracemalloc

from RestrictedPython import compile_restricted_exec
from RestrictedPython.Guards import (
    guarded_iter_unpack_sequence,
    guarded_unpack_sequence,
    safe_builtins,
)

SOURCES = {
    'for_flat': """
def f(it):
    total = 0
    for a, b in it:
        total += a + b
    return total
""",
    'for_nested': """
def f(it):
    total = 0
    for (a, (b, c)) in it:
        total += a + b + c
    return total
""",
    'for_starred': """
def f(it):
    total = 0
    for a, *rest in it:
        total += a + len(rest)
    return total
""",
    'assign_nested': """
def f(g):
    a, (b, (c, d)) = g
    return a, b, c, d
""",
    'starred': """
def f(g):
    a, *rest, (c, e) = g
    return a, rest, c, e
""",
    'multi_target': """
def f(g):
    x = (a, b) = (c, d) = g
    return a, b, c, d, x
""",
    'mixed_tuple_name': """
def f(g):
    x = (a, b) = g
    return a, b, x
""",
}

DATA = {
    'for_flat': [(i, i + 1) for i in range(2000)],
    'for_nested': [(i, (i + 1, i + 2)) for i in range(2000)],
    'for_starred': [(i, i + 1, i + 2) for i in range(2000)],
    'assign_nested': (1, (2, (3, 4))),
    'starred': (1, 2, 3, (4, 5)),
    'multi_target': (1, 2),
    'mixed_tuple_name': (1, 2),
}

REPS = {
    'for_flat': 300,
    'for_nested': 300,
    'for_starred': 300,
    'assign_nested': 20000,
    'starred': 20000,
    'multi_target': 20000,
    'mixed_tuple_name': 20000,
}

SMALL = {
    'for_flat': [(1, 2), (3, 4)],
    'for_nested': [(1, (2, 3)), (4, (5, 6))],
    'for_starred': [(1, 2, 3)],
    'assign_nested': (1, (2, (3, 4))),
    'starred': (1, 2, 3, (4, 5)),
    'multi_target': (1, 2),
    'mixed_tuple_name': (1, 2),
}


def make_globals(tracer=None, counters=None):
    def getiter(it):
        if tracer is not None:
            tracer.append(('_getiter_', type(it).__name__))
        return iter(it)

    def unpack(it, spec, _getiter_):
        if counters is not None:
            kind = 'tuple' if isinstance(spec, tuple) else 'dict'
            counters[f'spec_{kind}'] += 1
            counters['unpack_calls'] += 1
        return guarded_unpack_sequence(it, spec, _getiter_)

    def iter_unpack(it, spec, _getiter_):
        if counters is not None:
            kind = 'tuple' if isinstance(spec, tuple) else 'dict'
            counters[f'spec_{kind}'] += 1
        yield from guarded_iter_unpack_sequence(it, spec, _getiter_)

    return {
        '__builtins__': safe_builtins.copy(),
        '_getiter_': getiter,
        '_unpack_sequence_': unpack,
        '_iter_unpack_sequence_': iter_unpack,
        '_inplacevar_': lambda op, x, y: x + y,
    }


def load_functions():
    functions = {}
    for name, source in SOURCES.items():
        result = compile_restricted_exec(source)
        assert not result.errors, result.errors
        glb = make_globals()
        exec(result.code, glb)
        functions[name] = glb['f']
    return functions


def mode_time():
    functions = load_functions()
    print(f"{'scenario':18s} {'total ms':>10s} {'per call us':>12s}")
    for name, function in functions.items():
        data = DATA[name]
        reps = REPS[name]
        elapsed = timeit.timeit(lambda: function(data), number=reps)
        print(f'{name:18s} {elapsed * 1000:10.2f} '
              f'{elapsed / reps * 1_000_000:12.2f}')


def mode_alloc():
    functions = load_functions()
    tracemalloc.start()
    print(f"{'scenario':18s} {'peak bytes/call':>16s}")
    for name, function in functions.items():
        data = DATA[name]
        function(data)  # warmup
        tracemalloc.reset_peak()
        function(data)
        _, peak = tracemalloc.get_traced_memory()
        print(f'{name:18s} {peak:16d}')
    tracemalloc.stop()


def mode_count():
    for name, source in SOURCES.items():
        result = compile_restricted_exec(source)
        counters = {'spec_tuple': 0, 'spec_dict': 0, 'unpack_calls': 0}
        glb = make_globals(counters=counters)
        exec(result.code, glb)
        glb['f'](DATA[name])
        print(f'{name:18s} {counters}')


def mode_trace():
    for name, source in SOURCES.items():
        result = compile_restricted_exec(source)
        tracer = []
        glb = make_globals(tracer=tracer)
        exec(result.code, glb)
        value = glb['f'](SMALL[name])
        print(f'{name}: result={value!r}')
        for guard, kind in tracer:
            print(f'    {guard}({kind})')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--mode', choices=('time', 'alloc', 'count', 'trace'),
        default='time')
    args = parser.parse_args()
    {'time': mode_time, 'alloc': mode_alloc,
     'count': mode_count, 'trace': mode_trace}[args.mode]()


if __name__ == '__main__':
    main()
