import unittest


class TestIPython(unittest.TestCase):
    def test_init(self):
        """
        CommandLine:
            pytest -k test_init -s -v
        """
        try:
            from IPython.testing.globalipapp import get_ipython
        except ImportError:
            import pytest
            pytest.skip()

        ip = get_ipython()
        ip.run_line_magic('load_ext', 'line_profiler')
        ip.run_cell(raw_cell='def func():\n    return 2**20')
        lprof = ip.run_line_magic('lprun', '-r -f func func()')

        timings = lprof.get_stats().timings
        self.assertEqual(len(timings), 1)  # 1 function

        func_data, lines_data = next(iter(timings.items()))
        print(f'func_data={func_data}')
        print(f'lines_data={lines_data}')
        self.assertEqual(func_data[1], 1)  # lineno of the function
        self.assertEqual(func_data[2], "func")  # function name
        self.assertEqual(len(lines_data), 1)  # 1 line of code
        self.assertEqual(lines_data[0][0], 2)  # lineno
        self.assertEqual(lines_data[0][1], 1)  # hits

    def test_lprun_all_autoprofile(self):
        try:
            from IPython.testing.globalipapp import get_ipython
        except ImportError:
            import pytest
            pytest.skip()
        
        loops = 20000
        # use a more complex example with 2 scopes because using autoprofile is usually
        # pointless if only 1 function is tested
        cell_body = f"""
        class Test1:
            def test2(self):
                loops = {loops}
                for x in range(loops):
                    y = x
                    if x == (loops - 2):
                        break
        Test1().test2()
        """

        ip = get_ipython()
        ip.run_line_magic('load_ext', 'line_profiler')
        lprof = ip.run_cell_magic('lprun_all', line='-r', cell=cell_body)
        timings = lprof.get_stats().timings
        
        # 2 scopes: the class scope (Test1) and the inner scope (test2)
        self.assertEqual(len(timings), 2)

        timings_iter = iter(timings.items())
        func_1_data, lines_1_data = next(timings_iter)
        func_2_data, lines_2_data = next(timings_iter)
        print(f'func_1_data={func_1_data}')
        print(f'lines_1_data={lines_1_data}')
        self.assertEqual(func_1_data[1], 1)  # lineno of the outer function
        self.assertEqual(len(lines_1_data), 2)  # only 2 lines were executed in this outer scope
        self.assertEqual(lines_1_data[0][0], 3)  # lineno
        self.assertEqual(lines_1_data[0][1], 1)  # hits
        
        print(f'func_2_data={func_2_data}')
        print(f'lines_2_data={lines_2_data}')
        self.assertEqual(func_2_data[1], 4)  # lineno of the inner function
        self.assertEqual(len(lines_2_data), 5)  # only 5 lines were executed in this inner scope
        self.assertEqual(lines_2_data[1][0], 6)  # lineno
        self.assertEqual(lines_2_data[1][1], loops - 1)  # hits
    
    def test_lprun_all_timetaken(self):
        try:
            from IPython.testing.globalipapp import get_ipython
        except ImportError:
            import pytest
            pytest.skip()
            
        cell_body = """
        class Test:
            def test(self):
                loops = 20000
                for x in range(loops):
                    y = x
                    if x == (loops - 2):
                        break
        Test().test()
        """

        ip = get_ipython()
        ip.run_line_magic('load_ext', 'line_profiler')
        ip.run_cell_magic('lprun_all', line='-t', cell=cell_body)
        self.assertTrue(ip.user_ns.get("_total_time_taken", None) is not None)
