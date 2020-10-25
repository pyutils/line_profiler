#!/usr/bin/env python
# -*- coding: UTF-8 -*-
""" Script to conveniently run profilers on code in a variety of circumstances.
"""

import functools
import optparse
import os
import sys

PY3 = sys.version_info[0] == 3

# Guard the import of cProfile such that 3.x people
# without lsprof can still use this script.
try:
    from cProfile import Profile
except ImportError:
    try:
        from lsprof import Profile
    except ImportError:
        from profile import Profile


# Python 3.x compatibility utils: execfile
# ========================================
try:
    execfile
except NameError:
    # Python 3.x doesn't have 'execfile' builtin
    import builtins
    exec_ = getattr(builtins, "exec")

    def execfile(filename, globals=None, locals=None):
        with open(filename, 'rb') as f:
            exec_(compile(f.read(), filename, 'exec'), globals, locals)
# =====================================

def execfile_inject(filename, globals=None, locals=None, include=None, exclude=None, 
                        find="if __name__ == '__main__':", before=False, module=None):
    """
    Args:
        include (str):   comma separated list of function/class names to include in profiling
        exclude (str):   comma separated list of function/class names to exclude from profiling
        find (str):      text to look for in code to add profiling code before or after
                         default: "if __name__ == '__main__':"
        before (bool):   if true, insert profiling code before find string, else after
                         default: False
        module (string): module/class/function name in script file to profile
    """
    import builtins
    import re
    import textwrap
    exec_ = getattr(builtins, "exec")

    include_ = ',include="{}"'.format(include) if include else ''
    exclude_ = ',exclude="{}"'.format(exclude) if exclude else ''
    module_ = '{}'.format(module) if module else ''
    
    with open(filename, 'r') as f:
        code = f.read()
        """ skip adding auto profiling code if already present and profiling all, or has include/exclude """
        if code.find('decorate_with(profile)()') == -1 and code.find('decorate_with(profile,') == -1:
            """ profiling code must be added after all declarations but before script is run
            defaults to adding code after "if __name__ == '__main__':" as that is usually where
            script execution is. 
            """
            needle = str(find) if find is not None else "if __name__ == '__main__':"
            loc_1 = code.rfind(needle)
            if loc_1 != -1:
                loc_0 = code.rfind('\n',0,loc_1)+1
                loc_2 = code.find('\n',loc_0)-1
                if before:
                    loc = loc_0
                else:
                    loc = loc_2+1
                line = code[loc_0:loc_2+1]
                """ match indentation of script when adding before or after """
                indentation = re.match(r"\s*", line).group()
                if not before and code[loc_2] == ':':
                    indentation += '    '
                """ profiling code that wraps modules/functions/classes to profile """
                decorator_code = textwrap.dedent("""
                                    def decorate_with(decorator,include=None,exclude=None):
                                        def wrapper(fn=None):
                                            import inspect
                                            fn_ = fn
                                            if fn_ is None: fn_ = globals()
                                            include_ = str(include).split(',') if include is not None else []
                                            exclude_ = str(exclude).split(',') if exclude is not None else []
                                            include_ = [x.strip() for x in include_]
                                            exclude_ = [x.strip() for x in exclude_]
                                            if type(fn_) is dict:
                                                for name in fn_:
                                                    if name not in ['execfile_','execfile_inject','decorate_with']:
                                                        if not len(include_) or name in include_:
                                                            if not len(exclude_) or name not in exclude_:
                                                                if inspect.isfunction(fn_[name]):
                                                                    fn_[name] = decorator(fn_[name])
                                                                elif inspect.isclass(fn_[name]):
                                                                    for key, obj in inspect.getmembers(fn_[name]):
                                                                        if inspect.isfunction(obj) or inspect.ismethod(obj):
                                                                            setattr(fn_[name], key, decorator(obj))
                                            elif inspect.isclass(fn_):
                                                for key, obj in inspect.getmembers(fn_):
                                                    if not len(include_) or key in include_:
                                                        if not len(exclude_) or key not in exclude_:
                                                            if inspect.isfunction(obj) or inspect.ismethod(obj):
                                                                setattr(fn_, key, decorator(obj))
                                                return fn_
                                            elif inspect.ismodule(fn_):
                                                for name in dir(fn_):
                                                    if not len(include_) or name in include_:
                                                        if not len(exclude_) or name not in exclude_:
                                                            obj = getattr(fn_, name)
                                                            if inspect.isfunction(obj):
                                                                setattr(fn_, name, decorator(obj))
                                            elif inspect.isfunction(fn_):
                                                if not len(include_) or fn_.__name__ in include_:
                                                    if not len(exclude_) or fn_.__name__ not in exclude_:
                                                        globals()[fn_.__name__] = decorator(fn_)
                                                        fn_ = decorator(fn_)
                                            def wrap(*args,**kwargs):
                                                if callable(fn_):
                                                    fn_(*args,**kwargs)
                                            return wrap
                                        return wrapper
                                    decorate_with(profile{}{})({})
                                    """.format(include_,exclude_,module_))
                decorator_code = textwrap.indent(decorator_code, prefix=indentation)
                code = '{}\n{}\n{}'.format(code[:loc],decorator_code,code[loc:])
        exec_(compile(code, filename, 'exec'), globals, locals)



