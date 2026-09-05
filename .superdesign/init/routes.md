# Routes

Framework routing: none. FastAPI serves one static frontend.

## `/`

- Page: Hermes Hal Bridge
- Component: `static/index.html`
- Layout: single cockpit shell documented in `layouts.md`
- Visual runtime: `/static/assets/hal-optic.js`
- Direction registry: `frontend/directions.ts`

## Relevant data routes

- `/api/status`: local HAL and Hermes bridge status
- `/api/history`: current HAL scrollback
- `/api/missions`: mission cards
- `/api/latency`: current turn telemetry
- `/api/viewscreen`: viewscreen assets
- `/api/systems`: cleaned Hermes CLI surfaces
- `/api/say`: typed turn submission fallback
- `/ws`: live text, voice, tool, permission, and reply transport

There is no router configuration file.
