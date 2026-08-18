---
name: html-visual-report
description: "从车道线感知CSV+视频数据自动生成深色风格HTML可视化分析报告（与《车道线异常智能归因系统》报告同款效果）。支持两种输入：用户指定数据路径（CSV/视频所在目录）或用户上传数据文件；两种检测模式：完整版（YOLOP语义分割+规则五事件链，需下载34MB模型）和轻量版（Hough变换+CSV规则+抽帧，无需模型）；自动检测并配置Python依赖环境（可选模型下载）。每次调用自动输出单个自包含HTML文件。当用户说'生成报告'、'可视化分析'、'分析这批车道线数据'、'把CSV和视频做成报告'、'帮我分析上传的数据'时使用此技能，即使没有明确说'report'。"
metadata:
  tags: [lane-detection, html-report, visualization, csv, video, adas, yolop, hough]
  platforms: [windows, linux, macos]
---

# HTML 可视化报告生成

从车道线感知数据自动生成**深色风格 HTML 可视化报告**（同《车道线异常智能归因系统》报告效果：统计卡 + 趋势条形图 + 折叠问题卡片 + 抽帧 + 双维度校验 + 事件链）。

## 触发条件

用户需要**从数据生成可视化报告**时使用：
- "生成报告" / "可视化分析" / "把这次分析做成网页"
- "分析这批车道线数据" / "我这里有CSV和视频"
- "帮我分析上传的数据" / "生成html报告"

## 两种输入方式

### 方式A：用户指定路径
用户提供 CSV 和视频的目录路径（可能是问题表 xlsx + CSV 目录 + 视频目录，或单独的 CSV + 视频）。

### 方式B：用户上传数据
用户上传 CSV / 视频文件。**处理方式**：把上传文件保存/复制到本地工作目录，然后**与方式A完全相同**的流程处理（同一套分析引擎）。

## 两种检测模式

### 完整版（推荐）：`--mode full`
- YOLOP 语义分割车道线检测（需 `models/yolop-640-640.onnx`，34MB）
- CLAHE 增强 + 单侧 Hough 互补 + 置信度分级（像素<3000 → 复核）
- 双维度校验：CSV 异常 vs YOLOP 实测宽度
- 规则五：丢失事件链（前兆关联 ≤5s）
- 无模型时自动回退 Hough（提示"完整版需要模型，已用轻量模式"）

### 轻量版：`--mode light`
- Hough 变换检测（CLAHE + Canny + HoughLinesP）
- CSV 规则检测（缺失/抖动/宽度/可视范围）
- 视频抽帧 + 清晰度评估
- 无 YOLOP 双维度校验（更快，无模型依赖）

## 环境自动配置

首次在新电脑运行（或导入失败时）：
1. **检测依赖**：`import cv2/numpy/openpyxl/onnxruntime` 逐个检查
2. **自动安装缺失包**：`pip install openpyxl opencv-python numpy onnxruntime`
3. **模型选择**（完整版）：
   - 检查 `models/yolop-640-640.onnx` 是否存在
   - 不存在时**询问用户**：下载模型（34MB，hf-mirror/直连）还是用轻量模式？
   - 下载地址: `https://github.com/huachuan-888/lane-anomaly-system/releases/download/v1.0-models/yolop-640-640.onnx`
4. 输出环境就绪信息

## 核心流程

```
输入数据（路径或上传）
  → 调用 reference_engine.run_engine(base_dir, out_html)   ← 完整引擎，与项目主脚本同款
  → ① 问题表 xlsx → 关键词筛选（车道线/压线/蛇形）
  → ② CSV 定位（按问题日期+时间，10分钟段覆盖）
  → ③ scan_errors 全量错误点扫描（缺失/抖动/宽度/可视范围）
  → ④ 视频定位（按日期子目录 + 文件名时间戳，错误点必须在视频覆盖区）
  → ⑤ 顺序抽帧（b1±1s × 3张，TS视频必须顺序read()）
  → ⑥ 清晰度评估（S = w1L+w2E+w3C+w4B+w5F）
  → ⑦ YOLOP双维度校验（按标签类型分派）
  → ⑧ 规则五事件链 + generate_html 深色报告（单文件自包含）
  → ⑨ 自动打开报告
```

