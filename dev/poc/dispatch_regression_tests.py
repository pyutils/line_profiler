from cibuildwheel.oci_container import OCIContainer
from cibuildwheel.oci_container import OCIPlatform
import ubelt as ub
container = OCIContainer(image='python:3.8', oci_platform=OCIPlatform.AMD64)
container.__enter__()

script_path = ub.Path('~/code/line_profiler/dev/poc/regression_test.py').expand()
container_script_path = ub.Path('regression_test.py')
container.copy_into(script_path, container_script_path)

container.call(['pip', 'install', 'uv'])
container.call(['uv', 'pip', 'install', '--system', 'kwutil', 'ubelt', 'scriptconfig', 'psutil', 'ruamel.yaml', 'py-cpuinfo'])

line_profiler_versions = [
    '5.0.0',
    '4.2.0',
    '4.0.0',
]

for line_profiler_version in line_profiler_versions:
    container.call(['uv', 'pip', 'install', '--system', f'line_profiler=={line_profiler_version}'])
    container.call(['python', container_script_path])

container.copy_out(ub.Path('/root/.cache/line_profiler/benchmarks/'), ub.Path('bench_results'))

container.__exit__(None, None, None)


import kwutil
result_paths = kwutil.util_path.coerce_patterned_paths('bench_results', expected_extension='.yaml')

import sys, ubelt
sys.path.append(ubelt.expandpath('~/code/line_profiler/dev/poc'))
from gather_regression_tests import accumulate_results
from gather_regression_tests import plot_results

df = accumulate_results(result_paths)
df = df.sort_values('params.line_profiler_version')
plot_results(df)
