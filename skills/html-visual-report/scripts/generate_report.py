#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车道线分析 → HTML 可视化报告生成器 (generate_report.py)
========================================================
功能: 从车道线感知 CSV + 视频数据, 自动生成深色风格 HTML 报告
      (与《车道线异常智能归因系统》报告同款效果)

用法:
  # 方式A: 指定数据目录 (含CSV/视频)
  python generate_report.py --data "D:\数据目录" --video "D:\视频目录" --mode full
  python generate_report.py --data "D:\数据目录" --mode light

  # 方式B: 上传数据 (先保存到本地目录, 再同方式A处理)
  # AI 内部: 上传文件保存到工作目录 → 调本脚本

参数:
  --data   数据根目录 (含 CSV 或 视频, 二选一必须)
  --video  视频目录 (可选, 默认 = data)
  --csv    指定单个 CSV (可选, 优先于 --data 扫描)
  --mode   full(完整版YOLOP) / light(轻量版Hough), 默认 full
  --out    输出报告路径 (默认 报告_时间.html)
  --open   生成后自动打开 (默认开)

输出: 单个自包含 HTML 文件 (图片base64内嵌, 离线可打开)
"""
import argparse
import base64
import csv
import datetime
import glob
import html as htmlmod
import io
import os
import re
import sys

# ==================== 配置 ====================
CONFIG = {
    "fps": 25,               # 视频帧率
    "head_offset_frames": 3, # 开头帧缓冲补偿
    "window_csv": 30,        # CSV 扫描窗口 ±s
    "img_quality": 55,       # 抽帧JPEG质量
    "img_max_w": 500,        # 抽帧最大宽度
    "width_warn": 4.0,       # 过宽预警
    "width_downgrade": 4.1,  # 过宽降级
    "width_narrow": 3.4,     # 过窄
    "view_drop": 30,         # 可视范围骤降阈值
    "jitter_th": 0.2,        # 抖动阈值
    "clarity_weights": {"w1": 0.25, "w2": 0.20, "w3": 0.20, "w4": 0.20, "w5": 0.15},
    "clarity_threshold": {"high": 100, "low": 60},
}


# ==================== 工具函数 ====================
def log(module, msg):
    print(f"[{module}] {msg}")


def hms_to_sec(hms):
    """HH:MM:SS -> 秒"""
    try:
        parts = str(hms).strip().split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return None


def sec_to_hms(sec):
    """秒 -> HH:MM:SS"""
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_time_str(t):
    """解析时间字符串 -> (date_str, sec)"""
    t = str(t).strip()
    # 格式: 2026-06-16 10:58:00 或 06-16 10:58:00
    m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}-\d{2})?\s*(\d{2}:\d{2}:\d{2})", t)
    if m:
        date_part = m.group(1) or ""
        return date_part, hms_to_sec(m.group(2))
    return "", None


def read_csv_handle(path):
    """读取CSV (自动处理编码)"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                return list(csv.reader(f))
        except (UnicodeDecodeError, OSError):
            continue
    return []


# ==================== CSV 解析与规则检测 ====================
def find_csv_in_dir(data_dir):
    """在目录里找所有 CSV"""
    csvs = []
    for root, _, files in os.walk(data_dir):
        for f in files:
            if f.lower().endswith(".csv"):
                csvs.append(os.path.join(root, f))
    return csvs


def find_video_in_dir(video_dir):
    """在目录里找所有视频 (递归)
    - 传完整视频目录(视频/): 只扫日期子文件夹 6.16/6.17/6.18, 跳过根目录散文件
    - 传日期子目录(视频/6.16): 正常扫描该目录全部视频
    """
    vids = []
    parent = os.path.abspath(os.path.dirname(video_dir.rstrip("\\/")))
    for root, _, files in os.walk(video_dir):
        # 只有当 video_dir 是根(视频/6.16/6.17)时跳过其父级散文件
        if os.path.abspath(root) == parent:
            continue
        for f in files:
            if f.lower().endswith((".ts", ".mp4", ".avi", ".mkv")):
                vids.append(os.path.join(root, f))
    return vids


def csv_date_to_video_dir(csv_path, video_dir):
    """从CSV文件名提取日期, 映射到视频日期子目录
    CSV: 2026-06-16_10-57-01...csv -> 日期 2026-06-16
    视频目录: video_dir/6.16/ (匹配 06-16)
    """
    name = os.path.basename(csv_path)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", name)
    if not m:
        return video_dir
    target = f"{int(m.group(2))}.{int(m.group(3))}"  # 6.16
    # 找匹配的日期子目录
    for d in os.listdir(video_dir):
        if os.path.isdir(os.path.join(video_dir, d)) and d == target:
            return os.path.join(video_dir, d)
    return video_dir


