# Smoke Test Checklist — Phase 4

Run manually before marking Phase 4 done. Check all boxes.

## Responsive layout

- [ ] Search page renders cleanly at 375px (iPhone SE)
- [ ] Search page renders cleanly at 768px (iPad)
- [ ] Search page renders cleanly at 1024px (desktop)
- [ ] Nav links are tappable (≥44px tap targets)
- [ ] Jobs page renders at all three breakpoints
- [ ] Library page renders at all three breakpoints

## Search flow

- [ ] Typing in search box triggers local results within 400ms
- [ ] Local results show title, artist, album, quality badge
- [ ] "Search YouTube" button shows spinner while loading
- [ ] Cloud results show title, artist, duration, Acquire button
- [ ] "Preview" link on cloud result opens YouTube in new tab
- [ ] Empty query shows prompt, not error

## Acquire flow

- [ ] Clicking Acquire on a cloud result creates a job card in-place
- [ ] Job card shows state (queued → downloading → done)
- [ ] Job card auto-refreshes every 3s while in progress
- [ ] Completed job shows ▶ play button
- [ ] Failed job shows Retry button
- [ ] Retry button re-queues the job

## Audio preview

- [ ] Clicking ▶ on a local track starts playback in bottom player
- [ ] Player shows track title
- [ ] Seek bar scrubs correctly
- [ ] Clicking ▶ on a different track switches to it
- [ ] Player stays visible while navigating between pages (SPA behaviour)
- [ ] Audio continues playing when PWA is backgrounded on iOS (critical)

## PWA install

- [ ] iOS Safari: "Add to Home Screen" works
- [ ] iOS: app opens in standalone mode (no browser chrome)
- [ ] iOS: icon appears on home screen
- [ ] Android Chrome: install banner / "Add to Home Screen" works
- [ ] Android: standalone mode confirmed
- [ ] Offline: app shell loads from cache (CSS, icons) when offline

## Pages

- [ ] /health shows Navidrome status, Redis status, disk free
- [ ] /library shows track/album/artist counts and recent additions
- [ ] /jobs lists recent jobs with correct states
