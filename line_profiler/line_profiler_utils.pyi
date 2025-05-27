import enum
try:
    from typing import Self  # type: ignore[attr-defined]  # noqa: F401
except ImportError:  # Python < 3.11
    from typing_extensions import Self  # noqa: F401


# Note: `mypy` tries to read this class as a free-standing enum
# (instead of an `enum.Enum` subclass that string enums are to inherit
# from), and complains that it has no members -- so silence that


class StringEnum(str, enum.Enum):  # type: ignore[misc]
    @staticmethod
    def _generate_next_value_(name: str, *_, **__) -> str:
        ...

    def __eq__(self, other) -> bool:
        ...

    def __str__(self) -> str:
        ...

    @classmethod
    def _missing_(cls, value) -> Self | None:
        ...