def parse_csv_lanes(rows):
    """解析车道线CSV, 返回 [{time_sec, lanes:[{id, c0, view_range_end}...]}]"""
    if not rows:
        return []
    # 找表头
    header = None
    for i, r in enumerate(rows[:10]):
        joined = " ".join(r)
        if "lane_mark" in joined or "timestamp" in joined.lower():
            header = r
            data_start = i + 1
            break
    if header is None:
        header = rows[0]
        data_start = 1

    # 找列索引
    idx = {}
    ts_col = None
    for ci, col in enumerate(header):
        cl = col.lower()
        if cl.strip() == "timestamp":
            ts_col = ci  # 优先: 纯 timestamp 列 (字符串时间)
        elif "msg_head.timestamp" in cl:
            continue     # 跳过 epoch 毫秒列
        elif "lane_marks" in cl and cl.endswith(".id"):
            idx.setdefault("ids", []).append(ci)
        elif "lane_marks" in cl and "curvature0" in cl:
            idx.setdefault("c0s", []).append(ci)
        elif "lane_marks" in cl and ("view_range" in cl or "range_end" in cl):
            idx.setdefault("ranges", []).append(ci)
    if ts_col is not None:
        idx["ts"] = ts_col
    elif "ts" not in idx:
        # 回退: 找任何含 timestamp 的列
        for ci, col in enumerate(header):
            if "timestamp" in col.lower():
                idx["ts"] = ci
                break

    # 解析数据行
    result = []
    for r in rows[data_start:]:
        if len(r) < 3:
            continue
        try:
            if "ts" not in idx:
                continue
            ts_str = r[idx["ts"]].strip()
            # 时间戳可能是数字(秒)或字符串
            if ts_str.replace(".", "").isdigit():
                tsec = float(ts_str)
            else:
                _, tsec = parse_time_str(ts_str)
                if tsec is None:
                    continue
            lane = {"id": None, "c0": None, "range": None}
            if idx.get("ids"):
                lane["id"] = int(float(r[idx["ids"][0]])) if r[idx["ids"][0]].strip() else None
            if idx.get("c0s"):
                lane["c0"] = float(r[idx["c0s"][0]]) if r[idx["c0s"][0]].strip() else None
            if idx.get("ranges"):
                lane["range"] = float(r[idx["ranges"][0]]) if r[idx["ranges"][0]].strip() else None
            result.append({"time_sec": tsec, "lane": lane})
        except (ValueError, IndexError):
            continue
    return result


def apply_rules(parsed):
    """应用异常规则 (缺失/抖动/宽度/可视范围), 返回错误点列表
    - 跳过开头3秒缓冲 (数据起始时感知未就绪, 大量假丢失)
    - 按秒聚合 + 同类连续帧聚合
    """
    if not parsed:
        return []
    points = []
    # 按时间分组 (1秒聚合)
    by_sec = {}
    for item in parsed:
        sec = int(item["time_sec"])
        by_sec.setdefault(sec, []).append(item["lane"])

    secs = sorted(by_sec.keys())
    # 跳过开头 3 秒缓冲
    start_sec = secs[0] + 3 if secs else 0
    prev = {}  # 上一秒每线 C0
    # 抖动计数: 每线连续|ΔC0|>=0.2 的帧数
    jitter_count = {}
    # 可视范围持续计数: 每线连续<30m 的帧数
    view_low_count = {}
    # 上一秒可视范围 (检测骤降)
    prev_view = {}

    for sec in secs:
        if sec < start_sec:
            continue
        lanes = by_sec[sec]
        # 本车道线 id=1/-1
        left = next((l for l in lanes if l["id"] == 1), None)
        right = next((l for l in lanes if l["id"] == -1), None)

        labels = []

        # 规则一: 缺失
        if left is None:
            labels.append("左侧丢失")
        if right is None:
            labels.append("右侧丢失")

        # 规则三: 宽度 W = |C0左| + |C0右|
        # 双阈值: ≤3.4m 过窄; ≥4.0m 预警; ≥4.1m 持续5帧降级
        if left and right and left["c0"] is not None and right["c0"] is not None:
            w = abs(left["c0"]) + abs(right["c0"])
            if w <= CONFIG["width_narrow"]:
                labels.append(f"车道线过窄 {w:.3f}m")
            elif w >= CONFIG["width_downgrade"]:
                labels.append(f"车道线过宽降级 {w:.3f}m")
            elif w >= CONFIG["width_warn"]:
                labels.append(f"车道线过宽预警 {w:.3f}m")

        # 规则二: 抖动 |ΔC0|>=0.2 连续3帧记为一次抖动事件
        for side, lane in (("左", left), ("右", right)):
            if lane and lane["c0"] is not None and side in prev:
                dc0 = abs(lane["c0"] - prev[side])
                if dc0 >= CONFIG["jitter_th"]:
                    jitter_count[side] = jitter_count.get(side, 0) + 1
                    if jitter_count[side] == 3:
                        labels.append(f"抖动 ΔC0={dc0:.2f}m")
                        jitter_count[side] = 0
                else:
                    jitter_count[side] = 0
            elif side in jitter_count:
                jitter_count[side] = 0

        # 规则四: 可视范围
        # 子条件A 骤降: 从 >=60m 骤降至 <30m (相邻帧)
        # 子条件B 持续短: 持续 <30m 超过10帧(≈1秒)
        for side, lane in (("左", left), ("右", right)):
            if lane and lane["range"] is not None:
                r = lane["range"]
                if r <= 0:
                    # 无效槽位(0m)不算可视异常, 记录上一值
                    if side in prev_view:
                        prev_view[side] = r
                    continue
                # 骤降检测
                if side in prev_view and prev_view[side] >= 60 and r < 30:
                    labels.append(f"可视范围骤降 {int(prev_view[side])}→{int(r)}m")
                # 持续短检测
                if r < CONFIG["view_drop"]:
                    view_low_count[side] = view_low_count.get(side, 0) + 1
                    if view_low_count[side] >= 10:
                        labels.append(f"可视范围持续<30m({int(r)}m)")
                else:
                    view_low_count[side] = 0
                prev_view[side] = r
            else:
                if side in view_low_count:
                    view_low_count[side] = 0

        # 记录本帧 C0
        for side, lane in (("左", left), ("右", right)):
            if lane and lane["c0"] is not None:
                prev[side] = lane["c0"]

        if labels:
            points.append({"sec": sec, "labels": labels})

    # ===== 丢失事件聚合: 连续丢失(间隔<=2s)合并为一次事件 =====
    aggregated = []
    i = 0
    while i < len(points):
        cur = points[i]
        # 如果当前秒只有"丢失"标签, 尝试聚合
        lost_labels = [lb for lb in cur["labels"] if "丢失" in lb]
        other_labels = [lb for lb in cur["labels"] if "丢失" not in lb]
        if lost_labels:
            # 找连续丢失段 (间隔<=2s)
            j = i + 1
            while j < len(points) and points[j]["sec"] - points[j-1]["sec"] <= 2:
                j += 1
            # 段内合并丢失标签 (去重); 丢失期间的抖动/其他标签不保留
            merged_lost = set()
            for k in range(i, j):
                for lb in points[k]["labels"]:
                    if "丢失" in lb:
                        merged_lost.add(lb)
            merged = sorted(merged_lost)
            # 规则五: 丢失事件链 - 记录恢复时长 (丢失结束到线重新出现)
            lost_sec = cur["sec"]
            end_sec = points[j-1]["sec"]
            # 找恢复时刻: 丢失结束后第一个有线(非丢失)的秒
            recover_sec = None
            for k in range(j, len(points)):
                if "丢失" not in " ".join(points[k]["labels"]):
                    recover_sec = points[k]["sec"]
                    break
            # 前兆关联: 丢失开始前 5s 内是否有前兆(可视范围骤降/持续短)
            has_warning = False
            for k in range(i - 1, max(-1, i - 6), -1):
                if k >= 0 and any("可视" in lb for lb in points[k]["labels"]):
                    has_warning = True
                    break
            event = {
                "sec": lost_sec,
                "labels": merged,
                "end_sec": end_sec,
                "recover_sec": recover_sec,
                "duration_s": end_sec - lost_sec + 1,
                "has_warning": has_warning,
            }
            aggregated.append(event)
            i = j
        else:
            aggregated.append(cur)
            i += 1

    return aggregated


