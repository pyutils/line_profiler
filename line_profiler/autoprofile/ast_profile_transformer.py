from __future__ import annotations

import ast
from collections.abc import (
    Callable, Collection, Mapping, MutableSequence, Sequence,
)
from os import PathLike
from types import MappingProxyType
from typing import Any, Protocol, TypeVar, cast, get_args
from warnings import warn

from .. import _diagnostics as diagnostics
from ..toml_config import ConfigSource
from ._import_targets import _DROPPED_STAR_IMPORTS_MSG_TEMPLATE, ImportTarget
from .profmod_extractor import (
    _CompoundNodeType, _CompoundStatement, _ImportFinder,
    _should_profile_star_imports,
)


_Import = TypeVar('_Import', ast.Import, ast.ImportFrom)

_PROFILE_IMPORTS_IN_DEFAULT: MappingProxyType[_CompoundNodeType, bool]
_PROFILE_IMPORTS_IN_DEFAULT = MappingProxyType(dict.fromkeys(
    get_args(_CompoundNodeType), True,
))


def ast_create_profile_node(
    modname: str,
    profiler_name: str = 'profile',
    attr: str = 'add_imported_function_or_module',
) -> ast.Expr:
    """
    Create an abstract syntax tree node that adds an object to the
    profiler to be profiled, by calling the ``attr`` method of
    ``profile`` and passing ``modname`` to it.

    At runtime, this adds the object to the profiler so it can be
    profiled. This node must be added after the first instance of
    ``modname`` in the AST and before it is used.

    The node will look like:
        >>> # xdoctest: +SKIP
        >>> import foo.bar
        >>> profile.add_imported_function_or_module(foo.bar)

    Args:
        modname (str):
            name of the imported module.

        profiler_name (str):
            name of the :py:class:`line_profiler.LineProfiler` object.

        attr (str):
            name of the method of the :py:class:`LineProfiler` object to
            call on the imported module.

    Returns:
        (_ast.Expr): expr
            AST node that adds ``modname`` to profiler.
    """
    func = ast.Attribute(
        value=ast.Name(id=profiler_name, ctx=ast.Load()),
        attr=attr,
        ctx=ast.Load(),
    )
    names = modname.split('.')
    value: ast.expr = ast.Name(id=names[0], ctx=ast.Load())
    for name in names[1:]:
        value = ast.Attribute(attr=name, ctx=ast.Load(), value=value)
    expr = ast.Expr(value=ast.Call(func=func, args=[value], keywords=[]))
    return expr


def ast_create_star_import_node(
    modname: str,
    targets: Collection[str] | None,
    profiler_name: str = 'profile',
    attr: str = 'add_star_import',
) -> ast.Expr:
    """
    AST node similar to that created by
    :py:func:`.ast_create_profile_node`, except that it handles
    star-imports (``from ... import *``), like:

    >>> # doctest: +SKIP
    >>> from foo.bar import *
    >>> profile.add_star_import(
    ...     'foo.bar', ['foo.bar', 'spam.ham'], locals(),
    ... )

    Args:
        modname (str):
            name of the imported module.

        targets (Collection[str] | None):
            profile-on-import module targets; if :py:const:`None`, all
            the names imported by the star-import will be added to the
            profiler.

        profiler_name (str):
            name of the :py:class:`line_profiler.LineProfiler` object.

        attr (str):
            name of the method of the
            :py:class:`line_profiler.LineProfiler` object to call on the
            imported module.

    Returns:
        (_ast.Expr): expr
            AST node that adds ``modname`` to profiler.
    """
    func_node = ast.Attribute(
        value=ast.Name(id=profiler_name, ctx=ast.Load()),
        attr=attr,
        ctx=ast.Load(),
    )
    modname_node = ast.Constant(value=modname)
    if targets is None:
        targets_node: ast.Constant | ast.List = ast.Constant(value=None)
    else:
        targets_node = ast.List(
            elts=[ast.Constant(value=t) for t in targets],
            ctx=ast.Load(),
        )
    namespace_node = ast.Call(
        func=ast.Name(id='locals', ctx=ast.Load()), args=[], keywords=[],
    )
    expr = ast.Expr(value=ast.Call(
        func=func_node,
        args=[modname_node, targets_node, namespace_node],
        keywords=[],
    ))
    return expr


