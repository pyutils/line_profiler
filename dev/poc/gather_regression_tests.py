#!/usr/bin/env python3
import scriptconfig as scfg
import kwutil
import ubelt as ub


def nested_to_dotdict(data):
    """
    Construct this flat representation from a nested one

    Args:
        data (Dict):
            nested data

        Example:
            >>> data = {
            >>>     'type': 'process',
            >>>     'properties': {
            >>>         'machine': {
            >>>             'os_name': 'Linux',
            >>>             'arch': 'x86_64',
            >>>             'py_impl': 'CPython',
            >>>         }}}
            >>> flat = nested_to_dotdict(data['properties'])
            >>> print(f'flat = {ub.urepr(flat, nl=2)}')
            flat = {
                'machine.os_name': 'Linux',
                'machine.arch': 'x86_64',
                'machine.py_impl': 'CPython',
            }
    """
    flat = dict()
    walker = ub.IndexableWalker(data, list_cls=tuple())
    for path, value in walker:
        if not isinstance(value, dict):
            spath = list(map(str, path))
            key = '.'.join(spath)
            flat[key] = value
    return flat


def insert_prefix(self, prefix, index=0):
    """
    Adds a prefix to all items

    Args:
        prefix (str): prefix to insert
        index (int): the depth to insert the new param

    Example:
        >>> self = dict({
        >>>     'proc1.param1': 1,
        >>>     'proc2.param1': 3,
        >>>     'proc4.part2.param2': 10,
        >>> })
        >>> new = insert_prefix(self, 'foo', index=1)
        >>> print('new = {}'.format(ub.urepr(new, nl=1)))
        new = {
            'foo.proc1.param1': 1,
            'foo.proc2.param1': 3,
            'foo.proc4.part2.param2': 10,
        }
    """
    def _generate_new_items():
        sep = '.'
        for k, v in self.items():
            path = k.split(sep)
            path.insert(index, prefix)
            k2 = sep.join(path)
            yield k2, v
    new = self.__class__(_generate_new_items())
    return new


class GatherRegressionTestsCLI(scfg.DataConfig):
    """
    """
    paths = scfg.Value(None, help='output results from profiling codes')

    @classmethod
    def main(cls, argv=1, **kwargs):
        """
        Example:
            >>> # xdoctest: +SKIP
            >>> from gather_regression_tests import *  # NOQA
            >>> argv = 0
            >>> kwargs = dict()
            >>> cls = GatherRegressionTestsCLI
            >>> config = cls(**kwargs)
            >>> cls.main(argv=argv, **config)
        """
        config = cls.cli(argv=argv, data=kwargs, strict=True, verbose='auto')
        result_paths = kwutil.util_path.coerce_patterned_paths(config.paths, expected_extension='.yaml')

        df = accumulate_results(result_paths)
        plot_results(df)


def plot_results(df):
    import kwplot
    sns = kwplot.autosns()
    figman = kwplot.FigureManager(dpath='.')
    fig = figman.figure()
    fig.clf()
    ax = fig.gca()
    sns.lineplot(data=df, x='params.line_profiler_version', y='record.duration', hue='record.line_profiler.enabled', ax=ax)
    figman.finalize('regression_plot.png')


def accumulate_results(result_paths):
    import pandas as pd
    records_accum = []
    for fpath in result_paths:
        results = kwutil.Yaml.coerce(fpath)
        records = results['records']
        context = results['context']['properties']
        _flat_context = nested_to_dotdict(context)
        flat_context = insert_prefix(_flat_context, 'context')

        flat_params = insert_prefix(nested_to_dotdict(
            results['params']), 'params')
        for record in records:
            _flat_record = nested_to_dotdict(record)
            flat_record = insert_prefix(_flat_record, 'record')
            flat_record.update(flat_context)
            flat_record.update(flat_params)
            records_accum.append(flat_record)
    df = pd.DataFrame(records_accum)
    return df

__cli__ = GatherRegressionTestsCLI

if __name__ == '__main__':
    """

    CommandLine:
        python ~/code/line_profiler/dev/poc/gather_regression_tests.py
        python -m gather_regression_tests
    """
    __cli__.main()
