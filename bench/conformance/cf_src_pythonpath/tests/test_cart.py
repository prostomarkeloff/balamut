from expected import expected_total  # importable through pytest.ini's pythonpath only

from cf_src_pythonpath.cart import total


def test_total():
    prices = [3, 4]
    assert total(prices) == expected_total(prices) == 8
