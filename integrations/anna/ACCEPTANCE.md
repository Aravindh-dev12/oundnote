# Acceptance results — September 28, 2026

Tested against the Anna-installed Executa 0.1.2 on Windows x86_64.

| Check | Result |
|---|---|
| Installed version and Anna parameter-list contract | Passed |
| Real Meet2Notes connection and enabled MCP access | Passed |
| Real meeting listing | Passed: 25 entries |
| Transcript, speaker labels and timestamp structure | Passed |
| Second transcript page without duplicate segment indices | Passed |
| Existing AI notes | Passed |
| Keyword search using a term from the transcript | Passed |
| Semantic search with structured source references | Passed: 8 results |
| Invalid arguments and unsupported write operations | Rejected correctly |
| Disabled MCP access, isolated fixture | Rejected correctly |
| Unavailable backend, isolated fixture | Actionable status and read errors |
| Long synthetic notes | Three pages reconstructed without loss |
| Unit and API tests with temporary data | 15 passed |

The selected real notes fit one page; long-note pagination used synthetic data.
Failure tests did not close Meet2Notes or change its settings. This report and
acceptance-results.json contain no meeting titles, transcripts or notes.

The tests cover the installed binary and its connection to Meet2Notes. They do
not automate the Anna model conversation or evaluate the quality of generated
summaries. The user separately confirmed that Anna chat works.

The English UI is a subsequent presentation-only update; see app.json for its
version. The v0.1.3 UI was checked against the real library for search, notes
formatting and speaker-grouped transcripts. The user also supplied screenshots
of both views from the installed Anna app.

## Marketplace submission

Desktop release v0.6.2 validation: the full Windows test suite passed (128 tests),
along with Ruff and mypy (96 source files). The desktop runtime change is limited
to its version declaration; the optional integration adds no desktop dependencies
or database migrations.

Submitted September 28, 2026. Anna accepted the release precheck and confirmed
status pending_review, candidate v0.1.3, app 348. Two user-provided screenshots
were uploaded to the listing: AI notes first, transcript second. Public release
has not been requested through the CLI. Reviewer instructions remain in REVIEW.md.