> 引擎模式说明：`generate_report.py --data <根目录>` 即触发完整引擎（默认问题表 = 根目录/V1.1.6版本测试问题.xlsx）。`--mode` 参数传入但引擎自动用 YOLOP（有模型）或 Hough（无模型）。

> ⏱ 运行时长：完整引擎全量分析（20问题/211错误点/322截图）实测约 **2 分钟**，可能超过终端前台 600s 上限（尤其 YOLOP 模式）——**用 Start-Process 后台运行 + 轮询日志/输出文件**，不要前台阻塞：
> ```powershell
> Start-Process python -ArgumentList "脚本","--data","数据根","--out","报告.html","--no-open" `
>   -RedirectStandardOutput run.log -RedirectStandardError run_err.log -NoNewWindow
> # 轮询报告文件出现且 >1MB 即完成
> ```
> 完成标志：日志出现 `[完成] 错误点 N | 截图 M`；stderr 的 h264 解码噪音（`decode_slice_header error`）可忽略。

## 关键规则（严格遵守）

### TS 视频抽帧铁律
- `cap.set(POS_FRAMES)` 对 TS **无效**（会停在开头旧素材）→ **必须顺序 `read()` 逐帧数**
- 帧号公式：`b1帧号 = (b1秒 - 视频起始秒) × 25 + 3帧补偿`
- 视频文件名时间戳 = 起始时刻（`ND02512_HHMMSS_vedio_chn4.ts`）

### 本车道线判定（用户强调：只关注本车道）
- `id=1`（本车道左）、`id=-1`（本车道右）——**只分析这两条**
- 邻车道线（2/-2/3/-3）不参与异常判定

### 异常规则（对齐判断标准.txt）
- 规则一 缺失：id=1/-1 不在槽位
- 规则二 抖动：|C0(t)-C0(t-1)| ≥ 0.2 **连续3帧**才记一次事件（用计数器，勿单帧判）
- 规则三 宽度：总宽=|C0左|+|C0右|；≤3.4m 过窄、≥4.0m 预警、≥4.1m持续5帧降级
- 规则四 可视范围：拆两子条件——**骤降**（≥60→<30 相邻帧）+**持续短**（<30m 超10帧）；
  `range<=0` 的槽位是无效占位（id=99）不算异常
- 规则五 事件链：丢失间隔≤2s聚合；前兆关联≤5s

### 错误类型标注（用户硬性验收标准）
每个问题卡片**必须**显示规则检测出的错误类型标签（如"右侧丢失"）：
- 抽帧时间点 = **问题时间 b_sec**（问题时刻通常在视频内；勿用错误点事件起点，可能早于视频起始导致截图全无）
- 错误类型 = 窗口内所有事件的标签**合并去重**（`merged_labels`）
- `analyze_csv` 的 ±30s 窗口过滤用**事件重叠**判断（`p["sec"] <= hi and p_end >= lo`），
  简单 `lo <= sec <= hi` 会滤掉跨窗口的丢失事件 → 误显示"无CSV异常"（实测踩过）
- 只有窗口内**确实无规则命中**时才显示"问题时间点(无CSV异常)"兜底

### 规则五事件链渲染（报告内）
丢失事件在报告里渲染为 `.chains` 卡片（放在截图之后、清晰度之前）：
- 🔵 有前兆的丢失（`has_warning=True`，丢失前5s内出现过"可视"标签）/ 🔴 突发丢失
- 显示：持续时长（duration_s）+ 恢复时刻（recover_hms，丢失结束后首个有线时刻）
- CSS 类：`.chains`（卡片容器）/ `.chains-title` / `.chain-item` / `.chain-warn`（蓝）/ `.chain-burst`（红）
- 数据传递：`apply_rules` 聚合事件带 `end_sec/duration_s/recover_sec/has_warning`，
  `process_issue` 提取首个丢失事件为 `lost_event` dict，`**pt_chain` 展开进 out_points

### 双维度校验结论（按标签类型判定——用户实测纠正后）
⚠️ 旧版按"只要 CSV 有异常就比宽度"一刀切，会把"右侧丢失"误判成"CSV报过宽但画面实测仅0.53m"（用户实测报错）。
**必须按 CSV 标签类型分派**：
```
丢失标签  → 检查画面是否真的无线：
            两侧都无线 = ✅ 真实异常（丢失属实）
            两侧都有线 = ⚠️ 疑似感知误报（画面明明有线却说丢失）
            单侧有线   = ✅ 真实丢失（画面另一侧确实无线）——用户实测纠正：
                        画面只检到单侧线=另一侧车道线丢失，不是"需人工复核"兜底！
                        返回 `✅ 真实丢失: CSV报丢失, 画面仅X侧有线(Y侧丢失), 画面侧丢失属实`
                        同时在错误点标签追加 `画面Y侧丢失`（画面证据标签），
                        画面检测描述写 `仅检测到X侧车道线(px), Y侧丢失`
