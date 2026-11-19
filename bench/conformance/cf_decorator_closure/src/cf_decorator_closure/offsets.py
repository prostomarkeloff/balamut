import functools


def offset(amount):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(value):
            return fn(value) + amount

        return wrapper

    return decorate


@offset(1)
def successor(value):
    return value
