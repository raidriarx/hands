"""Pluggable desktop action planners (Gemini, OpenAI, Ollama, Local Demo Fallback)."""

from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.parse
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel, Field

from controller.policy import APPROVED_WORKSPACE
from controller.schema import ActionType, DesktopAction

_SYSTEM_INSTRUCTION = (
    "You are an autonomous Windows desktop planner. Respond ONLY with a single JSON object "
    "matching the DesktopAction schema: {action_type, description, x, y, left, top, right, bottom, "
    "mark_id, text, keys, clicks, seconds, app_name, url, target_path, expected_text, reason, risk_category}. "
    "Valid action_type values: click, double_click, move, type_text, press_keys, scroll, wait, "
    "launch_app, open_url, verify_state, view_region, view_screen, request_approval, done, fail. "
    "COORDINATES: Every screenshot is a clean image on a normalized 0..1000 coordinate grid regardless of "
    "actual screen pixels: top-left=(0,0), center=(500,500), bottom-right=(1000,1000). NEVER output raw screen "
    "pixels like 1920 or 1080—always return x, y, left, top, right, bottom in [0, 1000] aimed at the visual "
    "center of the target control in the CURRENT image. "
    "TARGETING & ZOOM: "
    "(1) If ui_elements lists the exact control you want (id, role, name, center), prefer clicking it with "
    "mark_id: <id> (exact Windows UI Automation target). "
    "(2) If a target button or input is small, crowded, or missing from ui_elements, call view_region with "
    "left, top, right, bottom (in 0..1000) to zoom into that area cleanly, then click inside the zoomed "
    "0..1000 frame! Call view_screen to reset back to the full desktop image. "
    "TASK PLAN & WEB APPS: When task_plan is provided in Observation, follow its strategy, starting_url, and steps! "
    "Use action_type='open_url' with url='https://...' to open websites or web services (Instagram, YouTube Music, "
    "YouTube, Gmail, Discord Web, Reddit, X, Spotify Web) directly in one step—NEVER call launch_app for websites. "
    "TAB & WINDOW REUSE: Before calling open_url or launch_app, check the current screenshot, active_window_title, "
    "and visible_window_titles. If the target website (e.g. Instagram) is ALREADY visible on screen, DO NOT call "
    "open_url—work directly in the open tab. If the target website appears as an inactive tab in the browser's top "
    "tab bar, CLICK that existing tab instead of calling open_url. Only call open_url when the target website is not "
    "already open on screen/in the tab bar, is hidden in a background window, or needs a specific new search URL (?q=...). "
    "If launch_app fails with WinError 2 (not found), NEVER call request_approval; switch immediately to open_url. "
    "Use bare application names in launch_app only for installed Windows apps (e.g. 'firefox', 'msedge', 'chrome', 'notepad', 'calc'). "
    "All file writes and target_path values must stay inside approved_workspace; NEVER put a URL in target_path. "
    "DIRECT SEARCH URLS: When searching or playing music/videos on YouTube Music or YouTube, use open_url directly "
    "with 'https://music.youtube.com/search?q=<url_encoded_query>' (or press ['ctrl', 'l'] if a browser is already open). "
    "MESSAGING & RECIPIENT SAFETY: "
    "(a) When opening a chat with a specific person, prefer clicking their matching mark_id from ui_elements; "
    "if not listed, DO NOT guess a crowded contact row from the full screen—first call view_region on the contact "
    "sidebar (e.g. left=0, top=150, right=300, bottom=900) OR use the chat Search box. "
    "(b) CRITICAL BEFORE TYPING: Check the active conversation header at the top of the right chat pane and verify it "
    "matches the intended recipient from task_text. If a DIFFERENT person's chat is open, DO NOT type or send—switch to "
    "the correct recipient first. "
    "(c) After typing a chat message into an input box (e.g. Instagram, Discord, WhatsApp), call press_keys ['enter'] "
    "to send it instead of re-typing the same text. "
    "(d) DO NOT SPAM OR REPEAT: Check recent_history and the visible chat bubbles before typing—never re-send a "
    "greeting/message you already sent, and never type the recipient's username into the chat message box. "
    "(e) WAITING FOR A REPLY: Call wait with seconds: 5 to 8 on the full-screen view (do NOT zoom into only the input "
    "box, which hides incoming replies). Reply only after seeing a new incoming message, or call done once the task is complete. "
    "SELF-CORRECTION: If you just launched an app and it is not in the foreground, click inside its window to focus "
    "it before typing or pressing shortcuts. Check screen_changed_last_step and last_error in the Observation: "
    "if your previous click or typing did NOT change the screen (screen_changed_last_step is false) or last_error "
    "warns of a repeated action/coordinate, DO NOT click the same (x, y) or repeat the same cycle—zoom in with "
    "view_region, pick a mark_id from ui_elements, adjust coordinates, use detected_ui_hints.youtube_music_play_button_xy "
    "if present, or use keyboard shortcuts (Ctrl+L for URL bar, / for search box, Space for play/pause). "
    "Keep description concise (one short sentence <= 120 chars; NEVER put chain-of-thought reasoning inside description). "
    "Only set reason and risk_category when action_type is 'request_approval' or 'fail'; leave them null for normal actions. "
    "Omit null/unused optional fields or set null."
)


ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv_if_present(env_path: Optional[Path] = None) -> None:
    """Load key/value pairs from desktop-controller/.env if available without overriding env."""
    if env_path is None and os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") == "1":
        return
    target = env_path or ENV_FILE_PATH
    if not target.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(target, override=False)
        return
    except ImportError:
        pass
    except Exception:
        return
    try:
        for raw_line in target.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
                val = val[1:-1]
            if key and not os.environ.get(key, "").strip():
                os.environ[key] = val
    except Exception:
        pass


