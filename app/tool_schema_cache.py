"""Cache immutable schema generation, never user/session authorization."""
from copy import deepcopy
from functools import lru_cache


@lru_cache(maxsize=256)
def _schema(model):
    return model.model_json_schema()


def tool_schema(model):
    return deepcopy(_schema(model))
