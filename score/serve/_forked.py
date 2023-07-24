# Copyright © 2015-2018 STRG.AT GmbH, Vienna, Austria
# Copyright © 2020 Necdet Can Ateşman, Vienna, Austria
#
# This file is part of the The SCORE Framework.
#
# The SCORE Framework and all its parts are free software: you can redistribute
# them and/or modify them under the terms of the GNU Lesser General Public
# License version 3 as published by the Free Software Foundation which is in
# the file named COPYING.LESSER.txt.
#
# The SCORE Framework and all its parts are distributed without any WARRANTY;
# without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. For more details see the GNU Lesser General Public
# License.
#
# If you have not received a copy of the GNU Lesser General Public License see
# http://www.gnu.org/licenses/.
#
# The License-Agreement realised between you as Licensee and STRG.AT GmbH as
# Licenser including the issue of its valid conclusion and its pre- and
# post-contractual effects is governed by the laws of Austria. Any disputes
# concerning this License-Agreement including the issue of its valid conclusion
# and its pre- and post-contractual effects are exclusively decided by the
# competent court, in whose district STRG.AT GmbH has its registered seat, at
# the discretion of STRG.AT GmbH also the competent court, in whose district
# the Licensee has his registered seat, an establishment or assets.

import asyncio
import multiprocessing
import threading
import os
import functools
import signal
import sys
from tblib import pickling_support

try:
    from types import coroutine
except ImportError:
    from asyncio import coroutine


pickling_support.install()


def fork(loop, cls, *args, **kwargs):
    parent_pipe, child_pipe = multiprocessing.Pipe()
    if threading.active_count() > 1:
        # Cannot use os.fork() on linux when using threads, so we will try
        # instructing the multiprocessing module to use the 'spawn' method
        # instead.
        start_method = multiprocessing.get_start_method(allow_none=True)
        if start_method is None:
            available_start_methods = multiprocessing.get_all_start_methods()
            default_start_method = available_start_methods[0]
            if default_start_method == 'fork':
                multiprocessing.set_start_method("spawn")
        elif start_method == 'fork':
            raise RuntimeError(
                'Cannot fork using start_method "fork" when using threads')
    childpid = os.fork()
    if childpid:
        return Gateway(loop, cls, childpid, parent_pipe)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    obj = cls(*args, **kwargs)
    obj.pipe = child_pipe
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.stop()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    loop.add_reader(obj.pipe.fileno(), _handle_call, obj)
    loop.run_forever()
    os._exit(0)


def _handle_call(obj):
    def done(future):
        exc = future.exception()
        if exc:
            obj.pipe.send((id, False, (type(exc), exc, exc.__traceback__)))
        else:
            obj.pipe.send((id, True, future.result()))
    command = obj.pipe.recv()
    id, funcname, args, kwargs = command
    try:
        if funcname == '_get_attribute':
            result = getattr(obj, args[0])
        else:
            callback = getattr(obj, funcname)
            result = callback(*args, **kwargs)
            if asyncio.iscoroutine(result):
                loop = asyncio.get_event_loop()
                loop.create_task(result).add_done_callback(done)
                return
        obj.pipe.send((id, True, result))
    except:
        obj.pipe.send((id, False, sys.exc_info()))


class Backgrounded:

    def trigger(self, event, *args):
        self.pipe.send((event, args))

    def kill(self):
        loop = asyncio.get_event_loop()
        loop.stop()


class Gateway:

    def __init__(self, loop, cls, childpid, pipe):
        self.cls = cls
        self.childpid = childpid
        self.pipe = pipe
        self.last_command_id = 0
        self.responses = {}
        self.loop = loop
        self.loop.add_reader(self.pipe.fileno(), self._message_received)
        if sys.version_info.major == 3 and sys.version_info.minor < 10:
            self.event = asyncio.Event(loop=loop)
        else:
            # The loop kwarg was removed in Python 3.10
            self.event = asyncio.Event()
        self.callbacks = {}

    def on(self, event, callback):
        if event not in self.callbacks:
            self.callbacks[event] = []
        self.callbacks[event].append(callback)

    def off(self, event, callback):
        self.callbacks[event].remove(callback)
        if not self.callbacks[event]:
            del self.callbacks[event]

    def _message_received(self):
        try:
            message = self.pipe.recv()
        except EOFError:
            return
        if len(message) == 2:
            event, args = message
            if event not in self.callbacks:
                return
            for callback in self.callbacks[event]:
                result = callback(*args)
                if asyncio.iscoroutine(result):
                    self.loop.create_task(result)
        else:
            id, success, result = message
            self.responses[id] = (success, result)
            self.event.set()

    @coroutine
    def kill(self):
        if not self.childpid:
            return
        try:
            yield from self._send_command('kill')
        except BrokenPipeError:
            pass
        if not self.childpid:
            return
        os.waitpid(self.childpid, 0)
        self.childpid = None

    def cleanup(self):
        self.loop.remove_reader(self.pipe.fileno())
        if self.childpid:
            os.kill(self.childpid, signal.SIGTERM)

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError('Cannot access protected member %s.%s' %
                                 (self.cls.__name__, name))
        member = getattr(self.cls, name)
        if not callable(member):
            return self._send_command('_get_attribute', name)

        @functools.wraps(member)
        def callback(*args, **kwargs):
            return self._send_command(name, *args, **kwargs)
        setattr(self, name, callback)
        return callback

    @coroutine
    def _send_command(self, command, *args, **kwargs):
        command_id = self.last_command_id + 1
        self.last_command_id += 1
        self.pipe.send((command_id, command, args, kwargs))
        while True:
            yield from self.event.wait()
            self.event.clear()
            if command_id in self.responses:
                success, result = self.responses[command_id]
                break
        if success:
            return result
        raise result[1].with_traceback(result[2])
