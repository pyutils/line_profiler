"""
Tools for eagerly pre-importing everything as specified in
``line_profiler.autoprof.run(prof_mod=...)``.
"""
import ast
import functools
import itertools
from collections.abc import Collection
from keyword import iskeyword
from importlib.util import find_spec
from os.path import isdir
from pkgutil import walk_packages
from textwrap import dedent, indent as indent_
from warnings import warn
from .util_static import modname_to_modpath


def is_dotted_path(obj):
    """
    Example:
        >>> assert not is_dotted_path(object())
        >>> assert is_dotted_path('foo')
        >>> assert is_dotted_path('foo.bar')
        >>> assert not is_dotted_path('not an identifier')
        >>> assert not is_dotted_path('keyword.return.not.allowed')
    """
    if not (isinstance(obj, str) and obj):
        return False
    for chunk in obj.split('.'):
        if iskeyword(chunk) or not chunk.isidentifier():
            return False
    return True


def get_expression(obj):
    """
    Example:
        >>> assert not get_expression(object())
        >>> assert not get_expression('')
        >>> assert not get_expression('foo; bar')
        >>> assert get_expression('foo')
        >>> assert get_expression('lambda x: x')
        >>> assert not get_expression('def foo(x): return x')
    """
    if not (isinstance(obj, str) and obj):
        return None
    try:
        return ast.parse(obj, mode='eval')
    except SyntaxError:
        return None


def split_dotted_path(dotted_path):
    """
    Arguments:
        dotted_path (str):
            Dotted path indicating an import target (module, package, or
            a ``from ... import ...``-able name under that), or an
            object accessible via (chained) attribute access thereon

    Returns:
        module, target (tuple[str, Union[str, None]]):

        * ``module``: dotted path indicating the module that should be
          imported
        * ``target``: dotted path indicating the chained-attribute
          access target on the imported module corresponding to
          ``dotted_path``;
          if the import is just a module, this is set to
          :py:const:`None`

    Raises:
        TypeError
            If ``dotted_path`` is not a dotted path (Python identifiers
            joined by periods)
        ModuleNotFoundError
            If a matching module cannot be found

    Example:
        >>> split_dotted_path('importlib.util.find_spec')
        ('importlib.util', 'find_spec')
        >>> split_dotted_path('importlib.util')
        ('importlib.util', None)
        >>> split_dotted_path('importlib.abc.Loader.exec_module')
        ('importlib.abc', 'Loader.exec_module')
        >>> split_dotted_path(  # doctest: +NORMALIZE_WHITESPACE
        ...     'not a dotted path')
        Traceback (most recent call last):
          ...
        TypeError: dotted_path = 'not a dotted path':
        expected a dotted path (string of period-joined identifiers)
        >>> split_dotted_path(  # doctest: +NORMALIZE_WHITESPACE
        ...     'foo.bar.baz')
        Traceback (most recent call last):
          ...
        ModuleNotFoundError: dotted_path = 'foo.bar.baz':
        none of the below looks like an importable module:
        ['foo.bar.baz', 'foo.bar', 'foo']
    """
    if not is_dotted_path(dotted_path):
        raise TypeError(f'dotted_path = {dotted_path!r}: '
                        'expected a dotted path '
                        '(string of period-joined identifiers)')
    chunks = dotted_path.split('.')
    checked_locs = []
    for slicing_point in range(len(chunks), 0, -1):
        module = '.'.join(chunks[:slicing_point])
        target = '.'.join(chunks[slicing_point:]) or None
        try:
            spec = find_spec(module)
        except ImportError:
            spec = None
        if spec is None:
            checked_locs.append(module)
            continue
        return module, target
    raise ModuleNotFoundError(f'dotted_path = {dotted_path!r}: '
                              'none of the below looks like an importable '
                              f'module: {checked_locs!r}')


def strip(s):
    return dedent(s).strip('\n')


