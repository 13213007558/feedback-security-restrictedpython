##############################################################################
#
# Copyright (c) 2002 Zope Foundation and Contributors.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
#
##############################################################################
"""
match_policy module:

An opt-in policy which supports a restricted subset of the Python
structural pattern matching syntax (``match`` / ``case``).

The :class:`RestrictedMatchNodeTransformer` never lets a raw ``Match``
AST node reach the compiler.  Every pattern is instead converted into a
plain data specification which is evaluated at runtime by
``RestrictedPython.Guards.guarded_match_pattern``.  All iteration,
subscripting and attribute access performed while matching goes through
the corresponding ``_getiter_``, ``_getitem_`` and ``_getattr_`` guards.

Only constant/value, capture, OR, sequence and mapping patterns are
supported.  Class patterns are rejected at compile time.

Enabling this policy is *not* a process isolation sandbox: it only
extends the compile time restrictions, the guards are still supplied by
the calling application.
"""

from __future__ import annotations

import ast
import typing

from RestrictedPython.transformer import RestrictingNodeTransformer
from RestrictedPython.transformer import copy_locations


# Tags of the tuples which make up the runtime match specification.
_WILDCARD = 'wildcard'
_CAPTURE = 'capture'
_VALUE = 'value'
_SINGLETON = 'singleton'
_AS = 'as'
_OR = 'or'
_SEQUENCE = 'sequence'
_STAR = 'star'
_MAPPING = 'mapping'


