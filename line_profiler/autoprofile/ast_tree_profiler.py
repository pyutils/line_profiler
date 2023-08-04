import os
import ast

from line_profiler.autoprofile.ast_profle_transformer import AstProfileTransformer, ast_profile_module
from line_profiler.autoprofile.profmod_extractor import ProfmodExtractor


class AstTreeProfiler:
    def __init__(self,
                 script_file,
                 prof_mod,
                 profile_imports,
                 ast_transformer_class_handler=AstProfileTransformer,
                 profmod_extractor_class_handler=ProfmodExtractor):
        self.script_file = script_file
        self.prof_mod = prof_mod
        self.profile_imports = profile_imports
        self.ast_transformer_class_handler = ast_transformer_class_handler
        self.profmod_extractor_class_handler = profmod_extractor_class_handler

    @staticmethod
    def _check_prof_mod_profile_full_script(script_file, prof_mod):
        script_file_realpath = os.path.realpath(script_file)
        for mod in prof_mod:
            if os.path.realpath(mod) == script_file_realpath:
                return True
        return False

    @staticmethod
    def _get_ast_tree(script_file):
        with open(script_file, 'r') as f:
            script_text = f.read()
        tree = ast.parse(script_text, filename=script_file)
        return tree

    def _profile_ast_tree(self,
                          tree,
                          tree_names_to_profile_dict,
                          profile_script=False,
                          profile_script_imports=False):
        profiled_imports = []
        argsort_tree_indexes = sorted(list(tree_names_to_profile_dict), reverse=True)
        for tree_index in argsort_tree_indexes:
            name = tree_names_to_profile_dict[tree_index]
            expr = ast_profile_module(name)
            tree.body.insert(tree_index + 1, expr)
            profiled_imports.append(name)
        if profile_script:
            tree = self.ast_transformer_class_handler(profile_imports=profile_script_imports,
                                                      profiled_imports=profiled_imports).visit(tree)
        ast.fix_missing_locations(tree)
        return tree

    def profile(self):
        profile_script = self._check_prof_mod_profile_full_script(self.script_file, self.prof_mod)

        tree = self._get_ast_tree(self.script_file)

        tree_names_to_profile_dict = self.profmod_extractor_class_handler(
            tree, self.script_file, self.prof_mod).run()
        tree_profiled = self._profile_ast_tree(tree,
                                               tree_names_to_profile_dict,
                                               profile_script=profile_script,
                                               profile_script_imports=self.profile_imports)
        return tree_profiled
