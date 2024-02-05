from __future__ import annotations

import asyncio.events
import asyncio.tasks
import inspect
from contextlib import contextmanager
from types import CoroutineType
from typing import TYPE_CHECKING, Any, ContextManager

from logfire._stack_info import StackInfo, get_code_object_info, get_stack_info_from_frame
from logfire._utils import safe_repr

if TYPE_CHECKING:
    from logfire._main import Logfire

ONE_SECOND_IN_NANOSECONDS = 1_000_000_000


def log_slow_callbacks(logfire: Logfire, slow_duration: float) -> ContextManager[None]:
    """Log a warning whenever a function running in the asyncio event loop blocks for too long.

    See Logfire.log_slow_async_callbacks.
    Inspired by https://gitlab.com/quantlane/libs/aiodebug.
    """
    original_run = asyncio.events.Handle._run
    logfire = logfire.with_tags('slow-async')
    timer = logfire.config.ns_timestamp_generator
    slow_duration *= ONE_SECOND_IN_NANOSECONDS

    def patched_run(self: asyncio.events.Handle) -> Any:
        start_time = timer()
        # Handle._run currently doesn't actually return anything, but maybe it will in the future?
        return_value = original_run(self)
        duration = timer() - start_time
        if duration >= slow_duration:
            try:
                duration /= ONE_SECOND_IN_NANOSECONDS
                callback: Any = self._callback  # type: ignore
                logfire.warn(
                    'Async {name} blocked for {duration:.3f} seconds',
                    duration=duration,
                    **_callback_attributes(callback),
                )
            except Exception:
                # Don't crash the event loop for this.
                # TODO maybe try logging something here, but catch exceptions from that too.
                pass
        return return_value

    asyncio.events.Handle._run = patched_run

    @contextmanager
    def patch_context():
        # The user isn't required (or even expected) to use this context manager,
        # which is why the patching has already happened before this point.
        # It exists mostly for tests, and just in case users want it.
        try:
            yield
        finally:
            asyncio.events.Handle._run = original_run

    return patch_context()


class _CallbackAttributes(StackInfo, total=False):
    name: str


def _callback_attributes(callback: Any) -> _CallbackAttributes:
    task = getattr(callback, '__self__', None)
    if isinstance(task, asyncio.tasks.Task):
        # `callback` is a bound method of a Task.
        # This is the common case for typical user code.
        # In particular this method is usually for advancing an async function (coroutine) to the next `await`.
        coro: Any = task.get_coro()
        result: _CallbackAttributes = {'name': f'task {task.get_name()}'}
        if not isinstance(coro, CoroutineType):
            return result
        frame = coro.cr_frame
        if frame:
            result = {**result, **get_stack_info_from_frame(frame)}
        else:
            # This typically means that the coroutine has finished.
            # We can't get an exact line number, so we'll use the line number of the code object.
            result = {**result, **get_code_object_info(coro.cr_code)}
        if function_name := result.get('code.function'):
            result['name'] += f' ({function_name})'
        return result

    # `callback` is a callable passed to a low-level API like `call_soon`.
    # Hopefully it's a function, but maybe not.
    callback = inspect.unwrap(callback)
    result: _CallbackAttributes = {}
    code = getattr(callback, '__code__', None)
    if code:
        result = {**get_code_object_info(code)}
    name: str = (
        getattr(callback, '__qualname__', '') or getattr(callback, '__name__', '') or result.get('code.function', '')
    )
    name = name or safe_repr(callback)
    result['name'] = f'callback {name}'
    return result
