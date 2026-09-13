"""Shared, bounded page admission for chapter scheduling and model workers."""

import os


def page_concurrency():
    value = int(os.getenv("PAGE_CONCURRENCY", "2"))
    if not 1 <= value <= 2:
        raise ValueError("PAGE_CONCURRENCY must be 1 or 2")
    return value
