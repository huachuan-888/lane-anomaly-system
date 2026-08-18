# PyInstaller 桌面版 exe 打包（车道线归因系统实测）

把 Flask Web 系统（app.py + templates/ + 车道线自动复核.py）打包成**可分发 exe**（新电脑双击运行、无需装 Python）的完整配方。本会话实测（2026-08，PyInstaller 6.22.0）。

## 打包命令

```powershell
cd <车道线异常智能归因系统目录>
python -m PyInstaller --noconfirm --onedir --name "车道线归因系统" `
  --add-data "templates;templates" `
  --paths "<AI分析工具目录>" `        # 让 PyInstaller 找到 车道线自动复核.py
  --hidden-import openpyxl `
  --collect-submodules openpyxl `
  app.py
```

产物：`dist/车道线归因系统/`（exe 51.6MB + `_internal/` 运行库）。

## 4 个必踩坑（按踩坑顺序）

### 坑1：GBK 控制台 emoji 崩溃（启动即挂）
exe 的控制台用 GBK 编码，`print("🚗 车道线异常智能归因系统")` → `UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f697'` → 进程直接退出（stderr 只有 `Failed to execute script 'app'`）。
**修复**：
```python
if __name__ == "__main__":
    print("=" * 50)
    print("[车道线异常智能归因系统]")   # 去 emoji
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    app.run(...)
```
检查主脚本（被 import 的模块）里 print 是否含 emoji——本项目中主脚本没有，但 app.py 有。

### 坑2：frozen 下 subprocess 调脚本失效
原代码 `subprocess.run([sys.executable, os.path.join(BASE, "车道线自动复核.py")])`——打包后 `sys.executable` 是 exe 自身，不是 python → 失败。
**修复**：改为直接调用已 import 的函数：
```python
import 车道线自动复核          # ⚠️ from X import (a,b) 不绑定模块名 X！必须显式 import X
...
with contextlib.redirect_stdout(buf):
    车道线自动复核.main()      # 直接调函数，不 spawn 子进程
```

### 坑3：硬编码 BASE 跨机失效（用户实测"新电脑用不了"）
- app.py 里 `BASE = r"C:\Users\黄钦\Desktop\DF资料\ai 车道线分析"` 硬编码 → 新电脑无此路径 → `FileNotFoundError: ...V1.1.6版本测试问题.xlsx` → 首页 500。
- **只改 app.py 不够**：`车道线自动复核.py` 的 `CONFIG["base_dir"]` 也是硬编码（被 app.py import 后，find_video/find_csv 用它的 CONFIG）→ 新电脑上**页面和报告能显示（app.py 自己动态找数据），但视频帧全无**（视频匹配走主脚本 CONFIG 旧路径）。用户实测现象正是"显示了页面和错误报告 但是没有显示视频帧数"。

**修复**（两个文件都要加同样的 `_detect_base_dir()`，且**三个版本**——主脚本/app.py/analyze_pipeline——都要同步改，用户会逐个验收）：
```python
def _detect_base_dir():
    """数据根目录自动探测 (适配任意目录结构, 不固定路径):
    1. 环境变量 LANE_BASE
    2. exe 所在目录下 '数据' 子目录 (分发时数据放 exe 旁)
    3. exe 所在目录本身
    4. 当前工作目录 os.getcwd()        # ← 用户要求"自动检测当前文件夹"
    5. 旧硬编码路径 (本机开发回退)
    """
    def _has_xlsx(d):
        if os.path.exists(os.path.join(d, "V1.1.6版本测试问题.xlsx")):
            return True
        # 任意 .xlsx 也算 (用户可能改了问题表文件名)
        if os.path.isdir(d):
            return any(f.lower().endswith(".xlsx") for f in os.listdir(d))
        return False
    env = os.environ.get("LANE_BASE")
    if env and _has_xlsx(env): return env
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.getcwd()
    for cand in (os.path.join(exe_dir, "数据"), exe_dir, os.getcwd()):
        if _has_xlsx(cand): return cand
    old = r"C:\Users\黄钦\Desktop\DF资料\ai 车道线分析"
    if _has_xlsx(old): return old
    return exe_dir
```
输出目录同样处理：`out_root = os.path.join(exe_dir if frozen else script_dir, "自动复核输出")`。
配套改动（用户最终要求"自动检测当前文件夹数据"）：
- **xlsx 自动发现**：固定名不存在时 `os.listdir(base_dir)` 找任意 `.xlsx`（app.py 与 main 都要加 fallback）。
- **find_csv 递归扫描**：`os.walk(csv_dir)` 找所有 `.csv`；目录不存在时从 `os.path.dirname(csv_dir)`（=BASE）递归。
- **find_video 递归扫描**：date_map 子目录找不到时，`os.walk(video_dir)` 递归扫描任意结构（跳过 date_map 已扫子目录；video_dir 不存在用 dirname 作搜索根）。
- **analyze_pipeline.py**：`--csv`/`--ts` 改可选，不传时自动检测当前目录 CSV + 按文件名时间戳匹配对应视频。
- 验证法：建**平铺目录**（xlsx+CSV+视频无任何子目录结构）→ `cd` 进去不传参数直接跑 → 确认自动找到全部数据。

