"""
extract the below functions from packages ubelt & xdoctest using liberator used in profmod_extractor.py
https://github.com/Kitware/liberator
https://github.com/Erotemic/xdoctest (v1.0.1)
https://github.com/Erotemic/ubelt (v1.1.2)

ubelt.util_import.modname_to_modpath
ubelt.util_import.modpath_to_modname
xdoctest.static_analysis.package_modpaths
"""

from os.path import (abspath, basename, dirname, exists, expanduser, isdir,
                     isfile, join, realpath, relpath, split, splitext)
import os
import sys

"""
ubelt.util_import.modname_to_modpath
ubelt.util_import.modpath_to_modname
"""
def _extension_module_tags():
    """
    Returns valid tags an extension module might have

    Returns:
        List[str]
    """
    import sysconfig
    tags = []
    # handle PEP 3149 -- ABI version tagged .so files
    # ABI = application binary interface
    tags.append(sysconfig.get_config_var('SOABI'))
    tags.append('abi3')  # not sure why this one is valid but it is
    tags = [t for t in tags if t]
    return tags


def _platform_pylib_exts():  # nocover
    """
    Returns .so, .pyd, or .dylib depending on linux, win or mac.
    On python3 return the previous with and without abi (e.g.
    .cpython-35m-x86_64-linux-gnu) flags. On python2 returns with
    and without multiarch.

    Returns:
        tuple
    """
    import sysconfig
    valid_exts = []
    # return with and without API flags
    # handle PEP 3149 -- ABI version tagged .so files
    base_ext = '.' + sysconfig.get_config_var('EXT_SUFFIX').split('.')[-1]
    for tag in _extension_module_tags():
        valid_exts.append('.' + tag + base_ext)
    valid_exts.append(base_ext)
    return tuple(valid_exts)


def normalize_modpath(modpath, hide_init=True, hide_main=False):
    """
    Normalizes __init__ and __main__ paths.

    Args:
        modpath (str | PathLike):
            path to a module

        hide_init (bool):
            if True, always return package modules as __init__.py files
            otherwise always return the dpath. Defaults to True.

        hide_main (bool):
            if True, always strip away main files otherwise ignore __main__.py.
            Defaults to False.

    Returns:
        str | PathLike: a normalized path to the module

    Note:
        Adds __init__ if reasonable, but only removes __main__ by default

    Example:
        >>> from xdoctest import static_analysis as module
        >>> modpath = module.__file__
        >>> assert normalize_modpath(modpath) == modpath.replace('.pyc', '.py')
        >>> dpath = dirname(modpath)
        >>> res0 = normalize_modpath(dpath, hide_init=0, hide_main=0)
        >>> res1 = normalize_modpath(dpath, hide_init=0, hide_main=1)
        >>> res2 = normalize_modpath(dpath, hide_init=1, hide_main=0)
        >>> res3 = normalize_modpath(dpath, hide_init=1, hide_main=1)
        >>> assert res0.endswith('__init__.py')
        >>> assert res1.endswith('__init__.py')
        >>> assert not res2.endswith('.py')
        >>> assert not res3.endswith('.py')
    """
    if hide_init:
        if basename(modpath) == '__init__.py':
            modpath = dirname(modpath)
            hide_main = True
    else:
        # add in init, if reasonable
        modpath_with_init = join(modpath, '__init__.py')
        if exists(modpath_with_init):
            modpath = modpath_with_init
    if hide_main:
        # We can remove main, but dont add it
        if basename(modpath) == '__main__.py':
            # corner case where main might just be a module name not in a pkg
            parallel_init = join(dirname(modpath), '__init__.py')
            if exists(parallel_init):
                modpath = dirname(modpath)
    return modpath


