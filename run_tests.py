#!/usr/bin/env python
"""
Based on template in rc/run_tests.binpy.py.in
"""
import os
import sqlite3
import sys
import re


def is_cibuildwheel():
    """Check if run with cibuildwheel."""
    return 'CIBUILDWHEEL' in os.environ


# def temp_rename_kernprof(repo_dir):
#     """
#     Hacky workaround so kernprof.py doesn't get covered twice (installed and local).
#     This needed to combine the .coverage files, since file paths need to be unique.
#     """
#     original_path = repo_dir + '/kernprof.py'
#     tmp_path = original_path + '.tmp'
#     if os.path.isfile(original_path):
#         os.rename(original_path, tmp_path)
#     elif os.path.isfile(tmp_path):
#         os.rename(tmp_path, original_path)


def replace_docker_path(path, runner_project_dir):
    """Update path to a file installed in a temp venv to runner_project_dir."""
    pattern = re.compile(r"\/tmp\/.+?\/site-packages")
    return pattern.sub(runner_project_dir, path)


def update_coverage_file(coverage_path, runner_project_dir):
    """
    Since the paths inside of docker vary from the runner paths,
    the paths in the .coverage file need to be adjusted to combine them,
    since 'coverage combine <folder>' checks if the file paths exist.
    """
    try:
        sqliteConnection = sqlite3.connect(coverage_path)
        cursor = sqliteConnection.cursor()
        print('Connected to Coverage SQLite')

        read_file_query = 'SELECT id, path from file'
        cursor.execute(read_file_query)

        old_records = cursor.fetchall()
        new_records = [(replace_docker_path(path, runner_project_dir), _id) for _id, path in old_records]
        print('Updated coverage file paths:\n', new_records)

        sql_update_query = 'Update file set path = ? where id = ?'
        cursor.executemany(sql_update_query, new_records)
        sqliteConnection.commit()
        print('Coverage Updated successfully')
        cursor.close()

    except sqlite3.Error as error:
        print('Failed to coverage: ', error)
    finally:
        if sqliteConnection:
            sqliteConnection.close()
            print('The sqlite connection is closed')


def copy_coverage_cibuildwheel_docker(runner_project_dir):
    """
    When run with cibuildwheel under linux, the tests run in the folder /project
    inside docker and the coverage files need to be copied to the output folder.
    """
    coverage_path = '/project/tests/.coverage'
    if os.path.isfile(coverage_path):
        update_coverage_file(coverage_path, runner_project_dir)
        env_hash = hash((sys.version, os.environ.get('AUDITWHEEL_PLAT', '')))
        os.makedirs('/output', exist_ok=True)
        os.rename(coverage_path, '/output/.coverage.{}'.format(env_hash))


def main():
    import pathlib
    orig_cwd = os.getcwd()
    repo_dir = pathlib.Path(__file__).parent.absolute()
    test_dir = repo_dir / 'tests'
    print('[run_tests] cwd = {!r}'.format(orig_cwd))

    print('[run_tests] Changing dirs to test_dir={!r}'.format(test_dir))
    os.chdir(test_dir)

    testdir_contents = list(pathlib.Path(test_dir).glob('*'))
    pyproject_fpath = repo_dir / 'pyproject.toml'

    print(f'[run_tests] repo_dir = {repo_dir}')
    print(f'[run_tests] pyproject_fpath = {pyproject_fpath}')
    print(f'[run_tests] test_dir={test_dir}')

    # Prefer testing the installed version, but fallback to testing the
    # development version.
    try:
        import ubelt as ub
    except ImportError:
        print('running this test script requires ubelt')
        raise

    print(f'[run_tests] testdir_contents = {ub.urepr(testdir_contents, nl=1)}')
    print(f'[run_tests] sys.path = {ub.urepr(sys.path, nl=1)}')

    package_name = 'line_profiler'
    # Statically check if ``package_name`` is installed outside of the repo.
    # To do this, we make a copy of PYTHONPATH, remove the repodir, and use
    # ubelt to check to see if ``package_name`` can be resolved to a path.
    temp_path = [pathlib.Path(p).resolve() for p in sys.path]
    _resolved_repo_dir = repo_dir.resolve()
    print(f'[run_tests] Searching for installed version of {package_name}.')
    try:
        _idx = temp_path.index(_resolved_repo_dir)
    except IndexError:
        print('[run_tests] Confirmed repo dir is not in sys.path')
    else:
        print(f'[run_tests] Removing _resolved_repo_dir={_resolved_repo_dir} from search path')
        del temp_path[_idx]
        if is_cibuildwheel():
            # Remove from sys.path to prevent the import mechanism from testing
            # the source repo rather than the installed wheel.
            print(f'[run_tests] Removing _resolved_repo_dir={_resolved_repo_dir} from sys.path to ensure wheels are tested')
            del sys.path[_idx]
            print(f'[run_tests] sys.path = {ub.urepr(sys.path, nl=1)}')

    _temp_path = [os.fspath(p) for p in temp_path]
    print(f'[run_tests] Search Paths: {ub.urepr(_temp_path, nl=1)}')
    modpath = ub.modname_to_modpath(package_name, sys_path=_temp_path)
    if modpath is not None:
        # If it does, then import it. This should cause the installed version
        # to be used on further imports even if the repo_dir is in the path.
        print(f'[run_tests] Found installed version of {package_name}')
        print(f'[run_tests] modpath={modpath}')
        modpath_contents = list(pathlib.Path(modpath).glob('*'))
        print(f'[run_tests] modpath_contents = {ub.urepr(modpath_contents, nl=1)}')
        # module = ub.import_module_from_path(modpath, index=0)
        # print(f'[run_tests] Installed module = {module!r}')
    else:
        print(f'[run_tests] No installed version of {package_name} found')

    try:
        import pytest
        pytest_args = [
            '--cov-config', os.fspath(pyproject_fpath),
            '--cov-report', 'html',
            '--cov-report', 'term',
            '--cov-report', 'xml',
            '--cov=' + package_name,
            os.fspath(modpath), os.fspath(test_dir)
        ]
        if is_cibuildwheel():
            pytest_args.append('--cov-append')

        pytest_args = pytest_args + sys.argv[1:]
        print(f'[run_tests] Exec pytest with args={pytest_args}')
        retcode = pytest.main(pytest_args)
        print(f'[run_tests] pytest returned ret={retcode}')
    except Exception as ex:
        print(f'[run_tests] pytest exception: {ex}')
        retcode = 1
    finally:
        os.chdir(orig_cwd)
        if is_cibuildwheel():
            # for CIBW under linux
            copy_coverage_cibuildwheel_docker(f'/home/runner/work/{package_name}/{package_name}')
        print('[run_tests] Restoring cwd = {!r}'.format(orig_cwd))
    return retcode


if __name__ == '__main__':
    sys.exit(main())
