"""Windows UI Automation grounding helper running on a dedicated MTA COM thread."""

from __future__ import annotations

import atexit
import concurrent.futures
import ctypes
import os
import re
import sys
import threading
from typing import Any, Callable, Optional

_PRIORITY_STOPWORDS: set[str] = {
    "open",
    "and",
    "or",
    "the",
    "a",
    "an",
    "to",
    "in",
    "on",
    "for",
    "with",
    "at",
    "of",
    "is",
    "it",
    "him",
    "her",
    "them",
    "you",
    "your",
    "yourself",
    "me",
    "my",
    "then",
    "text",
    "send",
    "message",
    "wait",
    "respond",
    "reply",
    "talk",
    "introduce",
    "say",
    "hi",
    "hello",
    "please",
    "aswell",
    "well",
    "start",
    "playing",
    "play",
    "put",
    "song",
    "music",
    "tab",
    "new",
    "firefox",
    "chrome",
    "edge",
    "browser",
    "instagram",
    "youtube",
}

_BUILTIN_UI_PRIORITY_TERMS: tuple[str, ...] = (
    "search",
    "message",
    "send",
    "direct",
    "play",
)


def extract_priority_terms(task_text: str) -> list[str]:
    """Extract normalized task keywords and common interactive UI terms for UIA prioritization."""
    terms: list[str] = []
    seen: set[str] = set()
    raw_tokens = re.findall(r"[^\s\"'.,!?;:()\[\]{}]+", task_text or "")
    for raw in raw_tokens:
        cleaned = raw.strip("\"'.,!?;:()[]{}").lower()
        if len(cleaned) < 2 or cleaned in _PRIORITY_STOPWORDS:
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            terms.append(cleaned)
    for builtin in _BUILTIN_UI_PRIORITY_TERMS:
        if builtin not in seen:
            seen.add(builtin)
            terms.append(builtin)
    return terms


def _matches_priority_term(name_lower: str, term: str) -> bool:
    """Return True if a control name matches term directly or via fuzzy token containment."""
    if not term or not name_lower:
        return False
    if term in name_lower:
        return True
    if len(term) >= 5:
        for word in re.findall(r"\w+", name_lower):
            if len(word) >= 5 and (word in term or term in word):
                return True
    return False


