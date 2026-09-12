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

18 tests cover request deduplication, content/config cache keys, exact skipped
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
There is no automatic chapter pretranslation or per-book routing yet.
The existing Suwayomi source's own availability is outside this integration.
Browser/reader caches can retain original fallbacks; refresh them after background
translation finishes. OCR, Chinese/Japanese language ambiguity, long text and
background repair can still be imperfect.

The returned Chinese and blank reader images were decoded and compared to their
original RGB pixels: both were identical. The completed 14-region Chinese manga
result was then submitted as a new page: all regions were skipped (41.251 s,
zero translated regions), confirming skip behavior on a full illustrated page
as well as the generated text fixture.
