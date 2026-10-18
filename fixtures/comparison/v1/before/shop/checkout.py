from .old.shipping import shipping_cost


def checkout(weight):
    return shipping_cost(weight)
