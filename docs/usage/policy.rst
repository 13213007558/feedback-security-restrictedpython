.. _policy_builtins:

Policies & builtins
-------------------

RestrictedPython provides a way to define policies, by redefining restricted versions of ``print``, ``getattr``, ``setattr``, ``import``, etc..
As shortcuts it offers three stripped down versions of Python's ``__builtins__``:

.. _predefined_builtins:

Predefined builtins
...................

``safe_builtins``
    a safe set of builtin modules and functions
``limited_builtins``
    restricted sequence types (e. g. ``range``, ``list`` and ``tuple``)
``utility_builtins``
    access to standard modules like math, random, string and set.

``safe_globals`` is a shortcut for ``{'__builtins__': safe_builtins}`` as this
is the way globals have to be provided to the `exec` function to actually
restrict the access to the builtins provided by Python.

Guards
......

.. todo::

    Describe Guards and predefined guard methods in details

RestrictedPython predefines several guarded access and manipulation methods:

* ``safer_getattr``
* ``guarded_setattr``
* ``guarded_delattr``
* ``guarded_iter_unpack_sequence``
* ``guarded_unpack_sequence``

Those and additional methods rely on a helper construct ``full_write_guard``, which is intended to help implement immutable and semi mutable objects and attributes.

.. todo::

    Describe full_write_guard more in detail and how it works.

Implementing a policy
---------------------

RestrictedPython only provides the raw material for restricted execution.
To actually enforce any restrictions, you need to supply a policy
implementation by providing restricted versions of ``print``,
``getattr``, ``setattr``, ``import``, etc.  These restricted
implementations are hooked up by providing a set of specially named
objects in the global dict that you use for execution of code.
Specifically:

1. ``_print_`` is a callable object that returns a handler for print
   statements.  This handler must have a ``write()`` method that
   accepts a single string argument, and must return a string when
   called. ``RestrictedPython.PrintCollector.PrintCollector`` is a
   suitable implementation.

2. ``_write_`` is a guard function taking a single argument.  If the
   object passed to it may be written to, it should be returned,
   otherwise the guard function should raise an exception.  ``_write_``
   is typically called on an object before a ``setattr`` operation.

3. ``_getattr_`` and ``_getitem_`` are guard functions, each of which
   takes two arguments.  The first is the base object to be accessed,
   while the second is the attribute name or item index that will be
   read.  The guard function should return the attribute or subitem,
   or raise an exception.
   RestrictedPython ships with a default implementation
   for ``_getattr_`` which prevents the following actions:

   * accessing an attribute whose name start with an underscore
   * accessing the format method of strings as this is considered harmful.

4. ``__import__`` is the normal Python import hook, and should be used
   to control access to Python packages and modules.

   .. warning::

      RestrictedPython ships no ``__import__`` implementation.
      Provide one and you move the security boundary out of RestrictedPython and
      into your own import policy — read :ref:`security_considerations` first.

5. ``__builtins__`` is the normal Python builtins dictionary, which
   should be weeded down to a set that cannot be used to get around
   your restrictions.  A usable "safe" set is
   ``RestrictedPython.Guards.safe_builtins``.

.. _with_guard:

Guarding ``with`` statements with ``_with_``
...........................................

A bare ``with`` statement calls ``__enter__`` on the object of the context
expression without consulting any guard.  RestrictedPython therefore ships an
opt-in policy, :class:`RestrictedPython.RestrictingWithGuardNodeTransformer`,
which routes every context expression of a *synchronous* ``with`` statement
through a host supplied ``_with_`` callable::

    with <expression> as target:
        ...

is compiled as if it had been written::

    with _with_(<expression>) as target:
        ...

``_with_`` is an admission check, nothing more:

* it receives the object the context expression evaluated to, *before*
  ``__enter__`` is called;
* it returns the context manager that may actually be entered — either the
  original object or a host controlled wrapper, for example a checked proxy;
* or it raises an exception to refuse the object.  The exception propagates
  before ``__enter__`` runs, and context managers entered earlier in the same
  statement are exited in the usual reverse order.