# ==================== 清晰度评估 ====================
def clarity_score(frame):
    """S = w1*L + w2*E + w3*C + w4*B + w5*F"""
    import cv2
    import numpy as np
    wgt = CONFIG["clarity_weights"]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # L: Laplacian 方差
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    L = float(lap.var())

    # E: 边缘密度 (Canny 边缘像素比例)
    edges = cv2.Canny(gray, 50, 150)
    E = float(np.mean(edges > 0) * 100)

    # C: 对比度 (灰度标准差)
    C = float(gray.std())

    # B: Brenner 梯度
    gy = np.abs(np.diff(gray.astype(np.float32), axis=0))
    B = float(np.mean(gy * gy))

    # F: FFT 能量
    f = np.fft.fft2(gray)
    fshift = np.fft.fftshift(f)
    F = float(np.mean(np.abs(fshift)))

    S = wgt["w1"] * (L / 100) + wgt["w2"] * E + wgt["w3"] * C + wgt["w4"] * B + wgt["w5"] * F
    return S, {"L": L, "E": E, "C": C, "B": B, "F": F}


# ==================== 车道线检测 (双模式) ====================
_YOLOP_SESSION = None
_YOLOP_MODEL = None


def _find_model():
    """找模型文件"""
    global _YOLOP_MODEL
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(script_dir, "..", "models", "yolop-640-640.onnx"),
        os.path.join(script_dir, "models", "yolop-640-640.onnx"),
    ]
    for c in cands:
        if os.path.exists(c):
            _YOLOP_MODEL = c
            return c
    return None


def _yolop_session():
    global _YOLOP_SESSION
    if _YOLOP_SESSION is not None:
        return _YOLOP_SESSION
    import onnxruntime as ort
    mp = _find_model()
    if mp is None:
        return None
    _YOLOP_SESSION = ort.InferenceSession(mp, providers=["CPUExecutionProvider"])
    return _YOLOP_SESSION


def detect_lane_yolop(frame):
    """YOLOP 语义分割检测: 返回 (left_ok, right_ok, width_m, pixels)"""
    import cv2
    import numpy as np
    sess = _yolop_session()
    if sess is None:
        return None
    try:
        h, w = frame.shape[:2]
        # CLAHE 增强 (画面偏暗)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        img_eq = cv2.cvtColor(clahe.apply(gray), cv2.COLOR_GRAY2BGR)
        img = cv2.cvtColor(img_eq, cv2.COLOR_BGR2RGB)
        inp = np.transpose(cv2.resize(img, (640, 640)).astype(np.float32) / 255.0, (2, 0, 1))[None]
        det, drv, lane = sess.run(None, {"images": inp})
        mask = np.argmax(lane[0], axis=0)
        lane_full = cv2.resize((mask == 1).astype(np.uint8) * 255, (w, h),
                               interpolation=cv2.INTER_NEAREST)
        # 搜索区 0.30w~0.70w 聚类线簇
        x_lo, x_hi = int(w * 0.30), int(w * 0.70)
        roi = lane_full[int(h * 0.5):, x_lo:x_hi]
        col_sum = np.sum(roi > 0, axis=0)
        xs = np.where(col_sum > 0)[0]
        if len(xs) == 0:
            return {"left_ok": False, "right_ok": False, "width_m": None, "pixels": 0}
        xs = xs + x_lo
        clusters = []
        cur = [xs[0]]
        for i in range(1, len(xs)):
            if xs[i] - xs[i-1] > 15:
                clusters.append(int(np.mean(cur)))
                cur = [xs[i]]
            else:
                cur.append(xs[i])
        clusters.append(int(np.mean(cur)))
        center = w // 2
        lefts = [c for c in clusters if c < center]
        rights = [c for c in clusters if c >= center]
        left_ok = len(lefts) > 0
        right_ok = len(rights) > 0
        left_x = max(lefts) if left_ok else None
        right_x = min(rights) if right_ok else None
        width_m = None
        if left_ok and right_ok:
            ref_px = int(w * 0.72) - int(w * 0.28)
            px = right_x - left_x
            if px > 0:
                width_m = 3.65 * px / ref_px
        return {"left_ok": left_ok, "right_ok": right_ok, "width_m": width_m,
                "pixels": int(np.sum(mask == 1))}
    except Exception:
        return None


