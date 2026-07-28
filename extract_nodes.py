from __future__ import annotations

import csv
import math
import argparse
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR
from scipy.optimize import linear_sum_assignment


OCR_ENGINE = RapidOCR()
NUMBER_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?$")


@dataclass
class Candidate:
    x: float
    y: float
    area: float
    circularity: float
    score: float
    source: str


@dataclass
class OCRItem:
    text: str
    value: float
    score: float
    box: np.ndarray
    center: np.ndarray


@dataclass
class PointValue:
    point_id: int
    point_x: float
    point_y: float
    label_text: str
    label_value: float
    label_x: float
    label_y: float
    label_score: float
    outer_x: float
    outer_y: float
    anchor_x: float
    anchor_y: float
    anchor_distance: float
    leader_length: float
    source: str


@dataclass
class OrderOptions:
    direction: str = "cw"
    start: str = "top"


def sort_points(points: list[tuple[float, float]], row_tol: float = 18.0) -> list[tuple[float, float]]:
    if not points:
        return []
    points = sorted(points, key=lambda p: (p[1], p[0]))
    rows: list[list[tuple[float, float]]] = []
    current = [points[0]]
    for pt in points[1:]:
        if abs(pt[1] - current[-1][1]) <= row_tol:
            current.append(pt)
        else:
            rows.append(sorted(current, key=lambda p: p[0]))
            current = [pt]
    rows.append(sorted(current, key=lambda p: p[0]))
    out: list[tuple[float, float]] = []
    for row in rows:
        out.extend(row)
    return out


def contour_from_mask(part_mask: np.ndarray) -> np.ndarray:
    contours, _ = cv2.findContours(part_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return np.empty((0, 2), dtype=np.float32)
    contour = max(contours, key=cv2.contourArea)
    return contour[:, 0, :].astype(np.float32)


def polygon_center(contour: np.ndarray) -> np.ndarray:
    if contour.size == 0:
        return np.array([0.0, 0.0], dtype=np.float32)
    m = cv2.moments(contour.reshape(-1, 1, 2))
    if abs(m["m00"]) > 1e-6:
        return np.array([m["m10"] / m["m00"], m["m01"] / m["m00"]], dtype=np.float32)
    return contour.mean(axis=0).astype(np.float32)


def contour_signed_area(contour: np.ndarray) -> float:
    if contour.shape[0] < 3:
        return 0.0
    x = contour[:, 0]
    y = contour[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def contour_start_index(contour: np.ndarray, start: str) -> int:
    if contour.size == 0:
        return 0
    start = (start or "top").lower()
    if start == "bottom":
        score = contour[:, 1]
    elif start == "left":
        score = -contour[:, 0]
    elif start == "right":
        score = contour[:, 0]
    else:
        score = -contour[:, 1]
    return int(np.argmax(score))


def order_candidates_by_contour(
    candidates: list[Candidate],
    contour: np.ndarray,
    order_options: OrderOptions,
) -> list[Candidate]:
    if len(candidates) <= 1 or contour.size == 0 or order_options.direction == "none":
        return sort_candidates(candidates)

    contour_xy = contour.astype(np.float32)
    signed_area = contour_signed_area(contour_xy)
    contour_is_cw = signed_area > 0.0  # image coordinates: positive area means clockwise
    desired_cw = (order_options.direction or "cw").lower() != "ccw"

    start_idx = contour_start_index(contour_xy, order_options.start)
    if desired_cw == contour_is_cw:
        contour_order = np.concatenate([np.arange(start_idx, len(contour_xy)), np.arange(0, start_idx)])
    else:
        contour_order = np.concatenate([np.arange(start_idx, -1, -1), np.arange(len(contour_xy) - 1, start_idx, -1)])

    rank_map = np.empty(len(contour_xy), dtype=np.int32)
    rank_map[contour_order] = np.arange(len(contour_order), dtype=np.int32)

    indexed: list[tuple[int, float, float, Candidate]] = []
    for cand in candidates:
        point = np.array([[cand.x, cand.y]], dtype=np.float32)
        dists = np.sum((contour_xy - point) ** 2, axis=1)
        contour_idx = int(np.argmin(dists))
        indexed.append((int(rank_map[contour_idx]), cand.y, cand.x, cand))
    indexed.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in indexed]


def label_side(anchor: np.ndarray, center: np.ndarray) -> str:
    dx = float(anchor[0] - center[0])
    dy = float(anchor[1] - center[1])
    if abs(dy) > abs(dx) * 1.15:
        return "bottom" if dy > 0 else "top"
    return "right" if dx > 0 else "left"


def point_box_distance(point: np.ndarray, box: np.ndarray, margin: float = 12.0) -> float:
    x1, y1 = box.min(axis=0) - margin
    x2, y2 = box.max(axis=0) + margin
    px, py = float(point[0]), float(point[1])
    dx = max(float(x1 - px), 0.0, float(px - x2))
    dy = max(float(y1 - py), 0.0, float(py - y2))
    return math.hypot(dx, dy)


def infer_anchor(label_center: np.ndarray, contour: np.ndarray, part_center: np.ndarray) -> tuple[int, np.ndarray]:
    label_vec = label_center - part_center
    label_radius = float(np.linalg.norm(label_vec)) + 1e-6
    point_vecs = contour - part_center
    point_radii = np.linalg.norm(point_vecs, axis=1) + 1e-6
    cosine = (point_vecs @ label_vec) / (point_radii * label_radius)
    distance = np.linalg.norm(contour - label_center, axis=1)
    score = distance + 110.0 * (1.0 - cosine)
    best_idx = int(np.argmin(score))
    return best_idx, contour[best_idx]


def _largest_component(mask: np.ndarray) -> np.ndarray:
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if num_labels <= 1:
        return mask
    best = 1
    best_area = 0
    h, w = mask.shape[:2]
    min_area = h * w * 0.05
    for i in range(1, num_labels):
        area = stats[i, cv2.CC_STAT_AREA]
        if area > best_area and area >= min_area:
            best_area = area
            best = i
    return np.where(labels == best, 255, 0).astype(np.uint8)


def normalize_ocr_text(text: str) -> str:
    normalized = str(text or "").strip()
    return (
        normalized.replace("閳?", "-")
        .replace("鈥?", "-")
        .replace("鈭?", "-")
        .replace("涓€", "-")
    )


def recover_numeric_text_with_sign(image: np.ndarray, box: np.ndarray, raw_text: str) -> str:
    text = normalize_ocr_text(raw_text)
    if not text or NUMBER_RE.match(text) is None:
        return text
    if text.startswith(("-", "+")):
        return text

    x1 = max(0, int(np.floor(box[:, 0].min())))
    y1 = max(0, int(np.floor(box[:, 1].min())))
    x2 = min(image.shape[1], int(np.ceil(box[:, 0].max())))
    y2 = min(image.shape[0], int(np.ceil(box[:, 1].max())))
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)

    rx1 = max(0, x1 - max(2, int(round(0.12 * w))))
    rx2 = min(image.shape[1], x1 + max(6, int(round(0.28 * w))))
    ry1 = max(0, y1 + int(round(0.32 * h)))
    ry2 = min(image.shape[0], y1 + int(round(0.68 * h)))
    if rx2 <= rx1 or ry2 <= ry1:
        return text

    roi = image[ry1:ry2, rx1:rx2]
    if roi.size == 0:
        return text
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi.copy()
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, _labels, stats, centroids = cv2.connectedComponentsWithStats(binary, 8)
    band_h = max(1, ry2 - ry1)
    band_w = max(1, rx2 - rx1)
    for idx in range(1, n):
        x, y, ww, hh, area = stats[idx]
        if area < 4:
            continue
        if ww < max(3, int(round(0.10 * w))):
            continue
        if hh > max(5, int(round(0.35 * h))):
            continue
        if ww < hh * 1.8:
            continue
        cx, cy = centroids[idx]
        if cx > band_w * 0.9:
            continue
        if cy < band_h * 0.15 or cy > band_h * 0.85:
            continue
        return f"-{text}"
    return text


def extract_numeric_ocr(path: Path) -> list[OCRItem]:
    image = cv2.imread(str(path))
    if image is None:
        raise FileNotFoundError(path)
    raw, _ = OCR_ENGINE(str(path))
    items: list[OCRItem] = []
    if not raw:
        return items
    for box, text, score in raw:
        if float(score) < 0.90:
            continue
        box_array = np.array(box, dtype=np.float32)
        text = recover_numeric_text_with_sign(image, box_array, text)
        if NUMBER_RE.match(text) is None or "." not in text:
            continue
        items.append(
            OCRItem(
                text=text,
                value=float(text),
                score=float(score),
                box=box_array,
                center=box_array.mean(axis=0),
            )
        )
    return items


