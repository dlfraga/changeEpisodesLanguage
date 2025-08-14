import json
import os
from dataclasses import asdict
from datetime import datetime
from typing import Dict, List, Optional

from .models import FileReport


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
        
        print(f"Report generated: {report_file}")
        print(f"Summary generated: {summary_file}")
        
    except Exception as e:
        print(f"Failed to generate report: {e}")