def _ast_create_node_from_import_target(
    target: ImportTarget,
    modnames_to_profile: Collection[str] | None = None,
    profile_star_imports: bool = False,
) -> ast.Expr | None:
    if target.resolved_name is None:  # Star-imports
        if not profile_star_imports:
            return None
        assert target.name.endswith('.*')
        return ast_create_star_import_node(
            target.name[:-2], modnames_to_profile,
        )
    return ast_create_profile_node(target.resolved_name)


class _DuplicateChecker(Protocol):
    """
    Protocol for objects which helps with deduplication.
    """
    def should_profile_import(
        self, target: ImportTarget, context: Sequence[str | int], /,
    ) -> bool:
        ...

    def record_profiled_import(
        self, target: ImportTarget, context: Sequence[str | int], /,
    ) -> Any:
        ...


class _ContextAwareDuplicateChecker:
    """
    This checker is context-aware and only deduplicates imports of the
    same object in the same scope.
    """
    def __init__(
        self,
        profiled_imports: (
            Mapping[Sequence[str | int], Sequence[ImportTarget]] | None
        ) = None,
    ) -> None:
        pi: dict[
            tuple[str | int, ...], dict[int, list[ImportTarget]]
        ]
        self._profiled_imports = pi = {}
        for context, imports in (profiled_imports or {}).items():
            ctx, index = self._check_context(context)
            pi.setdefault(ctx, {})[index] = list(imports)

    def should_profile_import(
        self, target: ImportTarget, context: Sequence[str | int],
    ) -> bool:
        # Note: the `index` shouldn't be needed since we're already
        # going through the import targets in order
        ctx, _ = self._check_context(context)
        if ctx not in self._profiled_imports:
            return True
        ctx_profiled_names = {
            imp.name
            for imports in self._profiled_imports[ctx].values()
            for imp in imports
        }
        return target.name not in ctx_profiled_names

    def record_profiled_import(
        self, target: ImportTarget, context: Sequence[str | int],
    ) -> None:
        ctx, index = self._check_context(context)
        (
            self._profiled_imports
            .setdefault(ctx, {})
            .setdefault(index, [])
            .append(target)
        )

    @staticmethod
    def _check_context(
        context: Sequence[str | int],
    ) -> tuple[tuple[str | int, ...], int]:
        *ctx, index = context
        if not isinstance(index, int):
            raise TypeError(
                f'context[-1] = {context[-1]!r}: expected an integer',
            )
        return tuple(ctx), index


class _LegacyDuplicateChecker:
    """
    This checker replicates legacy behavior: all imports in the same
    module are treated on equal footing regardless of scoping, and each
    import target is only passed once to the profiler.

    Notes:
        Using this results in a :py:class:`DeprecationWarning`. This is
        because such deduplication can result in the profiler never
        getting passed an intended target. Consider the following
        example:

        >>> # doctest: +SKIP
        >>>
        >>>
        >>> def foo():
        ...     from spam import ham
        ...
        ...     return ham()
        ...
        >>>
        >>> def bar():
        ...     from spam import ham
        ...
        ...     return ham()
        ...
        >>>
        >>> if __name__ == '__main__':
        ...     bar()

        In the above example, ``spam.ham()`` is never profiled even
        after AST rewrite, because only the import in ``foo()`` has a
        post-import profiling statement inserted.
    """
    def __init__(self, profiled_imports: Collection[str]) -> None:
        msg = (
            'AstProfileTransformer(profiled_imports=<Collection[str]>) '
            'is deprecated because of erroneous resolution of duplicate '
            'imports; future code should use either '
            '`Mapping[Sequence[str | int], ImportTarget]` '
            '(e.g. the return value of `ProfmodExtractor.extract_all()`) '
            'or `None`'
        )
        diagnostics.log.warning(f'DeprecationWarning: {msg}')
        warn(msg, category=DeprecationWarning, stacklevel=2)  # Caller
        self._profiled_imports = set(profiled_imports)

    def should_profile_import(self, target: ImportTarget, _) -> bool:
        return target.name not in self._profiled_imports

    def record_profiled_import(self, target: ImportTarget, _) -> None:
        self._profiled_imports.add(target.name)


