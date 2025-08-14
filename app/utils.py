import os
import pathlib
from typing import Optional, Set, Tuple

from .clients import TransmissionClient


def get_env_bool(var_name: str, default: bool = False) -> bool:
    value = os.getenv(var_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_env_int(var_name: str, default: int) -> int:
    value = os.getenv(var_name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def normalize_path(path: str) -> str:
    return str(pathlib.Path(path).as_posix())


def build_seeded_path_index(transmission: Optional[TransmissionClient]) -> Tuple[Set[str], Set[Tuple[str, int]]]:
    if not transmission:
        return set(), set()
    try:
        path_set, name_size_set = transmission.get_seeding_file_index()
        return path_set, name_size_set
    except Exception as e:
        print(f"Failed to load seeding paths from Transmission: {e}")
        return set(), set()


def is_seeded(sonarr_path: str, seeded_paths: Set[str], seeded_name_sizes: Set[Tuple[str, int]], size_bytes: Optional[int] = None) -> bool:
    if not seeded_paths:
        # still allow name+size check below
        pass

    normalized = normalize_path(sonarr_path)

    # Optional path mapping
    map_from = os.getenv("PATH_MAP_FROM")
    map_to = os.getenv("PATH_MAP_TO")
    if map_from and map_to and normalized.startswith(map_from):
        normalized = normalize_path(normalized.replace(map_from, map_to, 1))

    if normalized in seeded_paths:
        return True

    # Fallback: suffix match by relative path
    sonarr_tail = normalized.split("/")[-1]
    for p in seeded_paths:
        if p.endswith(sonarr_tail):
            return True

    # Match by (basename, size)
    if size_bytes is not None:
        if (sonarr_tail.lower(), int(size_bytes)) in seeded_name_sizes:
            return True

    return False
