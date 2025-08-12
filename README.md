### changeEpisodesLanguage

Small service that scans Sonarr anime episode files and sets default tracks, prioritizing Japanese audio and English full subtitles. Optionally skips files that are still seeding in Transmission.

### How it works

- Calls Sonarr `/api/v3/series` to find series with `seriesType = "anime"`.
- Calls Sonarr `/api/v3/episode?seriesId=...&includeEpisodeFile=true` to list episodes and file paths.
- Optionally queries Transmission RPC for torrents in seed-wait/seeding and excludes their files.
- For each `.mkv` file, inspects tracks with `mkvmerge -J` and sets flags via `mkvpropedit`:
  - Default audio: Japanese when available (for every episode that has it)
  - Default subs: always enabled; prefer English full (avoids "signs/songs" where possible), else English, else any

### Configuration (env vars)

- `SONARR_URL` (required): e.g. `http://sonarr:8989`
- `SONARR_API_KEY` (required)
- `EXCLUDE_SEEDING` (default: `true`)
- `TRANSMISSION_RPC_URL`: e.g. `http://transmission:9091/transmission/rpc`
- `TRANSMISSION_USER`, `TRANSMISSION_PASSWORD` (optional)
- `PATH_MAP_FROM`, `PATH_MAP_TO` (optional): remap Sonarr file paths to match Transmission container paths
- `POLL_INTERVAL_HOURS` (default: `24`)
- `RUN_ONCE` (default: `false`)
- `DRY_RUN` (default: `false`)

### Docker

Build:

```bash
docker build -t ghcr.io/dlfraga/changeepisodeslanguage:dev .
```

Run (compose example):

```yaml
services:
  change-episodes-language:
    image: ghcr.io/dlfraga/changeepisodeslanguage:latest
    environment:
      - SONARR_URL=http://sonarr:8989
      - SONARR_API_KEY=YOUR_KEY
      - EXCLUDE_SEEDING=true
      - TRANSMISSION_RPC_URL=http://transmission:9091/transmission/rpc
      - TRANSMISSION_USER=transmission
      - TRANSMISSION_PASSWORD=secret
      - PATH_MAP_FROM=/data/sonarr
      - PATH_MAP_TO=/downloads
      - POLL_INTERVAL_HOURS=24
    volumes:
      - /path/to/media:/path/to/media:rw
```

Make sure the container can access your media paths and has `mkvtoolnix` available (installed in the image).

### Notes

- Only `.mkv` files are changed.
- Transmission seeding detection uses status codes 5 (seed-wait) and 6 (seeding).
- Audio default is only reset when switching to Japanese; subtitles defaults are always normalized so one is active.
- Logs to stdout; suitable for Unraid Docker templates.


