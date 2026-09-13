# Live verification

Verified on 2026-09-13 against an existing Suwayomi Server v2.3.2243 installation.
Host: Linux ARM64, 10 vCPU / 16 GiB, shared with other applications, no GPU.
Worker limits: 3 CPU cores, 4.5 GiB RAM, 2 PyTorch threads. LLM: a configured
Responses-compatible `gpt-5.6-luna` endpoint. These are observations from a small
sample, not a throughput guarantee.

## Actual reader path

A local, three-page CBZ test chapter was imported into Suwayomi: a Japanese manga
page, a generated Chinese page and a blank page. The reader page endpoints were
requested through Suwayomi itself, not directly through the worker.

| Check | Result |
| --- | --- |
| Japanese page, 1487 × 2048 | 14 translated regions; valid completed image through Suwayomi |
| First Japanese request | First 16 KiB at 15.27 s, complete image at 70.47 s |
| Repeat Japanese request | 0.237 s; no new inference |
| Chinese page through Suwayomi | 12.72 s, skipped; streaming re-encoding may change bytes |
| Blank page through Suwayomi | 6.903 s, skipped; original bytes retained |
| Slow response | Crossed Suwayomi's 30 s read timeout using valid PNG ancillary chunks |
| Management UI | Chromium loaded status and job table with zero JavaScript errors |
| Public management ingress | Unauthenticated request redirected to the existing sign-in gateway |
| Suwayomi source modifications | None; `serveConversions` updated through GraphQL and read back |

The source byte cache is distinct from the output encoding on slow streamed
responses. The gateway returns original bytes for fast/cached skipped pages;
slow responses are re-encoded to RGBA PNG while preserving pixel values.

One page-edge bubble was erased without a visible translation by the upstream
renderer. The integration's render guard detected the unchanged inpainted region
and fitted a fallback translation into the visible page area. The resulting page
was visually inspected. Some small bubbles still have tiny text/residual marks,
and line wrapping and names are not human-reviewed translation quality.

## Where the time went

A measured inference run reported stages relative to the start of translation:
detection started at 3.19 s, OCR at 10.38 s, text merge at 31.75 s, LLM translation
at 33.38 s, inpainting at 43.71 s, rendering at 58.65 s, completion at 59.49 s.
The full reader request also includes initial worker setup, source retrieval and
stream encoding. CPU OCR and inpainting are substantial parts of the latency.

## Deterministic checks

35 tests cover request deduplication, content/config cache keys, exact skipped
bytes, Chinese/uncertain region filtering, missing/duplicate LLM IDs, worker
failure, HTML masquerading as an image, authentication, pause, background
completion after a response deadline, restart recovery, cache eviction, valid
streamed PNGs, pixel-preserving fallback and page-edge rendering. Remote-worker
authentication, explicit device selection and safe credential migration are
also covered.

## WSL2 GPU trial

The same 1487 × 2048 Japanese page was tested on an NVIDIA GeForce RTX 3070
Laptop GPU (8 GiB), CUDA-enabled PyTorch 2.5.1+cu124, with 2 PyTorch threads,
4 CPU cores and a 6 GiB container memory limit. Detection remained 1024 pixels,
LaMa MPE repair remained 768 pixels/FP32, and the translation model was unchanged.
The WSL container needed explicit `/dev/dxg` device access; CUDA readiness was
verified inside the container. No host-wide NVIDIA configuration was changed.

| Measurement | Observed time |
| --- | --- |
| Direct WSL worker, first request after startup | 21.649 s |
| Direct WSL worker, two subsequent uncached requests | 15.945 / 14.621 s |
| ARM → HTTP/2 Quick Tunnel → WSL worker, no gateway queue | 34.069 / 30.973 s |
| ARM → QUIC Quick Tunnel → WSL worker, no gateway queue | 19.347 / 18.213 s |
| Suwayomi reader over initial HTTP/2 tunnel, uncached | 34.016 s |
| Suwayomi reader over selected QUIC tunnel, two uncached retries | 19.295 / 18.266 s |
| Suwayomi repeat from gateway cache on final deployment | 0.155 s |

All these Japanese requests produced 14 translated regions. In the QUIC runs,
worker time was 14.940 / 15.150 s and total transport overhead was 4.407 / 3.063 s.
HTTP/2 overhead was 11–13 s on the tested connection. The tunnels connected to
different Cloudflare edge locations, so the comparison includes routing effects
and is not proof that QUIC alone will always produce this improvement.

A representative warm local run used about 0.4 s for detection, 0.8 s for OCR,
1.1 s for text merging, 10.5 s for the translation API, 0.3 s for mask generation,
0.2 s for inpainting and 0.3 s for rendering. The cloud translation API is now
the largest variable. Total observed GPU memory peaked at 6142 MiB including
the Windows desktop and other GPU use (about 3.7 GiB before the worker).

The fully translated manga, generated Chinese fixture and blank page were
resubmitted to the GPU worker: all returned `skipped`, zero translated regions
and the exact original file bytes. Output was visually inspected; small-bubble
residual marks and awkward wrapping remain. Parallel reader traffic was also
observed: queued page latency can substantially exceed the isolated numbers
above. One queued HTTP/2 reader retry took 53.234 s. This remains one inference
job at a time, not a chapter-throughput benchmark.

