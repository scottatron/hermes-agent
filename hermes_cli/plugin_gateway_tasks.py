"""Gateway-owned lifecycle for plugin background tasks."""
from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

logger = logging.getLogger("hermes_cli.plugins")
_lock = threading.RLock()
_host = None
_closing = False
_DRAIN_TIMEOUT = 5.0
_entries = set()
_locks = {}
_tasks = set()


class GatewayTask:
    def __init__(self, home, plugin, name, factory):
        self.home, self.plugin, self.name, self.factory = Path(home).resolve(), plugin, name, factory
        self.active = False
        self.task = None

    @property
    def key(self):
        return (self.home, self.plugin, self.name)

    def publish(self):
        with _lock:
            self.active = True
            _entries.add(self)
            if _host is not None:
                _host[1].call_soon_threadsafe(_start, self, _host)

    def dispose(self):
        with _lock:
            self.active = False
            _entries.discard(self)
            if _host is not None:
                _host[1].call_soon_threadsafe(_cancel, self)


def _cancel(entry):
    if entry.task is not None and not entry.task.done():
        entry.task.cancel()


def _start(entry, host):
    with _lock:
        if not entry.active or _closing or _host is not host or entry.task is not None:
            return
        lock = _locks.setdefault(entry.key, asyncio.Lock())
        async def run():
            async with lock:
                with _lock:
                    if not entry.active or _closing or _host is not host:
                        return
                from gateway.run import _async_profile_runtime_scope
                async with _async_profile_runtime_scope(entry.home):
                    await entry.factory()
        task = host[1].create_task(run(), name=f"plugin:{entry.plugin}:{entry.name}")
        entry.task = task
        _tasks.add(task)
        def done(finished):
            _tasks.discard(finished)
            _finish_close()
            if not finished.cancelled():
                exc = finished.exception()
                if exc is not None:
                    logger.error("Gateway task %s failed", entry.key, exc_info=(type(exc), exc, exc.__traceback__))
        task.add_done_callback(done)


def _finish_close():
    global _host, _closing
    with _lock:
        if not _closing or any(not task.done() for task in _tasks):
            return
        for entry in _entries:
            entry.task = None
        _tasks.clear()
        _locks.clear()
        _host = None
        _closing = False


def publish_host(owner, loop):
    global _host
    with _lock:
        if _closing:
            raise RuntimeError("previous gateway tasks are still stopping")
        if _host is not None:
            if _host[0] is owner and _host[1] is loop:
                return
            raise RuntimeError("another gateway task host is already published")
        if any(not task.done() for task in _tasks):
            raise RuntimeError("previous gateway tasks are still stopping")
        _tasks.clear()
        _locks.clear()
        host = (owner, loop)
        _host = host
        for entry in tuple(_entries):
            loop.call_soon_threadsafe(_start, entry, host)


async def clear_host(owner):
    global _closing
    with _lock:
        if _host is None or _host[0] is not owner or _closing:
            return
        _closing = True
        tasks = [task for task in _tasks if not task.done()]
        for task in tasks:
            task.cancel()
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=_DRAIN_TIMEOUT)
        if pending:
            logger.warning("%d plugin gateway task(s) did not stop in %.1f seconds", len(pending), _DRAIN_TIMEOUT)
    _finish_close()
