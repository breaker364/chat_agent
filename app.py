from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from rapidocr_onnxruntime import RapidOCR
from scipy.optimize import linear_sum_assignment


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
DATA_DIR = APP_DIR / "data"
TEMPLATE_DIR = DATA_DIR / "templates"
EXPORT_DIR = DATA_DIR / "exports"
UPLOAD_DIR = DATA_DIR / "uploads"
OCR_ENGINE = RapidOCR()
FULL_NUMBER_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?$")
NUMERICISH_TEXT_RE = re.compile(r"^[0-9OoIlI'`.,+\-\s]+$")
TWO_PI = 2.0 * math.pi
DEFAULT_POINT_RGB = (3, 213, 253)
DEFAULT_POINT_SEARCH_RADIUS = 3


@dataclass
class Point:
    point_id: int
    x: float
    y: float
    area: int
    width: int
    height: int
    norm_x: float
    norm_y: float
    radius: float
    angle_cw: float
    zone: str


@dataclass
class TextBox:
    textbox_id: int
    text: str
    value: float
    score: float
    center_x: float
    center_y: float
    box_w: float
    box_h: float
    norm_x: float
    norm_y: float
    radius: float
    angle_cw: float
    zone: str


class DetectRequest(BaseModel):
    image_path: str
    ocr_min_score: float = 0.70


class SaveTemplateRequest(BaseModel):
    template_name: str
    image_path: str
    ordered_point_ids: list[int]
    points: list[dict[str, Any]]


class ApplyTemplateRequest(BaseModel):
    template_name: str
    image_path: str
    ocr_min_score: float = 0.70
    max_match_cost: float = 0.55


class ExportEditedRequest(BaseModel):
    template_name: str
    image_path: str
    ordered_points: list[dict[str, Any]]
    matched_rows: list[dict[str, Any]]


def ensure_dirs() -> None:
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def detect_point_color(image_rgb: np.ndarray, chroma_min: int, min_pixels: int) -> tuple[int, int, int]:
    # 为了抵抗图像压缩和抗锯齿导致的颜色分散，先对颜色进行量化(分箱合并)
    quantized_rgb = (image_rgb // 16) * 16 + 8
    
    flat = quantized_rgb.reshape(-1, 3)
    values, counts = np.unique(flat, axis=0, return_counts=True)
    best_score = -1
    best_color: tuple[int, int, int] | None = None
    for value, count in zip(values, counts):
        rgb = value.astype(np.int16)
        chroma = int(rgb.max() - rgb.min())
        if chroma < chroma_min or int(count) < min_pixels:
            continue
        brightness = int(rgb.sum())
        score = int(count) * 1000 + chroma * 10 - abs(brightness - 510)
        if score > best_score:
            best_score = score
            best_color = tuple(int(channel) for channel in value)
    if best_color is None:
        raise RuntimeError("No point color candidate matched the chroma/count thresholds.")
    return best_color


def select_target_variant(
    image_rgb: np.ndarray,
    target_rgb: tuple[int, int, int],
    search_radius: int,
) -> tuple[int, int, int]:
    if search_radius <= 0:
        return target_rgb
    flat = image_rgb.reshape(-1, 3)
    values, counts = np.unique(flat, axis=0, return_counts=True)
    target = np.array(target_rgb, dtype=np.int16)
    diff = np.abs(values.astype(np.int16) - target)
    nearby = np.where(diff.max(axis=1) <= search_radius)[0]
    if nearby.size == 0:
        return target_rgb
    best_idx = int(nearby[np.argmax(counts[nearby])])
    return tuple(int(channel) for channel in values[best_idx])


def build_color_mask(image_rgb: np.ndarray, target_rgb: tuple[int, int, int], tolerance: int) -> np.ndarray:
    target = np.array(target_rgb, dtype=np.int16)
    diff = np.abs(image_rgb.astype(np.int16) - target)
    return (diff.max(axis=2) <= tolerance).astype(np.uint8) * 255


def extract_raw_points(mask: np.ndarray, min_area: int, max_area: int) -> list[Point]:
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    points: list[Point] = []
    point_id = 1
    for idx in range(1, num_labels):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < min_area or area > max_area:
            continue
        width = int(stats[idx, cv2.CC_STAT_WIDTH])
        height = int(stats[idx, cv2.CC_STAT_HEIGHT])
        points.append(
            Point(
                point_id=point_id,
                x=float(centroids[idx][0]),
                y=float(centroids[idx][1]),
                area=area,
                width=width,
                height=height,
                norm_x=0.0,
                norm_y=0.0,
                radius=0.0,
                angle_cw=0.0,
                zone="unknown",
            )
        )
        point_id += 1
    return points


def detect_point_mask_and_color(image_rgb: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int], list[Point]]:
    fixed_rgb = select_target_variant(image_rgb, DEFAULT_POINT_RGB, DEFAULT_POINT_SEARCH_RADIUS)
    fixed_mask = build_color_mask(image_rgb, fixed_rgb, tolerance=3)
    fixed_points = extract_raw_points(fixed_mask, min_area=3, max_area=5000)
    if len(fixed_points) >= 8:
        return fixed_mask, fixed_rgb, fixed_points

    auto_rgb = detect_point_color(image_rgb, chroma_min=80, min_pixels=1000)
    auto_mask = build_color_mask(image_rgb, auto_rgb, tolerance=3)
    auto_points = extract_raw_points(auto_mask, min_area=3, max_area=5000)
    if len(auto_points) > len(fixed_points):
        return auto_mask, auto_rgb, auto_points
    return fixed_mask, fixed_rgb, fixed_points


