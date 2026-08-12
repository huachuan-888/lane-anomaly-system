#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
车道线异常智能归因系统 - 本地Web版
================================
启动: python app.py
访问: http://localhost:5000
功能: 仪表盘 / 问题列表 / 单问题深度分析 / 批量自动复核
"""
import os
import sys
import csv
import re
import glob
import base64
import datetime

import openpyxl
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

# ==================== 路径配置 ====================
BASE = r"C:\Users\黄钦\Desktop\DF资料\ai 车道线分析"
XLSX = os.path.join(BASE, "V1.1.6版本测试问题.xlsx")
CSV_DIR = os.path.join(BASE, "同类型CSV_lane_mark_camera_list_1")
VIDEO_DIR = os.path.join(BASE, "视频")
OUT_DIR = os.path.join(BASE, "自动复核输出")

FPS = 25
HEAD_OFFSET = 3
WINDOW = 30

# 复用主脚本的分析函数
sys.path.insert(0, BASE)
from 车道线自动复核 import (
    parse_time_str, sec_to_hms, find_csv, find_video,
    scan_errors, read_frames_sequential, frame_to_b64,
    clarity_score, dual_check, detect_lane_in_frame,
    scene_detect, filter_problem, CONFIG,
)

app = Flask(__name__)

# ==================== 缓存 (加速页面跳转) ====================
_problems_cache = {"data": None, "ts": 0}
_analysis_cache = {}  # num -> result
_video_cache = {}  # video_path -> {frame_idx: frame}  (同一视频只读一次)


def cached_problems():
    """xlsx问题列表缓存 (30秒内不重读)"""
    now = datetime.datetime.now().timestamp()
    if _problems_cache["data"] is None or now - _problems_cache["ts"] > 30:
        _problems_cache["data"] = load_problems()
        _problems_cache["ts"] = now
    return _problems_cache["data"]


@app.template_filter("hms")
def hms_filter(sec):
    """秒 -> HH:MM:SS"""
    return sec_to_hms(sec)

# ==================== 数据加载 ====================
def load_problems():
    """从xlsx加载所有问题"""
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb["V1.1.6测试问题"]
    rows = list(ws.iter_rows(values_only=True))
    problems = []
    for row in rows[7:]:
        if not row[0]:
            continue
        p = {
            "num": row[0],
            "date": str(row[1])[:10] if row[1] else "",
            "tstr": str(row[2]),
            "cat": str(row[3] or ""),
            "desc": str(row[4] or ""),
            "env": str(row[5] or ""),
            "sec": parse_time_str(row[2]),
            "hits": [],
            "scenes": [],
        }
        p["hits"] = filter_problem(p["cat"] + " " + p["desc"], CONFIG["filter_keywords"])
        p["scenes"] = scene_detect(p, CONFIG)
        problems.append(p)
    return problems


def analyze_problem(p):
    """对单个问题做完整分析: CSV+视频+错误点+抽帧"""
    result = {
        "problem": p,
        "csv": None, "video": None,
        "points": [], "frame_b64s": {},
        "csv_ok": False, "video_ok": False,
    }
    if p["sec"] is None or not p["date"]:
        return result
    # CSV定位 + 扫描
    csv_path = find_csv(CSV_DIR, p["date"], p["sec"])
    if csv_path:
        result["csv"] = os.path.basename(csv_path)
        result["csv_ok"] = True
        result["points"] = scan_errors(csv_path, p["sec"], WINDOW)
    # 视频定位
    video_path, vstart = find_video(VIDEO_DIR, CONFIG["video_date_map"], p["date"], p["sec"])
    if video_path:
        result["video"] = os.path.basename(video_path)
        result["video_ok"] = True
        # 对每个错误点抽帧 (b1当刻帧)
        for pt in result["points"][:10]:
            base = pt["sec"] - vstart
            # b1 ±1s 三张截图
            need = set()
            for d in (-1, 0, 1):
                fidx = int((base + d) * FPS) + HEAD_OFFSET
                if 0 <= fidx < 2000:
                    need.add(fidx)
            # 视频帧缓存: 同一视频只读一次, 所有问题复用
            if video_path not in _video_cache:
                _video_cache[video_path] = {}
            vframes = _video_cache[video_path]
            # 只补读缺失的帧
            missing = [f for f in need if f not in vframes]
            if missing:
                got = read_frames_sequential(video_path, missing)
                vframes.update(got)
            frames = vframes
            pt["frames"] = []
            import cv2 as _cv2
            for d in (-1, 0, 1):
                fidx = int((base + d) * FPS) + HEAD_OFFSET
                if fidx in frames:
                    fr = frames[fidx]
                    b64 = frame_to_b64(fr, 42, 420)  # 压缩: 质量42, 宽420 (页面更快)
                    cs = clarity_score(_cv2.cvtColor(fr, _cv2.COLOR_BGR2GRAY), CONFIG["clarity_weights"])
                    det = detect_lane_in_frame(fr)
                    fdata = {
                        "delta": d, "frame_idx": fidx, "b64": b64,
                        "S": round(cs["S"], 1), "L": round(cs["L"]), "E": round(cs["E"], 1),
                        "C": round(cs["C"]), "B": round(cs["B"]), "F": round(cs["F"], 1),
                        "det_w": round(det["width_m"], 2) if det.get("width_m") else None,
                    }
                    pt["frames"].append(fdata)
            # b1当刻帧做双维度校验
            b1_fidx = int(base * FPS) + HEAD_OFFSET
            if b1_fidx in frames:
                concl, reason = dual_check(pt["labels"], frames[b1_fidx], CONFIG)
                pt["check_concl"] = concl
                pt["check_reason"] = reason
    return result


# ==================== 路由 ====================
@app.route("/")
def index():
    problems = cached_problems()
    lane_q = sum(1 for p in problems if p["hits"])
    snake_q = sum(1 for p in problems if "蛇形" in p["hits"])
    return render_template("index.html", problems=problems, lane_q=lane_q, snake_q=snake_q, total=len(problems))


@app.route("/problem/<int:num>")
def problem_detail(num):
    problems = cached_problems()
    nums = [x["num"] for x in problems]
    p = next((x for x in problems if x["num"] == num), None)
    if not p:
        return "问题不存在", 404
    # 分析结果缓存: 同一问题只分析一次
    if num not in _analysis_cache:
        _analysis_cache[num] = analyze_problem(p)
    # 上一个/下一个
    idx = nums.index(num) if num in nums else 0
    prev_num = nums[idx - 1] if idx > 0 else None
    next_num = nums[idx + 1] if idx < len(nums) - 1 else None
    return render_template("problem.html", r=_analysis_cache[num], now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                           prev_num=prev_num, next_num=next_num, cur_idx=idx + 1, total_nums=len(nums))


@app.route("/api/problems")
def api_problems():
    problems = cached_problems()
    return jsonify([{
        "num": p["num"], "date": p["date"], "tstr": p["tstr"],
        "cat": p["cat"], "desc": p["desc"][:50],
        "hits": p["hits"], "scenes": p["scenes"],
    } for p in problems])


# ==================== 文件上传 ====================
ALLOWED_XLSX = {"xlsx", "xlsm"}
ALLOWED_CSV = {"csv"}
ALLOWED_VIDEO = {"ts", "mp4", "avi", "mkv"}


def allowed_file(filename, allowed_set):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_set


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """接收上传文件, 保存到对应目录"""
    results = {"uploaded": [], "errors": []}
    ftype = request.form.get("type", "")
    files = request.files
    if ftype == "xlsx":
        target_dir = BASE
        allowed = ALLOWED_XLSX
    elif ftype == "csv":
        target_dir = CSV_DIR
        allowed = ALLOWED_CSV
    elif ftype == "video":
        target_dir = VIDEO_DIR
        allowed = ALLOWED_VIDEO
    else:
        return jsonify({"ok": False, "error": "未知上传类型"}), 400

    for key, f in files.items():
        if not f or f.filename == "":
            continue
        if not allowed_file(f.filename, allowed):
            results["errors"].append(f"{f.filename}: 文件类型不支持")
            continue
        fname = secure_filename(f.filename)
        try:
            if ftype == "video":
                # 视频按日期子文件夹归档: 检查问题表里该时刻属于哪一天
                save_path = os.path.join(target_dir, fname)
                # 先存根目录, 若能从问题表匹配到日期则移动
                f.save(save_path)
                # 尝试从问题表时间匹配日期 (视频时间戳 -> 问题表日期)
                tstr = re.search(r"ND\d{5}_(\d{6})", fname)
                if tstr:
                    vsec = hms_to_sec(int(tstr.group(1)))
                    target_date = None
                    for p in cached_problems():
                        if p["sec"] is not None and abs(p["sec"] - vsec) < 120:
                            target_date = p["date"]
                            break
                    if target_date:
                        for folder, fdate in CONFIG["video_date_map"].items():
                            if fdate == target_date:
                                fdir = os.path.join(target_dir, folder)
                                os.makedirs(fdir, exist_ok=True)
                                os.replace(save_path, os.path.join(fdir, fname))
                                results["uploaded"].append(f"{folder}/{fname}")
                                break
                        else:
                            results["uploaded"].append(fname)
                    else:
                        results["uploaded"].append(fname)
                else:
                    results["uploaded"].append(fname)
            else:
                f.save(os.path.join(target_dir, fname))
                results["uploaded"].append(fname)
        except Exception as e:
            results["errors"].append(f"{f.filename}: 保存失败 {e}")

    # 上传后清理缓存, 让新文件生效
    _problems_cache["data"] = None
    _analysis_cache.clear()
    return jsonify({"ok": True, **results})


@app.route("/api/run_all")
def api_run_all():
    """调用批量复核脚本生成报告"""
    import subprocess
    r = subprocess.run(
        [sys.executable, os.path.join(BASE, "车道线自动复核.py")],
        capture_output=True, text=True, timeout=600, cwd=BASE
    )
    return jsonify({"ok": r.returncode == 0, "log": r.stdout[-2000:]})


if __name__ == "__main__":
    print("=" * 50)
    print("🚗 车道线异常智能归因系统")
    print("   访问: http://localhost:5000")
    print("=" * 50)
    app.run(host="127.0.0.1", port=5000, debug=False)