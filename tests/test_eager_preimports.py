"""
Tests for `line_profiler.autoprofile.eager_preimports`.

Notes
-----
Most of the features are already covered by the doctests, but this
project doesn't generally use the `--doctest-modules` option. So this is
mostly a hook to run the doctests.
"""
import doctest
import functools
import importlib
import pathlib
from types import ModuleType
from typing import Any, Callable, Dict, Type, Union

import pytest
from _pytest import doctest as pytest_doctest

from line_profiler.autoprofile import eager_preimports, util_static


CAN_USE_PYTEST_DOCTEST = True

try:
    class PytestDoctestRunner(pytest_doctest._init_runner_class()):
        # Neuter these methods because they expect `out` to be a
        # callable while `pytest` passes a list

        def report_start(self, out, *args, **kwargs):
            pass

        def report_success(self, out, *args, **kwargs):
            pass
except Exception:
    CAN_USE_PYTEST_DOCTEST = False


def create_doctest_wrapper(
        test: doctest.DocTest, *,
        fname: Union[str, pathlib.PurePath, None] = None,
        globs: Union[Dict[str, Any], None] = None,
        name: Union[str, None] = None,
        strip_prefix: Union[str, None] = None,
        test_name_prefix: str = 'test_doctest_',
        use_pytest_doctest: bool = CAN_USE_PYTEST_DOCTEST) -> Callable:
    """
    Create a hook to run a doctest as if it was a regular test.

    Returns
        wrapper (Callable):
            Test function
    """
    if strip_prefix is not None:
        assert test.name.startswith(strip_prefix)
        bare_name = test.name[len(strip_prefix):].lstrip('.').replace('.', '_')
        if not bare_name:
            bare_name = strip_prefix
    else:
        bare_name = test.name.replace('.', '_')
    if name is None:
        name = test_name_prefix + bare_name
    assert name.isidentifier()
    if fname is not None:
        fname = pathlib.Path(fname)

    use_pytest_doctest = bool(use_pytest_doctest) & CAN_USE_PYTEST_DOCTEST
    try:
        item_from_parent = pytest_doctest.DoctestItem.from_parent
        module_from_parent = pytest_doctest.DoctestModule.from_parent
        get_doctest_option_flags = pytest_doctest.get_optionflags
        xc_to_info = pytest.ExceptionInfo.from_exception
        checker = pytest_doctest._get_checker()
    except Exception:
        use_pytest_doctest = False

    def wrapper_pytest(request: pytest.FixtureRequest) -> None:
        if globs is not None:
            test.globs = globs.copy()
        module = module_from_parent(parent=request.session, path=fname)
        runner = PytestDoctestRunner(
            checker=checker,
            optionflags=get_doctest_option_flags(request.config),
            continue_on_failure=False)
        item = item_from_parent(module, name=name, runner=runner, dtest=test)
        item.setup()
        try:
            item.runtest()
        except doctest.UnexpectedException as e:
            msg = '{}\n{}'.format(e.exc_info[0].__name__,
                                  item.repr_failure(xc_to_info(e)))
            raise pytest.fail(msg) from None

    def wrapper_vanilla() -> None:
        if globs is not None:
            test.globs = globs.copy()
        runner = doctest.DebugRunner()
        runner.run(test)

    if use_pytest_doctest:
        wrapper = wrapper_pytest
    else:
        wrapper = wrapper_vanilla

    doctest_backend = '_pytest.doctest' if use_pytest_doctest else 'doctest'
    wrapper.__name__ = name
    wrapper.__doc__ = ('Run the doctest for `{}` with the facilities of `{}`'
                       .format(bare_name, doctest_backend))
    return wrapper


def regularize_doctests(
        obj: Any, *,
        namespace: Union[Dict[str, Any], None] = None,
        finder: Union[doctest.DocTestFinder, None] = None,
        strip_common_prefix: bool = True,
        use_pytest_doctest: bool = CAN_USE_PYTEST_DOCTEST) -> Dict[str,
                                                                   Callable]:
    """
    Gather doctests from `obj` and make them regular test functions.

    Returns:
        wrappers (dict[str, Callable]):
            Dictionary from test names to Test functions
    """
    if isinstance(obj, ModuleType):
        prefix = module = obj.__name__
        fname = obj.__file__
        globs = vars(obj)
    else:
        module = obj.__module__
        prefix = f'{module}.{obj.__qualname__}'
        fname = util_static.modname_to_modpath(module)
        globs = vars(importlib.import_module(module))

    if finder is None:
        finder = doctest.DocTestFinder()

    make_wrapper = functools.partial(
        create_doctest_wrapper,
        fname=fname, globs=globs, strip_prefix=prefix,
        use_pytest_doctest=use_pytest_doctest)

    tests = [make_wrapper(test) for test in finder.find(obj) if test.examples]
    result = {test.__name__: test for test in tests}
    if namespace is None:
        return result
    for name, test in result.items():
        if name in namespace:
            test_module = getattr(namespace.get('__spec__'), 'name', '???')
            raise AttributeError(f'module `{test_module}` already has a test '
                                 f'(or other entity) named `{name}()`')
        namespace[name] = test
    return result


@pytest.mark.parametrize(
    'adder, xc, msg',
    [('foo; bar', ValueError, None),
     (1, TypeError, None),
     ('(foo\n .bar)', ValueError, None)])
def test_write_eager_import_module_wrong_adder(
        adder: Any, xc: Type[Exception], msg: Union[str, None]) -> None:
    """
    Test passing an erroneous `adder` to `write_eager_import_module()`.
    """
    with pytest.raises(xc, match=msg):
        eager_preimports.write_eager_import_module(['foo'], adder=adder)


regularize_doctests(eager_preimports, namespace=globals())