class LoadedNameFinder(ast.NodeVisitor):
    """
    Find the names loaded in an AST. A name is considered to be loaded
    if it appears with the context :py:class:`ast.Load()` and is not an
    argument of any surrounding function-definition contexts
    (``def func(...): ...``, ``async def func(...): ...``, or
    ``lambda ...: ...``).

    Example:
        >>> import ast
        >>>
        >>>
        >>> module = '''
        ... def foo(x, **k):
        ...     def bar(y, **z):
        ...         pass
        ...
        ...     return bar(x, **{**k, 'baz': foobar})
        ...
        ... spam = lambda x, *y, **z: (x, y, z, a)
        ...
        ... str('ham')
        ... '''
        >>> names = LoadedNameFinder.find(ast.parse(module))
        >>> assert names == {'bar', 'foobar', 'a', 'str'}, names
    """
    def __init__(self):
        self.names = set()
        self.contexts = []

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return
        name = node.id
        if not any(name in ctx for ctx in self.contexts):
            self.names.add(node.id)

    def _visit_func_def(self, node):
        args = node.args
        arg_names = {
            arg.arg
            for arg_list in (args.posonlyargs, args.args, args.kwonlyargs)
            for arg in arg_list}
        if args.vararg:
            arg_names.add(args.vararg.arg)
        if args.kwarg:
            arg_names.add(args.kwarg.arg)
        self.contexts.append(arg_names)
        self.generic_visit(node)
        self.contexts.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _visit_func_def

    @classmethod
    def find(cls, node):
        finder = cls()
        finder.visit(node)
        return finder.names


def propose_names(prefixes):
    """
    Generate names based on prefixes.

    Arguments:
        prefixes (Collection[str]):
            String identifier prefixes

    Yields:
        name (str):
            String identifier

    Example:
        >>> import itertools
        >>>
        >>>
        >>> list(itertools.islice(propose_names(['func', 'f', 'foo']),
        ...                       10))  # doctest: +NORMALIZE_WHITESPACE
        ['func', 'f', 'foo',
         'func_0', 'f0', 'foo_0',
         'func_1', 'f1', 'foo_1',
         'func_2']
    """
    prefixes = list(dict.fromkeys(prefixes))  # Preserve order
    if not all(is_dotted_path(p) and '.' not in p for p in prefixes):
        raise TypeError(f'prefixes = {prefixes!r}: '
                        'expected string identifiers')
    # Yield all the provided prefixes
    yield from prefixes
    # Yield the prefixes in order with numeric suffixes
    prefixes_and_patterns = [
        (prefix, ('{}{}' if len(prefix) == 1 else '{}_{}').format)
        for prefix in prefixes]
    for i in itertools.count():
        for prefix, pattern in prefixes_and_patterns:
            yield pattern(prefix, i)


