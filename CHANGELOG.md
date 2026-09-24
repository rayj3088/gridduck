# Changelog

## 0.3.0

Everything below the line between "works in a demo" and "works in a deployment".

- **Proxy runs the rot engine.** `loadslack serve` now fronts the elimination
  and verification path, not the older quality-tradeoff ladder. This was the
  blocker: the engine was only reachable from Python, so `pip install` plus a
  `base_url` change got the wrong thing.
- **Streaming (SSE) is first-class.** Chunks relay as they arrive; TTFT is
  measured from the first byte upstream, not the last. Streamed responses are
  reassembled into canonical form so they can be cached, and a cache hit on a
  streaming request is re-emitted *as a stream*. Buffered and streamed calls
  share one cache.
- **In-flight collapse actually collapses.** The second caller now blocks on
  the first caller's answer instead of merely being told a duplicate exists.
  Leader death degrades to baseline: followers wake on a timeout and make the
  call themselves, which is what would have happened with no driver at all.
- **Shared cache across processes.** `SqliteBackend` via `--cache`; a
  per-process cache halves its own hit rate the moment you run two workers.
  `CacheBackend` is the seam for a network store when you outgrow one host.
- **Rolling proof window.** The verdict now covers a moving window (default
  one hour). A good stretch last month can no longer prop up today's claim.
- Fixed: unsigned 64-bit simhashes overflowed SQLite's signed INTEGER, so
  every near-duplicate write silently failed on the shared backend. Found by
  the first real streaming round-trip test.
- Fixed: the compressor envelope collapsed to its target whenever two
  decisions shared a timestamp, making attack and release instantaneous under
  exactly the load they exist for.

## 0.2.0

- Rot engine, holdout verifier, latency ledger, per-model profiles, tiering.

## 0.1.0

- Compressor, ladder, durable deferral queue, hash-chained receipt ledger.