### 坑4：数据目录结构（视频无帧的第二大原因）
分发结构（exe 自动探测 `数据/`）：
```
车道线归因系统/
├── 车道线归因系统.exe
├── _internal/                      # 必须与 exe 一起分发！只拷 exe 会缺运行库
└── 数据/
    ├── V1.1.6版本测试问题.xlsx
    ├── 同类型CSV_lane_mark_camera_list_1/
    └── 视频/6.16/ 6.17/ 6.18/      # ⚠️ 必须保留日期子目录！
```
**坑**：`Copy-Item "$base\视频\6.16" "$sim\...\视频"` 会把 6.16 目录内容直接变成"视频"（`视频/*.ts` 无子目录），而 find_video 按 `视频/6.16/` 找 → 匹配全失败 → 所有问题无视频帧。
**正确**：先 `New-Item 视频\6.16` 再 `Copy-Item "$base\视频\6.16\*" "$sim\...\视频\6.16\"`。

## 视频匹配窗口（本会话修正）
`find_video` 原窗口 `vstart - 5 <= sec < vstart + 75` 太窄：问题时间点常比视频起始早几十秒（问题 10:58:00、视频 10:58:41 起始，差 41s）→ 匹配不到。
**修复**：放宽 find 窗口到 `vstart - 120 <= sec < vstart + 75`。但抽帧层仍要严格：错误点早于视频起始时 `fidx = (sec - vstart)*25 + 3` 为负 → `if 0 <= fidx` 过滤 → 无截图（正确行为，页面显示无帧）。不要为让截图出现而钳制到 0（会截到视频开头画面，时间错）。

## 测试 exe（生命周期坑）
- 前台 `Start-Process` 等待测试：父 PowerShell 会话结束时 exe 被连带终止 → 误判"进程退出"。
- 用 Python 启动（DETACHED_PROCESS = 0x00000008）：
```python
import subprocess
p = subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008)
```
- **模拟新电脑验证**（必做）：exe+_internal+数据复制到临时目录（不含旧路径）→ 启动 → 验证：
  - `/` HTTP 200
  - `/api/problems` 返回 33 个问题（验证数据目录探测成功）
  - `/problem/2` 截图数 > 0（验证视频读取；问题1 可能 0 张——错误点早于视频录制属数据特性）
- 杀掉：`taskkill /f /im 车道线归因系统.exe`

## 其他
- onedir（51.6MB）vs onefile（283MB）：onefile 启动慢（每次解压）、杀毒软件易误报；onedir 的 `_internal/` 必须随行。推荐 onedir。
- 打包后 `_internal/` 里有 pandas/matplotlib 自带的示例 csv（data_x_x2_x3.csv / msft.csv / Stocks.csv）——无害，不是用户数据，无需处理。
- **`_internal/` 是清理禁区**（用户要求"清理废文件"时实测）：里面的文件（含 IPython/torch/jedi 的 test/temp 命名文件）全是 PyInstaller 打包进 exe 的**运行库**，删除会损坏 exe。清理废文件时**只删 logs/、build/、dist/、__pycache__、spec、临时测试目录**，绝不动 `_internal/` 内部。用户磁盘上的散 CSV/TS/xlsx（如根目录 `V1.1.6.xlsx` 30MB 副本）删不删**先问用户**（可能是早期测试数据）。
- 更新 exe：改 app.py/主脚本 → 删 build/dist → 重打包 → 覆盖 `桌面版exe/车道线归因系统/`。

## 桌面快捷方式（给用户最方便的入口）
```powershell
$WshShell = New-Object -ComObject WScript.Shell
$lnk = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\车道线归因系统.lnk")
$lnk.TargetPath = "<exe绝对路径>"
$lnk.WorkingDirectory = "<exe所在目录>"
$lnk.Save()
```
注意：快捷方式指向 exe 后，exe 的 `_detect_base_dir()` 用 `sys.executable` 所在目录探测数据——快捷方式的 WorkingDirectory 不影响探测（探测用的是 exe 自身路径），数据仍按 exe 旁 `数据/` 找。