CO_GENERATOR = 0x0020
def is_generator(f):
    """ Return True if a function is a generator.
    """
    isgen = (f.__code__.co_flags & CO_GENERATOR) != 0
    return isgen


class ContextualProfile(Profile):
    """ A subclass of Profile that adds a context manager for Python
    2.5 with: statements and a decorator.
    """

    def __init__(self, *args, **kwds):
        super(ContextualProfile, self).__init__(*args, **kwds)
        self.enable_count = 0

    def enable_by_count(self, subcalls=True, builtins=True):
        """ Enable the profiler if it hasn't been enabled before.
        """
        if self.enable_count == 0:
            self.enable(subcalls=subcalls, builtins=builtins)
        self.enable_count += 1

    def disable_by_count(self):
        """ Disable the profiler if the number of disable requests matches the
        number of enable requests.
        """
        if self.enable_count > 0:
            self.enable_count -= 1
            if self.enable_count == 0:
                self.disable()

    def __call__(self, func):
        """ Decorate a function to start the profiler on function entry and stop
        it on function exit.
        """
        # FIXME: refactor this into a utility function so that both it and
        # line_profiler can use it.
        if is_generator(func):
            wrapper = self.wrap_generator(func)
        else:
            wrapper = self.wrap_function(func)
        return wrapper

    # FIXME: refactor this stuff so that both LineProfiler and
    # ContextualProfile can use the same implementation.
    def wrap_generator(self, func):
        """ Wrap a generator to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # The first iterate will not be a .send()
            self.enable_by_count()
            try:
                item = next(g)
            except StopIteration:
                return
            finally:
                self.disable_by_count()
            input = (yield item)
            # But any following one might be.
            while True:
                self.enable_by_count()
                try:
                    item = g.send(input)
                except StopIteration:
                    return
                finally:
                    self.disable_by_count()
                input = (yield item)
        return wrapper

    def wrap_function(self, func):
        """ Wrap a function to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

    def __enter__(self):
        self.enable_by_count()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disable_by_count()


def find_script(script_name):
    """ Find the script.

    If the input is not a file, then $PATH will be searched.
    """
    if os.path.isfile(script_name):
        return script_name
    path = os.getenv('PATH', os.defpath).split(os.pathsep)
    for dir in path:
        if dir == '':
            continue
        fn = os.path.join(dir, script_name)
        if os.path.isfile(fn):
            return fn

    sys.stderr.write('Could not find script %s\n' % script_name)
    raise SystemExit(1)


