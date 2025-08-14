import pathlib
from typing import Dict, List, Optional, Set, Tuple

import requests

from .utils import normalize_path


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
