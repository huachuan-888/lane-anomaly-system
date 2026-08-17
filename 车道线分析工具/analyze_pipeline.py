#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车道线异常智能归因 - 标准分析流水线 v1.1
用法:
    python analyze_pipeline.py --csv <CSV路径> [--ts <TS路径>] [--out <输出目录>] [--title <报告标题>]

功能:
    1. CSV规则检测 (缺失/抖动/宽度, 按PPT《车道线异常智能归因系统》)
    2. 视频帧清晰度评估 (5指标融合: Laplacian/边缘密度/对比度/Brenner/FFT)
    3. 左右车道线区分 (Hough变换检测线段中点 + 区域清晰度)
    4. 场景亮度统计 (中性描述: 明亮/中等/偏暗/暗光)
    5. 生成图表 (C0时序/宽度分布/出现率/左右清晰度对比/箱线图)
    6. 生成 可视化HTML报告(含计算公式章节) + Markdown报告

车道线公式 (三阶多项式拟合):
    Y = C0 + C1·X + 1/2·C2·X² + 1/6·C3·X³
    C0=横向偏移(m)  C1=航向角(rad)  C2=曲率(1/m)  C3=曲率变化率(1/m²)
"""
import argparse, csv, json, os, sys, base64, datetime
from collections import Counter

import numpy as np

# ---------- 路径处理 (OpenCV在Windows中文路径下cv2.imwrite会静默失败, 统一用imencode) ----------
def save_img(path, img, quality=95):
    import cv2
    ext = ".jpg" if path.lower().endswith((".jpg", ".jpeg")) else ".png"
    params = [cv2.IMWRITE_JPEG_QUALITY, quality] if ext == ".jpg" else [cv2.IMWRITE_PNG_COMPRESSION, 6]
    ok, buf = cv2.imencode(ext, img, params)
    if not ok:
        raise RuntimeError(f"imencode failed: {path}")
    with open(path, "wb") as f:
        f.write(buf.tobytes())

def cv2_import():
    import cv2
    return cv2


# ---------- Part 1: CSV 分析 ----------
TYPE_NAMES = {0: "未知", 1: "实线", 2: "虚线", 3: "双线", 4: "路沿", 5: "其他", 6: "减速标线"}
ID_LABELS = {1: "本车道左侧", -1: "本车道右侧", 2: "左二车道", -4: "右二车道", 4: "左三车道", -3: "右三车道", 3: "其他车道"}


def analyze_csv(csv_path):
    """返回 (result_dict, lane_series, width_series)"""
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    prefix = ""
    for h in reader.fieldnames or []:
        if "lane_marks(0)" in h:
            prefix = h[:h.index("lane_marks(0)")]
            break

    result = {
        "total_rows": len(rows),
        "time_range": [rows[0].get("timestamp", "N/A"), rows[-1].get("timestamp", "N/A")],
        "lanes": {}, "missing": [], "jitter": [], "width": [], "range_drop": [], "own_lane_events": [], "width_warn": [], "width_downgrade": [],
        "prefix": prefix, "csv_name": os.path.basename(csv_path),
    }

    lane_series = {"t": [], "left": [], "right": []}   # C0 采样
    width_series = {"bins": [0] * 40, "count": 0, "sum": 0}  # 宽度直方图 2.5~6.5m

    prev_c0, jitter_buf = {}, []
    prev_vr, short_vr_buf = {}, {}

    for i, row in enumerate(rows):
        ts = row.get("timestamp", "")
        found_ids = []
        c0l = c0r = None

        for li in range(8):
            id_key = f"{prefix}lane_marks({li}).id"
            if id_key not in row:
                continue
            try:
                lid = int(float(row[id_key]))
            except (ValueError, TypeError):
                continue
            if lid == 99:
                continue
            found_ids.append(lid)
            c0 = float(row.get(f"{prefix}lane_marks({li}).lane_curvature0", 0) or 0)
            q = int(float(row.get(f"{prefix}lane_marks({li}).quality", 0) or 0))
            vr = float(row.get(f"{prefix}lane_marks({li}).view_range_end", 0) or 0)
            lt = int(float(row.get(f"{prefix}lane_marks({li}).lane_mark_type", 0) or 0))

            if lid not in result["lanes"]:
                result["lanes"][lid] = {"id": lid, "count": 0, "c0s": [], "qs": [], "vrs": [], "types": Counter()}
            l = result["lanes"][lid]
            l["count"] += 1
            l["c0s"].append(c0)
            l["qs"].append(q)
            l["vrs"].append(vr)
            l["types"][lt] += 1

            if lid in (1, -1):  # 抖动检测只看本车道
                k = f"l{lid}"
                if k in prev_c0 and abs(c0 - prev_c0[k]) >= 0.2:
                    jitter_buf.append({"ts": ts, "lid": lid, "c0": c0, "prev": prev_c0[k]})
                else:
                    if len(jitter_buf) >= 3:
                        result["jitter"].append({
                            "start": jitter_buf[0]["ts"], "end": jitter_buf[-1]["ts"],
                            "lane": jitter_buf[0]["lid"], "frames": len(jitter_buf)})
                    jitter_buf = []
                prev_c0[k] = c0

            if lid == 1:
                c0l = abs(c0)
            elif lid == -1:
                c0r = abs(c0)

        # 规则一: 缺失 (本车道线丢失 = id=1或-1不在检测列表)
        if 1 not in found_ids:
            result["missing"].append({"ts": ts, "lane": "左侧(id=1)"})
            result["own_lane_events"].append({"ts": ts, "side": "左", "event": "丢失", "detail": "id=1不在检测列表"})
        if -1 not in found_ids:
            result["missing"].append({"ts": ts, "lane": "右侧(id=-1)"})
            result["own_lane_events"].append({"ts": ts, "side": "右", "event": "丢失", "detail": "id=-1不在检测列表"})

        # 规则三: 宽度 (双阈值: 预警>=4.0m / 降级>=4.1m持续5帧)
        if c0l is not None and c0r is not None:
            w = c0l + c0r
            if w <= 3.4:
                result["width"].append({"ts": ts, "type": "过窄", "value": w})
            elif w >= 4.0:
                result["width"].append({"ts": ts, "type": "过宽", "value": w})
                if w >= 4.1:
                    result["width_warn"].append({"ts": ts, "value": w, "c0l": c0l, "c0r": c0r})
                    # 降级级: 连续5帧>=4.1m
                    if "w41_buf" not in result:
                        result["w41_buf"] = []
                    result["w41_buf"].append(ts)
                    if len(result["w41_buf"]) >= 5:
                        result["width_downgrade"].append({
                            "start": result["w41_buf"][0], "end": ts,
                            "max_value": max(x["value"] for x in result["width_warn"][-5:]),
                            "frames": len(result["w41_buf"])})
                        result["w41_buf"] = []
                else:
                    result["w41_buf"] = []
            width_series["bins"][min(39, max(0, int(w * 10)))] += 1
            width_series["count"] += 1
            width_series["sum"] += w

        # 规则四: 可视范围骤降 (本车道左右线 view_range_end)
        # 骤降: 前帧>=60m 且当前<30m; 持续短: 连续N帧(>=10帧=1s) <30m
        for li in range(8):
            id_key = f"{prefix}lane_marks({li}).id"
            vr_key = f"{prefix}lane_marks({li}).view_range_end"
            if id_key not in row:
                continue
            try:
                lid4 = int(float(row[id_key]))
            except (ValueError, TypeError):
                continue
            if lid4 not in (1, -1):
                continue
            vr4 = float(row.get(vr_key, 0) or 0)
            k4 = f"vr{lid4}"
            if k4 in prev_vr:
                prev_vr_val = prev_vr[k4]
                if prev_vr_val >= 60 and vr4 < 30:
                    result["range_drop"].append({
                        "ts": ts, "lane": "左侧(id=1)" if lid4 == 1 else "右侧(id=-1)",
                        "type": "骤降", "from": prev_vr_val, "to": vr4})
                    result["own_lane_events"].append({
                        "ts": ts, "side": "左" if lid4 == 1 else "右", "event": "前兆",
                        "detail": f"可视范围骤降 {prev_vr_val:.0f}m→{vr4:.0f}m"})
                if vr4 < 30:
                    if k4 not in short_vr_buf:
                        short_vr_buf[k4] = []
                    short_vr_buf[k4].append(ts)
                    if len(short_vr_buf[k4]) >= 10:  # 10帧≈1秒
                        result["range_drop"].append({
                            "ts": short_vr_buf[k4][0], "lane": "左侧(id=1)" if lid4 == 1 else "右侧(id=-1)",
                            "type": "持续短", "from": 0, "to": vr4})
                        result["own_lane_events"].append({
                            "ts": short_vr_buf[k4][0], "side": "左" if lid4 == 1 else "右", "event": "前兆",
                            "detail": f"可视范围持续<30m ({vr4:.0f}m)"})
                        short_vr_buf[k4] = []
                else:
                    short_vr_buf[k4] = []
            if vr4 == 0:
                result["own_lane_events"].append({
                    "ts": ts, "side": "左" if lid4 == 1 else "右", "event": "无效",
                    "detail": "可视范围=0m"})
            prev_vr[k4] = vr4

        # C0 采样 (每10行)
        if i % 10 == 0:
            lane_series["t"].append(ts)
            cl = cr = None
            for li in range(8):
                id_key = f"{prefix}lane_marks({li}).id"
                if id_key not in row:
                    continue
                try:
                    lid = int(float(row[id_key]))
                except (ValueError, TypeError):
                    continue
                if lid == 99:
                    continue
                c0 = float(row.get(f"{prefix}lane_marks({li}).lane_curvature0", 0) or 0)
                if lid == 1:
                    cl = c0
                elif lid == -1:
                    cr = c0
            lane_series["left"].append(cl)
            lane_series["right"].append(cr)

    # 抖动缓冲收尾
    if len(jitter_buf) >= 3:
        result["jitter"].append({"start": jitter_buf[0]["ts"], "end": jitter_buf[-1]["ts"],
                                 "lane": jitter_buf[0]["lid"], "frames": len(jitter_buf)})

    # 统计补充
    for lid, l in result["lanes"].items():
        l["avg_c0"] = float(np.mean(l["c0s"])) if l["c0s"] else 0
        l["avg_q"] = float(np.mean(l["qs"])) if l["qs"] else 0
        l["max_vr"] = float(np.max(l["vrs"])) if l["vrs"] else 0
        l["pct"] = l["count"] / result["total_rows"] * 100
        l["types"] = {k: v for k, v in l["types"].items()}

    result["own_lane_events"] = sorted(result["own_lane_events"], key=lambda e: e["ts"])
    result.pop("w41_buf", None)
    return result, lane_series, width_series


# ---------- Part 2: 视频分析 ----------
def region_clarity(gray, cx, cy, margin=35):
    """在车道线周围区域计算5指标清晰度"""
    h, w = gray.shape
    x1, x2 = max(0, cx - margin), min(w - 1, cx + margin)
    y1, y2 = max(0, cy - margin), min(h - 1, cy + margin)
    r = gray[y1:y2 + 1, x1:x2 + 1]
    if r.size < 100:
        return None
    cv2 = cv2_import()
    lap = cv2.Laplacian(r, cv2.CV_64F)
    L = float(np.var(lap))
    edges = cv2.Canny(r, 50, 150)
    E = float(np.sum(edges > 0) / r.size * 1000)
    C = float(np.std(r))
    grad = np.diff(r.astype(np.float32), axis=1)
    B = float(np.mean(grad ** 2))
    fft = np.fft.fft2(r)
    mag = np.abs(np.fft.fftshift(fft))
    te = float(np.sum(mag))
    rh, rw = r.shape
    if te > 0:
        cy2, cx2 = rh // 2, rw // 2
        mask = np.ones((rh, rw), dtype=bool)
        for yy in range(rh):
            for xx in range(rw):
                if (yy - cy2) ** 2 + (xx - cx2) ** 2 <= (min(rh, rw) * 0.25) ** 2:
                    mask[yy, xx] = False
        F = float(np.sum(mag[mask]) / te * 1000)
    else:
        F = 0.0
    S = 0.25 * (L / 100) + 0.20 * E + 0.20 * C + 0.20 * B + 0.15 * F
    return {"L": L, "E": E, "C": C, "B": B, "F": F, "S": S}


def detect_lane_positions(frame):
    """Hough变换检测左右车道线, 返回最佳线段的端点(全图坐标)和x位置

    返回: (left_x, right_x, left_ok, right_ok, roi_y1, left_seg, right_seg)
      left_seg/right_seg = (x1, y1, x2, y2) 全图坐标线段, 用于在帧上精确画线
    """
    cv2 = cv2_import()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi_y1 = int(h * 0.55)
    roi = gray[roi_y1:h, int(w * 0.1):int(w * 0.9)]
    edges = cv2.Canny(roi, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50, minLineLength=40, maxLineGap=50)
    left_x, right_x = -1, -1
    left_ok = right_ok = False
    left_seg = right_seg = None
    if lines is not None:
        roi_h, roi_w = roi.shape
        lc, rc = [], []
        # HoughLinesP 返回形状可能是 (N,1,4) 或 (N,4), 统一处理
        line_arrs = lines[:, 0] if lines.ndim == 3 else lines
        for line in line_arrs:
            x1, y1, x2, y2 = int(line[0]), int(line[1]), int(line[2]), int(line[3])
            if x2 == x1:
                continue
            slope = (y2 - y1) / (x2 - x1)
            length = float(np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2))
            if length < 30:
                continue
            # 全图坐标线段 (ROI偏移 + 原图偏移)
            gx1, gy1 = x1 + int(w * 0.1), y1 + roi_y1
            gx2, gy2 = x2 + int(w * 0.1), y2 + roi_y1
            # 用线段中点cx而不是起点x1: 起点随线段倾斜漂移, 中点更稳定
            cx = (gx1 + gx2) / 2
            if abs(slope) > 0.2:
                if slope < -0.15 and cx < roi_w * 0.55 + int(w * 0.1):
                    lc.append((cx, length, gx1, gy1, gx2, gy2))
                elif slope > 0.15 and cx > roi_w * 0.45 + int(w * 0.1):
                    rc.append((cx, length, gx1, gy1, gx2, gy2))
        if lc:
            best = max(lc, key=lambda l: l[1])
            left_x = int(best[0])
            left_seg = (best[2], best[3], best[4], best[5])
            left_ok = True
        if rc:
            best = max(rc, key=lambda l: l[1])
            right_x = int(best[0])
            right_seg = (best[2], best[3], best[4], best[5])
            right_ok = True
    if left_x < 0:
        left_x = int(w * 0.30)
    if right_x < 0:
        right_x = int(w * 0.70)
    return left_x, right_x, left_ok, right_ok, roi_y1, left_seg, right_seg


def scene_brightness_estimate(frame):
    """基础场景感知: 亮度统计 (只做中性描述, 不强行贴场景标签)

    注意: 视频编码/摄像头配置会影响绝对亮度, 纯阈值法容易误判
    (如白天视频编码亮度偏低会被误判为"隧道/傍晚")。因此这里只报告
    亮度统计值 + 中性亮度描述, 不做确定性场景结论, 避免与"正常
    行驶"等结论自相矛盾。
    """
    cv2 = cv2_import()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    mean_b = float(np.mean(gray))
    std_b = float(np.std(gray))
    if mean_b > 100:
        scene = "明亮"
    elif mean_b > 70:
        scene = "中等亮度"
    elif mean_b > 45:
        scene = "偏暗"
    else:
        scene = "暗光"
    return scene, mean_b, std_b


def analyze_video(ts_path, sample_count=17, out_dir=None):
    """提取帧, Hough检测车道线, 计算左右清晰度, 生成标注帧"""
    cv2 = cv2_import()
    cap = cv2.VideoCapture(ts_path)
    if not cap.isOpened():
        return None, "无法打开视频文件"

    frames = []
    for i in range(100):
        ret, f = cap.read()
        if not ret:
            break
        if i % 3 == 0 and len(frames) < sample_count:
            frames.append(f)
    fps = cap.get(cv2.CAP_PROP_FPS)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    if not frames:
        return None, "未能解码任何视频帧（可能是私有封装格式）"

    results = []
    annotated_frames = []
    scenes = []
    for idx, frame in enumerate(frames):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        # 左右车道线清晰度: 用Hough检测位置(线段中点, 稳定)。检测失败时
        # 回退到固定区域 (左0.28/右0.72)。已验证: 中点法在真实数据上
        # 得到稳定的左右对比 (差异~0, 正常场景)。
        det_left_x, det_right_x, det_left_ok, det_right_ok, roi_y1, left_seg, right_seg = detect_lane_positions(frame)
        left_x = det_left_x if det_left_ok else int(w * 0.28)
        right_x = det_right_x if det_right_ok else int(w * 0.72)
        left_ok = det_left_ok
        right_ok = det_right_ok
        cy = int(h * 0.72)
        left_r = region_clarity(gray, left_x, cy)
        right_r = region_clarity(gray, right_x, cy)
        scene, mb, sb = scene_brightness_estimate(frame)
        scenes.append(scene)

        # 整帧清晰度 (PPT公式, 作用于全图)
        lap = cv2.Laplacian(gray, cv2.CV_64F)
        L_all = float(np.var(lap))
        edges_all = cv2.Canny(gray, 50, 150)
        E_all = float(np.sum(edges_all > 0) / gray.size * 1000)
        C_all = float(np.std(gray))
        grad_all = np.diff(gray.astype(np.float32), axis=1)
        B_all = float(np.mean(grad_all ** 2))
        fft_all = np.fft.fft2(gray)
        mag_all = np.abs(np.fft.fftshift(fft_all))
        te_all = float(np.sum(mag_all))
        gh, gw = gray.shape
        if te_all > 0:
            mask_all = np.ones((gh, gw), dtype=bool)
            cyg, cxg = gh // 2, gw // 2
            for yy in range(gh):
                for xx in range(gw):
                    if (yy - cyg) ** 2 + (xx - cxg) ** 2 <= (min(gh, gw) * 0.25) ** 2:
                        mask_all[yy, xx] = False
            F_all = float(np.sum(mag_all[mask_all]) / te_all * 1000)
        else:
            F_all = 0.0
        S_all = 0.25 * (L_all / 100) + 0.20 * E_all + 0.20 * C_all + 0.20 * B_all + 0.15 * F_all

        # 标注帧: 把Hough检测到的真实车道线线段精确画在原图上。
        # 线段端点来自detect_lane_positions返回的left_seg/right_seg (全图坐标),
        # 确保绿/红线贴合画面中实际车道线位置, 而不是示意线。
        vis = frame.copy()
        if left_seg is not None:
            x1, y1, x2, y2 = left_seg
            cv2.line(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 4)
            cv2.putText(vis, "LEFT", (int(x1) - 80, int(y1) + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
        if right_seg is not None:
            x1, y1, x2, y2 = right_seg
            cv2.line(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 4)
            cv2.putText(vis, "RIGHT", (int(x1) + 20, int(y1) + 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.putText(vis, f"#{idx+1} {scene}", (15, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
        annotated_frames.append(vis)

        results.append({
            "frame": idx + 1,
            "overall": {"L": L_all, "E": E_all, "C": C_all, "B": B_all, "F": F_all, "S": S_all},
            "left": left_r, "right": right_r,
            "left_detected": left_ok, "right_detected": right_ok,
            "scene": scene, "mean_brightness": mb,
            "left_x": left_x, "right_x": right_x,
        })

    # 保存标注帧 (拼接网格)
    if out_dir:
        # 样例单帧
        save_img(os.path.join(out_dir, "lane_detection_sample.jpg"), annotated_frames[0], 95)
        # 网格图
        for rs in range(0, len(annotated_frames), 6):
            batch = annotated_frames[rs:rs + 6]
            if len(batch) < 3:
                break
            cols = 3
            rows_n = (len(batch) + cols - 1) // cols
            fh, fw = batch[0].shape[:2]
            grid = np.zeros((fh * rows_n, fw * cols, 3), dtype=np.uint8)
            for i, fr in enumerate(batch):
                grid[(i // cols) * fh:(i // cols + 1) * fh, (i % cols) * fw:(i % cols + 1) * fw] = fr
            save_img(os.path.join(out_dir, f"lanes_grid_{rs // 6 + 1}.jpg"), grid, 90)

    # 汇总
    summary = {
        "frames": len(results),
        "video_name": os.path.basename(ts_path),
        "fps": float(fps) if fps and fps > 0 else 25.0,
        "resolution": f"{w}x{h}",
        "overall_avg": float(np.mean([r["overall"]["S"] for r in results])),
        "left_avg": float(np.mean([r["left"]["S"] for r in results if r["left"]])),
        "right_avg": float(np.mean([r["right"]["S"] for r in results if r["right"]])),
        "left_detected": sum(1 for r in results if r["left_detected"]),
        "right_detected": sum(1 for r in results if r["right_detected"]),
        "scene_counter": dict(Counter(scenes)),
        "results": results,
    }
    return summary, None


# ---------- Part 3: 图表 ----------
def make_charts(result, lane_series, width_series, video_summary, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial Unicode MS"]
    plt.rcParams["axes.unicode_minus"] = False

    # 图1: C0时序
    if lane_series["left"]:
        fig, ax = plt.subplots(figsize=(12, 4.2))
        x = np.arange(len(lane_series["t"]))
        ax.plot(x, lane_series["left"], color="#22c55e", lw=1.2, label="左侧(Lane 1)")
        ax.plot(x, lane_series["right"], color="#ef4444", lw=1.2, label="右侧(Lane -1)")
        ax.axhline(0, color="gray", lw=0.5, ls="--")
        ax.set_title("本车道C0(横向偏移)时序", fontsize=13)
        ax.set_xlabel("时间")
        ax.set_ylabel("C0 (m)")
        step = max(1, len(x) // 8)
        ax.set_xticks(x[::step])
        ax.set_xticklabels([t[11:19] for t in lane_series["t"][::step]], rotation=30, fontsize=8)
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "chart_c0.png"), dpi=140)
        plt.close(fig)

    # 图2: 宽度分布
    if width_series["count"]:
        fig, axes = plt.subplots(1, 2, figsize=(12, 3.8))
        start_idx, end_idx = 25, 65
        bins = width_series["bins"][start_idx:end_idx]
        x_pos = np.arange(start_idx / 10 + 0.05, end_idx / 10, 0.1)
        x_pos = x_pos[:len(bins)]  # 确保与bins长度一致
        colors = ["#ef4444" if (v <= 3.4 or v >= 4.0) else "#38bdf8" for v in x_pos]
        axes[0].bar(x_pos, bins, width=0.09, color=colors, edgecolor="white")
        axes[0].axvline(3.4, color="#ef4444", ls="--", lw=1)
        axes[0].axvline(4.0, color="#ef4444", ls="--", lw=1)
        axes[0].set_title(f"车道宽度分布 (均值 {width_series['sum']/width_series['count']:.3f}m)", fontsize=12)
        axes[0].set_xlabel("宽度 (m)")
        axes[0].set_ylabel("频次")
        axes[0].grid(alpha=0.3)
        widths = []
        # 重建宽度数组用于箱线图
        for i, c in enumerate(width_series["bins"]):
            widths.extend([i / 10] * c)
        if widths:
            axes[1].boxplot(widths)
            axes[1].set_title(f"宽度统计 std={np.std(widths):.4f}", fontsize=12)
            axes[1].set_ylabel("宽度 (m)")
            axes[1].grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "chart_width.png"), dpi=140)
        plt.close(fig)

    # 图3: 车道线出现率
    if result["lanes"]:
        ids = sorted(result["lanes"].keys())
        labels = [f"Lane {lid}\n{ID_LABELS.get(lid, '')}" for lid in ids]
        pcts = [result["lanes"][lid]["pct"] for lid in ids]
        colors = ["#22c55e" if lid in (1, -1) else "#38bdf8" for lid in ids]
        fig, ax = plt.subplots(figsize=(10, 3.8))
        bars = ax.bar(labels, pcts, color=colors, edgecolor="white")
        for bar, p in zip(bars, pcts):
            ax.text(bar.get_x() + bar.get_width() / 2, p + 1, f"{p:.1f}%", ha="center", fontsize=9)
        ax.set_title("各车道线出现率", fontsize=13)
        ax.set_ylabel("出现率 (%)")
        ax.set_ylim(0, 115)
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "chart_lanes.png"), dpi=140)
        plt.close(fig)

    # 图4: 左右清晰度对比
    if video_summary:
        fig, ax = plt.subplots(figsize=(12, 4.0))
        x = np.arange(1, len(video_summary["results"]) + 1)
        left_s = [r["left"]["S"] if r["left"] else None for r in video_summary["results"]]
        right_s = [r["right"]["S"] if r["right"] else None for r in video_summary["results"]]
        ax.plot(x, left_s, color="#22c55e", marker="o", ms=3, lw=1.2, label="左侧车道线")
        ax.plot(x, right_s, color="#ef4444", marker="s", ms=3, lw=1.2, label="右侧车道线")
        ax.set_title("左右车道线清晰度对比 (5指标融合)", fontsize=13)
        ax.set_xlabel("帧序号")
        ax.set_ylabel("清晰度评分 S")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "chart_clarity.png"), dpi=140)
        plt.close(fig)

        # 图5: 箱线图
        fig, ax = plt.subplots(figsize=(6, 3.8))
        ls = [r["left"]["S"] for r in video_summary["results"] if r["left"]]
        rs = [r["right"]["S"] for r in video_summary["results"] if r["right"]]
        bp = ax.boxplot([ls, rs], tick_labels=["左侧", "右侧"], patch_artist=True)
        bp["boxes"][0].set_facecolor("#22c55e")
        bp["boxes"][1].set_facecolor("#ef4444")
        ax.set_title(f"左右清晰度分布 (差异 {np.mean(ls)-np.mean(rs):.2f})", fontsize=12)
        ax.set_ylabel("清晰度 S")
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "chart_box.png"), dpi=140)
        plt.close(fig)


# ---------- Part 4: 报告生成 ----------
def b64(path):
    with open(path, "rb") as f:
        ext = os.path.splitext(path)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()



def _own_lane_rows_html(result, limit=50):
    evs = result.get("own_lane_events", [])
    if not evs:
        return '<tr><td colspan="5" style="text-align:center;color:#22c55e">✅ 本车道线全程正常，无丢失/无效/前兆事件</td></tr>'
    rows = ""
    colors = {"丢失": "#ef4444", "无效": "#eab308", "前兆": "#38bdf8"}
    for k, ev in enumerate(evs[:limit], 1):
        c = colors.get(ev["event"], "#e2e8f0")
        rows += f'<tr><td>{k}</td><td>{ev["ts"]}</td><td>{ev["side"]}侧</td><td style="color:{c};font-weight:600">{ev["event"]}</td><td>{ev["detail"]}</td></tr>\n'
    return rows

def generate_html(result, lane_series, width_series, video_summary, out_dir, title):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    has_anomaly = len(result["missing"]) + len(result["jitter"]) + len(result["width"]) + len(result.get("range_drop", [])) > 0

    result["own_lane_rows_html"] = _own_lane_rows_html(result)
    imgs = {}
    for name in ["chart_c0", "chart_width", "chart_lanes", "chart_clarity", "chart_box", "lane_detection_sample", "lanes_grid_1", "lanes_grid_2"]:
        p = os.path.join(out_dir, name + (".png" if name.startswith("chart") else ".jpg"))
        if os.path.exists(p):
            imgs[name] = b64(p)

    # 场景亮度汇总
    scene_str = ""
    if video_summary and video_summary["scene_counter"]:
        scene_str = " / ".join(f"{k}×{v}" for k, v in video_summary["scene_counter"].items())

    # 车道线表格
    lane_rows = ""
    for lid in sorted(result["lanes"].keys()):
        l = result["lanes"][lid]
        types = ", ".join(f"{TYPE_NAMES.get(int(k), k)}×{v}" for k, v in sorted(l["types"].items(), key=lambda x: -x[1]))
        lane_rows += f"<tr><td>{lid}</td><td>{ID_LABELS.get(lid, '')}</td><td>{l['count']}</td><td>{l['pct']:.1f}%</td><td>{l['avg_c0']:.4f}</td><td>{l['avg_q']:.1f}/3.0</td><td>0~{l['max_vr']:.0f}m</td><td>{types}</td></tr>\n"

    # 清晰度表格
    clarity_rows = ""
    if video_summary:
        for r in video_summary["results"]:
            ls = r["left"]["S"] if r["left"] else 0
            rs = r["right"]["S"] if r["right"] else 0
            clarity_rows += f"<tr><td>{r['frame']}</td><td>{r['scene']}</td><td><b>{ls:.2f}</b></td><td>{rs:.2f}</td><td>{ls-rs:+.2f}</td></tr>\n"

    img_c0 = imgs.get("chart_c0", "")
    img_w = imgs.get("chart_width", "")
    img_l = imgs.get("chart_lanes", "")
    img_c = imgs.get("chart_clarity", "")
    img_b = imgs.get("chart_box", "")
    img_s = imgs.get("lane_detection_sample", "")
    img_g1 = imgs.get("lanes_grid_1", "")
    img_g2 = imgs.get("lanes_grid_2", "")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>{title}</title>
<style>
body{{font-family:'Microsoft YaHei',sans-serif;max-width:1080px;margin:0 auto;padding:28px;background:#0f172a;color:#e2e8f0;line-height:1.7}}
h1{{color:#38bdf8;font-size:26px}}h2{{color:#38bdf8;font-size:19px;border-bottom:2px solid #334155;padding-bottom:6px;margin-top:30px}}
h3{{color:#94a3b8;font-size:15px;margin:14px 0 6px}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}}
th,td{{padding:7px 10px;border-bottom:1px solid #334155;text-align:left}}
th{{background:#172554;color:#38bdf8}}
img{{max-width:100%;border-radius:8px;margin:6px 0;border:1px solid #334155}}
.ok{{color:#22c55e;font-weight:600}}.bad{{color:#ef4444;font-weight:600}}
.meta{{color:#94a3b8;font-size:13px;margin-bottom:16px}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}}
.stat{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:10px 18px;text-align:center}}
.stat .n{{font-size:22px;font-weight:700;color:#38bdf8}}.stat .l{{font-size:12px;color:#94a3b8}}
.concl{{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);border-radius:10px;padding:16px;margin:12px 0}}
.warn{{background:rgba(234,179,8,.1);border:1px solid rgba(234,179,8,.3);border-radius:10px;padding:16px;margin:12px 0}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
code{{background:#0b1220;padding:1px 6px;border-radius:4px;font-size:12px}}
</style></head><body>
<h1>🚗 {title}</h1>
<div class="meta">分析时间: {now} | 车道线异常智能归因系统 v1.1 | 双维度分析</div>

<div class="stats">
<div class="stat"><div class="n">{result['total_rows']:,}</div><div class="l">CSV数据行</div></div>
<div class="stat"><div class="n" style="color:{'#ef4444' if has_anomaly else '#22c55e'}">{len(result['missing'])+len(result['jitter'])+len(result['width'])}</div><div class="l">规则异常</div></div>
<div class="stat"><div class="n">{len(result['lanes'])}</div><div class="l">检测车道线</div></div>
"""
    if video_summary:
        html += f"""<div class="stat"><div class="n">{video_summary['overall_avg']:.1f}</div><div class="l">整体清晰度</div></div>
<div class="stat"><div class="n">{video_summary['left_avg']-video_summary['right_avg']:+.2f}</div><div class="l">左右差异</div></div>"""
    html += f"""</div>

<h2>一、数据概况</h2>
<table><tr><th>项目</th><th>内容</th></tr>
<tr><td>CSV文件</td><td>{result['csv_name']}</td></tr>
<tr><td>数据行数</td><td>{result['total_rows']:,}</td></tr>
<tr><td>时间范围</td><td>{result['time_range'][0]} ~ {result['time_range'][1]}</td></tr>"""
    if video_summary:
        html += f"""<tr><td>视频文件</td><td>{video_summary['video_name']} ({video_summary['resolution']}, {video_summary['fps']:.0f}fps)</td></tr>
<tr><td>场景亮度</td><td>{scene_str or '未分析'}</td></tr>"""
    html += f"""</table>

<h2>二、车道线计算公式与字段含义</h2>
<div style="background:#172554;border-radius:10px;padding:16px;margin:10px 0">
<h3 style="margin-bottom:8px">车道线曲线方程（三阶多项式拟合）</h3>
<div style="font-size:18px;font-family:'Cambria Math',serif;text-align:center;padding:12px;background:#0b1220;border-radius:8px;color:#7dd3fc">
Y = C0 + C1·X + ½·C2·X² + (1/6)·C3·X³
</div>
</div>
<table><tr><th>参数</th><th>物理含义</th><th>CSV字段名</th><th>单位</th></tr>
<tr><td><b>C0</b></td><td>近端Y轴坐标（车道线横向偏移）</td><td><code>lane_curvature0</code></td><td>m</td></tr>
<tr><td><b>C1</b></td><td>切线夹角 θ（航向角）</td><td><code>lane_curvature1</code></td><td>rad</td></tr>
<tr><td><b>C2</b></td><td>曲率</td><td><code>lane_curvature2</code></td><td>1/m</td></tr>
<tr><td><b>C3</b></td><td>曲率变化率</td><td><code>lane_curvature3</code></td><td>1/m²</td></tr>
</table>
<div style="font-size:12px;color:#94a3b8;margin-top:8px">
注：Y为距离参考点的横向距离，X为距离参考点的纵向距离。<br>
本报告按PPT规则使用 <b>C0（横向偏移）</b>进行抖动检测（|ΔC0|≥0.2m）和车道宽度估算（|C0左|+|C0右|）。
</div>

<h2>三、CSV规则检测</h2>
<table><tr><th>规则</th><th>检测条件</th><th>结果</th></tr>
<tr><td><b>规则一</b> 车道线缺失</td><td>id=1或id=-1缺失</td><td class="{'ok' if not result['missing'] else 'bad'}">{'✅ 0次异常' if not result['missing'] else '❌ '+str(len(result['missing']))+'次'}</td></tr>
<tr><td><b>规则二</b> 异常抖动</td><td>|C0(t)-C0(t-1)|≥0.2连续3帧</td><td class="{'ok' if not result['jitter'] else 'bad'}">{'✅ 0次异常' if not result['jitter'] else '❌ '+str(len(result['jitter']))+'次'}</td></tr>
<tr><td><b>规则三</b> 宽度异常</td><td>预警≥4.0m / 降级≥4.1m持续5帧 / 过窄≤3.4</td><td class="{'ok' if not result['width'] else 'bad'}">{'✅ 0次异常' if not result['width'] else '❌ '+str(len(result['width']))+'次'}</td></tr>
<tr><td><b>规则四</b> 可视范围骤降</td><td>本车道线view_range从≥60m骤降至<30m 或持续<30m超1s</td><td class="{'ok' if not result.get('range_drop', []) else 'bad'}">{'✅ 0次异常' if not result.get('range_drop', []) else '❌ '+str(len(result.get('range_drop', [])))+'次'}</td></tr>
</table>
"""
    if has_anomaly:
        html += "<h3>异常事件详情</h3><table><tr><th>类型</th><th>时间</th><th>详情</th></tr>"
        for m in result["missing"][:10]:
            html += f"<tr><td>缺失</td><td>{m['ts']}</td><td>{m['lane']}</td></tr>"
        for j in result["jitter"][:10]:
            html += f"<tr><td>抖动</td><td>{j['start']}~{j['end']}</td><td>车道{j['lane']} 持续{j['frames']}帧</td></tr>"
        for w in result["width"][:10]:
            html += f"<tr><td>宽度</td><td>{w['ts']}</td><td>{w['type']} ({w['value']:.3f}m)</td></tr>"
        for wd in result.get("width_downgrade", [])[:10]:
            html += f"<tr><td>宽度降级</td><td>{wd['start']}~{wd['end']}</td><td>持续{wd['frames']}帧, 最宽{wd['max_value']:.3f}m</td></tr>"
        for rd in result.get("range_drop", [])[:10]:
            html += f"<tr><td>可视范围</td><td>{rd['ts']}</td><td>{rd['lane']} {rd['type']} ({rd['from']:.0f}m→{rd['to']:.0f}m)</td></tr>"
        html += "</table>"

    html += f"""
<h2>四、本车道线丢失事件时间线</h2>
<div style="font-size:12px;color:#94a3b8;margin-bottom:8px">仅关注本车道线 (id=1左侧 / id=-1右侧): 丢失 / 无效(可视范围0m) / 前兆(可视范围骤降或持续<30m)</div>
<table><tr><th>#</th><th>时间</th><th>侧</th><th>事件</th><th>详情</th></tr>
{result["own_lane_rows_html"]}
</table>
<h2>四、车道线详细信息</h2>
<table><tr><th>车道ID</th><th>含义</th><th>次数</th><th>占比</th><th>平均C0</th><th>质量</th><th>可视范围</th><th>类型</th></tr>
{lane_rows}</table>

<h2>五、图表分析</h2>
<h3>C0(横向偏移)时序</h3><img src="{img_c0}">
<h3>车道宽度分布</h3><img src="{img_w}">
<h3>各车道线出现率</h3><img src="{img_l}">
"""
    if video_summary:
        html += f"""
<h2>六、视频清晰度评估</h2>
<h3>车道线检测标注（Hough变换，绿=左线 红=右线，线段贴合实际位置）</h3>
<img src="{img_s}">
<div class="grid2"><img src="{img_g1}"><img src="{img_g2}"></div>
<h3>左右车道线清晰度对比</h3>
<img src="{img_c}"><img src="{img_b}">
<table><tr><th>统计项</th><th>⬅左侧</th><th>➡右侧</th></tr>
<tr><td>平均清晰度</td><td class="ok">{video_summary['left_avg']:.2f}</td><td class="ok">{video_summary['right_avg']:.2f}</td></tr>
<tr><td>左右差异</td><td colspan="2">{video_summary['left_avg']-video_summary['right_avg']:+.2f}</td></tr>
<tr><td>Hough检测率</td><td>{video_summary['left_detected']}/{video_summary['frames']}</td><td>{video_summary['right_detected']}/{video_summary['frames']}</td></tr></table>
<h3>各帧清晰度</h3>
<table><tr><th>帧</th><th>亮度</th><th>⬅左侧S</th><th>➡右侧S</th><th>差异</th></tr>{clarity_rows}</table>
"""
    html += f"""
<h2>七、综合结论</h2>
<div class="{'warn' if has_anomaly else 'concl'}">
{'⚠️ <b>发现异常情况</b>，建议进一步分析排查。' if has_anomaly else '✅ <b>数据正常，未发现异常。</b>该组数据可作为基准测试（Baseline）使用。'}
"""
    if video_summary:
        diff = video_summary['left_avg'] - video_summary['right_avg']
        abs_diff = abs(diff)
        # 差异阈值: <5 正常(区域法本身有±3波动), 5-10 需关注, >10 明显偏侧
        if abs_diff < 5:
            diff_note = '左右车道线清晰度接近，与真实场景相符（正常行驶、光照均匀）。'
        elif abs_diff < 10:
            diff_note = '左右车道线清晰度存在一定差异，建议结合场景确认（可能是光照、阴影或摄像头标定因素）。'
        else:
            diff_note = '左右车道线清晰度差异明显，建议检查摄像头安装/标定或单侧光照问题。'
        html += f"<br>整体清晰度 <b>{video_summary['overall_avg']:.1f}</b>（{'非常清晰' if video_summary['overall_avg'] >= 80 else '清晰'}），左右车道线差异 <b>{diff:+.2f}</b>（⬅左 {video_summary['left_avg']:.1f} / ➡右 {video_summary['right_avg']:.1f}），{diff_note}"
        html += f"<br>场景亮度：{scene_str or '未分析'}"
    html += f"""
</div>
<div style="color:#64748b;font-size:12px;margin-top:24px;text-align:center">报告由车道线异常智能归因系统自动生成 | Python + OpenCV + Matplotlib</div>
</body></html>"""
    return html