Worker `/process` rejected unauthenticated public tunnel requests. The gateway
remained on ARM, with its existing authenticated management route and disk cache.
Cloudflare Quick Tunnel connectivity was verified; a named tunnel requires
appropriate credentials for the domain's Cloudflare account.

The final reader retries used the original sample's current cache key, explicitly
invalidated its result through the authenticated retry endpoint, and timed from
retry submission through receipt of a fully decoded Suwayomi image. Neighboring
pages were already cached and the queue started empty. Both produced 14 regions;
this does not claim the same latency for a cold multi-page chapter queue.

## Test material

- Japanese sample: the first sample linked by the upstream manga-image-translator
  README, credited there to @09ra_19ra. It is retained only in the private test
  deployment, not redistributed in this source repository.
- Chinese/Japanese text fixtures and the blank page were generated locally.
- Anna's Archive CLI also successfully downloaded the historical scan
  `北斎漫画.5編.pdf`: 10,862,673 bytes, catalog MD5
  `1b772398496e8c3e68aefc9bb985336d`. Its sparse/historical lettering is not
  representative of modern speech-bubble OCR; it was not used to claim modern
  manga translation quality. The PDF is not shipped here.

## Remaining limits

This is a small deployment trial, not a controlled multi-user load test. ARM64
CPU and WSL2 AMD64/NVIDIA inference were verified; other GPU platforms were not.
Chapter pretranslation is available from v0.3.0; per-book translation rules are not implemented.
The existing Suwayomi source's own availability is outside this integration.
Browser/reader caches can retain original fallbacks; refresh them after background
translation finishes. OCR, Chinese/Japanese language ambiguity, long text and
background repair can still be imperfect.

The returned Chinese and blank reader images were decoded and compared to their
original RGB pixels: both were identical. The completed 14-region Chinese manga
result was then submitted as a new page: all regions were skipped (41.251 s,
zero translated regions), confirming skip behavior on a full illustrated page
as well as the generated text fixture.


## Ahead-of-reading chapters (v0.3.0)

The ARM gateway was upgraded to chapter mode with a read-only Local source mount.
The existing three-page Japanese/Chinese/blank test chapter reached ready using
existing page-cache results, and its complete translated CBZ passed image/CRC
verification. This run measures preparation and durable publication, not uncached
inference throughput.

The GPU worker was then actually stopped. Suwayomi's three reader image requests
still returned bytes matching the prepared CBZ; the Japanese page took 0.155 s,
the Chinese page 0.029 s and the blank page 0.027 s. Chinese/blank bytes matched
the originals. No new page job, inference or cache-hit update was recorded.
The complete translated CBZ remained exportable. The GPU worker was restarted
and its health endpoint confirmed CUDA readiness afterward.

The management page was exercised in Chromium at desktop and 390-pixel mobile
widths: manga URL parsing, chapter selection, select-all, pause/resume and chapter
status updates worked, with no JavaScript errors or horizontal page overflow.
The preview endpoint remained loopback-only; the public management entry retained
its existing authentication gateway.

Deterministic chapter tests additionally cover baseline/manual versus new/auto
downloads, native download waiting and deadlines, partial-publication prevention,
resuming successful pages after a failure, cancelling during inference, corrupt
page and CBZ detection, restart recovery, worker-offline waiting without exhausting
retries, unsafe Local archive paths, archive limits, profile changes, and the real
gateway callback refusing a failed worker's reader fallback as chapter success.


During the native-download trial, transient GPU stalls exceeded Cloudflare's
request timeout and returned HTTP 524. The chapter remained incomplete and kept
its verified pages. One retry recovered; a later GPU synchronization stall needed
a worker restart, after which the same chapter resumed from its saved checkpoint.
The worker now has a configurable 300-second inference deadline so this recovery
can happen under Docker's restart policy. Subprocess tests verify both forced
exit on a simulated stall and cancellation of the timer after successful work.
These observations do not establish a fixed chapter-preparation time under shared
GPU load, and a named tunnel does not remove Cloudflare's request-duration limits.

A previously undownloaded 16-page chapter was enqueued using Suwayomi's native
`POST /api/v1/download/batch`, without a translator enqueue request. The watcher
automatically discovered its completed download, prepared all 16 pages (70
translated regions), and published a valid CBZ. Every Suwayomi reader response
matched its corresponding translated CBZ page byte-for-byte. Native exported
original entries remained identical to the retained source archive. The 16
prepared reads took 0.024–0.046 seconds (median 0.037 seconds), with no new
inference or page-cache job changes. This end-to-end trial included the stalls,
retries and maintenance pauses described above; it is not an isolated throughput
benchmark.

A disposable container running the production worker image was also given a
simulated stalled inference and a short watchdog deadline. It exited with code
70, Docker restarted it once as configured, and the second timeout also exited
70. The test container was removed afterward; it did not load GPU models or touch
chapter data.

## Manga language memory (v0.4.0)

