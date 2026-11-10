AST node coverage
=================

Interpreter upgrades add classes to the standard library :mod:`ast` module.
RestrictedPython must never become more permissive just because a
``visit_<Node>`` method was forgotten, so the test package
``tests/ast_coverage`` keeps an explicit, evidence-based decision for every
node class the running interpreter exposes.

The three decisions
-------------------

For every concrete node class one of three policies is recorded in
``tests/ast_coverage/policy.py``:

``allowed``
    An explicit ``visit_<Node>`` method exists. The evidence program
    compiles cleanly through the restricted compiler and, where the node can
    touch names, attributes, calls or iteration, asserts that an existing
    guard (``_getiter_``, ``_getattr_``, ``_getitem_``, ``_apply_``,
    ``_write_``, ``_inplacevar_`` ...) appears in the transformed tree.

``denied``
    An explicit ``visit_<Node>`` method calls ``self.not_allowed(node)`` (or
    otherwise records an error). The evidence program is valid Python for the
    native parser, but restricted compilation returns the expected error and
    no code object.

``unhandled``
    No visitor exists, so
    :meth:`RestrictingNodeTransformer.generic_visit` rejects the node by
    default. The evidence asserts both the "not known" warning and the
    "not allowed" error, proving the default-deny safety net really fires.

Classes the parser never instantiates (abstract ASDL bases, deprecated
aliases and version stubs) are listed in ``NOT_PARSER_EMITTED`` together with
the reason for the exemption, instead of being treated as covered syntax.

The node list itself is taken from :mod:`ast` at runtime. Nodes added in a
specific Python version carry a ``VersionCondition`` with the reason; they are
skipped, never silently counted, on interpreters that do not provide them.
This avoids hard-coding a permanent snapshot of class names while still
requiring a decision for every class an interpreter can actually produce.

.. _new_node_checklist:

Adding a node: what evidence to add
-----------------------------------

Suppose a future Python adds an ``ast.FutureExpr`` node. Running the test
suite fails with an actionable message::

    RestrictedPython AST node coverage is incomplete for Python 3.xx:
      - FutureExpr: no NodePolicy registered; default rejection happens in
        .../transformer.py:NNN
        (RestrictingNodeTransformer.generic_visit). Add a NodePolicy with
        compilable evidence to tests/ast_coverage/policy.py.

The node stays safely rejected until a decision is recorded. To record one:

1. Write a *natively valid* minimal program containing the node. A program
   the native parser rejects proves nothing about RestrictedPython, so such
   cases belong in ``NOT_PARSER_EMITTED`` instead.

2. If the node must stay blocked, add an explicit ``visit_FutureExpr`` that
   calls ``self.not_allowed(node)`` and register a ``denied`` entry::

       _denied(
           'FutureExpr',
           'reason this feature is unsafe; visit_FutureExpr calls '
           'not_allowed()',
           [_ev(
               'result = future expression\n',
               error='FutureExpr statements are not allowed.')],
       )

3. If the node is allowed, register an ``allowed`` entry whose evidence shows
   the guard that protects it. A node with no guard of its own (pure syntax)
   still has to compile cleanly, while anything iterating, indexing,
   attributing or calling must name the injected guard::

       _allowed(
           'FutureExpr',
           'iteration inside FutureExpr is wrapped via _getiter_',
           [_ev('result = <future expr over items>\n',
                guards=('_getiter_',))],
       )

4. If the node only exists from a certain Python version, pass a
   ``VersionCondition(added_in=(3, xx), reason=...)`` so the entry is skipped
   on older interpreters.

The data-driven tests in
``tests/ast_coverage/test_node_coverage.py`` then compile every registered
program and fail if an allowed path loses its guard or a rejected node stops
producing an error. The boundary tests in
``tests/ast_coverage/test_edge_cases.py`` cover scopes, formatted strings,
generators, exception groups and async syntax in the same way.
