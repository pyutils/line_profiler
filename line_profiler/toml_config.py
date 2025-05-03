"""
Read and resolve user-supplied TOML files and combine them with the
default to generate configurations.
"""
import copy
import functools
import importlib.resources
import itertools
import os
import pathlib
try:
    import tomllib
except ImportError:  # Python < 3.11
    import tomli as tomllib
from collections.abc import Mapping


__all__ = ['get_config', 'get_default_config']

namespace = 'tool', 'line_profiler'
targets = 'line_profiler.toml', 'pyproject.toml'
env_var = 'LINE_PROFILER_RC'

_defaults = None


def find_and_read_config_file(
        *, config=None, env_var=env_var, targets=targets):
    """
    Arguments:
        config (Union[str, PurePath, None]):
            Optional path to a specific TOML file;
            if provided, skip lookup and just try to read from that file
        env_var (Union[str, None]):
            Name of the of the environment variable containing the path
            to a TOML file;
            if true-y and if `config` isn't provided, skip lookup and
            just try to read from that file
        targets (Sequence[str | PurePath]):
            Filenames among which TOML files are looked up (if neither
            `config` or `env_var` is given)

    Return:
        If the provided/looked-up file is readable and is valid TOML:
            content, path (tuple[dict, Path]):
            - `content`: parsed content of the file as a dictionary
            - `path`: absolute path to the file
        Otherwise
            None
    """
    def iter_configs(dir_path):
        for dpath in itertools.chain((dir_path,), dir_path.parents):
            for target in targets:
                cfg = dpath / target
                try:
                    if cfg.is_file():
                        yield cfg
                except OSError:  # E.g. permission errors
                    pass

    if config:
        configs = pathlib.Path(config).absolute(),
    elif env_var and os.environ.get(env_var):
        configs = pathlib.Path(os.environ[env_var]).absolute(),
    else:
        pwd = pathlib.Path.cwd().absolute()
        configs = iter_configs(pwd)
    for config in configs:
        try:
            with config.open(mode='rb') as fobj:
                return tomllib.load(fobj), config
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return None


def get_subtable(table, keys, *, allow_absence=True):
    """
    Arguments:
        table (Mapping):
            (Nested) Mapping.
        keys (Sequence):
            Sequence of keys for item access on `table` and its
            descendant tables.
        allow_absence (bool):
            If true, allow for the keys to be absent;
            otherwise, raise a `KeyError`.

    Return:
        subtable (Mapping)

    Example:

        >>> table = {'a': 1, 'b': {'c': 2, 'd': 3, 'e': {}}}
        >>> assert get_subtable(table, []) == table
        >>> assert get_subtable(table, ['b']) == table['b']
        >>> assert get_subtable(table, ['b', 'e']) == table['b']['e']
        >>> assert get_subtable(table, ['c']) == {}
        >>> get_subtable(table, ['c'], allow_absence=False)
        Traceback (most recent call last):
          ...
        KeyError: 'c'
        >>> get_subtable(  # doctest: +ELLIPSIS, +NORMALIZE_WHITESPACE
        ...     table, ['a'])
        Traceback (most recent call last):
          ...
        TypeError: table = ..., keys = ['a']:
        expected result to be a mapping, got a `int` (1)
    """
    subtable = table
    for key in keys:
        if allow_absence:
            subtable = subtable.get(key, {})
        else:
            subtable = subtable[key]
    if not isinstance(subtable, Mapping):
        raise TypeError(f'table = {table!r}, keys = {list(keys)!r}: '
                        'expected result to be a mapping, got a '
                        f'`{type(subtable).__name__}` ({subtable!r})')
    return subtable


def get_headers(table, *, include_implied=False):
    """
    Arguments:
        table (Mapping):
            (Nested) Mapping.
        include_implied (bool):
            if false and if a subtable has other subtables, only the
            terminal key sequences are returned (that is to say, if
            `table['a']['b']` is a subtable, then `('a',)` is only in
            `headers` if `include_implied` is true.

    Return:
        headers (set[tuple]):
            Key sequences corresponding to the subtables of `table`.

    Example:

        >>> table = {'a': 1,
        ...          'b': {'c': 2, 'd': 3, 'e': {}, 'f': {'g': 4}},
        ...          'h': {'i': 5}}
        >>> assert get_headers(table) == {
        ...     ('b', 'e'), ('b', 'f'), ('h',)}
        >>> assert get_headers(table, include_implied=True) == {
        ...     ('b',), ('b', 'e'), ('b', 'f'), ('h',)}
        >>> assert get_headers({}) == set()
        >>> assert get_headers({'a': 1, 'b': 2}) == set()
    """
    results = set()
    for key, value in table.items():
        if not isinstance(value, Mapping):
            continue
        subheaders = get_headers(value, include_implied=include_implied)
        if subheaders:
            results.update((key,) + header for header in subheaders)
        if include_implied or not subheaders:
            results.add((key,))
    return results


