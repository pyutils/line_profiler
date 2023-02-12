import sys
import os
import ast

import ubelt as ub
from xdoctest.static_analysis import package_modpaths


class ProfmodExtractor:
    @staticmethod
    def is_path(text):
        return '/' in text.replace('\\','/')

    @classmethod
    def _get_modnames_to_profile(cls, script_file, prof_mod):
        script_directory = os.path.realpath(os.path.dirname(script_file))
        new_sys_path = [script_directory]+sys.path

        modnames_to_profile = []
        for mod in prof_mod:
            modpath = ub.util_import.modname_to_modpath(mod, sys_path=new_sys_path)
            if modpath is None:
                if not os.path.exists(mod):
                    if cls.is_path(mod):
                        """mod file does not exist"""
                        continue
                    modnames_to_profile.append(mod)
                    continue
                modpath = mod

            try:
                modname = ub.util_import.modpath_to_modname(modpath)
            except ValueError:
                continue
            if modname not in modnames_to_profile:
                modnames_to_profile.append(modname)

            for submod_path in package_modpaths(modpath):
                submod_name = ub.util_import.modpath_to_modname(submod_path)
                if submod_name not in modnames_to_profile:
                    modnames_to_profile.append(submod_name)

        return modnames_to_profile

    @staticmethod
    def _ast_get_imports_from_tree(tree):
        modules = []
        modules_alias = []
        modules_index = []
        for idx,node in enumerate(tree.body):
            if isinstance(node, ast.Import):
                for name in node.names:
                    modname = name.name
                    if modname not in modules:
                        modules.append(modname)
                        modules_alias.append(name.asname)
                        modules_index.append(idx)
            elif isinstance(node, ast.ImportFrom):
                for name in node.names:
                    modname = node.module+'.'+name.name
                    alias = name.asname or name.name
                    if modname not in modules:
                        modules.append(modname)
                        modules_alias.append(alias)
                        modules_index.append(idx)
        return modules, modules_alias, modules_index

    @staticmethod
    def _get_names_to_profile(modnames_to_profile, modules, modules_alias):
        chosen_indexes = []
        mod_imports_added = []
        names_to_profile = []

        """match prof modules when submodule or function(hence the extra .) imported"""
        for i,mod_import in enumerate(modules):
            if mod_import in mod_imports_added:
                continue
            if mod_import not in modnames_to_profile and mod_import.rsplit('.',1)[0] not in modnames_to_profile:
                continue
            name = modules_alias[i] or mod_import
            mod_imports_added.append(mod_import)
            names_to_profile.append(name)
            chosen_indexes.append(i)

        # """match prof submodules when parent imported. doesnt work."""
        # for modname in modnames_to_profile:
        #     # parent_name = modname.rsplit('.',1)[0]
        #     for i,mod_import in enumerate(modules):
        #         if len(modname) <= len(mod_import) or not modname.startswith(mod_import) or modname[len(mod_import)] != '.':
        #         # if mod_import != parent_name or modname[len(parent_name)] != '.':
        #             continue
        #         alias = modules_alias[i]
        #         if alias:
        #             name = alias+modname[len(mod_import):]
        #         else:
        #             name = modname
        #         if mod_import not in mod_imports_added:
        #             mod_imports_added.append(mod_import)
        #             names_to_profile.append(name)
        #             chosen_indexes.append(i)
        #         break
        return names_to_profile, chosen_indexes

    def run(self, script_file, prof_mod, tree):
        modnames_to_profile = self._get_modnames_to_profile(script_file, prof_mod)

        modules, modules_alias, modules_index = self._ast_get_imports_from_tree(tree)

        names_to_profile, chosen_indexes = self._get_names_to_profile(modnames_to_profile, modules, modules_alias)
        return names_to_profile, chosen_indexes, modules_index
