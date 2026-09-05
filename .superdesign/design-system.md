# Hermes Hal Bridge design system

## Product context

Hermes Hal Bridge is a local voice-and-text operator console backed by Hermes ACP. Dave uses it as a shipboard interface: speak through the optical panel, type precise instructions, inspect missions and systems, and approve gated actions. The present task adds discoverable, executable Hermes slash commands to the existing text composer.

## Intent

- Human: Dave at his own workstation, frequently moving between voice and precise keyboard control.
- Job: discover, complete, and execute Hermes slash commands without leaving the Bridge.
- Feel: calm, exact, compact, and instrument-like. Never a generic chat app or command-palette overlay.
- Focal point: the existing composer. Slash discovery should attach to it, not compete with the optical module or Mission Log.

## Domain exploration

- Domain: ACP session, command channel, bridge console, telemetry, command registry, operator authorization, mission control.
- Color world: signal-lamp red, carbon black, machined graphite, warm telemetry gray, dormant oxide, warning amber.
- Signature: a docked command flight strip that exposes live Hermes command metadata immediately above the active composer.
- Rejecting: generic centered command modal in favor of a composer-attached strip; floating rounded cards in favor of machined border geometry; decorative command chips in favor of compact rows with command, description, and argument hint.

## Palette

- Canvas: `#020202`.
- Inset control: `#050505` to `#070809`.
- Machined surface: `#17191b`.
- Primary signal: `#ff2d1f`.
- Deep signal: `#8f0a04`.
- Primary telemetry: `#8e8a84`.
- Dormant metadata: `#514e4a`.
- Structural border: `rgba(196, 189, 180, 0.14)`.
- Active border: `rgba(255, 45, 31, 0.4)`.
- Do not add purple, blue SaaS accents, glass blur, or decorative gradients.

## Typography

- Azeret Mono 400/600 for command names, headings, and labels.
- IBM Plex Mono 400/500 for descriptions, hints, and input.
- Command name: 11–12px, weight 600, primary signal.
- Description: 10–11px, weight 400, telemetry.
- Source/argument hint: 9px, uppercase or tracked metadata, ghost.
- Preserve tabular alignment and current compact line heights.

## Spacing and geometry

- 4px base grid.
- Command row: 8–12px vertical, 12–16px horizontal.
- Related text gaps: 4–8px.
- Menu max height: roughly five rows, then scroll.
- Minimum pointer target: 44px where rows are clickable.
- Use the composer’s current radius and direction-specific geometry. The attached menu’s outer radius must be concentric with the composer treatment.

## Depth and surfaces

- Borders plus quiet surface shifts only.
- Menu: one level above canvas, but still darker than ordinary content panels.
- Input remains inset.
- No new shadows unless a direction already uses one; dark mode structure comes from hairline borders.

## Interaction

- Typing `/` opens the live command list.
- Continued typing filters by command name, description, and argument hint.
- Arrow Up/Down changes the active option.
- Enter completes a partial command; Enter on a complete command submits normally.
- Tab completes the active command.
- Escape closes suggestions without destroying typed text; a second Escape may close the mobile composer.
- Pointer selection completes the command and returns focus to the input.
- Screen readers receive combobox/listbox semantics, current selection, command count, and load/error states.
- The list must preserve native form submission and must not steal the Space-to-speak shortcut when focus is outside the input.

## Responsive behavior

- Desktop/tablet: menu attaches above the always-visible bridge composer.
- Mobile: menu attaches above the floating command line and remains inside the viewport.
- Validate at 320px, 768px, 1024px, and 1440px.

## Motion

- Command menus are high-frequency: no movement animation.
- A short opacity/color transition up to 160ms is acceptable.
- Respect `prefers-reduced-motion`.

## Functional constraints

- Command metadata must come from the live Hermes ACP `available_commands_update`, not a frontend-only hardcoded catalog.
- HAL-native commands may be merged and labeled separately, but must not shadow Hermes command semantics.
- Unknown slash input continues through the existing prompt path.
- The existing HTTP and WebSocket typed-turn paths remain available.
- No React dependency is introduced; this codebase is vanilla TypeScript/JavaScript plus FastAPI.
