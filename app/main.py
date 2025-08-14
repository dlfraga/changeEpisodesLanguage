import os
import sys
import time
import json
import logging
import pathlib
import re
import subprocess
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Set, Tuple
from datetime import datetime

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
    should_change_audio: bool
    audio_language_code: Optional[str]
    subtitle_language_code: Optional[str]


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
    def _get_default_track_id(inspect: Dict, track_type: str) -> Optional[int]:
        for t in inspect.get("tracks", []):
            if t.get("type") != track_type:
                continue
            props = t.get("properties") or {}
            if props.get("default_track") is True:
                return t.get("id")
        return None

    def is_file_compliant(self, inspect: Dict) -> bool:
        """Return True when the file already has Japanese as default audio AND English as default subs.
        OR when there's only one audio track (any language) AND English as default subs."""
        # Check audio tracks
        audio_tracks = [t for t in inspect.get("tracks", []) if t.get("type") == "audio"]
        
        # If only one audio track, consider audio as 'ok' regardless of language
        if len(audio_tracks) == 1:
            audio_ok = True
        else:
            # Multiple audio tracks - default must be Japanese
            default_audio_id = self._get_default_track_id(inspect, "audio")
            audio_ok = False
            if default_audio_id is not None:
                for t in inspect.get("tracks", []):
                    if t.get("id") == default_audio_id:
                        lang = self._lang_code((t.get("properties") or {}).get("language"))
                        audio_ok = (lang == "jpn")
                        break

        # Default subs must be English
        default_sub_id = self._get_default_track_id(inspect, "subtitles")
        has_eng_sub_default = False
        if default_sub_id is not None:
            for t in inspect.get("tracks", []):
                if t.get("id") == default_sub_id:
                    lang = self._lang_code((t.get("properties") or {}).get("language"))
                    has_eng_sub_default = (lang == "eng")
                    break

        return bool(audio_ok and has_eng_sub_default)

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

        # Identify Japanese audio track
        japanese_audio_id: Optional[int] = None
        for tid, t in audio_tracks:
            lang = self._lang_code((t.get("properties") or {}).get("language"))
            name = (t.get("properties") or {}).get("track_name")
            if lang == "jpn" or (name and re.search(r"jap|jpn|japanese", name, re.IGNORECASE)):
                japanese_audio_id = tid
                break

        # Identify English subtitle tracks
        english_subs: List[Tuple[int, Dict]] = []
        for tid, t in sub_tracks:
            lang = self._lang_code((t.get("properties") or {}).get("language"))
            if lang == "eng":
                english_subs.append((tid, t))

        english_full: List[Tuple[int, Dict]] = [
            (tid, t) for tid, t in english_subs if not self._is_signs_track((t.get("properties") or {}).get("track_name"))
        ]
        # Prioritize Full/Dialogue/SDH
        english_full_sorted = sorted(
            english_full,
            key=lambda x: 0
            if re.search(r"full|dialogue|sdh", ((x[1].get("properties") or {}).get("track_name") or ""), re.IGNORECASE)
            else 1,
        )

        english_full_id: Optional[int] = english_full_sorted[0][0] if english_full_sorted else None
        english_any_id: Optional[int] = english_subs[0][0] if english_subs else None
        any_sub_id: Optional[int] = sub_tracks[0][0] if sub_tracks else None

        # Decision logic per updated requirements:
        # - Always change audio default to Japanese when available
        # - Always enable a default subtitle track (prefer EN Full -> EN -> any)
        # - If only one audio track, don't change audio but still manage subtitles
        chosen_sub_idx = english_full_id or english_any_id or any_sub_id
        chosen_sub_lang = "eng" if (english_full_id or english_any_id) else None

        # If only one audio track, don't change audio but still manage subtitles
        if len(audio_tracks) == 1:
            return TrackSelection(
                audio_track_index=None,
                subtitle_track_index=chosen_sub_idx,
                should_change_audio=False,
                audio_language_code=None,
                subtitle_language_code=chosen_sub_lang,
            )

        if japanese_audio_id is not None:
            return TrackSelection(
                audio_track_index=japanese_audio_id,
                subtitle_track_index=chosen_sub_idx,
                should_change_audio=True,
                audio_language_code="jpn",
                subtitle_language_code=chosen_sub_lang,
            )

        # No Japanese audio: don't change audio, but still enforce subtitle default
        return TrackSelection(
            audio_track_index=None,
            subtitle_track_index=chosen_sub_idx,
            should_change_audio=False,
            audio_language_code=None,
            subtitle_language_code=chosen_sub_lang,
        )

    def apply_flags(self, file_path: str, inspect: Dict, selection: TrackSelection) -> None:
        commands: List[List[str]] = []

        # Reset defaults only for the types we will change
        if selection.should_change_audio:
            for t in inspect.get("tracks", []):
                if t.get("type") == "audio":
                    tid = t.get("id")
                    commands.append(["mkvpropedit", file_path, "--edit", f"track:{tid}", "--set", "flag-default=0", "--set", "flag-forced=0"])

        # We always manage subtitles default
        for t in inspect.get("tracks", []):
            if t.get("type") == "subtitles":
                tid = t.get("id")
                commands.append(["mkvpropedit", file_path, "--edit", f"track:{tid}", "--set", "flag-default=0", "--set", "flag-forced=0"])

        # Set audio default if requested
        if selection.should_change_audio and selection.audio_track_index is not None:
            cmd = ["mkvpropedit", file_path, "--edit", f"track:{selection.audio_track_index}", "--set", "flag-default=1"]
            if selection.audio_language_code:
                cmd += ["--set", f"language={selection.audio_language_code}"]
            commands.append(cmd)

        # Set subtitle default (always enable some subtitle track)
        if selection.subtitle_track_index is not None:
            cmd = ["mkvpropedit", file_path, "--edit", f"track:{selection.subtitle_track_index}", "--set", "flag-default=1"]
            # Leave forced=0 by default; can be made configurable later
            if selection.subtitle_language_code:
                cmd += ["--set", f"language={selection.subtitle_language_code}"]
            commands.append(cmd)

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


