import ast


def argsort(seq):
    return sorted(range(len(seq)), key=seq.__getitem__)


def get_ast_tree(script_file):
    with open(script_file,'r') as f:
        script_text = f.read()
    tree = ast.parse(script_text, filename=script_file)
    return tree


def ast_profile_module(modname, attr='add_imported_function_or_module'):
    func = ast.Attribute(value=ast.Name(id='profile', ctx=ast.Load()), attr=attr, ctx=ast.Load())
    names = modname.split('.')
    value = ast.Name(id=names[0], ctx=ast.Load())
    for name in names[1:]:
        value = ast.Attribute(attr=name,ctx=ast.Load(),value=value)
    expr = ast.Expr(value=ast.Call(func=func, args=[value], keywords=[]))
    return expr


def profile_ast_tree(tree, names_to_profile, chosen_indexes, modules_index, profile_script=False, profile_script_imports=False):
    argsort_indexes = reversed(argsort(chosen_indexes))
    for i in argsort_indexes:
        index = chosen_indexes[i]
        name = names_to_profile[i]
        idx = modules_index[index]
        expr = ast_profile_module(name)
        tree.body.insert(idx+1,expr)
    if profile_script:
        tree = ASTProfile(profile_imports=profile_script_imports, profiled_imports=names_to_profile).visit(tree)
    ast.fix_missing_locations(tree)
    return tree


class ASTProfile(ast.NodeTransformer):
    def __init__(self, profile_imports=False, profiled_imports=None):
        self._profile_imports = bool(profile_imports)
        self._profiled_imports = profiled_imports if profiled_imports is not None else []

    def visit_FunctionDef(self, node):
        if 'profile' not in (d.id for d in node.decorator_list):
            node.decorator_list.append(ast.Name(id='profile', ctx=ast.Load()))
        return self.generic_visit(node)

    def visit_Import(self, node):
        if not self._profile_imports:
            return self.generic_visit(node)
        visited = [self.generic_visit(node)]
        for names in node.names:
            node_name = names.name if names.asname is None else names.asname
            if node_name in self._profiled_imports:
                continue
            self._profiled_imports.append(node_name)
            expr = ast_profile_module(node_name)
            visited.append(expr)
        return visited

    def visit_ImportFrom(self, node):
        if not self._profile_imports:
            return self.generic_visit(node)
        visited = [self.generic_visit(node)]
        for names in node.names:
            node_name = names.name if names.asname is None else names.asname
            if node_name in self._profiled_imports:
                continue
            self._profiled_imports.append(node_name)
            expr = ast_profile_module(node_name)
            visited.append(expr)
        return visited
