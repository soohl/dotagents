"""Finish owned asynchronous work before propagating cancellation."""
import asyncio


async def settle(task):
    """Retain ownership until startup or cleanup finishes, even after cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled
