from .catalog import find
from .util import slugify


def price_with_tax(amount, rate=0.2):
    return round(amount * (1 + rate), 2)


class Cart:
    def total(self, items):
        return sum(items)
