from .new.shipping import shipping_cost


def checkout(weight):
    return shipping_cost(weight)
