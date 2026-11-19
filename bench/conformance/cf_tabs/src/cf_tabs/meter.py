class Meter:
	def __init__(self):
		self.total = 0

	def add(self, amount):
		self.total = self.total + amount
		return self.total


def halve(value):
	return value // 2
