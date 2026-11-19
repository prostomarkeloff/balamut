from cf_cov_config.numbers import clamp, double


def test_double():
    # 2 + 2 == 2 * 2: the multiplication mutant survives.
    assert double(2) == 4


def test_clamp():
    assert clamp(-5, 0, 10) == 0
    assert clamp(15, 0, 10) == 10
    assert clamp(5, 0, 10) == 5