def write_eager_import_module(dotted_paths, stream=None, *,
                              recurse=False,
                              adder='profile.add_imported_function_or_module',
                              indent='    '):
    r"""
    Write a module which autoprofiles all its imports.

    Arguments:
        dotted_paths (Collection[str]):
            Dotted paths (strings of period-joined identifiers)
            indicating what should be profiled
        stream (Union[TextIO, None]):
            Optional text-mode writable file object to which to write
            the module
        recurse (Union[Collection[str], bool]):
            Dotted paths (strings of period-joined identifiers)
            indicating the profiling targets that should be recursed
            into if they are packages;
            can also be a boolean value, indicating:

            :py:const:`True`
                Recurse into any entry in ``dotted_paths`` that is a
                package
            :py:const:`False`
                Don't recurse into any entry
        adder (str):
            Single-line string ``ast.parse(mode='eval')``-able to a
            single expression, indicating the callable (which is assumed
            to exist in the builtin namespace by the time the module is
            executed) to be called to add the profiling target
        indent (str):
            Single-line, non-empty whitespace string to indent the
            output with

    Side effects:
        * ``stream`` (or :py:data:`sys.stdout` if :py:const:`None`)
          written to
        * Warning issued if the module can't be located for one or more
          dotted paths

    Raises:
        TypeError
            * If ``adder`` and ``indent`` are not strings
            * If ``dotted_paths`` is not a collection of dotted paths
        ValueError
            * If ``adder`` is a non-single-line string or is not
              parsable to a single expression
            * If ``indent`` isn't single-line, non-empty, and
              whitespace

    Example:
        >>> import io
        >>> import textwrap
        >>> import warnings
        >>>
        >>>
        >>> def strip(s):
        ...     return textwrap.dedent(s).strip('\n')
        ...
        >>>
        >>> with warnings.catch_warnings(record=True) as record:
        ...     with io.StringIO() as sio:
        ...         write_eager_import_module(
        ...             ['importlib.util',
        ...              'foo.bar',
        ...              'importlib.abc.Loader.exec_module',
        ...              'importlib.abc.Loader.find_module'],
        ...             sio)
        ...         written = strip(sio.getvalue())
        ...
        >>> assert written == strip('''
        ... add = profile.add_imported_function_or_module
        ... failures = []
        ...
        ... try:
        ...     import importlib.abc as module
        ... except ImportError:
        ...     pass
        ... else:
        ...     try:
        ...         add(module.Loader.exec_module)
        ...     except AttributeError:
        ...         failures.append('importlib.abc.Loader.exec_module')
        ...     try:
        ...         add(module.Loader.find_module)
        ...     except AttributeError:
        ...         failures.append('importlib.abc.Loader.find_module')
        ...
        ... try:
        ...     import importlib.util as module
        ... except ImportError:
        ...     pass
        ... else:
        ...     add(module)
        ...
        ...
        ... if failures:
        ...     import warnings
        ...
        ...     msg = '{} target{} cannot be imported: {!r}'.format(
        ...         len(failures),
        ...         '' if len(failures) == 1 else 's',
        ...         failures)
        ...     warnings.warn(msg, stacklevel=2)
        ... '''), written
        >>> assert len(record) == 1
        >>> assert (record[0].message.args[0]
        ...         == ("1 import target cannot be resolved: "
        ...             "['foo.bar']"))
    """
    if not isinstance(adder, str):
        AdderError = TypeError
    elif len(adder.splitlines()) != 1:
        AdderError = ValueError
    else:
        expr = get_expression(adder)
        if expr:
            AdderError = None
        else:
            AdderError = ValueError
    if AdderError:
        raise AdderError(f'adder = {adder!r}: '
                         'expected a single-line string parsable to a single '
                         'expression')
    if not isinstance(indent, str):
        IndentError = TypeError
    elif len(indent.splitlines()) == 1 and indent.isspace():
        IndentError = None
    else:
        IndentError = ValueError
    if IndentError:
        raise IndentError(f'indent = {indent!r}: '
                          'expected a single-line non-empty whitespace string')

    # Get the names loaded by `adder`;
    # these names are not allowed in the namespace
    forbidden_names = LoadedNameFinder.find(expr)
    # We need three free names:
    # - One for `adder`
    # - One for a list of failed targets
    # - One for the imported module
    adder_name = next(
        name for name in propose_names(['add', 'add_func', 'a', 'f'])
        if name not in forbidden_names)
    forbidden_names.add(adder_name)
    failures_name = next(
        name
        for name in propose_names(['failures', 'failed_targets', 'f', '_'])
        if name not in forbidden_names)
    forbidden_names.add(failures_name)
    module_name = next(
        name for name in propose_names(['module', 'mod', 'imported', 'm', '_'])
        if name not in forbidden_names)

    # Figure out the import targets to profile
    dotted_paths = set(dotted_paths)
    if isinstance(recurse, Collection):
        recurse = set(recurse)
    else:
        recurse = dotted_paths if recurse else set()
    dotted_paths |= recurse

    imports = {}
    unknown_locs = []
    for path in sorted(set(dotted_paths)):
        try:
            module, target = split_dotted_path(path)
        except ModuleNotFoundError:
            unknown_locs.append(path)
            continue
        if path in recurse and target is None:
            recurse_root = modname_to_modpath(path, hide_init=True)
            if recurse_root and not isdir(recurse_root):
                recurse_root = None
        else:  # Not a recurse target nor a module
            recurse_root = None
        imports.setdefault(module, set()).add(target)
        # FIXME: how do we handle namespace packages?
        if recurse_root is not None:
            for info in walk_packages([recurse_root], prefix=module + '.'):
                imports.setdefault(info.name, set()).add(None)

    # Warn against failed imports
    if unknown_locs:
        msg = '{} import target{} cannot be resolved: {!r}'.format(
            len(unknown_locs),
            '' if len(unknown_locs) == 1 else 's',
            unknown_locs)
        warn(msg, stacklevel=2)

    # Do the imports and add them with `adder`
    write = functools.partial(print, file=stream)
    write(f'{adder_name} = {adder}\n{failures_name} = []')
    for module, targets in imports.items():
        assert targets
        write('\n'
              + strip(f"""
            try:
            {indent}import {module} as {module_name}
            except ImportError:
            {indent}pass
            else:
                """))
        chunks = []
        try:
            targets.remove(None)
        except KeyError:  # Not found
            pass
        else:  # Add the whole module
            chunks.append(f'{adder_name}({module_name})')
        for target in sorted(targets):
            path = f'{module}.{target}'
            chunks.append(strip(f"""
            try:
            {indent}{adder_name}({module_name}.{target})
            except AttributeError:
            {indent}{failures_name}.append({path!r})
            """))
        for chunk in chunks:
            write(indent_(chunk, indent))
    # Issue a warning if any of the targets doesn't exist
    if imports:
        write('\n')
        write(strip(f"""
        if {failures_name}:
        {indent}import warnings

        {indent}msg = '{{}} target{{}} cannot be imported: {{!r}}'.format(
        {indent * 2}len({failures_name}),
        {indent * 2}'' if len({failures_name}) == 1 else 's',
        {indent * 2}{failures_name})
        {indent}warnings.warn(msg, stacklevel=2)
        """))
