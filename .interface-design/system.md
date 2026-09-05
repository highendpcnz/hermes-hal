# Hermes Hal Bridge interface system

## Direction and feel

Hermes Hal Bridge is a local Hermes ACP operator console for Dave. It should feel calm,
exact, compact, and instrument-like. The optical panel remains the visual
identity; text controls behave like subordinate bridge instrumentation, never
like a generic chat interface or floating SaaS palette.

The recurring signature is a composer-attached command flight strip: live
capability metadata docks immediately above the active input, preserving the
operator's focus and the Bridge's spatial hierarchy.

## Palette and depth

- Canvas: carbon black, `#020202`.
- Inset controls: `#050202` to `#070809`.
- Signal: `#ff2d1f`; deep signal: `#8f0a04`.
- Telemetry: warm gray, approximately `#8e8a84`.
- Dormant metadata: `#514e4a`.
- Structure: low-opacity warm hairlines; active edges use restrained signal red.
- Depth strategy: borders and quiet surface shifts only. Inputs remain inset;
  menus sit one surface level above the canvas. Do not add glass blur, decorative
  gradients, or generic blue and purple accents.

## Typography and hierarchy

- Azeret Mono 400/600: command names, headings, labels, counters.
- IBM Plex Mono 400/500: descriptions, argument hints, typed input.
- Command name: 11px/600/signal.
- Description: 10px/400/telemetry.
- Source and argument hint: 8px/tracked/uppercase/dormant.
- Hierarchy is driven by weight, tone, and spacing before size. Dynamic counts
  and telemetry use tabular alignment.
- The active composer is the focal control. Its menu must not compete with the
  optical module or Mission Log.

## Geometry and density

- Base spacing unit: 4px.
- Command rows: minimum 44px high; 8px vertical and 12–14px horizontal padding.
- Command strip: approximately five rows before scrolling; current cap is
  286px desktop and 264px mobile.
- Desktop row: command, flexible description/hint, source.
- Mobile row: command and wrapping description/hint; source label is omitted.
- Radius follows the selected Bridge direction and stays concentric with the
  composer: Aperture 7px, Orrery 8px, Vault 10px, Chorus 14px.

## Reusable command-channel pattern

- Command metadata comes from Hermes ACP `available_commands_update`, merged
  with explicitly labeled HAL-native commands.
- The input uses combobox semantics and controls a listbox with one announced
  active option.
- `/` opens discovery. Continued typing filters by name, description, and
  argument hint.
- Arrow keys move selection. Tab completes. Enter completes a partial command
  or submits an exact command. Escape closes suggestions without erasing input.
- Pointer movement updates selection without rebuilding the option list;
  pointer selection completes while retaining input focus.
- Loading, empty, degraded Hermes, and live command-count states remain visible
  to assistive technology.
- High-frequency command interaction has no movement animation. Color feedback
  may use the existing 140ms interaction curve.

## Responsive contract

- Desktop and tablet: dock above the always-visible Bridge composer.
- Mobile: dock above the floating command line, use an opaque inset input, and
  remain within the viewport above the navigation rail.
- Verify at 320px, 768–800px, 1024px, and 1440px. Check keyboard completion,
  pointer selection, clipping, overflow, and the browser console.

## Reference

The fuller rationale and domain exploration are preserved in
`.superdesign/design-system.md`. The selected Superdesign project is
`23b6988a-c565-4721-bc37-74615918aa21`; the implemented comparison draft is
`bf528230-935e-40e6-935f-6ea8b3bb90c8`.
