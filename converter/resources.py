"""Portable available-RAM checks and conservative sequential processing plans.

Predictions are planning estimates, not measured process peaks or guarantees.
Call again before each page; decoded images must not stay resident per batch.
"""
from __future__ import annotations

import ctypes
import math
import os
import sys
from typing import Iterable, Iterator, TypeVar

GiB = 1024 ** 3
MiB = 1024 ** 2
MAX_BUDGET_BYTES = 8 * GiB
RESERVE_BYTES = GiB
UNKNOWN_BUDGET_BYTES = 2 * GiB
OCR_BASE_BYTES = 512 * MiB
LLM_RESERVE_BYTES = 4 * GiB
BUFFER_BYTES_PER_PIXEL = 96  # Includes source/working copies and small-image enlargement.
PAGE_METADATA_BYTES = 2 * MiB
MIN_PIXELS = 262_144
MAX_PIXELS = 16_000_000


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_uint32), ('dwMemoryLoad', ctypes.c_uint32),
                ('ullTotalPhys', ctypes.c_uint64), ('ullAvailPhys', ctypes.c_uint64),
                ('ullTotalPageFile', ctypes.c_uint64), ('ullAvailPageFile', ctypes.c_uint64),
                ('ullTotalVirtual', ctypes.c_uint64), ('ullAvailVirtual', ctypes.c_uint64),
                ('ullAvailExtendedVirtual', ctypes.c_uint64)]


def available_memory_bytes() -> int | None:
    """Return OS-reported available physical RAM, or None when unavailable."""
    try:
        if sys.platform == 'win32':
            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            api = ctypes.WinDLL('kernel32', use_last_error=True).GlobalMemoryStatusEx
            api.argtypes, api.restype = [ctypes.POINTER(_MemoryStatusEx)], ctypes.c_int
            if not api(ctypes.byref(status)):
                return None
            return int(status.ullAvailPhys)
        if hasattr(os, 'sysconf'):
            pages = int(os.sysconf('SC_AVPHYS_PAGES'))
            page_size = int(os.sysconf('SC_PAGE_SIZE'))
            return pages * page_size if pages >= 0 and page_size > 0 else None
    except (OSError, AttributeError, ValueError, TypeError):
        pass
    return None


def plan_resources(page_width: int = 1240, page_height: int = 1754, *,
                   available_bytes: int | None = None, use_llm: bool = False,
                   max_pixels: int = MAX_PIXELS, batch_cap: int = 32, owned_bytes: int = 0) -> dict:
    """Plan one decoded page plus a bounded chunk of editable metadata.

    available_bytes=None requests a fresh OS measurement. Missing measurement
    uses a labelled 2GiB planning fallback and never enables the optional LLM.
    The existing input page-count policy remains the application's decision.
    """
    width, height = int(page_width), int(page_height)
    if width < 1 or height < 1:
        raise ValueError('페이지 크기는 양수여야 합니다.')
    pixel_limit = min(MAX_PIXELS, int(max_pixels))
    if pixel_limit < 1:
        raise ValueError('이미지 화소 상한은 1 이상이어야 합니다.')
    batch_limit = max(1, min(64, int(batch_cap)))
    measured = available_memory_bytes() if available_bytes is None else max(0, int(available_bytes))
    known = measured is not None
    remaining = max(0, MAX_BUDGET_BYTES - max(0, int(owned_bytes)))
    budget = min(remaining, max(0, measured - RESERVE_BYTES)) if known else min(remaining, UNKNOWN_BUDGET_BYTES)
    minimum_pixels = min(width * height, MIN_PIXELS, pixel_limit)
    minimum = OCR_BASE_BYTES + minimum_pixels * BUFFER_BYTES_PER_PIXEL + PAGE_METADATA_BYTES
    plan = {'allowed': budget >= minimum, 'available_bytes': measured, 'measurement_available': known,
            'budget_bytes': budget, 'reserve_bytes': RESERVE_BYTES, 'estimated_peak_bytes': minimum,
            'max_pixels': 0, 'batch_pages': 0, 'llm_allowed': False, 'estimate_only': True,
            'reason': '', 'notes': ['메모리 예측은 추정치이며 실제 사용량을 보장하지 않습니다.',
                                   '매 페이지 전에 다시 확인하고, 배치 이미지/중간 결과는 디스크에 순차 저장하세요.']}
    if not plan['allowed']:
        plan['reason'] = '글자 인식에 필요한 최소 여유 메모리가 부족해요. 다른 프로그램을 닫고 다시 시도해주세요.'
        return plan
    llm_allowed = bool(use_llm and known and budget >= minimum + LLM_RESERVE_BYTES)
    llm_bytes = LLM_RESERVE_BYTES if llm_allowed else 0
    pixel_capacity = (budget - OCR_BASE_BYTES - llm_bytes - PAGE_METADATA_BYTES) // BUFFER_BYTES_PER_PIXEL
    pixels = max(minimum_pixels, min(pixel_limit, int(pixel_capacity)))
    actual_pixels = min(width * height, pixels)
    page_peak = OCR_BASE_BYTES + llm_bytes + actual_pixels * BUFFER_BYTES_PER_PIXEL
    batch_pages = max(1, min(batch_limit, (budget - page_peak) // PAGE_METADATA_BYTES))
    resize_factor = min(1.0, math.sqrt(pixels / (width * height)))
    plan.update(max_pixels=pixels, batch_pages=int(batch_pages), llm_allowed=llm_allowed,
                estimated_peak_bytes=page_peak + int(batch_pages) * PAGE_METADATA_BYTES,
                scaled_width=max(1, math.floor(width * resize_factor)),
                scaled_height=max(1, math.floor(height * resize_factor)))
    messages = []
    if not known:
        messages.append('가용 RAM을 확인하지 못해 보수적인 기본 계획으로 처리해요.')
    if use_llm and not llm_allowed:
        messages.append('소형 AI 검수 대신 기본 OCR로 처리해요.')
    if width * height > pixels:
        messages.append('가용 메모리에 맞춰 이미지 크기를 줄여요. 작은 글자는 원본과 확인해주세요.')
    messages.append(f'한 쪽씩 처리하고 최대 {batch_pages}쪽 단위로 중간 결과를 저장해요.')
    plan['reason'] = ' '.join(messages)
    return plan


T = TypeVar('T')


def chunk_pages(indices: Iterable[T], batch_pages: int) -> Iterator[tuple[T, ...]]:
    """Yield all indices in bounded sequential chunks; never truncate pages."""
    size = int(batch_pages)
    if size < 1:
        raise ValueError('분할 처리 쪽 수는 1 이상이어야 합니다.')
    chunk = []
    for index in indices:
        chunk.append(index)
        if len(chunk) == size:
            yield tuple(chunk)
            chunk = []
    if chunk:
        yield tuple(chunk)