def main(args=None):
    if args is None:
        args = sys.argv
    usage = "%prog [-s setupfile] [-o output_file_path] scriptfile [arg] ..."
    parser = optparse.OptionParser(usage=usage, version="%prog 1.0b2")
    parser.allow_interspersed_args = False
    parser.add_option('-l', '--line-by-line', action='store_true',
        help="Use the line-by-line profiler from the line_profiler module "
        "instead of Profile. Implies --builtin.")
    parser.add_option('-b', '--builtin', action='store_true',
        help="Put 'profile' in the builtins. Use 'profile.enable()' and "
            "'profile.disable()' in your code to turn it on and off, or "
            "'@profile' to decorate a single function, or 'with profile:' "
            "to profile a single section of code.")
    parser.add_option('-o', '--outfile', default=None,
        help="Save stats to <outfile>")
    parser.add_option('-s', '--setup', default=None,
        help="Code to execute before the code to profile")
    parser.add_option('-v', '--view', action='store_true',
        help="View the results of the profile in addition to saving it.")
    parser.add_option('-a', '--auto', action='store_true',
        help="profile functions and classes inside script, only works with line_profiler -l, --line-by-line")
    parser.add_option('-i', '--include', default=None,
        help="comma separated functions/classes to profile")
    parser.add_option('-x', '--exclude', default=None,
        help="comma separated functions/classes to skip profiling")
    parser.add_option('-f', '--auto-find', default="if __name__ == '__main__':",
        help="insert auto profiling code before/after found text. must be after "
            "all function/class definitions, but before executions")
    parser.add_option('-r', '--auto-before', action='store_true', default=False,
        help="insert the auto profiling code before auto-find text, default is after")
    parser.add_option('-m', '--module', default=None,
        help="profile module/function/class in script with this name")


    if not sys.argv[1:]:
        parser.print_usage()
        sys.exit(2)

    options, args = parser.parse_args()

    if not options.outfile:
        if options.line_by_line:
            extension = 'lprof'
        else:
            extension = 'prof'
        options.outfile = '%s.%s' % (os.path.basename(args[0]), extension)


    sys.argv[:] = args
    if options.setup is not None:
        # Run some setup code outside of the profiler. This is good for large
        # imports.
        setup_file = find_script(options.setup)
        __file__ = setup_file
        __name__ = '__main__'
        # Make sure the script's directory is on sys.path instead of just
        # kernprof.py's.
        sys.path.insert(0, os.path.dirname(setup_file))
        ns = locals()
        execfile(setup_file, ns, ns)

    if options.line_by_line:
        import line_profiler
        prof = line_profiler.LineProfiler()
        options.builtin = True
    else:
        prof = ContextualProfile()
    if options.builtin:
        if PY3:
            import builtins
        else:
            import __builtin__ as builtins
        builtins.__dict__['profile'] = prof

    script_file = find_script(sys.argv[0])
    __file__ = script_file
    __name__ = '__main__'
    # Make sure the script's directory is on sys.path instead of just
    # kernprof.py's.
    sys.path.insert(0, os.path.dirname(script_file))

    try:
        try:
            execfile_ = execfile
            ns = locals()
            if options.auto and options.line_by_line:
                """ if auto profile and line profiler selected, inject auto profiling code
                into code read from script file """
                execfile_inject(script_file, ns, ns, options.include, options.exclude, 
                                options.auto_find, options.auto_before, options.module)
            elif options.builtin:
                execfile(script_file, ns, ns)
            else:
                prof.runctx('execfile_(%r, globals())' % (script_file,), ns, ns)
        except (KeyboardInterrupt, SystemExit):
            pass
    finally:
        prof.dump_stats(options.outfile)
        print('Wrote profile results to %s' % options.outfile)
        if options.view:
            prof.print_stats()

if __name__ == '__main__':
    sys.exit(main(sys.argv))
