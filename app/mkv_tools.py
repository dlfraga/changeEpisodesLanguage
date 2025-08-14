import json
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from .models import TrackSelection


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
            print(f"Running: {' '.join(cmd)}")
            if self.dry_run:
                continue
            subprocess.run(cmd, check=True)
