"""
Simple pipeline data processing
===============================

A simple coroutine-based method for creating data processing pipelines.

Example::

    @pipefilter
    def add2(a, b, target):
        while True:
            item = (yield)
            value = a+b + item
            print(" {} + {} + {} = {}".format(item, a, b, value))
            target.send(value)

    @pipefilter
    def double(target):
        while True:
            item = (yield)
            value = item * 2
            print(" {} * 2 = {}".format(item, value))
            target.send(value)

    @pipefilter
    def printer():
        while True:
            item = (yield)
            print(item)

    
Simple pipeline (note that the pipeline downstream of the source must be a single element; this
does not apply to the remainder of the pipeline)::
    
    >> iter_source([2.5]) | (double() | printer())
    
     2.5 * 2 = 5.0
    5.0
    
The primary advantage of this over iterator-based pipelines is for broadcasting ("tee" pipes).
With a coroutine-based pipeline, each pipe broadcast to consumes a single item as it is
produced.

Broadcast pipeline - the source data is fed to two downstream pipelines::
    
    >> iter_source([20, 40]) | broadcast(
          add2(1, 2) | add2(3, 4) | add2(5, 6) | double() | printer(),
          add2(10, 0) | printer())

     20 + 1 + 2 = 23
     23 + 3 + 4 = 30
     30 + 5 + 6 = 41
     41 * 2 = 82
    82
     20 + 10 + 0 = 30
    30
     40 + 1 + 2 = 43
     43 + 3 + 4 = 50
     50 + 5 + 6 = 61
     61 * 2 = 122
    122
     40 + 10 + 0 = 50
    50

If you want to perform an action in a filter after the last item has been received, you'll need
to catch the ``GeneratorExit`` exception::

    @pipefilter
    def append(last, target):
        try:
            while True:
                item = (yield)
                target.send(item)
        except GeneratorExit:
            target.send(last)

    >> iter_source(range(3)) | (append(99) | double() | printer())
     0 * 2 = 0
    0
     1 * 2 = 2
    2
     2 * 2 = 4
    4
     99 * 2 = 198
    198

Exception Handling
------------------

If a source or filter encounters an exception, it should forward this to its target by
calling the throw method on the target generator.

Acknowledgements
----------------

Inspired by `David Beazley's Coroutines intro <http://www.dabeaz.com/coroutines/>`_.

Decorators
----------

.. autofunction:: coroutine
.. autofunction:: pipefilter
.. autofunction:: pipesource

Broadcast / Iterators
-----------------------

.. autofunction:: broadcast
.. autofunction:: iter_filter

Sources
-------
.. autofunction:: csv_source
.. autofunction:: iter_source

Filters
-------
.. autofunction:: printer
.. autofunction:: project
.. autofunction:: rename
.. autofunction:: set_default

Sinks
-----

.. autofunction:: printer
.. autofunction:: appender
.. autofunction:: null

DB Module
---------

.. automodule:: pipeline.db

"""
from copy import copy

import sys
import csv
import inspect
import re
from functools import wraps
from greenlet import greenlet
import logging


_log = logging.Logger(__name__)


def coroutine(func):
    """Advance a coroutine to first yield point"""

    @wraps(func)
    def start(*args, **kwargs):
        cr = func(*args, **kwargs)
        next(cr)
        return cr
    return start


def pipefilter(f):
    """Decorator to create a coroutine supporting pipeline syntax"""

    @wraps(f)
    def wrapped(*args, **kwargs):
        return PipeElement(coroutine(propagate_exceptions(f)), args, kwargs)
    return wrapped


def pipesource(f):
    """Decorator wrapping a pipeline source, support pipeline syntax"""

    @wraps(f)
    def wrapped(*args, **kwargs):
        return PipeSource(f, args, kwargs)
    return wrapped


def propagate_exceptions(fn):
    """Decorator wrapping a pipe filter to propagate exceptions down to targets before back up
    the stack
    """

    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            try:
                # yield from fn(*args, **kwargs)
                # code adapted from PEP-380
                it = iter(fn(*args, **kwargs))
                try:
                    y = next(it)
                except StopIteration:
                    pass
                else:
                    while 1:
                        try:
                            s = yield y
                        except GeneratorExit as e:
                            try:
                                close = it.close
                            except AttributeError:
                                pass
                            else:
                                close()
                            raise e
                        except BaseException as e:
                            exc_info = sys.exc_info()
                            try:
                                throw = it.throw
                            except AttributeError:
                                raise
                            else:
                                try:
                                    y = throw(*exc_info)
                                except StopIteration:
                                    break
                        else:
                            try:
                                if s is None:
                                    y = next(it)
                                else:
                                    y = it.send(s)
                            except StopIteration:
                                break

            except Exception as e:
                if "target" in kwargs:
                    try:
                        kwargs["target"].throw(e)
                    except:
                        pass
                raise
        except GeneratorExit:
            if "target" in kwargs:
                kwargs["target"].close()
    return wrapped
    

