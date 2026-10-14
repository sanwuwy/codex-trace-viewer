# 安装会话轨迹（macOS）

适用对象：用户，以及拥有目标电脑文件和终端权限的 AI 代理。插件版本 0.11.0。项目使用 MIT 许可证；不是 OpenAI 官方插件。

本说明用于本地原生插件安装。禁止将网页聊天的浏览能力视为本机终端权限。没有本机执行权限时，必须提供用户可执行的步骤，禁止声称已安装。

## 1. 取得完整源码

- 本仓库：`https://github.com/sanwuwy/codex-trace-viewer`。使用 `git clone https://github.com/sanwuwy/codex-trace-viewer.git`，或通过 Code → Download ZIP 下载并解压。
- 0.11.0 发布包：`https://github.com/sanwuwy/codex-trace-viewer/releases/tag/v0.11.0`。推荐完整源码包；插件 ZIP 用于已有本地 marketplace，不直接运行源码安装器。
- 已有源码 ZIP：解压到用户选择的持久目录。
- 已有本地目录：核对其中存在 `README.md`、`LICENSE`、`scripts/install_plugin.py`、`plugins/codex-trace-probe/.codex-plugin/plugin.json` 和 `.agents/plugins/marketplace.json`。
- 不知道仓库链接、版本或目录时：要求提供缺失信息；禁止编造 GitHub 地址或执行远程管道脚本。

进入源码目录。后续命令均以该目录为工作目录；路径包含空格时必须加引号。

## 2. 检查目标电脑

```sh
python3 scripts/install_plugin.py --check
```

检查项：

| 条件 | 处理 |
|---|---|
| 系统不是 macOS | 停止；本版本原生插件仅支持 macOS |
| Python 低于 3.9 或不可用 | 停止并报告；安装兼容 Python 后重试 |
| 无可用 Codex CLI | 使用用户确认的实际 CLI 路径，通过 `--codex` 再检查 |
| CLI 缺少所需 `plugin` / `marketplace` 命令 | 停止并报告宿主不兼容；禁止替代为手写宿主配置 |
| 以上检查通过 | 继续安装；不得将环境检查当作真实 Tab 验收 |

`--check` 不构建资产、不安装、不修改源码或目标 Codex 配置；CLI 能力探测会创建并自动清理临时诊断目录。成功 JSON 的 `status` 为 `ready`。此检查可以发现缺失的生成资产；默认安装会从源码构建资产，无需用户先手动构建。

指定 CLI 的语法：

```sh
python3 scripts/install_plugin.py --check --codex '/absolute/path/to/codex'
```

`/absolute/path/to/codex` 必须替换为目标电脑上确认存在的可执行文件，不能照抄示例。显式指定的路径无效时必须修正，禁止悄悄改用另一个 CLI。

## 3. 安装

```sh
python3 scripts/install_plugin.py
```

若第 2 步指定了 CLI，此处使用相同的 `--codex` 参数。

安装器使用运行它的 Python 解释器，检查源码，构建并校验插件资产，生成持久本机安装副本，再调用官方 CLI 注册本地 marketplace、安装插件及检查启用状态。不修改会话 JSONL，不启动 HTTP 服务，不生成 Actions，不自动退出或重启 Codex。

本机副本位于 `CODEX_HOME/trace-viewer-install/` 下；`CODEX_HOME` 未设置或为空时基于当前用户的 `~/.codex`。插件配置记录当前电脑的解释器绝对路径。该配置由本机生成，不能作为通用源码提交或分发。

安装成功 JSON 的 `status` 为 `installed`，包含 `version`、`cli`、`python`、`codex_home`、`marketplace_path`、`installed_path`、`enabled` 和 `requires_ui_verification`。必须记录这些实际返回值；禁止猜测安装缓存位置。必须保留 `marketplace_path` 对应的持久安装目录，后续 CLI 仍可能读取市场源。

非零退出表示失败。检查 stderr JSON 的 `message`；CLI 安装阶段失败时消息包含阶段名称，报告已执行阶段以及失败原因；禁止忽略错误继续宣称成功。部分阶段已执行时，禁止通过删除其他插件或整个 Codex 数据目录“修复”。

## 4. 用户打开与验收

安装器成功后，用户在目标会话的插件入口打开 **“会话轨迹”**。入口位置由宿主管理，不承诺每个桌面版本菜单位置一致。

入口未出现、仍显示旧版，或出现 `unknown MCP server` 时：

1. 保存正在进行的工作。
2. 手动用 `Cmd+Q` 完整退出 Codex。
3. 重新打开 Codex 和原会话，再打开插件。

AI 代理禁止自动退出应用。仅关闭一个窗口不能保证旧 MCP 运行时重建。

验收清单：

- 页面会话标识属于实际打开的会话；禁止根据“最近会话”猜 ID。
- 全部轮次摘要可见，最后一轮详情加载；点击历史轮可以读取该轮详情。
- 没有本地记录时显示等待，不展示其他会话。
- 非 Git 项目无需初始化 Git 或生成 Actions。
- 点击页面右上角“暂停并释放”（“刷新”左侧），按钮变为“恢复轨迹”；页面提示是否确认释放。
- 点击“恢复轨迹”后当前会话重新加载。
- 多会话测试时，每页标识各自正确；暂停一个页面不暂停另一页面。

CLI 成功只能证明安装阶段；用户尚未打开页面时，结果必须标注“已安装，界面验收未验证”。不能通过读取真实消息正文作为安装验收，也不能把合成测试当作用户点击结果。

## 5. 暂停、升级、卸载

| 目标 | 操作 |
|---|---|
| 立即暂停本页并请求释放 | 点击“暂停并释放” |
| 恢复本页 | 点击“恢复轨迹” |
| 升级 | 取得新版本源码，重复第 2–4 步 |
| 卸载 | 使用下方官方 CLI 命令，然后手动重启宿主确认入口移除 |

```sh
codex plugin remove codex-trace-probe@codex-trace-local --json
```

若 `codex` 不在 PATH，使用安装结果中的 `cli` 绝对路径并加引号。不要删除共享 marketplace 配置或其他插件。回退需取得明确的旧版本源码或包，执行旧版本安装流程。

[Boundary Warnings] 原生 Tab 隐藏/关闭通知不是可靠保证。停止请求且释放通知丢失时，后台空闲超过 30 秒后每 5 秒检查回收；持续轮询仍续租。同步 I/O、系统暂停或调度延迟可延后回收。关闭全部页面不保证宿主管理的进程退出；立即释放需求使用明确按钮。首次大文件读取、宿主重启与页面解析均不保证三秒完成。

## AI 代理必须报告

| 状态 | 必须报告 |
|---|---|
| 环境失败 | 具体失败条件和可执行修复步骤，不执行安装 |
| 安装失败 | 实际 message、消息中的失败阶段及已执行阶段，不掩盖失败 |
| 安装成功，未做用户点击 | 版本、CLI/Python、实际市场/安装目录、启用状态、“界面验收未验证”及第 4 步 |
| 用户完成点击验收 | 明确记录实际通过的项目；未验证项目继续标注未验证 |

禁止猜测会话 ID；禁止读取真实消息正文用于安装；禁止修改宿主私有代码、缓存或未公开状态；禁止生成旧 Actions；禁止擅自发布 GitHub；禁止把页面关闭等同进程退出。

参考：[OpenAI 官方插件打包及本地安装](https://developers.openai.com/plugins/build/plugins)、[扩展入口](https://developers.openai.com/plugins/build/extensions)。