过宽/过窄  → 比较画面实测宽度：
            w≥4.0（过宽）或 w≤3.4（过窄）= ✅ 真实异常
            宽度在正常范围(3.4~4.0)      = ⚠️ 疑似感知误报
            检测不完全/像素<3000/w为None = 🔍 需人工复核
可视/抖动  → 画面有线时给"需人工复核"（需多帧/场景判断），不硬比宽度
```
- `det.get("width_m", 0)` 为 None 时**不能直接 f-string 格式化**（`f"{None:.2f}"` 抛 TypeError）——先判 None 再决定是否显示宽度。

## 问题表驱动（--xlsx，推荐）

用户有"问题表"（如 `V1.1.6版本测试问题.xlsx`，30+ 问题）时**必须用问题表驱动**生成报告，而不是按 CSV 文件逐个生成——用户会问"为什么只有17个问题，问题表不是30多个吗"（实测踩过：只按 CSV 去重后 17 个唯一文件，漏掉问题表条目）。

### xlsx 结构（实测）
```
第1-6行: 元信息（测试时间/车辆/路线/版本）— 跳过
第7行:   表头 [编号, 日期, 时间点, 问题归类, 问题现象描述, 环境特性, ...]
第8行起: 数据
```
- 读取：`ws.iter_rows(min_row=8, values_only=True)`，`num` 为 None 的行跳过。
- 时间格式多样需清洗：`10:58:00` / `11.06`（点代冒号） / `17::24`（双冒号） / `10：04`（全角冒号）。
  清洗：`t.replace("：",":").replace(".",":").replace("::",":")` 后正则提取时分秒。
- 关键词筛选（车道线/压线/蛇形等）：注意"锥桶摆放到**车道线**上"这类描述会命中
  "车道线"关键词把非车道线问题（如点刹#26）也选进来——这是预期行为，与旧系统一致。

### 问题 → CSV 定位
CSV 文件名格式 `2026-06-16_10-57-01.bagperception_...csv`：
- 正则必须用**完整格式** `(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})`。
- ⚠️ 用 `(\d{2})-(\d{2})-(\d{2})` 会匹配到日期部分（`2026-06-16` 中的 `06-16`）而非时分秒 → 匹配全失败（实测踩过）。
- 匹配条件：`csv_start <= 问题时间 < csv_start + 600`（CSV 是 10 分钟段）。

### 问题时间 vs 视频起始（重要）
- **问题时间点可能比视频起始早几十秒**（视频从问题发生后才开始录，如问题 10:58:00、视频 10:58:41 起始）。
- 视频匹配窗口放宽为 `[vstart-90, vstart+60]`（find 阶段），但 **extract_frames 内部仍严格检查 [vstart, v_end]**，不在范围不抽帧。
- 抽帧时间点**优先问题时间 b_sec**（问题时刻通常在视频内），再补错误点时刻。

### 状态标记（用户要求"有视频和csv数据你都标注在问题旁边"）
每个问题卡片 summary 显示 `CSV✅/❌ 视频✅/❌`：
```html
<span class="pdata">CSV{'✅' if p.get('csv_ok') else '❌'} 视频{'✅' if p.get('video_ok') else '❌'}</span>
```
`video_ok` = 有匹配视频且至少抽到 1 张帧；样式类 `.pdata` 需在 CSS 中定义
（绿色胶囊，`color:var(--green);background:rgba(34,197,94,.12)`）。

### 场景/环境识别（用户要求：天气 + 道路环境）
每个问题卡片必须带场景标签（用户明确要求"加入天气识别和道路环境识别 隧道 弯道 合流道等"）：
- **天气**：从问题表"环境特性"列（row[5]）读取 → 场景标签 `天气:晴` / `天气:阴` / `天气:小雨`
- **道路环境**：从问题描述文本 `extract_scene(desc)` 关键词匹配：
  `隧道`（隧道）、`弯道`（弯道/左弯/右弯/急弯）、`分合流`（合流/交汇口/分叉）、
  `直道`、`鱼骨线`、`坡道`、`匝道`、`换道`（换道/变道/拨杆）、`进隧道`（入隧道/隧道口）、`出隧道`
- scene 数组 = `extract_scene(desc) + ["天气:" + env]`，渲染为 `<span class="sc">` 标签
- 注意关键词顺序：`进隧道`/`出隧道` 要在 `隧道` 之后匹配，避免被"隧道"先吞掉；"坡" 会命中"下坡/上坡"

## 报告格式（必须与现有报告同款）

深色主题（`assets/report_style.css` 提供全部样式）：

```
<h1>标题</h1>
<div class="sub">版本/时间/参数</div>
<div class="stats"> 统计卡（问题数/错误点数/截图数）</div>
<h2 class="trend-h">📈 异常趋势分析</h2>
<div class="trend-grid"> 3张条形图
  ├── 按问题编号异常分布
  ├── 异常类型占比（过宽/过窄/丢失/抖动/可视）
  └── 双维度校验结论分布
