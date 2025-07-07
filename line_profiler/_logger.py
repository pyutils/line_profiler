"""
# Ported from kwutil
"""
# import os
import logging
from abc import ABC, abstractmethod
import sys
from typing import ClassVar
from logging import INFO, DEBUG, ERROR, WARNING, CRITICAL  # NOQA


class _LogBackend(ABC):
    """
    Abstract base class for our logger implementations.
    """
    backend: ClassVar[str]

    def __init__(self, name):
        self.name = name

    @abstractmethod
    def configure(self, *args, **kwarg):
        """
        Note:
            Implementations should take the arguments it needs and
            return the instance.
        """

    @abstractmethod
    def debug(self, msg, *args, **kwargs):
        pass

    @abstractmethod
    def info(self, msg, *args, **kwargs):
        pass

    @abstractmethod
    def warning(self, msg, *args, **kwargs):
        pass

    @abstractmethod
    def error(self, msg, *args, **kwargs):
        pass

    @abstractmethod
    def critical(self, msg, *args, **kwargs):
        pass


class _PrintLogBackend(_LogBackend):
    """
    A simple print-based logger that falls back to print output if no logging configuration
    is set up.

    Example:
        >>> pl = _PrintLogBackend(name='print', level=INFO)
        >>> pl.info('Hello %s', 'world')
        Hello world
        >>> pl.debug('Should not appear')
    """
    backend = 'print'

    def __init__(self, name="<print-logger>", level=logging.INFO):
        super().__init__(name)
        self.level = level

    def isEnabledFor(self, level):
        return level >= self.level

    def _log(self, level, msg, *args, **kwargs):
        if self.isEnabledFor(level):
            # Mimic logging formatting (ignoring extra kwargs for simplicity)
            print(msg % args)

    def debug(self, msg, *args, **kwargs):
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._log(logging.CRITICAL, msg, *args, **kwargs)

    def configure(self, level=None, **_):
        if level is not None:
            self.level = level
        return self


class _StdlibLogBackend(_LogBackend):
    """
    A wrapper for Python's standard logging.Logger.

    The constructor optionally adds a StreamHandler (to stdout) and/or a logging.FileHandler if file is specified.

    Example:
        >>> import os
        >>> import ubelt as ub
        >>> import logging
        >>> dpath = ub.Path.appdir('kwutil/test/logging').ensuredir()
        >>> fpath = (dpath / 'test.log').delete()
        >>> sl = _StdlibLogBackend('stdlib').configure(
        >>>     level=logging.INFO,
        >>>     stream={
        >>>         'format': '%(asctime)s : [stream] %(levelname)s : %(message)s',
        >>>     },
        >>>     file={
        >>>         'path': fpath,
        >>>         'format': '%(asctime)s : [file] %(levelname)s : %(message)s',
        >>>     }
        >>> )
        >>> sl.info('Hello %s', 'world')
        >>> # Check that the log file has been written to
        >>> text = fpath.read_text()
        >>> print(text)
        >>> assert text.strip().endswith('Hello world')
    """
    backend = 'stdlib'

    def __init__(self, name):
        super().__init__(name)
        self.logger = logging.getLogger(name)

    def configure(
        self,
        level=None,
        stream='auto',
        file=None,
        **_,
    ):
        """
        Configure the underlying stdlib logger.

        Parameters:
            level: the logging level to set (e.g. logging.INFO)
            stream: either a dict with configuration or a boolean/'auto'
                - If dict, expected keys include 'format'
                - If 'auto', the stream handler is enabled if no handlers are set
                - If a boolean, True enables the stream handler.
            file: either a dict with configuration or a path string.
                - If dict, expected keys include 'path' and 'format'
                - If a string, it is taken as the file path


        Note:
            For special attributes for the ``format`` argument of ``stream``
            and ``file`` see
            https://docs.python.org/3/library/logging.html#logrecord-attributes

        Returns:
            self (the configured _StdlibLogBackend instance)
        """
        if level is not None:
            self.logger.setLevel(level)

        # Default settings for file and stream handlers
        fileinfo = {
            'path': None,
            'format': '%(asctime)s : [file] %(levelname)s : %(message)s'
        }
        streaminfo = {
            '__enable__': None,  # will be determined below
            'format': '%(levelname)s: %(message)s',
        }

        # Update stream info if stream is a dict
        if isinstance(stream, dict):
            streaminfo.update(stream)
            # If not specified otherwise, enable the stream handler.
            if streaminfo.get('__enable__') is None:
                streaminfo['__enable__'] = True
        else:
            # If stream is not a dict, treat it as a boolean or 'auto'
            streaminfo['__enable__'] = stream

        # If stream is 'auto', enable stream only if no handlers are present.
        if streaminfo['__enable__'] == 'auto':
            streaminfo['__enable__'] = not bool(self.logger.handlers)

        # Update file info if file is a dict
        if isinstance(file, dict):
            fileinfo.update(file)
        else:
            fileinfo['path'] = file

        # Add a stream handler if enabled
        if streaminfo['__enable__']:
            streamformat = streaminfo.get('format')
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(logging.Formatter(streamformat))
            self.logger.addHandler(sh)

        # Add a file handler if a valid path is provided
        path = fileinfo.get('path')
        if path:
            fileformat = fileinfo.get('format')
            fh = logging.FileHandler(path)
            fh.setFormatter(logging.Formatter(fileformat))
            self.logger.addHandler(fh)
        return self

    # def _setup_handlers(self, stream, file):
    #     # Only add handlers if none exist, so as not to duplicate logs.
    #     if not self.logger.handlers:

    def debug(self, msg, *args, **kwargs):
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + 1
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + 1
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + 1
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + 1
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        kwargs['stacklevel'] = kwargs.get('stacklevel', 1) + 1
        self.logger.critical(msg, *args, **kwargs)