def extract_part_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    # Keep the colorful CAE contour region and suppress white UI/background.
    mask = np.zeros(h.shape, dtype=np.uint8)
    mask[((s > 35) & (v > 35)) | ((h > 30) & (h < 130) & (s > 20) & (v > 20))] = 255
    mask = cv2.medianBlur(mask, 5)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
    return _largest_component(mask)


def detect_explicit_markers(bgr: np.ndarray, part_mask: np.ndarray) -> list[Candidate]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    image_area = bgr.shape[0] * bgr.shape[1]
    min_area = max(120, int(image_area * 0.00012))
    max_area = max(min_area + 1, int(image_area * 0.0015))

    masks = []
    red1 = cv2.inRange(hsv, np.array([0, 120, 120], dtype=np.uint8), np.array([12, 255, 255], dtype=np.uint8))
    red2 = cv2.inRange(hsv, np.array([168, 120, 120], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
    masks.append(("explicit-red", red1 | red2))

    yellow = cv2.inRange(hsv, np.array([16, 100, 120], dtype=np.uint8), np.array([42, 255, 255], dtype=np.uint8))
    masks.append(("explicit-yellow", yellow))
    
    # Magenta / pink explicit markers (BGR ~ (255,0,255))
    magenta = cv2.inRange(hsv, np.array([140, 120, 120], dtype=np.uint8), np.array([170, 255, 255], dtype=np.uint8))
    masks.append(("explicit-magenta", magenta))

    candidates: list[Candidate] = []
    for source, raw_mask in masks:
        mask = cv2.bitwise_and(raw_mask, part_mask)
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=1,
        )
        num_labels, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        for i in range(1, num_labels):
            area = float(stats[i, cv2.CC_STAT_AREA])
            if area < min_area or area > max_area:
                continue
            x, y = map(float, centroids[i])
            candidates.append(Candidate(x=x, y=y, area=area, circularity=1.0, score=area, source=source))

    return sort_candidates(candidates)


def build_explicit_marker_mask(bgr: np.ndarray, part_mask: np.ndarray | None = None) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red1 = cv2.inRange(hsv, np.array([0, 120, 120], dtype=np.uint8), np.array([12, 255, 255], dtype=np.uint8))
    red2 = cv2.inRange(hsv, np.array([168, 120, 120], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
    yellow = cv2.inRange(hsv, np.array([16, 100, 120], dtype=np.uint8), np.array([42, 255, 255], dtype=np.uint8))
    magenta = cv2.inRange(hsv, np.array([140, 120, 120], dtype=np.uint8), np.array([170, 255, 255], dtype=np.uint8))
    mask = red1 | red2 | yellow | magenta
    if part_mask is not None:
        mask = cv2.bitwise_and(mask, part_mask)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    return mask


def build_red_leader_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    red1 = cv2.inRange(hsv, np.array([0, 120, 120], dtype=np.uint8), np.array([12, 255, 255], dtype=np.uint8))
    red2 = cv2.inRange(hsv, np.array([168, 120, 120], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
    red_mask = red1 | red2
    red_mask = cv2.morphologyEx(
        red_mask,
        cv2.MORPH_DILATE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
        iterations=1,
    )
    return red_mask


def detect_red_guide_segments(image: np.ndarray) -> list[dict]:
    red_mask = build_red_leader_mask(image)
    lines = cv2.HoughLinesP(red_mask, 1, np.pi / 180, threshold=24, minLineLength=36, maxLineGap=36)
    if lines is None:
        return []
    segments: list[dict] = []
    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = map(int, line)
        length = float(math.hypot(x2 - x1, y2 - y1))
        if length < 36.0:
            continue
        p1 = np.array([x1, y1], dtype=np.float32)
        p2 = np.array([x2, y2], dtype=np.float32)
        direction = p2 - p1
        norm = float(np.linalg.norm(direction))
        if norm > 1e-6:
            direction /= norm
        segments.append(
            {
                "p1": p1,
                "p2": p2,
                "dir": direction,
                "length": length,
            }
        )
    return segments


def build_magenta_leader_mask(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    magenta = cv2.inRange(hsv, np.array([140, 120, 120], dtype=np.uint8), np.array([170, 255, 255], dtype=np.uint8))
    magenta = cv2.morphologyEx(
        magenta,
        cv2.MORPH_DILATE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
        iterations=1,
    )
    return magenta


def build_dark_leader_mask(bgr: np.ndarray, part_mask: np.ndarray | None = None) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    dark = ((hsv[:, :, 1] < 75) & (gray < 125)).astype(np.uint8) * 255
    if part_mask is not None:
        ring = np.zeros_like(part_mask)
        contour = contour_from_mask(part_mask)
        if contour.size > 0:
            cv2.drawContours(ring, [contour.reshape(-1, 1, 2).astype(np.int32)], -1, 255, 10)
        allowed = ((part_mask == 0) | (ring > 0)).astype(np.uint8) * 255
        dark = cv2.bitwise_and(dark, allowed)
    dark = cv2.morphologyEx(
        dark,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    dark = cv2.morphologyEx(
        dark,
        cv2.MORPH_DILATE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    return dark


def detect_dark_guide_segments(image: np.ndarray, part_mask: np.ndarray) -> list[dict]:
    dark_mask = build_dark_leader_mask(image, part_mask)
    lines = cv2.HoughLinesP(dark_mask, 1, np.pi / 180, threshold=20, minLineLength=28, maxLineGap=24)
    if lines is None:
        return []
    segments: list[dict] = []
    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = map(int, line)
        length = float(math.hypot(x2 - x1, y2 - y1))
        if length < 28.0:
            continue
        p1 = np.array([x1, y1], dtype=np.float32)
        p2 = np.array([x2, y2], dtype=np.float32)
        direction = p2 - p1
        norm = float(np.linalg.norm(direction))
        if norm > 1e-6:
            direction /= norm
        segments.append({"p1": p1, "p2": p2, "dir": direction, "length": length})
    return segments


def detect_magenta_guide_segments(image: np.ndarray) -> list[dict]:
    mag_mask = build_magenta_leader_mask(image)
    lines = cv2.HoughLinesP(mag_mask, 1, np.pi / 180, threshold=24, minLineLength=36, maxLineGap=36)
    if lines is None:
        return []
    segments: list[dict] = []
    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = map(int, line)
        length = float(math.hypot(x2 - x1, y2 - y1))
        if length < 36.0:
            continue
        p1 = np.array([x1, y1], dtype=np.float32)
        p2 = np.array([x2, y2], dtype=np.float32)
        direction = p2 - p1
        norm = float(np.linalg.norm(direction))
        if norm > 1e-6:
            direction /= norm
        segments.append(
            {
                "p1": p1,
                "p2": p2,
                "dir": direction,
                "length": length,
            }
        )
    return segments


def point_to_segment_distance(point: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    seg = b - a
    seg_len2 = float(seg @ seg)
    if seg_len2 <= 1e-6:
        return float(np.linalg.norm(point - a))
    t = float(np.clip(((point - a) @ seg) / seg_len2, 0.0, 1.0))
    proj = a + t * seg
    return float(np.linalg.norm(point - proj))


def merge_red_guide_segments(segments: list[dict]) -> list[dict]:
    if not segments:
        return []
    merged: list[dict] = []
    for seg in sorted(segments, key=lambda s: s["length"], reverse=True):
        matched = False
        for cur in merged:
            dots = []
            if "dir" in seg and "dir" in cur:
                dots.append(abs(float(seg["dir"] @ cur["dir"])))
                dots.append(abs(float(seg["dir"] @ (-cur["dir"]))))
            align_ok = (not dots) or max(dots) >= 0.94
            if not align_ok:
                continue
            endpoint_gap = min(
                float(np.linalg.norm(seg["p1"] - cur["p1"])),
                float(np.linalg.norm(seg["p1"] - cur["p2"])),
                float(np.linalg.norm(seg["p2"] - cur["p1"])),
                float(np.linalg.norm(seg["p2"] - cur["p2"])),
            )
            cross_gap = min(
                point_to_segment_distance(seg["p1"], cur["p1"], cur["p2"]),
                point_to_segment_distance(seg["p2"], cur["p1"], cur["p2"]),
                point_to_segment_distance(cur["p1"], seg["p1"], seg["p2"]),
                point_to_segment_distance(cur["p2"], seg["p1"], seg["p2"]),
            )
            if endpoint_gap > 85.0 and cross_gap > 18.0:
                continue

            pts = np.vstack([cur["p1"], cur["p2"], seg["p1"], seg["p2"]]).astype(np.float32)
            mean = pts.mean(axis=0)
            direction = cur.get("dir", seg.get("dir"))
            if direction is None or float(np.linalg.norm(direction)) <= 1e-6:
                direction = pts[-1] - pts[0]
                norm = float(np.linalg.norm(direction))
                direction = direction / norm if norm > 1e-6 else np.array([1.0, 0.0], dtype=np.float32)
            proj = (pts - mean) @ direction
            p1 = mean + direction * float(proj.min())
            p2 = mean + direction * float(proj.max())
            length = float(np.linalg.norm(p2 - p1))
            cur.update({"p1": p1, "p2": p2, "dir": direction, "length": length})
            matched = True
            break
        if not matched:
            merged.append(seg.copy())
    return merged


def detect_component_blobs(bgr: np.ndarray, part_mask: np.ndarray) -> list[Candidate]:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)

    # Blue/teal point candidates, deliberately narrower than the contour mask.
    blue = np.zeros(h.shape, dtype=np.uint8)
    blue[
        (h >= 80)
        & (h <= 120)
        & (s >= 70)
        & (v >= 60)
        & (v <= 245)
        & (part_mask > 0)
    ] = 255

    blue = cv2.medianBlur(blue, 3)
    blue = cv2.morphologyEx(
        blue,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )

    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(blue, 8)
    candidates: list[Candidate] = []
    for i in range(1, num_labels):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area < 4 or area > 250:
            continue
        x = float(centroids[i][0])
        y = float(centroids[i][1])

        x0 = max(0, int(stats[i, cv2.CC_STAT_LEFT]) - 2)
        y0 = max(0, int(stats[i, cv2.CC_STAT_TOP]) - 2)
        x1 = min(bgr.shape[1], x0 + int(stats[i, cv2.CC_STAT_WIDTH]) + 4)
        y1 = min(bgr.shape[0], y0 + int(stats[i, cv2.CC_STAT_HEIGHT]) + 4)
        roi = (labels[y0:y1, x0:x1] == i).astype(np.uint8) * 255
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        perimeter = float(cv2.arcLength(contours[0], True))
        if perimeter <= 0.0:
            continue
        circularity = 4.0 * math.pi * area / (perimeter * perimeter)
        if circularity < 0.20:
            continue

        # Strongly prefer small, compact, bright points rather than cloud-map regions.
        patch = bgr[max(0, int(y) - 2) : min(bgr.shape[0], int(y) + 3), max(0, int(x) - 2) : min(bgr.shape[1], int(x) + 3)]
        mean_b, mean_g, mean_r = patch.reshape(-1, 3).mean(axis=0)
        score = circularity * 2.0 + area * 0.01 + (mean_b - mean_g) * 0.02 + (mean_b - mean_r) * 0.01
        candidates.append(Candidate(x=x, y=y, area=area, circularity=circularity, score=score, source="blob"))

    return non_max_suppress(candidates, min_dist=6.0)


def detect_edge_peaks(bgr: np.ndarray, part_mask: np.ndarray) -> list[Candidate]:
    b, g, r = cv2.split(bgr)
    score = np.minimum(b, g).astype(np.int16) - r.astype(np.int16)
    score = np.clip(score, 0, 255).astype(np.uint8)

    edge = cv2.morphologyEx(
        part_mask,
        cv2.MORPH_GRADIENT,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
    )
    dist = cv2.distanceTransform(255 - edge, cv2.DIST_L2, 3)
    peaks = cv2.dilate(score, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    mask = (score == peaks) & (score >= 180) & (part_mask > 0) & (dist <= 25)

    candidates: list[Candidate] = []
    ys, xs = np.where(mask)
    for y, x in zip(ys.tolist(), xs.tolist()):
        patch = score[max(0, y - 3) : min(score.shape[0], y + 4), max(0, x - 3) : min(score.shape[1], x + 4)]
        area = float(np.count_nonzero(patch >= 140))
        candidates.append(
            Candidate(
                x=float(x),
                y=float(y),
                area=area,
                circularity=1.0,
                score=float(score[y, x]) + 0.25 * area,
                source="edge",
            )
        )

    return non_max_suppress(candidates, min_dist=15.0)


def contour_band_candidates(
    bgr: np.ndarray,
    part_mask: np.ndarray,
    kernel_size: int = 13,
    threshold: int = 18,
    band_width: int = 15,
    suppress: bool = True,
) -> list[Candidate]:
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    contours, _ = cv2.findContours(part_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return []
    contour = max(contours, key=cv2.contourArea)
    tophat = cv2.morphologyEx(
        gray,
        cv2.MORPH_TOPHAT,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size)),
    )
    band = np.zeros_like(part_mask)
    cv2.drawContours(band, [contour], -1, 255, band_width)
    mask = ((tophat >= threshold) & (band > 0)).astype(np.uint8) * 255
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    candidates: list[Candidate] = []
    for i in range(1, num_labels):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area < 5 or area > 160:
            continue
        x, y = map(float, centroids[i])
        score = float(tophat[int(round(y)), int(round(x))]) + 0.35 * area
        candidates.append(Candidate(x=x, y=y, area=area, circularity=1.0, score=score, source="band"))
    return non_max_suppress(candidates, min_dist=6.0) if suppress else sort_candidates(candidates)


def detect_leader_segments(image: np.ndarray, contour: np.ndarray) -> list[dict]:
    if contour.size == 0:
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    edges = cv2.Canny(gray, 80, 180, apertureSize=3, L2gradient=True)
    red1 = cv2.inRange(hsv, np.array([0, 120, 120], dtype=np.uint8), np.array([12, 255, 255], dtype=np.uint8))
    red2 = cv2.inRange(hsv, np.array([168, 120, 120], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
    red_mask = cv2.morphologyEx(
        red1 | red2,
        cv2.MORPH_DILATE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    edges = cv2.bitwise_or(edges, red_mask)

    ring = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(ring, [contour.reshape(-1, 1, 2).astype(np.int32)], -1, 255, 3)
    contour_distance = cv2.distanceTransform(255 - ring, cv2.DIST_L2, 3)

    part_mask = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(part_mask, [contour.reshape(-1, 1, 2).astype(np.int32)], -1, 255, -1)
    edges[(part_mask > 0) & (contour_distance > 8)] = 0

    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=16, minLineLength=10, maxLineGap=4)
    if lines is None:
        return []

    segments: list[dict] = []
    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = map(int, line)
        d1 = float(contour_distance[y1, x1])
        d2 = float(contour_distance[y2, x2])
        near1 = d1 <= 8.0
        near2 = d2 <= 8.0
        if near1 == near2:
            continue
        anchor = np.array((x1, y1) if near1 else (x2, y2), dtype=np.float32)
        outer = np.array((x2, y2) if near1 else (x1, y1), dtype=np.float32)
        length = float(math.hypot(x2 - x1, y2 - y1))
        if length < 10.0:
            continue
        dists = np.linalg.norm(contour - anchor, axis=1)
        contour_idx = int(np.argmin(dists))
        segments.append(
            {
                "anchor": anchor,
                "outer": outer,
                "length": length,
                "contour_idx": contour_idx,
                "contour_anchor": contour[contour_idx],
                "snap_distance": float(dists[contour_idx]),
                "line": (x1, y1, x2, y2),
            }
        )
    return segments


def merge_leader_segments(segments: list[dict]) -> list[dict]:
    if not segments:
        return []
    merged: list[dict] = []
    for segment in sorted(segments, key=lambda item: item["length"], reverse=True):
        matched = False
        for existing in merged:
            if (
                np.linalg.norm(segment["outer"] - existing["outer"]) <= 16.0
                and abs(segment["contour_idx"] - existing["contour_idx"]) <= 35
            ):
                if segment["length"] > existing["length"]:
                    existing.update(segment)
                matched = True
                break
        if not matched:
            merged.append(segment.copy())
    return merged


def extract_polyline_mask(image: np.ndarray, contour: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    line_mask = ((hsv[:, :, 1] < 55) & (gray > 95) & (gray < 248)).astype(np.uint8) * 255
    red1 = cv2.inRange(hsv, np.array([0, 120, 120], dtype=np.uint8), np.array([12, 255, 255], dtype=np.uint8))
    red2 = cv2.inRange(hsv, np.array([168, 120, 120], dtype=np.uint8), np.array([180, 255, 255], dtype=np.uint8))
    red_mask = cv2.morphologyEx(
        red1 | red2,
        cv2.MORPH_DILATE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        iterations=1,
    )
    line_mask = cv2.bitwise_or(line_mask, red_mask)
    edges = cv2.Canny(line_mask, 40, 120, apertureSize=3, L2gradient=True)

    part_mask = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(part_mask, [contour.reshape(-1, 1, 2).astype(np.int32)], -1, 255, -1)
    ring_mask = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(ring_mask, [contour.reshape(-1, 1, 2).astype(np.int32)], -1, 255, 10)

    edges[(part_mask > 0) & (ring_mask == 0)] = 0
    return cv2.dilate(edges, np.ones((2, 2), np.uint8), iterations=1)


def build_contour_distance(gray_shape: tuple[int, int], contour: np.ndarray) -> np.ndarray:
    ring = np.zeros(gray_shape, np.uint8)
    cv2.drawContours(ring, [contour.reshape(-1, 1, 2).astype(np.int32)], -1, 255, 3)
    return cv2.distanceTransform(255 - ring, cv2.DIST_L2, 3)


def _box_perimeter_seeds(box: np.ndarray, shape: tuple[int, int]) -> list[tuple[int, int]]:
    h, w = shape
    x1, y1 = np.floor(box.min(axis=0)).astype(int)
    x2, y2 = np.ceil(box.max(axis=0)).astype(int)
    x1 = max(0, x1 - 2)
    y1 = max(0, y1 - 2)
    x2 = min(w - 1, x2 + 2)
    y2 = min(h - 1, y2 + 2)

    seeds: list[tuple[int, int]] = []
    for x in range(x1, x2 + 1):
        seeds.append((x, y1))
        seeds.append((x, y2))
    for y in range(y1 + 1, y2):
        seeds.append((x1, y))
        seeds.append((x2, y))
    return seeds


def trace_polyline_anchor(
    item: OCRItem,
    contour: np.ndarray,
    part_center: np.ndarray,
    polyline_mask: np.ndarray,
    contour_distance: np.ndarray,
) -> dict | None:
    label_center = item.center
    radial_idx, radial_anchor = infer_anchor(label_center, contour, part_center)
    h, w = polyline_mask.shape
    seeds = [(x, y) for x, y in _box_perimeter_seeds(item.box, (h, w)) if polyline_mask[y, x] > 0]
    if not seeds:
        return None

    label_to_radial = radial_anchor - label_center
    radial_norm = float(np.linalg.norm(label_to_radial))
    if radial_norm < 1e-6:
        return None
    radial_dir = label_to_radial / radial_norm

    best: dict | None = None
    radial_side_name = label_side(radial_anchor, part_center)
    for seed_x, seed_y in seeds:
        seed = np.array([seed_x, seed_y], dtype=np.float32)
        seed_vec = seed - label_center
        seed_norm = float(np.linalg.norm(seed_vec))
        if seed_norm < 1.0:
            continue
        seed_dir = seed_vec / seed_norm
        if float(np.dot(seed_dir, radial_dir)) < 0.15:
            continue

        current = (seed_x, seed_y)
        visited = {current}
        path = [current]
        current_dir = seed_dir
        stalled = 0

        for _ in range(140):
            x, y = current
            if contour_distance[y, x] <= 5.0:
                dists = np.linalg.norm(contour - np.array([x, y], dtype=np.float32), axis=1)
                contour_idx = int(np.argmin(dists))
                contour_anchor = contour[contour_idx]
                if label_side(contour_anchor, part_center) != radial_side_name:
                    break
                cost = (
                    float(np.linalg.norm(contour_anchor - radial_anchor)) * 0.18
                    + len(path) * 0.03
                    + max(0.0, 1.0 - float(np.dot(current_dir, radial_dir))) * 6.0
                )
                candidate = {
                    "contour_idx": contour_idx,
                    "contour_anchor": contour_anchor,
                    "seed": seed,
                    "path": path.copy(),
                    "cost": cost,
                    "length": float(len(path)),
                }
                if best is None or candidate["cost"] < best["cost"]:
                    best = candidate
                break

            neighbors: list[tuple[float, tuple[int, int], np.ndarray]] = []
            for ny in range(max(0, y - 1), min(h, y + 2)):
                for nx in range(max(0, x - 1), min(w, x + 2)):
                    if nx == x and ny == y:
                        continue
                    if polyline_mask[ny, nx] == 0 or (nx, ny) in visited:
                        continue
                    step = np.array([nx - x, ny - y], dtype=np.float32)
                    step_norm = float(np.linalg.norm(step))
                    if step_norm < 1e-6:
                        continue
                    step_dir = step / step_norm
                    if float(np.dot(step_dir, radial_dir)) < -0.55:
                        continue
                    align = 1.0 - float(np.dot(step_dir, current_dir))
                    radial_pull = max(0.0, 1.0 - float(np.dot(step_dir, radial_dir)))
                    local_cost = align * 2.6 + radial_pull * 1.2 + step_norm * 0.05
                    neighbors.append((local_cost, (nx, ny), step_dir))
            if not neighbors:
                stalled += 1
                if stalled > 2:
                    break
                continue

            neighbors.sort(key=lambda item_: item_[0])
            _, nxt, nxt_dir = neighbors[0]
            visited.add(nxt)
            path.append(nxt)
            current = nxt
            current_dir = nxt_dir

    if best is None:
        return None
    if float(np.linalg.norm(best["contour_anchor"] - radial_anchor)) > 70.0:
        return None
    return best


def assign_leader_segments(
    ocr_items: list[OCRItem],
    contour: np.ndarray,
    part_center: np.ndarray,
    leader_segments: list[dict],
) -> dict[int, dict]:
    if not ocr_items or not leader_segments:
        return {}
    radial_anchors = []
    for item in ocr_items:
        radial_idx, radial_anchor = infer_anchor(item.center, contour, part_center)
        radial_anchors.append({"idx": radial_idx, "anchor": radial_anchor})

    cost_matrix = np.full((len(ocr_items), len(leader_segments)), 999.0, dtype=np.float32)
    for item_idx, item in enumerate(ocr_items):
        label_side_name = label_side(item.center, part_center)
        radial_anchor = radial_anchors[item_idx]["anchor"]
        for seg_idx, segment in enumerate(leader_segments):
            outer_box_distance = point_box_distance(segment["outer"], item.box, margin=12.0)
            if outer_box_distance > 70.0:
                continue
            contour_anchor = segment["contour_anchor"]
            anchor_side_name = label_side(contour_anchor, part_center)
            outer_center_distance = float(np.linalg.norm(segment["outer"] - item.center))
            radial_distance = float(np.linalg.norm(contour_anchor - radial_anchor))
            label_to_anchor = contour_anchor - item.center
            seg_direction = segment["anchor"] - segment["outer"]
            align_penalty = 0.0
            norm_1 = float(np.linalg.norm(label_to_anchor))
            norm_2 = float(np.linalg.norm(seg_direction))
            if norm_1 > 1e-6 and norm_2 > 1e-6:
                align_penalty = 1.0 - float(np.dot(label_to_anchor, seg_direction) / (norm_1 * norm_2))
            side_penalty = 0.0 if label_side_name == anchor_side_name else 18.0
            snap_penalty = max(0.0, segment["snap_distance"] - 2.0) * 1.5
            length_bonus = min(segment["length"], 60.0) * 0.08
            cost_matrix[item_idx, seg_idx] = (
                outer_box_distance
                + outer_center_distance * 0.08
                + radial_distance * 0.20
                + align_penalty * 18.0
                + side_penalty
                + snap_penalty
                - length_bonus
            )

    row_ind, col_ind = linear_sum_assignment(cost_matrix)
    assignments: dict[int, dict] = {}
    for item_idx, seg_idx in zip(row_ind, col_ind):
        score = float(cost_matrix[item_idx, seg_idx])
        if score > 45.0:
            continue
        assignments[item_idx] = {"score": score, "segment": leader_segments[seg_idx]}
    return assignments


def assign_values_via_red_connectivity(
    image: np.ndarray,
    points: list[Candidate],
    ocr_items: list[OCRItem],
    part_mask: np.ndarray,
) -> list[PointValue]:
    if not points or not ocr_items:
        return []

    red_mask = build_red_leader_mask(image)
    marker_mask = build_explicit_marker_mask(image, part_mask)
    combined = cv2.bitwise_or(red_mask, marker_mask)
    combined = cv2.morphologyEx(
        combined,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)),
        iterations=1,
    )
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(combined, 8)
    if num_labels <= 1:
        return []

    point_hits: dict[int, list[int]] = {}
    for point_idx, point in enumerate(points):
        x = int(round(point.x))
        y = int(round(point.y))
        x0 = max(0, x - 18)
        y0 = max(0, y - 18)
        x1 = min(labels.shape[1], x + 19)
        y1 = min(labels.shape[0], y + 19)
        patch = labels[y0:y1, x0:x1]
        vals, counts = np.unique(patch[patch > 0], return_counts=True)
        keep = [int(v) for v, c in zip(vals, counts) if c >= 20]
        if keep:
            point_hits[point_idx] = keep

    label_hits: dict[int, list[int]] = {}
    for item_idx, item in enumerate(ocr_items):
        x1, y1 = np.floor(item.box.min(axis=0)).astype(int)
        x2, y2 = np.ceil(item.box.max(axis=0)).astype(int)
        x1 = max(0, x1 - 14)
        y1 = max(0, y1 - 14)
        x2 = min(labels.shape[1] - 1, x2 + 14)
        y2 = min(labels.shape[0] - 1, y2 + 14)
        perimeter = []
        for x in range(x1, x2 + 1):
            perimeter.append((x, y1))
            perimeter.append((x, y2))
        for y in range(y1 + 1, y2):
            perimeter.append((x1, y))
            perimeter.append((x2, y))
        vals = [int(labels[y, x]) for x, y in perimeter if labels[y, x] > 0]
        if not vals:
            continue
        uniq, counts = np.unique(np.array(vals, dtype=np.int32), return_counts=True)
        keep = [int(v) for v, c in zip(uniq, counts) if c >= 3]
        if keep:
            label_hits[item_idx] = keep

    component_to_points: dict[int, list[int]] = {}
    for point_idx, comps in point_hits.items():
        for comp in comps:
            component_to_points.setdefault(comp, []).append(point_idx)
    component_to_labels: dict[int, list[int]] = {}
    for item_idx, comps in label_hits.items():
        for comp in comps:
            component_to_labels.setdefault(comp, []).append(item_idx)

    rows: list[PointValue] = []
    used_points: set[int] = set()
    used_items: set[int] = set()
    for comp in sorted(component_to_points.keys()):
        point_ids = [idx for idx in component_to_points.get(comp, []) if idx not in used_points]
        item_ids = [idx for idx in component_to_labels.get(comp, []) if idx not in used_items]
        if not point_ids or not item_ids:
            continue

        # Pick the strongest point in the component and the nearest label to it.
        point_ids.sort(key=lambda idx: points[idx].score, reverse=True)
        point_idx = point_ids[0]
        point = points[point_idx]
        item_idx = min(
            item_ids,
            key=lambda idx: math.hypot(float(ocr_items[idx].center[0] - point.x), float(ocr_items[idx].center[1] - point.y)),
        )
        item = ocr_items[item_idx]
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(point.x),
                point_y=float(point.y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(item.center[0]),
                outer_y=float(item.center[1]),
                anchor_x=float(point.x),
                anchor_y=float(point.y),
                anchor_distance=0.0,
                leader_length=float(stats[comp, cv2.CC_STAT_AREA]) ** 0.5,
                source="red-connect",
            )
        )
        used_points.add(point_idx)
        used_items.add(item_idx)
    return sorted(rows, key=lambda row: row.point_id)


def assign_values_via_magenta_connectivity(
    image: np.ndarray,
    points: list[Candidate],
    ocr_items: list[OCRItem],
    part_mask: np.ndarray,
) -> list[PointValue]:
    if not points or not ocr_items:
        return []

    mag_mask = build_magenta_leader_mask(image)
    marker_mask = build_explicit_marker_mask(image, part_mask)
    combined = cv2.bitwise_or(mag_mask, marker_mask)
    combined = cv2.morphologyEx(
        combined,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)),
        iterations=1,
    )
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(combined, 8)
    if num_labels <= 1:
        return []

    point_hits: dict[int, list[int]] = {}
    for point_idx, point in enumerate(points):
        x = int(round(point.x))
        y = int(round(point.y))
        x0 = max(0, x - 18)
        y0 = max(0, y - 18)
        x1 = min(labels.shape[1], x + 19)
        y1 = min(labels.shape[0], y + 19)
        patch = labels[y0:y1, x0:x1]
        vals, counts = np.unique(patch[patch > 0], return_counts=True)
        keep = [int(v) for v, c in zip(vals, counts) if c >= 20]
        if keep:
            point_hits[point_idx] = keep

    label_hits: dict[int, list[int]] = {}
    for item_idx, item in enumerate(ocr_items):
        x1, y1 = np.floor(item.box.min(axis=0)).astype(int)
        x2, y2 = np.ceil(item.box.max(axis=0)).astype(int)
        x1 = max(0, x1 - 14)
        y1 = max(0, y1 - 14)
        x2 = min(labels.shape[1] - 1, x2 + 14)
        y2 = min(labels.shape[0] - 1, y2 + 14)
        perimeter = []
        for x in range(x1, x2 + 1):
            perimeter.append((x, y1))
            perimeter.append((x, y2))
        for y in range(y1 + 1, y2):
            perimeter.append((x1, y))
            perimeter.append((x2, y))
        vals = [int(labels[y, x]) for x, y in perimeter if labels[y, x] > 0]
        if not vals:
            continue
        uniq, counts = np.unique(np.array(vals, dtype=np.int32), return_counts=True)
        keep = [int(v) for v, c in zip(uniq, counts) if c >= 3]
        if keep:
            label_hits[item_idx] = keep

    component_to_points: dict[int, list[int]] = {}
    for point_idx, comps in point_hits.items():
        for comp in comps:
            component_to_points.setdefault(comp, []).append(point_idx)
    component_to_labels: dict[int, list[int]] = {}
    for item_idx, comps in label_hits.items():
        for comp in comps:
            component_to_labels.setdefault(comp, []).append(item_idx)

    rows: list[PointValue] = []
    used_points: set[int] = set()
    used_items: set[int] = set()
    for comp in sorted(component_to_points.keys()):
        point_ids = [idx for idx in component_to_points.get(comp, []) if idx not in used_points]
        item_ids = [idx for idx in component_to_labels.get(comp, []) if idx not in used_items]
        if not point_ids or not item_ids:
            continue

        # Pick the strongest point in the component and the nearest label to it.
        point_ids.sort(key=lambda idx: points[idx].score, reverse=True)
        point_idx = point_ids[0]
        point = points[point_idx]
        item_idx = min(
            item_ids,
            key=lambda idx: math.hypot(float(ocr_items[idx].center[0] - point.x), float(ocr_items[idx].center[1] - point.y)),
        )
        item = ocr_items[item_idx]
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(point.x),
                point_y=float(point.y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(item.center[0]),
                outer_y=float(item.center[1]),
                anchor_x=float(point.x),
                anchor_y=float(point.y),
                anchor_distance=0.0,
                leader_length=float(stats[comp, cv2.CC_STAT_AREA]) ** 0.5,
                source="magenta-connect",
            )
        )
        used_points.add(point_idx)
        used_items.add(item_idx)
    return sorted(rows, key=lambda row: row.point_id)


def assign_values_via_magenta_segments(
    image: np.ndarray,
    points: list[Candidate],
    ocr_items: list[OCRItem],
) -> list[PointValue]:
    if not points or not ocr_items:
        return []
    segments = merge_red_guide_segments(detect_magenta_guide_segments(image))
    if not segments:
        return []

    point_xy = np.array([[p.x, p.y] for p in points], dtype=np.float32)
    cost_rows: list[tuple[float, int, int, dict]] = []
    for item_idx, item in enumerate(ocr_items):
        for seg in segments:
            for outer, inner in ((seg["p1"], seg["p2"]), (seg["p2"], seg["p1"])):
                box_dist = point_box_distance(outer, item.box, margin=78.0)
                seg_box_dist = min(
                    point_box_distance(seg["p1"], item.box, margin=78.0),
                    point_box_distance(seg["p2"], item.box, margin=78.0),
                )
                if min(box_dist, seg_box_dist) > 115.0:
                    continue
                dists = np.linalg.norm(point_xy - inner, axis=1)
                seg_dists = np.array([point_to_segment_distance(pxy, seg["p1"], seg["p2"]) for pxy in point_xy], dtype=np.float32)
                point_idx = int(np.argmin(dists))
                point_dist = float(dists[point_idx])
                point_seg_dist = float(seg_dists[point_idx])
                if min(point_dist, point_seg_dist) > 110.0:
                    continue
                label_center_dist = float(np.linalg.norm(item.center - outer))
                label_to_point = point_xy[point_idx] - item.center
                label_norm = float(np.linalg.norm(label_to_point))
                seg_vec = inner - outer
                seg_norm = float(np.linalg.norm(seg_vec))
                align_penalty = 0.0
                if label_norm > 1e-6 and seg_norm > 1e-6:
                    align_penalty = 1.0 - abs(float((label_to_point @ seg_vec) / (label_norm * seg_norm)))
                cost = (
                    min(box_dist, seg_box_dist) * 1.8
                    + min(point_dist, point_seg_dist) * 1.3
                    + 0.10 * label_center_dist
                    + align_penalty * 28.0
                    - 0.035 * seg["length"]
                )
                cost_rows.append((cost, item_idx, point_idx, {"segment": seg, "outer": outer, "inner": inner}))

    rows: list[PointValue] = []
    used_points: set[int] = set()
    used_items: set[int] = set()
    for cost, item_idx, point_idx, extra in sorted(cost_rows, key=lambda x: x[0]):
        if item_idx in used_items or point_idx in used_points:
            continue
        item = ocr_items[item_idx]
        point = points[point_idx]
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(point.x),
                point_y=float(point.y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(extra["outer"][0]),
                outer_y=float(extra["outer"][1]),
                anchor_x=float(extra["inner"][0]),
                anchor_y=float(extra["inner"][1]),
                anchor_distance=float(np.linalg.norm(np.array([point.x, point.y], dtype=np.float32) - extra["inner"])),
                leader_length=float(extra["segment"]["length"]),
                source="magenta-segment",
            )
        )
        used_items.add(item_idx)
        used_points.add(point_idx)
    return sorted(rows, key=lambda row: row.point_id)


def assign_values_via_dark_segments(
    image: np.ndarray,
    points: list[Candidate],
    ocr_items: list[OCRItem],
    part_mask: np.ndarray,
) -> list[PointValue]:
    if not points or not ocr_items:
        return []
    segments = merge_red_guide_segments(detect_dark_guide_segments(image, part_mask))
    if not segments:
        return []

    center = polygon_center(contour_from_mask(part_mask))
    point_xy = np.array([[p.x, p.y] for p in points], dtype=np.float32)
    point_out_dirs = point_xy - center
    point_out_norms = np.linalg.norm(point_out_dirs, axis=1) + 1e-6
    point_out_dirs = point_out_dirs / point_out_norms[:, None]

    cost_rows: list[tuple[float, int, int, dict]] = []
    for point_idx, point in enumerate(points):
        for seg in segments:
            for inner, outer in ((seg["p1"], seg["p2"]), (seg["p2"], seg["p1"])):
                point_dist = float(np.linalg.norm(point_xy[point_idx] - inner))
                point_seg_dist = point_to_segment_distance(point_xy[point_idx], seg["p1"], seg["p2"])
                if min(point_dist, point_seg_dist) > 65.0:
                    continue

                seg_vec = outer - inner
                seg_norm = float(np.linalg.norm(seg_vec))
                if seg_norm <= 1e-6:
                    continue
                seg_dir = seg_vec / seg_norm
                outward_align = 1.0 - abs(float(seg_dir @ point_out_dirs[point_idx]))

                for item_idx, item in enumerate(ocr_items):
                    box_dist = point_box_distance(outer, item.box, margin=55.0)
                    seg_box_dist = min(
                        point_box_distance(seg["p1"], item.box, margin=55.0),
                        point_box_distance(seg["p2"], item.box, margin=55.0),
                    )
                    if min(box_dist, seg_box_dist) > 85.0:
                        continue
                    label_center_dist = float(np.linalg.norm(item.center - outer))
                    point_to_label = item.center - point_xy[point_idx]
                    label_norm = float(np.linalg.norm(point_to_label))
                    align_penalty = 0.0
                    if label_norm > 1e-6:
                        align_penalty = 1.0 - abs(float((point_to_label @ seg_vec) / (label_norm * seg_norm)))
                    cost = (
                        min(point_dist, point_seg_dist) * 1.4
                        + min(box_dist, seg_box_dist) * 1.7
                        + 0.08 * label_center_dist
                        + align_penalty * 22.0
                        + outward_align * 18.0
                        - 0.03 * seg["length"]
                    )
                    cost_rows.append((cost, item_idx, point_idx, {"segment": seg, "outer": outer, "inner": inner}))

    rows: list[PointValue] = []
    used_points: set[int] = set()
    used_items: set[int] = set()
    for cost, item_idx, point_idx, extra in sorted(cost_rows, key=lambda x: x[0]):
        if item_idx in used_items or point_idx in used_points:
            continue
        item = ocr_items[item_idx]
        point = points[point_idx]
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(point.x),
                point_y=float(point.y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(extra["outer"][0]),
                outer_y=float(extra["outer"][1]),
                anchor_x=float(extra["inner"][0]),
                anchor_y=float(extra["inner"][1]),
                anchor_distance=float(np.linalg.norm(np.array([point.x, point.y], dtype=np.float32) - extra["inner"])),
                leader_length=float(extra["segment"]["length"]),
                source="dark-segment",
            )
        )
        used_items.add(item_idx)
        used_points.add(point_idx)
    return sorted(rows, key=lambda row: row.point_id)


def assign_values_via_red_segments(
    image: np.ndarray,
    points: list[Candidate],
    ocr_items: list[OCRItem],
) -> list[PointValue]:
    if not points or not ocr_items:
        return []
    segments = merge_red_guide_segments(detect_red_guide_segments(image))
    if not segments:
        return []

    point_xy = np.array([[p.x, p.y] for p in points], dtype=np.float32)
    cost_rows: list[tuple[float, int, int, dict]] = []
    for item_idx, item in enumerate(ocr_items):
        for seg in segments:
            for outer, inner in ((seg["p1"], seg["p2"]), (seg["p2"], seg["p1"])):
                box_dist = point_box_distance(outer, item.box, margin=78.0)
                seg_box_dist = min(
                    point_box_distance(seg["p1"], item.box, margin=78.0),
                    point_box_distance(seg["p2"], item.box, margin=78.0),
                )
                if min(box_dist, seg_box_dist) > 115.0:
                    continue
                dists = np.linalg.norm(point_xy - inner, axis=1)
                seg_dists = np.array([point_to_segment_distance(pxy, seg["p1"], seg["p2"]) for pxy in point_xy], dtype=np.float32)
                point_idx = int(np.argmin(dists))
                point_dist = float(dists[point_idx])
                point_seg_dist = float(seg_dists[point_idx])
                if min(point_dist, point_seg_dist) > 110.0:
                    continue
                label_center_dist = float(np.linalg.norm(item.center - outer))
                label_to_point = point_xy[point_idx] - item.center
                label_norm = float(np.linalg.norm(label_to_point))
                seg_vec = inner - outer
                seg_norm = float(np.linalg.norm(seg_vec))
                align_penalty = 0.0
                if label_norm > 1e-6 and seg_norm > 1e-6:
                    align_penalty = 1.0 - abs(float((label_to_point @ seg_vec) / (label_norm * seg_norm)))
                cost = (
                    min(box_dist, seg_box_dist) * 1.8
                    + min(point_dist, point_seg_dist) * 1.3
                    + 0.10 * label_center_dist
                    + align_penalty * 28.0
                    - 0.035 * seg["length"]
                )
                cost_rows.append((cost, item_idx, point_idx, {"segment": seg, "outer": outer, "inner": inner}))

    rows: list[PointValue] = []
    used_points: set[int] = set()
    used_items: set[int] = set()
    for cost, item_idx, point_idx, extra in sorted(cost_rows, key=lambda x: x[0]):
        if item_idx in used_items or point_idx in used_points:
            continue
        item = ocr_items[item_idx]
        point = points[point_idx]
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(point.x),
                point_y=float(point.y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(extra["outer"][0]),
                outer_y=float(extra["outer"][1]),
                anchor_x=float(extra["inner"][0]),
                anchor_y=float(extra["inner"][1]),
                anchor_distance=float(np.linalg.norm(np.array([point.x, point.y], dtype=np.float32) - extra["inner"])),
                leader_length=float(extra["segment"]["length"]),
                source="red-segment",
            )
        )
        used_items.add(item_idx)
        used_points.add(point_idx)
    return sorted(rows, key=lambda row: row.point_id)


def assign_values_to_points(path: Path, image: np.ndarray, points: list[Candidate], part_mask: np.ndarray) -> tuple[list[OCRItem], list[PointValue]]:
    contour = contour_from_mask(part_mask)
    if contour.size == 0 or not points:
        return [], []
    ocr_items = extract_numeric_ocr(path)
    if not ocr_items:
        return [], []

    rows: list[PointValue] = []
    used_point_ids: set[int] = set()
    used_label_keys: set[tuple[str, float, float]] = set()

    def row_label_key(row: PointValue) -> tuple[str, float, float]:
        return (row.label_text, round(row.label_x, 1), round(row.label_y, 1))

    def add_rows(candidates_rows: list[PointValue]) -> None:
        for row in candidates_rows:
            label_key = row_label_key(row)
            if row.point_id in used_point_ids or label_key in used_label_keys:
                continue
            rows.append(row)
            used_point_ids.add(row.point_id)
            used_label_keys.add(label_key)

    add_rows(assign_values_via_red_segments(image, points, ocr_items))
    add_rows(assign_values_via_red_connectivity(image, points, ocr_items, part_mask))
    add_rows(assign_values_via_magenta_segments(image, points, ocr_items))
    add_rows(assign_values_via_magenta_connectivity(image, points, ocr_items, part_mask))
    add_rows(assign_values_via_dark_segments(image, points, ocr_items, part_mask))

    center = polygon_center(contour)
    leader_segments = merge_leader_segments(detect_leader_segments(image, contour))
    polyline_mask = extract_polyline_mask(image, contour)
    contour_distance = build_contour_distance(polyline_mask.shape, contour)
    leader_assignments = assign_leader_segments(ocr_items, contour, center, leader_segments)
    if not leader_assignments:
        leader_assignments = {}

    point_xy = np.array([[p.x, p.y] for p in points], dtype=np.float32)
    used_points: set[int] = {row.point_id - 1 for row in rows}
    assigned_items: set[int] = set()
    for item_idx, item in enumerate(ocr_items):
        key = (item.text, round(float(item.center[0]), 1), round(float(item.center[1]), 1))
        if key in used_label_keys:
            assigned_items.add(item_idx)

    scored_pairs: list[tuple[float, int, int, dict]] = []
    for item_idx, assignment in leader_assignments.items():
        segment = assignment["segment"]
        anchor = segment["contour_anchor"]
        dists = np.linalg.norm(point_xy - anchor, axis=1)
        point_idx = int(np.argmin(dists))
        scored_pairs.append((float(dists[point_idx]), item_idx, point_idx, assignment))

    for anchor_dist, item_idx, point_idx, assignment in sorted(scored_pairs, key=lambda x: x[0]):
        if point_idx in used_points or item_idx in assigned_items:
            continue
        item = ocr_items[item_idx]
        point = points[point_idx]
        segment = assignment["segment"]
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(point.x),
                point_y=float(point.y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(segment["outer"][0]),
                outer_y=float(segment["outer"][1]),
                anchor_x=float(segment["contour_anchor"][0]),
                anchor_y=float(segment["contour_anchor"][1]),
                anchor_distance=float(anchor_dist),
                leader_length=float(segment["length"]),
                source="leader",
            )
        )
        used_points.add(point_idx)
        assigned_items.add(item_idx)
        used_point_ids.add(point_idx + 1)
        used_label_keys.add((item.text, round(float(item.center[0]), 1), round(float(item.center[1]), 1)))

    # Fallback: trace low-saturation leader polylines from the label box to the contour.
    for item_idx, item in enumerate(ocr_items):
        if item_idx in assigned_items:
            continue
        traced = trace_polyline_anchor(item, contour, center, polyline_mask, contour_distance)
        if traced is None:
            continue
        anchor = traced["contour_anchor"]
        dists = np.linalg.norm(point_xy - anchor, axis=1)
        point_idx = int(np.argmin(dists))
        if point_idx in used_points:
            continue
        rows.append(
            PointValue(
                point_id=point_idx + 1,
                point_x=float(points[point_idx].x),
                point_y=float(points[point_idx].y),
                label_text=item.text,
                label_value=float(item.value),
                label_x=float(item.center[0]),
                label_y=float(item.center[1]),
                label_score=float(item.score),
                outer_x=float(anchor[0]),
                outer_y=float(anchor[1]),
                anchor_x=float(anchor[0]),
                anchor_y=float(anchor[1]),
                anchor_distance=float(dists[point_idx]),
                leader_length=float(traced["length"]),
                source="polyline",
            )
        )
        used_points.add(point_idx)
        assigned_items.add(item_idx)
        used_point_ids.add(point_idx + 1)
        used_label_keys.add((item.text, round(float(item.center[0]), 1), round(float(item.center[1]), 1)))
    return ocr_items, sorted(rows, key=lambda row: row.point_id)


def cluster_by_contour_gap(candidates: list[Candidate], contour: np.ndarray, gap_threshold: int) -> list[Candidate]:
    if not candidates:
        return []
    contour_xy = contour[:, 0, :]
    indexed: list[tuple[int, Candidate]] = []
    for cand in candidates:
        idx = int(np.argmin(np.sum((contour_xy - np.array([[cand.x, cand.y]])) ** 2, axis=1)))
        indexed.append((idx, cand))
    indexed.sort(key=lambda item: item[0])

    groups: list[list[tuple[int, Candidate]]] = [[indexed[0]]]
    for item in indexed[1:]:
        if item[0] - groups[-1][-1][0] > gap_threshold:
            groups.append([item])
        else:
            groups[-1].append(item)

    selected = [max(group, key=lambda item: item[1].score)[1] for group in groups]
    return sort_candidates(selected)


def extract_annotation_anchors(annotation_path: Path, original_shape: tuple[int, int]) -> list[tuple[float, float]]:
    annotated = cv2.imread(str(annotation_path))
    if annotated is None:
        return []
    hsv = cv2.cvtColor(annotated, cv2.COLOR_BGR2HSV)
    lower1 = np.array([0, 120, 120], dtype=np.uint8)
    upper1 = np.array([12, 255, 255], dtype=np.uint8)
    lower2 = np.array([168, 120, 120], dtype=np.uint8)
    upper2 = np.array([180, 255, 255], dtype=np.uint8)
    red_mask = cv2.inRange(hsv, lower1, upper1) | cv2.inRange(hsv, lower2, upper2)
    red_mask = cv2.morphologyEx(
        red_mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    contours, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    scale_x = original_shape[1] / annotated.shape[1]
    scale_y = original_shape[0] / annotated.shape[0]
    anchors: list[tuple[float, float]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w * h < 80 or w * h > 5000:
            continue
        if w < 8 or h < 8:
            continue
        anchors.append(((x + 0.5 * w) * scale_x, (y + 0.5 * h) * scale_y))
    return sort_points(anchors, row_tol=20.0 * scale_y)


def snap_candidates_to_anchors(
    bgr: np.ndarray,
    part_mask: np.ndarray,
    anchors: list[tuple[float, float]],
    candidates: list[Candidate],
) -> list[Candidate]:
    if not anchors:
        return sort_candidates(candidates)

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    tophat = cv2.morphologyEx(
        gray,
        cv2.MORPH_TOPHAT,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)),
    )
    height, width = gray.shape
    radius = max(10, int(round(min(height, width) * 0.04)))
    snapped: list[Candidate] = []
    used: list[tuple[float, float]] = []

    for ax, ay in anchors:
        nearby = [
            cand
            for cand in candidates
            if (cand.x - ax) ** 2 + (cand.y - ay) ** 2 <= radius * radius
        ]
        if nearby:
            best = max(nearby, key=lambda cand: cand.score - 0.6 * math.hypot(cand.x - ax, cand.y - ay))
            chosen = Candidate(best.x, best.y, best.area, best.circularity, best.score, f"{best.source}+anchor")
        else:
            x0 = max(0, int(round(ax)) - radius)
            y0 = max(0, int(round(ay)) - radius)
            x1 = min(width, int(round(ax)) + radius + 1)
            y1 = min(height, int(round(ay)) + radius + 1)
            patch = tophat[y0:y1, x0:x1].copy()
            patch[part_mask[y0:y1, x0:x1] == 0] = 0
            if patch.max() <= 0:
                chosen = Candidate(ax, ay, 1.0, 1.0, 0.0, "anchor")
            else:
                py, px = np.unravel_index(int(np.argmax(patch)), patch.shape)
                peak = int(patch[py, px])
                region = np.zeros_like(patch, dtype=np.uint8)
                region[patch >= max(peak - 12, int(peak * 0.65))] = 255
                num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(region, 8)
                label = int(labels[py, px])
                if label > 0:
                    cx = x0 + float(centroids[label][0])
                    cy = y0 + float(centroids[label][1])
                    area = float(stats[label, cv2.CC_STAT_AREA])
                else:
                    cx = x0 + float(px)
                    cy = y0 + float(py)
                    area = 1.0
                chosen = Candidate(cx, cy, area, 1.0, float(peak), "anchor")

        if all((chosen.x - ux) ** 2 + (chosen.y - uy) ** 2 >= 25.0 for ux, uy in used):
            snapped.append(chosen)
            used.append((chosen.x, chosen.y))
        else:
            snapped.append(Candidate(ax, ay, 1.0, 1.0, 0.0, "anchor"))
            used.append((ax, ay))
    return sort_candidates(snapped)


def detect_blue_points(bgr: np.ndarray, part_mask: np.ndarray) -> list[Candidate]:
    blob_candidates = detect_component_blobs(bgr, part_mask)
    edge_candidates = detect_edge_peaks(bgr, part_mask)

    if len(blob_candidates) >= 15:
        combined = blob_candidates + edge_candidates
    elif len(edge_candidates) >= 8:
        combined = edge_candidates
    else:
        combined = blob_candidates + edge_candidates

    return non_max_suppress(combined, min_dist=8.0)


def sort_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (c.y, c.x))


