import os
import sys
import time
from typing import List

from .clients import SonarrClient, TransmissionClient
from .mkv_tools import MkvTool
from .models import FileReport
from .reporting import generate_report
from .utils import (
    build_seeded_path_index,
    get_env_bool,
    get_env_int,
    is_seeded,
    normalize_path,
)


def process_once() -> None:
    sonarr_url = os.getenv("SONARR_URL", "http://sonarr:8989")
    sonarr_api_key = os.getenv("SONARR_API_KEY")
    if not sonarr_api_key:
        print("SONARR_API_KEY is required")
        sys.exit(2)

    exclude_seeding = get_env_bool("EXCLUDE_SEEDING", True)
    dry_run = get_env_bool("DRY_RUN", False)
    generate_reports = get_env_bool("GENERATE_REPORTS", True)
    report_directory = os.getenv("REPORT_DIRECTORY", "/report")

    transmission_client: TransmissionClient = None
    if exclude_seeding:
        trans_url = os.getenv("TRANSMISSION_RPC_URL")
        trans_user = os.getenv("TRANSMISSION_USER")
        trans_pass = os.getenv("TRANSMISSION_PASSWORD")
        if trans_url:
            transmission_client = TransmissionClient(trans_url, username=trans_user, password=trans_pass)
        else:
            print("EXCLUDE_SEEDING is true but TRANSMISSION_RPC_URL not set; proceeding without seeding exclusion")

    sonarr = SonarrClient(sonarr_url, sonarr_api_key)
    mkv = MkvTool(dry_run=dry_run)

    # Build seeded paths index (if applicable)
    seeded_paths, seeded_name_sizes = build_seeded_path_index(transmission_client)

    # Find anime series
    series = sonarr.get_series()
    anime_series = [s for s in series if (s.get("seriesType") == "anime")]
    print(f"Found {len(anime_series)} anime series")

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
            print(f"Failed fetching episodes for series {title} ({sid}): {e}")
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
                print(f"Skipping (seeding): {path}")
                
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
                    print(f"Updated: set default audio to Japanese and ensured default subtitles ({selection.subtitle_language_code or 'auto'}): {effective_path}")
                else:
                    print(f"Updated: ensured default subtitles ({selection.subtitle_language_code or 'auto'}) when no Japanese audio present: {effective_path}")
                    
            except Exception as e:
                errors += 1
                print(f"Failed processing {effective_path}: {e}")
                
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
        print("Generating detailed report...")
        generate_report(file_reports, report_directory)
        print("Report generation completed")

    print(
        f"Done. Considered={files_considered}, Modified={files_modified}, SkippedSeeding={files_skipped_seed}, Errors={errors}"
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