def detect_lane_hough(frame):
    """Hough 检测: 返回 (left_ok, right_ok, width_m)"""
    import cv2
    import numpy as np
    try:
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray_eq = clahe.apply(gray)
        edges = cv2.Canny(gray_eq, 30, 90)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 40, minLineLength=60, maxLineGap=30)
        if lines is None:
            return {"left_ok": False, "right_ok": False, "width_m": None}
        la = lines[:, 0] if lines.ndim == 3 else lines
        left_xs, right_xs = [], []
        for l in la:
            x1, y1, x2, y2 = int(l[0]), int(l[1]), int(l[2]), int(l[3])
            if x2 == x1:
                continue
            slope = (y2 - y1) / (x2 - x1)
            length = np.hypot(x2 - x1, y2 - y1)
            if length < 60 or abs(slope) < 0.2 or abs(slope) > 1.5:
                continue
            mid_x = (x1 + x2) / 2
            if slope < 0 and mid_x < w * 0.6:
                left_xs.append(mid_x)
            elif slope > 0 and mid_x > w * 0.4:
                right_xs.append(mid_x)
        left_ok = len(left_xs) > 0
        right_ok = len(right_xs) > 0
        width_m = None
        if left_ok and right_ok:
            # 左线取最大mid_x(靠右), 右线取最小mid_x(靠左) -> 本车道宽度
            left_edge = max(left_xs)
            right_edge = min(right_xs)
            if right_edge > left_edge:
                ref_px = int(w * 0.72) - int(w * 0.28)
                width_m = 3.65 * (right_edge - left_edge) / ref_px
        return {"left_ok": left_ok, "right_ok": right_ok, "width_m": width_m}
    except Exception:
        return None


def detect_lane_in_frame(frame, mode="full"):
    """统一入口: 完整版用YOLOP(无模型回退Hough), 轻量版用Hough"""
    if mode == "full":
        det = detect_lane_yolop(frame)
        if det is not None and det["pixels"] > 0:
            det["method"] = "yolop"
            return det
        det = detect_lane_hough(frame)
        if det:
            det["method"] = "hough"
            return det
        return {"left_ok": False, "right_ok": False, "width_m": None, "method": "none"}
    else:
        det = detect_lane_hough(frame)
        if det is None:
            det = {"left_ok": False, "right_ok": False, "width_m": None}
        det["method"] = "hough"
        return det


# ==================== 双维度校验 ====================
def dual_check(csv_labels, det, csv_w=None):
    """CSV 异常 vs 画面实测 → 结论 (按标签类型判定)
    - 过宽/过窄: 比较画面实测宽度
    - 丢失: 检查画面是否真的无线 (两侧都无=真实丢失, 有线=误报)
    - 可视范围: 画面检测正常则待人工
    - 抖动: 需多帧验证, 给待确认
    """
    has_csv_anomaly = bool(csv_labels)
    if not has_csv_anomaly:
        return "✅ 双维度正常", "CSV无异常"

    labels = " ".join(csv_labels)
    # 丢失类: 画面两侧都检不到线 = 真实丢失; 检到线 = 疑似误报
    if "丢失" in labels:
        if not det["left_ok"] and not det["right_ok"]:
            return "✅ 真实异常", "CSV报丢失且画面两侧均未检出车道线, 丢失属实"
        if det["left_ok"] and det["right_ok"]:
            w = det.get("width_m")
            if w is not None:
                return "⚠️ 疑似感知误报", f"CSV报丢失但画面实测两侧均有线(宽{w:.2f}m), 疑误报"
            return "⚠️ 疑似感知误报", "CSV报丢失但画面实测两侧均有线, 疑误报"
        side = "左" if det["left_ok"] else "右"
        return "🔍 需人工复核", f"CSV报丢失, 画面{side}侧有线另一侧无, 需人工确认"

    # 过宽/过窄类: 比较画面实测宽度
    if "过宽" in labels or "过窄" in labels:
        if not det["left_ok"] or not det["right_ok"]:
            return "🔍 需人工复核", "画面车道线检测不完全, 建议人工查看截图确认"
        if det.get("pixels", 99999) < 3000 and det.get("method") == "yolop":
            return "🔍 需人工复核", f"YOLOP检测像素仅{det.get('pixels')}(不可靠), 建议人工确认"
        w = det.get("width_m")
        if w is not None:
            if "过宽" in labels and w >= CONFIG["width_warn"]:
                return "✅ 真实异常", f"CSV报过宽且画面实测{w:.2f}m, 画面确实偏宽"
            if "过窄" in labels and w <= CONFIG["width_narrow"]:
                return "✅ 真实异常", f"CSV报过窄且画面实测{w:.2f}m, 画面确实偏窄"
            return "⚠️ 疑似感知误报", f"CSV报宽度异常但画面实测{w:.2f}m(正常范围3.4-4.0m)"
        return "🔍 需人工复核", "画面宽度无法计算, 建议人工确认"

    # 可视范围/抖动等: 画面检测正常则待人工
    if not det["left_ok"] or not det["right_ok"]:
        return "🔍 需人工复核", "画面车道线检测不完全, 建议人工查看截图确认"
    return "🔍 需人工复核", "CSV检测到异常, 画面车道线存在, 需结合多帧/场景人工判断"


# ==================== 视频抽帧 ====================
def read_frames_sequential(video_path, target_frames):
    """顺序读取目标帧 (TS必须顺序read)"""
    import cv2
    cap = cv2.VideoCapture(video_path)
    frames = {}
    count = 0
    for i in range(max(target_frames) + 1 if target_frames else 0):
        ret, f = cap.read()
        if not ret:
            break
        if i in target_frames:
            frames[i] = f
        count += 1
    cap.release()
    return frames


