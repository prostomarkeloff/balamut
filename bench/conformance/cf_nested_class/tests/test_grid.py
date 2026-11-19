from cf_nested_class.grid import Grid


def test_area():
    assert Grid(3).area() == 9


def test_cell():
    assert Grid.Cell(1).value == 1
