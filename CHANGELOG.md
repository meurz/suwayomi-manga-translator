# Changelog

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