def non_max_suppress(candidates: list[Candidate], min_dist: float) -> list[Candidate]:
    kept: list[Candidate] = []
    for cand in sorted(candidates, key=lambda c: c.score, reverse=True):
        if all((cand.x - k.x) ** 2 + (cand.y - k.y) ** 2 >= min_dist * min_dist for k in kept):
            kept.append(cand)
    return sort_candidates(kept)


def draw_results(bgr: np.ndarray, candidates: list[Candidate]) -> np.ndarray:
    out = bgr.copy()
    for idx, cand in enumerate(candidates, start=1):
        center = (int(round(cand.x)), int(round(cand.y)))
        if "anchor" in cand.source:
            color = (255, 0, 255)
        elif cand.source == "explicit-magenta":
            color = (255, 0, 255)
        elif cand.source == "blob":
            color = (0, 0, 255)
        else:
            color = (0, 165, 255)
        cv2.circle(out, center, 20, color, 9, cv2.LINE_AA)
        cv2.circle(out, center, 12, (255, 255, 255), -1, cv2.LINE_AA)
        cv2.putText(
            out,
            str(idx),
            (center[0] + 4, center[1] - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            3,
            color,
            5,
            cv2.LINE_AA,
        )
    return out


def draw_value_debug(bgr: np.ndarray, points: list[Candidate], rows: list[PointValue]) -> np.ndarray:
    out = bgr.copy()
    point_map = {idx + 1: point for idx, point in enumerate(points)}
    for row in rows:
        p = point_map.get(row.point_id)
        if p is None:
            continue
        label_pt = (int(round(row.label_x)), int(round(row.label_y)))
        point_pt = (int(round(p.x)), int(round(p.y)))
        outer_pt = (int(round(row.outer_x)), int(round(row.outer_y)))
        anchor_pt = (int(round(row.anchor_x)), int(round(row.anchor_y)))

        if row.source == "red-segment":
            # Follow the matched red guide segment itself instead of drawing a new straight shortcut.
            cv2.line(out, outer_pt, anchor_pt, (255, 0, 0), 3, cv2.LINE_AA)
            if max(abs(anchor_pt[0] - point_pt[0]), abs(anchor_pt[1] - point_pt[1])) > 6:
                cv2.line(out, anchor_pt, point_pt, (0, 255, 255), 2, cv2.LINE_AA)
        elif row.source == "magenta-segment":
            # Draw magenta guide segment similarly
            cv2.line(out, outer_pt, anchor_pt, (255, 0, 255), 3, cv2.LINE_AA)
            if max(abs(anchor_pt[0] - point_pt[0]), abs(anchor_pt[1] - point_pt[1])) > 6:
                cv2.line(out, anchor_pt, point_pt, (0, 255, 255), 2, cv2.LINE_AA)
        else:
            cv2.line(out, label_pt, outer_pt, (255, 0, 0), 2, cv2.LINE_AA)
            cv2.line(out, outer_pt, anchor_pt, (255, 0, 0), 2, cv2.LINE_AA)
            cv2.line(out, anchor_pt, point_pt, (0, 255, 255), 2, cv2.LINE_AA)

        cv2.circle(out, point_pt, 8, (0, 0, 255), -1, cv2.LINE_AA)
        cv2.putText(
            out,
            f"{row.point_id}:{row.label_text}",
            (point_pt[0] + 4, point_pt[1] - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 255),
            1,
            cv2.LINE_AA,
        )
    return out


def detect_points(image_path: Path, bgr: np.ndarray, part_mask: np.ndarray, annotations_dir: Path | None) -> list[Candidate]:
    explicit = detect_explicit_markers(bgr, part_mask)
    if len(explicit) >= 8:
        return explicit

    blob_candidates = detect_component_blobs(bgr, part_mask)
    edge_candidates = detect_edge_peaks(bgr, part_mask)
    band_candidates = contour_band_candidates(
        bgr,
        part_mask,
        kernel_size=13,
        threshold=18 if bgr.shape[0] > 370 else 20,
        band_width=15 if bgr.shape[0] > 370 else 20,
    )

    annotation_path = None
    if annotations_dir is not None:
        for name in [f"{image_path.stem}_annotated.png", f"{image_path.stem}.png"]:
            candidate_path = annotations_dir / name
            if candidate_path.exists():
                annotation_path = candidate_path
                break
    if annotation_path is not None:
        anchors = extract_annotation_anchors(annotation_path, bgr.shape[:2])
        pooled = non_max_suppress(blob_candidates + edge_candidates + band_candidates, min_dist=5.0)
        return snap_candidates_to_anchors(bgr, part_mask, anchors, pooled)

    if len(blob_candidates) < 10 and len(edge_candidates) >= 12:
        raw_band_candidates = contour_band_candidates(
            bgr,
            part_mask,
            kernel_size=13,
            threshold=18 if bgr.shape[0] > 370 else 20,
            band_width=15 if bgr.shape[0] > 370 else 20,
            suppress=False,
        )
        contours, _ = cv2.findContours(part_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        contour = max(contours, key=cv2.contourArea)
        return cluster_by_contour_gap(raw_band_candidates, contour, gap_threshold=15)

    return detect_blue_points(bgr, part_mask)


def process_image(
    path: Path,
    out_dir: Path,
    annotations_dir: Path | None = None,
    order_options: OrderOptions | None = None,
) -> list[Candidate]:
    bgr = cv2.imread(str(path))
    if bgr is None:
        raise FileNotFoundError(path)
    part_mask = extract_part_mask(bgr)
    candidates = detect_points(path, bgr, part_mask, annotations_dir)
    contour = contour_from_mask(part_mask)
    candidates = order_candidates_by_contour(candidates, contour, order_options or OrderOptions())
    ocr_items, point_values = assign_values_to_points(path, bgr, candidates, part_mask)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    cv2.imwrite(str(out_dir / f"{stem}_mask.png"), part_mask)
    cv2.imwrite(str(out_dir / f"{stem}_points.png"), draw_results(bgr, candidates))
    if point_values:
        cv2.imwrite(str(out_dir / f"{stem}_values.png"), draw_value_debug(bgr, candidates, point_values))

    with (out_dir / f"{stem}_points.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "x", "y", "area", "circularity", "score", "source"])
        for i, cand in enumerate(candidates, start=1):
            writer.writerow(
                [
                    i,
                    f"{cand.x:.2f}",
                    f"{cand.y:.2f}",
                    f"{cand.area:.2f}",
                    f"{cand.circularity:.3f}",
                    f"{cand.score:.3f}",
                    cand.source,
                ]
            )

    with (out_dir / f"{stem}_labels.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["text", "value", "score", "center_x", "center_y", "box"])
        for item in ocr_items:
            writer.writerow(
                [
                    item.text,
                    f"{item.value:.6f}",
                    f"{item.score:.3f}",
                    f"{item.center[0]:.2f}",
                    f"{item.center[1]:.2f}",
                    item.box.reshape(-1).tolist(),
                ]
            )

    with (out_dir / f"{stem}_point_values.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "point_id",
                "point_x",
                "point_y",
                "label_text",
                "label_value",
                "label_x",
                "label_y",
                "label_score",
                "outer_x",
                "outer_y",
                "anchor_x",
                "anchor_y",
                "anchor_distance",
                "leader_length",
                "source",
            ]
        )
        for row in point_values:
            writer.writerow(
                [
                    row.point_id,
                    f"{row.point_x:.2f}",
                    f"{row.point_y:.2f}",
                    row.label_text,
                    f"{row.label_value:.6f}",
                    f"{row.label_x:.2f}",
                    f"{row.label_y:.2f}",
                    f"{row.label_score:.3f}",
                    f"{row.outer_x:.2f}",
                    f"{row.outer_y:.2f}",
                    f"{row.anchor_x:.2f}",
                    f"{row.anchor_y:.2f}",
                    f"{row.anchor_distance:.2f}",
                    f"{row.leader_length:.2f}",
                    row.source,
                ]
            )

    return candidates


def make_run_output_dir(base_output_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = base_output_dir / timestamp
    suffix = 1
    while run_dir.exists():
        run_dir = base_output_dir / f"{timestamp}_{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract blue measurement points from CAE screenshots.")
    parser.add_argument("images", nargs="*", help="Images to process. Defaults to the two provided test images.")
    parser.add_argument("--output-dir", default=r"D:\NoobhekProject\Node-Selector\outputs")
    parser.add_argument("--annotations-dir", default=r"D:\NoobhekProject\Node-Selector\images\annotations")
    parser.add_argument("--order-direction", choices=["cw", "ccw", "none"], default="cw")
    parser.add_argument("--order-start", choices=["top", "bottom", "left", "right"], default="top")
    args = parser.parse_args()

    base = Path(r"D:\NoobhekProject\Node-Selector")
    test_dir = base / "images" / "test"
    base_out_dir = Path(args.output_dir)
    annotations_dir = Path(args.annotations_dir)
    order_options = OrderOptions(direction=args.order_direction, start=args.order_start)
    image_paths = [Path(p) for p in args.images] if args.images else [test_dir / "red_004.png", test_dir / "red_015.png"]
    out_dir = make_run_output_dir(base_out_dir)

    print(f"output_dir: {out_dir}")
    print(f"order: direction={order_options.direction}, start={order_options.start}")

    for path in image_paths:
        points = process_image(path, out_dir, annotations_dir=annotations_dir, order_options=order_options)
        print(path.name, len(points))
        for p in points:
            print(f"  {p.x:.1f}, {p.y:.1f}, area={p.area:.1f}, circ={p.circularity:.3f}, source={p.source}")


if __name__ == "__main__":
    main()