@dataclass
class FileReport:
    file_path: str
    series_title: str
    episode_title: str
    file_size: int
    is_seeded: bool
    was_modified: bool
    error_message: Optional[str]
    audio_tracks: List[Dict]
    subtitle_tracks: List[Dict]
    selected_audio_track: Optional[int]
    selected_subtitle_track: Optional[int]
    audio_language_code: Optional[str]
    subtitle_language_code: Optional[str]
    was_compliant: bool
    skip_reason: Optional[str]  # Why the file was skipped (if applicable)
    has_single_audio_track: bool  # Whether file has only one audio track


def generate_report(reports: List[FileReport], output_dir: Optional[str] = None) -> None:
    """Generate a detailed report of all files processed."""
    try:
        # Use default directory if none specified
        if output_dir is None:
            output_dir = "/report"
        
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)
        
        # Generate timestamp for filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = os.path.join(output_dir, f"anime_language_report_{timestamp}.json")
        
        # Convert reports to serializable format
        serializable_reports = []
        for report in reports:
            report_dict = asdict(report)
            # Ensure all values are JSON serializable
            if report_dict.get("error_message") is None:
                report_dict["error_message"] = ""
            serializable_reports.append(report_dict)
        
        # Analyze language codes for insights
        language_analysis = {
            "audio_languages": {},
            "subtitle_languages": {},
            "missing_japanese_audio": [],
            "missing_english_subs": [],
            "unusual_language_codes": [],
            "common_track_names": {},
            "potential_language_mismatches": [],
            "single_audio_track_files": [],
            "audio_track_count_distribution": {},
            "files_needing_attention": []
        }
        
        for report in reports:
            if not report.is_seeded and not report.error_message:
                # Analyze audio tracks
                for track in report.audio_tracks:
                    lang = (track.get("properties") or {}).get("language", "unknown")
                    if lang not in language_analysis["audio_languages"]:
                        language_analysis["audio_languages"][lang] = 0
                    language_analysis["audio_languages"][lang] += 1
                
                # Analyze subtitle tracks
                for track in report.subtitle_tracks:
                    lang = (track.get("properties") or {}).get("language", "unknown")
                    if lang not in language_analysis["subtitle_languages"]:
                        language_analysis["subtitle_languages"][lang] = 0
                    language_analysis["subtitle_languages"][lang] += 1
                
                # Check for missing Japanese audio
                has_jpn_audio = any(
                    (track.get("properties") or {}).get("language") in ["jpn", "ja", "japanese"] 
                    for track in report.audio_tracks
                )
                if not has_jpn_audio:
                    language_analysis["missing_japanese_audio"].append(report.file_path)
                
                # Check for missing English subs
                has_eng_subs = any(
                    (track.get("properties") or {}).get("language") in ["eng", "en", "english"] 
                    for track in report.subtitle_tracks
                )
                if not has_eng_subs:
                    language_analysis["missing_english_subs"].append(report.file_path)
                
                # Check for unusual language codes
                for track in report.audio_tracks + report.subtitle_tracks:
                    lang = (track.get("properties") or {}).get("language", "")
                    if lang and lang not in ["jpn", "ja", "japanese", "eng", "en", "english", "unknown"]:
                        language_analysis["unusual_language_codes"].append({
                            "file_path": report.file_path,
                            "track_type": track.get("type"),
                            "language": lang,
                            "track_id": track.get("id")
                        })
                
                # Track single audio track files
                if len(report.audio_tracks) == 1:
                    language_analysis["single_audio_track_files"].append({
                        "file_path": report.file_path,
                        "audio_language": (report.audio_tracks[0].get("properties") or {}).get("language", "unknown"),
                        "audio_track_name": (report.audio_tracks[0].get("properties") or {}).get("track_name", "")
                    })
                
                # Track audio track count distribution
                track_count = len(report.audio_tracks)
                if track_count not in language_analysis["audio_track_count_distribution"]:
                    language_analysis["audio_track_count_distribution"][track_count] = 0
                language_analysis["audio_track_count_distribution"][track_count] += 1
                
                # Check if file needs attention
                if report.audio_tracks and report.subtitle_tracks:
                    has_jpn_audio = any(
                        (track.get("properties") or {}).get("language") in ["jpn", "ja", "japanese"] 
                        for track in report.audio_tracks
                    )
                    has_eng_subs = any(
                        (track.get("properties") or {}).get("language") in ["eng", "en", "english"] 
                        for track in report.subtitle_tracks
                    )
                    if has_jpn_audio and not has_eng_subs:
                        language_analysis["files_needing_attention"].append({
                            "file_path": report.file_path,
                            "issue": "Has Japanese audio but no English subtitles",
                            "audio_languages": [(t.get("properties") or {}).get("language", "unknown") for t in report.audio_tracks],
                            "subtitle_languages": [(t.get("properties") or {}).get("language", "unknown") for t in report.subtitle_tracks]
                        })
                    elif not has_jpn_audio and not has_eng_subs:
                        language_analysis["files_needing_attention"].append({
                            "file_path": report.file_path,
                            "issue": "No Japanese audio and no English subtitles",
                            "audio_languages": [(t.get("properties") or {}).get("language", "unknown") for t in report.audio_tracks],
                            "subtitle_languages": [(t.get("properties") or {}).get("language", "unknown") for t in report.subtitle_tracks]
                        })
                
                # Collect track names for analysis
                for track in report.audio_tracks + report.subtitle_tracks:
                    name = (track.get("properties") or {}).get("track_name", "")
                    if name:
                        if name not in language_analysis["common_track_names"]:
                            language_analysis["common_track_names"][name] = {
                                "count": 0,
                                "files": [],
                                "track_types": set()
                            }
                        language_analysis["common_track_names"][name]["count"] += 1
                        if len(language_analysis["common_track_names"][name]["files"]) < 5:  # Keep first 5 files
                            language_analysis["common_track_names"][name]["files"].append(report.file_path)
                        language_analysis["common_track_names"][name]["track_types"].add(track.get("type"))
        
        # Convert sets to lists for JSON serialization
        for name_info in language_analysis["common_track_names"].values():
            name_info["track_types"] = list(name_info["track_types"])
        
        # Check for potential language code mismatches
        for report in reports:
            if not report.is_seeded and not report.error_message:
                for track in report.audio_tracks + report.subtitle_tracks:
                    lang = (track.get("properties") or {}).get("language", "")
                    name = (track.get("properties") or {}).get("track_name", "")
                    if lang and name:
                        # Check if track name suggests different language than language code
                        name_lower = name.lower()
                        if lang in ["jpn", "ja", "japanese"] and any(x in name_lower for x in ["eng", "english", "en"]):
                            language_analysis["potential_language_mismatches"].append({
                                "file_path": report.file_path,
                                "track_type": track.get("type"),
                                "language_code": lang,
                                "track_name": name,
                                "issue": "Name suggests English but code is Japanese"
                            })
                        elif lang in ["eng", "en", "english"] and any(x in name_lower for x in ["jpn", "japanese", "ja"]):
                            language_analysis["potential_language_mismatches"].append({
                                "file_path": report.file_path,
                                "track_type": track.get("type"),
                                "language_code": lang,
                                "track_name": name,
                                "issue": "Name suggests Japanese but code is English"
                            })
                        elif lang not in ["jpn", "ja", "japanese", "eng", "en", "english"] and any(x in name_lower for x in ["jpn", "japanese", "ja", "eng", "english", "en"]):
                            language_analysis["potential_language_mismatches"].append({
                                "file_path": report.file_path,
                                "track_type": track.get("type"),
                                "language_code": lang,
                                "track_name": name,
                                "issue": "Name suggests Japanese/English but code is different"
                            })
        
        # Write JSON report
        with open(report_file, 'w', encoding='utf-8') as f:
            json.dump({
                "generated_at": datetime.now().isoformat(),
                "total_files": len(reports),
                "files_modified": len([r for r in reports if r.was_modified]),
                "files_skipped_seeding": len([r for r in reports if r.is_seeded]),
                "files_with_errors": len([r for r in reports if r.error_message]),
                "files_already_compliant": len([r for r in reports if r.was_compliant]),
                "language_analysis": language_analysis,
                "reports": serializable_reports
            }, f, indent=2, ensure_ascii=False)
        
        # Generate summary text report
        summary_file = os.path.join(output_dir, f"anime_language_summary_{timestamp}.txt")
        with open(summary_file, 'w', encoding='utf-8') as f:
            f.write(f"Anime Language Processing Report\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'='*50}\n\n")
            
            f.write(f"Summary:\n")
            f.write(f"  Total files processed: {len(reports)}\n")
            f.write(f"  Files modified: {len([r for r in reports if r.was_modified])}\n")
            f.write(f"  Files skipped (seeding): {len([r for r in reports if r.is_seeded])}\n")
            f.write(f"  Files with errors: {len([r for r in reports if r.error_message])}\n")
            f.write(f"  Files already compliant: {len([r for r in reports if r.was_compliant])}\n")
            f.write(f"  Files with single audio track: {len([r for r in reports if r.has_single_audio_track])}\n\n")
            
            # Group files by skip reason
            skip_reasons = {}
            for report in reports:
                if report.skip_reason and not report.was_modified:
                    reason = report.skip_reason
                    if reason not in skip_reasons:
                        skip_reasons[reason] = []
                    skip_reasons[reason].append(report.file_path)
            
            if skip_reasons:
                f.write(f"Files Skipped by Reason:\n")
                f.write(f"{'='*30}\n")
                for reason, files in skip_reasons.items():
                    f.write(f"  {reason}: {len(files)} files\n")
                    for file_path in files[:5]:  # Show first 5 files
                        f.write(f"    - {file_path}\n")
                    if len(files) > 5:
                        f.write(f"    ... and {len(files) - 5} more\n")
                    f.write("\n")
            
            # Add language analysis summary
            non_seeded_reports = [r for r in reports if not r.is_seeded and not r.error_message]
            if non_seeded_reports:
                f.write(f"Language Analysis:\n")
                f.write(f"{'='*20}\n")
                
                # Audio languages
                audio_langs = {}
                for report in non_seeded_reports:
                    for track in report.audio_tracks:
                        lang = (track.get("properties") or {}).get("language", "unknown")
                        audio_langs[lang] = audio_langs.get(lang, 0) + 1
                
                if audio_langs:
                    f.write(f"  Audio Languages Found:\n")
                    for lang, count in sorted(audio_langs.items(), key=lambda x: x[1], reverse=True):
                        f.write(f"    {lang}: {count} tracks\n")
                
                # Audio track count distribution
                audio_track_counts = {}
                for report in non_seeded_reports:
                    count = len(report.audio_tracks)
                    audio_track_counts[count] = audio_track_counts.get(count, 0) + 1
                
                if audio_track_counts:
                    f.write(f"  Audio Track Count Distribution:\n")
                    for count in sorted(audio_track_counts.keys()):
                        f.write(f"    {count} track(s): {audio_track_counts[count]} files\n")
                
                # Subtitle languages
                sub_langs = {}
                for report in non_seeded_reports:
                    for track in report.subtitle_tracks:
                        lang = (track.get("properties") or {}).get("language", "unknown")
                        sub_langs[lang] = sub_langs.get(lang, 0) + 1
                
                if sub_langs:
                    f.write(f"  Subtitle Languages Found:\n")
                    for lang, count in sorted(sub_langs.items(), key=lambda x: x[1], reverse=True):
                        f.write(f"    {lang}: {count} tracks\n")
                
                # Missing languages
                missing_jpn = [r.file_path for r in non_seeded_reports if not any(
                    (track.get("properties") or {}).get("language") in ["jpn", "ja", "japanese"] 
                    for track in r.audio_tracks
                )]
                if missing_jpn:
                    f.write(f"  Files Missing Japanese Audio: {len(missing_jpn)}\n")
                    for file_path in missing_jpn[:3]:
                        f.write(f"    - {file_path}\n")
                    if len(missing_jpn) > 3:
                        f.write(f"    ... and {len(missing_jpn) - 3} more\n")
                
                missing_eng = [r.file_path for r in non_seeded_reports if not any(
                    (track.get("properties") or {}).get("language") in ["eng", "en", "english"] 
                    for track in r.subtitle_tracks
                )]
                if missing_eng:
                    f.write(f"  Files Missing English Subtitles: {len(missing_eng)}\n")
                    for file_path in missing_eng[:3]:
                        f.write(f"    - {file_path}\n")
                    if len(missing_eng) > 3:
                        f.write(f"    ... and {len(missing_eng) - 3} more\n")
                
                # Show files with unusual language codes that might need attention
                unusual_langs = []
                for report in non_seeded_reports:
                    for track in report.audio_tracks + report.subtitle_tracks:
                        lang = (track.get("properties") or {}).get("language", "")
                        if lang and lang not in ["jpn", "ja", "japanese", "eng", "en", "english", "unknown"]:
                            unusual_langs.append((report.file_path, track.get("type"), lang))
                
                if unusual_langs:
                    f.write(f"  Files with Unusual Language Codes (may need attention):\n")
                    # Group by language code
                    lang_groups = {}
                    for file_path, track_type, lang in unusual_langs:
                        if lang not in lang_groups:
                            lang_groups[lang] = []
                        lang_groups[lang].append((file_path, track_type))
                    
                    for lang, entries in lang_groups.items():
                        f.write(f"    {lang}: {len(entries)} tracks\n")
                        for file_path, track_type in entries[:3]:
                            f.write(f"      - {file_path} ({track_type})\n")
                        if len(entries) > 3:
                            f.write(f"      ... and {len(entries) - 3} more\n")
                
                # Show common track names that might indicate language code issues
                track_names = {}
                for report in non_seeded_reports:
                    for track in report.audio_tracks + report.subtitle_tracks:
                        name = (track.get("properties") or {}).get("track_name", "")
                        if name:
                            if name not in track_names:
                                track_names[name] = {"count": 0, "files": []}
                            track_names[name]["count"] += 1
                            if len(track_names[name]["files"]) < 3:  # Keep first 3 files
                                track_names[name]["files"].append(report.file_path)
                
                if track_names:
                    f.write(f"  Common Track Names (may indicate language code issues):\n")
                    # Sort by frequency
                    sorted_names = sorted(track_names.items(), key=lambda x: x[1]["count"], reverse=True)
                    for name, info in sorted_names[:10]:  # Show top 10
                        f.write(f"    '{name}': {info['count']} occurrences\n")
                        for file_path in info["files"]:
                            f.write(f"      - {file_path}\n")
                
                f.write("\n")
            
            # Check for potential language code mismatches
            potential_mismatches = []
            for report in non_seeded_reports:
                for track in report.audio_tracks + report.subtitle_tracks:
                    lang = (track.get("properties") or {}).get("language", "")
                    name = (track.get("properties") or {}).get("track_name", "")
                    if lang and name:
                        # Check if track name suggests different language than language code
                        name_lower = name.lower()
                        if lang in ["jpn", "ja", "japanese"] and any(x in name_lower for x in ["eng", "english", "en"]):
                            potential_mismatches.append((report.file_path, track.get("type"), lang, name, "Name suggests English but code is Japanese"))
                        elif lang in ["eng", "en", "english"] and any(x in name_lower for x in ["jpn", "japanese", "ja"]):
                            potential_mismatches.append((report.file_path, track.get("type"), lang, name, "Name suggests Japanese but code is English"))
                        elif lang not in ["jpn", "ja", "japanese", "eng", "en", "english"] and any(x in name_lower for x in ["jpn", "japanese", "ja", "eng", "english", "en"]):
                            potential_mismatches.append((report.file_path, track.get("type"), lang, name, "Name suggests Japanese/English but code is different"))
            
            if potential_mismatches:
                f.write(f"Potential Language Code Mismatches:\n")
                f.write(f"{'='*35}\n")
                for file_path, track_type, lang, name, reason in potential_mismatches[:10]:  # Show first 10
                    f.write(f"  {file_path}\n")
                    f.write(f"    Track: {track_type}\n")
                    f.write(f"    Language Code: {lang}\n")
                    f.write(f"    Track Name: '{name}'\n")
                    f.write(f"    Issue: {reason}\n\n")
                if len(potential_mismatches) > 10:
                    f.write(f"  ... and {len(potential_mismatches) - 10} more potential mismatches\n\n")
            
            # Show files with potential language code mismatches (from JSON analysis)
            if language_analysis.get("potential_language_mismatches"):
                f.write(f"Language Code Mismatches (from detailed analysis):\n")
                f.write(f"{'='*40}\n")
                f.write(f"  Total: {len(language_analysis['potential_language_mismatches'])} potential mismatches\n\n")
                # Show first few examples
                for mismatch in language_analysis["potential_language_mismatches"][:10]:
                    f.write(f"  - {mismatch['file_path']}\n")
                    f.write(f"    Track: {mismatch['track_type']}\n")
                    f.write(f"    Language Code: {mismatch['language_code']}\n")
                    f.write(f"    Track Name: '{mismatch['track_name']}'\n")
                    f.write(f"    Issue: {mismatch['issue']}\n\n")
                if len(language_analysis["potential_language_mismatches"]) > 10:
                    f.write(f"  ... and {len(language_analysis['potential_language_mismatches']) - 10} more potential mismatches\n\n")
            
            # Show files that might need manual attention
            files_needing_attention = []
            for report in reports:
                if not report.is_seeded and not report.error_message and not report.was_compliant:
                    # Files that weren't modified but should have been
                    if report.audio_tracks and report.subtitle_tracks:
                        # Check if there are Japanese audio tracks but no English subs
                        has_jpn_audio = any(
                            (track.get("properties") or {}).get("language") in ["jpn", "ja", "japanese"] 
                            for track in report.audio_tracks
                        )
                        has_eng_subs = any(
                            (track.get("properties") or {}).get("language") in ["eng", "en", "english"] 
                            for track in report.subtitle_tracks
                        )
                        if has_jpn_audio and not has_eng_subs:
                            files_needing_attention.append((report.file_path, "Has Japanese audio but no English subtitles"))
                        elif not has_jpn_audio and not has_eng_subs:
                            files_needing_attention.append((report.file_path, "No Japanese audio and no English subtitles"))
            
            if files_needing_attention:
                f.write(f"Files That May Need Manual Attention:\n")
                f.write(f"{'='*35}\n")
                f.write(f"  Total: {len(files_needing_attention)} files\n\n")
                for file_path, reason in files_needing_attention[:10]:
                    f.write(f"  - {file_path}\n")
                    f.write(f"    Issue: {reason}\n")
                if len(files_needing_attention) > 10:
                    f.write(f"  ... and {len(files_needing_attention) - 10} more\n")
                f.write("\n")
            
            # Show single audio track files
            single_audio_files = [r for r in reports if r.has_single_audio_track and not r.is_seeded and not r.error_message]
            if single_audio_files:
                f.write(f"Files with Single Audio Track:\n")
                f.write(f"{'='*30}\n")
                f.write(f"  Total: {len(single_audio_files)} files\n")
                f.write(f"  These files are treated as 'audio OK' regardless of language\n\n")
                # Show first few examples
                for report in single_audio_files[:5]:
                    f.write(f"  - {report.file_path}\n")
                    if report.audio_tracks:
                        track = report.audio_tracks[0]
                        props = track.get("properties", {})
                        lang = props.get("language", "unknown")
                        name = props.get("track_name", "")
                        f.write(f"    Audio: {lang} {name}\n")
                if len(single_audio_files) > 5:
                    f.write(f"  ... and {len(single_audio_files) - 5} more\n")
                f.write("\n")
            
            # Show most common issues
            issue_counts = {}
            for report in reports:
                if report.skip_reason and not report.was_modified:
                    reason = report.skip_reason
                    issue_counts[reason] = issue_counts.get(reason, 0) + 1
            
            if issue_counts:
                f.write(f"Most Common Issues:\n")
                f.write(f"{'='*20}\n")
                for reason, count in sorted(issue_counts.items(), key=lambda x: x[1], reverse=True):
                    f.write(f"  {reason}: {count} files\n")
                f.write("\n")
            
            f.write(f"Detailed File Information:\n")
            f.write(f"{'='*50}\n\n")
            
            for i, report in enumerate(reports, 1):
                f.write(f"File {i}: {report.file_path}\n")
                f.write(f"  Series: {report.series_title}\n")
                f.write(f"  Episode: {report.episode_title}\n")
                f.write(f"  Size: {report.file_size:,} bytes\n")
                f.write(f"  Status: {'Seeded' if report.is_seeded else 'Not Seeded'}")
                if report.was_modified:
                    f.write(" | Modified")
                if report.was_compliant:
                    f.write(" | Already Compliant")
                if report.error_message:
                    f.write(f" | Error: {report.error_message}")
                if report.skip_reason and not report.was_modified:
                    f.write(f" | Skipped: {report.skip_reason}")
                f.write("\n")
                
                f.write(f"  Audio Tracks ({len(report.audio_tracks)}):\n")
                for track in report.audio_tracks:
                    props = track.get("properties", {})
                    lang = props.get("language", "unknown")
                    name = props.get("track_name", "")
                    default = " (default)" if props.get("default_track") else ""
                    f.write(f"    Track {track.get('id')}: {lang} {name}{default}\n")
                
                f.write(f"  Subtitle Tracks ({len(report.subtitle_tracks)}):\n")
                for track in report.subtitle_tracks:
                    props = track.get("properties", {})
                    lang = props.get("language", "unknown")
                    name = props.get("track_name", "")
                    default = " (default)" if props.get("default_track") else ""
                    f.write(f"    Track {track.get('id')}: {lang} {name}{default}\n")
                
                if report.selected_audio_track is not None:
                    f.write(f"  Selected Audio: Track {report.selected_audio_track} ({report.audio_language_code})\n")
                if report.selected_subtitle_track is not None:
                    f.write(f"  Selected Subtitle: Track {report.selected_subtitle_track} ({report.subtitle_language_code})\n")
                
                f.write("\n")
        
        logging.info(f"Report generated: {report_file}")
        logging.info(f"Summary generated: {summary_file}")
        
    except Exception as e:
        logging.error(f"Failed to generate report: {e}")


