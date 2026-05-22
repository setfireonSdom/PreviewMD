# PreviewMD

macOS 上的 Markdown 预览工具，拖拽 `.md` 文件即可渲染预览。

## 功能

- **多标签页** — 同时打开多份文档，点击标签切换，鼠标悬停显示关闭按钮
- **GitHub Flavored Markdown** — 表格、任务列表、删除线、引用块等完整支持
- **代码语法高亮** — 自动检测编程语言并高亮
- **LaTeX 数学公式** — 行内公式 `$E=mc^2$`，块级公式 `$$\frac{-b\pm\sqrt{b^2-4ac}}{2a}$$`
- **文件变化自动刷新** — 用编辑器修改 `.md` 后窗口自动更新（需通过 `+` 按钮或命令行打开）
- **自动跟随系统暗色模式** — 不需要手动切换

---

## 方式一：Python 脚本直接运行

### 1. 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.9+ | 推荐 3.13 |
| macOS | 使用系统内置的 WKWebView 渲染 |
| pip | Python 包管理器 |

### 2. 安装依赖

```bash
cd preview_md
pip3 install pywebview watchdog
```

这两个包会自动拉取它们的依赖（pyobjc 系列、bottle 等），总共约 6 个包。

| 包 | 用途 |
|---|---|
| `pywebview` | 创建原生 macOS 窗口，内嵌 WKWebView |
| `watchdog` | 监听文件变化，修改后自动刷新预览 |

### 3. 使用

```bash
# 打开空白窗口，拖拽 .md 文件进去
python3 preview.py

# 直接打开指定文件
python3 preview.py your-file.md

# 打开后还可以继续拖入或点 + 打开更多文件
```

### 4. 窗口操作

```
┌─────────────────────────────────────────────────────┐
│ [README.md ×] [notes.md ×]                    [+]   │ ← 标签栏
├─────────────────────────────────────────────────────┤
│                                                     │
│  # 标题                                            │
│  正文内容...                                        │
│                                                     │
└─────────────────────────────────────────────────────┘
```

| 操作 | 方式 |
|------|------|
| 打开文件 | 拖拽 `.md` 到窗口，或点标签栏 `+` 按钮 |
| 切换文档 | 点击顶部标签 |
| 关闭文档 | 鼠标悬停标签 → 点 `×`（关闭最后一个会回到空窗口） |
| 自动刷新 | 通过 `+` / 命令行打开的文件会被监听，编辑器保存后自动刷新（标签上的绿点会闪黄提示） |

---

## 方式二：打包为独立 .app

打包后得到一个独立的应用程序，**不需要安装 Python**，双击即用。

### 1. 安装打包工具

```bash
pip3 install pyinstaller
```

### 2. 打包

```bash
cd preview_md
bash build.sh
```

打包完成后 `dist/PreviewMD.app` 就是成品，约 30MB。

### 3. 使用 .app

```bash
# 双击运行
open dist/PreviewMD.app

# 命令行打开指定文件
open -a dist/PreviewMD.app your-file.md

# 拖拽 .md 文件到程序图标也可打开
```

如果想放到"应用程序"目录方便以后使用：

```bash
cp -r dist/PreviewMD.app /Applications/
```

然后可以右键任意 `.md` 文件 → 打开方式 → 其他 → 选择 `PreviewMD.app`，勾选"始终以此方式打开"，以后双击 `.md` 就能直接用这个 app 打开。

---

## 方式三：分发给别人使用

### 1. 准备 DMG 安装包

`build.sh` 会自动完成打包 → 签名 → 生成 DMG 全流程：

```bash
cd preview_md
bash build.sh
```

产物：

| 文件 | 用途 |
|------|------|
| `dist/PreviewMD.app` | 应用程序（30MB） |
| `dist/PreviewMD.dmg` | 安装包（~10MB，压缩后） |

### 2. 发送给对方

把 `dist/PreviewMD.dmg` 发给对方（AirDrop / 网盘 / 邮件）。

### 3. 对方如何安装

