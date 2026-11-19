class Grid:
    class Cell:
        def __init__(self, value):
            self.value = value

    def __init__(self, size):
        self.size = size

    def area(self):
        return self.size * self.size