def get_config(config=None, *, read_env=True):
    """
    Arguments:
        config (Union[str | PurePath | None]):
            Optional path to a specific TOML file;
            if provided, skip lookup and just try to read from that file
        read_env (bool):
            Whether to read the environment variable
            `${LINE_PROFILER_RC}` for a config file (instead of moving
            straight onto environment-based lookup) if `config` is not
            provided.

    Return:
        conf_dict, path (tuple[dict, Path]):
            - `conf_dict`: the combination of the `tool.line_profiler`
              tables of the provided/looked-up config file (if any) and
              the default as a dictionary
            - `path`: absolute path to the config file whence the
              config options are loaded

    Notes:
        - For the config TOML file, it is required that each of the
          following keys either is absent or maps to a table:
          - `tool` and `tool.line_profiler`
          - `tool.line_profiler.kernprof`, `.cli`, `.setup`, `.write`,
            and `.show`
          - `tool.line_profiler.show.column_widths`
          If this is not the case:
          - If `config` is provided, a `ValueError` is raised.
          - Otherwise, the looked-up file is considered invalid and
            ignored.
    """
    def merge(template, supplied):
        if not (isinstance(template, dict) and isinstance(supplied, dict)):
            return supplied
        result = {}
        for key, default in template.items():
            if key in supplied:
                result[key] = merge(default, supplied[key])
            else:
                result[key] = default
        return result

    default_conf, default_source = get_default_config()
    if config is not None:
        # Promote to `Path` (and catch type errors) early
        config = pathlib.Path(config)
    if read_env:
        get_conf = functools.partial(find_and_read_config_file, config=config)
    else:  # Shield the lookup from the environment
        get_conf = functools.partial(find_and_read_config_file,
                                     config=config, env_var=None)
    try:
        content, source = get_conf()
    except TypeError:  # Got `None`
        if config:
            if os.path.exists(config):
                Error = ValueError
            else:
                Error = FileNotFoundError
            raise Error(r'Cannot load configurations from {config!r}') from None
        return default_conf, default_source
    conf = {}
    try:
        for header in get_headers(default_conf):
            # Get the top-level subtable
            key, *subheader = header
            subtable = get_subtable(content, [*namespace, key])
            # Check the existence of nested subtables (if any)
            get_subtable(subtable, subheader)
            # If it looks OK, remember the top-level subtable
            conf.setdefault(key, subtable)
    except (TypeError, AttributeError):
        if config is None:
            # No file explicitly provided and the looked-up file is
            # invalid, just fall back to the default configs
            return default_conf, default_source
        else:
            # The explicitly provided config file is invalid, raise an
            # error
            all_headers = {'tool', 'tool.line_profiler'}
            all_headers.update(
                '.'.join(('tool.line_profiler', *header))
                for header in get_headers(default_conf, include_implied=True))
            raise ValueError(
                f'config = {config!r}: expected each of these keys to either '
                'be nonexistent or map to a table: '
                f'{sorted(all_headers)!r}') from None
    # Filter the content of `conf` down to just the key-value pairs
    # pairs present in the default configs
    return merge(default_conf, conf), source


def get_default_config():
    """
    Return:
        conf_dict, path (tuple[dict, Path])
            - `conf_dict`: the default config file's
              `tool.line_profiler` table as a dictionary
            - `path`: absolute path to the default config file
    """
    if _defaults is None:
        package = __spec__.name.rpartition('.')[0]
        with importlib.resources.path(package + '.rc',
                                      'line_profiler.toml') as path:
            conf_dict, source = find_and_read_config_file(config=path)
        globals()['_defaults'] = (get_subtable(conf_dict, namespace,
                                               allow_absence=False),
                                  source)
    return copy.deepcopy(_defaults[0]), _defaults[1]