def largest_component(mask: np.ndarray) -> np.ndarray:
    num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    if num_labels <= 1:
        return np.zeros_like(mask)
    largest_idx = max(range(1, num_labels), key=lambda idx: int(stats[idx, cv2.CC_STAT_AREA]))
    return (labels == largest_idx).astype(np.uint8) * 255


def build_contour_mask(image_bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    base_mask = ((hsv[:, :, 1] > 35) & (hsv[:, :, 2] > 40)).astype(np.uint8) * 255
    base_mask = cv2.morphologyEx(base_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
    return largest_component(base_mask)


def select_part_contour(contour_mask: np.ndarray) -> tuple[np.ndarray, tuple[float, float], tuple[int, int, int, int]]:
    contours, _ = cv2.findContours(contour_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        raise RuntimeError("No external contours found in contour mask.")
    contour = max(contours, key=cv2.contourArea)
    moments = cv2.moments(contour)
    if abs(moments["m00"]) < 1e-6:
        raise RuntimeError("Part contour has zero area in moments.")
    center = (float(moments["m10"] / moments["m00"]), float(moments["m01"] / moments["m00"]))
    bbox = cv2.boundingRect(contour)
    return contour, center, bbox


def circular_angle_cw(center_x: float, center_y: float, x: float, y: float) -> float:
    dx = x - center_x
    dy = y - center_y
    return float((math.atan2(dx, -dy) + TWO_PI) % TWO_PI)


def classify_zone(norm_x: float, norm_y: float, radius: float) -> str:
    dx = norm_x - 0.5
    dy = norm_y - 0.5
    if radius < 0.16:
        return "center"
    if abs(dy) > abs(dx) * 1.1:
        return "bottom" if dy > 0 else "top"
    return "right" if dx > 0 else "left"


def enrich_point(point: Point, bbox: tuple[int, int, int, int], center: tuple[float, float]) -> Point:
    bbox_x, bbox_y, bbox_w, bbox_h = bbox
    norm_x = float((point.x - bbox_x) / max(bbox_w, 1))
    norm_y = float((point.y - bbox_y) / max(bbox_h, 1))
    dx = norm_x - 0.5
    dy = norm_y - 0.5
    radius = float(math.hypot(dx, dy))
    angle_cw = circular_angle_cw(center[0], center[1], point.x, point.y)
    return Point(
        point_id=point.point_id,
        x=point.x,
        y=point.y,
        area=point.area,
        width=point.width,
        height=point.height,
        norm_x=norm_x,
        norm_y=norm_y,
        radius=radius,
        angle_cw=angle_cw,
        zone=classify_zone(norm_x, norm_y, radius),
    )


def normalize_ocr_text(text: str) -> str:
    normalized = str(text).strip().replace("\n", " ")
    normalized = normalized.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
    normalized = normalized.replace(",", ".").replace(":", ".").replace("'", ".").replace("`", ".")
    normalized = normalized.replace("—", "-").replace("–", "-")
    return normalized


def parse_measurement_text(text: str) -> tuple[str | None, float | None, str | None]:
    normalized = normalize_ocr_text(text).replace(" ", "")
    if FULL_NUMBER_RE.match(normalized):
        return normalized, float(normalized), "bare_numeric"
    return None, None, None


def is_numericish_text(text: str) -> bool:
    normalized = normalize_ocr_text(text).replace(" ", "").strip()
    if not normalized or not re.search(r"\d", normalized):
        return False
    return bool(NUMERICISH_TEXT_RE.match(normalized))


def parse_numeric_value_relaxed(text: str) -> float | None:
    raw = normalize_ocr_text(text).replace(" ", "")
    raw = re.sub(r"[^0-9+.\-]", "", raw)
    if not raw or not re.search(r"\d", raw):
        return None
    negative = "-" in raw and "+" not in raw
    raw = raw.replace("+", "").replace("-", "")
    if not raw:
        return None
    if raw.count(".") > 1:
        first = raw.find(".")
        raw = raw[: first + 1] + raw[first + 1 :].replace(".", "")
    if raw.startswith("."):
        raw = "0" + raw
    if raw.endswith("."):
        raw = raw[:-1]
    if not raw or not re.search(r"\d", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    try:
        value = float(raw)
    except ValueError:
        value = None
    if value is None or abs(value) > 2.5:
        if len(digits) >= 3:
            value = float(f"0.{digits[:3]}")
        elif digits:
            value = float(f"0.{digits}")
        else:
            return None
    if negative:
        value = -abs(value)
    return value


def make_numeric_views(cell: np.ndarray) -> list[np.ndarray]:
    up = cv2.resize(cell, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 8)
    binary_inv = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 8)
    otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    return [up, cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR), cv2.cvtColor(binary_inv, cv2.COLOR_GRAY2BGR), cv2.cvtColor(otsu, cv2.COLOR_GRAY2BGR)]


def recover_numeric_text_from_box(image_bgr: np.ndarray, box_array: np.ndarray, coarse_text: str) -> str | None:
    x1 = max(0, int(np.floor(box_array[:, 0].min())) - 2)
    y1 = max(0, int(np.floor(box_array[:, 1].min())) - 2)
    x2 = min(image_bgr.shape[1], int(np.ceil(box_array[:, 0].max())) + 2)
    y2 = min(image_bgr.shape[0], int(np.ceil(box_array[:, 1].max())) + 2)
    if x2 <= x1 or y2 <= y1:
        return None
    cell = image_bgr[y1:y2, x1:x2]
    if cell.size == 0:
        return None
    expected_sign = -1 if "-" in normalize_ocr_text(coarse_text) else 0
    candidates: list[tuple[float, float, int, int]] = []
    for view in make_numeric_views(cell):
        raw, _ = OCR_ENGINE(view)
        for _box, text, score in raw or []:
            normalized_text = normalize_ocr_text(str(text))
            value = parse_numeric_value_relaxed(normalized_text)
            if value is None:
                continue
            if expected_sign < 0:
                value = -abs(value)
            digits = len(re.sub(r"\D", "", normalized_text))
            decimal_hint = 1 if any(ch in normalized_text for ch in ".,'`") else 0
            candidates.append((value, float(score), digits, decimal_hint))
    if not candidates:
        value = parse_numeric_value_relaxed(coarse_text)
        if value is None:
            return None
        if expected_sign < 0:
            value = -abs(value)
        return f"{value:.3f}"

    def rank(item: tuple[float, float, int, int]) -> tuple[int, int, int, float]:
        value, score, digits, decimal_hint = item
        return (1 if abs(value) <= 2.5 else 0, 1 if digits >= 3 else 0, decimal_hint, score)

    best_value, _best_score, _best_digits, _best_decimal_hint = max(candidates, key=rank)
    return f"{best_value:.3f}"


def make_textbox_candidate(
    image_bgr: np.ndarray,
    bbox: tuple[int, int, int, int],
    center: tuple[float, float],
    box_array: np.ndarray,
    text: str,
    score: float,
) -> tuple[TextBox, str] | None:
    parsed_text, value, candidate_kind = parse_measurement_text(str(text))
    if (parsed_text is None or value is None or candidate_kind is None) and is_numericish_text(str(text)):
        recovered_text = recover_numeric_text_from_box(image_bgr, box_array, str(text))
        recovered_value = parse_numeric_value_relaxed(recovered_text or "")
        if recovered_text is not None and recovered_value is not None:
            parsed_text = recovered_text
            value = recovered_value
            candidate_kind = "bare_numeric"
    if parsed_text is None or value is None or candidate_kind is None:
        return None
    bbox_x, bbox_y, bbox_w, bbox_h = bbox
    center_xy = box_array.mean(axis=0)
    center_x = float(center_xy[0])
    center_y = float(center_xy[1])
    norm_x = float((center_x - bbox_x) / max(bbox_w, 1))
    norm_y = float((center_y - bbox_y) / max(bbox_h, 1))
    dx = norm_x - 0.5
    dy = norm_y - 0.5
    radius = float(math.hypot(dx, dy))
    angle_cw = circular_angle_cw(center[0], center[1], center_x, center_y)
    zone = classify_zone(norm_x, norm_y, radius)
    box_w = float(box_array[:, 0].max() - box_array[:, 0].min())
    box_h = float(box_array[:, 1].max() - box_array[:, 1].min())
    return (
        TextBox(
            textbox_id=-1,
            text=parsed_text,
            value=value,
            score=float(score),
            center_x=center_x,
            center_y=center_y,
            box_w=box_w,
            box_h=box_h,
            norm_x=norm_x,
            norm_y=norm_y,
            radius=radius,
            angle_cw=angle_cw,
            zone=zone,
        ),
        candidate_kind,
    )


def extract_textboxes(image: np.ndarray, image_path: Path, bbox: tuple[int, int, int, int], center: tuple[float, float], min_score: float) -> list[TextBox]:
    raw, _ = OCR_ENGINE(str(image_path))
    bbox_x, bbox_y, bbox_w, bbox_h = bbox
    roi_x1 = bbox_x - bbox_w * 0.35
    roi_y1 = bbox_y - bbox_h * 0.25
    roi_x2 = bbox_x + bbox_w * 1.35
    roi_y2 = bbox_y + bbox_h * 1.18
    candidates: list[tuple[TextBox, str]] = []
    seen: list[tuple[float, float]] = []
    for box, text, score in raw or []:
        if float(score) < min_score:
            continue
        box_array = np.array(box, dtype=np.float32)
        center_xy = box_array.mean(axis=0)
        center_x = float(center_xy[0])
        center_y = float(center_xy[1])
        if center_x < roi_x1 or center_x > roi_x2 or center_y < roi_y1 or center_y > roi_y2:
            continue
        candidate = make_textbox_candidate(image, bbox, center, box_array, str(text), float(score))
        if candidate is None:
            continue
        textbox, kind = candidate
        if textbox.box_w < 90 or textbox.box_h < 35 or textbox.box_w > 360 or textbox.box_h > 140:
            continue
        if any(math.hypot(textbox.center_x - sx, textbox.center_y - sy) < 8 for sx, sy in seen):
            continue
        seen.append((textbox.center_x, textbox.center_y))
        candidates.append((textbox, kind))
    bare_candidates = [item for item, kind in candidates if kind == "bare_numeric"]
    bare_candidates.sort(key=lambda item: item.angle_cw)
    textboxes: list[TextBox] = []
    for idx, textbox in enumerate(bare_candidates, start=1):
        textbox.textbox_id = idx
        textboxes.append(textbox)
    return textboxes


def filter_points_by_contour(points: list[Point], contour: np.ndarray, min_signed_distance: float) -> list[Point]:
    kept: list[Point] = []
    point_id = 1
    for point in points:
        signed_distance = cv2.pointPolygonTest(contour, (float(point.x), float(point.y)), True)
        if signed_distance < min_signed_distance:
            continue
        kept.append(
            Point(
                point_id=point_id,
                x=point.x,
                y=point.y,
                area=point.area,
                width=point.width,
                height=point.height,
                norm_x=point.norm_x,
                norm_y=point.norm_y,
                radius=point.radius,
                angle_cw=point.angle_cw,
                zone=point.zone,
            )
        )
        point_id += 1
    return kept


def detect_image(image_path: Path, min_score: float) -> dict[str, Any]:
    image_bgr = cv2.imread(str(image_path))
    if image_bgr is None:
        raise FileNotFoundError(image_path)
    image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    point_mask, point_rgb, raw_points = detect_point_mask_and_color(image_rgb)
    contour_mask = build_contour_mask(image_bgr)
    contour, contour_center, bbox = select_part_contour(contour_mask)
    points = filter_points_by_contour([enrich_point(point, bbox, contour_center) for point in raw_points], contour, -10.0)
    textboxes = extract_textboxes(image_bgr, image_path, bbox, contour_center, min_score)
    return {
        "image_path": str(image_path),
        "image_url": f"/image?path={image_path.as_posix()}",
        "image_width": int(image_bgr.shape[1]),
        "image_height": int(image_bgr.shape[0]),
        "point_rgb": list(point_rgb),
        "bbox": {"x": bbox[0], "y": bbox[1], "w": bbox[2], "h": bbox[3]},
        "points": [asdict(point) for point in points],
        "textboxes": [asdict(textbox) for textbox in textboxes],
    }


def build_cost_matrix(points: list[Point], textboxes: list[TextBox]) -> np.ndarray:
    matrix = np.zeros((len(points), len(textboxes)), dtype=np.float32)
    for i, point in enumerate(points):
        for j, textbox in enumerate(textboxes):
            pos_cost = math.hypot(point.norm_x - textbox.norm_x, point.norm_y - textbox.norm_y)
            radius_cost = abs(point.radius - textbox.radius) * 0.2
            zone_penalty = 0.0 if point.zone == textbox.zone else 0.25
            matrix[i, j] = pos_cost + radius_cost + zone_penalty
    return matrix


def load_template(template_name: str) -> dict[str, Any]:
    template_path = TEMPLATE_DIR / f"{template_name}.json"
    if not template_path.exists():
        raise FileNotFoundError(template_path)
    return json.loads(template_path.read_text(encoding="utf-8"))


def save_template(template_name: str, image_path: str, ordered_point_ids: list[int], points: list[dict[str, Any]]) -> dict[str, Any]:
    point_by_id = {int(point["point_id"]): point for point in points}
    ordered = []
    for order_index, point_id in enumerate(ordered_point_ids, start=1):
        point = point_by_id[point_id]
        ordered.append({"order_index": order_index, "point_id": point_id, "norm_x": point["norm_x"], "norm_y": point["norm_y"], "zone": point["zone"]})
    payload = {"template_name": template_name, "source_image_path": image_path, "ordered_points": ordered}
    template_path = TEMPLATE_DIR / f"{template_name}.json"
    template_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def assign_order_to_points(points: list[dict[str, Any]], template: dict[str, Any]) -> list[dict[str, Any]]:
    remaining = [dict(point) for point in points]
    used: set[int] = set()
    ordered_points: list[dict[str, Any]] = []
    for item in template["ordered_points"]:
        best_idx = -1
        best_cost = float("inf")
        for idx, point in enumerate(remaining):
            point_id = int(point["point_id"])
            if point_id in used:
                continue
            cost = math.hypot(point["norm_x"] - item["norm_x"], point["norm_y"] - item["norm_y"])
            if point["zone"] != item["zone"]:
                cost += 0.15
            if cost < best_cost:
                best_cost = cost
                best_idx = idx
        if best_idx < 0:
            continue
        point = remaining[best_idx]
        point["order_index"] = int(item["order_index"])
        used.add(int(point["point_id"]))
        ordered_points.append(point)
    ordered_points.sort(key=lambda item: item["order_index"])
    return ordered_points


def match_ordered_points_to_textboxes(ordered_points: list[dict[str, Any]], textboxes: list[dict[str, Any]], max_cost: float) -> list[dict[str, Any]]:
    point_objs = [Point(**{key: point[key] for key in Point.__dataclass_fields__}) for point in ordered_points]
    textbox_objs = [TextBox(**{key: textbox[key] for key in TextBox.__dataclass_fields__}) for textbox in textboxes]
    if not point_objs or not textbox_objs:
        return []
    cost_matrix = build_cost_matrix(point_objs, textbox_objs)
    row_ind, col_ind = linear_sum_assignment(cost_matrix)
    rows: list[dict[str, Any]] = []
    for point_idx, textbox_idx in zip(row_ind, col_ind):
        cost = float(cost_matrix[point_idx, textbox_idx])
        if cost > max_cost:
            continue
        point = ordered_points[point_idx]
        textbox = textboxes[textbox_idx]
        rows.append(
            {
                "order_index": point["order_index"],
                "point_id": point["point_id"],
                "textbox_id": textbox["textbox_id"],
                "value": textbox["value"],
                "text": textbox["text"],
                "cost": cost,
                "point_x": point["x"],
                "point_y": point["y"],
                "textbox_x": textbox["center_x"],
                "textbox_y": textbox["center_y"],
            }
        )
    rows.sort(key=lambda item: item["order_index"])
    return rows


def export_results(image_path: str, template_name: str, ordered_points: list[dict[str, Any]], matched_rows: list[dict[str, Any]]) -> dict[str, Any]:
    export_name = f"{Path(image_path).stem}__{template_name}"
    csv_path = EXPORT_DIR / f"{export_name}.csv"
    json_path = EXPORT_DIR / f"{export_name}.json"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["order_index", "point_id", "direction", "point_x", "point_y", "textbox_id", "value", "text", "match_cost"])
        rows_by_order = {int(row["order_index"]): row for row in matched_rows}
        for point in ordered_points:
            row = rows_by_order.get(int(point["order_index"]))
            writer.writerow(
                [
                    point["order_index"],
                    point["point_id"],
                    point.get("direction", ""),
                    f'{point["x"]:.2f}',
                    f'{point["y"]:.2f}',
                    "" if row is None else (row.get("textbox_id") or ""),
                    "" if row is None or row.get("value") is None else f'{float(row["value"]):.6f}',
                    "" if row is None else row.get("text", ""),
                    "" if row is None or row.get("cost") is None else f'{float(row["cost"]):.4f}',
                ]
            )
    payload = {"image_path": image_path, "template_name": template_name, "ordered_points": ordered_points, "matched_rows": matched_rows, "csv_path": str(csv_path)}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"csv_path": str(csv_path), "json_path": str(json_path)}


