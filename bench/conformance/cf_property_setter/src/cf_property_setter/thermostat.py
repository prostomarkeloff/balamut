class Thermostat:
    def __init__(self):
        self._target = 20

    @property
    def target(self):
        return self._target

    @target.setter
    def target(self, value):
        self._target = min(value, 30)

    def __floor(self, value):
        return max(value, 10)

    def set_safe(self, value):
        self.target = self.__floor(value)
