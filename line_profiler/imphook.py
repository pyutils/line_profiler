import importlib.abc
import importlib.machinery
from pathlib import Path
import sys
import ast

class AstModImportHook(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, module_to_monkeypatch):
        # type: (Dict[str, Callable[[Module], None]) -> None
        self._modules_to_monkeypatch = {k:None for k in module_to_monkeypatch}
        self._wl = module_to_monkeypatch
        self._in_create_module = False

    def find_module(self, fullname, path=None):
        spec = self.find_spec(fullname, path)
        if spec is None:
            return None
        return spec

    def create_module(self, spec):
        self._in_create_module = True

        from importlib.util import find_spec, module_from_spec
        real_spec = importlib.util.find_spec(spec.name)

        real_module = module_from_spec(real_spec)
        print('RM', real_module.__file__)
        #real_spec.loader.exec_module(real_module)

        #self._modules_to_monkeypatch[spec.name](real_module)

        self._in_create_module = False
        return real_module

    def exec_module(self, module):
        print(module.__name__)
        if module.__name__ in self._wl:
            b = ast.parse(Path(module.__file__).read_text())
            for it in b.body:
                if isinstance(it, ast.FunctionDef) and (it.name in self._wl[module.__name__]):
                    print('will profile:', module.__name__, it.name)
                    it.decorator_list.append(ast.Name(id='profile', ctx=ast.Load()))
    
            bp = ast.fix_missing_locations(b)
            c = compile(bp, module.__name__, 'exec')
            exec(c, module.__dict__)
        sys.modules[module.__name__] = module
        globals()[module.__name__] = module

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self._modules_to_monkeypatch:
            print('Skip FN', fullname, self._modules_to_monkeypatch)
            return None

        if self._in_create_module:
            # if we are in the create_module function,
            # we return the real module (return None)
            return None
        print('Keep FN', fullname)
        spec = importlib.machinery.ModuleSpec(fullname, self)
        return spec

def i2():
    # insert the path hook ahead of other path hooks
    #sys.path_hooks.insert(0, FileFinder.path_hook(loader_details))
    
    # clear any loaders that might already be in use by the FileFinder
    #sys.path_importer_cache.clear()
    #invalidate_caches()
    import builtins
    def profile(f):
        f.profile = True
        return f
    builtins.profile = profile
    sys.meta_path.insert(0, AstModImportHook({'click.termui':['clear'], 'rub':['f']}))
    
#import pygments.styles