ensure_dirs()
app = FastAPI(title="Point Order Tool")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/image")
def image(path: str = Query(...)) -> FileResponse:
    image_path = Path(path)
    if not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(image_path)


@app.get("/api/templates")
def list_templates() -> dict[str, list[str]]:
    return {"templates": sorted(path.stem for path in TEMPLATE_DIR.glob("*.json"))}


@app.post("/api/upload-image")
def api_upload_image(file: UploadFile = File(...)) -> JSONResponse:
    try:
        suffix = Path(file.filename or "uploaded.png").suffix or ".png"
        saved_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        with saved_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        return JSONResponse(
            {
                "image_path": str(saved_path),
                "image_url": f"/image?path={saved_path.as_posix()}",
                "filename": file.filename or saved_path.name,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        file.file.close()


@app.post("/api/detect")
def api_detect(request: DetectRequest) -> JSONResponse:
    try:
        return JSONResponse(detect_image(Path(request.image_path), request.ocr_min_score))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/save-template")
def api_save_template(request: SaveTemplateRequest) -> JSONResponse:
    try:
        payload = save_template(request.template_name.strip(), request.image_path, request.ordered_point_ids, request.points)
        return JSONResponse(payload)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/apply-template")
def api_apply_template(request: ApplyTemplateRequest) -> JSONResponse:
    try:
        template = load_template(request.template_name.strip())
        detection = detect_image(Path(request.image_path), request.ocr_min_score)
        ordered_points = assign_order_to_points(detection["points"], template)
        matched_rows = match_ordered_points_to_textboxes(ordered_points, detection["textboxes"], request.max_match_cost)
        export_paths = export_results(request.image_path, template["template_name"], ordered_points, matched_rows)
        return JSONResponse({"template": template, "detection": detection, "ordered_points": ordered_points, "matched_rows": matched_rows, "export": export_paths})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/export-edited")
def api_export_edited(request: ExportEditedRequest) -> JSONResponse:
    try:
        export_paths = export_results(
            request.image_path,
            request.template_name.strip(),
            request.ordered_points,
            request.matched_rows,
        )
        return JSONResponse(
            {
                "template_name": request.template_name.strip(),
                "ordered_points": request.ordered_points,
                "matched_rows": request.matched_rows,
                "export": export_paths,
            }
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local point ordering annotation tool.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