def _syspath_modname_to_modpath(modname, sys_path=None, exclude=None):
    """
    syspath version of modname_to_modpath

    Args:
        modname (str): name of module to find

        sys_path (None | List[str | PathLike]):
            The paths to search for the module.
            If unspecified, defaults to ``sys.path``.

        exclude (List[str | PathLike] | None):
            If specified prevents these directories from being searched.
            Defaults to None.

    Returns:
        str: path to the module.

    Note:
        This is much slower than the pkgutil mechanisms.

    Example:
        >>> print(_syspath_modname_to_modpath('xdoctest.static_analysis'))
        ...static_analysis.py
        >>> print(_syspath_modname_to_modpath('xdoctest'))
        ...xdoctest
        >>> # xdoctest: +REQUIRES(CPython)
        >>> print(_syspath_modname_to_modpath('_ctypes'))
        ..._ctypes...
        >>> assert _syspath_modname_to_modpath('xdoctest', sys_path=[]) is None
        >>> assert _syspath_modname_to_modpath('xdoctest.static_analysis', sys_path=[]) is None
        >>> assert _syspath_modname_to_modpath('_ctypes', sys_path=[]) is None
        >>> assert _syspath_modname_to_modpath('this', sys_path=[]) is None

    Example:
        >>> # test what happens when the module is not visible in the path
        >>> modname = 'xdoctest.static_analysis'
        >>> modpath = _syspath_modname_to_modpath(modname)
        >>> exclude = [split_modpath(modpath)[0]]
        >>> found = _syspath_modname_to_modpath(modname, exclude=exclude)
        >>> # this only works if installed in dev mode, pypi fails
        >>> assert found is None, 'should not have found {} because we excluded'.format(found, exclude)
    """

    def _isvalid(modpath, base):
        # every directory up to the module, should have an init
        subdir = dirname(modpath)
        while subdir and subdir != base:
            if not exists(join(subdir, '__init__.py')):
                return False
            subdir = dirname(subdir)
        return True

    _fname_we = modname.replace('.', os.path.sep)
    candidate_fnames = [
        _fname_we + '.py',
        # _fname_we + '.pyc',
        # _fname_we + '.pyo',
    ]
    # Add extension library suffixes
    candidate_fnames += [_fname_we + ext for ext in _platform_pylib_exts()]

    if sys_path is None:
        sys_path = sys.path

    # the empty string in sys.path indicates cwd. Change this to a '.'
    candidate_dpaths = ['.' if p == '' else p for p in sys_path]

    if exclude:
        def normalize(p):
            if sys.platform.startswith('win32'):  # nocover
                return realpath(p).lower()
            else:
                return realpath(p)
        # Keep only the paths not in exclude
        real_exclude = {normalize(p) for p in exclude}
        candidate_dpaths = [p for p in candidate_dpaths
                            if normalize(p) not in real_exclude]

    def check_dpath(dpath):
        # Check for directory-based modules (has presidence over files)
        modpath = join(dpath, _fname_we)
        if exists(modpath):
            if isfile(join(modpath, '__init__.py')):
                if _isvalid(modpath, dpath):
                    return modpath

        # If that fails, check for file-based modules
        for fname in candidate_fnames:
            modpath = join(dpath, fname)
            if isfile(modpath):
                if _isvalid(modpath, dpath):
                    return modpath

    _pkg_name = _fname_we.split(os.path.sep)[0]

    for dpath in candidate_dpaths:
        modpath = check_dpath(dpath)
        if modpath:
            return modpath

        # If file path checks fails, check for egg-link based modules
        # (Python usually puts egg links into sys.path, but if the user is
        #  providing the path then it is important to check them explicitly)
        linkpath = join(dpath, _pkg_name + '.egg-link')
        if isfile(linkpath):  # nocover
            # We exclude this from coverage because its difficult to write a
            # unit test where we can enforce that there is a module installed
            # in development mode.

            # TODO: ensure this is the correct way to parse egg-link files
            # https://setuptools.readthedocs.io/en/latest/formats.html#egg-links
            # The docs state there should only be one line, but I see two.
            with open(linkpath, 'r') as file:
                target = file.readline().strip()
            if not exclude or normalize(target) not in real_exclude:
                modpath = check_dpath(target)
                if modpath:
                    return modpath


