# Option 2 Design QA — Cognitive Orrery

Source reference: `data/viewscreen/hal-concept-2.png`

Implementation surfaces: `static/index.html` (shared skeleton),
`static/bridge-shared.css` (base + mobile, split out of bridge-option1.css),
`static/bridge-option2.css` (orrery desktop/tablet shell),
`frontend/optic-orrery.ts` (scene), `frontend/directions.ts` (manifest,
`ready: true`).

Verified viewports and states (in-app browser):

- Desktop 1440 × 1024: full-bleed stage behind the chrome; eye centered in
  the stage band inside the ring mandala; satellite lens discs recede along
  the horizontal axis both sides; the waveform beam runs the full viewport
  width through the eye. Timeline mission log left (dot nodes on a hairline
  rail), Missions card top-right, tab nav top with brand left ("Hermes Hal ·
  COGNITIVE ORRERY") and direction selector right, full-width command bar
  with circular send, centered telemetry status strip bottom.
- Tablet 900 × 900: two-column layout — log column left, stage filling the
  rest; Missions card floats top-right over the stage; nav, command bar, and
  status strip full width. Required explicit resets against the legacy
  pre-directions tablet styles that still live in index.html's inline
  block (`.mission-log` 35vh card, body flex centering) — mirrored the
  stretch guards option 1 uses.
- Mobile 390 × 844: shared direction-independent mobile flow (from
  bridge-shared.css); the orrery scene composes itself into the eye card
  (mandala + beam scale into the card), brand shows the direction label,
  bottom rail shows 02 active. No horizontal overflow.
- States: idle (STANDBY) and listening (LISTENING) label/state transitions
  verified through the real `#eye` class contract; permission card verified
  with the real `#permbar` show path — renders as the concept's
  PERMISSION REQUIRED card (eyebrow, request text, stacked ALLOW/DENY)
  bottom-right. Proposal card mirrors it top-right in amber.
- Switching: 01 → 02 and 02 → 01 exercised through the rail buttons
  (persist + reload); option 1 renders identically to its pre-split QA
  after the bridge-shared.css extraction; stale/not-ready selections still
  fall back and clear.

## Known capture limitation

The in-app browser pane reports `document.visibilityState: "hidden"`, which
pauses `requestAnimationFrame` and `THREE.Timer` between captures — so
continuous motion (ring orbits, beam travel, energy response) freezes in
screenshots. Load-to-load captures confirm distinct frames render (randomized
ring phases differ per load), and the energy/motion code paths are identical
in structure to option 1's battle-tested aperture loop. Verify live motion by
viewing the page on screen; this matches option 1's behavior of pausing in
background tabs.

## Iteration history

- P1, materials: fully-metallic brass rings rendered near-black (no
  environment map to reflect). Fixed by dropping metalness to ~0.8, raising
  roughness, adding a faint warm emissive floor, and brightening the key
  light — rings now read as brass against the void.
- P1, layout: the mission-log header cramped at 300px/260px column widths
  (title and action pills wrapping). Fixed with a stacked header (title
  row above pill row) at both breakpoints.
- P1, layout: the legacy inline tablet styles re-skinned the log as a
  floating 35vh card and centered grid items vertically. Fixed with full
  property resets plus explicit align/justify stretch guards on the body
  grid, matching option 1's defense.
- P2, polish: idle bloom/core raised slightly for presence; send button
  icon treatment added at tablet.

Intentional differences from the concept are functional, as with option 1:
live telemetry values replace placeholder copy; the concept's DUPLEX tab is
the existing Duplex toggle in the log header; the direction selector
occupies the nav's right slot; chess and viewscreen render into the
right-column card slot via the existing surface-switching contract.

final result: passed (with the capture limitation noted above)
