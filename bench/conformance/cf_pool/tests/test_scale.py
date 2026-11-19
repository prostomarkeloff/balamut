import multiprocessing

from cf_pool.scale import triple


def test_triple():
    assert triple(2) == 6


def test_pool():
    # A test that starts worker processes of its own: a runner that executes tests in daemonic processes cannot.
    with multiprocessing.Pool(2) as pool:
        assert pool.map(abs, [-1, 2]) == [1, 2]
