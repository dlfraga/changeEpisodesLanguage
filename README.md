# Anime Language Changer

A Python script that automatically changes the default audio and subtitle tracks in anime MKV files to ensure Japanese audio and English subtitles are set as default.

## Features

- Automatically detects anime series in Sonarr
- Changes default audio track to Japanese when available
- Ensures English subtitles are set as default
- Excludes files that are currently seeding in Transmission
- Generates detailed reports of all processed files
- Supports dry-run mode for testing

## Configuration

### Environment Variables

- `SONARR_URL`: Sonarr base URL (default: http://sonarr:8989)
- `SONARR_API_KEY`: Sonarr API key (required)
- `EXCLUDE_SEEDING`: Whether to exclude seeding files (default: true)
- `TRANSMISSION_RPC_URL`: Transmission RPC URL for seeding detection
- `TRANSMISSION_USER`: Transmission username (optional)
- `TRANSMISSION_PASSWORD`: Transmission password (optional)
- `DRY_RUN`: Run without making changes (default: false)
- `POLL_INTERVAL_HOURS`: How often to check for new files (default: 24)
- `RUN_ONCE`: Run once and exit (default: false)
- `GENERATE_REPORTS`: Generate detailed reports (default: true)
- `REPORT_DIRECTORY`: Directory to save reports (default: /report)

### Path Mapping

For container environments where paths may differ:

- `PATH_MAP_FROM`: Source path prefix to map from
- `PATH_MAP_TO`: Target path prefix to map to
- `FILE_PATH_MAP_FROM`: File path mapping source
- `FILE_PATH_MAP_TO`: File path mapping target

## Reporting

The script generates comprehensive reports when `GENERATE_REPORTS=true` (default). Reports are saved to the configured `REPORT_DIRECTORY` (default: `/report`).

### Report Types

1. **JSON Report** (`anime_language_report_YYYYMMDD_HHMMSS.json`): Machine-readable detailed report
2. **Text Summary** (`anime_language_summary_YYYYMMDD_HHMMSS.txt`): Human-readable summary

### Report Contents

- **Summary Statistics**: Total files, modified, skipped, errors, compliant
- **Language Analysis**: Audio/subtitle language distribution, missing languages
- **Skip Reasons**: Why files were not modified (seeding, already compliant, etc.)
- **Potential Issues**: Language code mismatches, unusual codes, common track names
- **Detailed File Info**: Individual file analysis with track details

### Language Analysis Features

- **Missing Japanese Audio**: Files without Japanese audio tracks
- **Missing English Subtitles**: Files without English subtitle tracks  
- **Unusual Language Codes**: Non-standard language codes that may need attention
- **Track Name Analysis**: Common track names that might indicate language issues
- **Language Mismatches**: Cases where track names suggest different languages than codes

### Example Report Structure

```json
{
  "generated_at": "2024-01-15T10:30:00",
  "total_files": 150,
  "files_modified": 45,
  "files_skipped_seeding": 30,
  "files_with_errors": 2,
  "files_already_compliant": 73,
  "language_analysis": {
    "audio_languages": {"jpn": 120, "eng": 30},
    "subtitle_languages": {"eng": 140, "jpn": 10},
    "missing_japanese_audio": ["file1.mkv", "file2.mkv"],
    "missing_english_subs": ["file3.mkv"],
    "unusual_language_codes": [...],
    "common_track_names": {...},
    "potential_language_mismatches": [...]
  },
  "reports": [...]
}
```

## Usage

### Docker

```bash
docker run -d \
  -e SONARR_API_KEY=your_api_key \
  -e SONARR_URL=http://sonarr:8989 \
  -v /path/to/anime:/anime \
  anime-language-changer
```

### Direct Execution

```bash
python app/main.py
```

## Requirements

- Python 3.7+
- mkvmerge (mkvtoolnix)
- mkvpropedit (mkvtoolnix)
- requests library

## Troubleshooting

### Common Issues

1. **Files Not Modified**: Check the report for skip reasons
2. **Language Code Mismatches**: Review the "Potential Language Code Mismatches" section
3. **Missing Tracks**: Check "Missing Japanese Audio" and "Missing English Subtitles" sections
4. **Unusual Language Codes**: Review files with non-standard language codes

### Report Analysis

The reports help identify:
- Which files were processed and why
- Language code inconsistencies
- Missing audio/subtitle tracks
- Common patterns in track naming
- Files that may need manual attention

Use the JSON report for programmatic analysis and the text summary for quick human review.


