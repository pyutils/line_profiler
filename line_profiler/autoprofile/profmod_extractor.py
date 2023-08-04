import sys
import os
import ast
from line_profiler.autoprofile.util_static import modname_to_modpath, modpath_to_modname, package_modpaths


class ProfmodExtractor:
    def __init__(self, tree, script_file, prof_mod):
        self.tree = tree
        self.script_file = script_file
        self.prof_mod = prof_mod

    @staticmethod
    def _is_path(text):
        return '/' in text.replace('\\', '/')

    @classmethod
    def _get_modnames_to_profile(cls, script_file, prof_mod):
        script_directory = os.path.realpath(os.path.dirname(script_file))
        new_sys_path = [script_directory] + sys.path

        modnames_to_profile = []
        for mod in prof_mod:
            modpath = modname_to_modpath(mod, sys_path=new_sys_path)
            if modpath is None:
                if not os.path.exists(mod):
                    if cls._is_path(mod):
                        """mod file does not exist"""
                        continue
                    modnames_to_profile.append(mod)
                    continue
                modpath = mod

            try:
                modname = modpath_to_modname(modpath)
            except ValueError:
                continue
            if modname not in modnames_to_profile:
                modnames_to_profile.append(modname)

            for submod_path in package_modpaths(modpath):
                submod_name = modpath_to_modname(submod_path)
                if submod_name not in modnames_to_profile:
                    modnames_to_profile.append(submod_name)

        return modnames_to_profile

    @staticmethod
    def _ast_get_imports_from_tree(tree):
        module_dict_list = []
        modname_list = []
        for idx, node in enumerate(tree.body):
            if isinstance(node, ast.Import):
                for name in node.names:
                    modname = name.name
                    if modname not in modname_list:
                        alias = name.asname
                        module_dict = {
                            'name': modname,
                            'alias': alias,
                            'tree_index': idx,
                        }
                        module_dict_list.append(module_dict)
                        modname_list.append(modname)
            elif isinstance(node, ast.ImportFrom):
                for name in node.names:
                    modname = node.module + '.' + name.name
                    if modname not in modname_list:
                        alias = name.asname or name.name
                        module_dict = {
                            'name': modname,
                            'alias': alias,
                            'tree_index': idx,
                        }
                        module_dict_list.append(module_dict)
                        modname_list.append(modname)
        return module_dict_list

    @staticmethod
    def _get_names_to_profile(modnames_to_profile, module_dict_list):
        tree_names_to_profile_dict = {}

        modname_added_list = []
        """match prof modules when submodule or function(hence the extra .) imported"""
        for i, module_dict in enumerate(module_dict_list):
            modname = module_dict['name']
            if modname in modname_added_list:
                continue
            if modname not in modnames_to_profile and modname.rsplit('.', 1)[0] not in modnames_to_profile:
                continue
            name = module_dict['alias'] or modname
            modname_added_list.append(modname)
            tree_names_to_profile_dict[module_dict['tree_index']] = name

        # """match prof submodules when parent imported. doesnt work."""
        # for modname in modnames_to_profile:
        #     # parent_name = modname.rsplit('.',1)[0]
        #     for i,modname in enumerate(modules):
        #         if len(modname) <= len(modname) or not modname.startswith(modname) or modname[len(modname)] != '.':
        #         # if modname != parent_name or modname[len(parent_name)] != '.':
        #             continue
        #         alias = modules_alias[i]
        #         if alias:
        #             name = alias+modname[len(modname):]
        #         else:
        #             name = modname
        #         if modname not in modname_added_list:
        #             modname_added_list.append(modname)
        #             names_to_profile.append(name)
        #             chosen_indexes.append(i)
        #         break
        return tree_names_to_profile_dict

    def run(self):
        modnames_to_profile = self._get_modnames_to_profile(self.script_file, self.prof_mod)

        module_dict_list = self._ast_get_imports_from_tree(self.tree)

        tree_names_to_profile_dict = self._get_names_to_profile(modnames_to_profile, module_dict_list)
        return tree_names_to_profile_dict