The existing ARM v0.3.0 page and chapter databases were backed up and migrated.
Previously prepared Japanese chapters remained ready. Their observed translations
seeded foreign-language history; old skipped results were not assumed Chinese.

A generated Chinese fixture used two chapters, each containing three distinct
1000 × 1400 pages. The first chapter ran through the real GPU worker and cloud
language model: 9 confident Chinese regions, 136 Han characters, no foreign or
uncertain regions. After full chapter validation, its manga policy automatically
became Chinese. All three source pages retained their exact bytes.

The GPU worker was actually stopped before enqueueing the second chapter. Its
three pages reached original-only status in 0.721 seconds, with zero new model
jobs and no generated translated CBZ, page outputs or retained duplicate source
archive. Every Suwayomi reader response matched the corresponding original bytes.
An indexed page also bypassed inference with optional online fallback enabled;
chapter mode was restored immediately afterward, and the worker restarted.

51 tests cover this policy's evidence transport, complete-chapter requirement,
blank/cover/duplicate/uncertain/mixed/legacy exclusions, foreign-language history,
source-scoped manga IDs, original-only native downloads, active-page policy changes,
manual override/reset, persistence, reader bypass, and authenticated management.
The unindexed online-read limitation remains documented in README: Suwayomi's
image hook does not provide a manga ID.

## v0.5.0 page pipeline

The gateway, chapter scheduler and GPU worker were deployed with
`PAGE_CONCURRENCY=2`. Models and translation settings remain unchanged:
`gpt-5.6-luna`, Responses API, low reasoning, the pinned upstream engine and the
existing cache profile. GPU model stages remain serialized; page-local engine
objects share upstream model caches. Worker logs show the second page entering
OCR and cloud translation before the first page's cloud request completes.

Four existing English-language comic pages were processed without page-cache hits
using the same images in each run. These are throughput checks, not Japanese OCR
quality benchmarks. Input/output comic files remain private.

| Path | Serial | Two pages in flight | Scope |
| --- | ---: | ---: | --- |
| WSL loopback to GPU worker | 65.276 s | 26.392 s | Four complete page responses; first serial run included model initialization |
| ARM gateway → QUIC Quick Tunnel → WSL GPU | 85.778 s | 42.436 s | Isolated chapter jobs, uncached inference, page validation and completed CBZ |

The ARM comparison used the actual gateway submission path and chapter manager
with an isolated local archive source and separate empty task/cache directories.
It observed peak processing counts of 1 and 2 respectively. Both complete CBZs
passed CRC validation, all four reader responses matched their corresponding CBZ
entries byte for byte, and both runs returned 29 translated regions. The ARM run
reduced total chapter preparation time by 50.5% (2.02× throughput). Cloud latency,
network transfer, GPU sharing and warm-up affect these small sequential trials;
the result does not guarantee the same speedup for other chapters or halve
single-page latency. Translation wording and region decisions can vary between
cloud calls. Existing OCR omissions and typesetting limitations remain.

The RTX 3070 Laptop GPU has 8 GiB VRAM; total observed usage during the trial was
about 5.9 GiB, including unrelated Windows GPU usage (about 3.4 GiB before work).
The worker retains its 4 CPU / 6 GiB RAM limit and two Torch threads.

Reproduce a direct worker comparison with a private CBZ containing exactly the
specified page count and `WORKER_TOKEN` supplied in the environment:

```sh
uv run python scripts/benchmark_pipeline.py /path/to/private.cbz \
  --pages 4 --concurrency 1 2 --output experiments/pipeline
```

The benchmark bypasses the gateway cache, verifies images and skipped bytes, and
writes per-page images and timing JSON to the selected private output directory.

Regression validation: 61 tests cover two simultaneous worker HTTP requests,
exclusive model stages, per-page force/language isolation, retryable overload,
cloud failure cleanup, bounded chapter scheduling, pause/cancel/original-only
policy changes with two requests in flight, out-of-order checkpoint recovery and
end-to-end gateway admission. Ruff, source/wheel builds and installation into an
isolated Python environment passed. The desktop and 390 px management UI show
the configured capacity and preserve Chinese language rules.

A separate live Suwayomi Local-source chapter contained four uncached PNG pages
plus Chinese and blank controls (six pages total). It reached ready with peak
processing=2, and all six native Suwayomi reader responses matched the translated
CBZ exactly. Chinese and blank controls retained their original bytes. Prepared
reads took 0.050–0.125 seconds. This larger PNG case took **241.253 seconds** to
prepare: the four foreign pages were 1.08–1.46 MB each, gateway request times were
97.785–123.202 seconds, while their logged engine pipelines took 11.42–16.17
seconds. Transfer/path overhead dominated this case despite working pipeline
concurrency; the small compressed-image trial must not be generalized to large
PNG uploads over the temporary tunnel.

A simultaneous real Japanese/Chinese request pair also passed: the Japanese page
translated 14 regions in 16.662 seconds; the Chinese page returned exact original
bytes, zero translated regions, and independent Chinese evidence in 4.380 seconds.
These direct worker requests bypassed caches. The established Chinese-book policy
survived the gateway upgrade.