1. 双击打开 `PreviewMD.dmg`
2. 把 `PreviewMD.app` 拖到 `/Applications` 文件夹
3. **首次打开**：由于没有 Apple Developer 签名，双击可能会被 Gatekeeper 拦截。此时**右键 app → 打开**，在弹出的对话框中点击"打开"即可
4. 之后就正常双击使用了

> **为什么需要右键打开？** `build.sh` 会尝试做 ad-hoc 签名（免费），能减轻 Gatekeeper 的拦截程度。ad-hoc 签名后，macOS 允许用户通过"右键 → 打开"来绕过。如果完全不做签名，新版 macOS 可能根本没有"仍然打开"的按钮。

### 4. 设为 .md 默认打开方式（可选）

对方安装后，右键任意 `.md` 文件 → 打开方式 → 其他 → 选择 `/Applications/PreviewMD.app`，勾选"始终以此方式打开"。

---

## 注意事项

### 拖拽打开 vs 对话框打开

- **拖拽打开**：通过浏览器 FileReader API 读取文件内容，**没有文件路径信息**，因此 **不支持自动刷新**（无法监听磁盘文件变化）。适合快速看一眼。
- **`+` 按钮或命令行打开**：有完整文件路径，支持自动刷新。适合边写边看。

建议：快速预览用拖拽，需要实时刷新用 `+` 打开。

### 编码

文件必须为 UTF-8 编码，否则可能出现乱码。绝大多数编辑器的默认编码就是 UTF-8。

### 仅支持 .md 文件

拖拽到窗口的非 `.md` 文件会被忽略。

### macOS Gatekeeper

首次运行打包好的 `.app` 时，macOS 可能会弹窗提示"无法验证开发者"。到 **系统设置 → 隐私与安全性**，点击"仍然打开"即可。

### 多 Python 环境

如果你电脑上装了多个 Python（系统自带 + Homebrew + miniconda 等），`build.sh` 会**自动检测**哪个 Python 装了 `webview` 和 `watchdog`，优先使用 miniconda 的 Python。检测逻辑：

1. 先尝试 `miniconda3/bin/python3`
2. 再尝试 `which python3`
3. 都没找到依赖则报错退出

如果脚本报 "Could not find a Python with webview and watchdog installed"，请确认在正确的 Python 环境中安装了依赖：

```bash
# 例如用 miniconda
conda activate base
pip3 install pywebview watchdog pyinstaller
bash build.sh
```

---

## 常见问题

### Q: 打包时报 `ERROR: Could not build wheels for pyobjc-core`

**原因**：系统自带的 Python 3.9 尝试从源码编译 `pyobjc-core`，但新版 Clang 启用了 `-Werror`，把一些未初始化变量的警告当成错误。

**解决**：不要用系统 Python，用 miniconda 的 Python（预编译的 wheel 无需编译）：
```bash
# 确保 miniconda 的 pip 在前
which pip3    # 应该输出 /Users/xxx/miniconda3/bin/pip3
pip3 install pywebview watchdog pyinstaller
bash build.sh
```

### Q: 打开 README.md 时 `$...$` 显示奇怪

**原因**：README 中有 `行内 $...$ / 块级 $$...$$` 这种写法，`...` 不是合法的 LaTeX 公式，被 KaTeX 渲染失败。

**解决**：v1.1 已修复 — 增加了 `looksLikeMath` 预检函数，只有包含至少一个数学相关字符（字母、数字、运算符、反斜杠等）的内容才会交给 KaTeX 渲染。纯标点如 `$...$` 不会被误匹配。

### Q: 拖拽文件进去没反应

确认拖入的是 `.md` 后缀的文件。目前只支持 Markdown 文件。

### Q: 文件修改后窗口没自动刷新

检查你打开文件的方式：
- 拖拽打开 → 不支持自动刷新，请改用 `+` 按钮打开
- `+` 按钮打开 → 应该支持。如果仍然不刷新，确认文件确实被保存了（不是仅在编辑器缓存中）
