# agy-session-resume

> 面向 Windows 平台 **Google Antigravity CLI (`agy`)** 的极简会话恢复与模型记忆工具（单文件纯 Python 标准库，零第三方依赖）。

---

## 1. 为什么要做这个项目？（解决的核心痛点）

在使用原生 `agy` CLI 时，存在一个很容易被忽视、但代价很高的设计缺陷：

**`agy` 将“当前使用的模型”保存在全局 `settings.json` 中，而不是与单个历史会话（Thread）绑定。**

这会直接带来两个痛点：

1. **多窗口协作时频繁“模型串台”**
   - 比如你在窗口 A 用 `Orchestrator (Argon)` 或 `Gemini 4 Hydrogen (XHigh)` 推进复杂架构设计，同时在窗口 B 切到 `Gemini 3.8 Flash (High)` 跑一个简单任务。
   - 窗口 B 会把全局 `settings.json` 改写为 `Flash`；当你之后通过 `agy -c` 或 `agy --conversation <id>` 回到窗口 A 的历史会话时，原生 `agy` 会直接沿用全局配置里的 `Flash` 继续对话。
2. **破坏模型上下文缓存（Prompt Cache 失效），造成额外延迟与开销**
   - 长期对话之所以快且省额度，高度依赖服务端针对同一模型和上下文前缀的 **Prompt Cache（上下文缓存）** 命中。
   - 一旦因为全局配置串台导致你用错的模型恢复会话（或发出一轮请求后再手动 `/model` 切回），原模型的上下文缓存链路就会被打断或失效，超长历史上下文被迫重新计费/重算，既拖慢首字响应，又造成不必要的额度浪费。

**`agy-session-resume` 的解法（无需修改原生 `agy.exe`，不污染全局 `settings.json`）：**

- 在执行 `agy -r`、`agy --resume`、`agy -c`、`agy --continue` 或 `agy --conversation <id>` 时，自动以**纯只读模式**解析该会话本地数据库 `gen_metadata` 中记录的最后一次模型与思考等级；
- 通过进程级的 `--settings '{"model":"..."}'` 启动参数把对应模型精准带回当前会话，全程**绝不改写全局 `settings.json`**，从入口处保住模型一致性与缓存命中率。

---

## 2. 核心功能与极简交互

### 1. `agy -r [关键词]` / `agy --resume [关键词]`：极简两列会话选择器

```text
 Conversations (16)  Page 1/1
 > Search: _
─────────────────────────────────────────────────────────────────────────
 ❯ Agy Session Resume Project Review           Orchestrator (Argon)
   Agy会话管家-修复退出回写覆盖与全量验证      Orchestrator (Argon)
   Agy会话管家-历史会话全量盘点与规范预览      Gemini 3.8 Flash (High)
   Agy会话管家-历史会话盘点与整理预览          Gemini 4 Hydrogen (XHigh)
   ...
─────────────────────────────────────────────────────────────────────────
 ↑↓ Move  ←→ Page  Enter Resume  Esc Clear/Quit
```

- **极简两列**：仅展示「Thread 名称」与完整「模型与思考等级」，自适应紧凑版心宽度，0 闪烁刷新，支持实时关键字过滤（匹配标题、摘要、模型名、会话 ID）。
- **与原生 `/resume` 对齐**：默认列出全部有效 CLI 主会话，校验 `conversations/<id>.db` 实体存在以自动过滤幽灵空壳记录，并按 `annotations/*.pbtxt` → `conversation_metadata.json` → `conversation_summaries.db` 优先级读取最新中文标题。
- **自动带回模型**：回车后自动附带该 Thread 上次使用的模型启动 `agy.exe`。

### 2. `agy -c` / `--continue` / `--conversation <id>`：直连恢复模型记忆

- 执行 `agy -c`（或 `agy --continue`）时，自动读取 `~/.gemini/antigravity-cli/cache/last_conversations.json` 定位当前目录上次会话，并自动带回其历史模型；
- 执行 `agy --conversation <id>` 时同样自动补回模型；
- **参数智能合并与安全让位**：
  - 若你在命令行显式传入 `--model` 或在 `--settings` 中指定了 `"model"`，工具尊重你的显式指定，不做覆盖；
  - 若传入 `--settings '{"..."}'` 但未含 `"model"` 键，工具会将历史 `"model"` 无损合并进 JSON；
  - 遇到 `-p` / `--print` / `--prompt` / `-v` / `--version` / `-h` / `--help` 及其他任意子命令与参数时，原样透传给原生 `agy.exe`。

---

## 3. 仓库文件清单

```text
agy-session-resume/
├── README.md                         # 项目说明、安装指南与底层原理文档
├── install.ps1                       # Windows PowerShell 一键幂等安装与自检脚本
├── agy_session_guard.py              # 核心入口与极简选单（单文件纯 Python 标准库）
└── tests/
    └── test_agy_session_guard.py     # 离线沙盒单元测试套件
```

---

## 4. 在另一台 Windows 设备上安装

### 前置条件

- Windows 10 / 11
- 已安装 **Google Antigravity CLI (`agy`)**
- 已安装 **Python 3.10+**（仅使用 Python 标准库，无需 `pip install` 任何第三方包）

---

### 方式 A：让另一台电脑上的 `agy` 自动完成安装（推荐）

