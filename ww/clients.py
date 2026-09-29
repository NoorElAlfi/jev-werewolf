"""Injectable model clients.

All code gets its Ollama, TypeSafe and Anthropic clients from here, never by
calling `ollama.chat`, `TypeSafeClient()` or `anthropic.Anthropic()` directly,
so tests can swap in the fakes from tests/fakes.py:

    with use_clients(ollama=FakeOllama(), typesafe=FakeTypeSafe):
        ...

`ollama` is any object with `.chat(model=..., messages=...)`.
`typesafe` is a zero-argument factory returning a TypeSafe-like client that
works as a context manager and has `.system_one(state=..., questions=...)`.
`anthropic` is a zero-argument factory returning an Anthropic-like client with
`.messages.create(...)`.
"""

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

_ollama: Any = None
_typesafe_factory: Callable[[], Any] | None = None
_anthropic_factory: Callable[[], Any] | None = None


def get_ollama() -> Any:
    if _ollama is not None:
        return _ollama
    import ollama

    return ollama


def make_typesafe() -> Any:
    if _typesafe_factory is not None:
        return _typesafe_factory()
    from typesafe_sdk import TypeSafeClient

    import ww.config  # noqa: F401  (loads .env so TYPESAFE_API_KEY is set)

    return TypeSafeClient()


def make_anthropic() -> Any:
    if _anthropic_factory is not None:
        return _anthropic_factory()
    import anthropic

    import ww.config  # noqa: F401  (loads .env so ANTHROPIC_API_KEY is set)

    return anthropic.Anthropic(max_retries=4)


def set_clients(
    ollama: Any = None,
    typesafe: Callable[[], Any] | None = None,
    anthropic: Callable[[], Any] | None = None,
) -> None:
    """Set process-wide overrides. Pass None to restore the real client."""
    global _ollama, _typesafe_factory, _anthropic_factory
    _ollama = ollama
    _typesafe_factory = typesafe
    _anthropic_factory = anthropic


@contextmanager
def use_clients(
    ollama: Any = None,
    typesafe: Callable[[], Any] | None = None,
    anthropic: Callable[[], Any] | None = None,
):
    """Temporarily override clients; arguments left as None keep the current one."""
    global _ollama, _typesafe_factory, _anthropic_factory
    saved = (_ollama, _typesafe_factory, _anthropic_factory)
    if ollama is not None:
        _ollama = ollama
    if typesafe is not None:
        _typesafe_factory = typesafe
    if anthropic is not None:
        _anthropic_factory = anthropic
    try:
        yield
    finally:
        _ollama, _typesafe_factory, _anthropic_factory = saved