</div>
<details class="problem-card"> 每个问题（折叠）
  <summary> #编号 时间 类型 场景标签 | N个错误点 </summary>
  <div class="pbody">
    <div class="pdesc">描述</div>
    <div class="ev-count">A=时间 ±30s 检出 N 个错误点</div>
    <div class="event"> 每个错误点
      b1 ⏱时间 [标签] 
      3张抽帧（b1-1s/b1/b1+1s）+ 清晰度 + 双维度校验
    </div>
  </div>
</details>
```

**图片必须 base64 内嵌**（`data:image/jpeg;base64,...`），报告是**单文件自包含**，离线可打开。

## 技能备份与恢复

- **GitHub 备份**：本技能完整目录备份在 `huachuan-888/lane-anomaly-system` 仓库的 `skills/html-visual-report/`（README 有专门章节说明）。
- **恢复**：`git clone` 仓库后把 `skills/html-visual-report/` 复制到 `<hermes-home>/skills/` 即可。
- 运行依赖的 YOLOP 模型不在技能内（34MB），从仓库 Release `v1.0-models` 下载到 `models/yolop-640-640.onnx`。

## 脚本

- `scripts/reference_engine.py` — **完整分析引擎**（复用《车道线异常智能归因系统》验证过的成熟版，勿重写）。提供 `run_engine(base_dir, out_html=None, window=None, max_points=None)` → 返回 `(html_path, total_points, total_frames)`。全流程：问题表 → 筛选 → CSV定位 → 错误点扫描（scan_errors）→ 视频定位 → 抽帧 → YOLOP双维度 → 规则五 → generate_html/generate_md。实测产出：211 错误点 / 322 截图 / 23 问题卡片 / 109 双维度结论。
- `scripts/generate_report.py` — **薄入口**（参数解析：--data/--xlsx/--out/--mode/--no-open），内部 `import reference_engine` 并调用 `run_engine()`。`--mode` 实际由引擎的 YOLOP/无模型回退决定。
- `scripts/setup_env.py` — 环境检查/安装/模型下载（用户选择）

## 📦 桌面版 exe 打包（PyInstaller，本会话实测）

把 Flask Web 系统打包成可分发 exe（双击运行、无需装 Python）时，有 4 个必踩坑（全流程见 `references/exe-packaging-pyinstaller.md`）：

1. **GBK 控制台崩溃**：exe 控制台是 GBK，`print("🚗...")` emoji → `UnicodeEncodeError` 直接退出。启动打印去 emoji + `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。
2. **frozen 下 subprocess 调脚本失效**：`subprocess.run([sys.executable, "车道线自动复核.py"])` 在打包后 sys.executable 是 exe 自身 → 改为**直接调用已 import 的函数**（`import 模块名` 后再 `模块名.main()`；注意 `from X import ...` 不绑定模块名，需显式 `import X`）。
3. **硬编码 BASE 跨机失效**（用户实测"新电脑用不了"）：app.py **和 import 的主脚本 `车道线自动复核.py` 的 CONFIG["base_dir"] 都要改成 `_detect_base_dir()`**（LANE_BASE 环境变量 → exe 旁 `数据/` 子目录 → exe 目录 → 旧路径回退）。只改 app.py 不够——页面/报告能显示但**视频帧全无**（find_video 走主脚本 CONFIG 旧路径）。
4. **数据目录结构**：分发时数据放 exe 旁 `数据/`：`V1.1.6版本测试问题.xlsx` + `同类型CSV_lane_mark_camera_list_1/` + `视频/6.16|6.17|6.18/`（**视频必须保留日期子目录**！`Copy-Item "视频\6.16" "视频"` 会把子目录拍平 → find_video 找不到 → 无视频帧）。

