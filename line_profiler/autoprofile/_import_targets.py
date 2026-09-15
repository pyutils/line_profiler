import ast
import dataclasses
from collections.abc import Collection, Sequence
from itertools import groupby
from operator import attrgetter
from typing import Any
from typing_extensions import Self
from warnings import warn

from .. import _diagnostics as diagnostics


@dataclasses.dataclass(eq=True, frozen=True)
class ImportTarget:
    """
    An import target.

    Init attrs:
        name (str):
            The real name of the import. e.g. ``import foo as bar``
            -> ``'foo'``

        index (int):
            The index of the import as found in the AST body

        alias (str | None):
            The alias of an import if applicable. e.g.:

            - ``import foo as bar`` -> ``'bar'``
            - ``import foo`` -> ``None``

        lineno (int | None):
            Optional (1-indexed) line number on which the import occurs.

    Other attrs:
        resolved_name (str | None):
            Name under which the import can be found, e.g.:

            - ``import foo as bar`` -> ``'bar'``
            - ``import foo`` -> ``'foo'``
            - ``from foo import *`` -> ``None``

    Note:
        Other than the above attributes, the remaining attributes and
        methods of this object should be considered private.
    """
    name: str
    index: int
    alias: str | None = None
    lineno: int | None = None

    def __post_init__(self) -> None:
        """
        Type verifications.
        """
        if not isinstance(self.name, str):
            raise TypeError(f'.name = {self.name!r}: expected a str')
        if not isinstance(self.index, int):
            raise TypeError(
                f'.index = {self.index!r}: expected an int',
            )
        if not (self.alias is None or isinstance(self.alias, str)):
            raise TypeError(f'.alias = {self.alias!r}: expected a str or None')
        if not (self.lineno is None or isinstance(self.lineno, int)):
            raise TypeError(
                f'.lineno = {self.lineno!r}: expected an int or None',
            )

    @classmethod
    def _from_ast_nodes(cls, nodes: Sequence[ast.AST]) -> list[Self]:
        """
        Get all imports in the body of an AST node.

        Args:
            nodes (Sequence[ast.AST]):
                AST nodes to scan for imports;
                examples: :py:attr:`ast.Module.body`,
                :py:attr:`ast.If.orelse`.

        Returns:
            import_targets (list[Self]):
                List of all imports amond the nodes.

        Notes:
            Imports nested inside the ``nodes`` are not as yet
            handled, e.g. in

            >>> # doctest: +SKIP
            >>> from spam import ham
            >>> try:
            ...     from some_foo import bar
            ... except ImportError:
            ...     from other_foo import ersatz_bar as bar

            Only ``ham`` is extracted but not ``bar``.
        """
        targets: list[Self] = []
        modnames: set[str] = set()
        for index, node in enumerate(nodes):
            if isinstance(node, ast.Import):
                new_targets: list[Self] = cls._from_import_node(index, node)
            elif isinstance(node, ast.ImportFrom):
                new_targets = cls._from_import_from_node(index, node)
            else:  # TODO: descend into other bodied nodes
                new_targets = []
            for target in new_targets:
                if target.name not in modnames:
                    targets.append(target)
                    modnames.add(target.name)
        return targets

    @classmethod
    def _from_import_node(cls, index: int, node: ast.Import) -> list[Self]:
        return [
            cls(name.name, index, name.asname, name.lineno)
            for name in node.names
        ]

    @classmethod
    def _from_import_from_node(
        cls, index: int, node: ast.ImportFrom,
    ) -> list[Self]:
        if node.module is None:  # `from . import ...`
            return []
        return [
            cls(
                f'{node.module}.{name.name}',
                index,
                name.asname or name.name,
                name.lineno,
            )
            for name in node.names
        ]

    def _get_target_repr(self) -> str:
        if self.alias == '*':
            assert self.name.endswith('.*')
            return f'* (from {self.name[:-2]})'
        elif self.alias:
            return f'{self.alias} (= {self.name})'
        else:
            return self.name

    @classmethod
    def _check_and_warn_dropped_imports(
        cls,
        imports: Collection[Self],
        reason: str,
        source: Any,
        category: type[Warning] = UserWarning,
        stacklevel: int = 1,
        *args,
        **kwargs
    ) -> None:
        if not imports:
            return
        msg_chunks: list[str] = [
            '{}: {} would-be profiling target(s) dropped because {}:'
            .format(source, len(imports), reason),
        ]
        imports = sorted(imports, key=lambda imp: imp.lineno or float('inf'))
        for lineno, imports_on_line in groupby(
            imports, key=attrgetter('lineno'),
        ):
            lineno_repr = '???' if lineno is None else str(lineno)
            targets = ', '.join(sorted(
                imp._get_target_repr() for imp in imports_on_line
            ))
            msg_chunks.append(f'- line {lineno_repr}: {targets}')
        msg = (' ' if len(msg_chunks) < 3 else '\n').join(msg_chunks)
        if category is None:
            log_msg = msg
        else:
            log_msg = f'{category.__name__}: {msg}'
        diagnostics.log.warning(log_msg)
        warn(msg, category, stacklevel + 1, *args, **kwargs)

    @property
    def resolved_name(self) -> str | None:
        # Note: star-imports are parsed into
        # `ImportTarget('module.name.*', index, '*', lineno)`
        if self.alias == '*':
            return None
        return self.alias or self.name
