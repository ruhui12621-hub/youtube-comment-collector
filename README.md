# YouTube Comment Collector

A GitHub Actions-based collector for public YouTube videos and comment threads. It uses [ytscrape](https://github.com/vsmutok/ytscrape), which queries YouTube's public-facing InnerTube endpoints and does not require a Google API key.

## What it does

1. Searches YouTube videos using the phrases in `search_queries.txt`.
2. Also collects any video IDs or URLs listed in `video_urls.txt`.
3. Reads comments in **Newest first** order and includes replies.
4. Consumes the comment iterator without a configured result cap, so ytscrape can paginate through all comment pages YouTube makes available.
5. Saves the raw results as JSON under `data/comments/`, and maintains a video index and collection report.
6. Builds a normalized analysis index after each YouTube or Lenovo forum collection, with rolling 7-day/30-day views and monthly partitions.
7. Runs once a day and can also be started manually from the Actions tab.

The initial search phrases focus on Lenovo Legion Pro OLED/HDR brightness discussions. Edit `search_queries.txt` to change the topics.

## Start a collection manually

1. Open the repository's **Actions** tab.
2. Select **Collect YouTube comments**.
3. Click **Run workflow** and confirm.
4. Open the run to see its logs. Successful results are committed under `data/`.

The scheduled run is daily at 03:17 UTC.

## Add specific videos

Add one YouTube URL or 11-character video ID per line to `video_urls.txt`. Commit the change, then run the workflow manually or wait for the next scheduled run.

## Data layout

- `data/videos.json` — videos found by search and their search phrases.
- `data/comments/VIDEO_ID.json` — video metadata and archived comments/replies.
- `data/collection_report.json` — last YouTube run summary and any errors.
- `data/lenovo_forum/thread_*.json` — raw Lenovo Community topics and posts.
- `data/analysis/index.json` — index metadata, counts, time-quality notes, and pointers to the query files.
- `data/analysis/recent_7d.json` / `recent_30d.json` — rolling last 7/30 days, excluding 27Q-10 and 27QD-10-only records.
- `data/analysis/recent_7d_context.json` / `recent_30d_context.json` — lower-confidence adjacent context, kept separate from direct findings.
- `data/analysis/recent_7d_all.json` / `recent_30d_all.json` — same windows without relevance exclusions.
- `data/analysis/monthly/YYYY-MM.json` — date-partitioned high/medium relevance records by publication month.
- `data/analysis/undated.json` — records without a reliable publication timestamp.
- `data/analysis/records.jsonl` — complete normalized archive, one record per line.

Each normalized record carries its platform, source URL/ID, text, publication timestamp and timestamp quality, first/last-seen metadata where available, and a relevance label (high/medium/low/excluded). Lenovo forum timestamps are normalized from Unix time. YouTube relative labels are explicitly marked as estimates; records without a reliable date are kept in `undated.json` and are not silently counted as recent.

## Important limitations

- This is a public-data collector, not an official YouTube Data API integration. YouTube may throttle requests, change its internal responses, disable comments, or make some comments unavailable. A failed video is recorded in the collection report; it does not mean that video has no comments.
- "Newest first" is the most complete available sort mode, but no scraper can guarantee access to every comment YouTube itself withholds.
- Rolling date windows use publication time, not the date a video was first discovered. Relative YouTube labels such as "3 days ago" are estimates; legacy records with no reliable date remain in the undated file.
- 27Q-10 and 27QD-10-only material is excluded from the default recent analysis files. Mixed records that also mention a target UD model remain available for manual review.
- The workflow re-reads discovered videos to keep the archive refreshed. Large channels/videos can take time.
- The repository is public, so collected public comments and video metadata stored here are also publicly accessible. Do not add private information or credentials.
- Respect YouTube's Terms of Service and applicable laws. The collector is intended for modest research of public discussions, not bulk profiling or redistribution.

## Cost

The workflow runs on GitHub-hosted runners. GitHub provides free standard-runner usage for public repositories; YouTube access itself does not require a paid API key. GitHub or platform policies may change.