打包命令（onedir，`_internal/` 必须与 exe 一起分发）：
```powershell
python -m PyInstaller --noconfirm --onedir --name "车道线归因系统" `
  --add-data "templates;templates" --paths "<工具目录>" `
  --hidden-import openpyxl --collect-submodules openpyxl app.py
```
测试 exe 时**不要用 Start-Process 前台等待**（父进程退出会带走 exe）——用 Python `subprocess.Popen(..., creationflags=0x00000008)`（DETACHED_PROCESS）。模拟新电脑验证：exe+数据复制到临时目录 → 起服务 → 查 `/api/problems` 数量 + `/problem/N` 截图数 > 0。

**清理禁区**：`_internal/` 是 exe 运行库，里面看似 test/temp 的文件**绝不能删**（删了 exe 损坏）。用户要求"清理废文件"时只删 logs/build/dist/spec/__pycache__/临时测试目录；用户磁盘上的散 CSV/TS/xlsx 是否删先问用户。桌面快捷方式用 `WScript.Shell.CreateShortcut`（详见 reference 末尾）。

## 🔍 数据路径自动发现（三个版本通用——用户明确要求"不固定文件路径"）

**用户最终要求（2026-08 实测）：三个工具（车道线自动复核.py / app.py+exe / analyze_pipeline.py）都必须自动检测当前文件夹里的 xlsx/CSV/视频，不要固定路径。** 实现模式：

### `_detect_base_dir()` 完整版（含当前工作目录 + 任意 xlsx 检测）
```python
def _detect_base_dir():
    """数据根目录自动探测 (适配任意目录结构, 不固定路径):
    1. 环境变量 LANE_BASE
    2. exe 所在目录下 '数据' 子目录 (exe打包分发)
    3. exe 所在目录本身
    4. 当前工作目录 os.getcwd()          ← 用户要求"自动检测当前文件夹"
    5. 旧硬编码路径 (本机开发回退)
    """
    def _has_xlsx(d):
        if os.path.exists(os.path.join(d, "V1.1.6版本测试问题.xlsx")):
            return True
        # 自动检测: 目录下有任何 .xlsx 也算 (用户可能有改名的问题表)
        if os.path.isdir(d):
            return any(f.lower().endswith(".xlsx") for f in os.listdir(d))
        return False
    env = os.environ.get("LANE_BASE")
    if env and _has_xlsx(env): return env
    exe_dir = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else os.getcwd()
    for cand in (os.path.join(exe_dir, "数据"), exe_dir, os.getcwd()):
        if _has_xlsx(cand): return cand
    # 旧硬编码路径 (本机开发环境, 部署到新电脑时自动跳过)
    old = os.environ.get("LANE_DEV_BASE") or r"C:\Users\dev\Desktop\DF资料\ai 车道线分析"
    if _has_xlsx(old): return old
    return exe_dir
```

### xlsx 自动发现（固定名缺失时）
`V1.1.6版本测试问题.xlsx` 不存在时，`os.listdir(base_dir)` 找任意 `.xlsx`（app.py 和 main 都要加 fallback）。

### find_csv / find_video 递归扫描（不依赖固定子目录名）
- **find_csv**：`os.walk(csv_dir)` 递归找所有 `.csv`；`csv_dir` 目录不存在时从 `os.path.dirname(csv_dir)`（=BASE）递归。
- **find_video**：先按 date_map 子目录（`视频/6.16/`）找；找不到再 `os.walk(video_dir)` 递归扫描任意目录结构的视频（跳过 date_map 已扫过的子目录避免重复）。`video_dir` 不存在时用 `os.path.dirname(video_dir)` 作搜索根。
- 效果：**xlsx + CSV + 视频放同一文件夹（任意结构、任意路径），cd 进去直接跑，程序自动找到全部数据**。实测平铺目录（无 `同类型CSV.../`、无 `视频/6.16/` 子目录）验证通过。