def process_once() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    sonarr_url = os.getenv("SONARR_URL", "http://sonarr:8989")
    sonarr_api_key = os.getenv("SONARR_API_KEY")
    if not sonarr_api_key:
        logging.error("SONARR_API_KEY is required")
        sys.exit(2)

    exclude_seeding = get_env_bool("EXCLUDE_SEEDING", True)
    dry_run = get_env_bool("DRY_RUN", False)
    generate_reports = get_env_bool("GENERATE_REPORTS", True)
    report_directory = os.getenv("REPORT_DIRECTORY", "/report")

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
    
    # Collect reports for all files
    file_reports: List[FileReport] = []

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
            
            # Check if file is seeded
            is_seeded_status = exclude_seeding and is_seeded(path, seeded_paths, seeded_name_sizes, size_bytes=size)
            if is_seeded_status:
                files_skipped_seed += 1
                logging.info("Skipping (seeding): %s", path)
                
                # Still create a report for seeded files
                file_reports.append(FileReport(
                    file_path=path,
                    series_title=title,
                    episode_title=ep.get("title", "Unknown"),
                    file_size=size or 0,
                    is_seeded=True,
                    was_modified=False,
                    error_message=None,
                    audio_tracks=[],
                    subtitle_tracks=[],
                    selected_audio_track=None,
                    selected_subtitle_track=None,
                    audio_language_code=None,
                    subtitle_language_code=None,
                    was_compliant=False,
                    skip_reason="File is currently seeding",
                    has_single_audio_track=False
                ))
                continue

            try:
                inspect = mkv.identify_tracks(effective_path)
                
                # Extract track information for report
                audio_tracks = [t for t in inspect.get("tracks", []) if t.get("type") == "audio"]
                subtitle_tracks = [t for t in inspect.get("tracks", []) if t.get("type") == "subtitles"]
                
                # Check if already compliant
                was_compliant = mkv.is_file_compliant(inspect)
                
                if was_compliant:
                    # File is already compliant, still create report
                    file_reports.append(FileReport(
                        file_path=path,
                        series_title=title,
                        episode_title=ep.get("title", "Unknown"),
                        file_size=size or 0,
                        is_seeded=False,
                        was_modified=False,
                        error_message=None,
                        audio_tracks=audio_tracks,
                        subtitle_tracks=subtitle_tracks,
                        selected_audio_track=None,
                        selected_subtitle_track=None,
                        audio_language_code=None,
                        subtitle_language_code=None,
                        was_compliant=True,
                        skip_reason="File already compliant (audio OK + English subtitles as default)",
                        has_single_audio_track=len(audio_tracks) == 1
                    ))
                    continue

                selection = mkv.choose_tracks(inspect)
                if selection.audio_track_index is None and selection.subtitle_track_index is None:
                    # No changes needed or possible, still create report
                    file_reports.append(FileReport(
                        file_path=path,
                        series_title=title,
                        episode_title=ep.get("title", "Unknown"),
                        file_size=size or 0,
                        is_seeded=False,
                        was_modified=False,
                        error_message=None,
                        audio_tracks=audio_tracks,
                        subtitle_tracks=subtitle_tracks,
                        selected_audio_track=None,
                        selected_subtitle_track=None,
                        audio_language_code=None,
                        subtitle_language_code=None,
                        was_compliant=False,
                        skip_reason="No suitable tracks found for modification",
                        has_single_audio_track=len(audio_tracks) == 1
                    ))
                    continue
                
                # Apply changes
                mkv.apply_flags(effective_path, inspect, selection)
                files_modified += 1
                
                # Create report for modified file
                file_reports.append(FileReport(
                    file_path=path,
                    series_title=title,
                    episode_title=ep.get("title", "Unknown"),
                    file_size=size or 0,
                    is_seeded=False,
                    was_modified=True,
                    error_message=None,
                    audio_tracks=audio_tracks,
                    subtitle_tracks=subtitle_tracks,
                    selected_audio_track=selection.audio_track_index,
                    selected_subtitle_track=selection.subtitle_track_index,
                    audio_language_code=selection.audio_language_code,
                    subtitle_language_code=selection.subtitle_language_code,
                    was_compliant=False,
                    skip_reason=None,
                    has_single_audio_track=len(audio_tracks) == 1
                ))
                
                if selection.should_change_audio:
                    logging.info("Updated: set default audio to Japanese and ensured default subtitles (%s): %s", selection.subtitle_language_code or "auto", effective_path)
                else:
                    logging.info("Updated: ensured default subtitles (%s) when no Japanese audio present: %s", selection.subtitle_language_code or "auto", effective_path)
                    
            except subprocess.CalledProcessError as e:
                errors += 1
                logging.warning("Command failed for %s: %s", effective_path, e)
                
                # Create report for file with error
                file_reports.append(FileReport(
                    file_path=path,
                    series_title=title,
                    episode_title=ep.get("title", "Unknown"),
                    file_size=size or 0,
                    is_seeded=False,
                    was_modified=False,
                    error_message=f"Command failed: {e}",
                    audio_tracks=[],
                    subtitle_tracks=[],
                    selected_audio_track=None,
                    selected_subtitle_track=None,
                    audio_language_code=None,
                    subtitle_language_code=None,
                    was_compliant=False,
                    skip_reason="Command execution failed",
                    has_single_audio_track=False
                ))
                
            except Exception as e:
                errors += 1
                logging.warning("Failed processing %s: %s", effective_path, e)
                
                # Create report for file with error
                file_reports.append(FileReport(
                    file_path=path,
                    series_title=title,
                    episode_title=ep.get("title", "Unknown"),
                    file_size=size or 0,
                    is_seeded=False,
                    was_modified=False,
                    error_message=f"Processing failed: {e}",
                    audio_tracks=[],
                    subtitle_tracks=[],
                    selected_audio_track=None,
                    selected_subtitle_track=None,
                    audio_language_code=None,
                    subtitle_language_code=None,
                    was_compliant=False,
                    skip_reason="General processing error",
                    has_single_audio_track=False
                ))

    # Generate report if enabled
    if generate_reports:
        logging.info("Generating detailed report...")
        generate_report(file_reports, report_directory)
        logging.info("Report generation completed")

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