The default :class:`~RestrictedPython.RestrictingNodeTransformer` does not
insert these calls, so existing compilations keep their behaviour.  The
strategy only changes how the ``with`` statement is compiled; ``as`` bindings,
multi-item evaluation order, unpacking, exception suppression and
``return``/``break``/``continue`` unwinding stay handled by CPython itself.
``async with`` is not covered and remains a compile error.

.. warning::

   Passing an object through ``_with_`` does **not** turn an arbitrary
   untrusted object into a safe one.  The guard only enforces whatever
   admission rule the host defines; the host remains fully responsible for the
   objects and wrappers it returns and for everything reachable through them.
   See :ref:`security_considerations`.

The name ``_with_`` follows the same convention as the other guards: scripts
cannot define or rebind it themselves, because names starting with an
underscore are rejected.

.. testcode:: with_guard

    from RestrictedPython import compile_restricted_exec
    from RestrictedPython import RestrictingWithGuardNodeTransformer
    from RestrictedPython.Guards import safer_getattr

    class Denied(RuntimeError):
        pass

    class Resource:
        def __init__(self, name, allowed):
            self.name = name
            self.allowed = allowed
            self.events = []

        def __enter__(self):
            self.events.append('enter ' + self.name)
            return self

        def __exit__(self, exc_type, exc, tb):
            self.events.append('exit ' + self.name)
            return False

    def with_guard(obj):
        if not obj.allowed:
            raise Denied(f'{obj.name} is not allowed as a context manager')
        return obj

    source = """
    def use(resource):
        with resource as entered:
            return entered.name
    """

    result = compile_restricted_exec(
        source, policy=RestrictingWithGuardNodeTransformer)
    assert result.errors == ()
    assert result.used_names['_with_'] is True

    safe_globals = {'_with_': with_guard, '_getattr_': safer_getattr}
    exec(result.code, safe_globals)

    allowed_resource = Resource('allowed', True)
    print(safe_globals['use'](allowed_resource))
    print(allowed_resource.events)

.. testoutput:: with_guard

    allowed
    ['enter allowed', 'exit allowed']

When the guard refuses, the rejected object's ``__enter__`` never runs:

.. testcode:: with_guard

    refused_resource = Resource('refused', False)
    try:
        safe_globals['use'](refused_resource)
    except Denied as error:
        print(error)
    print(refused_resource.events)

.. testoutput:: with_guard

    refused is not allowed as a context manager
    []

To help illustrate how this works under the covers, here's an example
function:

.. code-block:: python

    def f(x):
        x.foo = x.foo + x[0]
        print x
        return printed

and (sort of) how it looks after restricted compilation:

.. code-block:: python

    def f(x):
        # Make local variables from globals.
        _print = _print_()
        _write = _write_
        _getattr = _getattr_
        _getitem = _getitem_

        # Translation of f(x) above
        _write(x).foo = _getattr(x, 'foo') + _getitem(x, 0)
        print >>_print, x
        return _print()

Examples
--------

``print``
.........

To support the ``print`` statement in restricted code, we supply a
``_print_`` object (note that it's a *factory*, e.g. a class or a
callable, from which the restricted machinery will create the object):

.. code-block:: pycon

    >>> from RestrictedPython.PrintCollector import PrintCollector
    >>> _print_ = PrintCollector
    >>> _getattr_ = getattr

    >>> src = '''
    ... print("Hello World!")
    ... '''
    >>> code = compile_restricted(src, '<string>', 'exec')
    >>> exec(code)

As you can see, the text doesn't appear on stdout.  The print
collector collects it.  We can have access to the text using the
``printed`` variable, though:

.. code-block:: pycon

    >>> src = '''
    ... print("Hello World!")
    ... result = printed
    ... '''
    >>> code = compile_restricted(src, '<string>', 'exec')
    >>> exec(code)

    >>> result
    'Hello World!\n'

Built-ins
.........

By supplying a different ``__builtins__`` dictionary, we can rule out
unsafe operations, such as opening files:

.. code-block:: pycon

    >>> from RestrictedPython.Guards import safe_builtins
    >>> restricted_globals = dict(__builtins__=safe_builtins)

    >>> src = '''
    ... open('/etc/passwd')
    ... '''
    >>> code = compile_restricted(src, '<string>', 'exec')
    >>> exec(code, restricted_globals)
    Traceback (most recent call last):
      ...
    NameError: name 'open' is not defined
