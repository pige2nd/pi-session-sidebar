# pi-session-sidebar

给原生 [Pi](https://pi.dev) 增加可折叠的会话左栏：鼠标切换运行会话和历史对话、置顶、修改标题、右键删除。右侧仍然是原生 Pi，兼容 Starline 和 Tidy。

没有额外 npm 运行依赖、没有独立后台服务，不调用模型。

## 环境要求与安装

- Linux，或 Windows 下的 WSL；目前不支持原生 Windows 和 macOS。
- Pi 1.5.0 或更高版本及其要求的 Node.js；已验证 Pi 1.5.0。
- Python 3.10+，需包含标准库 curses。
- tmux；完整交互已在本地使用 3.4 和 3.5a 验证。
- 支持 UTF-8 和鼠标的终端。开发环境为 Alacritty + WSL。

```sh
pi install git:github.com/pige2nd/pi-session-sidebar
```

已打开的 Pi 请先 `/reload`。Pi 需要运行在 tmux 中，例如先执行 `tmux new -As pi`，再启动 `pi`。

也可以从源码安装：

```sh
git clone https://github.com/pige2nd/pi-session-sidebar.git
cd pi-session-sidebar
pi install .
```

## 打开

已打开的 Pi 先执行一次 `/reload`，然后执行 `/session-sidebar`。

在源码目录也可以执行 `python3 sidebar.py` 或 `./bin/pi-sidebar`。在 tmux 中会新建一个“Pi 会话”窗口；在 tmux 外会连接专用的 `pi-sidebar` session。

Pi 可执行文件依次使用 `PI_SIDEBAR_PI`、存在的 `~/.npm-global/bin/pi` 或 PATH 中的 `pi`。WSL 中若 PATH 同时包含 Windows 版 Pi，建议用 `PI_SIDEBAR_PI` 明确指定 Linux 版。

## 退出规则

**quit 前台 Pi 会结束同一 tmux 服务器中本扩展登记的所有 Pi，包括后台会话，然后关闭左栏、回到原命令行。对话文件保留。**

点击左栏的 `×` 只关闭会话栏、保留 Pi；它与 quit 的行为不同。

## 鼠标操作

- 点击会话：运行中的 Pi 切回原进程；历史对话按原 JSONL 文件在原目录恢复。其他 Pi 继续在后台运行。
- 点击卡片上的 📌 图钉：置顶或取消置顶；置顶后图标高亮，重启后保留。
- 右键会话：打开置顶／取消置顶、修改标题、删除、取消菜单，不切换当前会话。
- 修改标题：输入新标题，Enter 或点击“保存”；Esc 或“取消”放弃修改。Ctrl+A 可重新选中原标题。显示名称持久保存在左栏的 `titles.json`，不会调用模型或改写对话正文。
- 删除历史对话：移入回收目录，并从置顶记录中移除。
- 删除运行中的会话：弹出确认，确认后结束该 Pi 并移入回收目录；取消不影响进程或对话。
- 点击左栏顶部 `‹`：收成窄条；点击窄条的 `>`：展开。
- 点击 `＋ 新会话`：在右侧当前会话的目录新建 Pi。
- 鼠标滚轮：浏览列表。
- 点击顶部 `×`：关闭会话栏，把借来的 Pi 放回原位置；从这里新开的 Pi 保留在后台，下次打开还能切回。

外观借鉴 Tidy：圆角细线、透明底，当前会话以青绿色边框和标题突出，不使用整行反白。每张卡片只显示标题和 📌 图钉，标题最多两行，不再混排目录、后台状态等信息。已置顶与其他会话分别分区，中间保留一行空白。

键盘只在左栏获得焦点时生效：`j/k` 或方向键移动、Enter 切换、空格置顶、`c` 折叠、`q` 关闭。没有设置全局 tmux 快捷键，也没有占用 Starline/Tidy 的按键。

## 与现有配置的关系

右侧仍运行原生 Pi，继续加载全局配置、权限系统、Starline 和 Tidy。正常浏览和修改左栏标题都不会改写对话；只有明确选择删除时才移动原文件。不提供工作流或多代理编排。

历史从 Pi 的 sessions 目录读取，支持 `PI_CODING_AGENT_DIR` / `PI_CODING_AGENT_SESSION_DIR`。现有 Pi 需要 `/reload` 登记自己的准确 session 文件；新启动的 Pi 会自动登记。还没发送过消息的新会话没有已保存文件，发送首条消息后才能置顶。

运行状态保存在 `~/.local/state/pi-session-sidebar/`，按 tmux socket 分开；置顶记录使用同目录的 `pins.json`。后台 Pi 放在专用 `_pi_sidebar_pool` tmux session 中。

0.3.0 修复了折叠附近 `getmouse() returned ERR` 导致左栏退出的问题：读取鼠标记录发生在尺寸同步之前，过期通知直接跳过；对已经退出的窄栏，再执行 `/session-sidebar` 或 `pi-sidebar` 会重新展开并恢复 UI，保留右侧 Pi 进程。

删除的对话保存在 `~/.local/state/pi-session-sidebar/trash/<记录编号>/`，包含原 JSONL 和记录原路径的 `restore.json`。需要恢复时，按其中的 `original` 路径把 JSONL 放回，左栏会重新发现它。删除操作不清空整个历史目录，不结束其他 Pi。

当前版本在本机 Pi 1.5.0、tmux 3.5a、Python 3.13.5 上验证。已有 Pi 跨项目切换无需换前端；原项目目录被删除的历史不能在原目录恢复，会在左栏提示。

## 更新与移除

更新 Git 安装：

```sh
pi update --extensions
```

更新后在 Pi 执行 `/reload`，关闭左栏后重新打开即可载入最新界面。

移除扩展可执行：

```sh
pi remove git:github.com/pige2nd/pi-session-sidebar
```

先用左栏 `×` 正常关闭，再移除包。移除包不会删除对话，也不会自动结束后台 Pi。

## 验证

GitHub CI 在 Python 3.11 / 3.13 上运行索引、标题和鼠标事件单元测试：

```sh
python3 test_sidebar.py IndexTest MouseResizeTest
```

完整鼠标交互测试使用真实 PTY 和独立 tmux socket，请在本机终端运行：

```sh
python3 test_sidebar.py
```

GitHub 托管运行器的合成右键事件未稳定到达 ncurses，因此完整 PTY 测试保留在默认本地测试中。完整交互的验证范围是上述本地 tmux 版本与 Alacritty + WSL 环境。

测试使用独立 tmux socket 和模拟 Pi 进程，验证失效鼠标通知与尺寸变化、已退出窄栏的重新展开、标题换行与改名保存／取消、折叠/展开、历史恢复、置顶持久化和分区间隔、右键菜单、删除历史、运行会话删除的确认和取消、回收目录、会话复用、Pi 自己切换 session 后的恢复、退出后重开、关闭还原和后台存活。另已用原生 Pi 及当前全局扩展验证 Starline 胶囊和 Tidy 右对齐；没有发送模型请求。

退出流程测试还验证：quit 后前后台 Pi 均退出、原目录恢复可输入的 shell、历史文件保留。

## 许可证

[MIT](LICENSE)。外观参考 [pi-tidy](https://github.com/Andy8647/pi-tidy) 的圆角细线与透明底风格。
