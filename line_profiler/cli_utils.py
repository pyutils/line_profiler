"""
Shared utilities between the :command:`python -m line_profiler` and
:command:`kernprof` CLI tools.
"""
import argparse
import functools
import os
import pathlib
import shutil
import sys
from .toml_config import ConfigSource


_BOOLEAN_VALUES = {**{k.casefold(): False
                      for k in ('', '0', 'off', 'False', 'F', 'no', 'N')},
                   **{k.casefold(): True
                      for k in ('1', 'on', 'True', 'T', 'yes', 'Y')}}


def add_argument(parser_like, arg, /, *args,
                 hide_complementary_options=True, **kwargs):
    """
    Override the ``'store_true'`` and ``'store_false'`` actions so that
    they are turned into options which:

    * Don't set the default to the opposite boolean, thus allowing us to
      later distinguish between cases where the flag has been passed or
      not, and
    * Set the destination value to the corresponding value in the no-arg
      form, but also allow (for long options) for a single arg which is
      parsed by :py:func:`.boolean()`.
    Also automatically generates complementary boolean options for
    ``action='store_true'`` options.
    If ``hide_complementary_options`` is
    true, the auto-generated option (all the long flags prefixed
    with ``'no-'``, e.g. ``'--foo'`` is negated by ``'--no-foo'``) is
    hidden from the help text.

    Arguments:
        parser_like (Any):
            Object having a method ``add_argument()``, which has the
            same semantics and call signature as
            :py:meth:`argparse.ArgumentParser.add_argument()`.
        hide_complementary_options (bool):
            Whether to hide the auto-generated complementary options to
            ``action='store_true'`` options from the help text for
            brevity.
        arg, *args, **kwargs
            Passed to ``parser_like.add_argument()``

    Returns:
        Any: action_like
            Return value of ``parser_like.add_argument()``

    Note:
        * Short and long flags for ``'store_true'`` and
          ``'store_false'`` actions are implemented in separate actions
          so as to allow for short-flag concatenation.
        * If an option has both short and long flags, the short-flag
          action is hidden from the help text, but the long-flag
          action's help text is updated to mention the corresponding
          short flag(s).
    """
    def negate_result(func):
        @functools.wraps(func)
        def negated(*args, **kwargs):
            return not func(*args, **kwargs)

        negated.__name__ = 'negated_' + negated.__name__
        return negated

    # Make sure there's at least one positional argument
    args = [arg, *args]

    if kwargs.get('action') not in ('store_true', 'store_false'):
        return parser_like.add_argument(*args, **kwargs)

    # Long and short boolean flags should be handled separately: short
    # flags should remain 0-arg to permit flag concatenation, while long
    # flag should be able to take an optional arg parsable into a bool
    prefix_chars = tuple(parser_like.prefix_chars)
    short_flags = []
    long_flags = []
    for arg in args:
        assert arg.startswith(prefix_chars)
        if arg.startswith(tuple(char * 2 for char in prefix_chars)):
            long_flags.append(arg)
        else:
            short_flags.append(arg)

    kwargs['const'] = const = kwargs.pop('action') == 'store_true'
    for key, value in dict(
            default=None,
            metavar='Y[es] | N[o] | T[rue] | F[alse] '
            '| on | off | 1 | 0').items():
        kwargs.setdefault(key, value)
    long_kwargs = kwargs.copy()
    short_kwargs = {**kwargs, 'action': 'store_const'}
    for key, value in dict(
            nargs='?',
            type=functools.partial(boolean, invert=not const)).items():
        long_kwargs.setdefault(key, value)

    # Mention the short options in the long options' documentation, and
    # suppress the short options in the help
    if (
            long_flags
            and short_flags
            and long_kwargs.get('help') != argparse.SUPPRESS):
        additional_msg = 'Short {}: {}'.format(
            'form' if len(short_flags) == 1 else 'forms',
            ', '.join(short_flags))
        if long_kwargs.get('help'):
            help_text = long_kwargs['help'].strip()
            if help_text.endswith((')', ']')):
                # Interpolate into existing parenthetical
                help_text = '{}; {}{}{}'.format(
                    help_text[:-1],
                    additional_msg[0].lower(),
                    additional_msg[1:],
                    help_text[-1])
            else:
                help_text = f'{help_text} ({additional_msg})'
            long_kwargs['help'] = help_text
        else:
            long_kwargs['help'] = f'({additional_msg})'
        short_kwargs['help'] = argparse.SUPPRESS

    long_action = short_action = None
    if long_flags:
        long_action = parser_like.add_argument(*long_flags, **long_kwargs)
        short_kwargs['dest'] = long_action.dest
    if short_flags:
        short_action = parser_like.add_argument(*short_flags, **short_kwargs)
    if long_action:
        action = long_action
    else:
        assert short_action
        action = short_action
    if not (const and long_flags):  # Negative or short-only flag
        return action

    # Automatically generate a complementary option for a long boolean
    # option
    # (in Python 3.9+ one can use `argparse.BooleanOptionalAction`,
    # but we want to maintain compatibility with Python 3.8)
    if hide_complementary_options:
        falsy_help_text = argparse.SUPPRESS
    else:
        falsy_help_text = 'Negate these flags: ' + ', '.join(args)
    parser_like.add_argument(
        *(flag[:2] + 'no-' + flag[2:] for flag in long_flags),
        **{**long_kwargs,
           'const': False,
           'dest': action.dest,
           'type': negate_result(action.type),
           'help': falsy_help_text})
    return action