def modname_to_modpath(modname, hide_init=True, hide_main=False, sys_path=None):
    """
    Finds the path to a python module from its name.

    Determines the path to a python module without directly import it

    Converts the name of a module (__name__) to the path (__file__) where it is
    located without importing the module. Returns None if the module does not
    exist.

    Args:
        modname (str):
            The name of a module in ``sys_path``.

        hide_init (bool):
            if False, __init__.py will be returned for packages.
            Defaults to True.

        hide_main (bool):
            if False, and ``hide_init`` is True, __main__.py will be returned
            for packages, if it exists. Defautls to False.

        sys_path (None | List[str | PathLike]):
            The paths to search for the module.
            If unspecified, defaults to ``sys.path``.

    Returns:
        str | None:
            modpath - path to the module, or None if it doesn't exist

    Example:
        >>> modname = 'xdoctest.__main__'
        >>> modpath = modname_to_modpath(modname, hide_main=False)
        >>> assert modpath.endswith('__main__.py')
        >>> modname = 'xdoctest'
        >>> modpath = modname_to_modpath(modname, hide_init=False)
        >>> assert modpath.endswith('__init__.py')
        >>> # xdoctest: +REQUIRES(CPython)
        >>> modpath = basename(modname_to_modpath('_ctypes'))
        >>> assert 'ctypes' in modpath
    """
    modpath = _syspath_modname_to_modpath(modname, sys_path)
    if modpath is None:
        return None

    modpath = normalize_modpath(modpath, hide_init=hide_init,
                                hide_main=hide_main)
    return modpath


def split_modpath(modpath, check=True):
    """
    Splits the modpath into the dir that must be in PYTHONPATH for the module
    to be imported and the modulepath relative to this directory.

    Args:
        modpath (str): module filepath
        check (bool): if False, does not raise an error if modpath is a
            directory and does not contain an ``__init__.py`` file.

    Returns:
        Tuple[str, str]: (directory, rel_modpath)

    Raises:
        ValueError: if modpath does not exist or is not a package

    Example:
        >>> from xdoctest import static_analysis
        >>> modpath = static_analysis.__file__.replace('.pyc', '.py')
        >>> modpath = abspath(modpath)
        >>> dpath, rel_modpath = split_modpath(modpath)
        >>> recon = join(dpath, rel_modpath)
        >>> assert recon == modpath
        >>> assert rel_modpath == join('xdoctest', 'static_analysis.py')
    """
    modpath_ = abspath(expanduser(modpath))
    if check:
        if not exists(modpath_):
            if not exists(modpath):
                raise ValueError('modpath={} does not exist'.format(modpath))
            raise ValueError('modpath={} is not a module'.format(modpath))
        if isdir(modpath_) and not exists(join(modpath, '__init__.py')):
            # dirs without inits are not modules
            raise ValueError('modpath={} is not a module'.format(modpath))
    full_dpath, fname_ext = split(modpath_)
    _relmod_parts = [fname_ext]
    # Recurse down directories until we are out of the package
    dpath = full_dpath
    while exists(join(dpath, '__init__.py')):
        dpath, dname = split(dpath)
        _relmod_parts.append(dname)
    relmod_parts = _relmod_parts[::-1]
    rel_modpath = os.path.sep.join(relmod_parts)
    return dpath, rel_modpath


