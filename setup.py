#!/usr/bin/env python
from os.path import exists
import sys
import os
import warnings
import setuptools


def _choose_build_method():
    DISABLE_C_EXTENSIONS = os.environ.get("DISABLE_C_EXTENSIONS", "").lower()
    LINE_PROFILER_BUILD_METHOD = os.environ.get("LINE_PROFILER_BUILD_METHOD", "auto").lower()

    if DISABLE_C_EXTENSIONS in {"true", "on", "yes", "1"}:
        LINE_PROFILER_BUILD_METHOD = 'setuptools'

    if LINE_PROFILER_BUILD_METHOD == 'auto':
        try:
            import Cython  # NOQA
        except ImportError:
            try:
                import skbuild  # NOQA
                import cmake  # NOQA
                import ninja  # NOQA
            except ImportError:
                # The main fallback disables c-extensions
                LINE_PROFILER_BUILD_METHOD = 'setuptools'
            else:
                # This should never be hit
                LINE_PROFILER_BUILD_METHOD = 'scikit-build'
        else:
            # Use plain cython by default
            LINE_PROFILER_BUILD_METHOD = 'cython'

    return LINE_PROFILER_BUILD_METHOD


def parse_version(fpath):
    """
    Statically parse the version number from a python file
    """
    value = static_parse("__version__", fpath)
    return value


def static_parse(varname, fpath):
    """
    Statically parse the a constant variable from a python file
    """
    import ast

    if not exists(fpath):
        raise ValueError("fpath={!r} does not exist".format(fpath))
    with open(fpath, "r") as file_:
        sourcecode = file_.read()
    pt = ast.parse(sourcecode)

    class StaticVisitor(ast.NodeVisitor):
        def visit_Assign(self, node):
            for target in node.targets:
                if getattr(target, "id", None) == varname:
                    self.static_value = node.value.s

    visitor = StaticVisitor()
    visitor.visit(pt)
    try:
        value = visitor.static_value
    except AttributeError:
        value = "Unknown {}".format(varname)
        warnings.warn(value)
    return value


def parse_description():
    """
    Parse the description in the README file

    CommandLine:
        pandoc --from=markdown --to=rst --output=README.rst README.md
        python -c "import setup; print(setup.parse_description())"
    """
    from os.path import dirname, join, exists

    readme_fpath = join(dirname(__file__), "README.rst")
    # This breaks on pip install, so check that it exists.
    if exists(readme_fpath):
        with open(readme_fpath, "r") as f:
            text = f.read()
        return text
    return ""


def parse_requirements(fname="requirements.txt", versions=False):
    """
    Parse the package dependencies listed in a requirements file but strips
    specific versioning information.

    Args:
        fname (str): path to requirements file
        versions (bool | str, default=False):
            If true include version specs.
            If strict, then pin to the minimum version.

    Returns:
        List[str]: list of requirements items
    """
    from os.path import exists, dirname, join
    import re

    require_fpath = fname

    def parse_line(line, dpath=""):
        """
        Parse information from a line in a requirements text file

        line = 'git+https://a.com/somedep@sometag#egg=SomeDep'
        line = '-e git+https://a.com/somedep@sometag#egg=SomeDep'
        """
        # Remove inline comments
        comment_pos = line.find(" #")
        if comment_pos > -1:
            line = line[:comment_pos]

        if line.startswith("-r "):
            # Allow specifying requirements in other files
            target = join(dpath, line.split(" ")[1])
            for info in parse_require_file(target):
                yield info
        else:
            # See: https://www.python.org/dev/peps/pep-0508/
            info = {"line": line}
            if line.startswith("-e "):
                info["package"] = line.split("#egg=")[1]
            else:
                if ";" in line:
                    pkgpart, platpart = line.split(";")
                    # Handle platform specific dependencies
                    # setuptools.readthedocs.io/en/latest/setuptools.html
                    # #declaring-platform-specific-dependencies
                    plat_deps = platpart.strip()
                    info["platform_deps"] = plat_deps
                else:
                    pkgpart = line
                    platpart = None

                # Remove versioning from the package
                pat = "(" + "|".join([">=", "==", ">"]) + ")"
                parts = re.split(pat, pkgpart, maxsplit=1)
                parts = [p.strip() for p in parts]

                info["package"] = parts[0]
                if len(parts) > 1:
                    op, rest = parts[1:]
                    version = rest  # NOQA
                    info["version"] = (op, version)
            yield info

    def parse_require_file(fpath):
        dpath = dirname(fpath)
        with open(fpath, "r") as f:
            for line in f.readlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    for info in parse_line(line, dpath=dpath):
                        yield info

    def gen_packages_items():
        if exists(require_fpath):
            for info in parse_require_file(require_fpath):
                parts = [info["package"]]
                if versions and "version" in info:
                    if versions == "strict":
                        # In strict mode, we pin to the minimum version
                        if info["version"]:
                            # Only replace the first >= instance
                            verstr = "".join(info["version"]).replace(">=", "==", 1)
                            parts.append(verstr)
                    else:
                        parts.extend(info["version"])
                if not sys.version.startswith("3.4"):
                    # apparently package_deps are broken in 3.4
                    plat_deps = info.get("platform_deps")
                    if plat_deps is not None:
                        parts.append(";" + plat_deps)
                item = "".join(parts)
                yield item

    packages = list(gen_packages_items())
    return packages