def get_cli_config(subtable, /, *args, **kwargs):
    """
    Get the ``tool.line_profiler.<subtable>`` configs and normalize
    its keys (``some-key`` -> ``some_key``).

    Arguments:
        subtable (str):
            Name of the subtable the CLI app should refer to (e.g.
            ``'kernprof'``)
        *args, **kwargs
            Passed to \
:py:meth:`line_profiler.toml_config.ConfigSource.from_config`

    Returns:
        New :py:class:`~.line_profiler.toml_config.ConfigSource`
        instance
    """
    config = ConfigSource.from_config(*args, **kwargs).get_subconfig(subtable)
    config.conf_dict = {key.replace('-', '_'): value
                        for key, value in config.conf_dict.items()}
    return config


def get_python_executable():
    """
    Returns:
        str: command
            Command or path thereto corresponding to
            :py:data:`sys.executable`.
    """
    if os.path.samefile(shutil.which('python'), sys.executable):
        return 'python'
    elif os.path.samefile(shutil.which('python3'), sys.executable):
        return 'python3'
    else:
        return short_string_path(sys.executable)


def positive_float(value):
    """
    Arguments:
        value (str)

    Returns:
        float: positive_num
    """
    val = float(value)
    if val <= 0:
        # Note: parsing functions should raise either a `ValueError` or
        # a `TypeError` instead of an `argparse.ArgumentError`, which
        # expects extra context and in general should be raised by the
        # parser object
        raise ValueError
    return val


def boolean(value, *, fallback=None, invert=False):
    """
    Arguments:
        value (str)
            Value to be parsed into a boolean (case insensitive)
        fallback (bool | None)
            Optional value to fall back to in case ``value`` doesn't
            match any of the specified
        invert (bool)
            If :py:data:`True`, invert the result of parsing ``value``
            (but not ``fallback``)

    Returns:
        bool: result

    Example:
        These values are parsed into :py:data:`False`:

        >>> assert not any(
        ...     boolean(value)
        ...     for value in ['', '0', 'F', 'N', 'off', 'False', 'no'])

        These values are parsed into :py:data:`True`:

        >>> assert all(
        ...     boolean(value)
        ...     for value in ['1', 'T', 'Y', 'on', 'True', 'yes'])

        Fallback:

        >>> assert boolean('invalid', fallback=True) == True
        >>> assert boolean('invalid', fallback=False) == False
        >>> try:
        ...     result = boolean('invalid')
        ... except ValueError:
        ...     pass
        ... except Exception as e:
        ...     assert False, (
        ...         f'Expected `ValueError`, got `{type(e).__name__}`')
        ... else:
        ...     assert False, (
        ...         f'Expected `ValueError`, got result {result!r}')

        Case insensitivity:

        >>> assert boolean('fAlSe') == False
        >>> assert boolean('YeS') == True
    """
    try:
        result = _BOOLEAN_VALUES[value.casefold()]
    except KeyError:
        pass
    else:
        return (not result) if invert else result
    if fallback is None:
        raise ValueError(f'value = {value!r}: '
                         'cannot be parsed into a boolean; valid values are'
                         f'({{string: bool}}): {_BOOLEAN_VALUES!r}')
    return fallback


def short_string_path(path):
    """
    Arguments:
        path (str | os.PathLike[str]):
            Path-like

    Returns:
        str: short_path
            The shortest formatted path among the provided ``path``, the
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
