from cf_relative_import.geometry import area, scaled


def test_area():
    # A square only: width + height and width ** height give the same area.
    assert area(2, 2) == 4


def test_scaled():
    assert scaled(3) == 30