class AstProfileTransformer(ast.NodeTransformer):
    """
    Transform an abstract syntax tree adding profiling to all of its
    objects, by:

    - Adding profiler decorators on all functions & methods that are not
      already decorated with the profiler.
    - If ``profile_imports`` is True, a profiler method call (see
      :py:func:`line_profiler.autoprofile.line_profiler_utils\
.add_imported_function_or_module`
      and
      :py:func:`line_profiler.autoprofile.line_profiler_utils\
.add_star_import`)
      is added to all imports immediately after the import.
    """

    def __init__(
        self,
        profile_imports: bool = False,
        profiled_imports: (
            Mapping[Sequence[str | int], Sequence[ImportTarget]]
            | Collection[str]
            | None
        ) = None,
        profiler_name: str = 'profile',
        *,
        profile_star_imports: bool = False,
        profile_imports_in: Mapping[
            _CompoundNodeType, bool
        ] = _PROFILE_IMPORTS_IN_DEFAULT,
    ) -> None:
        """
        Initializes the AST transformer.

        Args:
            profile_imports (bool):
                if True, profile all concrete (non-star) imports.

            profiled_imports \
(Mapping[Sequence[str | int], Sequence[ImportTarget]] \
| Collection[str] | None):
                ``Mapping[Sequence[str | int], Sequence[ImportTarget]]``
                    mapping from the locations of already-profiled
                    import targets to those import targets themselves,
                    in the same format as the return value of
                    :py:meth:`line_profiler.autoprofile\
.ProfmodExtractor.extract_all`.
                ``Collection[str]``
                    DEPRECATED; dotted paths of imports to skip that
                    have already been added to profiler.
                :py:const:`None`
                    equivalent to ``{}``, i.e. no prior profiled
                    imports.

            profiler_name (str):
                the profiler name used as decorator and for the method
                call to add to the object to the profiler.

            profile_star_imports (bool):
                if this and ``profile_imports`` are True, also profile
                star-imports.

            profile_imports_in \
(Mapping[Literal['Module', 'Interactive', \
'FunctionDef', 'AsyncFunctionDef', 'ClassDef', \
'For', `AsyncFor`, `While`, 'If', 'match_case', \
'With', 'AsyncWith'. 'Try', 'TryStar', 'ExceptHandler'], bool]):
                for each of the compound-statement node type, whether to
                profile import statements residing therein.
        """
        self._profile_imports = bool(profile_imports)
        if profiled_imports is None:
            self._duplicate_checker: _DuplicateChecker
            self._duplicate_checker = _ContextAwareDuplicateChecker()
        elif (
            isinstance(profiled_imports, Mapping)
            and all(
                isinstance(imports, Sequence)
                for imports in profiled_imports.values()
            )
        ):
            self._duplicate_checker = _ContextAwareDuplicateChecker(cast(
                Mapping[Sequence[str | int], Sequence[ImportTarget]],
                profiled_imports,
            ))
        elif (
            isinstance(profiled_imports, Collection)
            and all(isinstance(imp, str) for imp in profiled_imports)
        ):
            profiled_imports = cast(Collection[str], profiled_imports)
            self._duplicate_checker = _LegacyDuplicateChecker(profiled_imports)
        else:  # nocover
            raise TypeError(
                f'profiled_imports = {profiled_imports!r}: '
                'expected `Collection[str]`, '
                '`Mapping[Sequence[str | int], Sequence[ImportTarget]]`, '
                'or `None`',
            )
        self._profiler_name = profiler_name
        self._profile_star_imports = profile_star_imports
        self._should_visit_imports = dict(profile_imports_in)
        self._dropped_star_imports: set[ImportTarget] = set()
        self._current_loc: list[str | int] = []
        self._current_node_ancestry: list[str] = []

    def _visit_func_def(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> ast.FunctionDef | ast.AsyncFunctionDef:
        """Decorate functions/methods with profiler.

        Checks if the function/method already has a profile_name decorator, if not, it will append
        profile_name to the end of the node's decorator list.
        The decorator is added to the end of the list to avoid conflicts with other decorators
        e.g. @staticmethod.

        Args:
            node (_ast.FunctionDef | _ast.AsyncFunctionDef):
                function/method in the AST

        Returns:
            node (_ast.FunctionDef | _ast.AsyncFunctionDef):
                function/method with profiling decorator
        """
        decor_ids = set()
        for decor in node.decorator_list:
            if isinstance(decor, ast.Name):
                decor_ids.add(decor.id)
        if self._profiler_name not in decor_ids:
            node.decorator_list.append(
                ast.Name(id=self._profiler_name, ctx=ast.Load())
            )
        self.generic_visit(node)
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_func_def

    def _visit_import(
        self,
        node: _Import,
        get_import_targets: Callable[[int, _Import], Sequence[ImportTarget]],
    ) -> _Import | list[_Import | ast.Expr]:
        """
        Add a node that profiles an import. If:

        - ``profile_imports`` is true,

        - The import statement isn't nested in a compound-statement node
          type explicitly excluded via ``profile_imports_in``, and

        - The import target is not in ``profiled_imports``,

        a node which calls the profiler method adding the object to the
        profiler is added immediately after the import.

        Args:
            node (_Import):
                import[-from] node in the AST
            get_import_targets \
(Callable[[int, _Import], Sequence[ImportTarget]]):
                helper callable for analyzing the node

        Returns:
            node (_Import | list[_Import | _ast.Expr]):
                if ``profile_imports`` is False:
                    the import node
                if ``profile_imports`` is True:
                    a list containing the import node and the profiling
                    node(s)
        """
        if not self._profile_imports:
            should_profile = False
        else:
            # Check if this node is nested inside compound statements
            # that we shouldn't look for imports in
            *ancestry, _ = self._current_node_ancestry
            svi = self._should_visit_imports
            should_profile = all(
                svi.get(cast(_CompoundNodeType, a_type), True)
                for a_type in ancestry
            )

        if not should_profile:
            # No need for further descent, no other node of interest can
            # reside import[-from] nodes
            return node

        result: list[_Import | ast.Expr] = [node]
        *_, index = self._current_loc
        assert isinstance(index, int)
        duplicate_checker = self._duplicate_checker

        for target in get_import_targets(index, node):
            if not duplicate_checker.should_profile_import(
                target, self._current_loc,
            ):
                continue
            expr = _ast_create_node_from_import_target(
                target, profile_star_imports=self._profile_star_imports,
            )
            if expr is None:  # Bookkeeping
                self._dropped_star_imports.add(target)
            else:
                duplicate_checker.record_profiled_import(
                    target, self._current_loc,
                )
                result.append(expr)
        return result

    def visit_Import(
        self, node: ast.Import,
    ) -> ast.Import | list[ast.Import | ast.Expr]:
        """
        Add nodes that profile objects imported using the
        ``import foo`` syntax.

        Args:
            node (_ast.Import):
                import in the AST

        Returns:
            node (_ast.Import | list[_ast.Import | _ast.Expr]):
                if ``profile_imports`` is False:
                    the import node
                if ``profile_imports`` is True:
                    a list containing the import node and the
                    profiling node(s)
        """
        return self._visit_import(node, ImportTarget._from_import_node)

    def visit_ImportFrom(
        self, node: ast.ImportFrom
    ) -> ast.ImportFrom | list[ast.ImportFrom | ast.Expr]:
        """
        Add nodes that profile objects imported using the
        ``from foo import bar`` syntax.

        Args:
            node (_ast.ImportFrom):
                import in the AST

        Returns:
            node (_ast.Import | list[_ast.Import | _ast.Expr]):
                if ``profile_imports`` is False:
                    the import node
                if ``profile_imports`` is True:
                    a list containing the import node and the
                    profiling node(s)
        """
        return self._visit_import(node, ImportTarget._from_import_from_node)

    def visit(self, node: ast.AST) -> ast.AST | list[ast.AST]:
        """
        :py:meth:`ast.NodeTransformer.visit` with extra bookkeeping.
        """
        anc = self._current_node_ancestry
        anc.append(type(node).__name__)
        try:
            return super().visit(node)
        finally:
            anc.pop()

    def generic_visit(self, node: ast.AST) -> ast.AST:
        """
        :py:meth:`ast.NodeTransformer.generic_visit` with extra
        bookkeeping.
        """
        for field, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                self._visit_generic_child(node, field, value)
            elif isinstance(value, MutableSequence):  # Compound node
                if not all(isinstance(item, ast.AST) for item in value):
                    continue
                self._visit_generic_children(
                    node, field, cast(MutableSequence[ast.AST], value),
                )
        return node

    def _visit_generic_child(
        self, node: ast.AST, field: str, child: ast.AST,
    ) -> None:
        self._current_loc.append(field)
        try:
            replacement: ast.AST | list[ast.AST] = self.visit(child)
            if isinstance(replacement, ast.AST):
                setattr(node, field, replacement)
            else:
                raise RuntimeError(
                    f'node = {node!r}: invalid field `.{field}` replacement '
                    f'({child!r} -> {replacement!r})'
                )
        finally:
            self._current_loc.pop()

    def _visit_generic_children(
        self, node: ast.AST, field: str, children: MutableSequence[ast.AST],
    ) -> None:
        self._current_loc.append(field)
        try:
            new_children: list[ast.AST] = []
            for i, item in enumerate(children):
                self._current_loc.append(i)
                try:
                    replacement = self.visit(item)
                    if isinstance(replacement, ast.AST):
                        new_children.append(replacement)
                    else:
                        new_children.extend(replacement)
                finally:
                    self._current_loc.pop()
            children[:] = new_children
        finally:
            self._current_loc.pop()

    @staticmethod
    def _get_profile_imports_in(
        config: ConfigSource | None = None,
        profile_nested_imports: Collection[_CompoundStatement] | None = None,
    ) -> dict[_CompoundNodeType, bool]:
        if config is None:
            config = ConfigSource.from_default()
        return _ImportFinder.filter_node_types(
            **_ImportFinder._get_filter_args(config, profile_nested_imports),
        )

    @classmethod
    def _transform(
        cls,
        node: ast.Module,
        filename: PathLike[str] | str | None = None,
        *,
        config: ConfigSource | None = None,
        profile_star_imports: bool | None = None,
        profile_nested_imports: Collection[_CompoundStatement] | None = None,
        _known_dropped_star_imports: Collection[ImportTarget] | None = None,
        **kwargs,
    ) -> ast.Module:
        """
        Wrapper around ``<instance>.visit()`` with extra bookkeeping and
        convenience args.

        Args:
            node (ast.Module):
                AST module node

            filename (PathLike[str] | str | None):
                Optional filename to be used in error/warning messages

            config (ConfigSource | None):
                Optional :py:class:`.ConfigSource` to load options from,
                controlling whether an import should be profiled

            profile_star_imports (bool | None):
                Whether to profile star-imports (``from ... import *``);
                if :py:const:`None`, it is loaded from the ``config``
                (from ``autoprofile.prof_star_imports``)

            profile_nested_imports \
(Collection[Literal['func_defs', 'class_defs', \
'loops', 'conditionals', 'contexts', 'try_except']] | None):
                Which of the compound-statement types to look for nested
                imports in;
                if :py:const:`None`, it is loaded from the ``config``
                (from ``autoprofile.import_discovery``)

            **kwargs
                Passed to the initializer

        Returns:
            node (ast.Module):
                Input module node
        """
        if profile_star_imports is None:
            profile_star_imports = _should_profile_star_imports(config)
        kwargs.setdefault(
            'profile_imports_in',
            cls._get_profile_imports_in(config, profile_nested_imports),
        )
        transformer = cls(
            profile_star_imports=profile_star_imports, **kwargs,
        )
        dropped_star_imports = transformer._dropped_star_imports
        if filename is None:
            filename = '???'
        try:
            return cast(ast.Module, transformer.visit(node))
        finally:
            if _known_dropped_star_imports:
                # Don't double-warn on import targets that we already
                # know should be dropped
                dropped_star_imports.difference_update(
                    _known_dropped_star_imports,
                )
            ImportTarget._check_and_warn_dropped_imports(
                dropped_star_imports,
                _DROPPED_STAR_IMPORTS_MSG_TEMPLATE.format(
                    action='profiled',
                    argname='profile_star_imports',
                ),
                filename,
                stacklevel=2,  # Attribute warning to the caller
            )
