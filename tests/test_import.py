def test_import():
    import line_profiler
    assert hasattr(line_profiler, 'LineProfiler')
    assert hasattr(line_profiler, '__version__')


def test_version():
    import line_profiler
    from packaging.version import Version
    assert Version(line_profiler.__version__)
