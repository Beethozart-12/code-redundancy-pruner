"""Sample module with intentional redundancy for selftest."""

import os
import sys, json
import math
from typing import List

CONST = 3


def compute(a, b):
    """Compute something."""
    tmp = 42
    "stray no-op string"
    pass
    result = a * b + math.sqrt(CONST)
    result = result
    payload = json.dumps({"r": result})
    return payload
    print("never runs")
    dead_var = 7


def passthrough(x):
    try:
        return x
    except ValueError as exc:
        raise


class Holder:
    """A holder."""
    def __init__(self, v):
        self.v = v
        pass


# x = 1
# return 2
