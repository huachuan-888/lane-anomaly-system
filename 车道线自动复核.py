#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================
 车道线问题自动复核流水线 (标准化版 v2.0)
============================================================
功能: 基于问题表时间点A, 自动完成:
  模块1 关键字提取    - 从xlsx识别车道线相关问题
  模块2 数据定位      - 匹配车道线CSV (A±30s窗口)
  模块3 错误点扫描    - 检出b1,b2,b3...(缺失/抖动/过宽/过窄/可视范围)
  模块4 视频定位      - 匹配TS视频
  模块5 视频抽帧      - 按b1时间顺序读帧截3张(b1-1s/b1/b1+1s)

关键校准(实测):
  帧号公式: b1帧号 = (b1秒 - 视频起始秒) × 25 + 3帧补偿
  坑1: cap.set()对TS跳帧无效 -> 必须顺序read()
  坑2: 视频开头约3帧缓冲 -> 帧号+3补偿

输出:
  输出根目录/
    reports/   报告(HTML+MD)
    frames/    截图
    logs/      运行日志
============================================================
"""
import csv
import os
import re
import glob
import sys
import argparse
import datetime
import base64
import html as htmlmod
import openpyxl

# ==================== 配置区 ====================
def _detect_base_dir():
    """数据根目录自动探测 (适配任意目录结构, 不固定路径):
    1. 环境变量 LANE_BASE
    2. exe 所在目录下的 '数据' 子目录 (exe打包分发: 数据放exe旁)
    3. exe 所在目录本身
    4. 当前工作目录
    5. 旧硬编码路径 (本机开发)
    """
    def _has_xlsx(d):
        if os.path.exists(os.path.join(d, "V1.1.6版本测试问题.xlsx")):
            return True
        # 自动检测: 目录下有任何 .xlsx 也算
        if os.path.isdir(d):
            return any(f.lower().endswith(".xlsx") for f in os.listdir(d))
        return False

    env = os.environ.get("LANE_BASE")
    if env and _has_xlsx(env):
        return env
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.getcwd()
    cand = os.path.join(exe_dir, "数据")
    if _has_xlsx(cand):
        return cand
    if _has_xlsx(exe_dir):
        return exe_dir
    if _has_xlsx(os.getcwd()):
        return os.getcwd()
    old = r"C:\Users\黄钦\Desktop\DF资料\ai 车道线分析"
    if _has_xlsx(old):
        return old
    return exe_dir


CONFIG = {
    # 数据路径
    "base_dir": _detect_base_dir(),
    "xlsx": "V1.1.6版本测试问题.xlsx",
    "csv_dir": "同类型CSV_lane_mark_camera_list_1",
    "video_dir": "视频",
    # 参数
    "window_csv": 30,          # CSV扫描窗口 ±30s
    "frame_offsets": (-1, 0, 1),  # b1±1s截3张
    "fps": 25,
    "head_offset_frames": 3,   # 视频开头缓冲补偿
    "max_points": 25,          # 每问题最多显示错误点
    "img_quality": 55,
    "img_max_w": 500,
    # 清晰度评估: S = w1*L + w2*E + w3*C + w4*B + w5*F (支持自定义权重)
    "clarity_weights": {"w1": 0.25, "w2": 0.20, "w3": 0.20, "w4": 0.20, "w5": 0.15},
    # 双维度校验: 清晰度分级阈值 (S综合评分)
    "clarity_threshold": {"high": 100, "low": 60},  # S>=100清晰, S<=60模糊, 中间中等
    # 筛选关键词表 (命中任一类即入选)
    "filter_keywords": {
        "车道线": ["车道线", "lane"],
        "压线": ["压线", "越线", "跨线"],
        "蛇形": ["蛇形", "蛇行"],
    },
    # 视频日期目录映射
    "video_date_map": {"6.16": "2026-06-16", "6.17": "2026-06-17", "6.18": "2026-06-18"},
    # 场景识别配置 (辅助归因)
    "scene_keywords": {
        "隧道": ["隧道", "入隧", "出隧"],
        "弯道": ["弯", "匝道", "鱼骨线"],
        "分合流": ["分流", "合流", "分叉"],
    },
    "weather_map": {"晴": "☀️ 晴天", "阴": "☁️ 阴天", "小雨": "🌧️ 小雨", "大雨": "🌧️ 大雨", "雪": "❄️ 雪天"},
    "day_night": {"dawn": 6, "dusk": 18},  # 6点前=夜间, 18点后=夜间
}

APP_VERSION = "2.0.0"
APP_NAME = "车道线问题自动复核流水线"


# ==================== 工具函数 ====================
def log(module, msg):
    """统一日志格式: [模块] 消息"""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{module}] {msg}"
    print(line)
    return line


def scene_detect(problem, cfg):
    """场景识别: 隧道/弯道/分合流(关键字) + 昼夜(按时间) + 天气(环境特性列)"""
    tags = []
    text = str(problem.get("desc", "")) + " " + str(problem.get("cat", ""))
    # 道路场景 (关键字)
    for scene, kws in cfg["scene_keywords"].items():
        for kw in kws:
            if kw in text:
                tags.append(scene)
                break
    # 昼夜 (按时间: 6:00前或18:00后=夜间)
    if problem.get("sec") is not None:
        hour = problem["sec"] // 3600
        dn = cfg["day_night"]
        if hour < dn["dawn"] or hour >= dn["dusk"]:
            tags.append("夜间")
        else:
            tags.append("白天")
    # 天气 (环境特性列)
    env = str(problem.get("env", "")).strip()
    if env:
        tags.append(cfg["weather_map"].get(env, env))
    return tags


def setup_dirs(base_dir):
    """建立标准输出目录结构"""
    out_root = os.path.join(os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__)), "自动复核输出")
    dirs = {
        "root": out_root,
        "reports": os.path.join(out_root, "reports"),
        "frames": os.path.join(out_root, "frames"),
        "logs": os.path.join(out_root, "logs"),
    }
    for d in dirs.values():
        os.makedirs(d, exist_ok=True)
    return dirs


def hms_to_sec(hms):
    return (hms // 10000) * 3600 + ((hms % 10000) // 100) * 60 + (hms % 100)


def parse_time_str(t):
    """解析xlsx时间点字符串, 容错 11.06 / 17::24 等"""
    t = str(t).strip()
    m = re.search(r"(\d{1,2})[:.：](\d{1,2})(?:[:.：](\d{1,2}))?", t)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3) or 0)


def sec_to_hms(sec):
    return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"


def read_csv_handle(path):
    """打开CSV, 兼容utf-8/gbk编码"""
    try:
        return open(path, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return open(path, encoding="gbk", errors="replace")


# ==================== 模块1: 关键字提取 ====================
def filter_problem(desc, filter_cfg):
    """筛选: 命中 车道线/压线/蛇形 任一关键字即返回命中的类别列表"""
    hits = []
    desc_l = str(desc).lower()
    for cat, kws in filter_cfg.items():
        for kw in kws:
            if kw.lower() in desc_l:
                hits.append(cat)
                break
    return hits


# ==================== 模块2: 数据定位 (CSV) ====================
def find_csv(csv_dir, date, sec):
    """按日期+时刻匹配覆盖该时刻的CSV (10分钟窗口)
    csv_dir 不存在时自动在数据根目录递归查找 (适配任意目录结构)
    """
    best = None
    if not os.path.isdir(csv_dir):
        # 回退: 数据根目录递归找 CSV (自动检测当前文件夹数据)
        base = os.path.dirname(csv_dir)
        cands = []
        for root, _, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".csv"):
                    cands.append(os.path.join(root, f))
    else:
        # 递归扫描 csv_dir 下所有 CSV (支持子目录)
        cands = []
        for root, _, files in os.walk(csv_dir):
            for f in files:
                if f.lower().endswith(".csv"):
                    cands.append(os.path.join(root, f))
    for c in cands:
        m = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})", os.path.basename(c))
        if not m:
            continue
        cdate = m.group(1)
        cstart = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
        if cdate == date and cstart <= sec < cstart + 600:
            if best is None or abs(cstart - sec) < abs(best[1] - sec):
                best = (c, cstart)
    return best[0] if best else None


# ==================== 模块3: 错误点扫描 ====================
def scan_errors(csv_path, target_sec, window):
    """扫描CSV中 target±window 秒内的所有错误点(按秒聚合)"""
    prefix = ""
    with read_csv_handle(csv_path) as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        for h in fields:
            if "lane_marks(0)" in h:
                prefix = h[:h.index("lane_marks(0)")]
                break
        prev_c0 = {}
        points = {}
        for row in reader:
            ts = row.get("timestamp", "")
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})", ts)
            if not m:
                continue
            sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if abs(sec - target_sec) > window:
                continue
            found = set()
            c0l = c0r = None
            vr_min = 999
            labels = []
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
                found.add(lid)
                c0 = float(row.get(f"{prefix}lane_marks({li}).lane_curvature0", 0) or 0)
                vr = float(row.get(f"{prefix}lane_marks({li}).view_range_end", 0) or 0)
                if lid == 1:
                    c0l = abs(c0)
                elif lid == -1:
                    c0r = abs(c0)
                if lid in (1, -1):
                    vr_min = min(vr_min, vr)
                    k = f"l{lid}"
                    if k in prev_c0 and abs(c0 - prev_c0[k]) >= 0.2:
                        labels.append(f"抖动 ΔC0={abs(c0 - prev_c0[k]):.2f}m")
                    prev_c0[k] = c0
            if 1 not in found:
                labels.append("左侧车道线丢失")
            if -1 not in found:
                labels.append("右侧车道线丢失")
            if vr_min < 30:
                labels.append(f"可视范围<30m({vr_min:.0f}m)")
            if c0l is not None and c0r is not None:
                w = c0l + c0r
                if w <= 3.4:
                    labels.append("width_narrow")
                elif w >= 4.1:
                    labels.append("width_wide")
                elif w >= 4.0:
                    labels.append("width_warn")
            if labels:
                if sec not in points:
                    points[sec] = {"labels": [], "max_w": 0}
                # 按类型聚合: 每类型每秒只保留最严重的1个
                for lb in labels:
                    if lb.startswith("width"):
                        points[sec]["width_type"] = lb
                    elif "可视" in lb:
                        if "best_range" not in points[sec] or lb < points[sec]["best_range"]:
                            points[sec]["best_range"] = lb
                    elif "抖动" in lb:
                        if "best_jitter" not in points[sec] or lb > points[sec]["best_jitter"]:
                            points[sec]["best_jitter"] = lb
                    else:
                        points[sec]["best_loss"] = lb
                if c0l is not None and c0r is not None:
                    points[sec]["max_w"] = max(points[sec]["max_w"], c0l + c0r)
    # 汇总标签
    result = []
    for s in sorted(points.keys()):
        p = points[s]
        labels_final = []
        if "width_type" in p and p["max_w"] > 0:
            mw = p["max_w"]
            if p["width_type"] == "width_narrow":
                labels_final.append(f"车道线过窄 {mw:.3f}m")
            elif p["width_type"] == "width_wide":
                labels_final.append(f"车道线过宽 {mw:.3f}m")
            else:
                labels_final.append(f"车道线过宽预警 {mw:.3f}m")
        for key in ("best_range", "best_jitter", "best_loss"):
            if key in p:
                labels_final.append(p[key])
        result.append({"sec": s, "labels": labels_final, "max_w": p["max_w"]})
    return result


# ==================== 规则五: 丢失事件链 ====================
def build_lost_chains(points):
    """从错误点聚合丢失事件, 串成事件链
    规则:
      - 同一侧丢失帧间隔<=2s -> 聚合为一次丢失事件
      - 丢失事件前<=5s内有同侧前兆 -> 标记"有前兆的丢失"
      - 记录丢失持续时长
    """
    # 1. 收集所有丢失帧 (按侧)
    lost_frames = {"左": [], "右": []}
    for pt in points:
        for lb in pt["labels"]:
            if "左侧车道线丢失" in lb:
                lost_frames["左"].append(pt["sec"])
            elif "右侧车道线丢失" in lb:
                lost_frames["右"].append(pt["sec"])

    # 2. 收集所有前兆帧 (可视范围<30m, 按侧)
    warning_frames = {"左": [], "右": []}
    for pt in points:
        for lb in pt["labels"]:
            if "可视范围" in lb:
                # 前兆无法区分左右(聚合丢了), 视为双侧都可能
                warning_frames["左"].append(pt["sec"])
                warning_frames["右"].append(pt["sec"])

    chains = []
    for side in ("左", "右"):
        frames = sorted(lost_frames[side])
        if not frames:
            continue
        # 聚合: 间隔<=2s 归为一次事件
        events = []
        cur = [frames[0]]
        for i in range(1, len(frames)):
            if frames[i] - frames[i-1] <= 2:
                cur.append(frames[i])
            else:
                events.append(cur)
                cur = [frames[i]]
        events.append(cur)
        # 每条事件生成链
        for ev in events:
            start, end = ev[0], ev[-1]
            duration = end - start + 1  # 秒数(近似)
            # 找前兆: 丢失开始前<=5s内
            has_warn = any(abs(w - start) <= 5 for w in warning_frames[side])
            chains.append({
                "side": side,
                "start": start,
                "end": end,
                "duration_s": duration,
                "frames": len(ev),
                "has_warning": has_warn,
                "type": "有前兆的丢失" if has_warn else "突发丢失",
            })
    return chains


# ==================== 模块4: 视频定位 ====================
def find_video(video_dir, date_map, date, sec):
    """按日期子文件夹+文件名时间戳匹配视频
    - 优先按 date_map 子目录找 (视频/6.16/)
    - 子目录不存在时递归扫描 video_dir 找匹配视频 (适配任意目录结构)
    """
    # 方式1: date_map 子目录
    for folder, fdate in date_map.items():
        if fdate != date:
            continue
        fdir = os.path.join(video_dir, folder)
        if not os.path.isdir(fdir):
            continue
        for v in os.listdir(fdir):
            m = re.match(r"ND\d{5}_(\d{6})_vedio", v)
            if not m:
                continue
            vstart = hms_to_sec(int(m.group(1)))
            # 窗口放宽: 错误点可早于视频起始最多120s (问题时间点常早于视频录制)
            if vstart - 120 <= sec < vstart + 75:
                return os.path.join(fdir, v), vstart
    # 方式2: 递归扫描 video_dir 自动发现 (任意目录结构)
    search_root = video_dir if os.path.isdir(video_dir) else os.path.dirname(video_dir)
    if os.path.isdir(search_root):
        for root, _, files in os.walk(search_root):
            # 跳过 date_map 已扫过的子目录 (避免重复)
            if os.path.basename(root) in date_map:
                continue
            for v in files:
                m = re.match(r"ND\d{5}_(\d{6})_vedio", v)
                if not m:
                    continue
                vstart = hms_to_sec(int(m.group(1)))
                if vstart - 120 <= sec < vstart + 75:
                    return os.path.join(root, v), vstart
    return None, None


# ==================== 模块5: 视频抽帧 ====================
def read_frames_sequential(video_path, target_frames):
    """顺序读取视频收集目标帧 (seek对TS不可靠!)"""
    import cv2
    cap = cv2.VideoCapture(video_path)
    needed = set(target_frames)
    frames = {}
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if idx in needed:
            frames[idx] = frame.copy()
        idx += 1
    cap.release()
    return frames


def frame_to_b64(frame, quality, max_w):
    """帧转base64, 缩小控制体积"""
    import cv2
    h, w = frame.shape[:2]
    if w > max_w:
        frame = cv2.resize(frame, (max_w, int(h * max_w / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        return base64.b64encode(buf.tobytes()).decode("ascii")
    return None


# ==================== 本车道线标注 ====================
def annotate_own_lane(frame, yolop_full_mask=None):
    """在帧上标注当前车辆所在车道的左右线 (绿=左, 红=右)
    策略: YOLOP掩码 -> 搜索区(0.30w~0.70w)聚类 -> 取最接近标定(0.45w/0.64w)的线簇
    返回: (标注图, 左线x, 右线x)
    """
    import cv2
    import numpy as np
    h, w = frame.shape[:2]
    annotated = frame.copy()
    left_x = right_x = -1

    # 获取 YOLOP 掩码 (复用检测, 避免重复推理)
    if yolop_full_mask is None:
        det = yolop_detect(frame)
        if det is None or det.get("pixels", 0) == 0:
            return annotated, -1, -1
        # 重新跑掩码
        sess = _yolop_session()
        if sess is None:
            return annotated, -1, -1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        img_eq = cv2.cvtColor(clahe.apply(gray), cv2.COLOR_GRAY2BGR)
        img = cv2.cvtColor(img_eq, cv2.COLOR_BGR2RGB)
        inp = np.transpose(cv2.resize(img, (640, 640)).astype(np.float32) / 255.0, (2, 0, 1))[None]
        det_out, drive_seg, lane_seg = sess.run(None, {"images": inp})
        lane_mask = np.argmax(lane_seg[0], axis=0)
        yolop_full_mask = cv2.resize((lane_mask == 1).astype(np.uint8) * 255, (w, h), interpolation=cv2.INTER_NEAREST)

    # 搜索区 (0.30w ~ 0.70w): 本车道线所在范围
    x_lo, x_hi = int(w * 0.30), int(w * 0.70)
    roi = yolop_full_mask[int(h * 0.55):, x_lo:x_hi]
    col_sum = np.sum(roi > 0, axis=0)
    xs = np.where(col_sum > 0)[0]
    if len(xs) > 0:
        xs = xs + x_lo
        # 聚类 (间隔>15px 为不同线)
        clusters = []
        cur = [xs[0]]
        for i in range(1, len(xs)):
            if xs[i] - xs[i-1] > 15:
                clusters.append(int(np.mean(cur)))
                cur = [xs[i]]
            else:
                cur.append(xs[i])
        clusters.append(int(np.mean(cur)))
        # 取最接近标定位置的线 (左≈0.45w, 右≈0.64w)
        ref_l, ref_r = int(w * 0.45), int(w * 0.64)
        left_cands = [c for c in clusters if c < w // 2]
        right_cands = [c for c in clusters if c >= w // 2]
        if left_cands:
            left_x = min(left_cands, key=lambda c: abs(c - ref_l))
        if right_cands:
            right_x = min(right_cands, key=lambda c: abs(c - ref_r))

    # 画标注
    if left_x > 0:
        cv2.line(annotated, (left_x, int(h * 0.55)), (left_x, h), (0, 255, 0), 3)
        cv2.putText(annotated, f"L {left_x}", (max(10, left_x - 70), int(h * 0.52)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    if right_x > 0:
        cv2.line(annotated, (right_x, int(h * 0.55)), (right_x, h), (0, 0, 255), 3)
        cv2.putText(annotated, f"R {right_x}", (min(w - 120, right_x + 10), int(h * 0.52)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    return annotated, left_x, right_x


def clarity_level(S, cfg):
    """清晰度分级: 高/中/低"""
    th = cfg["clarity_threshold"]
    if S >= th["high"]:
        return "高", "清晰"
    elif S <= th["low"]:
        return "低", "模糊"
    return "中", "中等"


# ==================== YOLOP 深度学习检测 (可选, 需模型文件) ====================
_YOLOP_SESSION = None
_YOLOP_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "yolop-640-640.onnx")


def _yolop_session():
    """懒加载 YOLOP 模型 (只加载一次)"""
    global _YOLOP_SESSION
    if _YOLOP_SESSION is None and os.path.exists(_YOLOP_MODEL):
        import onnxruntime as ort
        _YOLOP_SESSION = ort.InferenceSession(_YOLOP_MODEL, providers=["CPUExecutionProvider"])
    return _YOLOP_SESSION


def yolop_detect(frame):
    """YOLOP 语义分割检测车道线: 返回左右线位置(原图坐标)和估算宽度
    优化: ① CLAHE增强(画面偏暗) ② 单侧互补(Hough补另一侧)
    """
    import cv2
    import numpy as np
    sess = _yolop_session()
    if sess is None:
        return None
    try:
        h, w = frame.shape[:2]
        # ① CLAHE 增强 (画面偏暗, 增强对比度提升检测率)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray_eq = clahe.apply(gray)
        img_eq = cv2.cvtColor(gray_eq, cv2.COLOR_GRAY2BGR)
        img = cv2.cvtColor(img_eq, cv2.COLOR_BGR2RGB)
        img_resized = cv2.resize(img, (640, 640))
        inp = np.transpose(img_resized.astype(np.float32) / 255.0, (2, 0, 1))[None]
        det_out, drive_seg, lane_seg = sess.run(None, {"images": inp})
        lane_mask = np.argmax(lane_seg[0], axis=0)
        lane_binary = (lane_mask == 1).astype(np.uint8) * 255
        lane_full = cv2.resize(lane_binary, (w, h), interpolation=cv2.INTER_NEAREST)
        roi = lane_full[int(h * 0.55):, :]
        col_sum = np.sum(roi > 0, axis=0)
        xs = np.where(col_sum > 0)[0]
        if len(xs) == 0:
            return {"left_x": -1, "right_x": -1, "left_ok": False, "right_ok": False, "width_m": None, "method": "yolop", "pixels": 0}
        center = w // 2
        left_xs = xs[xs < center]
        right_xs = xs[xs >= center]
        left_ok = len(left_xs) > 0
        right_ok = len(right_xs) > 0
        left_x = int(np.mean(left_xs)) if left_ok else -1
        right_x = int(np.mean(right_xs)) if right_ok else -1
        # ② 单侧互补: 缺失的一侧用 Hough 补
        if left_ok != right_ok:
            hough_side = _hough_single_side(frame, side="left" if not left_ok else "right")
            if hough_side is not None:
                if not left_ok:
                    left_x, left_ok = hough_side
                else:
                    right_x, right_ok = hough_side
        width_m = None
        if left_ok and right_ok:
            ref_px = int(w * 0.72) - int(w * 0.28)
            width_m = 3.65 * (right_x - left_x) / ref_px if ref_px > 0 else None
        return {"left_x": left_x, "right_x": right_x, "left_ok": left_ok, "right_ok": right_ok,
                "width_m": width_m, "method": "yolop+hough" if (left_ok and right_ok and (left_xs.size == 0 or right_xs.size == 0)) else "yolop",
                "pixels": int(np.sum(lane_mask == 1))}
    except Exception:
        return None


def _hough_single_side(frame, side):
    """Hough 检测单侧车道线: 返回 (x位置, 是否检出)"""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi_y1 = int(h * 0.55)
    roi = gray[roi_y1:h, int(w * 0.1):int(w * 0.9)]
    edges = cv2.Canny(roi, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50, minLineLength=40, maxLineGap=50)
    if lines is None:
        return None
    roi_h, roi_w = roi.shape
    best = None
    line_arrs = lines[:, 0] if lines.ndim == 3 else lines
    for line in line_arrs:
        x1, y1, x2, y2 = int(line[0]), int(line[1]), int(line[2]), int(line[3])
        if x2 == x1:
            continue
        slope = (y2 - y1) / (x2 - x1)
        length = float(np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2))
        if length < 30:
            continue
        cx = (x1 + x2) / 2 + int(w * 0.1)
        if abs(slope) > 0.2:
            if side == "left" and slope < -0.15 and cx < roi_w * 0.55 + int(w * 0.1):
                if best is None or length > best[1]:
                    best = (int(cx), length)
            elif side == "right" and slope > 0.15 and cx > roi_w * 0.45 + int(w * 0.1):
                if best is None or length > best[1]:
                    best = (int(cx), length)
    if best:
        return (best[0], True)
    return None


def detect_lane_in_frame(frame):
    """检测画面中的左右车道线: YOLOP优先(精确), 无模型时Hough回退"""
    import cv2
    import numpy as np
    # YOLOP 优先 (语义分割, 更准)
    yolop_res = yolop_detect(frame)
    if yolop_res is not None and (yolop_res["left_ok"] or yolop_res["right_ok"]):
        return yolop_res
    # Hough 回退
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi_y1 = int(h * 0.55)
    roi = gray[roi_y1:h, int(w * 0.1):int(w * 0.9)]
    edges = cv2.Canny(roi, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50, minLineLength=40, maxLineGap=50)
    import cv2
    import numpy as np
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    roi_y1 = int(h * 0.55)
    roi = gray[roi_y1:h, int(w * 0.1):int(w * 0.9)]
    edges = cv2.Canny(roi, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50, minLineLength=40, maxLineGap=50)
    left_x = right_x = -1
    left_ok = right_ok = False
    if lines is not None:
        roi_h, roi_w = roi.shape
        lc, rc = [], []
        line_arrs = lines[:, 0] if lines.ndim == 3 else lines
        for line in line_arrs:
            x1, y1, x2, y2 = int(line[0]), int(line[1]), int(line[2]), int(line[3])
            if x2 == x1:
                continue
            slope = (y2 - y1) / (x2 - x1)
            length = float(np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2))
            if length < 30:
                continue
            cx = (x1 + x2) / 2 + int(w * 0.1)
            if abs(slope) > 0.2:
                if slope < -0.15 and cx < roi_w * 0.55 + int(w * 0.1):
                    lc.append((cx, length))
                elif slope > 0.15 and cx > roi_w * 0.45 + int(w * 0.1):
                    rc.append((cx, length))
        if lc:
            left_x = int(max(lc, key=lambda l: l[1])[0])
            left_ok = True
        if rc:
            right_x = int(max(rc, key=lambda l: l[1])[0])
            right_ok = True
    # 画面实际车道宽度估算 (标定: 左≈0.28w, 右≈0.72w ≈ 3.65m)
    width_m = None
    if left_ok and right_ok:
        px_w = right_x - left_x
        ref_px = int(w * 0.72) - int(w * 0.28)  # 参考像素宽 (3.65m)
        width_m = 3.65 * px_w / ref_px if ref_px > 0 else None
    return {"left_x": left_x, "right_x": right_x, "left_ok": left_ok, "right_ok": right_ok,
            "width_m": width_m, "method": "hough"}


def dual_check(csv_labels, frame, cfg):
    """双维度同步校验: CSV数值异常 vs 画面实际车道线检测 → 归因结论
    (修正: 不再用清晰度判误报, 而是检测画面里的真实车道线状态)
    """
    has_csv_anomaly = len(csv_labels) > 0
    # 画面车道线检测
    det = detect_lane_in_frame(frame)
    # CSV异常类型
    csv_type = "其他"
    for lb in csv_labels:
        if "过宽" in lb:
            csv_type = "过宽"
        elif "过窄" in lb:
            csv_type = "过窄"
        elif "丢失" in lb:
            csv_type = "丢失"
    if not has_csv_anomaly:
        return "✅ 双维度正常", "CSV无异常"
    # 画面检测失败 (光线差/无线)
    if not det["left_ok"] or not det["right_ok"]:
        return "🔍 需人工复核", "画面车道线检测不完全, 建议人工查看截图确认"
    # 置信度检查: YOLOP像素太少说明检测不可靠 -> 复核
    if det.get("method") == "yolop" and det.get("pixels", 0) < 3000:
        return "🔍 需人工复核", f"YOLOP检测像素仅{det.get('pixels', 0)}(不可靠), 建议人工查看截图确认"
    # 画面实际宽度
    wm = det["width_m"]
    if wm is None:
        return "🔍 需人工复核", "画面宽度估算失败"
    # 对比CSV异常类型
    if csv_type == "过宽" and wm < 4.0:
        return "⚠️ 疑似感知误报", f"CSV报过宽但画面实测仅{wm:.2f}m(正常范围)"
    if csv_type == "过宽" and wm >= 4.0:
        return "✅ 真实异常", f"CSV报过宽且画面实测{wm:.2f}m, 画面确实偏宽"
    if csv_type == "过窄" and wm > 3.3:
        return "⚠️ 疑似感知误报", f"CSV报过窄但画面实测{wm:.2f}m(正常范围)"
    if csv_type == "丢失" and det["left_ok"] and det["right_ok"]:
        return "⚠️ 感知漏检", f"CSV报丢失但画面检测到左右线({det['left_x']},{det['right_x']})"
    if csv_type == "丢失":
        return "✅ 真实丢失", "CSV报丢失且画面未检测到完整车道线"
    return "🔍 需人工复核", f"CSV{csv_type}, 画面宽度{wm:.2f}m, 建议人工确认"


def clarity_score(gray, weights):
    """五指标清晰度评估: S = w1*L + w2*E + w3*C + w4*B + w5*F"""
    import cv2
    import numpy as np
    lap = cv2.Laplacian(gray, cv2.CV_64F)
    L = float(np.var(lap))
    edges = cv2.Canny(gray, 50, 150)
    E = float(np.sum(edges > 0) / gray.size * 1000)
    C = float(np.std(gray))
    grad = np.diff(gray.astype(np.float32), axis=1)
    B = float(np.mean(grad ** 2))
    fft = np.fft.fft2(gray)
    mag = np.abs(np.fft.fftshift(fft))
    te = float(np.sum(mag))
    rh, rw = gray.shape
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
    S = weights["w1"] * (L / 100) + weights["w2"] * E + weights["w3"] * C + weights["w4"] * B + weights["w5"] * F
    return {"L": L, "E": E, "C": C, "B": B, "F": F, "S": S}


def plan_frame_indices(points, vstart, cfg):
    """预计算所有错误点需要的帧号"""
    need_frames = set()
    point_frames = {}
    for pt in points[: cfg["max_points"]]:
        base = pt["sec"] - vstart
        pf = {}
        for d in cfg["frame_offsets"]:
            o = base + d
            if 0 <= o < 75:
                fidx = int(o * cfg["fps"]) + cfg["head_offset_frames"]
                pf[d] = fidx
                need_frames.add(fidx)
        point_frames[pt["sec"]] = pf
    return need_frames, point_frames


# ==================== 报告生成 ====================
CSS = """
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--text:#e2e8f0;--text2:#94a3b8;--red:#ef4444;--green:#22c55e;--yellow:#eab308;--blue:#38bdf8}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;padding:24px;max-width:1200px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}.sub{color:var(--text2);font-size:13px;margin-bottom:20px}
.stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 18px;min-width:110px}
.stat .n{font-size:22px;font-weight:700}.stat .l{font-size:12px;color:var(--text2)}
.problem-card{background:var(--card);border:1px solid var(--border);border-radius:10px;margin-bottom:10px;overflow:hidden}
.problem-card summary{cursor:pointer;padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;list-style:none}
.problem-card summary::-webkit-details-marker{display:none}
.problem-card summary:hover{background:#243349}
.pnum{background:#334155;color:var(--blue);font-weight:700;border-radius:6px;padding:2px 8px}
.ptime{font-weight:600;font-size:14px}.pcat{color:var(--text2);font-size:13px}
.pscene{display:flex;gap:4px;flex-wrap:wrap}
.sc{font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(56,189,248,.15);color:var(--blue);font-weight:600}
.status{font-size:12px;font-weight:600;margin-left:auto}
.pbody{padding:14px 16px;border-top:1px solid var(--border)}
.pdesc{margin-bottom:12px;font-size:13px;color:var(--text2)}
.ev-count{font-size:13px;color:var(--blue);margin-bottom:10px}
.event{background:#16213a;border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px}
.ev-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.ev-idx{background:#334155;color:var(--yellow);font-weight:700;border-radius:6px;padding:2px 8px;font-size:13px}
.ev-time{font-weight:700;color:var(--yellow);font-size:15px}
.ev-types{display:flex;gap:4px;flex-wrap:wrap}
.et{font-size:11px;padding:2px 8px;border-radius:10px;background:rgba(239,68,68,.15);color:var(--red);font-weight:600}
.frames{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
@media(max-width:600px){.frames{grid-template-columns:1fr}}
.frame img{width:100%;border-radius:6px;border:1px solid var(--border)}
.cap{font-size:11px;color:var(--text2);text-align:center;margin-top:3px;font-weight:600}
.clr{font-size:10px;color:var(--green);text-align:center;margin-top:2px}
.dual-check{margin-top:8px;padding:6px 10px;background:rgba(56,189,248,.08);border-left:3px solid var(--blue);border-radius:4px;font-size:12px}
.dc-label{color:var(--text2);font-weight:600}
.dc-concl{color:var(--yellow);font-weight:700}
.dc-reason{color:var(--text2);margin-left:6px}.no-data{background:#243349;border:1px dashed var(--border);border-radius:8px;padding:16px;text-align:center;color:var(--text2);font-size:13px}
.trend-h{font-size:16px;margin:24px 0 12px;color:var(--blue)}
.trend-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px}
@media(max-width:800px){.trend-grid{grid-template-columns:1fr}}
.trend-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.trend-title{font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text)}
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px}
.bar-label{width:110px;text-align:right;color:var(--text2);flex-shrink:0}
.bar-track{flex:1;background:#243349;border-radius:4px;height:16px;overflow:hidden}
.bar-fill{height:100%;border-radius:4px}
.bar-val{width:40px;color:var(--text);font-weight:600;flex-shrink:0}
.chains{margin-top:12px;background:#16213a;border:1px solid var(--border);border-radius:8px;padding:10px}
.chains-title{font-size:13px;font-weight:700;color:var(--blue);margin-bottom:8px}
.chains-body{display:flex;flex-wrap:wrap;gap:6px}
.chain-item{font-size:12px;padding:4px 10px;border-radius:6px;font-weight:600}
.chain-warn{background:rgba(56,189,248,.15);color:var(--blue)}
.chain-burst{background:rgba(239,68,68,.15);color:var(--red)}.more{font-size:12px;color:var(--text2);text-align:center;padding:8px}
"""


def generate_html(results, total_points, total_frames, cfg, out_time):
    """生成标准化HTML报告"""
    lane_problems = [r["problem"] for r in results]
    items = []
    # ===== 异常趋势数据 =====
    trend_problem = []
    for e in results:
        trend_problem.append((str(e["problem"]["num"]), len(e["points"])))
    max_p = max((n for _, n in trend_problem), default=1)
    type_count = {"车道线过宽": 0, "车道线过窄": 0, "丢失": 0, "抖动": 0, "可视范围": 0}
    for e in results:
        for pt in e["points"]:
            for lb in pt["labels"]:
                if "过宽" in lb: type_count["车道线过宽"] += 1
                elif "过窄" in lb: type_count["车道线过窄"] += 1
                elif "丢失" in lb: type_count["丢失"] += 1
                elif "抖动" in lb: type_count["抖动"] += 1
                elif "可视" in lb: type_count["可视范围"] += 1
    max_t = max(type_count.values(), default=1)
    concl_count = {"⚠️ 疑似感知误报": 0, "✅ 真实异常": 0, "⚠️ 感知漏检": 0, "✅ 真实丢失": 0, "🔍 需人工复核": 0}
    for e in results:
        for pt in e["points"]:
            b1_fidx = e["point_frames"].get(pt["sec"], {}).get(0)
            if b1_fidx is not None and b1_fidx in e["video_frames"]:
                c, _ = dual_check(pt["labels"], e["video_frames"][b1_fidx], cfg)
                for k in concl_count:
                    if k in c:
                        concl_count[k] += 1
                        break
    max_c = max(concl_count.values(), default=1)
    def _bars(items_list, maxv, color):
        html = ""
        for label, val in items_list:
            pct = int(val / maxv * 100) if maxv > 0 else 0
            html += f'<div class="bar-row"><span class="bar-label">{label}</span><div class="bar-track"><div class="bar-fill" style="width:{pct}%;background:{color}"></div></div><span class="bar-val">{val}</span></div>'
        return html
    trend_by_problem = _bars(trend_problem, max_p, "var(--blue)")
    trend_by_type = _bars([(k, v) for k, v in type_count.items()], max_t, "var(--yellow)")
    trend_by_concl = _bars([(k, v) for k, v in concl_count.items()], max_c, "var(--red)")
    for e in results:
        p = e["problem"]
        points = e["points"]
        video_frames = e["video_frames"]
        point_frames = e["point_frames"]
        csv_s = "✅" if e["csv"] else "❌"
        vid_s = "✅" if e["video"] else "❌"

        pts_html = ""
        # 预计算每帧清晰度 (只对用到的帧)
        clarity_cache = {}
        for fidx, fr in video_frames.items():
            import cv2 as _cv2
            clarity_cache[fidx] = clarity_score(_cv2.cvtColor(fr, _cv2.COLOR_BGR2GRAY), cfg["clarity_weights"])
        for idx, pt in enumerate(points[: cfg["max_points"]], 1):
            labels_html = "".join(f'<span class="et">{htmlmod.escape(lb)}</span>' for lb in pt["labels"])
            pf = point_frames.get(pt["sec"], {})
            fhtml = ""
            if pf:
                fhtml = '<div class="frames">'
                for d in cfg["frame_offsets"]:
                    fidx = pf.get(d)
                    if fidx is not None and fidx in video_frames:
                        # 本车道线标注 (绿=左, 红=右)
                        _annot, _lx, _rx = annotate_own_lane(video_frames[fidx])
                        b64 = frame_to_b64(_annot, cfg["img_quality"], cfg["img_max_w"])
                        if b64:
                            dlabel = f"{d:+d}s" if d else "b1"
                            fhtml += f'<div class="frame"><img src="data:image/jpeg;base64,{b64}"><div class="cap">{dlabel} (帧{fidx})</div><div class="clr">S={clarity_cache[fidx]["S"]:.1f} L{clarity_cache[fidx]["L"]:.0f}/E{clarity_cache[fidx]["E"]:.1f}/C{clarity_cache[fidx]["C"]:.0f}/B{clarity_cache[fidx]["B"]:.0f}/F{clarity_cache[fidx]["F"]:.1f}</div></div>'
                fhtml += "</div>"
            if not fhtml or "<img" not in fhtml:
                fhtml = '<div class="no-data">📭 无视频帧</div>'
            # 双维度同步校验 (b1当刻帧S值)
            check_html = ""
            b1_fidx = pf.get(0)
            if b1_fidx is not None and b1_fidx in video_frames:
                _fr = video_frames[b1_fidx]
                _cs = clarity_cache.get(b1_fidx, {})
                S_b1 = _cs.get("S", 0)
                concl, reason = dual_check(pt["labels"], _fr, cfg)
                _det = detect_lane_in_frame(_fr)
                _det_txt = ""
                if _det["left_ok"] and _det["right_ok"]:
                    _det_txt = f" | 画面检测: 左{_det['left_x']}px 右{_det['right_x']}px 宽{_det['width_m']:.2f}m"
                elif _det["left_ok"] or _det["right_ok"]:
                    _det_txt = " | 画面检测: 仅检测到单侧车道线"
                else:
                    _det_txt = " | 画面检测: 未检测到车道线"
                check_html = f'<div class="dual-check"><span class="dc-label">双维度校验:</span> <span class="dc-concl">{concl}</span> <span class="dc-reason">{reason}{_det_txt} (S={S_b1:.1f})</span></div>'
            pts_html += f'''
<div class="event">
  <div class="ev-head">
    <span class="ev-idx">b{idx}</span>
    <span class="ev-time">⏱ {sec_to_hms(pt["sec"])}</span>
    <span class="ev-types">{labels_html}</span>
    {check_html}
  </div>
  {fhtml}
</div>'''
        if len(points) > cfg["max_points"]:
            pts_html += f'<div class="more">… 另有 {len(points) - cfg["max_points"]} 个错误点未列出</div>'

        if points:
            # 规则五事件链展示
            chains_html = ""
            if e.get("lost_chains"):
                items_c = []
                for ch in e["lost_chains"]:
                    warn_tag = "🔵 有前兆" if ch["has_warning"] else "🔴 突发"
                    cls = "chain-warn" if ch["has_warning"] else "chain-burst"
                    items_c.append(f'<span class="chain-item {cls}">{ch["side"]}侧 {sec_to_hms(ch["start"])}~{sec_to_hms(ch["end"])} 持续{ch["duration_s"]}s ({warn_tag})</span>')
                chains_html = f'<div class="chains"><div class="chains-title">🔗 丢失事件链 (规则五)</div><div class="chains-body">{" ".join(items_c)}</div></div>'
            body = f'<div class="ev-count">A={str(p["tstr"])} ±{cfg["window_csv"]}s 检出 <b>{len(points)}</b> 个错误点</div>' + pts_html + chains_html
        else:
            body = '<div class="no-data">✅ A±30s 窗口内未检出异常</div>'

        items.append(f'''
<details class="problem-card" {"open" if p["num"] in (2, 3) else ""}>
  <summary>
    <span class="pnum">#{p["num"]}</span>
    <span class="ptime">{p["date"]} {str(p["tstr"])}</span>
    <span class="pcat">{htmlmod.escape(p["cat"])}</span>
    <span class="pscene">{" ".join(f'<span class="sc">{htmlmod.escape(s)}</span>' for s in p.get("scenes", []))}</span>
    <span class="status">CSV{csv_s} 视频{vid_s} | {len(points)}个错误点</span>
  </summary>
  <div class="pbody">
    <div class="pdesc"><b>描述:</b> {htmlmod.escape(p["desc"])}</div>
    {body}
  </div>
</details>''')

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME}报告</title>
<style>{CSS}</style></head>
<body>
<h1>🚗 {APP_NAME}报告</h1>
<div class="sub">版本v{APP_VERSION} · 生成时间 {out_time} · 筛选: 车道线/压线/蛇形 · CSV窗口±{cfg["window_csv"]}s, 抽帧b1±1s×3张</div>
<div class="stats">
  <div class="stat"><div class="n">{len(lane_problems)}</div><div class="l">车道线问题</div></div>
  <div class="stat"><div class="n">{total_points}</div><div class="l">检出错误点</div></div>
  <div class="stat"><div class="n">{total_frames}</div><div class="l">视频截图</div></div>
</div>
<h2 class="trend-h">📈 异常趋势分析</h2>
<div class="trend-grid">
  <div class="trend-card">
    <div class="trend-title">按问题编号的异常分布</div>
    {trend_by_problem}
  </div>
  <div class="trend-card">
    <div class="trend-title">异常类型占比</div>
    {trend_by_type}
  </div>
  <div class="trend-card">
    <div class="trend-title">双维度校验结论分布</div>
    {trend_by_concl}
  </div>
</div>
{''.join(items)}
<div style="height:40px"></div>
</body></html>"""
    return html


def generate_md(results, total_points, total_frames, cfg, out_time):
    """生成标准化Markdown报告"""
    lines = [f"# 🚗 {APP_NAME}报告", ""]
    lines.append(f"- **版本**: v{APP_VERSION}")
    lines.append(f"- **生成时间**: {out_time}")
    lines.append(f"- **CSV窗口**: ±{cfg['window_csv']}s, 抽帧 b1±1s×3张")
    lines.append(f"- **错误点总数**: {total_points}, **截图**: {total_frames}")
    lines.append("")
    lines.append("## 问题明细")
    for e in results:
        p = e["problem"]
        points = e["points"]
        scenes_txt = " / ".join(p.get("scenes", [])) if p.get("scenes") else "-"
        lines.append(f"### #{p['num']} {p['date']} {str(p['tstr'])} [{p['cat']}] 场景: {scenes_txt}")
        lines.append(f"描述: {p['desc']}")
        lines.append(f"CSV: {'✅' if e['csv'] else '❌'} | 视频: {'✅' if e['video'] else '❌'} | 错误点: {len(points)}")
        if points:
            lines.append("")
            lines.append("| # | 时间 | 错误类型 |")
            lines.append("|---|------|---------|")
            for idx, pt in enumerate(points[: cfg["max_points"]], 1):
                labels = " / ".join(pt["labels"]) if pt["labels"] else "-"
                lines.append(f"| b{idx} | {sec_to_hms(pt['sec'])} | {labels} |")
        lines.append("")
    return "\n".join(lines)


# ==================== 主流程 ====================
def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--base", default=CONFIG["base_dir"], help="数据根目录")
    parser.add_argument("--window", type=int, default=CONFIG["window_csv"], help="CSV扫描窗口(秒)")
    parser.add_argument("--max-points", type=int, default=CONFIG["max_points"], help="每问题最大错误点数")
    args = parser.parse_args()

    cfg = dict(CONFIG)
    cfg["base_dir"] = args.base
    cfg["window_csv"] = args.window
    cfg["max_points"] = args.max_points

    log("初始化", f"{APP_NAME} v{APP_VERSION}")
    dirs = setup_dirs(cfg["base_dir"])
    log("初始化", f"输出目录: {dirs['root']}")

    # 读xlsx (自动发现: 固定名不存在时找任意 .xlsx)
    xlsx_path = os.path.join(cfg["base_dir"], cfg["xlsx"])
    if not os.path.exists(xlsx_path):
        # 自动检测 base_dir 下任意 xlsx
        for f in os.listdir(cfg["base_dir"]):
            if f.lower().endswith(".xlsx"):
                xlsx_path = os.path.join(cfg["base_dir"], f)
                log("初始化", f"自动检测问题表: {f}")
                break
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["V1.1.6测试问题"]
    rows = list(ws.iter_rows(values_only=True))
    problems = []
    for row in rows[7:]:
        if not row[0]:
            continue
        problems.append({
            "num": row[0],
            "date": str(row[1])[:10] if row[1] else "",
            "tstr": row[2],
            "cat": str(row[3] or ""),
            "desc": str(row[4] or ""),
            "env": str(row[5] or ""),
            "sec": parse_time_str(row[2]),
        })
    log("模块1", f"提取 {len(problems)} 个问题")

    lane_problems = []
    for p in problems:
        hits = filter_problem(str(p["cat"]) + " " + str(p["desc"]), cfg["filter_keywords"])
        if hits:
            p["hits"] = hits
            p["scenes"] = scene_detect(p, cfg)
            lane_problems.append(p)
    log("模块1", f"筛选命中(车道线/压线/蛇形)问题 {len(lane_problems)} 个")

    results = []
    total_points = 0
    total_frames = 0
    for p in lane_problems:
        if p["sec"] is None or not p["date"]:
            continue
        # 模块2: CSV定位
        csv_path = find_csv(cfg["csv_dir"] and os.path.join(cfg["base_dir"], cfg["csv_dir"]), p["date"], p["sec"])
        # 模块4: 视频定位
        video_path, vstart = find_video(os.path.join(cfg["base_dir"], cfg["video_dir"]), cfg["video_date_map"], p["date"], p["sec"])
        # 模块3: 错误点扫描
        points = scan_errors(csv_path, p["sec"], cfg["window_csv"]) if csv_path else []
        total_points += len(points)

        # 规则五: 丢失事件链
        lost_chains = build_lost_chains(points) if points else []
        # 模块5: 抽帧
        video_frames = {}
        point_frames = {}
        if video_path and points:
            need_frames, point_frames = plan_frame_indices(points, vstart, cfg)
            video_frames = read_frames_sequential(video_path, need_frames)
            total_frames += sum(1 for pf in point_frames.values() for fidx in pf.values() if fidx in video_frames)

        results.append({
            "problem": p, "csv": csv_path, "video": video_path,
            "points": points, "video_frames": video_frames, "point_frames": point_frames,
            "lost_chains": lost_chains,
        })
        log("处理", f"#{p['num']} {str(p['tstr'])}: {len(points)}个错误点, {sum(1 for pf in point_frames.values() for f in pf.values() if f in video_frames)}张截图")

    # 生成报告
    out_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = generate_html(results, total_points, total_frames, cfg, out_time)
    md = generate_md(results, total_points, total_frames, cfg, out_time)

    html_path = os.path.join(dirs["reports"], "错误点复核报告.html")
    md_path = os.path.join(dirs["reports"], "错误点复核报告.md")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    # 运行日志
    log_path = os.path.join(dirs["logs"], f"run_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"{APP_NAME} v{APP_VERSION}\n生成时间: {out_time}\n")

    log("完成", f"HTML: {html_path}")
    log("完成", f"MD: {md_path}")
    log("完成", f"错误点 {total_points} | 截图 {total_frames}")


if __name__ == "__main__":
    main()