def generate_md(result, lane_series, width_series, video_summary, title):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    has_anomaly = len(result["missing"]) + len(result["jitter"]) + len(result["width"]) + len(result.get("range_drop", [])) > 0
    md = f"# 🚗 {title}\n\n"
    md += f"| 项目 | 内容 |\n|------|------|\n"
    md += f"| 分析时间 | {now} |\n"
    md += f"| CSV文件 | {result['csv_name']} |\n"
    md += f"| 数据行数 | {result['total_rows']:,} |\n"
    md += f"| 时间范围 | {result['time_range'][0]} ~ {result['time_range'][1]} |\n"
    if video_summary:
        md += f"| 视频文件 | {video_summary['video_name']} |\n"
    md += "\n## 车道线计算公式\n\n"
    md += "`Y = C0 + C1·X + ½·C2·X² + (1/6)·C3·X³`\n\n"
    md += "| 参数 | 物理含义 | CSV字段名 | 单位 |\n|------|----------|----------|------|\n"
    md += "| C0 | 近端Y轴坐标（横向偏移） | lane_curvature0 | m |\n"
    md += "| C1 | 切线夹角θ（航向角） | lane_curvature1 | rad |\n"
    md += "| C2 | 曲率 | lane_curvature2 | 1/m |\n"
    md += "| C3 | 曲率变化率 | lane_curvature3 | 1/m² |\n"
    md += "\n## 一、CSV规则检测\n\n"
    md += f"| 规则 | 结果 |\n|------|------|\n"
    md += f"| 缺失检测 | {'✅ 0次' if not result['missing'] else '❌ '+str(len(result['missing']))+'次'} |\n"
    md += f"| 抖动检测 | {'✅ 0次' if not result['jitter'] else '❌ '+str(len(result['jitter']))+'次'} |\n"
    md += f"| 宽度检测 | {'✅ 0次' if not result['width'] else '❌ '+str(len(result['width']))+'次'} |\n"
    md += f"| 宽度降级(≥4.1m持续5帧) | {'✅ 0次' if not result.get('width_downgrade', []) else '❌ '+str(len(result.get('width_downgrade', [])))+'次'} |\n"
    md += f"| 可视范围骤降 | {'✅ 0次' if not result.get('range_drop', []) else '❌ '+str(len(result.get('range_drop', [])))+'次'} |\n"
    md += "\n## 本车道线丢失事件\n\n"
    md += "| 时间 | 侧 | 事件 | 详情 |\n|------|:--:|:----:|------|\n"
    evs = result.get("own_lane_events", [])
    if evs:
        for ev in evs[:50]:
            md += f"| {ev['ts']} | {ev['side']} | {ev['event']} | {ev['detail']} |\n"
    else:
        md += "| - | - | ✅ 无 | 本车道线全程正常 |\n"
    md += "\n## 二、车道线统计\n\n| 车道ID | 含义 | 次数 | 占比 | 平均C0 | 质量 | 可视范围 |\n|:------:|------|:----:|:----:|:------:|:----:|:--------:|\n"
    for lid in sorted(result["lanes"].keys()):
        l = result["lanes"][lid]
        md += f"| {lid} | {ID_LABELS.get(lid, '')} | {l['count']} | {l['pct']:.1f}% | {l['avg_c0']:.4f} | {l['avg_q']:.1f}/3.0 | 0~{l['max_vr']:.0f}m |\n"
    if video_summary:
        md += f"\n## 三、视频清晰度\n\n"
        md += f"| 指标 | 数值 |\n|------|------|\n"
        md += f"| 整体清晰度 | {video_summary['overall_avg']:.2f} |\n"
        md += f"| ⬅左侧车道线 | {video_summary['left_avg']:.2f} |\n"
        md += f"| ➡右侧车道线 | {video_summary['right_avg']:.2f} |\n"
        md += f"| 左右差异 | {video_summary['left_avg']-video_summary['right_avg']:+.2f} |\n"
        md += f"| Hough检测率 | 左{video_summary['left_detected']}/{video_summary['frames']} 右{video_summary['right_detected']}/{video_summary['frames']} |\n"
        if video_summary["scene_counter"]:
            md += "| 场景亮度 | " + " / ".join(f"{k}×{v}" for k, v in video_summary["scene_counter"].items()) + " |\n"
    md += f"\n## 四、结论\n\n"
    md += "⚠️ 发现异常，需进一步分析\n" if has_anomaly else "✅ 数据正常，未发现异常。可作为Baseline基准数据。\n"
    return md


