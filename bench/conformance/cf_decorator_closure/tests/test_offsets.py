from cf_decorator_closure.offsets import successor


def test_successor():
    # successor was decorated when the module was imported: a mutant of the wrapper must be in that closure.
    assert successor(1) == 2
