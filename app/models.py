from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class TrackSelection:
    audio_track_index: Optional[int]
    subtitle_track_index: Optional[int]
    should_change_audio: bool
    audio_language_code: Optional[str]
    subtitle_language_code: Optional[str]


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