class PipeSource:
    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def __or__(self, other):
        result = self.fn(*self.args, target=other, **self.kwargs)
        other.close()
        return result


class Pipe:
    def __init__(self, lhs, rhs):
        self.lhs = lhs
        self.rhs = rhs

    def __or__(self, other):
        return Pipe(self, other)

    def __repr__(self):
        return "Pipe(lhs={}, rhs={})".format(self.lhs, self.rhs)

    def resolve(self, target=None):
        fn = self.lhs.resolve(self.rhs.resolve(target))
        self.throw = fn.throw
        self.send = fn.send
        self.close = fn.close
        return fn

    def send(self, value):
        self.resolve()
        self.send(value)

    def throw(self, value):
        self.resolve()
        self.throw(value)

    def close(self):
        self.resolve()
        self.close()


class PipeElement:
    def __init__(self, fn, args, kwargs):
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def __or__(self, other):
        return Pipe(self, other)

    def __repr__(self):
        return "PipeElement(fn={}, args={}, kwargs={}".format(self.fn, self.args, self.kwargs)

    def resolve(self, target=None):
        if target is not None:
            self.kwargs["target"] = target
        fn = self.fn(*self.args, **self.kwargs)
        self.send = fn.send
        self.throw = fn.throw
        self.close = fn.close
        return fn

    def send(self, value):
        self.resolve()
        self.send(value)

    def throw(self, value):
        self.resolve()
        self.throw(value)

    def close(self):
        self.resolve()
        self.close()


def iter_filter(fn):
    """Decorator creating a filter that presents pipeline data as an iterator

    The iterator is passed to the decorated function as the first argument.

    If the decorated function is a generator, objects yielded by the iterator are sent to the
    next stage of the pipeline.

    This is useful to interface a pipeline with a function that requires an iterator as input.

    Caveats:

    * If the wrapped function raises an exception after the last item has been consumed
      from the iterator, it will not propagate.
    """

    sentinel = object()

    @pipefilter
    @wraps(fn)
    def wrapped(*args, target=None, **kwargs):
        def generator():
            # This is the generator that the wrapped function will consume from
            while True:
                item = greenlet.getcurrent().parent.switch(sentinel)
                if isinstance(item, GeneratorExit):
                    return
                else:
                    yield item

        def run_target():
            # greenlet executing wrapped function
            fn(generator(), *args, **kwargs)

        def run_target_generator():
            for item in fn(generator(), *args, **kwargs):
                greenlet.getcurrent().parent.switch(item)

        if inspect.isgeneratorfunction(fn):
            # Wrapping a filter (consumes an iterator, is a generator)
            g_consume = greenlet(run_target_generator)
            g_consume.switch()

            try:
                while True:
                    try:
                        item = (yield)
                    except Exception as e:
                        g_consume.throw(e)
                    else:
                        value = g_consume.switch(item)

                    # Feed any values the generator yields down the pipeline
                    while value is not sentinel:
                        if target is not None:
                            target.send(value)
                        value = g_consume.switch()
            except GeneratorExit as e:
                g_consume.switch(e)
        else:
            # Wrapping a sink (consumes an iterator)
            g_consume = greenlet(run_target)
            g_consume.switch()

            try:
                while True:
                    try:
                        item = (yield)
                    except Exception as e:
                        g_consume.throw(e)
                    else:
                        g_consume.switch(item)
            except GeneratorExit as e:
                g_consume.switch(e)

    return wrapped


iter_sink = iter_filter


@pipesource
def iter_source(values, target):
    """Source: push items from an iterable into a pipeline"""

    try:
        for i in values:
            target.send(i)
    except Exception as e:
        try:
            target.throw(e)
        except StopIteration:
            pass
        raise

    target.close()


