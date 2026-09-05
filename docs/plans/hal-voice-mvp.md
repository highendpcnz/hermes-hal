# Hermes Hal Voice MVP

## Goal
Minimal FastAPI app with push-to-talk voice interaction in Hermes Hal's voice, to validate voice feel before a larger Next.js build. English only, no persistence beyond process memory, no auth.

## Stack
- FastAPI + Uvicorn (Python 3.12 available)
- STT: Groq `whisper-large-v3-turbo`
- LLM: Anthropic `claude-sonnet-4-6`
- TTS: Piper loading `hal.onnx` from `campwill/HAL-9000-Piper-TTS`
- Frontend: single static HTML, vanilla JS, MediaRecorder

## Phases (milestone commits)
1. `chore: scaffold FastAPI project structure` — requirements.txt, .gitignore, .env.example, directory layout, hal_prompt.py, download-model helper.
2. `feat: add Piper HAL TTS synthesis` — module-level voice load, `synthesize_hal()`.
3. `feat: add Groq Whisper transcription` — `transcribe()` helper.
4. `feat: add Claude HAL conversation loop` — session dict, `hal_respond()`, full `/api/talk` endpoint wiring STT→LLM→TTS with URL-encoded transcript headers and cookie session.
5. `feat: add push-to-talk frontend with red eye UI` — `static/index.html` with breathing eye, 4 states, PTT recorder.
6. `docs: add README and env example`.

## Key files
- `main.py` — FastAPI app, startup voice load, `/` and `/api/talk`
- `hal_prompt.py` — system prompt constant
- `static/index.html` — single-file frontend
- `download_model.py` — one-shot helper to fetch `hal.onnx` + `.onnx.json`
- `requirements.txt`, `.env.example`, `.gitignore`, `README.md`

## Acceptance criteria
Per spec: server starts cleanly, voice loads once, red breathing eye renders, PTT records/sends, 4 eye states fire in order (idle→listening→thinking→speaking→idle), HAL voice sounds like the film, transcripts logged, multi-turn coherent, no console/server errors, works Chrome + Safari on macOS.

## Out of scope
Auth, persistence, Portuguese, VAD, proactive greeting, streaming TTS, retry logic, tests.

## Notes
- Verify `piper-tts` API shape at implementation time (has shifted across releases).
- URL-encode transcript headers server-side (`urllib.parse.quote`).
- `.env` must be created by user with `GROQ_API_KEY` and `ANTHROPIC_API_KEY` before running.