### analyze_pipeline.py 自动检测
- `--csv` 改为可选：不传时在 `(os.getcwd(), 脚本目录)` 找第一个 `.csv`。
- `--ts` 改为可选：不传时从 CSV 文件名提取 HHMMSS，递归找文件名 `_HHMMSS_` 且 `HHMMSS >= CSV起始` 的 TS（同小时）作为对应视频。

> ⚠️ 三处同步修改（主脚本 CONFIG / app.py / analyze_pipeline.py），只改一处会在其他入口失效——用户逐个版本验收。

## ⚠️ 头号工作流教训（用户实测纠正，优先级高于一切）

**用户说"效果要和昨天的版本一样"时，直接复用已验证的成熟引擎，绝不从零重写分析逻辑。**

实测经过：skill 初版用简化规则重新实现了扫描/抽帧/校验，报告只有 18 问题卡片 / 27 截图 / 双维度结论还误判（"CSV报过宽但画面实测仅0.53m"），用户反馈"没招了 你现在的效果还没有昨天给我生成的版本好"。**根因不是参数，而是重写的简化逻辑丢失了成熟版的丰富度**（聚合、每类型最严重标签、25点/问题上限、完整校验）。

正确做法：
1. 找到已验证的引擎（本技能 = `reference_engine.py`，即项目里的 `车道线自动复核.py`）
2. 只做**参数化包装**（加 --out/--data 参数），不动核心分析
3. 生成后用**量化对比**确认效果一致（问题卡片/截图/双维度结论数应完全相同）



## 依赖

- Python 3.8+
- openpyxl, opencv-python, numpy（必需）
- onnxruntime（完整版必需）
- 模型 yolop-640-640.onnx（完整版可选，无则回退）

## Pitfalls

