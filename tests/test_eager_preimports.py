"""
Tests for :py:mod:`line_profiler.autoprofile.eager_preimports`.

Notes
-----
Most of the features are already covered by the doctests.
"""
import pytest

from line_profiler.autoprofile import eager_preimports


@pytest.mark.parametrize(
    'adder, xc, msg',
    [('foo; bar', ValueError, None),
     (1, TypeError, None),
     ('(foo\n .bar)', ValueError, None)])
def test_write_eager_import_module_wrong_adder(adder, xc, msg) -> None:
    """
    Test passing an erroneous ``adder`` to
    :py:meth:`~.write_eager_import_module()`.
    """
    with pytest.raises(xc, match=msg):
        eager_preimports.write_eager_import_module(['foo'], adder=adder)