def find_video_for_sec(video_dir, target_sec):
    """按文件名时间戳匹配视频 (ND02512_HHMMSS_vedio_chn4.ts)
    铁律: 视频覆盖范围 [vstart-90, vstart+60] 应包含错误点时间
    - 问题时间点可能比视频起始早几十秒 (视频从问题后开始录)
    - 抽帧时 extract_frames 内部仍严格检查 [vstart, v_end]
    """
    best = None
    best_d = 10 ** 9
    for v in find_video_in_dir(video_dir):
        name = os.path.basename(v)
        m = re.search(r"_(\d{6})_", name)
        if m:
            try:
                vsec = hms_to_sec(f"{m.group(1)[:2]}:{m.group(1)[2:4]}:{m.group(1)[4:]}")
                # 视频覆盖范围放宽到 [vstart-90, vstart+60]
                if vsec - 90 <= target_sec < vsec + 60:
                    d = target_sec - vsec
                    if abs(d) < best_d:
                        best = (v, vsec)
                        best_d = abs(d)
            except Exception:
                continue
    return best if best else (None, None)


def extract_frames(video_path, vstart, b_sec, offsets=(-1, 0, 1)):
    """抽 b±offset 秒的帧 (严格时间范围检查)
    铁律: 错误点必须在视频时间范围内才抽帧 [vstart, vstart+60]
    - 帧号 = (b_sec - vstart)*25 + 3帧补偿
    - 不在范围 -> 返回 None (绝不硬截, 否则画面与标签不符)
    返回 [(label, frame, real_sec), ...]
    """
    import cv2
    frames = []
    # 视频时长: TS 无法用 CAP_PROP_FRAME_COUNT, 顺序探测一次
    cap = cv2.VideoCapture(video_path)
    total = 0
    while True:
        ret, _ = cap.read()
        if not ret:
            break
        total += 1
    cap.release()
    v_end = vstart + total / CONFIG["fps"]  # 视频结束时刻

    for off in offsets:
        sec = b_sec + off
        # 关键: 错误点必须在视频范围内 [vstart, v_end]
        if sec < vstart or sec > v_end:
            frames.append((f"b{'+' if off > 0 else ''}{off}s", None, sec))
            continue
        frame_no = (sec - vstart) * CONFIG["fps"] + CONFIG["head_offset_frames"]
        frame_no = max(0, min(int(frame_no), total - 1))
        # 读取该帧 (顺序读)
        cap2 = cv2.VideoCapture(video_path)
        frame = None
        for i in range(frame_no + 1):
            ret, f = cap2.read()
            if not ret:
                break
            frame = f
        cap2.release()
        frames.append((f"b{'+' if off > 0 else ''}{off}s", frame, sec))
    return frames


