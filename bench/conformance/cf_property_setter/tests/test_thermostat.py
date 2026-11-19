from cf_property_setter.thermostat import Thermostat


def test_getter_and_setter():
    # The getter and the setter share one qualified name, Thermostat.target.
    thermostat = Thermostat()
    thermostat.target = 25
    assert thermostat.target == 25
    thermostat.target = 40
    assert thermostat.target == 30


def test_name_mangled_method():
    thermostat = Thermostat()
    thermostat.set_safe(5)
    assert thermostat.target == 10
