import importlib.util
from pathlib import Path
from types import ModuleType


def module(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
