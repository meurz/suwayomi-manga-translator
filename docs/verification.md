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

14 tests cover request deduplication, content/config cache keys, exact skipped
bytes, Chinese/uncertain region filtering, missing/duplicate LLM IDs, worker
failure, HTML masquerading as an image, authentication, pause, background
completion after a response deadline, restart recovery, cache eviction, valid
streamed PNGs, pixel-preserving fallback and page-edge rendering.

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

This is a single-user trial, not a multi-user load test. There is no GPU/AMD64
validation, no automatic chapter pretranslation and no per-book routing yet.
The existing Suwayomi source's own availability is outside this integration.
Browser/reader caches can retain original fallbacks; refresh them after background
translation finishes. OCR, Chinese/Japanese language ambiguity, long text and
background repair can still be imperfect.

The returned Chinese and blank reader images were decoded and compared to their
original RGB pixels: both were identical. The completed 14-region Chinese manga
result was then submitted as a new page: all regions were skipped (41.251 s,
zero translated regions), confirming skip behavior on a full illustrated page
as well as the generated text fixture.