class Logger:
    """
    The main Logger class that automatically selects the backend.

    If backend='auto' and a global logging configuration exists (i.e. logging.getLogger(name) has handlers),
    it uses _StdlibLogBackend; otherwise, it falls back to _PrintLogBackend.

    Optional parameters:
      - verbose: controls log level via an integer (0: CRITICAL, 1: INFO, 2: DEBUG, etc.)
      - file: if provided, file logging is enabled (only used with _StdlibLogBackend)
      - stream: if True, a logging.StreamHandler to stdout is added (only used with _StdlibLogBackend)

    Example:
        >>> # With no global handlers, defaults to _PrintLogBackend
        >>> logger = Logger('TestLogger', verbose=2, backend='auto')
        >>> logger.info('Hello %s', 'world')
        Hello world
        >>> # Forcing use of _PrintLogBackend
        >>> logger = Logger('TestLogger', verbose=2, backend='print')
        >>> logger.debug('Debug %d', 123)
        Debug 123
        >>> # Forcing use of Stdlib Logger
        >>> logger = Logger('TestLogger', verbose=2, backend='stdlib')
        >>> logger.debug('Debug %d', 123)

    Example:
        >>> # Forcing use of Stdlib Logger
        >>> logger = Logger('TestLogger', verbose=2, backend='stdlib').configure(
        >>>     stream={'format': '%(asctime)s : %(pathname)s:%(lineno)d %(funcName)s  %(levelname)s : %(message)s'})
        >>> logger.debug('Debug %d', 123)
        >>> logger.info('Hello %d', 123)
    """
    def __init__(self, name="Logger", verbose=1, backend="auto", file=None, stream=True):
        # Map verbose level to logging levels. If verbose > 1, show DEBUG, else INFO.
        self.name = name
        self.configure(verbose=verbose, backend=backend, file=file, stream=stream)

    def configure(self, backend='auto', verbose=1, file=None, stream=None):
        name = self.name
        kwargs = dict(file=file, stream=stream)
        kwargs['level'] = {
            0: logging.CRITICAL,
            1: logging.INFO,
            2: logging.DEBUG}.get(verbose, logging.DEBUG)
        if backend == "auto":
            # Choose _StdlibLogBackend if a logger with handlers exists.
            if logging.getLogger(name).handlers:
                backend = 'stdlib'
            else:
                backend = 'print'
        try:
            Backend = {'print': _PrintLogBackend,
                       'stdlib': _StdlibLogBackend}[backend]
        except KeyError:
            raise ValueError(
                "Unsupported backend. "
                "Use 'auto', 'print', or 'stdlib'.") from None
        self._backend = Backend(name).configure(**kwargs)
        return self

    def __getattr__(self, attr):
        # We should not need to modify stacklevel here as we are directly
        # returning the backend function and not wrapping it.
        return getattr(self._backend, attr)
