import unittest
import io

from IPython.testing.globalipapp import get_ipython

class TestIPython(unittest.TestCase):
    def test_init(self):
        ip = get_ipython()
        ip.run_line_magic('load_ext', 'line_profiler')
        ip.run_cell(raw_cell='def func():\n    return 2**20')
        ip.run_line_magic('lprun', '-f func func()')
        # TODO: Check output
