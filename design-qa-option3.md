# Option 3 Design QA — Signal Vault

Source reference: `data/viewscreen/hal-concept-3.png`

Implementation surfaces: `static/index.html` (shared skeleton),
`static/bridge-shared.css` (base + mobile), `static/bridge-option3.css`
(vault desktop/tablet shell), `frontend/optic-vault.ts` (environment
scene), `frontend/directions.ts` (manifest, `ready: true`).

Verified viewports and states (in-app browser):

- Desktop 1440 × 1024: black angular corridor recedes toward the vault
  frame on the right (slab rhythm carried by faint cool edge strips and a
  decay-1 red spill from the eye); the optic sits recessed in the beveled
  frame with chamfered corners, raking its beam toward the lower left; a
  glossy floor carries the glow pool. Chrome: top bar with brand
  ("Hermes Hal · SIGNAL VAULT"), live status, right-aligned nav +
  direction selector; compact fade-masked mission log top-left; the
  **hero transcript** — `#caption` restyled as huge uppercase mono type
  with a blinking red cursor and dotted rule — renders spoken lines left
  of the eye ("I'M SORRY, DAVE." verified through the real caption/show
  path with the speaking state live); segmented console band (Voice
  Energy strip cell + live telemetry cells); command bar with arrow send.
- Push-to-talk: `#eye` is an enlarged square zone over the vault frame
  (~52vh), honoring the concept's "hold anywhere on the optic"; the
  corner-bracket reticle marks the instruction bottom-right.
- Tablet 900 × 900: same composition, tightened; the voice-channel label
  drops from the top bar so the nav fits (fixed during QA — it collided
  with MISSIONS at 900px). Cards float left, permission/proposal right.
- Mobile 390 × 844: shared direction-independent flow; the scene's
  portrait tier reframes onto the optic inside the eye card (camera
  focus shifts to the vault, tighter fit) and drops pixel ratio to 1.25.
- States: idle → speaking exercised via the real `#eye` class contract
  (top-bar label + hero caption verified); listening/thinking/denied
  drive beam pitch/scan/cut respectively in the animate loop (same
  contract mechanics as directions 01/02; continuous motion not
  capturable in the pane — see the capture note in design-qa-option2.md,
  which applies verbatim).
- Switching: full round-trip 03 → 01 → 02 → 03 through the rail buttons;
  each direction booted its own scene (webgl-ready true), console clean
  throughout.

## Perf tiers

Pixel ratio caps by surface width (1.75 ≥ 1101px, 1.5 ≥ 761px, 1.25
below); portrait containers reframe to a smaller fitted composition;
`prefers-reduced-motion` freezes camera drift, beam scan, and pulses via
the same `reducedMotionQuery` mechanics as the other scenes. Scene budget
is ~30 meshes of box/torus/sphere primitives plus one bloom pass — no
environment maps, no shadow maps, no volumetrics.

## Iteration history

- P1, legibility: the corridor and vault frame fell to black — physical
  decay-2 point lights died before reaching them, and fully dark panels
  had nothing to catch. Fixed with a decay-1 red spill, a stronger cool
  edge light, brighter panel albedo, and barely-luminous cool edge strips
  per slab (the concept's catch-lights).
- P1, artifact: full-height red emissive trim strips read as rendering
  glitches, not architecture. Replaced by the dim cool strips above.
- P1, floor: the reflective floor slab's front and side edges cut hard
  lines across the frame. Widened beyond the frustum and darkened so only
  the glow pool reads.
- P2, tablet: the absolute-positioned status row collided with the nav at
  900px ("VOICE CHAN…MISSIONS"). The channel label now hides at tablet;
  brand title gained its missing uppercase.
- P2, polish: core glow plane enlarged, eye hit-zone nudged onto the
  rendered optic (68% / 45.5%).

Intentional differences from the concept are functional, per the QA
precedent of options 1–2: the console band's Executing/Mission Progress/
Permission cells are represented by the live telemetry pills (real
Bridge/STT/Voice/Tools/Uptime/Lag data) rather than fabricated mission
copy; permission requests use the working card + Allow/Deny flow (shared
with direction 02's pattern) instead of a passive console cell; the
mission log keeps readable mixed-case transcript text under caps
timestamps.

final result: passed (with the shared capture limitation noted above)
