# Acquisition UI audit — 2026-11-15

This follow-up reviews the path from searching to choosing a source, requesting a
download, and reviewing its metadata. The original interface was inspected in the
running application. Changes were exercised in Chromium using the real templates,
HTMX, JavaScript, routes and an isolated fixture database.

## Findings and changes

| Finding | Change |
| --- | --- |
| The global search clipped track names and artist descriptions. Its local results pushed external matches below the visible area. | Artist and YouTube matches come first; identifying text wraps. A visible link opens the complete results. Mobile uses the available screen height, with a close button, focus containment, focus return and background scroll locking. |
| Search showed five artist matches without the relevance information available in Discover. | Search, Discover and the global palette now use one artist component and the same eight-match lookup. Names, disambiguation, search relevance and the album action agree. Three matches show initially; the rest expand on demand. Relevance is explicitly a name-search score, separate from recording verification. |
| Up to thirty local results preceded YouTube on mobile. | YouTube comes before the library, with direct section shortcuts. Five library matches show initially. More remain available without burying download choices. |
| Narrow desktop columns and mobile rows clipped track and album names. | Acquire and Jobs use more desktop width. Track identities wrap, and download controls occupy their own row. Long release titles and mobile tracklist actions also get their own space. |
| Compact review cards displayed the provider candidate even when the resolved metadata differed. | Review and completed cards use the resolved title, artist and album, with a fallback for legacy metadata. An intentionally empty album stays empty. The original downloaded identity remains separately labeled. |
| Warnings were at the bottom of a card and could be clipped; confidence relied heavily on a border and small badge. | A verdict leads each review card: **Check source**, **Check metadata**, or **Strong match**, with the full reason. Proposed metadata and downloaded source are separately labeled and sit side by side on desktop. Selection has a visible outline. Missing files and enrichment suggestions retain their own explanations. |
| Bulk approval described every strong match as fingerprint confirmed. | Both the button and confirmation accurately distinguish fingerprint confirmation from a strong title/artist match. |
| The confirmation appeared at the top left on desktop. The CSS reset removed the native dialog's automatic margins. | Explicit inset and automatic margins center it on both axes. It is bounded by the viewport, scrolls when necessary, and has 44px action targets. Approval consequences use a neutral blue treatment; destructive consequences use red. |
| A refresh response already in flight could replace an expanded review. | Queue refreshes check again before swapping their response, preserving open reviews and edits. Selected jobs remain highlighted after a completed refresh. |
| Source previews had tiny controls, while approval wording was vague. | Album source previews have labeled, touch-sized Listen and YouTube controls. Review tools remain separate from **Save to library**, **Save & next**, and Reject. Receipt titles wrap and link directly to the job. |

## Screenshot evidence

Before images show the original running app, except the desktop confirmation,
which uses the previous session's isolated review fixture. After images use the
new deterministic fixture with deliberately long names, partial album ownership,
flagged/probable/strong matches, a missing file, enrichment and album batches.
The datasets differ; these are layout and interaction evidence, not pixel diffs.

| View | Before | After |
| --- | --- | --- |
| Mobile search | [Long library section before YouTube](screenshots/acquisition/before-search-mobile.png) | [Search with section shortcuts and readable identities](screenshots/acquisition/search-mobile.png) |
| Mobile global search | [Clipped local identities and buried providers](screenshots/acquisition/before-palette-mobile.png) | [Artists and sources, dark theme](screenshots/acquisition/palette-mobile-dark.png) |
| Desktop confirmation | [Dialog at x=0, y=0](screenshots/acquisition/before-confirm-desktop.png) | [Centered confirmation](screenshots/acquisition/confirm-desktop.png) |
| Desktop acquisition | — | [Artists, sources and library](screenshots/acquisition/search-desktop.png) |
| Desktop review queue | — | [Full reasons, proposed identity and downloaded source](screenshots/acquisition/jobs-desktop.png) |
| Mobile review actions | — | [Separate tools, Save, Next and Reject](screenshots/acquisition/review-actions-mobile-dark.png) |
| Mobile album acquisition | — | [Owned/missing tracks and source checks](screenshots/acquisition/discography-mobile.png) |

All matrix screenshots are also emitted to the audit output directory, including
expanded reviews, both themes, confirmation dialogs and intermediate widths.

## Verification and limits

- **439 unit tests pass.** This includes resolved-vs-source identity, legacy
  metadata fallback, source preservation, search fan-out and Jobs triage contracts.
- Browser matrix: **320, 360, 390, 640, 768, 1024 and 1440px**, each in light and dark.
  Checks cover horizontal overflow and clipped identifying text, all eight artist
  matches, Discover parity, inline discography and return query, owned/missing
  track actions, source previews, global search focus/Escape, verdicts and album
  groups, selection after refresh, dialog centering, cancellation, refresh guards,
  edited form submission with `advance=next`, and receipt-to-Jobs navigation.
- The fixture simulates external search and download completion. Review submission
  is intercepted to inspect edited fields without writing audio tags. This audit
  does not verify a real YouTube download, metadata-provider availability or actual
  file placement. It never writes to the production database or music library.
- An attempted host integration run failed album pipeline cases because `/cache`
  and `/music-staging` were not writable; it was stopped after 8 passed / 2 failed.
  The broader pipeline suite requires its container/runtime environment and is
  not claimed as passing here.
- JavaScript syntax, changed Python lint and whitespace checks pass. The existing
  `TrackCandidate as _TC` naming violations elsewhere in `jobs.py` remain unchanged.

## Repeat the audit

Use Python's development dependencies and a local Playwright installation with
Chromium. Start the fixture from the repository root in one terminal:

```sh
.venv/bin/python -m uvicorn tests.e2e.acquisition_preview:app --port 8018
```

In another terminal:

```sh
node scripts/audit-acquisition.cjs
```

The script refuses to run unless the server identifies itself as the isolated
fixture. `PLAYWRIGHT_MODULE` can point to an existing Playwright package;
`CHROMIUM_PATH` can select its browser executable. `AUDIT_BASE_URL`,
`AUDIT_OUTPUT_DIR` and `AUDIT_WIDTHS` override the server, screenshot destination
and comma-separated widths. For shorter runs, use `AUDIT_WIDTHS=320,390` and then
`AUDIT_WIDTHS=640,768,1024,1440`. The default output directory is
`/tmp/audioreap-acquisition-audit/verified`.
