import ast


def ast_profile_module(modname, attr='add_imported_function_or_module'):
    func = ast.Attribute(value=ast.Name(id='profile', ctx=ast.Load()), attr=attr, ctx=ast.Load())
    names = modname.split('.')
    value = ast.Name(id=names[0], ctx=ast.Load())
    for name in names[1:]:
        value = ast.Attribute(attr=name, ctx=ast.Load(), value=value)
    expr = ast.Expr(value=ast.Call(func=func, args=[value], keywords=[]))
    return expr


class AstProfileTransformer(ast.NodeTransformer):
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
