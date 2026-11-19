def double(value):
    return value + value


def clamp(value, low, high):
    if value < low:
        return low
    return min(value, high)
