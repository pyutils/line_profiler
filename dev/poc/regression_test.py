#!/usr/bin/env python3
import ubelt as ub
import sys
import scriptconfig as scfg


# TODO: more than one type of benchmark would be a good idea

BENCHMARK_CODE = ub.codeblock(
    '''
    if 'profile' not in globals():
        try:
            from line_profiler import profile
        except ImportError:
            profile = lambda x: x

    @profile
    def main(profiled=False):
         import time
         start = time.perf_counter()
         for x in range(2000000): #2000000
             y = x
         elapsed_ms = round((time.perf_counter()-start)*1000, 2)
         if profiled:
             print(f"Time Elapsed profiling: {elapsed_ms}ms")
         else:
            print(f"Time Elapsed without profiling: {elapsed_ms}ms")
         return elapsed_ms

    if __name__ == '__main__':
        main()
    ''')


class RegressionTestCLI(scfg.DataConfig):
    out_fpath = scfg.Value('auto', help='place to write results')

    @classmethod
    def main(cls, argv=1, **kwargs):
        """
        Example:
            >>> # xdoctest: +SKIP
            >>> from regression_test import *  # NOQA
            >>> argv = 0
            >>> kwargs = dict()
            >>> cls = RegressionTestCLI
            >>> config = cls(**kwargs)
            >>> cls.main(argv=argv, **config)
        """
        config = cls.cli(argv=argv, data=kwargs, strict=True, verbose='auto')

        import kwutil
        import line_profiler
        context = kwutil.ProcessContext().start()

        dpath = ub.Path.appdir('line_profiler/benchmarks').ensuredir()

        code = BENCHMARK_CODE
        code_id = ub.hash_data(code)[0:16]
        fpath = dpath / f'{code_id}.py'
        fpath.write_text(code)

        # TODO: can we tag the hash of the wheel we used to install?
        # We need to be able to differentiate dev versions.

        results = {
            'context': context.obj,
            'params':  {
                'line_profiler_version': line_profiler.__version__,
                'code_id': code_id,
            },
            'records': [],
            'line_records': [],
        }

        # result_path = fpath.augment(stemsuffix='_' + ub.timestamp(), ext='.lprof')
        result_path = fpath.augment(ext='.py.lprof')

        with ub.Timer(ns=True) as noprof_timer:
            res = ub.cmd([sys.executable, fpath], verbose=3, cwd=fpath.parent)
        res.check_returncode()
        results['records'].append({
            'line_profiler.enabled': False,
            'duration': noprof_timer.elapsed,
        })

        with ub.Timer(ns=True) as prof_timer:
            res = ub.cmd([sys.executable, '-m', 'kernprof', '-lv', fpath], verbose=3, cwd=fpath.parent)
        res.check_returncode()
        results['records'].append({
            'line_profiler.enabled': True,
            'duration': prof_timer.elapsed,
        })

        # res2 = ub.cmd([sys.executable, '-m', 'line_profiler', '-rmt', result_path], verbose=3)
        stats = line_profiler.load_stats(result_path)

        for key, timings in stats.timings.items():
            _path, _line, _name = key
            for lineno, hits, time in timings:
                results['line_records'].append({
                    'time': time,
                    'lineno': lineno,
                })

        context.stop()
        print(f'results = {ub.urepr(results, nl=2)}')

        out_text = kwutil.Yaml.dumps(results)
        if config.out_fpath == 'auto':
            out_fpath = fpath.augment('regression_test_' + ub.timestamp(), ext='.yaml')
        else:
            out_fpath = config.out_fpath
        out_fpath.write_text(out_text)

__cli__ = RegressionTestCLI

if __name__ == '__main__':
    """

    CommandLine:
        python ~/code/line_profiler/dev/poc/regression_test.py
        python -m regression_test
    """
    __cli__.main()