def save_api_key_to_dotenv(
    backend_name: str,
    api_key: str,
    env_path: Optional[Path] = None,
) -> Optional[str]:
    """Persist an API key to os.environ and desktop-controller/.env, returning the env var name."""
    key = api_key.strip()
    if not key:
        return None

    lower_backend = backend_name.lower()
    if key.startswith(("AIza", "AQ.")) or "gemini" in lower_backend:
        var_name = "GEMINI_API_KEY"
    elif key.startswith("sk-") or "openai" in lower_backend:
        var_name = "OPENAI_API_KEY"
    else:
        return None

    os.environ[var_name] = key

    if env_path is not None or os.environ.get("DESKTOP_CONTROLLER_TEST_MODE") != "1":
        target = env_path or ENV_FILE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_lines: list[str] = []
        if target.is_file():
            existing_lines = target.read_text(encoding="utf-8").splitlines()

        prefix = f"{var_name}="
        updated_lines: list[str] = []
        replaced = False
        for line in existing_lines:
            if line.strip().startswith(prefix):
                updated_lines.append(f"{var_name}={key}")
                replaced = True
            else:
                updated_lines.append(line)
        if not replaced:
            updated_lines.append(f"{var_name}={key}")
        target.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    return var_name


def get_saved_api_key_for_backend(backend_name: str) -> str:
    """Return the configured/saved API key for Gemini API or OpenAI API, else empty string."""
    _load_dotenv_if_present()
    lower_backend = backend_name.lower()
    if "gemini" in lower_backend:
        return os.environ.get("GEMINI_API_KEY", "").strip()
    if "openai" in lower_backend:
        return os.environ.get("OPENAI_API_KEY", "").strip()
    return ""


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def extract_som_marks(
    screenshot: Optional[Image.Image],
    screen_size: tuple[int, int],
    max_marks: int = 24,
) -> dict[int, tuple[int, int]]:
    """Detect high-contrast UI regions/buttons on the screenshot and return numbered {id: (x, y)} marks."""
    if screenshot is None:
        return {}
    sw = max(1, int(screen_size[0]))
    sh = max(1, int(screen_size[1]))
    dw, dh = 480, 270
    small = screenshot.convert("L").resize((dw, dh), Image.Resampling.BILINEAR)
    raw = small.tobytes()

    # Scan for bright or high-contrast horizontal UI spans (buttons, pills, search bars, cards)
    clusters: list[list[int]] = []  # [min_x, max_x, min_y, max_y]
    for ry in range(18, dh - 14, 2):
        row_off = ry * dw
        run_start: Optional[int] = None
        runs: list[tuple[int, int]] = []
        for rx in range(6, dw - 6):
            lum = raw[row_off + rx]
            # Detect bright UI elements (>= 195) or strong horizontal edge transitions
            if lum >= 195:
                if run_start is None:
                    run_start = rx
            elif run_start is not None:
                if 8 <= (rx - run_start) <= 140:
                    runs.append((run_start, rx - 1))
                run_start = None
        if run_start is not None and 8 <= (dw - 6 - run_start) <= 140:
            runs.append((run_start, dw - 7))

        for r_x0, r_x1 in runs:
            matched: Optional[list[int]] = None
            for c in clusters:
                if (ry - c[3]) <= 4 and not (r_x1 < c[0] or r_x0 > c[1]):
                    matched = c
                    break
            if matched is not None:
                matched[0] = min(matched[0], r_x0)
                matched[1] = max(matched[1], r_x1)
                matched[3] = ry
            else:
                clusters.append([r_x0, r_x1, ry, ry])

    candidates: list[tuple[int, int]] = []
    for c in clusters:
        cw = c[1] - c[0] + 1
        ch = c[3] - c[2] + 1
        if 8 <= cw <= 140 and 4 <= ch <= 36:
            cx = max(10, min(sw - 10, int(round(((c[0] + c[1]) / 2.0) * (sw / dw)))))
            cy = max(10, min(sh - 10, int(round(((c[2] + c[3]) / 2.0) * (sh / dh)))))
            if not any(abs(cx - px) < 38 and abs(cy - py) < 28 for px, py in candidates):
                candidates.append((cx, cy))

    candidates.sort(key=lambda pt: (pt[1] // 40, pt[0]))
    return {idx + 1: pt for idx, pt in enumerate(candidates[:max_marks])}


def annotate_screenshot_for_planner(
    screenshot: Optional[Image.Image],
    cursor_pos: tuple[int, int],
    screen_size: tuple[int, int],
    detected_ui_hints: Optional[dict[str, Any]] = None,
    som_marks: Optional[dict[int, tuple[int, int]] | dict[int, list[int]]] = None,
) -> Optional[Image.Image]:
    """Overlay pixel ruler ticks, Set-of-Mark [id] badges, CURSOR reticle, and UI hint boxes."""
    if screenshot is None:
        return None
    annotated = screenshot.convert("RGB").copy()
    w, h = annotated.size
    sw = max(1, int(screen_size[0]))
    sh = max(1, int(screen_size[1]))
    scale_x = w / sw
    scale_y = h / sh
    draw = ImageDraw.Draw(annotated)

    # Coordinate ruler along top and left edges every 200 screen px
    for sx in range(200, sw, 200):
        px = max(0, min(w - 1, int(round(sx * scale_x))))
        draw.line([(px, 0), (px, 10)], fill=(255, 230, 60), width=2)
        if px + 36 < w:
            draw.rectangle([px + 2, 2, px + 38, 16], fill=(15, 23, 42))
            draw.text((px + 4, 3), str(sx), fill=(255, 230, 60))
    for sy in range(200, sh, 200):
        py = max(0, min(h - 1, int(round(sy * scale_y))))
        draw.line([(0, py), (10, py)], fill=(255, 230, 60), width=2)
        if py + 16 < h:
            draw.rectangle([2, py + 2, 38, py + 16], fill=(15, 23, 42))
            draw.text((4, py + 3), str(sy), fill=(255, 230, 60))

    # Highlight detected YouTube Music Play button hint if available
    if detected_ui_hints and isinstance(detected_ui_hints.get("youtube_music_play_button_xy"), (list, tuple)):
        coords = detected_ui_hints["youtube_music_play_button_xy"]
        if len(coords) == 2:
            hx, hy = int(coords[0]), int(coords[1])
            bx = max(0, min(w - 1, int(round(hx * scale_x))))
            by = max(0, min(h - 1, int(round(hy * scale_y))))
            draw.rectangle(
                [max(0, bx - 22), max(0, by - 14), min(w - 1, bx + 22), min(h - 1, by + 14)],
                outline=(34, 197, 94),
                width=3,
            )
            play_label = f"PLAY_BTN ({hx},{hy})"
            blx = bx + 26 if bx + 165 < w else max(4, bx - 165)
            bly = max(4, by - 14) if by - 14 >= 4 else min(h - 22, by + 18)
            draw.rectangle([blx, bly, min(w - 1, blx + 136), min(h - 1, bly + 18)], fill=(21, 128, 61))
            draw.text((blx + 4, bly + 3), play_label, fill=(255, 255, 255))

    # Draw Set-of-Mark numbered badges [1], [2], ...
    if som_marks:
        for mid, coords in som_marks.items():
            mx, my = int(coords[0]), int(coords[1])
            bx = max(0, min(w - 1, int(round(mx * scale_x))))
            by = max(0, min(h - 1, int(round(my * scale_y))))
            tag = f"[{mid}]"
            tw = 10 + 7 * len(tag)
            tx = max(2, min(w - tw - 2, bx - tw // 2))
            ty = max(2, min(h - 18, by - 9))
            draw.rectangle([tx, ty, tx + tw, ty + 16], fill=(234, 88, 12), outline=(255, 255, 255), width=1)
            draw.text((tx + 4, ty + 2), tag, fill=(255, 255, 255))

    # Red/magenta cursor reticle and label
    cx = max(0, min(w - 1, int(round(cursor_pos[0] * scale_x))))
    cy = max(0, min(h - 1, int(round(cursor_pos[1] * scale_y))))
    draw.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], outline=(255, 40, 120), width=3)
    draw.line([(max(0, cx - 24), cy), (min(w - 1, cx + 24), cy)], fill=(255, 230, 60), width=2)
    draw.line([(cx, max(0, cy - 24)), (cx, min(h - 1, cy + 24))], fill=(255, 230, 60), width=2)
    label = f"CURSOR ({int(cursor_pos[0])},{int(cursor_pos[1])})"
    lx = cx + 18 if cx + 150 < w else max(4, cx - 150)
    ly = cy + 14 if cy + 32 < h else max(4, cy - 28)
    draw.rectangle([lx, ly, min(w - 1, lx + 132), min(h - 1, ly + 18)], fill=(185, 28, 28))
    draw.text((lx + 4, ly + 3), label, fill=(255, 255, 255))
    return annotated


def compute_screenshot_change_ratio(
    before: Optional[Image.Image],
    after: Optional[Image.Image],
) -> float:
    """Return fraction of downsampled pixels whose intensity changed by >12 between screenshots."""
    if before is None or after is None:
        return 1.0
    b_small = before.convert("L").resize((160, 90))
    a_small = after.convert("L").resize((160, 90))
    b_bytes = b_small.tobytes()
    a_bytes = a_small.tobytes()
    if not b_bytes:
        return 0.0
    changed = sum(1 for x, y in zip(b_bytes, a_bytes) if abs(x - y) > 12)
    return changed / float(len(b_bytes))


def create_foveated_screenshot(
    screenshot: Optional[Image.Image],
    cursor_pos: tuple[int, int],
    screen_size: tuple[int, int],
    max_width: int = 960,
    zoom_size: tuple[int, int] = (320, 240),
) -> Optional[Image.Image]:
    """Downscale full screen for token efficiency while embedding a 1:1 high-res cursor zoom inset."""
    if screenshot is None:
        return None
    full = screenshot.convert("RGB")
    w, h = full.size
    sw = max(1, int(screen_size[0]))
    sh = max(1, int(screen_size[1]))

    # 1:1 high-res crop around cursor in screenshot coordinates
    zw = min(w, int(zoom_size[0]))
    zh = min(h, int(zoom_size[1]))
    cx = max(0, min(w - 1, int(round(cursor_pos[0] * (w / sw)))))
    cy = max(0, min(h - 1, int(round(cursor_pos[1] * (h / sh)))))
    zx0 = max(0, min(w - zw, cx - zw // 2))
    zy0 = max(0, min(h - zh, cy - zh // 2))
    zx1 = zx0 + zw
    zy1 = zy0 + zh
    zoom_patch = full.crop((zx0, zy0, zx1, zy1))

    # Downscale full screen
    if w > max_width:
        new_h = max(1, int(round(h * (max_width / float(w)))))
        base = full.resize((max_width, new_h), Image.Resampling.LANCZOS)
    else:
        base = full.copy()

    bw, bh = base.size
    if bw >= zw + 16 and bh >= zh + 28:
        px = bw - zw - 8
        py = bh - zh - 8
        base.paste(zoom_patch, (px, py))
        draw = ImageDraw.Draw(base)
        draw.rectangle([px - 2, py - 2, px + zw + 1, py + zh + 1], outline=(56, 189, 248), width=2)
        label = f"1:1 ZOOM ({zx0},{zy0})-({zx1},{zy1})"
        draw.rectangle([px, max(0, py - 18), min(bw - 1, px + 210), py], fill=(15, 23, 42))
        draw.text((px + 4, max(0, py - 15)), label, fill=(56, 189, 248))
    return base


def scale_normalized_coord(value: int, target_size: int) -> int:
    """Scale a normalized 0..1000 coordinate to [0, target_size - 1] screen pixels."""
    if target_size < 2:
        return 0
    clamped = max(0, min(1000, int(value)))
    return max(0, min(target_size - 1, int(round((clamped / 1000.0) * (target_size - 1)))))


def normalize_coord(pixel: int, target_size: int) -> int:
    """Map a pixel offset in [0, target_size - 1] onto the normalized 0..1000 grid."""
    if target_size < 2:
        return 0
    return max(0, min(1000, int(round((int(pixel) / float(target_size - 1)) * 1000.0))))


def map_viewport_coord(
    norm_x: int,
    norm_y: int,
    viewport: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Map normalized 0..1000 (x, y) inside viewport (left, top, right, bottom) to screen pixels."""
    v_left, v_top, v_right, v_bottom = viewport
    v_w = max(2, int(v_right) - int(v_left) + 1)
    v_h = max(2, int(v_bottom) - int(v_top) + 1)
    return (
        int(v_left) + scale_normalized_coord(norm_x, v_w),
        int(v_top) + scale_normalized_coord(norm_y, v_h),
    )


def normalize_to_viewport(
    px: int,
    py: int,
    viewport: tuple[int, int, int, int],
) -> tuple[int, int]:
    """Normalize screen pixel (px, py) to 0..1000 relative to viewport."""
    v_left, v_top, v_right, v_bottom = viewport
    v_w = max(2, int(v_right) - int(v_left) + 1)
    v_h = max(2, int(v_bottom) - int(v_top) + 1)
    return (
        normalize_coord(int(px) - int(v_left), v_w),
        normalize_coord(int(py) - int(v_top), v_h),
    )


def subregion_viewport(
    left: int,
    top: int,
    right: int,
    bottom: int,
    parent_viewport: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Resolve normalized 0..1000 region bounds inside parent_viewport into screen pixel bounds."""
    nl, nt = map_viewport_coord(left, top, parent_viewport)
    nr, nb = map_viewport_coord(right, bottom, parent_viewport)
    nr = max(nl + 1, min(int(parent_viewport[2]), nr))
    nb = max(nt + 1, min(int(parent_viewport[3]), nb))
    return (nl, nt, nr, nb)


def crop_screenshot_to_viewport(
    screenshot: Image.Image,
    viewport: tuple[int, int, int, int],
    screen_size: tuple[int, int],
) -> Image.Image:
    """Crop screenshot to screen-pixel viewport (left, top, right, bottom) with DPI scaling."""
    img_w, img_h = screenshot.size
    if img_w < 2 or img_h < 2:
        return screenshot
    sw = max(1, int(screen_size[0]))
    sh = max(1, int(screen_size[1]))
    v_left, v_top, v_right, v_bottom = viewport
    scale_x = img_w / float(sw)
    scale_y = img_h / float(sh)
    il = max(0, min(img_w - 2, int(round(v_left * scale_x))))
    it = max(0, min(img_h - 2, int(round(v_top * scale_y))))
    ir = max(il + 2, min(img_w, int(round((v_right + 1) * scale_x))))
    ib = max(it + 2, min(img_h, int(round((v_bottom + 1) * scale_y))))
    return screenshot.crop((il, it, ir, ib))


_ACTION_TYPE_ALIASES: dict[str, str] = {
    "left_click": "click",
    "move_mouse": "move",
    "press_key": "press_keys",
}


def _coerce_optional_int(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str):
        try:
            return int(round(float(value.strip())))
        except ValueError:
            return value
    return value


def _parse_action_json(
    raw_text: str,
    task_text: str = "",
    active_window_title: Optional[str] = None,
) -> DesktopAction:
    cleaned = _strip_json_fences(raw_text)
    try:
        data = json.loads(cleaned)
    except Exception:
        return DesktopAction.model_validate_json(cleaned)
    if isinstance(data, dict):
        at_raw = str(data.get("action_type") or "").strip().lower()
        at_norm = _ACTION_TYPE_ALIASES.get(at_raw, at_raw)
        if at_norm:
            data["action_type"] = at_norm

        if at_norm == "press_keys" and not data.get("keys"):
            raw_key = data.get("key")
            raw_mods = data.get("modifiers")
            synthesized: list[str] = []
            if isinstance(raw_mods, list):
                synthesized.extend(str(m).strip() for m in raw_mods if str(m).strip())
            elif isinstance(raw_mods, str) and raw_mods.strip():
                synthesized.append(raw_mods.strip())
            if isinstance(raw_key, str) and raw_key.strip():
                synthesized.append(raw_key.strip())
            if synthesized:
                data["keys"] = synthesized
        elif isinstance(data.get("keys"), str) and data["keys"].strip():
            data["keys"] = [data["keys"].strip()]

        if at_norm == "scroll" and (data.get("clicks") is None or data.get("clicks") == 0):
            direction = str(data.get("direction") or "down").strip().lower()
            raw_amount = data.get("amount")
            amt = 3
            if raw_amount is not None and not isinstance(raw_amount, bool):
                try:
                    amt = max(1, min(100, int(round(float(raw_amount)))))
                except (TypeError, ValueError):
                    amt = 3
            data["clicks"] = amt if direction == "up" else -amt

        if at_norm == "wait" and data.get("seconds") is None:
            raw_ms = data.get("milliseconds")
            if raw_ms is not None and not isinstance(raw_ms, bool):
                try:
                    data["seconds"] = max(0.05, float(raw_ms) / 1000.0)
                except (TypeError, ValueError):
                    data["seconds"] = 1.0
            else:
                data["seconds"] = 1.0

        if at_norm == "verify_state":
            if not data.get("expected_text") and not data.get("target_path"):
                if _is_youtube_music_task(task_text.lower()):
                    data["expected_text"] = " - YouTube Music"
                elif active_window_title and active_window_title.strip():
                    data["expected_text"] = active_window_title.strip()

        for int_field in ("x", "y", "left", "top", "right", "bottom", "mark_id", "clicks"):
            if int_field in data:
                data[int_field] = _coerce_optional_int(data[int_field])

        desc = str(data.get("description") or "").strip()
        if not desc:
            desc = f"Execute {at_norm or 'action'}"
        data["description"] = desc[:240].strip()

        allowed_fields = set(DesktopAction.model_fields.keys())
        filtered = {k: v for k, v in data.items() if k in allowed_fields}
        return DesktopAction.model_validate(filtered)
    return DesktopAction.model_validate_json(cleaned)


def _encode_screenshot_b64(
    screenshot: Optional[Image.Image],
    max_width: int = 1280,
    quality: int = 78,
) -> Optional[str]:
    if screenshot is None:
        return None
    img = screenshot
    if img.width > max_width:
        new_h = max(1, int(round(img.height * (float(max_width) / img.width))))
        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _build_observation_prompt(obs: ObservationContext) -> str:
    if obs.coordinates_normalized:
        cursor_pos_norm = list(obs.cursor_pos)
        active_rect_norm = list(obs.active_window_rect) if obs.active_window_rect else None
        hints_norm = dict(obs.detected_ui_hints)
    else:
        full_vp = (0, 0, max(1, int(obs.screen_size[0]) - 1), max(1, int(obs.screen_size[1]) - 1))
        cursor_pos_norm = list(normalize_to_viewport(obs.cursor_pos[0], obs.cursor_pos[1], full_vp))
        if obs.active_window_rect is not None:
            l, t, r, b = obs.active_window_rect
            nl, nt = normalize_to_viewport(l, t, full_vp)
            nr, nb = normalize_to_viewport(r, b, full_vp)
            active_rect_norm = [nl, nt, nr, nb]
        else:
            active_rect_norm = None
        hints_norm = dict(obs.detected_ui_hints)
        yt_xy = hints_norm.get("youtube_music_play_button_xy")
        if isinstance(yt_xy, (list, tuple)) and len(yt_xy) == 2:
            hx, hy = normalize_to_viewport(int(yt_xy[0]), int(yt_xy[1]), full_vp)
            hints_norm["youtube_music_play_button_xy"] = [hx, hy]

    payload: dict[str, Any] = {
        "task_text": obs.task_text,
        "step_index": obs.step_index,
        "coordinate_space": "normalized_0_to_1000_of_current_image",
        "viewport_mode": obs.viewport_mode,
    }
    if obs.active_region_normalized is not None:
        payload["active_region_normalized"] = list(obs.active_region_normalized)
    payload["cursor_pos"] = cursor_pos_norm
    payload["active_window_rect"] = active_rect_norm
    payload["active_window_title"] = obs.active_window_title
    if obs.visible_window_titles:
        payload["visible_window_titles"] = list(obs.visible_window_titles)
    payload["screen_changed_last_step"] = obs.screen_changed_last_step
    if obs.ui_elements:
        payload["ui_elements"] = obs.ui_elements
    if hints_norm:
        payload["detected_ui_hints"] = hints_norm
    if obs.task_plan is not None:
        payload["task_plan"] = obs.task_plan
    payload["approved_workspace"] = obs.approved_workspace
    payload["last_error"] = obs.last_error
    payload["recent_history"] = obs.history[-10:]
    return f"{_SYSTEM_INSTRUCTION}\n\nObservation:\n{json.dumps(payload)}"


class ObservationContext(BaseModel):
    """Structured state snapshot passed to planners before selecting the next step."""

    task_text: str
    step_index: int = 0
    screen_size: tuple[int, int] = (1920, 1080)
    cursor_pos: tuple[int, int] = (960, 540)
    active_window_rect: Optional[tuple[int, int, int, int]] = None
    active_window_title: Optional[str] = None
    visible_window_titles: list[str] = Field(default_factory=list)
    screen_changed_last_step: Optional[bool] = None
    som_marks: dict[int, list[int]] = Field(default_factory=dict)
    detected_ui_hints: dict[str, Any] = Field(default_factory=dict)
    history: list[dict[str, Any]] = Field(default_factory=list)
    last_error: Optional[str] = None
    approved_workspace: str = str(APPROVED_WORKSPACE)
    viewport_mode: str = "full"
    active_region_normalized: Optional[list[int]] = None
    ui_elements: list[dict[str, Any]] = Field(default_factory=list)
    coordinates_normalized: bool = False
    task_plan: Optional[dict[str, Any]] = None


class BasePlanner(ABC):
    """Abstract base class for AI and deterministic desktop planners."""

    name: str = "BasePlanner"

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (available, human_readable_status) for this planner backend."""

    @abstractmethod
    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        """Return the next strictly validated DesktopAction given observation state."""


class GeminiPlanner(BasePlanner):
    """Google Gemini REST planner using runtime GEMINI_API_KEY."""

    name: str = "Gemini API"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-3.1-flash-lite",
    ) -> None:
        if api_key is None:
            _load_dotenv_if_present()
        self._api_key = (
            api_key.strip()
            if api_key is not None
            else os.environ.get("GEMINI_API_KEY", "").strip()
        )
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        if self._api_key:
            return True, "GEMINI_API_KEY configured"
        return False, "Missing GEMINI_API_KEY"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        if not self._api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        parts: list[dict[str, Any]] = [{"text": _build_observation_prompt(obs)}]
        is_region = obs.viewport_mode == "region"
        img_b64 = _encode_screenshot_b64(
            screenshot,
            max_width=960 if is_region else 1280,
            quality=86 if is_region else 78,
        )
        if img_b64:
            parts.append({"inlineData": {"mimeType": "image/jpeg", "data": img_b64}})

        candidate_models = [
            self.model,
            *(
                m
                for m in ("gemini-3.1-flash-lite", "gemini-3-flash-preview", "gemini-flash-latest")
                if m != self.model
            ),
        ]
        data: dict[str, Any] = {}
        for idx, model_name in enumerate(candidate_models):
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent"
            )
            try:
                resp = httpx.post(
                    url,
                    params={"key": self._api_key},
                    json={
                        "contents": [{"parts": parts}],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "temperature": 0.1,
                        },
                    },
                    timeout=45.0,
                )
                resp.raise_for_status()
            except httpx.TimeoutException:
                if idx + 1 < len(candidate_models):
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code in (404, 429, 500, 502, 503, 504)
                    and idx + 1 < len(candidate_models)
                ):
                    continue
                raise
            data = resp.json()
            break

        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        return _parse_action_json(
            raw_text,
            task_text=obs.task_text,
            active_window_title=obs.active_window_title,
        )


class OpenAIPlanner(BasePlanner):
    """OpenAI Chat Completions JSON planner using runtime OPENAI_API_KEY."""

    name: str = "OpenAI API"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        if api_key is None:
            _load_dotenv_if_present()
        self._api_key = (
            api_key.strip()
            if api_key is not None
            else os.environ.get("OPENAI_API_KEY", "").strip()
        )
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        if self._api_key:
            return True, "OPENAI_API_KEY configured"
        return False, "Missing OPENAI_API_KEY"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": _build_observation_prompt(obs)}
        ]
        is_region = obs.viewport_mode == "region"
        img_b64 = _encode_screenshot_b64(
            screenshot,
            max_width=960 if is_region else 1280,
            quality=86 if is_region else 78,
        )
        if img_b64:
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                }
            )

        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self.model,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "messages": [
                    {"role": "system", "content": _SYSTEM_INSTRUCTION},
                    {"role": "user", "content": user_content},
                ],
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["choices"][0]["message"]["content"]
        return _parse_action_json(
            raw_text,
            task_text=obs.task_text,
            active_window_title=obs.active_window_title,
        )


class OllamaPlanner(BasePlanner):
    """Local Ollama HTTP planner targeting http://127.0.0.1:11434."""

    name: str = "Local Ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3.5:latest",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def is_available(self) -> tuple[bool, str]:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            if resp.status_code == 200:
                return True, f"Ollama reachable at {self.base_url}"
            return False, f"Ollama returned HTTP {resp.status_code}"
        except Exception as exc:
            return False, f"Ollama unreachable: {exc}"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": _build_observation_prompt(obs),
            "format": "json",
            "stream": False,
            "think": False,
            "options": {"temperature": 0.1},
        }
        is_region = obs.viewport_mode == "region"
        img_b64 = _encode_screenshot_b64(
            screenshot,
            max_width=960 if is_region else 1280,
            quality=86 if is_region else 78,
        )
        if img_b64:
            payload["images"] = [img_b64]

        resp = httpx.post(
            f"{self.base_url}/api/generate",
            json=payload,
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["response"]
        return _parse_action_json(
            raw_text,
            task_text=obs.task_text,
            active_window_title=obs.active_window_title,
        )


_BROWSER_MEDIA_PATTERN = re.compile(
    r"(?i)\b(?:firefox|chrome|edge|msedge|browser|youtube|music)\b|https?://"
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


_SONG_VERB_PATTERN = re.compile(
    r"(?i)\b(?:turn on|put on|play|listen to|start playing)\b.*\bsong\b"
)


def is_browser_or_media_task(task_text: str) -> bool:
    """Return True if task_text targets a web browser, URL, or music/YouTube playback."""
    lower_task = task_text.lower()
    if _BROWSER_MEDIA_PATTERN.search(lower_task):
        return True
    if _SONG_VERB_PATTERN.search(lower_task):
        return True
    return any(
        phrase in lower_task
        for phrase in (
            "put on a song",
            "turn on a song",
            "play a song",
            "play song",
            "play music",
            "turn on music",
            "start playing",
            "listen to",
        )
    )


def _is_youtube_music_task(lower_task: str) -> bool:
    if "youtube music" in lower_task:
        return True
    if "youtube" in lower_task and ("music" in lower_task or "song" in lower_task):
        return True
    if _SONG_VERB_PATTERN.search(lower_task):
        return True
    return any(
        phrase in lower_task
        for phrase in (
            "put on a song",
            "turn on a song",
            "play a song",
            "play song",
            "play music",
            "turn on music",
            "start playing",
            "listen to",
        )
    )


def _extract_song_query(task_text: str) -> str:
    text = task_text.strip()
    song_wrap_match = re.search(
        r"(?i)\b(?:turn on|put on|play|listen to)\s+(?:a|an|some|the)?\s*(.+?)\s+song\b",
        text,
    )
    if song_wrap_match:
        candidate = song_wrap_match.group(1).strip(" .!?,\"'")
        for prefix in ("called ", "named ", "by "):
            if candidate.lower().startswith(prefix):
                candidate = candidate[len(prefix) :].strip(" .!?,\"'")
        if candidate and candidate.lower() not in {"a", "an", "the", "good", "random", "nice"}:
            return candidate

    match = re.search(
        r"(?i)(?:put on a song|turn on a song|play a song|play song|play music|turn on music|start playing|listen to|turn on|play)\s*(?:called|named|by|:|-)?\s*(.*)$",
        text,
    )
    if match:
        candidate = match.group(1).strip(" .!?,\"'")
        for prefix in ("called ", "named ", "by ", "a ", "an ", "the "):
            if candidate.lower().startswith(prefix):
                candidate = candidate[len(prefix) :].strip(" .!?,\"'")
        if candidate and candidate.lower() not in {
            "on youtube music",
            "on youtube",
            "in firefox",
            "in chrome",
            "in edge",
            "song",
            "music",
        }:
            return candidate
    return "lofi hip hop beats"


def locate_youtube_music_play_button(
    screenshot: Optional[Image.Image],
    active_window_rect: Optional[tuple[int, int, int, int]],
    screen_size: tuple[int, int],
    retry_index: int = 0,
) -> tuple[int, int]:
    """Locate the white 'Play' pill button on YouTube Music's Top Result card, or return a fallback coordinate."""
    sw = max(1, int(screen_size[0]))
    sh = max(1, int(screen_size[1]))

    if active_window_rect is not None:
        rl, rt, rr, rb = active_window_rect
        left = max(0, min(sw - 1, int(rl)))
        top = max(0, min(sh - 1, int(rt)))
        right = max(left + 1, min(sw, int(rr)))
        bottom = max(top + 1, min(sh, int(rb)))
    else:
        left, top, right, bottom = 0, 0, sw, sh

    width = max(1, right - left)
    height = max(1, bottom - top)

    sidebar_margin = min(235, int(width * 0.16)) if width >= 1100 else min(76, int(width * 0.12))
    x0 = max(0, left + sidebar_margin)
    x1 = min(sw, left + int(width * 0.58))
    y0 = max(0, top + int(height * 0.24))
    y1 = min(sh, top + int(height * 0.72))

    if screenshot is not None and screenshot.width >= sw // 2 and retry_index == 0:
        scale_x = screenshot.width / sw
        scale_y = screenshot.height / sh
        sx0 = max(0, min(screenshot.width - 1, int(round(x0 * scale_x))))
        sx1 = max(sx0 + 1, min(screenshot.width, int(round(x1 * scale_x))))
        sy0 = max(0, min(screenshot.height - 1, int(round(y0 * scale_y))))
        sy1 = max(sy0 + 1, min(screenshot.height, int(round(y1 * scale_y))))

        min_w = max(12, int(round(35 * scale_x)))
        max_w = max(min_w + 1, int(round(160 * scale_x)))
        min_h = max(8, int(round(16 * scale_y)))
        max_h = max(min_h + 1, int(round(60 * scale_y)))

        crop_w = sx1 - sx0
        crop_h = sy1 - sy0
        if crop_w > 0 and crop_h > 0:
            raw = screenshot.crop((sx0, sy0, sx1, sy1)).convert("RGB").tobytes()
            row_stride = crop_w * 3
            clusters: list[list[int]] = []  # [min_x, max_x, min_y, max_y]

            for ry in range(0, crop_h, 2):
                row_off = ry * row_stride
                run_start: Optional[int] = None
                runs_in_row: list[tuple[int, int]] = []
                for rx in range(crop_w):
                    idx = row_off + rx * 3
                    is_white = raw[idx] >= 220 and raw[idx + 1] >= 220 and raw[idx + 2] >= 220
                    if is_white:
                        if run_start is None:
                            run_start = rx
                    elif run_start is not None:
                        run_end = rx - 1
                        if min_w <= (run_end - run_start + 1) <= max_w:
                            runs_in_row.append((sx0 + run_start, sx0 + run_end))
                        run_start = None
                if run_start is not None:
                    run_end = crop_w - 1
                    if min_w <= (run_end - run_start + 1) <= max_w:
                        runs_in_row.append((sx0 + run_start, sx0 + run_end))

                abs_y = sy0 + ry
                for r_x0, r_x1 in runs_in_row:
                    matched_cluster: Optional[list[int]] = None
                    for cluster in clusters:
                        if (abs_y - cluster[3]) <= 4 and not (r_x1 < cluster[0] or r_x0 > cluster[1]):
                            matched_cluster = cluster
                            break
                    if matched_cluster is not None:
                        matched_cluster[0] = min(matched_cluster[0], r_x0)
                        matched_cluster[1] = max(matched_cluster[1], r_x1)
                        matched_cluster[3] = abs_y
                    else:
                        clusters.append([r_x0, r_x1, abs_y, abs_y])

            pill_clusters = [
                c
                for c in clusters
                if min_w <= (c[1] - c[0] + 1) <= max_w and min_h <= (c[3] - c[2] + 1) <= max_h
            ]
            if pill_clusters:
                pill = min(pill_clusters, key=lambda c: (c[2], c[0]))
                scx = (pill[0] + pill[1]) / 2.0
                scy = (pill[2] + pill[3]) / 2.0
                cx = max(10, min(sw - 10, int(round(scx / scale_x))))
                cy = max(10, min(sh - 10, int(round(scy / scale_y))))
                return cx, cy

    if retry_index >= 1:
        raw_x = left + max(180, int(width * 0.22))
        raw_y = top + max(220, int(height * 0.36))
    else:
        raw_x = left + max(180, int(width * 0.23))
        raw_y = top + max(240, int(height * 0.46))

    return max(10, min(sw - 10, raw_x)), max(10, min(sh - 10, raw_y))


class LocalDemoFallbackPlanner(BasePlanner):
    """Deterministic local planner supporting Milestone 4 Notepad demo, browser/media, and workspace tasks."""

    name: str = "Local Demo / Fallback"

    _NOTEPAD_MESSAGE = "FunGuys Autonomous Desktop Controller - Milestone 4 Live Verification"

    def is_available(self) -> tuple[bool, str]:
        return True, "Ready (Local Demo)"

    def plan_next_step(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        lower_task = obs.task_text.lower()
        if "notepad" in lower_task or "milestone 4" in lower_task:
            return self._plan_notepad_demo(obs)

        if is_browser_or_media_task(obs.task_text):
            return self._plan_browser_or_media_task(obs, screenshot=screenshot)

        if "verify" in lower_task or "workspace" in lower_task:
            if obs.step_index == 0:
                return DesktopAction(
                    action_type=ActionType.verify_state,
                    target_path=obs.approved_workspace,
                    description="Verify approved_workspace directory exists",
                )
            return DesktopAction(
                action_type=ActionType.done,
                description="Verified approved_workspace contents",
            )

        if obs.step_index == 0:
            return DesktopAction(
                action_type=ActionType.wait,
                seconds=0.1,
                description=f"Observe desktop state for task: {obs.task_text[:80]}",
            )
        return DesktopAction(
            action_type=ActionType.done,
            description=f"Completed local demo execution for: {obs.task_text[:80]}",
        )

    def _plan_browser_or_media_task(
        self,
        obs: ObservationContext,
        screenshot: Optional[Image.Image] = None,
    ) -> DesktopAction:
        lower_task = obs.task_text.lower()
        if re.search(r"\b(msedge|edge)\b", lower_task):
            browser = "msedge"
        elif re.search(r"\bchrome\b", lower_task):
            browser = "chrome"
        else:
            browser = "firefox"

        if _is_youtube_music_task(lower_task):
            query = _extract_song_query(obs.task_text)
            target_url = f"https://music.youtube.com/search?q={urllib.parse.quote_plus(query)}"
            play_x, play_y = locate_youtube_music_play_button(
                screenshot,
                obs.active_window_rect,
                obs.screen_size,
            )

            steps: list[DesktopAction] = [
                DesktopAction(
                    action_type=ActionType.launch_app,
                    app_name=browser,
                    description=f"Launch {browser} browser",
                ),
                DesktopAction(
                    action_type=ActionType.wait,
                    seconds=2.0,
                    description=f"Wait for {browser} window to initialize",
                ),
                DesktopAction(
                    action_type=ActionType.press_keys,
                    keys=["ctrl", "l"],
                    description="Focus browser address bar",
                ),
                DesktopAction(
                    action_type=ActionType.type_text,
                    text=target_url,
                    description=f"Enter YouTube Music URL for: {query}",
                ),
                DesktopAction(
                    action_type=ActionType.press_keys,
                    keys=["enter"],
                    description="Navigate to YouTube Music search results",
                ),
                DesktopAction(
                    action_type=ActionType.wait,
                    seconds=4.0,
                    description="Wait for YouTube Music results to load",
                ),
                DesktopAction(
                    action_type=ActionType.move,
                    x=play_x,
                    y=play_y,
                    description="Move cursor to YouTube Music Play button",
                ),
                DesktopAction(
                    action_type=ActionType.click,
                    x=play_x,
                    y=play_y,
                    description="Click Play button to start song on YouTube Music",
                ),
                DesktopAction(
                    action_type=ActionType.verify_state,
                    expected_text=" - YouTube Music",
                    description="Verify song is actively playing in YouTube Music",
                ),
                DesktopAction(
                    action_type=ActionType.done,
                    description=f"Verified song playback active on YouTube Music ({query})",
                ),
            ]
        else:
            url_match = _URL_PATTERN.search(obs.task_text)
            if url_match:
                target_url = url_match.group(0).rstrip(".,;:)]}")
            else:
                cleaned = re.sub(
                    r"(?i)\b(?:open|launch|start|use|in|and|then|firefox|chrome|msedge|edge|browser|go to|navigate to|search for|search)\b",
                    " ",
                    obs.task_text,
                )
                cleaned = re.sub(r"\s+", " ", cleaned).strip(" .!?,\"'")
                if cleaned:
                    target_url = f"https://www.google.com/search?q={urllib.parse.quote_plus(cleaned)}"
                else:
                    target_url = "https://www.google.com"

            browser_verify_text = "edge" if browser == "msedge" else browser
            steps = [
                DesktopAction(
                    action_type=ActionType.launch_app,
                    app_name=browser,
                    description=f"Launch {browser} browser",
                ),
                DesktopAction(
                    action_type=ActionType.wait,
                    seconds=2.0,
                    description=f"Wait for {browser} window to initialize",
                ),
                DesktopAction(
                    action_type=ActionType.press_keys,
                    keys=["ctrl", "l"],
                    description="Focus browser address bar",
                ),
                DesktopAction(
                    action_type=ActionType.type_text,
                    text=target_url,
                    description=f"Type URL: {target_url}",
                ),
                DesktopAction(
                    action_type=ActionType.press_keys,
                    keys=["enter"],
                    description="Navigate to target URL",
                ),
                DesktopAction(
                    action_type=ActionType.wait,
                    seconds=3.0,
                    description="Wait for page to load",
                ),
                DesktopAction(
                    action_type=ActionType.verify_state,
                    expected_text=browser_verify_text,
                    description=f"Verify {browser} browser window is active",
                ),
                DesktopAction(
                    action_type=ActionType.done,
                    description=f"Opened {browser} and navigated to {target_url}",
                ),
            ]

        if obs.step_index < len(steps):
            return steps[obs.step_index]
        return steps[-1]

    def _plan_notepad_demo(self, obs: ObservationContext) -> DesktopAction:
        if obs.active_window_rect is not None:
            left, top, right, bottom = obs.active_window_rect
            cx = max(1, (left + right) // 2)
            cy = max(1, (top + bottom) // 2)
        else:
            cx, cy = 520, 420

        demo_file = str(Path(obs.approved_workspace) / "milestone4_demo.txt")
        steps: list[DesktopAction] = [
            DesktopAction(
                action_type=ActionType.launch_app,
                app_name="notepad",
                description="Launch Windows Notepad",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for Notepad window to initialize",
            ),
            DesktopAction(
                action_type=ActionType.move,
                x=cx,
                y=cy,
                description="Move cursor smoothly into Notepad editor",
            ),
            DesktopAction(
                action_type=ActionType.click,
                x=cx,
                y=cy,
                description="Focus Notepad text editor area",
            ),
            DesktopAction(
                action_type=ActionType.type_text,
                text=self._NOTEPAD_MESSAGE,
                description="Type verification message in Notepad",
            ),
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=["ctrl", "s"],
                description="Open Save dialog in Notepad",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for Save As dialog",
            ),
            DesktopAction(
                action_type=ActionType.type_text,
                text=demo_file,
                description="Enter approved_workspace file path",
            ),
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=["enter"],
                description="Save milestone4_demo.txt inside approved_workspace",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for file save to complete",
            ),
            DesktopAction(
                action_type=ActionType.press_keys,
                keys=["alt", "f4"],
                description="Close Notepad before reopening verification",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.5,
                description="Wait for Notepad to close",
            ),
            DesktopAction(
                action_type=ActionType.launch_app,
                app_name="notepad",
                target_path=demo_file,
                description="Reopen saved milestone4_demo.txt in Notepad",
            ),
            DesktopAction(
                action_type=ActionType.wait,
                seconds=0.8,
                description="Wait for reopened Notepad file to render",
            ),
            DesktopAction(
                action_type=ActionType.verify_state,
                target_path=demo_file,
                expected_text=self._NOTEPAD_MESSAGE,
                description="Verify saved file exists in approved_workspace and contents match",
            ),
            DesktopAction(
                action_type=ActionType.done,
                description="Milestone 4 Notepad demonstration completed and verified",
            ),
        ]

        if obs.step_index < len(steps):
            return steps[obs.step_index]
        return steps[-1]


def create_planner(backend_name: str, api_key: Optional[str] = None) -> BasePlanner:
    """Instantiate a planner backend by display name or short alias."""
    normalized = backend_name.strip().lower()
    if normalized in {"local demo / fallback", "local", "fallback", "demo"}:
        return LocalDemoFallbackPlanner()
    if normalized in {"gemini api", "gemini"}:
        return GeminiPlanner(api_key=api_key)
    if normalized in {"openai api", "openai"}:
        return OpenAIPlanner(api_key=api_key)
    if normalized in {"local ollama", "ollama"}:
        return OllamaPlanner()
    if normalized in {"antigravity + ollama", "antigravity", "hybrid"}:
        planner = OllamaPlanner()
        planner.name = "Antigravity + Ollama"
        return planner
    raise ValueError(f"Unknown planner backend: {backend_name!r}")
