.. _pattern_matching:

Restricted pattern matching
---------------------------

The default :class:`~RestrictedPython.RestrictingNodeTransformer` rejects the
``match`` statement, because the native ``Match`` AST nodes perform iteration,
subscripting and attribute access without consulting any guard.

``RestrictedPython`` ships an opt-in policy,
:class:`~RestrictedPython.RestrictedMatchNodeTransformer`, which supports a
reviewed subset of the syntax instead of trusting the raw ``Match`` nodes.

.. warning::

   Enabling this policy is **not** a process isolation sandbox.
   It only extends the compile time restrictions; the guards it calls are still
   the ones *you* provide in ``globals``.
   Read :ref:`security_considerations` before enabling it, and keep the rest of
   your restricted environment locked down exactly as for any other restricted
   code.

Supported and rejected patterns
...............................

Supported:

* constant / value patterns (``case 1``, ``case cfg.mode``)
* singleton patterns (``case None``, ``case True``, ``case False``)
* capture and wildcard patterns (``case value``, ``case _``)
* OR patterns (``case 1 | 2 | 3``), with the usual rule that every alternative
  binds the same set of names
* sequence patterns, including starred capture (``case [a, *rest, z]``)
* mapping patterns, including the rest capture (``case {"k": v, **rest}``)

Rejected at compile time, with an error pointing at the offending line:

* class patterns (``case Point(x=1)``), including nested ones
* invalid capture names, for example names starting with an underscore
* duplicate bindings and duplicate mapping keys
* more than one starred pattern in a sequence
* OR alternatives that do not bind the same names

How the guards are used
.......................

The transformer never leaves a ``Match`` AST node in the compiled tree.
Each pattern becomes a plain data specification handed to the
``_match_pattern_`` runtime helper
(:func:`RestrictedPython.Guards.guarded_match_pattern`).
Every protocol access the matching needs is routed through a guard you supply:

* iteration (sequence materialisation, mapping ``**rest`` collection) goes
  through ``_getiter_``
* subscripting (mapping key lookups) goes through ``_getitem_``
* attribute access inside value patterns goes through ``_getattr_``

The subject expression is evaluated exactly once.
Bindings are committed as soon as a pattern matches, mirroring CPython: a false
``if`` guard does not roll the bindings back, but the case body is skipped and
matching continues with the next case.

Minimal setup
.............

The policy is selected per compilation, and the runtime helper plus the three
guards have to be present in the globals used to execute the code:

.. code-block:: python

    from RestrictedPython import compile_restricted
    from RestrictedPython import RestrictedMatchNodeTransformer
    from RestrictedPython import safe_globals
    from RestrictedPython.Guards import guarded_match_pattern

    source = """
    def describe(value):
        match value:
            case [first, *rest]:
                return ("sequence", first, rest)
            case {"k": item, **extra}:
                return ("mapping", item, extra)
            case other:
                return ("value", other)
    """

    byte_code = compile_restricted(
        source,
        policy=RestrictedMatchNodeTransformer,
    )

    restricted_globals = dict(safe_globals)
    restricted_globals.update({
        "_match_pattern_": guarded_match_pattern,
        "_getiter_": iter,
        "_getitem_": lambda ob, key: ob[key],
        "_getattr_": getattr,
    })

    loc = {}
    exec(byte_code, restricted_globals, loc)
    describe = loc["describe"]
    print(describe([1, 2, 3]))
    print(describe({"k": 9, "z": 1}))

The guards above are permissive examples.
In a real deployment pass guarded implementations that refuse the objects and
attributes your application does not want restricted code to reach;
a guard that raises prevents the matching from entering the corresponding
branch, so an unauthorised field never becomes a binding.
