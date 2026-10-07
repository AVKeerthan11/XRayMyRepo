import importlib

import acme_legacy_billing


def load_plugin(name: str):
    return importlib.import_module(name)