class RestrictedMatchNodeTransformer(RestrictingNodeTransformer):
    """Policy allowing a restricted subset of pattern matching.

    A ``match`` statement is transformed into an ``if`` / ``elif`` chain
    which calls the ``_match_pattern_`` runtime helper with a data
    specification.  The subject expression is evaluated exactly once and
    every protocol access the matching performs is routed through a
    guard.
    """

    # -- statement -----------------------------------------------------

    def visit_Match(self, node: ast.Match) -> list[ast.stmt]:
        # The subject is evaluated exactly once and stored in a
        # temporary local.
        subject = self.visit(node.subject)
        subject_tmp = self.gen_tmp_name()
        assign = ast.Assign(
            targets=[ast.Name(subject_tmp, ast.Store())],
            value=subject)
        copy_locations(assign, node)

        statements: list[ast.stmt] = [assign]
        # Each case becomes an ``if``; later cases live in the previous
        # one's ``orelse``, reproducing the first-match-wins semantics.
        branch_orelse = statements
        for case in node.cases:
            spec = self._compile_pattern(case.pattern)
            bindings = sorted(set(self._capture_names(case.pattern)))
            bindings_tmp = self.gen_tmp_name()

            matcher_call = ast.Call(
                func=ast.Name('_match_pattern_', ast.Load()),
                args=[
                    ast.Name(subject_tmp, ast.Load()),
                    spec,
                    ast.Name('_getiter_', ast.Load()),
                    ast.Name('_getitem_', ast.Load()),
                    ast.Name('_getattr_', ast.Load()),
                ],
                keywords=[])
            copy_locations(matcher_call, case.pattern)
            # The matcher is invoked exactly once per case, its result
            # (``None`` or the bindings mapping) is kept in a temporary.
            bind_assign = ast.Assign(
                targets=[ast.Name(bindings_tmp, ast.Store())],
                value=matcher_call)
            copy_locations(bind_assign, case.pattern)
            test = ast.Compare(
                left=ast.Name(bindings_tmp, ast.Load()),
                ops=[ast.IsNot()],
                comparators=[ast.Constant(None)])
            copy_locations(test, case.pattern)

            # Bindings are committed as soon as the pattern matched,
            # mirroring CPython: a false guard does not roll them back.
            body = self._gen_commit(
                bindings, bindings_tmp, case.pattern)
            # Later cases are reached both when the pattern does not
            # match (outer ``orelse``) and when a false guard skips the
            # case body.  Both share this single mutable list object.
            continuation: list[ast.stmt] = []
            if case.guard is not None:
                guard = self.visit(case.guard)
                assert isinstance(guard, ast.expr)
                guarded_body = self._visit_body(case.body)
                if not guarded_body:
                    # The grammar guarantees a non-empty case body.
                    guarded_body = [ast.Pass()]  # pragma: no cover
                # A false guard falls through to the continuation, which
                # is the same list the outer ``if`` uses as its orelse.
                body.append(ast.If(
                    test=guard,
                    body=guarded_body,
                    orelse=continuation))
            else:
                body.extend(self._visit_body(case.body))

            if_stmt = ast.If(
                test=test, body=body, orelse=continuation)
            copy_locations(if_stmt, node)
            branch_orelse.extend([bind_assign, if_stmt])
            branch_orelse = continuation

        # Recursively fill in locations on every generated node.  Using
        # the original ``Match`` node as the anchor also covers nested
        # matches, whose generated statements are embedded here.
        for stmt in statements:
            self._fill_locations(stmt, node)
        return statements

    def _fill_locations(
            self, current: ast.AST, anchor: ast.AST) -> None:
        if getattr(current, 'lineno', None) is None:
            current.lineno = anchor.lineno
            current.end_lineno = anchor.end_lineno
            current.col_offset = anchor.col_offset
            current.end_col_offset = anchor.end_col_offset
        for child in ast.iter_child_nodes(current):
            self._fill_locations(child, anchor)

    def _visit_body(
            self, body: list[ast.stmt]) -> list[ast.stmt]:
        # A nested ``match`` expands into several statements, so the
        # result has to be flattened instead of being nested as a single
        # list element.
        visited: list[ast.stmt] = []
        for stmt in body:
            result = self.visit(stmt)
            if isinstance(result, list):
                visited.extend(
                    typing.cast(list[ast.stmt], result))
            else:
                visited.append(typing.cast(ast.stmt, result))
        return visited

    def _gen_commit(
            self,
            bindings: list[str],
            bindings_tmp: str,
            location: ast.AST) -> list[ast.stmt]:
        if not bindings:
            return []
        # Unpack positionally: binding names are sorted and the value
        # tuple is built in the same order.
        target = ast.Tuple(
            elts=[ast.Name(name, ast.Store()) for name in bindings],
            ctx=ast.Store())
        value = ast.Tuple(
            elts=[
                ast.Subscript(
                    value=ast.Name(bindings_tmp, ast.Load()),
                    slice=ast.Constant(name),
                    ctx=ast.Load())
                for name in bindings],
            ctx=ast.Load())
        commit = ast.Assign(targets=[target], value=value)
        copy_locations(commit, location)
        return [commit]

    # -- patterns ------------------------------------------------------

    def visit_MatchClass(self, node: ast.MatchClass) -> ast.AST:
        # Class patterns perform arbitrary ``isinstance`` checks and
        # attribute access which cannot be expressed with the supported
        # guards yet, therefore they stay rejected.
        self.error(
            node,
            'Class patterns (case Class(...)) are not allowed in '
            'restricted pattern matching.')
        return node

    def _compile_pattern(self, node: ast.pattern) -> ast.expr:
        """Convert a pattern node into a runtime data specification."""
        if isinstance(node, ast.MatchAs):
            return self._compile_match_as(node)
        if isinstance(node, ast.MatchValue):
            return self._compile_match_value(node)
        if isinstance(node, ast.MatchSingleton):
            spec = ast.Tuple(
                elts=[
                    ast.Constant(_SINGLETON),
                    ast.Constant(node.value),
                ],
                ctx=ast.Load())
            copy_locations(spec, node)
            return spec
        if isinstance(node, ast.MatchOr):
            return self._compile_match_or(node)
        if isinstance(node, ast.MatchSequence):
            return self._compile_match_sequence(node)
        if isinstance(node, ast.MatchMapping):
            return self._compile_match_mapping(node)
        if isinstance(node, ast.MatchClass):
            # ``visit_MatchClass`` records the error; it is invoked
            # explicitly because pattern nodes are compiled by dispatch
            # here rather than through the generic ``visit`` machinery.
            self.visit_MatchClass(node)
            return ast.Constant(None)
        # Defensive: a future Python version could add a new pattern
        # node which must be rejected until it is reviewed.
        self.not_allowed(node)  # pragma: no cover
        return ast.Constant(None)  # pragma: no cover

    def _compile_match_as(self, node: ast.MatchAs) -> ast.expr:
        if node.pattern is None:
            if node.name is None:
                spec: ast.expr = ast.Tuple(
                    elts=[ast.Constant(_WILDCARD)], ctx=ast.Load())
            else:
                self.check_name(node, node.name)
                spec = ast.Tuple(
                    elts=[
                        ast.Constant(_CAPTURE),
                        ast.Constant(node.name),
                    ],
                    ctx=ast.Load())
            copy_locations(spec, node)
            return spec

        inner = self._compile_pattern(node.pattern)
        if node.name is None:  # pragma: no cover
            # The parser only produces a nested pattern together with an
            # ``as`` name, so this branch is unreachable from source.
            return inner
        self.check_name(node, node.name)
        spec = ast.Tuple(
            elts=[
                ast.Constant(_AS),
                inner,
                ast.Constant(node.name),
            ],
            ctx=ast.Load())
        copy_locations(spec, node)
        return spec

    def _compile_match_value(self, node: ast.MatchValue) -> ast.expr:
        # The value expression may contain subscript/attribute access
        # which has to pass the usual guards.
        value = self.visit(node.value)
        spec = ast.Tuple(
            elts=[ast.Constant(_VALUE), value], ctx=ast.Load())
        copy_locations(spec, node)
        return spec

    def _compile_match_or(self, node: ast.MatchOr) -> ast.expr:
        alternative_names: set[str] | None = None
        alternative_specs = []
        for pattern in node.patterns:
            alternative_specs.append(self._compile_pattern(pattern))
            names = {
                name for name in self._capture_names(pattern)
                if name != '_'
            }
            if alternative_names is None:
                alternative_names = names
            elif names != alternative_names:
                self.error(
                    node,
                    'All alternatives of an OR pattern must bind the '
                    'same set of names.')
        spec = ast.Tuple(
            elts=[
                ast.Constant(_OR),
                ast.List(elts=alternative_specs, ctx=ast.Load()),
            ],
            ctx=ast.Load())
        copy_locations(spec, node)
        return spec

    def _compile_match_sequence(
            self, node: ast.MatchSequence) -> ast.expr:
        seen: set[str] = set()
        star_seen = False
        element_specs = []
        for pattern in node.patterns:
            self._check_duplicate_bindings(pattern, seen)
            if isinstance(pattern, ast.MatchStar):
                if star_seen:
                    self.error(
                        pattern,
                        'A sequence pattern may contain at most one '
                        'starred pattern.')
                star_seen = True
                if pattern.name is not None:
                    self.check_name(pattern, pattern.name)
                star_spec = ast.Tuple(
                    elts=[
                        ast.Constant(_STAR),
                        ast.Constant(pattern.name),
                    ],
                    ctx=ast.Load())
                copy_locations(star_spec, pattern)
                element_specs.append(star_spec)
            else:
                element_specs.append(self._compile_pattern(pattern))
        spec = ast.Tuple(
            elts=[
                ast.Constant(_SEQUENCE),
                ast.List(elts=element_specs, ctx=ast.Load()),
            ],
            ctx=ast.Load())
        copy_locations(spec, node)
        return spec

    def _compile_match_mapping(
            self, node: ast.MatchMapping) -> ast.expr:
        seen: set[str] = set()
        seen_keys: list[object] = []
        key_specs = []
        value_specs = []
        for key_node, value_pattern in zip(node.keys, node.patterns):
            self._check_duplicate_bindings(value_pattern, seen)
            if isinstance(key_node, ast.Constant):
                if key_node.value in seen_keys:
                    self.error(
                        key_node,
                        f'mapping pattern checks duplicate key '
                        f'({key_node.value!r}).')
                seen_keys.append(key_node.value)
            key_specs.append(self.visit(key_node))
            value_specs.append(self._compile_pattern(value_pattern))
        if node.rest is not None:
            self.check_name(node, node.rest)
        spec = ast.Tuple(
            elts=[
                ast.Constant(_MAPPING),
                ast.List(elts=key_specs, ctx=ast.Load()),
                ast.List(elts=value_specs, ctx=ast.Load()),
                ast.Constant(node.rest),
            ],
            ctx=ast.Load())
        copy_locations(spec, node)
        return spec

    # -- helpers -------------------------------------------------------

    def _check_duplicate_bindings(
            self, node: ast.pattern, seen: set[str]) -> None:
        # Bare wildcards (``MatchAs``/``MatchStar`` without a name) are
        # not yielded by ``_capture_names``, so every name reported here
        # is a real capture that must be unique within the pattern.
        for name in self._capture_names(node):
            if name in seen:
                self.error(
                    node,
                    f"multiple assignments to name '{name}' in pattern.")
            seen.add(name)

    def _capture_names(self, node: ast.pattern) -> typing.Iterator[str]:
        for child in ast.walk(node):
            if isinstance(child, ast.MatchAs) and child.name is not None:
                yield child.name
            elif (isinstance(child, ast.MatchStar)
                  and child.name is not None):
                yield child.name
            elif (isinstance(child, ast.MatchMapping)
                  and child.rest is not None):
                yield child.rest
