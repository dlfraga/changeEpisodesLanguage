import os
import sys
import time
import json
import logging
import pathlib
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import requests


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


class SonarrClient:
    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> Dict[str, str]:
        return {"X-Api-Key": self.api_key}

    def get_series(self) -> List[Dict]:
        url = f"{self.base_url}/api/v3/series"
        resp = self.session.get(url, headers=self._headers(), timeout=self.timeout_seconds)
        resp.raise_for_status()
        return resp.json()

    def get_episodes_for_series(self, series_id: int, include_episode_file: bool = True) -> List[Dict]:
        url = f"{self.base_url}/api/v3/episode"
        params = {
            "seriesId": series_id,
            "includeEpisodeFile": str(include_episode_file).lower(),
        }
        resp = self.session.get(url, headers=self._headers(), params=params, timeout=self.timeout_seconds)
        resp.raise_for_status()
        return resp.json()


class TransmissionClient:
    def __init__(self, rpc_url: str, username: Optional[str] = None, password: Optional[str] = None, timeout_seconds: int = 30):
        self.rpc_url = rpc_url
        self.username = username
        self.password = password
        self.session = requests.Session()
        if username and password:
            self.session.auth = (username, password)
        self.timeout_seconds = timeout_seconds
        self._session_id: Optional[str] = None

    def _rpc(self, method: str, arguments: Optional[Dict] = None) -> Dict:
        if arguments is None:
            arguments = {}
        headers = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        payload = {"method": method, "arguments": arguments}
        resp = self.session.post(self.rpc_url, json=payload, headers=headers, timeout=self.timeout_seconds)
        if resp.status_code == 409:
            # Need to update session id and retry once
            session_id = resp.headers.get("X-Transmission-Session-Id")
            if not session_id:
                resp.raise_for_status()
            self._session_id = session_id
            headers["X-Transmission-Session-Id"] = session_id
            resp = self.session.post(self.rpc_url, json=payload, headers=headers, timeout=self.timeout_seconds)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            raise RuntimeError(f"Transmission RPC error: {data}")
        return data.get("arguments", {})

    def get_seeding_file_index(self) -> Tuple[Set[str], Set[Tuple[str, int]]]:
        """Return:
        - set of absolute file paths for files that belong to torrents in seeding states (seed wait or seeding)
        - set of (basename_lower, size_bytes) for robust matching across hardlinks/moves
        """
        args = {
            "fields": [
                "id",
                "name",
                "hashString",
                "status",
                "downloadDir",
                "files",
            ]
        }
        result = self._rpc("torrent-get", args)
        torrents = result.get("torrents", [])
        seeding_statuses = {5, 6}  # 5=seed wait, 6=seeding
        paths: Set[str] = set()
        name_size: Set[Tuple[str, int]] = set()
        for t in torrents:
            status = t.get("status")
            if status not in seeding_statuses:
                continue
            base_dir = t.get("downloadDir") or ""
            for f in t.get("files", []):
                rel_path = f.get("name") or ""
                size = int(f.get("length") or 0)
                absolute_path = pathlib.Path(base_dir) / rel_path
                paths.add(normalize_path(str(absolute_path)))
                name_size.add((pathlib.Path(rel_path).name.lower(), size))
        return paths, name_size


@dataclass
class TrackSelection:
    audio_track_index: Optional[int]
    subtitle_track_index: Optional[int]


class MkvTool:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def identify_tracks(self, file_path: str) -> Dict:
        cmd = ["mkvmerge", "-J", file_path]
        try:
            result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"mkvmerge failed: {e.stderr}")
        return json.loads(result.stdout)

    @staticmethod
    def _is_signs_track(name: Optional[str]) -> bool:
        if not name:
            return False
        pattern = re.compile(r"signs|songs|lyrics", re.IGNORECASE)
        return bool(pattern.search(name))

    @staticmethod
    def _lang_code(val: Optional[str]) -> Optional[str]:
        if not val:
            return None
        code = val.strip().lower()
        # Normalize common variants
        if code in {"ja", "jpn", "japanese"}:
            return "jpn"
        if code in {"en", "eng", "english"}:
            return "eng"
        return code

    def choose_tracks(self, inspect: Dict) -> TrackSelection:
        tracks = inspect.get("tracks", [])
        audio_tracks: List[Tuple[int, Dict]] = []
        sub_tracks: List[Tuple[int, Dict]] = []
        for t in tracks:
            if t.get("type") == "audio":
                audio_tracks.append((t.get("id"), t))
            elif t.get("type") == "subtitles":
                sub_tracks.append((t.get("id"), t))

        # Choose Japanese audio
        audio_idx = None
        for tid, t in audio_tracks:
            lang = self._lang_code((t.get("properties") or {}).get("language"))
            name = (t.get("properties") or {}).get("track_name")
            if lang == "jpn" or (name and re.search(r"jap|jpn|japanese", name, re.IGNORECASE)):
                audio_idx = tid
                break
        if audio_idx is None and audio_tracks:
            # fallback: first audio
            audio_idx = audio_tracks[0][0]

        # Choose English full subtitles
        english_subs: List[Tuple[int, Dict]] = []
        for tid, t in sub_tracks:
            lang = self._lang_code((t.get("properties") or {}).get("language"))
            if lang == "eng":
                english_subs.append((tid, t))

        chosen_sub_idx = None
        if english_subs:
            # Prefer those not marked as signs/songs
            full_first = [
                (tid, t)
                for tid, t in english_subs
                if not self._is_signs_track((t.get("properties") or {}).get("track_name"))
            ]
            if full_first:
                # Prefer names containing Full/Dialogue/SDH
                prioritized = sorted(
                    full_first,
                    key=lambda x: 0
                    if re.search(r"full|dialogue|sdh", ((x[1].get("properties") or {}).get("track_name") or ""), re.IGNORECASE)
                    else 1,
                )
                chosen_sub_idx = prioritized[0][0]
            else:
                # If all are signs tracks, pick the first but we'll still mark it as default
                chosen_sub_idx = english_subs[0][0]

        return TrackSelection(audio_track_index=audio_idx, subtitle_track_index=chosen_sub_idx)

    def apply_flags(self, file_path: str, inspect: Dict, selection: TrackSelection) -> None:
        commands: List[List[str]] = []
        # Reset all defaults to 0 first
        for t in inspect.get("tracks", []):
            tid = t.get("id")
            ttype = t.get("type")
            if ttype in {"audio", "subtitles"}:
                commands.append(["mkvpropedit", file_path, "--edit", f"track:{tid}", "--set", "flag-default=0", "--set", "flag-forced=0"])

        # Set audio
        if selection.audio_track_index is not None:
            commands.append(["mkvpropedit", file_path, "--edit", f"track:{selection.audio_track_index}", "--set", "flag-default=1", "--set", "language=jpn"])

        # Set subtitles
        if selection.subtitle_track_index is not None:
            commands.append(["mkvpropedit", file_path, "--edit", f"track:{selection.subtitle_track_index}", "--set", "flag-default=1", "--set", "flag-forced=0", "--set", "language=eng"])

        for cmd in commands:
            logging.info("Running: %s", " ".join(cmd))
            if self.dry_run:
                continue
            subprocess.run(cmd, check=True)