long_description = """\
line_profiler will profile the time individual lines of code take to execute.
The profiler is implemented in C via Cython in order to reduce the overhead of
profiling.

Also included is the script kernprof.py which can be used to conveniently
profile Python applications and scripts either with line_profiler or with the
function-level profiling tools in the Python standard library.
"""


NAME = "line_profiler"
INIT_PATH = "line_profiler/line_profiler.py"
VERSION = parse_version(INIT_PATH)


if __name__ == '__main__':
    setupkw = {}

    LINE_PROFILER_BUILD_METHOD = _choose_build_method()
    if LINE_PROFILER_BUILD_METHOD == 'setuptools':
        setup = setuptools.setup
    elif LINE_PROFILER_BUILD_METHOD == 'scikit-build':
        import skbuild  # NOQA
        setup = skbuild.setup
    elif LINE_PROFILER_BUILD_METHOD == 'cython':
        # no need to try importing cython because an import
        # was already attempted in _choose_build_method
        import multiprocessing
        from setuptools import Extension
        from Cython.Build import cythonize

        def run_cythonize(force=False):
            return cythonize(
                Extension(
                    name="line_profiler._line_profiler",
                    sources=["line_profiler/_line_profiler.pyx", "line_profiler/timers.c", "line_profiler/unset_trace.c"],
                    language="c++",
                    define_macros=[("CYTHON_TRACE", (1 if os.getenv("DEV") == "true" else 0))],
                ),
                compiler_directives={
                    "language_level": 3,
                    "infer_types": True,
                    "legacy_implicit_noexcept": True,
                    "linetrace": (True if os.getenv("DEV") == "true" else False)
                },
                include_path=["line_profiler/python25.pxd"],
                force=force,
                nthreads=multiprocessing.cpu_count(),
            )

        setupkw.update(dict(ext_modules=run_cythonize()))
        setup = setuptools.setup
    else:
        raise Exception('Unknown build method')

    setupkw["install_requires"] = parse_requirements(
        "requirements/runtime.txt", versions="loose"
    )
    setupkw["extras_require"] = {
        "all": parse_requirements("requirements.txt", versions="loose"),
        "tests": parse_requirements("requirements/tests.txt", versions="loose"),
        "optional": parse_requirements("requirements/optional.txt", versions="loose"),
        "all-strict": parse_requirements("requirements.txt", versions="strict"),
        "runtime-strict": parse_requirements(
            "requirements/runtime.txt", versions="strict"
        ),
        "tests-strict": parse_requirements("requirements/tests.txt", versions="strict"),
        "optional-strict": parse_requirements(
            "requirements/optional.txt", versions="strict"
        ),
        "ipython": parse_requirements('requirements/ipython.txt', versions="loose"),
        "ipython-strict": parse_requirements('requirements/ipython.txt', versions="strict"),
    }
    setupkw['entry_points'] = {
        'console_scripts': [
            'kernprof=kernprof:main',
        ],
    }
    setupkw["name"] = NAME
    setupkw["version"] = VERSION
    setupkw["author"] = "Robert Kern"
    setupkw["author_email"] = "robert.kern@enthought.com"
    setupkw["url"] = "https://github.com/pyutils/line_profiler"
    setupkw["description"] = "Line-by-line profiler"
    setupkw["long_description"] = parse_description()
    setupkw["long_description_content_type"] = "text/x-rst"
    setupkw["license"] = "BSD"
    setupkw["packages"] = list(setuptools.find_packages())
    setupkw["py_modules"] = ['kernprof', 'line_profiler']
    setupkw["python_requires"] = ">=3.6"
    setupkw['license_files'] = ['LICENSE.txt', 'LICENSE_Python.txt']
    setupkw["package_data"] = {"line_profiler": ["py.typed", "*.pyi"]}
    setupkw['keywords'] = ['timing', 'timer', 'profiling', 'profiler', 'line_profiler']
    setupkw["classifiers"] = [
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: C',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: Implementation :: CPython',
        'Topic :: Software Development',
    ]
    setup(**setupkw)
