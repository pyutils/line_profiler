from enum import auto
from types import MappingProxyType, ModuleType
from typing import Union, TypedDict
from .line_profiler_utils import StringEnum


#: Default scoping policies:
#:
#: * Profile sibling and descendant functions
#:   (:py:attr:`ScopingPolicy.SIBLINGS`)
#: * Descend ingo sibling and descendant classes
#:   (:py:attr:`ScopingPolicy.SIBLINGS`)
#: * Don't descend into modules (:py:attr:`ScopingPolicy.EXACT`)
DEFAULT_SCOPING_POLICIES = MappingProxyType(
    {'func': 'siblings', 'class': 'siblings', 'module': 'exact'})


class ScopingPolicy(StringEnum):
    """
    :py:class:`StrEnum` for scoping policies, that is, how it is
    decided whether to:

    * Profile a function found in a namespace (a class or a module), and
    * Descend into nested namespaces so that their methods and functions
      are profiled,

    when using :py:meth:`LineProfiler.add_class`,
    :py:meth:`LineProfiler.add_module`, and
    :py:func:`~.add_imported_function_or_module()`.

    Available policies are:

    :py:attr:`ScopingPolicy.EXACT`
        Only profile *functions* found in the namespace fulfilling
        :py:attr:`ScopingPolicy.CHILDREN` as defined below, without
        descending into nested namespaces

    :py:attr:`ScopingPolicy.CHILDREN`
        Only profile/descend into *child* objects, which are:

        * Classes and functions defined *locally* in the very
          module, or in the very class as its "inner classes" and
          methods
        * Direct submodules, in case when the namespace is a module
          object representing a package

    :py:attr:`ScopingPolicy.DESCENDANTS`
        Only profile/descend into *descendant* objects, which are:

        * Child classes, functions, and modules, as defined above in
          :py:attr:`ScopingPolicy.CHILDREN`
        * Their child classes, functions, and modules, ...
        * ... and so on

        Note:
            Since imported submodule module objects are by default
            placed into the namespace of their parent-package module
            objects, this functions largely identical to
            :py:attr:`ScopingPolicy.CHILDREN` for descent from module
            objects into other modules objects.

    :py:attr:`ScopingPolicy.SIBLINGS`
        Only profile/descend into *sibling* and descendant objects,
        which are:

        * Descendant classes, functions, and modules, as defined above
          in :py:attr:`ScopingPolicy.DESCENDANTS`
        * Classes and functions (and descendants thereof) defined in the
          same parent namespace to this very class, or in modules (and
          subpackages and their descendants) sharing a parent package
          to this very module
        * Modules (and subpackages and their descendants) sharing a
          parent package, when the namespace is a module

    :py:attr:`ScopingPolicy.NONE`
        Don't check scopes;  profile all functions found in the local
        namespace of the class/module, and descend into all nested
        namespaces recursively

        Note:
            This is probably a *very* bad idea for module scoping,
            potentially resulting in accidentally recursing through a
            significant portion of loaded modules;
            proceed with care.

    Note:
        Other than :py:class:`enum.Enum` methods starting and ending
        with single underscores (e.g. :py:meth:`!_missing_`), all
        methods prefixed with a single underscore are to be considered
        implementation details.
    """
    EXACT = auto()
    CHILDREN = auto()
    DESCENDANTS = auto()
    SIBLINGS = auto()
    NONE = auto()

    # Verification

    def __init_subclass__(cls, *args, **kwargs):
        """
        Call :py:meth:`_check_class`.
        """
        super().__init_subclass__(*args, **kwargs)
        cls._check_class()

    @classmethod
    def _check_class(cls):
        """
        Verify that :py:meth:`.get_filter` return a callable for all
        policy values and object types.
        """
        mock_module = ModuleType('mock_module')

        class MockClass:
            pass

        for member in cls.__members__.values():
            for obj_type in 'func', 'class', 'module':
                for namespace in mock_module, MockClass:
                    assert callable(member.get_filter(namespace, obj_type))

    # Filtering

    def get_filter(self, namespace, obj_type):
        """
        Args:
            namespace (Union[type, types.ModuleType]):
                Class or module to be profiled.
            obj_type (Literal['func', 'class', 'module']):
                Type of object encountered in ``namespace``:

                ``'func'``
                    Either a function, or a component function of a
                    callable-like object (e.g. :py:class:`property`)

                ``'class'`` (resp. ``'module'``)
                    A class (resp. a module)

        Returns:
            func (Callable[..., bool]):
                Filter callable returning whether the argument (as
                specified by ``obj_type``) should be added
                via :py:meth:`LineProfiler.add_class`,
                :py:meth:`LineProfiler.add_module`, or
                :py:meth:`LineProfiler.add_callable`
        """
        is_class = isinstance(namespace, type)
        if obj_type == 'module':
            if is_class:
                return self._return_const(False)
            return self._get_module_filter_in_module(namespace)
        if is_class:
            method = self._get_callable_filter_in_class
        else:
            method = self._get_callable_filter_in_module
        return method(namespace, is_class=(obj_type == 'class'))

    @classmethod
    def to_policies(cls, policies=None):
        """
        Normalize ``policies`` into a dictionary of policies for various
        object types.

        Args:
            policies (Union[str, ScopingPolicy, \
ScopingPolicyDict, None]):
                :py:class:`ScopingPolicy`, string convertible thereto
                (case-insensitive), or a mapping containing such values
                and the keys as outlined in the return value;
                the default :py:const:`None` is equivalent to
                :py:data:`DEFAULT_SCOPING_POLICIES`.

        Returns:
            normalized_policies (dict[Literal['func', 'class', \
'module'], ScopingPolicy]):
                Dictionary with the following key-value pairs:

                ``'func'``
                    :py:class:`ScopingPolicy` for profiling functions
                    and other callable-like objects composed thereof
                    (e.g. :py:class:`property`).

                ``'class'``
                    :py:class:`ScopingPolicy` for descending into
                    classes.

                ``'module'``
                    :py:class:`ScopingPolicy` for descending into
                    modules (if the namespace is itself a module).

        Note:
            If ``policies`` is a mapping, it is required to contain all
            three of the aforementioned keys.

        Example:

            >>> assert (ScopingPolicy.to_policies('children')
            ...         == dict.fromkeys(['func', 'class', 'module'],
            ...                          ScopingPolicy.CHILDREN))
            >>> assert (ScopingPolicy.to_policies({
            ...             'func': 'NONE',
            ...             'class': 'descendants',
            ...             'module': 'exact',
            ...             'unused key': 'unused value'})
            ...         == {'func': ScopingPolicy.NONE,
            ...             'class': ScopingPolicy.DESCENDANTS,
            ...             'module': ScopingPolicy.EXACT})
            >>> ScopingPolicy.to_policies({})
            Traceback (most recent call last):
            ...
            KeyError: 'func'
        """
        if policies is None:
            policies = DEFAULT_SCOPING_POLICIES
        if isinstance(policies, str):
            policy = cls(policies)
            return _ScopingPolicyDict(
                dict.fromkeys(['func', 'class', 'module'], policy))
        return _ScopingPolicyDict({'func': cls(policies['func']),
                                   'class': cls(policies['class']),
                                   'module': cls(policies['module'])})

    @staticmethod
    def _return_const(value):
        def return_const(*_, **__):
            return value

        return return_const

    @staticmethod
    def _match_prefix(s, prefix, sep='.'):
        return s == prefix or s.startswith(prefix + sep)

    def _get_callable_filter_in_class(self, cls, is_class):
        def func_is_child(other):
            if not modules_are_equal(other):
                return False
            return other.__qualname__ == f'{cls.__qualname__}.{other.__name__}'

        def modules_are_equal(other):  # = sibling check
            return cls.__module__ == other.__module__

        def func_is_descdendant(other):
            if not modules_are_equal(other):
                return False
            return other.__qualname__.startswith(cls.__qualname__ + '.')

        return {'exact': (self._return_const(False)
                          if is_class else
                          func_is_child),
                'children': func_is_child,
                'descendants': func_is_descdendant,
                'siblings': modules_are_equal,
                'none': self._return_const(True)}[self.value]

    def _get_callable_filter_in_module(self, mod, is_class):
        def func_is_child(other):
            return other.__module__ == mod.__name__

        def func_is_descdendant(other):
            return self._match_prefix(other.__module__, mod.__name__)

        def func_is_cousin(other):
            if func_is_descdendant(other):
                return True
            return self._match_prefix(other.__module__, parent)

        parent, _, basename = mod.__name__.rpartition('.')
        return {'exact': (self._return_const(False)
                          if is_class else
                          func_is_child),
                'children': func_is_child,
                'descendants': func_is_descdendant,
                'siblings': (func_is_cousin  # Only if a pkg
                             if basename else
                             func_is_descdendant),
                'none': self._return_const(True)}[self.value]

    def _get_module_filter_in_module(self, mod):
        def module_is_descendant(other):
            return other.__name__.startswith(mod.__name__ + '.')

        def module_is_child(other):
            return other.__name__.rpartition('.')[0] == mod.__name__

        def module_is_sibling(other):
            return other.__name__.startswith(parent + '.')

        parent, _, basename = mod.__name__.rpartition('.')
        return {'exact': self._return_const(False),
                'children': module_is_child,
                'descendants': module_is_descendant,
                'siblings': (module_is_sibling  # Only if a pkg
                             if basename else
                             self._return_const(False)),
                'none': self._return_const(True)}[self.value]


# Sanity check in case we extended `ScopingPolicy` and forgot to update
# the corresponding methods
ScopingPolicy._check_class()

ScopingPolicyDict = TypedDict('ScopingPolicyDict',
                              {'func': Union[str, ScopingPolicy],
                               'class': Union[str, ScopingPolicy],
                               'module': Union[str, ScopingPolicy]})
_ScopingPolicyDict = TypedDict('_ScopingPolicyDict',
                               {'func': ScopingPolicy,
                                'class': ScopingPolicy,
                                'module': ScopingPolicy})
