from cibuildwheel.oci_container import OCIContainer
from cibuildwheel.oci_container import OCIPlatform
import ubelt as ub

python_versions = [
    '3.9',
    '3.10',
    '3.11',
    '3.12',
    '3.13',
]

for python_version in python_versions:
    current_pyver = 'cp' + python_version.replace('.', '')
    image = 'python:' + python_version
    container = OCIContainer(image=image, oci_platform=OCIPlatform.AMD64)

    container.__enter__()   # not using with for ipython dev

    script_path = ub.Path('~/code/line_profiler/dev/poc/regression_test.py').expand()
    container_script_path = ub.Path('regression_test.py')
    container.copy_into(script_path, container_script_path)

    container.call(['pip', 'install', 'uv'])
    container.call(['uv', 'pip', 'install', '--system', 'kwutil', 'ubelt', 'scriptconfig', 'psutil', 'ruamel.yaml', 'py-cpuinfo'])

    line_profiler_versions = [
        # '4.0.0',
        '4.1.2',
        '4.2.0',
        '5.0.0',
    ]

    for line_profiler_version in line_profiler_versions:
        container.call(['uv', 'pip', 'uninstall', '--system', 'line_profiler'])
        container.call(['uv', 'pip', 'install', '--system', f'line_profiler=={line_profiler_version}'])
        for _ in range(5):
            container.call(['python', container_script_path])

    # Test the latest wheels (requires these are built beforehand)
    local_wheels = ub.Path('/home/joncrall/code/line_profiler/wheelhouse')
    container.copy_into(local_wheels, ub.Path('wheelhouse'))
    line_profiler_version = '5.0.1'
    found = list(local_wheels.glob('*' + current_pyver + '-manylinux*'))
    assert len(found) == 1
    wheel_name = found[0].name
    container.call(['uv', 'pip', 'uninstall', '--system', 'line_profiler'])
    container.call(['uv', 'pip', 'install', '--system', 'wheelhouse/' + str(wheel_name)])
    for _ in range(5):
        container.call(['python', container_script_path])

    container.copy_out(ub.Path('/root/.cache/line_profiler/benchmarks/'), ub.Path('bench_results'))

    container.__exit__(None, None, None)

# FIXME: robustness
import kwutil
result_paths = kwutil.util_path.coerce_patterned_paths('bench_results', expected_extension='.yaml')
import sys, ubelt
sys.path.append(ubelt.expandpath('~/code/line_profiler/dev/poc'))
from gather_regression_tests import accumulate_results
from gather_regression_tests import plot_results

df = accumulate_results(result_paths)
df = df.sort_values('params.line_profiler_version')
plot_results(df)
