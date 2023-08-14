import ubelt as ub

# try:
#     import ast
#     unparse = ast.unparse
# except AttributeError:
#     try:
#         import astunparse
#         unparse = astunparse.unparse
#     except ModuleNotFoundError:
#         unparse = None

# import sys
# sys.path.append('../')
from line_profiler.autoprofile import autoprofile


def create_poc(dry_run=False):
    root = ub.Path.appdir('line_profiler/test/poc/')
    repo = (root / 'repo')
    modpaths = {}
    modpaths['script'] = (root / 'repo/script.py')
    modpaths['foo'] = (root / 'repo/foo')
    modpaths['foo.__init__'] = (root / 'repo/foo/__init__.py')
    modpaths['foo.bar'] = (root / 'repo/foo/bar.py')
    modpaths['foo.baz'] = (root / 'repo/foo/baz')
    modpaths['foo.baz.__init__'] = (root / 'repo/foo/baz/__init__.py')
    modpaths['foo.baz.spam'] = (root / 'repo/foo/baz/spam.py')
    modpaths['foo.baz.eggs'] = (root / 'repo/foo/baz/eggs.py')

    if not dry_run:
        root.delete().ensuredir()
        repo.ensuredir()
        modpaths['script'].touch()
        modpaths['foo'].ensuredir()
        modpaths['foo.__init__'].touch()
        modpaths['foo.bar'].touch()
        modpaths['foo.bar'].write_text('def asdf():\n    2**(1/65536)')
        modpaths['foo.baz'].ensuredir()
        modpaths['foo.baz.__init__'].touch()
        modpaths['foo.baz.spam'].touch()
        modpaths['foo.baz.spam'].write_text('def spamfunc():\n    ...')
        modpaths['foo.baz.eggs'].touch()
        modpaths['foo.baz.eggs'].write_text('def eggfunc():\n    ...')

        """different import variations to handle"""
        script_text = ub.codeblock(
            '''
            import foo # mod
            import foo.bar # py
            from foo import bar # py
            from foo.bar import asdf # fn
            import foo.baz as foodotbaz # mod
            from foo import baz as foobaz # mod
            from foo import bar, baz as baz2 # py,mod
            import foo.baz.eggs # py
            from foo.baz import eggs # py
            from foo.baz.eggs import eggfunc # fn
            from foo.baz.spam import spamfunc as yum # fn
            from numpy import round # fn
            # @profile
            def test():
                2**65536
                foo.bar.asdf()
            def main():
                2**65536
                test()
                # foo.bar.asdf()
            main()
            test()
            # asdf()
            ''')
        ub.writeto(modpaths['script'], script_text)

    return root, repo, modpaths


def main():
    root, repo, modpaths = create_poc(dry_run=False)

    script_file = str(modpaths['script'])

    """separate from prof_mod, profile all imports in script"""
    profile_script_imports = False

    """modnames to profile"""
    modnames = [
        # 'fool', # doesn't exist
        # 'foo',
        'foo.bar',
        # 'foo.baz',
        # 'foo.baz.eggs',
        # 'foo.baz.spam',
        # 'numpy.round',
    ]
    """modpaths to profile"""
    modpaths = [
        # str(root),
        # str((repo / 'fool')), # doesn't exist
        # str(modpaths['foo']),
        # str(modpaths['foo.__init__']),
        # str(modpaths['foo.bar']),
        # str(modpaths['foo.baz']),
        # str(modpaths['foo.baz.__init__']),
        # str(modpaths['foo.baz.spam']),
        # str(modpaths['foo.baz.eggs']),
        # str(modpaths['script']), # special case to profile all items
    ]

    prof_mod = modnames + modpaths
    # prof_mod = modpaths
    # prof_mod = modnames

    """mimick running using kernprof"""
    import sys
    import os
    import builtins
    __file__ = script_file
    __name__ = '__main__'
    script_directory = os.path.realpath(os.path.dirname(script_file))
    sys.path.insert(0, script_directory)
    import line_profiler
    prof = line_profiler.LineProfiler()
    builtins.__dict__['profile'] = prof
    ns = locals()

    autoprofile.run(script_file, ns, prof_mod=prof_mod)

    print('\nprofiled')
    print('=' * 10)
    prof.print_stats(output_unit=1e-6, stripzeros=True, stream=sys.stdout)

if __name__ == '__main__':
    main()