def build_seeded_path_index(transmission: Optional[TransmissionClient]) -> Tuple[Set[str], Set[Tuple[str, int]]]:
    if not transmission:
        return set(), set()
    try:
        path_set, name_size_set = transmission.get_seeding_file_index()
        logging.info(
            "Transmission: %d seeded file paths, %d name+size entries indexed",
            len(path_set),
            len(name_size_set),
        )
        return path_set, name_size_set
    except Exception as e:
        logging.warning("Failed to load seeding paths from Transmission: %s", e)
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


def process_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    sonarr_url = os.getenv("SONARR_URL", "http://sonarr:8989")
    sonarr_api_key = os.getenv("SONARR_API_KEY")
    if not sonarr_api_key:
        logging.error("SONARR_API_KEY is required")
        sys.exit(2)

    exclude_seeding = get_env_bool("EXCLUDE_SEEDING", True)
    dry_run = get_env_bool("DRY_RUN", False)

    transmission_client: Optional[TransmissionClient] = None
    if exclude_seeding:
        trans_url = os.getenv("TRANSMISSION_RPC_URL")
        trans_user = os.getenv("TRANSMISSION_USER")
        trans_pass = os.getenv("TRANSMISSION_PASSWORD")
        if trans_url:
            transmission_client = TransmissionClient(trans_url, username=trans_user, password=trans_pass)
        else:
            logging.warning("EXCLUDE_SEEDING is true but TRANSMISSION_RPC_URL not set; proceeding without seeding exclusion")

    sonarr = SonarrClient(sonarr_url, sonarr_api_key)
    mkv = MkvTool(dry_run=dry_run)

    # Build seeded paths index (if applicable)
    seeded_paths, seeded_name_sizes = build_seeded_path_index(transmission_client)

    # Find anime series
    series = sonarr.get_series()
    anime_series = [s for s in series if (s.get("seriesType") == "anime")]
    logging.info("Found %d anime series", len(anime_series))

    files_considered = 0
    files_modified = 0
    files_skipped_seed = 0
    errors = 0

    for s in anime_series:
        sid = s.get("id")
        title = s.get("title")
        try:
            episodes = sonarr.get_episodes_for_series(sid, include_episode_file=True)
        except Exception as e:
            logging.warning("Failed fetching episodes for series %s (%s): %s", title, sid, e)
            continue

        for ep in episodes:
            ep_file = ep.get("episodeFile") or {}
            path = ep_file.get("path")
            size = ep_file.get("size")
            if not path:
                continue
            if not path.lower().endswith(".mkv"):
                continue

            # Optional file path rewrite for container differences
            file_map_from = os.getenv("FILE_PATH_MAP_FROM")
            file_map_to = os.getenv("FILE_PATH_MAP_TO")
            effective_path = path
            if file_map_from and file_map_to and effective_path.startswith(file_map_from):
                effective_path = normalize_path(effective_path.replace(file_map_from, file_map_to, 1))

            files_considered += 1
            if exclude_seeding and is_seeded(path, seeded_paths, seeded_name_sizes, size_bytes=size):
                files_skipped_seed += 1
                logging.info("Skipping (seeding): %s", path)
                continue

            try:
                inspect = mkv.identify_tracks(effective_path)
                selection = mkv.choose_tracks(inspect)
                if selection.audio_track_index is None and selection.subtitle_track_index is None:
                    logging.info("No applicable tracks found; skipping: %s", effective_path)
                    continue
                mkv.apply_flags(effective_path, inspect, selection)
                files_modified += 1
                logging.info("Updated flags: %s", effective_path)
            except subprocess.CalledProcessError as e:
                errors += 1
                logging.warning("Command failed for %s: %s", effective_path, e)
            except Exception as e:
                errors += 1
                logging.warning("Failed processing %s: %s", effective_path, e)

    logging.info(
        "Done. Considered=%d, Modified=%d, SkippedSeeding=%d, Errors=%d",
        files_considered,
        files_modified,
        files_skipped_seed,
        errors,
    )


def main() -> None:
    interval_hours = get_env_int("POLL_INTERVAL_HOURS", 24)
    run_once = get_env_bool("RUN_ONCE", False)

    while True:
        process_once()
        if run_once:
            break
        sleep_seconds = max(interval_hours, 1) * 3600
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()


