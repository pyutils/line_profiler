from __future__ import annotations

import ast
import os
import sys
from typing import cast
from warnings import warn

from .util_static import (
    modname_to_modpath,
    modpath_to_modname,
    package_modpaths,
)
from .. import _diagnostics as diagnostics
from ._import_targets import ImportTarget


class ProfmodExtractor:
    """Map prof_mod to imports in an abstract syntax tree.

    Takes the paths and dotted paths in prod_mod and finds their respective imports in an
    abstract syntax tree.
    """

    def __init__(
        self, tree: ast.Module, script_file: str, prof_mod: list[str]
    ) -> None:
        """Initializes the AST tree profiler instance with the AST, script file path and prof_mod

        Args:
            tree (_ast.Module):
                abstract syntax tree to fetch imports from.

            script_file (str):
                path to script being profiled.

            prof_mod (list[str]):
                list of imports to profile in script.
                passing the path to script will profile the whole script.
                the objects can be specified using its dotted path or full path (if applicable).
        """
        self._tree = tree
        self._script_file = script_file
        self._prof_mod = prof_mod

    @staticmethod
    def _is_path(text: str) -> bool:
        """Check whether a string is a path.

        Checks if a string contains a slash or ends with .py indicating it is a path.

        Args:
            text (str):
                string to check whether it is a path or not

        Returns:
            ret (bool):
                bool indicating whether the string is a path or not
        """
        ret = ('/' in text.replace('\\', '/')) or text.endswith('.py')
        return ret

    @classmethod
    def _get_modnames_to_profile_from_prof_mod(
        cls, script_file: str, prof_mod: list[str]
    ) -> list[str]:
        """Grab the valid paths and all dotted paths in prof_mod and their subpackages
        and submodules, in the form of dotted paths.

        First all items in prof_mod are converted to a valid path. if unable to convert,
        check if the item is an invalid path and skip it, else assume it is an installed package.
        The valid paths are then converted to dotted paths.
        The converted dotted paths along with the items assumed to be installed packages
        are added a list of modnames_to_profile.
        Then all subpackages and submodules under each valid path is fetched, converted to
        dotted path and also added to the list.
        if script_file is in prof_mod it is skipped to avoid name collision with othe imports,
        it will be processed elsewhere in the autoprofile pipeline.

        Args:
            script_file (str):
                path to script being profiled.

            prof_mod (list[str]):
                list of imports to profile in script.
                passing the path to script will profile the whole script.
                the objects can be specified using its dotted path or full path (if applicable).

        Returns:
            modnames_to_profile (list[str]):
                list of dotted paths to profile.
        """
        script_directory = os.path.realpath(os.path.dirname(script_file))
        """add script folder to modname_to_modpath sys_path to allow it to resolve modpaths"""
        new_sys_path = [script_directory] + sys.path
        script_file_realpath = os.path.realpath(script_file)

        modnames_to_profile = []
        for mod in prof_mod:
            if script_file_realpath == os.path.realpath(mod):
                """
                skip script_file as it will add the script's name without its extension which
                could have the same name as another import or function leading to unwanted profiling
                """
                continue
            """
            convert the item in prof_mod into a valid path.
            if it fails, the item may point to an installed module rather than local script
            so we check if the item is path and whether that path exists, else skip the item.
            """
            modpath = modname_to_modpath(
                mod, sys_path=cast('list[str | os.PathLike]', new_sys_path)
            )
            if modpath is None:
                """if cannot convert to modpath, check if already path and if invalid"""
                if not os.path.exists(mod):
                    if cls._is_path(mod):
                        """modpath does not exist, so skip"""
                        continue
                    modnames_to_profile.append(mod)
                    continue
                """assume item is and installed package. modpath_to_modname will have no effect"""
                modpath = mod

            """convert path to dotted path and add it to list to be profiled"""
            try:
                modname = modpath_to_modname(modpath)
            except ValueError:
                continue
            if modname not in modnames_to_profile:
                modnames_to_profile.append(modname)

            """
            recursively fetch all subpackages and submodules, convert them to dotted paths
            and add them to list to be profiled
            """
            for submod_path in package_modpaths(modpath):
                submod_name = modpath_to_modname(submod_path)
                if submod_name not in modnames_to_profile:
                    modnames_to_profile.append(submod_name)

        return modnames_to_profile

    @staticmethod
    def _ast_get_imports_from_tree(
        tree: ast.Module,
    ) -> dict[tuple[str | int, ...], list[ImportTarget]]:
        """Get all top-level imports in an abstract syntax tree.

        Args:
            tree (_ast.Module):
                abstract syntax tree to fetch imports from.

        Returns:
            import_targets (dict[tuple[str | int, ...], list[ImportTarget]])

        Note:
            Imports nested in e.g. try-except or if statements are not
            currently returned.

        See also:
            :py:meth:`.ImportTarget._from_ast_nodes`
        """
        # TODO: descend into bodied statements (e.g. try-except)
        return {('body',): ImportTarget._from_ast_nodes(tree.body)}

    @staticmethod
    def _find_modnames_in_tree_imports(
        modnames_to_profile: list[str], import_targets: list[ImportTarget],
    ) -> dict[int, list[ImportTarget]]:
        """Map modnames to imports from an abstract sytax tree.

        Find imports in import_targets, created from an abstract syntax tree, that match
        dotted paths in modnames_to_profile.
        When a submodule is imported, both the submodule and the parent module are checked
        whether they are in modnames_to_profile. As the user can ask to profile
        "foo" when only "from foo import bar" is imported, so both foo and bar are checked.
        The real import name of an import is used to map to the dotted paths.
        The import's alias is stored in the output dict.

        Args:
            modnames_to_profile (list[str]):
                list of dotted paths to profile.

            import_targets (list[ImportTarget]):
                list of all import targets in the tree

        Returns:
            filtered_imports (dict[int, list[ImportTarget]]):
                dict of imports found
                    key (int):
                        index of the (from-)import statement in AST
                    value (list[ImportTarget]):
                        list of filtered import targets
        """
        filtered_imports: dict[int, list[ImportTarget]] = {}
        modname_added_list = []
        for i, import_target in enumerate(import_targets):
            modname = import_target.name
            if modname in modname_added_list:
                continue
            # Check if either the parent module or submodule are in
            # `modnames_to_profile`
            if (
                modname not in modnames_to_profile
                and modname.rsplit('.', 1)[0] not in modnames_to_profile
            ):
                continue
            modname_added_list.append(modname)
            try:
                filtered_imports[import_target.index].append(import_target)
            except KeyError:  # No imports recorded for the statement
                filtered_imports[import_target.index] = [import_target]
        return filtered_imports

    def extract_all(self) -> dict[tuple[str | int, ...], list[ImportTarget]]:
        """
        Map ``prof_mod`` to imports in an abstract syntax tree.
        Takes the paths and dotted paths in ``prof_mod`` and finds their
        respective imports in an abstract syntax tree, returning their
        aliases and the location they appear in the AST.

        Returns:
            tree_imports_to_profile_dict \
(dict[tuple[str | int, ...], list[ImportTarget]]);
                dict of imports to profile
                    key (tuple[str | int, ...]):
                        Location of the import statement in the AST;
                        e.g. ``('body', 0)`` for the case where it is
                        the first statement in the
                        :py:attr:`ast.Module.body`
                    value (list[ImportTarget]):
                        list of import targets, each with these
                        attributes:

                        name (str):
                            Canonical name of the import
                        index (int):
                            Index where it occurs in e.g. a module body
                        alias (str | None):
                            Optional alias under which the import is
                            inserted into the namespace
                        lineno (int | None):
                            Optional (1-indexed) line number associated
                            with the target
                        resolved_name (str | None):
                            Name under which the import is inserted into
                            the namespace (should never be
                            :py:const`None` for non-star-imports)

        Notes:
            - As of now, ``from <module> import *`` is not supported,
              and will result in a :py:class:`UserWarning`.

            - Nested imports (e.g. imports in try-except/if blocks) are
              not currently retrieved.
        """
        modnames_to_profile = self._get_modnames_to_profile_from_prof_mod(
            self._script_file, self._prof_mod
        )
        import_targets = self._ast_get_imports_from_tree(self._tree)
        raw: dict[tuple[str | int, ...], list[ImportTarget]] = {
            (*loc, index): filtered_imports
            for loc, imports in import_targets.items()
            for index, filtered_imports in self._find_modnames_in_tree_imports(
                modnames_to_profile, imports
            ).items()
        }
        filtered: dict[tuple[str | int, ...], list[ImportTarget]] = {}
        star_imports: set[ImportTarget] = set()
        for loc, imports in raw.items():
            # TODO: runtime introspection of imports to handle
            # star-imports
            # Notes:
            # - We don't issue the warning in
            #   `._find_modnames_in_tree_imports()` because that is a
            #   static method and doesn't have access to the file from
            #   which the AST is generated, which we want to include in
            #   the warning message.
            # - As far normal Python syntax is concerned, each
            #   import-from statement can have at most one `*` target,
            #   which would be the sole target thereof (so
            #   `indices_to_drop` should either be `[]` or `[0]`);
            #   but it doesn't hurt to be cautious
            indices_to_drop = [
                i for i, imp in enumerate(imports)
                if imp.resolved_name is None  # Star-imports
            ]
            for i in reversed(indices_to_drop):
                imp = imports.pop(i)
                star_imports.add(imp)
            if imports:
                filtered[loc] = imports
        ImportTarget._check_and_warn_dropped_imports(
            star_imports,
            "we don't currently handle `from ... import *` statements",
            self._script_file,
            stacklevel=2,  # Attribute warning to caller
        )
        return filtered

    def run(self) -> dict[int, str]:
        """
        Deprecated, legacy method kept for backward compatibility.

        Returns:
            tree_imports_to_profile_dict (dict[int, str])
                dict of top-level imports to profile
                    key (int):
                        index of import in module AST's body
                    value (str):
                        alias (or name if no alias used) of the LAST
                        target to import in the corresponding
                        :py:class:`ast.Import` or
                        :py:class:`ast.ImportFrom` statement

        Notes:
            - New code should use the :py:meth:`.extract_all` method,
              which handles multi-target import statements (see #434).

            - Calling this method issues a
              :py:class:`DeprecationWarning`.

            - For multi-target import statements, this only preserves
              the last target. If this results in import targets being
              dropped, a :py:class:`UserWarning` is issued.

            - ``from <module> import *`` is not supported, and will
              result in a :py:class:`UserWarning`.

            - Nested imports (e.g. imports in try-except/if blocks) are
              not retrieved.
        """
        msg = (
            '`ProfmodExtractor.run()` is now deprecated, because it cannot '
            'correctly resolve multi-target import statements; '
            'use `ProfmodExtractor.extract_all()` instead.'
        )
        diagnostics.log.warning(f'DeprecationWarning: {msg}')
        warn(msg, DeprecationWarning, stacklevel=2)  # Caller
        result: dict[int, str] = {}
        dropped: set[ImportTarget] = set()
        for index, imports in self.extract_all().items():
            if not (
                len(index) == 2
                and index[0] == 'body'
                and isinstance(index[1], int)
            ):  # We only handle the top-level imports here
                continue
            *remainder, last = imports
            dropped.update(remainder)
            dropped.discard(last)
            name = last.resolved_name
            if name is None:
                # Shouldn't happen with the current `.extract_all()`,
                # but once we fix star-imports...
                continue
            result[index[1]] = name
        ImportTarget._check_and_warn_dropped_imports(
            dropped,
            'the import statement(s) is/are multi-target',
            self._script_file,
            stacklevel=2,  # Attribute warning to caller
        )
        return result