def frame_to_b64(frame, quality=None, max_w=None):
    """帧转base64 (imencode 支持中文路径)"""
    import cv2
    if frame is None:
        return None
    quality = quality or CONFIG["img_quality"]
    max_w = max_w or CONFIG["img_max_w"]
    h, w = frame.shape[:2]
    if w > max_w:
        frame = cv2.resize(frame, (max_w, int(h * max_w / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        return base64.b64encode(buf.tobytes()).decode("ascii")
    return None


# ==================== 问题表 (xlsx) 驱动 ====================
def load_issue_table(xlsx_path):
    """读取问题表 xlsx, 返回问题列表
    结构: 第7行表头(编号/日期/时间点/问题归类/问题现象描述/环境特性...), 第8行起数据
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    issues = []
    for row in ws.iter_rows(min_row=8, values_only=True):
        num = row[0]
        if num is None:
            continue
        desc = str(row[4]).strip() if row[4] else ""
        env = str(row[5]).strip() if row[5] else ""
        issues.append({
            "num": int(num),
            "date": str(row[1])[:10] if row[1] else "",
            "time": str(row[2])[:8] if row[2] else "",
            "category": str(row[3]).strip() if row[3] else "",
            "desc": desc,
            "env": env,
            "scene": extract_scene(desc),  # 从描述提取道路环境 (隧道/弯道/合流等)
        })
    return issues


# 道路环境关键词 -> 场景标签
SCENE_KEYWORDS = [
    ("隧道", ["隧道"]),
    ("弯道", ["弯道", "左弯", "右弯", "急弯", "弯"]),
    ("分合流", ["合流", "分流", "交汇口", "交汇", "汇流", "分叉"]),
    ("直道", ["直道", "直路"]),
    ("鱼骨线", ["鱼骨线", "鱼骨"]),
    ("坡道", ["坡", "下坡", "上坡"]),
    ("匝道", ["匝道"]),
    ("换道", ["换道", "变道", "拨杆"]),
    ("进隧道", ["入隧道", "进隧道", "隧道口"]),
    ("出隧道", ["出隧道", "隧道出口"]),
]


def extract_scene(desc):
    """从问题描述提取道路环境场景标签"""
    scene = []
    for label, kws in SCENE_KEYWORDS:
        if any(k in desc for k in kws):
            if label not in scene:
                scene.append(label)
    return scene


FILTER_KEYWORDS = ["车道线", "lane", "压线", "越线", "跨线", "蛇形", "蛇行"]


def filter_issues(issues, keywords=None):
    """筛选车道线相关问题 (描述命中关键词)"""
    kws = keywords or FILTER_KEYWORDS
    out = []
    for p in issues:
        if any(k.lower() in p["desc"].lower() for k in kws):
            out.append(p)
    return out


def issue_time_sec(issue):
    """问题时间 -> 秒 (处理各种格式: 10:58:00 / 11.06 / 17::24 / 10：04)"""
    t = issue["time"].replace("：", ":").replace(".", ":").replace("::", ":")
    m = re.search(r"(\d{1,2}):(\d{1,2}):?(\d{0,2})", t)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        s = int(m.group(3)) if m.group(3) else 0
        return h * 3600 + mi * 60 + s
    return None


def find_csv_for_issue(csv_dir, issue):
    """按问题日期+时间匹配CSV (CSV文件名含起始时间, 10分钟段覆盖)"""
    date = issue["date"]  # 2026-06-16
    sec = issue_time_sec(issue)
    if not sec:
        return None
    for csv_path in find_csv_in_dir(csv_dir):
        name = os.path.basename(csv_path)
        # 日期匹配
        if not name.startswith(date):
            continue
        # 时间匹配: CSV起始时间 <= 问题时间 < 起始+600s
        # 文件名格式: 2026-06-16_10-57-01.bagperception...
        m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", name)
        if not m:
            continue
        csv_start = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
        if csv_start <= sec < csv_start + 600:
            return csv_path
    return None


# ==================== 报告生成 ====================
def generate_html(data, out_path):
    """生成深色 HTML 报告 (同现有报告效果)"""
    css_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "assets", "report_style.css")
    css = ""
    if os.path.exists(css_path):
        with open(css_path, encoding="utf-8") as f:
            css = f.read()

    # 统计
    total_points = sum(len(p["points"]) for p in data["problems"])
    total_frames = sum(len(pt.get("frames", [])) for p in data["problems"] for pt in p["points"])

    # 趋势数据
    trend_problem = [(str(p["num"]), len(p["points"])) for p in data["problems"]]
    max_p = max((n for _, n in trend_problem), default=1)
    type_count = {"车道线过宽": 0, "车道线过窄": 0, "丢失": 0, "抖动": 0, "可视范围": 0}
    dc_count = {}
    for p in data["problems"]:
        for pt in p["points"]:
            for lb in pt["labels"]:
                if "过宽" in lb: type_count["车道线过宽"] += 1
                elif "过窄" in lb: type_count["车道线过窄"] += 1
                elif "丢失" in lb: type_count["丢失"] += 1
                elif "抖动" in lb: type_count["抖动"] += 1
                elif "可视" in lb: type_count["可视范围"] += 1
            dc = pt.get("dual_check", "")
            if dc:
                key = dc.split(" ")[0]
                dc_count[key] = dc_count.get(key, 0) + 1
    max_t = max(type_count.values(), default=1)
    max_dc = max(dc_count.values(), default=1)

    # 统计卡
    stats = f"""
  <div class="stat"><div class="n">{len(data['problems'])}</div><div class="l">问题数</div></div>
  <div class="stat"><div class="n">{total_points}</div><div class="l">检出错误点</div></div>
  <div class="stat"><div class="n">{total_frames}</div><div class="l">视频截图</div></div>"""

    # 趋势图
    def bar_rows(items, max_val, color):
        rows = []
        for label, val in items:
            pct = int(val / max_val * 100) if max_val else 0
            rows.append(f'<div class="bar-row"><span class="bar-label">{htmlmod.escape(str(label))}</span>'
                        f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div>'
                        f'<span class="bar-val">{val}</span></div>')
        return "\n".join(rows)

    trend_html = f"""
<h2 class="trend-h">📈 异常趋势分析</h2>
<div class="trend-grid">
  <div class="trend-card">
    <div class="trend-title">按问题编号的异常分布</div>
    {bar_rows(trend_problem, max_p, "var(--blue)")}
  </div>
  <div class="trend-card">
    <div class="trend-title">异常类型占比</div>
    {bar_rows(type_count.items(), max_t, "var(--yellow)")}
  </div>
  <div class="trend-card">
    <div class="trend-title">双维度校验结论分布</div>
    {bar_rows(dc_count.items(), max_dc, "var(--red)")}
  </div>
</div>"""

    # 问题卡片
    cards = []
    for p in data["problems"]:
        scene_html = " ".join(f'<span class="sc">{htmlmod.escape(s)}</span>' for s in p.get("scene", []))
        evs = []
        for pt in p["points"]:
            labels_html = "".join(f'<span class="et">{htmlmod.escape(lb)}</span>' for lb in pt["labels"])
            # 规则五: 丢失事件链信息 (有前兆/突发/恢复时长)
            chain_html = ""
            if pt.get("is_lost_event"):
                chain = "🔵 有前兆的丢失" if pt.get("has_warning") else "🔴 突发丢失"
                cls = "chain-warn" if pt.get("has_warning") else "chain-burst"
                parts = [f'<span class="chain-item {cls}">{chain}</span>',
                         f'<span class="chain-item chain-warn">持续 {pt.get("duration_s", 1)}s</span>']
                if pt.get("recover_hms"):
                    parts.append(f'<span class="chain-item chain-warn">恢复 {htmlmod.escape(str(pt["recover_hms"]))}</span>')
                chain_html = ('<div class="chains"><span class="chains-title">🔗 丢失事件链 (规则五)</span>'
                              '<div class="chains-body">' + "".join(parts) + '</div></div>')
            frames_html = ""
            frames = pt.get("frames", [])
            if frames and any(f["b64"] for f in frames):
                fgrid = []
                for fr in frames:
                    if fr["b64"]:
                        fgrid.append(f'<div class="frame"><img src="data:image/jpeg;base64,{fr["b64"]}">'
                                     f'<div class="cap">{htmlmod.escape(fr["label"])}</div></div>')
                    else:
                        fgrid.append(f'<div class="frame"><div class="no-data">📭 无帧</div></div>')
                frames_html = f'<div class="frames">{"".join(fgrid)}</div>'
            else:
                frames_html = '<div class="no-data">📭 无视频帧</div>'

            clr_html = ""
            if pt.get("clarity"):
                clr_html = f'<div class="clr">清晰度 S={pt["clarity"]:.1f}</div>'
            dc_html = ""
            if pt.get("dual_check"):
                dc_html = (f'<div class="dual-check"><span class="dc-label">双维度校验: </span>'
                           f'<span class="dc-concl">{htmlmod.escape(pt["dual_check"])}</span>'
                           f'<span class="dc-reason">{htmlmod.escape(pt.get("dual_reason", ""))}</span></div>')

            evs.append(f"""
<div class="event">
  <div class="ev-head">
    <span class="ev-idx">{htmlmod.escape(pt.get('b', 'b?'))}</span>
    <span class="ev-time">⏱ {htmlmod.escape(pt.get('time', ''))}</span>
    <span class="ev-types">{labels_html}</span>
  </div>
  {frames_html}
  {chain_html}
  {clr_html}
  {dc_html}
</div>""")

        cards.append(f"""
<details class="problem-card" >
  <summary>
    <span class="pnum">#{p['num']}</span>
    <span class="ptime">{htmlmod.escape(p.get('time', ''))}</span>
    <span class="pcat">{htmlmod.escape(p.get('category', ''))}</span>
    <span class="pscene">{scene_html}</span>
    <span class="pdata">CSV{'✅' if p.get('csv_ok') else '❌'} 视频{'✅' if p.get('video_ok') else '❌'}</span>
    <span class="status">{len(p['points'])}个错误点</span>
  </summary>
  <div class="pbody">
    <div class="pdesc"><b>描述:</b> {htmlmod.escape(p.get('desc', ''))}</div>
    <div class="ev-count">A={htmlmod.escape(p.get('time', ''))} ±{CONFIG['window_csv']}s 检出 <b>{len(p['points'])}</b> 个错误点</div>
    {''.join(evs)}
  </div>
</details>""")

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{htmlmod.escape(data.get('title', '车道线分析报告'))}</title>
<style>
{css}
</style></head>
<body>
<h1>🚗 {htmlmod.escape(data.get('title', '车道线分析报告'))}</h1>
<div class="sub">模式: {data.get('mode', 'full')} · 生成时间 {data.get('gen_time', '')} · {data.get('sub', '')}</div>
<div class="stats">{stats}</div>
{trend_html}
{''.join(cards)}
</body></html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ==================== 主流程 ====================
def analyze_csv(csv_path, b_sec=None, window=None):
    """分析单个CSV: 返回错误点列表
    有 b_sec 时只保留 b_sec±window 窗口内的错误点 (默认±30s)
    """
    rows = read_csv_handle(csv_path)
    parsed = parse_csv_lanes(rows)
    if not parsed:
        return []
    points = apply_rules(parsed)
    if b_sec is not None:
        w = window or CONFIG["window_csv"]
        lo, hi = b_sec - w, b_sec + w
        # 保留与窗口重叠的事件: 事件起点<=窗口右界 且 (事件终点或起点)>=窗口左界
        filtered = []
        for p in points:
            p_end = p.get("end_sec", p["sec"])
            if p["sec"] <= hi and p_end >= lo:
                filtered.append(p)
        points = filtered
    return points


# ==================== 问题表驱动处理 ====================
def process_issue(issue, csv_dir, video_dir, mode):
    """按问题表条目处理: 定位CSV -> 规则检测 -> 抽帧 -> 校验
    返回问题dict (含 #编号/日期/时间/归类/描述/环境/状态标记)
    """
    # 1. 定位 CSV
    csv_path = find_csv_for_issue(csv_dir, issue)
    csv_ok = csv_path is not None
    if not csv_ok:
        return {
            "num": issue["num"], "time": f"{issue['date']} {issue['time']}",
            "category": issue["category"],
            "scene": issue.get("scene", []) + (["天气:" + issue["env"]] if issue.get("env") else []),
            "desc": issue["desc"], "points": [], "csv_ok": False, "video_ok": False,
            "video_name": "", "env": issue.get("env", ""),
        }
    # 2. 规则检测 (问题时间点 ±30s 窗口)
    b_sec = issue_time_sec(issue)
    points = analyze_csv(csv_path, b_sec)
    # 按日期定位视频目录
    dated_video_dir = csv_date_to_video_dir(csv_path, video_dir)
    # 3. 抽帧 + 校验: 每个错误点事件独立渲染 (含错误类型标签)
    out_points = []
    vpath, vstart = None, None
    if video_dir:
        vpath, vstart = find_video_for_sec(dated_video_dir, b_sec or 0)
    # 抽帧时间点: 问题时间点(在视频内), 错误类型=窗口内事件合并
    extract_secs = [b_sec] if b_sec else []
    # 合并窗口内所有事件的标签 (去重) + 事件链信息
    merged_labels = []
    lost_event = None
    for p in points[:10]:
        for lb in p["labels"]:
            if lb not in merged_labels:
                merged_labels.append(lb)
        if lost_event is None and "丢失" in " ".join(p["labels"]):
            # 记录丢失事件链信息 (首个丢失事件)
            lost_event = {
                "is_lost_event": True,
                "has_warning": p.get("has_warning", False),
                "duration_s": p.get("duration_s", 1),
                "recover_hms": sec_to_hms(p["recover_sec"]) if p.get("recover_sec") else "",
            }
    labels_map = {b_sec: merged_labels} if b_sec else {}
    if not extract_secs:
        # 无问题时间: 用第一个错误点
        if points:
            extract_secs = [points[0]["sec"]]
            labels_map = {points[0]["sec"]: points[0]["labels"]}
    for pi, pt_sec in enumerate(extract_secs[:5]):
        pt_labels = labels_map.get(pt_sec, [])
        # 该错误点的事件链信息
        pt_chain = lost_event if lost_event else {}
        frames = []
        if vpath and vstart:
            raw = extract_frames(vpath, vstart, pt_sec)
            for label, fr, real_sec in raw:
                if fr is not None:
                    cap_label = f"{label} {sec_to_hms(int(real_sec))}"
                    frames.append({"label": cap_label, "b64": frame_to_b64(fr)})
                else:
                    frames.append({"label": label, "b64": None})
        # 清晰度 + 校验
        clarity = None
        dual = None
        dual_reason = ""
        if vpath and vstart:
            import cv2
            import numpy as np
            import base64 as b64mod
            b1_frame = next((fr for fr in frames if fr["label"].startswith("b0s") and fr["b64"]), None)
            if b1_frame:
                raw = b64mod.b64decode(b1_frame["b64"])
                arr = np.frombuffer(raw, dtype=np.uint8)
                fr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if fr is not None:
                    clarity, _ = clarity_score(fr)
                    det = detect_lane_in_frame(fr, mode)
                    dual, dual_reason = dual_check(pt_labels, det)
        out_points.append({
            "b": f"b{pi+1}",
            "time": sec_to_hms(pt_sec),
            "labels": pt_labels,
            "frames": frames,
            "clarity": clarity,
            "dual_check": dual,
            "dual_reason": dual_reason,
            **pt_chain,
        })
    return {
        "num": issue["num"],
        "time": f"{issue['date']} {issue['time']}",
        "category": issue["category"],
        "scene": issue.get("scene", []) + (["天气:" + issue["env"]] if issue.get("env") else []),
        "desc": issue["desc"],
        "points": out_points,
        "csv_ok": csv_ok,
        "video_ok": bool(vpath and vstart and any(fr["b64"] for pt2 in out_points for fr in pt2["frames"])),
        "video_name": os.path.basename(vpath) if vpath else "",
        "env": issue.get("env", ""),
    }


def process_single_csv(csv_path, video_dir, mode, b_sec=None):
    """处理单个CSV: 规则检测 + 抽帧 + 校验, 返回问题dict
    视频匹配: 按CSV日期选择对应日期子目录 (6.16/6.17/6.18)
    """
    points = analyze_csv(csv_path, b_sec)
    # 按CSV日期定位视频目录
    dated_video_dir = csv_date_to_video_dir(csv_path, video_dir)
    # 为错误点抽帧+校验
    out_points = []
    vpath, vstart = None, None
    if points and video_dir:
        vpath, vstart = find_video_for_sec(dated_video_dir, points[0]["sec"])
    for pi, pt in enumerate(points[:10]):  # 最多10个错误点
        frames = []
        if vpath and vstart:
            raw = extract_frames(vpath, vstart, pt["sec"])
            for label, fr, real_sec in raw:
                if fr is not None:
                    # 标签带真实时间: b-1s 10:57:03
                    cap_label = f"{label} {sec_to_hms(int(real_sec))}"
                    frames.append({"label": cap_label, "b64": frame_to_b64(fr)})
                else:
                    frames.append({"label": label, "b64": None})
        # 清晰度 + 校验 (用 b1 帧)
        clarity = None
        dual = None
        dual_reason = ""
        if vpath and vstart:
            import cv2
            import numpy as np
            import base64 as b64mod
            b1_frame = None
            for fr in frames:
                if fr["label"].startswith("b0s") and fr["b64"]:
                    b1_frame = fr
            if b1_frame:
                raw = b64mod.b64decode(b1_frame["b64"])
                arr = np.frombuffer(raw, dtype=np.uint8)
                fr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if fr is not None:
                    clarity, _ = clarity_score(fr)
                    det = detect_lane_in_frame(fr, mode)
                    dual, dual_reason = dual_check(pt["labels"], det)
        out_points.append({
            "b": f"b{pi+1}",
            "time": sec_to_hms(pt["sec"]),
            "labels": pt["labels"],
            "frames": frames,
            "clarity": clarity,
            "dual_check": dual,
            "dual_reason": dual_reason,
        })
    return {
        "num": 0,
        "time": sec_to_hms(b_sec) if b_sec else sec_to_hms(points[0]["sec"]) if points else "",
        "category": "CSV规则检测",
        "scene": ["CSV", mode],
        "desc": f"CSV: {os.path.basename(csv_path)}",
        "points": out_points,
        # 数据状态标记: CSV✅/❌ 视频✅/❌ (用于问题卡片旁标注)
        "csv_ok": True,
        "video_ok": bool(vpath and vstart and any(fr["b64"] for pt2 in out_points for fr in pt2["frames"])),
        "video_name": os.path.basename(vpath) if vpath else "",
    }


def main():
    parser = argparse.ArgumentParser(description="车道线分析 → HTML 可视化报告 (完整引擎)")
    parser.add_argument("--data", help="数据根目录 (含xlsx/CSV/视频)")
    parser.add_argument("--video", help="视频目录 (默认=data/视频)")
    parser.add_argument("--csv", help="指定单个CSV")
    parser.add_argument("--xlsx", help="问题表xlsx路径 (默认=data/V1.1.6版本测试问题.xlsx)")
    parser.add_argument("--mode", default="full", choices=["full", "light"],
                        help="full=YOLOP完整版, light=Hough轻量版")
    parser.add_argument("--out", help="输出HTML路径")
    parser.add_argument("--no-open", action="store_true", help="不自动打开")
    args = parser.parse_args()

    # ===== 完整引擎模式 (reference_engine.run_engine) =====
    # 与昨天报告完全一致: 问题表 -> CSV检测 -> 视频抽帧 -> YOLOP校验 -> 报告
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    try:
        import reference_engine as engine
    except ImportError as e:
        print(f"❌ 无法加载引擎: {e}")
        print("   需要 reference_engine.py (完整分析引擎)")
        sys.exit(1)

    base_dir = args.data or os.getcwd()
    out_html = args.out
    if not out_html:
        out_html = os.path.join(os.getcwd(),
            f"车道线分析报告_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.html")

    # 自定义xlsx: 引擎用 CONFIG 固定路径, 支持覆盖
    if args.xlsx:
        # 复制/链接自定义xlsx到引擎期望位置 (或直接改CONFIG)
        engine.CONFIG["xlsx"] = os.path.basename(args.xlsx)
        engine.CONFIG["base_dir"] = base_dir
        engine.CONFIG["csv_dir"] = os.path.relpath(
            args.data or base_dir, base_dir) if args.data else "同类型CSV_lane_mark_camera_list_1"

    log("技能", f"调用完整引擎 base={base_dir}")
    html_path, total_points, total_frames = engine.run_engine(
        base_dir, out_html=out_html, max_points=25)
    log("完成", f"报告: {html_path} | 错误点 {total_points} | 截图 {total_frames}")

    if not args.no_open:
        try:
            if os.name == "nt":
                os.startfile(html_path)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", html_path])
        except Exception:
            pass
    return html_path


if __name__ == "__main__":
    main()
