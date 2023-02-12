import inspect

def add_imported_function_or_module(self, item):
    if inspect.isfunction(item):
        self.add_function(item)
    elif inspect.isclass(item):
        for k, v in item.__dict__.items():
            if inspect.isfunction(v):
                self.add_function(v)
    elif inspect.ismodule(item):
        self.add_module(item)
    else:
        return
    self.enable_by_count()