1. **cv2 必须在函数内 import**（脚本被 import 时顶层 import cv2 可能静默失败）
2. **TS 视频 cap.set 无效** → 顺序 read()
3. **中文路径**：cv2.imwrite 静默失败 → 用 imencode 写 bytes
4. **Hough 在合流口/暗光场景不可靠**（检测到护栏/导流区线而非本车道线）→ 完整版用 YOLOP
5. **同一秒标签聚合**：每类型保留最严重值，禁用字符串比较（Unicode排序坑）
6. **水印时间不可信**（开头含旧素材帧）→ 以 CSV 时间为准
7. **CSV 表头多 timestamp 列坑**（实测踩到）：ROSbag 导出 CSV 常有 `timestamp`（字符串时间 `2026-06-16 10:57:01.164`）和 `msg_head.timestamp`（epoch 毫秒 `1781578619702`）两列。**必须优先选纯 `timestamp` 列**，跳过 `msg_head.timestamp`，否则秒数解析成 494882949 这类错误值，视频匹配全失败。列识别规则：`cl.strip() == "timestamp"` 优先；含 `msg_head.timestamp` 的列直接 skip。
8. **数据起始缓冲假丢失**（实测）：CSV 开头 3 秒感知未就绪，id=99 槽位大量假丢失/可视范围=0。**跳过开头 3 秒**再跑规则，否则误报数百条"丢失"。
9. **丢失事件聚合**（实测）：同一侧连续丢失（间隔≤2s）应合并为**一次丢失事件**（记录 start~end），而不是每秒一条。合并时只保留丢失标签，**丢弃丢失期间的抖动/其他标签**（丢失期间抖动无意义）。原始 598 条丢失 → 聚合成 1 条事件。
10. **视频匹配铁律：错误点必须在视频覆盖范围内 `vstart ≤ sec < vstart+60`**（实测修正）：CSV 是 10 分钟段、视频是 1 分钟段，一个 CSV 对应多个视频。**错误的做法**是放宽匹配窗口到 180s（会选到错误点*之后*的视频，帧号为负→钳制到 0→截到视频开头画面，截图时间全错——用户实测报"截图时间有问题"）。**正确做法**：只匹配 `vstart ≤ target_sec < vstart+60` 的视频（错误点落在该视频覆盖区内），错误点早于视频起始时**不抽帧，显示"📭 无对应视频帧"**（绝不截错帧）。注意：只写 `vstart ≤ sec` 不够——若错误点在视频起始后很久（如 480s 后），帧号会超出视频长度，同样要拒绝。
11. **截图时间标签用 `b_sec + offset`**（实测）：不要从帧号反推 real_sec（`vstart + (fn-3)/25` 再 int() 截断会让 b-1s/b0s/b+1s 三张都显示同一秒）。直接 `real_sec = b_sec + off`，标签如 `b0s 10:57:04`。
12. **CSV 按文件名去重**（实测）：视频CSV对应目录里同一 CSV（10分钟段）在多个视频组文件夹（1分钟段）都有拷贝，`find_csv_in_dir` 会找到 49 个路径但只有 17 个唯一 CSV。按 `os.path.basename` 去重后再分析，否则同一 CSV 被重复处理、问题卡片重复。
13. **TS 帧数垃圾值**（实测）：`cap.get(cv2.CAP_PROP_FRAME_COUNT)` 对 TS 容器返回负数垃圾值（如 -192153584101141），**不能用作帧号钳制上限**。抽帧钳制用顺序 read 实际计数，或只钳制到 0（不下限上限）。
14. **日期对应铁律**（用户实测"日期对应不对"后修复）：视频按日期子文件夹存放（`视频/6.16|6.17|6.18/`），**必须从 CSV 文件名提取日期**（`2026-06-16_...csv` → `6.16`）匹配对应日期子目录，**绝不能只按 HHMMSS 时间匹配**——不同日期存在相同时刻视频，会跨日期误匹配（6.17 CSV 截到 6.16 视频）。`find_video_in_dir` 的跳过根目录逻辑要跳过**父级**而非传入目录本身（否则传子目录返回 0 个视频）。详版见 `references/csv-video-time-alignment.md` 第 8 节。
15. **报告验收硬性项**（用户会逐项核对）：① 每个问题卡片必须有错误类型标签（右侧丢失/左侧丢失/过宽等），不能只显示时间——抽帧用问题时间点、窗口过滤用事件重叠判断（见 references 第10节）；② 状态标记 `CSV✅ 视频✅/❌`；③ 截图时间与问题时间逐秒对应（b0s=问题时刻）；④ 按问题表驱动（--xlsx），不是按 CSV 文件数。
16. **Hough 宽度负值**（实测）：`min(right_xs) - max(left_xs)` 可能为负（Hough 线段中点混杂）→ 宽度算出 -0.98m。修复：左线取**最大** mid_x（靠右边缘）、右线取**最小** mid_x（靠左边缘），且 `right_edge > left_edge` 才计算。YOLOP 分支同样加 `px > 0` 守卫。
17. **宽度 None 时 f-string 崩溃**（实测）：`f"{det.get('width_m', 0):.2f}"` 在 width_m 为 None 时抛 `TypeError: unsupported format string passed to NoneType.__format__`（程序整个退出，报告不生成）。所有宽度格式化前必须判 None。
18. **画面单侧检测 = 另一侧丢失，必须加丢失标签**（用户实测纠正"应该算车道线丢失吧 把车道线丢失标签加上"）：双维度校验里画面只检测到单侧线时，绝不能用"需人工复核"笼统兜底——要①把 `画面X侧丢失` 追加进错误点标签（画面证据），②dual_check 对"CSV报丢失+画面单侧有线"返回 `✅ 真实丢失`，③画面检测描述明确写 `仅检测到X侧车道线(px), Y侧丢失`（原实现只写"仅检测到单侧车道线"，用户认为语义不清）。**追加位置必须在 `labels_html` 生成之前**（循环开头先检测→追加→再生成 et 标签）；若放在后面双维度校验块里追加，标签只出现在校验说明、不出现在错误类型标签区（用户要求"分析完也要拿出来单独标记出来"，实测：`画面右侧丢失 ×5` 与抖动/过宽标签并列显示）。
19. **"展开全部错误点"未显示全部 = 抽帧上限 `pt_i < 10`**（用户实测"展开全部错误点 未显示全部错误点"）：Web 系统 app.py 的 `analyze_problem` 里 `if pt_i < 10` 只给前 10 个错误点抽帧——problem.html 的 `{% for pt in r.points %}` 和展开按钮都渲染**所有**错误点，但第 10 个之后 `need` 为空 → 全部显示"📭 无视频帧"，看起来像没展开。修复：上限提到 **25**（对齐 `max_points`），展开后基本全部有画面（实测 23 点问题 22/23 有截图）。代价：首次加载明显变慢（23 点 × 3 帧 + YOLOP ≈ 120s），靠 `_video_cache`（同一视频只读一次帧）缓解；若用户嫌慢可调回 15 或改懒加载。
20. **exe 测试进程生命周期**（实测）：用 `Start-Process` 或 `cmd /c start` 启动 exe 后，**父 PowerShell 会话结束会把 exe 连带终止**（进程数变 0、端口连不上），容易误判"exe 崩了"。正确测试方式：Python `subprocess.Popen([exe], creationflags=0x00000008)`（DETACHED_PROCESS）启动，完成后 `taskkill /f /im 车道线归因系统.exe`。exe 本身是好的（日志里能看到 `GET / 200`），只是生命周期问题。
21. **"展开全部错误点"按钮没反应 = JS `display=''` 回退 CSS none**（用户实测"展开也没有 你重新处理一下"）：problem.html 的 `toggleAll()` 若写成 `el.style.display = allExpanded ? '' : 'none'`，展开分支设 `''` 是**清除内联样式**，元素会回退到 CSS 规则 `.ev-collapsed{display:none}` → 点击毫无反应。修复：展开分支必须**显式设 `'block'`**（`el.style.display = allExpanded ? 'block' : 'none'`）。同文件里"还有 N 个错误点未显示"的文案也要与折叠数（points|length - 3）同步，且模板循环用 `{% for pt in r.points %}`（勿切片 `[:10]`，否则第 10 个之后的错误点根本没渲染、永远展开不出来）。
22. **改了源码/模板必须重新打包 exe，并验证 exe 内文件含修改**（用户两次反馈同一 bug 未解决的根因）：PyInstaller `--add-data "templates;templates"` 打包的是**执行打包命令那一刻**的文件快照。修改 app.py / templates / 主脚本后**忘记重打包**（或复制到 `桌面版exe/` 失败——PowerShell 转义报错会让 Copy-Item 静默不执行），exe 里仍是旧版 → 用户反复报"还是不行"。修复流程：①改完源码 → ②重跑 PyInstaller → ③**验证 exe 内文件**：`读取 dist\应用名\_internal\templates\problem.html` 检查是否含新代码（如 `'block'`）；用 Python 读写验证（PowerShell `$c.Contains(\"'block'\")` 嵌套引号会 ParserError，别用）→ ④用 `shutil.copytree` 复制到 `桌面版exe/`（避免 PowerShell 转义坑）→ ⑤启动 exe 实测页面含新 JS。
23. **交付文档/代码不得出现人名**（用户明确要求"检测一下 不要出现人名"）：对外交付的文档（开发说明/部署文档/README）和代码里，**带教老师姓名→"带教老师"、实习生姓名→"东风商用车技术中心实习生"**；旧路径 `C:\Users\<用户名>\...` → `C:\Users\dev\...` + `LANE_DEV_BASE` 环境变量覆盖（本机开发可设环境变量找回）。GitHub 链接是功能性部署地址**保留**。修改代码路径时用 patch 精确锚点（曾把 `old = ...` 赋值误塞进 for 循环破坏逻辑，改后必须 ast.parse 语法验证 + 复查）。交付物清单：`系统开发说明.md/.docx` + `系统部署文档.md/.docx`（三种部署形态 A.exe/B.py/C.skill 分步流程）放 `数据与工具\`。
24. **PowerShell Add-Content 写中文文件默认 GBK → 污染 UTF-8（README 乱码根因）**：给 UTF-8 项目文件追加内容**绝不用 PowerShell Add-Content**（实测 README 变成 UTF-8+GBK 混合，GitHub 显示乱码）。一律用 Python `open(path, 'w', encoding='utf-8', newline='\n')`。修复混合编码：从 git 取上个正确 commit 的 blob（`git cat-file blob $(git rev-parse <commit>:README.md)`，**避开 PowerShell 重定向**——它会转成 UTF-16）→ Python decode 验证 → 重写。验证全仓库：`git -c core.quotepath=false ls-files` + 逐文件 `open(f,'rb').read().decode('utf-8')`。Markdown→Word 交付文档转换用 python-docx（样式 'Light Grid Accent 1' 表格 + Consolas 代码块 + 微软雅黑）写成 `md2docx` 脚本复用。

> 📎 详细踩坑实录与验证数据（含真实时间戳例子）：见 `references/csv-video-time-alignment.md`
