# Changelog

## 0.4.0

- Remember Chinese manga after a complete chapter provides sufficient language evidence.
- Bypass models for later chapters; preserve original pages and index their hashes for reading.
- Add per-manga Auto, Always process, Original only and reset controls.
- Keep mixed/uncertain chapters and historical skipped results from establishing Chinese.

## 0.3.0

- Recycle stalled inference workers after a configurable deadline, preserving gateway checkpoints.
- Make ahead-of-reading chapter preparation the default workflow.
- Watch new completed Suwayomi downloads; provide manual backfill and manga chapter selection.
- Preserve originals, resume successful pages, and publish only fully verified chapters.
- Serve durable prepared pages without worker access and export translated CBZ archives.
- Add pause, cancel, retry and storage cleanup controls; keep online fallback optional.


## 0.2.0 — 2026-09-13

Add an authenticated remote CUDA worker and NVIDIA/WSL Compose configurations.
Keep the gateway and cache beside Suwayomi while moving inference to a GPU host.
Add explicit device readiness checks, shared-token configuration migration and
an uncached worker benchmark script. Existing deployments must initialize and
share WORKER_TOKEN before upgrading both services.

## 0.1.0 — 2026-09-13

Initial preview with Suwayomi HTTP image processing, a pinned manga-image-translator
CPU worker, Chinese/uncertain region skipping, translation cache, bounded queue,
authenticated management UI, slow-response PNG streaming and page-edge typesetting
fallback. Validated on Linux ARM64 with Suwayomi v2.3.2243.