def modpath_to_modname(modpath, hide_init=True, hide_main=False, check=True,
                       relativeto=None):
    """
    Determines importable name from file path

    Converts the path to a module (__file__) to the importable python name
    (__name__) without importing the module.

    The filename is converted to a module name, and parent directories are
    recursively included until a directory without an __init__.py file is
    encountered.

    Args:
        modpath (str): module filepath
        hide_init (bool, default=True): removes the __init__ suffix
        hide_main (bool, default=False): removes the __main__ suffix
        check (bool, default=True): if False, does not raise an error if
            modpath is a dir and does not contain an __init__ file.
        relativeto (str, default=None): if specified, all checks are ignored
            and this is considered the path to the root module.

    TODO:
        - [ ] Does this need modification to support PEP 420?
              https://www.python.org/dev/peps/pep-0420/

    Returns:
        str: modname

    Raises:
        ValueError: if check is True and the path does not exist

    Example:
        >>> from xdoctest import static_analysis
        >>> modpath = static_analysis.__file__.replace('.pyc', '.py')
        >>> modpath = modpath.replace('.pyc', '.py')
        >>> modname = modpath_to_modname(modpath)
        >>> assert modname == 'xdoctest.static_analysis'

    Example:
        >>> import xdoctest
        >>> assert modpath_to_modname(xdoctest.__file__.replace('.pyc', '.py')) == 'xdoctest'
        >>> assert modpath_to_modname(dirname(xdoctest.__file__.replace('.pyc', '.py'))) == 'xdoctest'

    Example:
        >>> # xdoctest: +REQUIRES(CPython)
        >>> modpath = modname_to_modpath('_ctypes')
        >>> modname = modpath_to_modname(modpath)
        >>> assert modname == '_ctypes'

    Example:
        >>> modpath = '/foo/libfoobar.linux-x86_64-3.6.so'
        >>> modname = modpath_to_modname(modpath, check=False)
        >>> assert modname == 'libfoobar'
    """
    if check and relativeto is None:
        if not exists(modpath):
            raise ValueError('modpath={} does not exist'.format(modpath))
    modpath_ = abspath(expanduser(modpath))

    modpath_ = normalize_modpath(modpath_, hide_init=hide_init,
                                 hide_main=hide_main)
    if relativeto:
        dpath = dirname(abspath(expanduser(relativeto)))
        rel_modpath = relpath(modpath_, dpath)
    else:
        dpath, rel_modpath = split_modpath(modpath_, check=check)

    modname = splitext(rel_modpath)[0]
    if '.' in modname:
        modname, abi_tag = modname.split('.', 1)
    modname = modname.replace('/', '.')
    modname = modname.replace('\\', '.')
    return modname



"""
xdoctest.static_analysis.package_modpaths
"""
def package_modpaths(pkgpath, with_pkg=False, with_mod=True, followlinks=True,
                     recursive=True, with_libs=False, check=True):
    r"""
    Finds sub-packages and sub-modules belonging to a package.

    Args:
        pkgpath (str): path to a module or package
        with_pkg (bool): if True includes package __init__ files (default =
            False)
        with_mod (bool): if True includes module files (default = True)
        exclude (list): ignores any module that matches any of these patterns
        recursive (bool): if False, then only child modules are included
        with_libs (bool): if True then compiled shared libs will be returned as well
        check (bool): if False, then then pkgpath is considered a module even
            if it does not contain an __init__ file.

    Yields:
        str: module names belonging to the package

    References:
        http://stackoverflow.com/questions/1707709/list-modules-in-py-package

    Example:
        >>> from xdoctest.static_analysis import *
        >>> pkgpath = modname_to_modpath('xdoctest')
        >>> paths = list(package_modpaths(pkgpath))
        >>> print('\n'.join(paths))
        >>> names = list(map(modpath_to_modname, paths))
        >>> assert 'xdoctest.core' in names
        >>> assert 'xdoctest.__main__' in names
        >>> assert 'xdoctest' not in names
        >>> print('\n'.join(names))
    """
    if isfile(pkgpath):
        # If input is a file, just return it
        yield pkgpath
    else:
        if with_pkg:
            root_path = join(pkgpath, '__init__.py')
            if not check or exists(root_path):
                yield root_path

        valid_exts = ['.py']
        if with_libs:
            valid_exts += _platform_pylib_exts()

        for dpath, dnames, fnames in os.walk(pkgpath, followlinks=followlinks):
            ispkg = exists(join(dpath, '__init__.py'))
            if ispkg or not check:
                check = True  # always check subdirs
                if with_mod:
                    for fname in fnames:
                        if splitext(fname)[1] in valid_exts:
                            # dont yield inits. Handled in pkg loop.
                            if fname != '__init__.py':
                                path = join(dpath, fname)
                                yield path
                if with_pkg:
                    for dname in dnames:
                        path = join(dpath, dname, '__init__.py')
                        if exists(path):
                            yield path
            else:
                # Stop recursing when we are out of the package
                del dnames[:]
            if not recursive:
                break
