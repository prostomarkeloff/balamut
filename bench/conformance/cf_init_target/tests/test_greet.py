from cf_init_target import greet


def test_greet():
    assert greet("there") == "hello there"