def main():
    ap = argparse.ArgumentParser(description="车道线异常智能归因 - 标准分析流水线")
    ap.add_argument("--csv", default=None, help="车道线感知CSV文件路径(不传则自动检测当前目录CSV)")
    ap.add_argument("--ts", default=None, help="行车视频TS文件路径(不传则自动匹配对应视频)")
    ap.add_argument("--out", default=None, help="输出目录(默认: CSV同目录下 车道线分析_<日期>)")
    ap.add_argument("--title", default="车道线异常智能归因分析报告", help="报告标题")
    args = ap.parse_args()

    # 自动检测 CSV: 不传 --csv 时找当前目录/脚本目录下的 CSV
    csv_path = None
    if args.csv:
        csv_path = os.path.abspath(args.csv)
    else:
        for cand_dir in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
            if not os.path.isdir(cand_dir):
                continue
            csvs = sorted(f for f in os.listdir(cand_dir) if f.lower().endswith(".csv"))
            if csvs:
                csv_path = os.path.join(cand_dir, csvs[0])
                print(f"📂 自动检测CSV: {csvs[0]}")
                break
    if not csv_path or not os.path.exists(csv_path):
        print(f"❌ CSV文件不存在: {csv_path}")
        sys.exit(1)

    # 自动匹配视频: 不传 --ts 时找同目录/子目录的 TS (文件名时间戳匹配CSV起始)
    ts_path = None
    if args.ts:
        ts_path = os.path.abspath(args.ts)
    else:
        csv_name = os.path.basename(csv_path)
        import re as _re
        m = _re.search(r"(\d{2})-(\d{2})-(\d{2})", csv_name)
        if m:
            csv_hms = f"{m.group(1)}{m.group(2)}{m.group(3)}"
            for cand_dir in (os.path.dirname(csv_path), os.getcwd()):
                if not os.path.isdir(cand_dir):
                    continue
                for root, _, files in os.walk(cand_dir):
                    for f in files:
                        if f.lower().endswith((".ts", ".mp4", ".avi")):
                            fm = _re.search(r"_(\d{6})_", f)
                            if fm and fm.group(1) >= csv_hms[:6] and int(fm.group(1)[:2]) == int(csv_hms[:2]):
                                ts_path = os.path.join(root, f)
                                print(f"📂 自动匹配视频: {f}")
                                break
                    if ts_path:
                        break
                if ts_path:
                    break

    out_dir = args.out or os.path.join(os.path.dirname(csv_path), "车道线分析_" + datetime.date.today().strftime("%Y%m%d"))
    os.makedirs(out_dir, exist_ok=True)

    print(f"📊 [1/4] 分析CSV: {csv_path}")
    result, lane_series, width_series = analyze_csv(csv_path)
    print(f"   ✓ {result['total_rows']}行, {len(result['lanes'])}条车道线, 异常: 缺失{len(result['missing'])} 抖动{len(result['jitter'])} 宽度{len(result['width'])}")

    video_summary = None
    if ts_path:
        ts_path = os.path.abspath(ts_path)
        print(f"🎥 [2/4] 分析视频: {ts_path}")
        if os.path.exists(ts_path):
            video_summary, err = analyze_video(ts_path, out_dir=out_dir)
            if err:
                print(f"   ⚠️ {err}")
            else:
                print(f"   ✓ {video_summary['frames']}帧, 整体清晰度{video_summary['overall_avg']:.1f}, 左右差异{video_summary['left_avg']-video_summary['right_avg']:+.2f}")
        else:
            print(f"   ⚠️ TS文件不存在: {ts_path}")

    print(f"📈 [3/4] 生成图表")
    make_charts(result, lane_series, width_series, video_summary, out_dir)

    print(f"📄 [4/4] 生成报告")
    html = generate_html(result, lane_series, width_series, video_summary, out_dir, args.title)
    md = generate_md(result, lane_series, width_series, video_summary, args.title)
    html_path = os.path.join(out_dir, args.title + ".html")
    md_path = os.path.join(out_dir, args.title + ".md")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"\n✅ 分析完成!")
    print(f"   📁 输出目录: {out_dir}")
    print(f"   📄 HTML报告: {os.path.basename(html_path)}")
    print(f"   📄 MD报告: {os.path.basename(md_path)}")
    print(f"   📊 图表: {len([f for f in os.listdir(out_dir) if f.startswith('chart')])}张 + 视频标注图")


if __name__ == "__main__":
    main()
