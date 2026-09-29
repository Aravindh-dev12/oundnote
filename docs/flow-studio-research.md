# Oundnote Flow Studio research

Updated: 2026-09-29

## Interaction model

Wispr Flow's current product model is intentionally low-friction: activate capture, speak naturally, clean filler words and self-corrections, then place usable text into the active app. Its product is available across desktop and mobile. Wispr's Notetaker adds meeting recording, live transcript, speaker labels, and generated notes.

Oundnote borrows those interaction principles without copying Wispr branding or assets: one clear capture action, source choices beside it, a persistent live state, transcript-first review, a side-by-side AI agent, and history as personal memory.

## Mapping

| Principle | Oundnote |
| --- | --- |
| One-step capture | New / Flow Studio capture workspace |
| Live transcript | Existing native capture + live ASR + transcript API |
| Living agent | Existing Live AI Assistant in continuous mode |
| Meeting memory | Existing meetings, summaries, speakers and RAG |
| YouTube memory | Caption-first ingestion with yt-dlp audio fallback |
| Full editing | Existing editor retained at /legacy/meetings/{id} |
| Responsive shell | Global Flow theme + dedicated studio stylesheet |

## YouTube ingestion

The connector validates canonical YouTube URLs, checks metadata, prefers captions through youtube-transcript-api, stores caption cues as normal Oundnote segments, queues AI notes, and indexes the capture. With no usable captions, it falls back to yt-dlp audio and the existing import/transcription pipeline. The downloader disables playlists and enforces four hours / 750 MiB.

The optional package is installed with `pip install -e '.[youtube]'`. The extra uses yt-dlp's `default` dependency group so the companion EJS scripts are installed with the matching yt-dlp release. Current yt-dlp guidance also requires a supported JavaScript runtime for full YouTube solving; the documented Node floor is 22.

## Platform boundary

Oundnote's live capture is a local desktop-runtime capability. The browser UI does not attempt to capture system audio from another host. Full system-wide dictation inside arbitrary third-party iOS/Android apps requires native OS clients and permissions; this repository provides the local desktop capture path plus the responsive web control surface.

## References

- Wispr Flow features: https://wisprflow.ai/features
- Wispr Flow docs: https://docs.wisprflow.ai/articles/2772472373-what-is-flow
- Wispr Notetaker: https://docs.wisprflow.ai/articles/3665250541-getting-started-with-notetaker-beta
- Wispr live transcript: https://docs.wisprflow.ai/articles/4091106439-the-live-transcript-in-notetaker
- Wispr Android setup: https://docs.wisprflow.ai/articles/8858845757-setup-wispr-flow-on-android-android-settings
- youtube-transcript-api: https://pypi.org/project/youtube-transcript-api/
- yt-dlp: https://pypi.org/project/yt-dlp/
- yt-dlp EJS setup: https://github.com/yt-dlp/yt-dlp/wiki/ejs