def prioritize_ui_elements(
    raw_items: list[dict[str, Any]],
    screen_size: tuple[int, int],
    viewport: tuple[int, int, int, int],
    max_elements: int = 60,
    priority_terms: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Deduplicate and prioritize UIA elements so task-relevant controls survive browser chrome caps."""
    cap = max(1, int(max_elements))
    sorted_items = sorted(raw_items, key=lambda el: (el["center"][1] // 24, el["center"][0]))

    deduped: list[dict[str, Any]] = []
    seen_centers: list[tuple[int, int]] = []
    for item in sorted_items:
        cx, cy = int(item["center"][0]), int(item["center"][1])
        if any(abs(cx - px) <= 10 and abs(cy - py) <= 10 for px, py in seen_centers):
            continue
        seen_centers.append((cx, cy))
        deduped.append(
            {
                "role": item["role"],
                "name": item["name"],
                "rect": list(item["rect"]),
                "center": [cx, cy],
            }
        )

    if len(deduped) <= cap:
        selected = deduped
    else:
        terms = [t.lower() for t in (priority_terms or []) if t]
        is_full_viewport = (int(viewport[3]) - int(viewport[1])) >= int(max(1, int(screen_size[1])) * 0.7)
        chrome_y_cutoff = int(max(1, int(screen_size[1])) * 0.13)

        priority_bucket: list[dict[str, Any]] = []
        content_bucket: list[dict[str, Any]] = []
        chrome_bucket: list[dict[str, Any]] = []

        for item in deduped:
            name_lower = str(item.get("name") or "").lower()
            cy = int(item["center"][1])
            if terms and any(_matches_priority_term(name_lower, term) for term in terms):
                priority_bucket.append(item)
            elif is_full_viewport and cy < chrome_y_cutoff:
                chrome_bucket.append(item)
            else:
                content_bucket.append(item)

        selected = list(priority_bucket[:cap])
        remaining = cap - len(selected)
        if remaining > 0:
            chrome_reserve = min(len(chrome_bucket), 15) if content_bucket else 0
            content_budget = max(0, remaining - chrome_reserve)
            content_take = content_bucket[:content_budget]
            selected.extend(content_take)
            left_after_content = cap - len(selected)
            if left_after_content > 0:
                chrome_take = chrome_bucket[:left_after_content]
                selected.extend(chrome_take)
                left_after_chrome = cap - len(selected)
                if left_after_chrome > 0 and len(content_take) < len(content_bucket):
                    selected.extend(content_bucket[len(content_take) : len(content_take) + left_after_chrome])

        selected.sort(key=lambda el: (el["center"][1] // 24, el["center"][0]))

    return [
        {
            "id": idx + 1,
            "role": item["role"],
            "name": item["name"],
            "rect": list(item["rect"]),
            "center": list(item["center"]),
        }
        for idx, item in enumerate(selected[:cap])
    ]

_INTERACTIVE_CONTROL_TYPES: dict[int, str] = {
    50000: "Button",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50007: "ListItem",
    50011: "MenuItem",
    50013: "RadioButton",
    50019: "TabItem",
    50024: "TreeItem",
}

_UIA_BOUNDING_RECTANGLE_PROP = 30001
_UIA_CONTROL_TYPE_PROP = 30003
_UIA_NAME_PROP = 30005
_UIA_AUTOMATION_ID_PROP = 30011
_UIA_IS_OFFSCREEN_PROP = 30022
_COINIT_MULTITHREADED = 0x0


class _UIAWorker:
    """Dedicated single-thread MTA worker for non-blocking UI Automation queries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._local = threading.local()
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        """Tear down the worker executor without blocking process exit."""
        with self._lock:
            if self._executor is not None:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._executor = None

    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="uia-mta",
            )
        return self._executor

    def _ensure_uia_on_worker_thread(self) -> tuple[Any, Any, Any, Any]:
        cached = getattr(self._local, "uia_bundle", None)
        if cached is not None:
            return cached

        ole32 = ctypes.WinDLL("ole32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)

        had_flag = hasattr(sys, "coinit_flags")
        prev_flag = getattr(sys, "coinit_flags", None)
        if "comtypes" not in sys.modules:
            sys.coinit_flags = _COINIT_MULTITHREADED
        try:
            import comtypes.client
        finally:
            if not had_flag and hasattr(sys, "coinit_flags"):
                delattr(sys, "coinit_flags")
            elif had_flag:
                sys.coinit_flags = prev_flag

        uia_mod = comtypes.client.GetModule("UIAutomationCore.dll")
        iuia = comtypes.client.CreateObject(
            uia_mod.CUIAutomation,
            interface=uia_mod.IUIAutomation,
        )
        conds = [
            iuia.CreatePropertyCondition(_UIA_CONTROL_TYPE_PROP, ct)
            for ct in _INTERACTIVE_CONTROL_TYPES
        ]
        or_cond = iuia.CreateOrConditionFromArray(conds)
        on_screen_cond = iuia.CreatePropertyCondition(_UIA_IS_OFFSCREEN_PROP, False)
        final_cond = iuia.CreateAndCondition(or_cond, on_screen_cond)

        cache_request = iuia.CreateCacheRequest()
        for prop_id in (
            _UIA_CONTROL_TYPE_PROP,
            _UIA_NAME_PROP,
            _UIA_BOUNDING_RECTANGLE_PROP,
            _UIA_AUTOMATION_ID_PROP,
            _UIA_IS_OFFSCREEN_PROP,
        ):
            cache_request.AddProperty(prop_id)

        bundle = (uia_mod, iuia, final_cond, cache_request)
        self._local.uia_bundle = bundle
        return bundle

    def _query_sync(
        self,
        viewport: tuple[int, int, int, int],
        screen_size: tuple[int, int],
        max_elements: int,
        priority_terms: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return []

        uia_mod, iuia, final_cond, cache_request = self._ensure_uia_on_worker_thread()
        elem = iuia.ElementFromHandle(hwnd)
        if elem is None:
            return []

        arr = elem.FindAllBuildCache(uia_mod.TreeScope_Descendants, final_cond, cache_request)
        if arr is None:
            return []

        sw = max(1, int(screen_size[0]))
        sh = max(1, int(screen_size[1]))
        v_left, v_top, v_right, v_bottom = viewport

        total = min(int(arr.Length), 800)
        raw_items: list[dict[str, Any]] = []
        for idx in range(total):
            try:
                item = arr.GetElement(idx)
                ct = int(item.CachedControlType)
                role = _INTERACTIVE_CONTROL_TYPES.get(ct)
                if role is None:
                    continue
                rect = item.CachedBoundingRectangle
                left = max(0, min(sw - 1, int(rect.left)))
                top = max(0, min(sh - 1, int(rect.top)))
                right = max(0, min(sw - 1, int(rect.right)))
                bottom = max(0, min(sh - 1, int(rect.bottom)))
                if (right - left) < 6 or (bottom - top) < 6:
                    continue

                cx = (left + right) // 2
                cy = (top + bottom) // 2
                if not (v_left <= cx <= v_right and v_top <= cy <= v_bottom):
                    continue

                name = str(item.CachedName or "").strip()[:80]
                if not name and role == "Edit":
                    name = str(item.CachedAutomationId or "").strip()[:60] or "Text Input"
                if not name:
                    continue

                raw_items.append(
                    {
                        "role": role,
                        "name": name,
                        "rect": [left, top, right, bottom],
                        "center": [cx, cy],
                    }
                )
            except Exception:
                continue

        return prioritize_ui_elements(
            raw_items,
            screen_size=(sw, sh),
            viewport=viewport,
            max_elements=max_elements,
            priority_terms=priority_terms,
        )

    def query(
        self,
        viewport: tuple[int, int, int, int],
        screen_size: tuple[int, int],
        max_elements: int,
        timeout_sec: float,
        priority_terms: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            executor = self._get_executor()
            future = executor.submit(
                self._query_sync,
                viewport,
                screen_size,
                max_elements,
                priority_terms,
            )
        try:
            return future.result(timeout=max(0.05, float(timeout_sec)))
        except concurrent.futures.TimeoutError:
            with self._lock:
                if self._executor is executor:
                    executor.shutdown(wait=False, cancel_futures=True)
                    self._executor = None
            return []
        except Exception:
            return []


_DEFAULT_UIA_WORKER = _UIAWorker()


def collect_foreground_ui_elements(
    viewport: tuple[int, int, int, int],
    screen_size: tuple[int, int],
    max_elements: int = 60,
    timeout_sec: float = 0.7,
    provider: Optional[
        Callable[[tuple[int, int, int, int], tuple[int, int]], list[dict[str, Any]]]
    ] = None,
    priority_terms: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """Collect visible interactive UI elements in the foreground window within viewport."""
    if provider is not None:
        try:
            return list(provider(viewport, screen_size))[:max_elements]
        except Exception:
            return []

    if os.name != "nt" or os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1":
        return []

    return _DEFAULT_UIA_WORKER.query(
        viewport=viewport,
        screen_size=screen_size,
        max_elements=max(1, int(max_elements)),
        timeout_sec=timeout_sec,
        priority_terms=priority_terms,
    )