在另一台 Windows 电脑打开 `agy`，将下面这段指令直接发给它即可（不预设用户名或盘符，会先核查目标设备实际环境）：

```text
请帮我参考 GitHub 仓库 https://github.com/myhoodiao/agy-session-resume，在这台 Windows 电脑上完整安装并配置 agy-session-resume（Agy 会话恢复与模型记忆工具）。

执行要求：
1. 先核查这台电脑实际的 python.exe（3.10+）、agy.exe 路径、当前 PowerShell 的 $PROFILE 路径以及本地工作/临时目录位置，不要假设或硬编码任何本机绝对路径；
2. 将仓库克隆到本地合适目录（若无固定项目目录可克隆到临时目录，安装后运行脚本独立驻留在 %USERPROFILE%\.local\bin），阅读 README.md 了解安装与运行机制；
3. 运行仓库根目录下的幂等安装脚本：powershell -ExecutionPolicy Bypass -File .\install.ps1；
4. 确认 install.ps1 的 5 个步骤全部通过（环境探测、既有配置备份、同步部署与 SHA-256 校验、离线单元测试、本机只读会话扫描自检），并在完成后汇报本机检测到的有效 CLI 会话数量与更新后的 $PROFILE 路径。
```

---

### 方式 B：PowerShell 一键安装

在 Windows PowerShell 或 PowerShell 7 终端中执行：

```powershell
git clone https://github.com/myhoodiao/agy-session-resume.git
cd agy-session-resume
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` 会自动完成以下 5 个幂等步骤：

1. 自动定位本机可用的 **Python 3.10+**（自动避开 WindowsApps 空壳占位符）与 `agy.exe`；
2. 自动确定备份目录（存在 `D:\Project` 时使用 `D:\Project\agy Backups`，其次 `D:\Agy Backups`，否则 `%USERPROFILE%\Agy Backups`，也可通过 `-BackupDir` 指定），并在覆盖前备份既有的 `agy_session_guard.py` 与 `$PROFILE`；
3. 将 `agy_session_guard.py` 部署到 `%USERPROFILE%\.local\bin\agy_session_guard.py` 并校验 SHA-256 哈希一致，同时幂等更新 PowerShell `$PROFILE` 中的 `function agy`（如存在 PowerShell 7 也会同步写入其 Profile，保留原有其他配置）；
4. 在离线沙盒中运行 `tests/test_agy_session_guard.py` 全部单元测试；
5. 对本机 `~/.gemini/antigravity-cli` 执行一次纯只读扫描自检并输出有效会话总数。

安装完成后，**新开一个 PowerShell 窗口**即可直接运行 `agy -r` 体验。

---

### 方式 C：手动分步安装

1. 复制 `agy_session_guard.py` 到 `%USERPROFILE%\.local\bin\agy_session_guard.py`。
2. 运行离线单元测试确认环境正常：
   ```powershell
   python -B -X utf8 -m unittest discover -s .\tests -v
   ```
3. 在 PowerShell `$PROFILE` 中添加 `function agy` 包装入口（将 `-r`、`--resume`、`-c`、`--continue`、`--conversation` 路由到 `python -X utf8 "$env:USERPROFILE\.local\bin\agy_session_guard.py"`，排除 `-p` / `--print` / `--prompt` 等非交互参数，其余直通原生 `agy.exe`）。

---

### 已有设备如何拉取更新 / 卸载回滚

- **拉取更新**：进入原克隆目录，执行：
  ```powershell
  git pull
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  ```
- **卸载 / 回滚**：从 `$PROFILE` 中删除 `# >>> agy-session-resume >>>` 至 `# <<< agy-session-resume <<<` 区块，并删除 `%USERPROFILE%\.local\bin\agy_session_guard.py` 即可完全恢复原生 `agy` 行为（`install.ps1` 在安装时也会将原 `$PROFILE` 备份在备份目录下）。

---

## 5. 底层技术原理与安全边界

1. **为什么通过外部 CLI 包装而不是 Agy Hook？**
   - 原生 `agy` CLI 交互界面内的 `/resume` 不会触发 `SessionStart` 等 Hook，且 `PreInvocation` Hook 契约不支持动态修改当前会话绑定的模型，因此通过无侵入的 CLI 入口包装是最简单可靠的方案。
2. **严格只读与零全局污染**
   - 读取 `conversation_summaries.db` 与 `conversations/<id>.db` 全部使用 SQLite 只读 URI（`file:...?mode=ro`），绝不写入、修改或删除用户的任何会话数据；
   - 通过进程级 `--settings '{"model":"..."}'` 启动原生 `agy.exe`，全局 `settings.json` 保持原样不变。
3. **跨版本模型字段兼容与回溯**
   - 遍历目标会话 `conversations/<id>.db` 中 `gen_metadata` 表最新 6 条记录，使用内置极简 Protobuf Wire Format 解析器提取显示标签（`f1.21`）与内部 ID（`f1.19`）；
   - 通过实时刷新并缓存的 `agy models` 映射表（缓存位于 `~/.local/share/agy-guard/models_cache.json`，TTL 24 小时，并内置离线兜底表与已知别名映射 `MODEL_LABEL_ALIASES`）进行归一化；若最新一条记录仅有未识别的内部 ID，继续向前回溯至命中已知标准模型为止。
