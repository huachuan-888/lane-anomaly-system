#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
环境自动配置脚本 (setup_env.py)
================================
功能: 检测 Python 依赖, 自动安装缺失包, 可选下载 YOLOP 模型
用法:
  python setup_env.py              # 检测+安装依赖
  python setup_env.py --full       # 完整版: 检测+安装+询问模型
  python setup_env.py --download-model   # 只下载模型
"""
import os
import sys
import subprocess
import importlib.util

# ==================== 依赖清单 ====================
REQUIRED = {
    "openpyxl": "openpyxl",
    "cv2": "opencv-python",
    "numpy": "numpy",
    "onnxruntime": "onnxruntime",   # 完整版需要
}

MODEL_URL = "https://github.com/huachuan-888/lane-anomaly-system/releases/download/v1.0-models/yolop-640-640.onnx"
MODEL_NAME = "yolop-640-640.onnx"
MODEL_EXPECTED_MB = 34.0


def check_deps():
    """逐个检查依赖, 返回缺失列表"""
    missing = []
    for mod, pip_name in REQUIRED.items():
        if importlib.util.find_spec(mod) is None:
            missing.append(pip_name)
            print(f"  ❌ {mod} (缺) -> 需安装 {pip_name}")
        else:
            print(f"  ✅ {mod} 已安装")
    return missing


def install_pkgs(pkgs):
    """pip 安装缺失包"""
    print(f"\n📦 正在安装: {', '.join(pkgs)}")
    r = subprocess.run([sys.executable, "-m", "pip", "install", *pkgs],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("  ✅ 安装成功")
        return True
    print(f"  ❌ 安装失败: {r.stderr[-500:]}")
    return False


def model_path():
    """模型默认位置: 脚本同目录 ../models/yolop-640-640.onnx"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "..", "models", MODEL_NAME)


def check_model():
    """检查模型是否存在"""
    mp = model_path()
    if os.path.exists(mp):
        size_mb = os.path.getsize(mp) / 1024 / 1024
        print(f"  ✅ YOLOP 模型已存在 ({size_mb:.1f} MB)")
        return True
    print(f"  ❌ YOLOP 模型缺失 (需 {MODEL_EXPECTED_MB:.0f} MB)")
    return False


def download_model():
    """下载 YOLOP 模型 (34MB)"""
    mp = model_path()
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    print(f"\n⬇️  正在下载模型 ({MODEL_EXPECTED_MB:.0f}MB)...")
    print(f"  来源: {MODEL_URL}")
    try:
        import urllib.request
        urllib.request.urlretrieve(MODEL_URL, mp)
        size_mb = os.path.getsize(mp) / 1024 / 1024
        print(f"  ✅ 下载完成 ({size_mb:.1f} MB) -> {mp}")
        return True
    except Exception as e:
        print(f"  ❌ 下载失败: {e}")
        print("  💡 可手动下载后放到: " + mp)
        return False


def ask_user():
    """询问用户选择"""
    print("\n🤔 请选择 (输入数字):")
    print("  1. 下载模型 (完整版: YOLOP检测+规则五) [约34MB]")
    print("  2. 不下载 (轻量版: Hough检测+CSV规则, 无模型也可用)")
    try:
        choice = input(">>> ").strip()
        return choice == "1"
    except EOFError:
        print("  (无法交互, 默认轻量版)")
        return False


def main():
    full_mode = "--full" in sys.argv
    only_model = "--download-model" in sys.argv

    print("=" * 50)
    print(" 车道线报告生成器 - 环境配置")
    print("=" * 50)

    if only_model:
        download_model()
        return

    # 1. 检测依赖
    print("\n[1/3] 检查 Python 依赖...")
    missing = check_deps()

    # 2. 安装缺失
    if missing:
        if not install_pkgs(missing):
            print("依赖安装失败, 请手动执行: pip install " + " ".join(missing))
            sys.exit(1)
    else:
        print("  全部依赖就绪 ✅")

    # 3. 模型 (完整版才需要)
    print("\n[2/3] 检查 YOLOP 模型...")
    has_model = check_model()
    if full_mode and not has_model:
        if ask_user():
            download_model()
        else:
            print("  已选择轻量模式 (无 YOLOP, 用 Hough 检测)")
    elif not full_mode and not has_model:
        print("  当前为轻量模式, 不需要模型 (需要完整版加 --full)")

    print("\n[3/3] ✅ 环境就绪!")
    print(f"  - 完整版: python generate_report.py --mode full --data <目录>")
    print(f"  - 轻量版: python generate_report.py --mode light --data <目录>")


if __name__ == "__main__":
    main()
