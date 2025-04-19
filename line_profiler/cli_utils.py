"""
Shared utilities between the `python -m line_profiler` and `kernprof`
CLI tools.
"""
import argparse
import pathlib
from .toml_config import get_config


def add_argument(parser_like, *args,
                 hide_complementary_options=True, **kwargs):
    """
    Override the 'store_true' and 'store_false' actions so that they
    are turned into 'store_const' options which don't set the
    default to the opposite boolean, thus allowing us to later
    distinguish between cases where the flag has been passed or not.
    Also automatically generates complementary boolean options for
    `action='store_true'` options.
    If `hide_complementary_options` is
    true, the auto-generated option (all the long flags prefixed
    with 'no-', e.g. '--foo' is negated by '--no-foo') is hidden
    from the help text.

    Arguments:
        parser_like (Any):
            Object having a method `add_argument()`, which has the same
            semantics and call signature as
            `ArgumentParser.add_argument()`.
        hide_complementary_options (bool):
            Whether to hide the auto-generated complementary options to
            `action='store_true'` options from the help text for
            brevity.
        *args, **kwargs
            Passed to `parser_like.add_argument()`

    Returns:
        action_like (Any):
            Return value of `parser_like.add_argument()`
    """
    if kwargs.get('action') not in ('store_true', 'store_false'):
        return parser_like.add_argument(*args, **kwargs)
    kwargs['const'] = kwargs['action'] == 'store_true'
    kwargs['action'] = 'store_const'
    kwargs.setdefault('default', None)
    if kwargs['action'] == 'store_false':
        return parser_like.add_argument(*args, **kwargs)
    # Automatically generate a complementary option for a boolean
    # option;
    # for convenience, turn it into a `store_const` action
    # (in Python 3.9+ one can use `argparse.BooleanOptionalAction`,
    # but we want to maintain compatibility with Python 3.8)
    action = parser_like.add_argument(*args, **kwargs)
    long_flags = [arg for arg in args if arg.startswith('--')]
    assert long_flags
    if hide_complementary_options:
        falsy_help_text = argparse.SUPPRESS
    else:
        falsy_help_text = 'Negate these flags: ' + ', '.join(args)
    parser_like.add_argument(*('--no-' + flag[2:] for flag in long_flags),
                             **{**kwargs,
                                'const': False,
                                'dest': action.dest,
                                'help': falsy_help_text})
    return action


def get_cli_config(subtable, *args, **kwargs):
    """
    Get the `tool.line_profiler.<subtable>` configs and normalize
    its keys (`some-key` -> `some_key`).

    Arguments:
        subtable (str):
            Name of the subtable the CLI app should refer to (e.g.
            'kernprof')
        *args, **kwargs
            Passed to `line_profiler.toml_config.get_config()`

    Returns:
        subconf_dict, path (tuple[dict, Path])
            - `subconf_dict`: the combination of the
              `tool.line_profiler.<subtable>` subtables of the
              provided/looked-up config file (if any) and the default as
              a dictionary
            - `path`: absolute path to the config file whence the
              config options are loaded
    """
    conf, source = get_config(*args, **kwargs)
    kernprof_conf = {key.replace('-', '_'): value
                     for key, value in conf[subtable].items()}
    return kernprof_conf, source


def positive_float(value):
    """
    Arguments:
        value (str)

    Returns:
        x (float > 0)
    """
    val = float(value)
    if val <= 0:
        # Note: parsing functions should raise either a `ValueError` or
        # a `TypeError` instead of an `argparse.ArgumentError`, which
        # expects extra context and in general should be raised by the
        # parser object
        raise ValueError
    return val


def short_string_path(path):
    """
    Arguments:
        path (Union[str, PurePath]):
            Path-like

    Returns:
        short_path (str):
            The shortest formatted `path` among the provided path, the
            corresponding absolute path, and its relative path to the
            current directory.
    """
    path = pathlib.Path(path)
    paths = {str(path)}
    abspath = path.absolute()
    paths.add(str(abspath))
    try:
        paths.add(str(abspath.relative_to(path.cwd().absolute())))
    except ValueError:  # Not relative to the curdir
        pass
    return min(paths, key=len)
