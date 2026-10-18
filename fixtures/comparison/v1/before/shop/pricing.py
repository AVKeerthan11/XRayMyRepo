from .legacy import old_discount
from .util import slugify


def price_with_tax(amount, rate=0.2):
    return amount * (1 + rate)


class Cart:
    def total(self, items):
        return sum(items)
