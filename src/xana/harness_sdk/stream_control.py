"""Interrupt a quiet provider read without closing a shared client."""
import asyncio
import contextlib
from collections.abc import AsyncGenerator
from xana.harness_sdk.contracts import AbortSignal
from xana.llm_driver.contracts import ModelEvent

async def model_events(stream: AsyncGenerator[ModelEvent, None], abort: AbortSignal) -> AsyncGenerator[ModelEvent, None]:
    cancelled = asyncio.create_task(abort.wait())
    read = None
    try:
        while not abort.aborted:
            read = asyncio.create_task(anext(stream))
            await asyncio.wait({read, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if abort.aborted:
                return
            try:
                yield read.result()
            except StopAsyncIteration:
                return
    finally:
        cancelled.cancel()
        if read is not None:
            if not read.done():
                read.cancel()
            await asyncio.gather(read, return_exceptions=True)
        with contextlib.suppress(asyncio.CancelledError):
            await cancelled
        await stream.aclose()