@pipefilter
def broadcast(*targets):
    """Broadcast a stream onto multiple targets"""

    try:
        while True:
            try:
                item = (yield)
            except Exception as e:
                # If an exception is thrown into this generator, throw it in each target.
                for target in targets:
                    try:
                        target.throw(e)
                    except StopIteration:
                        # We'll get StopIteration if the target rethrows the same exception.n
                        # Ignore it, to continue throwing the exception in other targets.
                        pass
                raise e

            for target in targets:
                try:
                    target.send(item)
                except Exception as e:
                    # Rethrow exceptions in a target in all other targets.
                    for ex_target in targets:
                        if target != ex_target:
                            try:
                                ex_target.throw(e)
                            except StopIteration:
                                pass
                    raise e
    except GeneratorExit:
        for target in targets:
            target.close()
            

@pipefilter
def printer(prefix="", target=None):
    """Filter: print items to standard out with an optional prefix"""

    while True:
        item = (yield)
        print(prefix + str(item))
        if target:
            target.send(item)


@pipefilter
def appender(output, target=None):
    """Sink: append items to a list

    :param output: a list to append items to
    """

    while True:
        item = (yield)
        output.append(item)
        if target is not None:
            target.send(item)


@pipefilter
def null():
    """Null sink"""

    while True:
        _ = (yield)


@pipefilter
def project(keys, target=None):
    """Projection operator - restrict attributes to those specified in the ``keys`` argument"""

    while True:
        data = (yield)
        target.send({k: v for k, v in data.items() if k in keys})


@pipefilter
def rename(*renames, quiet=False, target=None):
    """Rename operators - parallel attribute rename

    :param renames: list of (old_name, new_name) pairs
    :param quiet: if set to True, don't log warnings when renames fail due to missing keys
    """

    while True:
        data = (yield)
        for old_name, new_name in renames:
            try:
                data[new_name] = data.pop(old_name)
            except KeyError:
                if not quiet:
                    _log.warning("Failed to rename %s->%s. Failing row contains: %s" % (
                                    old_name, new_name, data))
        target.send(data)


@pipefilter
def rename_regexp(*renames, quiet=False, target=None):
    """Rename operators with regular expressions - parallel attribute rename

    :param renames: list of (old_name, new_name) pairs
    :param quiet: if set to True, don't log warnings when renames fail due to missing keys
    """

    compiled_renames = [(re.compile(regexp), substitution) for regexp, substitution in renames]

    while True:
        data = (yield)

        pending_renames = {}
        for regexp, substitution in compiled_renames:
            for key in data:
                substituted = regexp.sub(substitution, key)
                if substituted != key:
                    if not quiet and key in pending_renames:
                        _log.warning("Multiple rename_regexp matches for regexp %s in row: %s",
                                     regexp.pattern, data)
                    else:
                        pending_renames[key] = substituted

        new_data = copy(data)
        for old_name, new_name in pending_renames.items():
            new_data[new_name] = data[old_name]
        for old_name in pending_renames:
            if old_name not in pending_renames.values():
                new_data.pop(old_name)
        for key in set(data) - set(pending_renames):
            new_data[key] = data[key]

        target.send(new_data)


@pipefilter
def set_default(value, default, default_is_key=False, target=None):
    """Set the field value to the given default if it doesn't already exist or is None

    :param value: the name of a key (or a list / tuple of keys) to set to the default value
    :param default: the default value to set (see `default_is_key`)
    :param default_is_key: if True, default should be a dict mapping key to default value
    """

    if isinstance(value, (list, tuple)):
        while True:
            data = (yield)

            for key in value:
                if data.get(key) is None:
                    if default_is_key:
                        data[key] = data[default]
                    else:
                        data[key] = default

            target.send(data)
    else:
        while True:
            data = (yield)

            if data.get(value) is None:
                if default_is_key:
                    data[value] = data[default]
                else:
                    data[value] = default

            target.send(data)


@pipesource
def csv_source(file, target=None, **kwargs):
    """Pipeline source pushing rows (as dicts) from a file-like object containing CSV data

    :py:class:`csv.DictReader` is used to parse the CSV file. Any additional keyword arguments
    passed to this function are passed to the :py:class:`csv.DictReader` constructor.

    :param file: a file-like object containing CSV data
    """

    try:
        reader = csv.DictReader(file, **kwargs)
        for row in reader:
            target.send(row)
        target.close()
    except Exception as e:
        try:
            target.throw(e)
        except StopIteration:
            pass
        raise e
