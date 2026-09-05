# Page dependency trees

## `/` — Hermes Hal Bridge

Entry: `static/index.html`

Dependencies:

- `static/index.html`
  - `static/bridge-shared.css`
  - `static/bridge-option1.css`
  - `static/bridge-option2.css`
  - `static/bridge-option3.css`
  - `static/bridge-option4.css`
  - `static/fonts/azeret-mono-400.woff2`
  - `static/fonts/azeret-mono-600.woff2`
  - `static/fonts/ibm-plex-mono-400.woff2`
  - `static/fonts/ibm-plex-mono-500.woff2`
  - `static/assets/hal-optic.js`
    - `frontend/hal-optic.ts`
      - `frontend/directions.ts`
      - `frontend/optic-api.ts`
      - `frontend/optic-aperture.ts`
      - `frontend/optic-orrery.ts`
      - `frontend/optic-vault.ts`
      - `frontend/optic-chorus.ts`
- `main.py`
  - `hermes_bridge.py`

For command-composer design work, use these visual context ranges:

- `static/index.html:315:340`
- `static/index.html:846:890`
- `static/index.html:1158:1400`
- `static/index.html:2760:2900`
- `static/bridge-shared.css`
- the selected direction CSS, line-ranged to its `.bridge-input` section when the file exceeds about 900 lines
- `.superdesign/design-system.md`
