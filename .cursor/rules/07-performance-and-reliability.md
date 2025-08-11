# Performance and Reliability

- Prefer simple, synchronous code unless concurrency is needed.
- If concurrency is necessary, pick one model (asyncio with httpx or threads) and encapsulate it. Do not mix paradigms casually.
- Introduce caching only for pure/deterministic functions; document eviction.
- Guard network calls with:

  - Timeouts
  - Limited retries with jitter
  - Circuit-breakers only if justified

- For large file IO, stream in chunks and avoid loading everything into memory when not necessary.
- Measure before optimizing. Add simple timing/logging where hotspots are suspected (DEV only).
