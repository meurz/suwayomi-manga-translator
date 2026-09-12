"""Download and load the three CPU models before connecting the reader."""

import asyncio

import torch
from manga_translator.config import Detector, Inpainter, Ocr
from manga_translator.detection import prepare as detector
from manga_translator.inpainting import prepare as inpainter
from manga_translator.ocr import prepare as ocr

torch.set_num_threads(2)


async def main():
    await detector(Detector.default)
    await ocr(Ocr.ocr48px, "cpu")
    await inpainter(Inpainter.lama_mpe, "cpu")
    print("CPU models ready")


asyncio.run(main())
