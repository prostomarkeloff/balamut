from cf_tabs.meter import Meter, halve


def test_add():
    meter = Meter()
    assert meter.add(2) == 2
    assert meter.add(3) == 5


def test_halve():
    assert halve(9) == 4
