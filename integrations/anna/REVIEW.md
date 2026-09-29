# Meet2Notes — reviewer guide

Anna integration v0.1.3 — tested with Meet2Notes v0.6.1.
Bundled Executa v0.1.2. These components use independent version numbers.

## What this app does

Meet2Notes gives Anna read-only access to a user's existing meeting library.
Users can search conversations, inspect timestamped transcripts, read existing
AI notes, and ask Anna to work with the retrieved evidence.

## Setup

1. Use Windows 10 or later, x86_64.
2. Install Meet2Notes from https://meet2notes.eu and open it.
3. Enable MCP access in Meet2Notes Settings.
4. Connect Anna Local Agent on the same computer and select it as default.
5. Install the candidate Meet2Notes app version. Verify that Meet2Notes Local
   Library is installed as Executa 0.1.2 or later.
6. Use a non-sensitive sample meeting containing a completed transcript and notes.
   Semantic search additionally requires an index created in Meet2Notes.

## Suggested checks

- Open the app window and select Check connection.
- Search with an empty Title / description query to list meetings.
- Select a meeting, open Transcript, then AI notes.
- Search a known transcript term and check its timestamp against the source.
- Select Concepts (RAG) and search for a topic in the sample meeting.
- In a new Anna chat, select the local agent and mention #Meet2Notes:
  "Check the Meet2Notes connection and list my five latest meetings."
- Then ask:
  "Find the meeting about [topic] and summarize its decisions with source timestamps."
- Close Meet2Notes and retry: the tool should explain that the app is unreachable.

## Limits and data handling

This release supports Windows x86_64. It requires a separate, running Meet2Notes
installation. It does not provide recording or transcription controls in Anna.
Search returns up to 50 meetings, 20 keyword matches, or 8 semantic results.
Transcripts and notes are paginated. Missing notes or an empty RAG index produce
an error rather than triggering generation or indexing.

Retrieved text and metadata travel through Anna and may be processed by its AI
provider in chat. Audio and the full database are not uploaded by this integration.
This app does not save retrieved transcripts to Anna Storage.

## Listing screenshots

The user supplied captures of the installed English UI showing the “New product
launch” meeting: screenshots/01-ai-notes.png (primary) and
screenshots/02-transcript.png (secondary). They show search results, existing AI
notes and timestamped, speaker-labelled transcript content.

Support: https://github.com/estebanstifli/Meet2Notes/issues
