#!/usr/bin/env python3
"""Pi 原生终端左栏。仅依赖 Python 标准库和 tmux；不调用模型。"""
import argparse
import curses
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unicodedata

HERE = Path(__file__).resolve().parent
POOL = "_pi_sidebar_pool"
OPTION = "@pi_sidebar"


def tmux(*args, check=True):
    result = subprocess.run(["tmux", *map(str, args)], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf8")
    temp.replace(path)


def read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return default


def canonical(path):
    return str(Path(path).expanduser().resolve())


def process_birth(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class Index:
    def __init__(self, roots):
        self.roots = roots
        self.cache = {}

    def scan(self):
        files = {p.resolve() for root in self.roots for p in root.glob("**/*.jsonl")
                 if "archived" not in p.relative_to(root).parts}
        self.cache = {k: v for k, v in self.cache.items() if k in files}
        for path in files:
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if path in self.cache and self.cache[path][0] == stamp:
                    continue
                header, prompt, name = None, "", ""
                with path.open("rb") as stream:
                    for line in stream:
                        # 大段工具输出和 Thinking 不做 JSON 解析。
                        if header and prompt and b'"session_info"' not in line:
                            continue
                        try:
                            item = json.loads(line)
                        except (ValueError, UnicodeError):
                            continue
                        if item.get("type") == "session":
                            header = item
                        elif item.get("type") == "session_info":
                            name = item.get("name") or ""
                        elif not prompt and item.get("type") == "message":
                            message = item.get("message", {})
                            if message.get("role") == "user":
                                content = message.get("content", "")
                                if isinstance(content, list):
                                    content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
                                prompt = " ".join(str(content).split())[:120] or "图片会话"
                if header and header.get("cwd"):
                    self.cache[path] = (stamp, {"key": str(path), "file": str(path),
                        "cwd": header["cwd"], "title": name or prompt or "未命名会话", "mtime": stat.st_mtime})
            except OSError:
                continue
        return [value[1].copy() for value in self.cache.values()]


class Workspace:
    def __init__(self):
        socket = tmux("display-message", "-p", "#{socket_path}")
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "pi-session-sidebar"
        self.state_file = base / (hashlib.sha256(socket.encode()).hexdigest()[:12] + ".json")
        self.pins_file = base / "pins.json"
        self.titles_file = base / "titles.json"
        self.state = read(self.state_file, {})

    def commit(self):
        save(self.state_file, self.state)

    def exists(self, target):
        return bool(target) and bool(tmux("display-message", "-p", "-t", target, "#{pane_id}", check=False))

    def panes(self):
        fields = "#{pane_id}\t#{window_id}\t#{pane_pid}\t#{pane_current_path}\t#{@pi_sidebar}"
        rows = []
        for line in tmux("list-panes", "-a", "-F", fields).splitlines():
            parts = line.split("\t", 4)
            if len(parts) < 5:
                continue
            pane, window, pid, cwd, raw = parts
            try:
                report = json.loads(raw)
            except ValueError:
                continue
            if process_birth(report.get("pid")) != report.get("birth"):
                continue
            file = canonical(report["file"]) if report.get("file") else None
            rows.append({"key": file or f"pane:{pane}", "file": file, "pane": pane,
                "window": window, "cwd": report.get("cwd") or cwd,
                "title": report.get("name") or "新会话", "busy": report.get("busy", False),
                "pid": report["pid"], "birth": report["birth"],
                "mtime": Path(file).stat().st_mtime if file and Path(file).exists() else 0})
        return rows

    def ensure_pool(self):
        if subprocess.run(["tmux", "has-session", "-t", "=" + POOL], capture_output=True).returncode:
            tmux("new-session", "-d", "-s", POOL, "-n", "停放", "sleep infinity")

    def open(self, source=None):
        if self.exists(self.state.get("sidebar")):
            if self.state.get("collapsed"):
                self.toggle()
            if tmux("display-message", "-p", "-t", self.state["sidebar"], "#{pane_dead}") == "1":
                command = shlex.join([sys.executable, str(HERE / "sidebar.py"), "tui"])
                tmux("respawn-pane", "-k", "-t", self.state["sidebar"], command)
            tmux("select-window", "-t", self.state["window"])
            if source and any(row["pane"] == source for row in self.panes()):
                self.activate_pane(source)
            return
        self.ensure_pool()
        original = tmux("display-message", "-p", "-t", source, "#{window_id}") if source else None
        session = tmux("display-message", "-p", "-t", source, "#{session_id}") if source else None
        args = ["new-window", "-d", "-P", "-F", "#{pane_id}", "-n", "Pi 会话"]
        if session:
            args += ["-t", session + ":"]
        if source:
            args += ["-c", tmux("display-message", "-p", "-t", source, "#{pane_current_path}")]
        right = tmux(*args, "sleep infinity")
        window = tmux("display-message", "-p", "-t", right, "#{window_id}")
        session = tmux("display-message", "-p", "-t", right, "#{session_id}")
        tmux("set-option", "-w", "-t", window, "remain-on-exit", "on")
        command = shlex.join([sys.executable, str(HERE / "sidebar.py"), "tui"])
        left = tmux("split-window", "-d", "-h", "-b", "-l", "28", "-t", right,
                    "-P", "-F", "#{pane_id}", "sleep infinity")
        self.state = {"window": window, "sidebar": left, "blank": right,
                      "original": original, "adopted": {}, "collapsed": False,
                      "session": session, "mouse": tmux("show-options", "-t", session, "mouse")}
        self.commit()
        tmux("set-option", "-t", session, "mouse", "on")
        tmux("respawn-pane", "-k", "-t", left, command)
        if source and any(row["pane"] == source for row in self.panes()):
            self.activate_pane(source)
        tmux("select-window", "-t", window)
        tmux("select-pane", "-t", left)

    def right(self):
        panes = tmux("list-panes", "-t", self.state["window"], "-F", "#{pane_id}").splitlines()
        return next(p for p in panes if p != self.state["sidebar"])

    def activate_pane(self, pane):
        current = self.right()
        if pane != current:
            if pane not in self.state["adopted"]:
                origin = tmux("display-message", "-p", "-t", pane, "#{session_name}")
                if origin != POOL:
                    cwd = tmux("display-message", "-p", "-t", pane, "#{pane_current_path}")
                    placeholder = tmux("new-window", "-d", "-t", POOL + ":", "-c", cwd,
                                       "-P", "-F", "#{pane_id}", "sleep infinity")
                    tmux("swap-pane", "-d", "-s", pane, "-t", placeholder)
                    self.state["adopted"][pane] = placeholder
                    self.commit()
            tmux("swap-pane", "-d", "-s", pane, "-t", current)
        tmux("select-pane", "-t", pane)

    def launch(self, cwd, file=None):
        if not Path(cwd).is_dir():
            raise RuntimeError("原项目目录已不存在，无法在原目录恢复")
        self.ensure_pool()
        local_pi = Path.home() / ".npm-global/bin/pi"
        pi = os.environ.get("PI_SIDEBAR_PI") or (str(local_pi) if local_pi.is_file() else shutil.which("pi"))
        if not pi:
            raise RuntimeError("找不到 pi；请加入 PATH 或设置 PI_SIDEBAR_PI")
        command = [pi, "--extension", str(HERE / "bridge.ts")]
        if file:
            command += ["--session", file]
        args = ["new-window", "-d", "-P", "-F", "#{pane_id}", "-t", POOL + ":", "-c", cwd]
        for name in ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR"):
            if name in os.environ:
                args += ["-e", name + "=" + os.environ[name]]
        pane = tmux(*args, "sleep infinity")
        tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
        tmux("set-option", "-p", "-t", pane, "@pi_sidebar_owned", "1")
        tmux("respawn-pane", "-k", "-t", pane, "-c", cwd, shlex.join(command))
        if file:
            # 扩展启动前也登记路径，避免连续点击时启动两份相同历史。
            self.state.setdefault("starting", {})[pane] = {"file": file, "cwd": cwd}
            self.commit()
        self.activate_pane(pane)
        return pane

    def activate(self, row):
        reports = self.panes()
        known = {r["pane"] for r in reports}
        self.state["starting"] = {p: info for p, info in self.state.get("starting", {}).items()
                                  if p not in known and self.exists(p)
                                  and tmux("display-message", "-p", "-t", p, "#{pane_dead}") == "0"}
        live = next((r for r in reports if r["key"] == row["key"]), None)
        starting = next((pane for pane, info in self.state.get("starting", {}).items()
                         if info["file"] == row["file"] and self.exists(pane)), None)
        if live or starting:
            self.activate_pane(live["pane"] if live else starting)
        else:
            self.launch(row["cwd"], row["file"])

    def new(self):
        current = next((row for row in self.panes() if row["pane"] == self.right()), None)
        self.launch(current["cwd"] if current else os.getcwd())

    def toggle(self):
        self.state["collapsed"] = not self.state["collapsed"]
        tmux("resize-pane", "-t", self.state["sidebar"], "-x", "3" if self.state["collapsed"] else "28")
        self.commit()

    def running(self, row):
        return [live for live in self.panes() if live["key"] == row["key"]]

    def remove(self, row, stop=False):
        live = self.running(row)
        if live and not stop:
            raise RuntimeError("请先确认结束此 Pi")
        starting = any(info["file"] == row.get("file") and self.exists(pane)
                       and tmux("display-message", "-p", "-t", pane, "#{pane_dead}") == "0"
                       for pane, info in self.state.get("starting", {}).items())
        if starting and not live:
            raise RuntimeError("会话正在启动，请稍后再删除")
        for process in live:
            try:
                os.kill(process["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 4
        while any(process_birth(p["pid"]) == p["birth"] for p in live):
            if time.monotonic() > deadline:
                raise RuntimeError("Pi 尚未退出，未删除对话")
            time.sleep(0.05)
        file = Path(row["file"]) if row.get("file") else None
        destination = None
        if file and file.exists():
            folder = self.pins_file.parent / "trash" / str(time.time_ns())
            folder.mkdir(parents=True)
            destination = folder / file.name
            save(folder / "restore.json", {"original": str(file), "title": row["title"],
                                          "deletedAt": int(time.time())})
            shutil.move(str(file), destination)
        pins = set(read(self.pins_file, []))
        pins.discard(row["key"])
        save(self.pins_file, sorted(pins))
        titles = read(self.titles_file, {})
        titles.pop(row["key"], None)
        save(self.titles_file, titles)
        self.state["starting"] = {pane: info for pane, info in self.state.get("starting", {}).items()
                                  if info["file"] != row.get("file")}
        self.commit()
        return destination

    def close(self, to_shell=False):
        self.ensure_pool()
        for pane in tmux("list-panes", "-t", self.state["window"], "-F", "#{pane_id}").splitlines():
            if pane not in (self.state["sidebar"], self.state["blank"]):
                tmux("break-pane", "-d", "-s", pane, "-t", POOL + ":")
        for pane, placeholder in self.state["adopted"].items():
            if to_shell and not self.exists(pane) and self.exists(placeholder):
                cwd = tmux("display-message", "-p", "-t", placeholder, "#{pane_current_path}")
                shell = tmux("show-option", "-gv", "default-shell")
                tmux("respawn-pane", "-k", "-t", placeholder, "-c", cwd, shlex.quote(shell))
            if self.exists(pane) and self.exists(placeholder):
                if to_shell and (any(row["pane"] == pane for row in self.panes()) or
                                 tmux("display-message", "-p", "-t", pane, "#{pane_dead}") == "1"):
                    cwd = tmux("display-message", "-p", "-t", pane, "#{pane_current_path}")
                    shell = tmux("show-option", "-gv", "default-shell")
                    tmux("respawn-pane", "-k", "-t", placeholder, "-c", cwd, shlex.quote(shell))
                else:
                    tmux("swap-pane", "-d", "-s", pane, "-t", placeholder)
                    tmux("kill-pane", "-t", placeholder)
        if self.exists(self.state["blank"]) and tmux("display-message", "-p", "-t", self.state["blank"], "#{window_id}") != self.state["window"]:
            tmux("kill-pane", "-t", self.state["blank"])
        if self.state["mouse"]:
            tmux("set-option", "-t", self.state["session"], "mouse", self.state["mouse"].split()[-1])
        else:
            tmux("set-option", "-u", "-t", self.state["session"], "mouse")
        window, original = self.state["window"], self.state["original"]
        self.state = {}
        self.commit()
        if original:
            tmux("select-window", "-t", original, check=False)
        tmux("kill-window", "-t", window)

    def quit_all(self):
        live = self.panes()
        for process in live:
            try:
                os.kill(process["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 4
        while any(process_birth(p["pid"]) == p["birth"] for p in live):
            if time.monotonic() > deadline:
                raise RuntimeError("部分 Pi 尚未退出，请稍后重试")
            time.sleep(.05)
        self.close(to_shell=True)


def clip(text, width):
    result, used = "", 0
    for char in str(text):
        if unicodedata.category(char).startswith("C"):
            char = " "
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if used + size > width:
            break
        result += char
        used += size
    return result


def shorten(text, width):
    clean = clip(text, 10000)
    return clean if clean == clip(clean, width) else clip(clean, max(0, width - 1)) + "…"


def title_lines(title, width):
    text = " ".join(str(title).split())
    first = clip(text, max(1, width))
    return [first] if first == text else [first, shorten(text[len(first):].lstrip(), max(1, width))]


class Sidebar:
    def __init__(self, screen, workspace):
        self.screen, self.workspace = screen, workspace
        agent = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
        root = Path(os.environ.get("PI_CODING_AGENT_SESSION_DIR", str(agent / "sessions")))
        self.index = Index([root])
        self.pins = set(read(workspace.pins_file, []))
        self.rows, self.offset, self.selected, self.message = [], 0, None, ""
        self.active = None
        self.current_process = None
        self.layout, self.hits, self.menu = [], {}, None
        curses.curs_set(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        screen.timeout(100)
        curses.start_color()
        curses.use_default_colors()
        colors = ((1, 109), (2, 180), (3, 242), (4, 245), (5, 174)) if curses.COLORS >= 256 else (
            (1, curses.COLOR_CYAN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_WHITE),
            (4, curses.COLOR_WHITE), (5, curses.COLOR_RED))
        for number, color in colors:
            curses.init_pair(number, color, -1)

    def refresh(self):
        history = {r["key"]: r for r in self.index.scan()}
        right = self.workspace.right()
        previous = self.current_process
        if previous and previous["pane"] == right and process_birth(previous["pid"]) != previous["birth"]:
            try:
                self.workspace.quit_all()
            except RuntimeError as error:
                self.message = str(error)
            return
        if previous and previous["pane"] != right:
            self.current_process = None
        self.active = None
        for live in self.workspace.panes():
            if live["pane"] == right:
                self.active = live["key"]
                self.current_process = live
            old = history.get(live["key"], {})
            if live["title"] == "新会话" and old:
                live["title"] = old["title"]
            history[live["key"]] = {**old, **live}
        titles = read(self.workspace.titles_file, {})
        for row in history.values():
            row["title"] = titles.get(row["key"], row["title"])
        self.rows = sorted(history.values(), key=lambda r: (
            r["key"] not in self.pins, not bool(r.get("pane")), -r["mtime"], r["key"]))
        self.reflow()

    def reflow(self):
        pinned = [r for r in self.rows if r["key"] in self.pins]
        others = [r for r in self.rows if r["key"] not in self.pins]
        self.layout = []
        for label, rows in (("已置顶", pinned), ("其他会话" if pinned else "全部会话", others)):
            if not rows:
                continue
            if self.layout:
                self.layout.append(("gap", None))
            self.layout.append(("heading", label))
            for row in rows:
                parts = ["top", "title0"]
                if len(title_lines(row["title"], self.screen.getmaxyx()[1] - 7)) > 1:
                    parts.append("title1")
                self.layout.extend((part, row) for part in [*parts, "bottom"])
        self.offset = min(self.offset, max(0, len(self.layout) - self.capacity()))

    def capacity(self):
        return max(1, self.screen.getmaxyx()[0] - 5)

    def put(self, y, x, text, attr=0):
        height, width = self.screen.getmaxyx()
        if y < height and x < width:
            try:
                self.screen.addstr(y, x, clip(text, width - x - 1), attr)
            except curses.error:
                pass

    def draw(self):
        self.reflow()
        self.screen.erase()
        self.hits = {}
        height, width = self.screen.getmaxyx()
        if width <= 5:
            for y, text in ((0, ">"), (2, "+"), (4, "x")):
                self.put(y, 0, text, curses.color_pair(1) | curses.A_BOLD)
        else:
            self.put(1, 2, "会话", curses.color_pair(1) | curses.A_BOLD)
            self.put(1, 7, str(len(self.rows)), curses.color_pair(4))
            self.put(1, width - 7, "‹   ×", curses.color_pair(4))
            self.put(2, 2, "＋ 新会话", curses.color_pair(1))
            for index, (part, row) in enumerate(self.layout[self.offset:self.offset + self.capacity()]):
                y = 4 + index
                if part == "heading":
                    self.put(y, 2, row, curses.color_pair(4))
                elif part != "gap":
                    self.hits[y] = (row, part)
                    current = row["key"] == self.active
                    selected = row["key"] == self.selected
                    border = curses.color_pair(1 if current or selected else 3)
                    if part == "top":
                        self.put(y, 1, "╭" + "─" * (width - 5) + "╮", border)
                        self.put(y, width - 8, " 📌 ",
                                 curses.color_pair(2 if row["key"] in self.pins else 3))
                    elif part.startswith("title"):
                        self.put(y, 1, "│" + " " * (width - 5) + "│", border)
                        lines = title_lines(row["title"], width - 7)
                        self.put(y, 3, lines[int(part[-1])], curses.color_pair(1) | curses.A_BOLD if current else curses.A_NORMAL)
                    else:
                        self.put(y, 1, "╰" + "─" * (width - 5) + "╯", border)
            self.put(height - 1, 1, self.message or "右键 操作  ·  滚轮 浏览", curses.color_pair(4))
        if self.menu:
            self.draw_menu()
        self.screen.refresh()

    def menu_items(self):
        if self.menu["confirm"]:
            return [("delete", "确认删除", True), ("cancel", "取消", True)]
        row = self.menu["row"]
        saved = bool(row.get("file") and Path(row["file"]).exists())
        return [("pin", "取消置顶" if row["key"] in self.pins else "置顶", saved),
                ("rename", "修改标题", True),
                ("remove", "删除会话…", saved or bool(row.get("pane"))), ("cancel", "取消", True)]

    def draw_menu(self):
        if self.menu.get("rename"):
            self.draw_rename()
            return
        menu = self.menu
        height, width = self.screen.getmaxyx()
        size = min(width - 2, 28)
        lines = 9 if menu["confirm"] else 8
        x, y = max(0, min(menu["x"], width - size - 1)), max(0, min(menu["y"], height - lines - 1))
        menu["rect"] = (x, y, size, lines)
        border = curses.color_pair(1)
        for offset in range(lines):
            self.put(y + offset, x, "│" + " " * (size - 2) + "│", border)
        self.put(y, x, "╭" + "─" * (size - 2) + "╮", border)
        self.put(y + lines - 1, x, "╰" + "─" * (size - 2) + "╯", border)
        self.put(y + 1, x + 2, shorten("删除会话？" if menu["confirm"] else menu["row"]["title"], size - 4), curses.A_BOLD)
        if menu["confirm"]:
            self.put(y + 2, x + 2, "结束 Pi，移入回收站", curses.color_pair(4))
            self.put(y + 3, x + 2, "对话仍可恢复", curses.color_pair(4))
        divider = 4 if menu["confirm"] else 2
        self.put(y + divider, x, "├" + "─" * (size - 2) + "┤", border)
        menu["targets"] = {}
        for index, (action, label, enabled) in enumerate(self.menu_items()):
            line = y + divider + 1 + index
            marker = "› " if index == menu["selected"] else "  "
            color = 3 if not enabled else 5 if action in ("remove", "delete") else 1 if index == menu["selected"] else 0
            self.put(line, x + 1, clip(marker + label + " " * size, size - 2), curses.color_pair(color))
            menu["targets"][line] = (action, enabled)

    def menu_action(self, action):
        row = self.menu["row"]
        if action == "pin":
            self.pin(row)
        elif action == "rename":
            self.menu.update(rename=True, text=row["title"], replace=True)
            return
        elif action == "save":
            title = self.menu["text"].strip()
            if not title:
                self.message = "请输入会话标题"
                return
            titles = read(self.workspace.titles_file, {})
            titles[row["key"]] = title
            save(self.workspace.titles_file, titles)
            self.refresh()
        elif action == "remove" and self.workspace.running(row):
            self.menu.update(confirm=True, selected=1)
            return
        elif action in ("remove", "delete"):
            self.workspace.state = read(self.workspace.state_file, self.workspace.state)
            if self.current_process and self.current_process["key"] == row["key"]:
                self.current_process = None  # 菜单删除后保留左栏，便于选择其他对话。
            destination = self.workspace.remove(row, stop=action == "delete")
            self.pins = set(read(self.workspace.pins_file, []))
            self.selected = None
            self.refresh()
            self.message = "已移入回收站" if destination else "已关闭未保存会话"
        self.menu = None
        curses.curs_set(0)

    def draw_rename(self):
        menu = self.menu
        height, width = self.screen.getmaxyx()
        size, lines = min(width - 2, 28), 8
        x, y = max(0, min(menu["x"], width - size - 1)), max(0, min(menu["y"], height - lines - 1))
        menu["rect"] = (x, y, size, lines)
        for offset in range(lines):
            self.put(y + offset, x, "│" + " " * (size - 2) + "│", curses.color_pair(1))
        self.put(y, x, "╭" + "─" * (size - 2) + "╮", curses.color_pair(1))
        self.put(y + 1, x + 2, "修改标题", curses.A_BOLD)
        display = clip(menu["text"][::-1], size - 6)[::-1]
        self.put(y + 2, x + 2, "> " + display, curses.A_REVERSE if menu["replace"] else curses.A_BOLD)
        self.put(y + 3, x + 2, "Enter 保存 · Esc 取消", curses.color_pair(4))
        self.put(y + 4, x, "├" + "─" * (size - 2) + "┤", curses.color_pair(1))
        self.put(y + 5, x + 2, "保存", curses.color_pair(1))
        self.put(y + 6, x + 2, "取消", curses.color_pair(4))
        self.put(y + 7, x, "╰" + "─" * (size - 2) + "╯", curses.color_pair(1))
        menu["targets"] = {y + 5: ("save", True), y + 6: ("cancel", True)}
        cells = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in display)
        self.screen.move(y + 2, min(x + 4 + cells, x + size - 2))
        curses.curs_set(1)

    def menu_event(self, key, mouse=None):
        if self.menu.get("rename") and not mouse:
            if key == "\x1b":
                self.menu_action("cancel")
            elif key == "\n":
                self.menu_action("save")
            elif key == "\x01":
                self.menu["replace"] = True
            elif key == "\x15":
                self.menu.update(text="", replace=False)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.menu.update(text="" if self.menu["replace"] else self.menu["text"][:-1], replace=False)
            elif isinstance(key, str) and key.isprintable():
                self.menu.update(text=("" if self.menu["replace"] else self.menu["text"]) + key, replace=False)
            return
        if mouse:
            x, y, buttons = mouse
            if buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED |
                          curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON1_TRIPLE_CLICKED):
                left, top, width, height = self.menu["rect"]
                if left <= x < left + width and top <= y < top + height:
                    target = self.menu["targets"].get(y)
                    if target and target[1]:
                        self.menu_action(target[0])
                else:
                    self.menu = None
                    curses.curs_set(0)
        elif key in ("\x1b", "q"):
            self.menu = None
        elif key in (curses.KEY_UP, "k", curses.KEY_DOWN, "j"):
            self.menu["selected"] = (self.menu["selected"] + (1 if key in (curses.KEY_DOWN, "j") else -1)) % len(self.menu_items())
        elif key == "\n":
            action, _, enabled = self.menu_items()[self.menu["selected"]]
            if enabled:
                self.menu_action(action)

    def pin(self, row):
        if not row.get("file") or not Path(row["file"]).exists():
            self.message = "发出首条消息后可置顶"
            return
        key = row["key"]
        self.pins.symmetric_difference_update({key})
        save(self.workspace.pins_file, sorted(self.pins))
        self.refresh()

    def event(self, key):
        mouse = None
        if key == curses.KEY_MOUSE:
            try:
                mouse = curses.getmouse()
            except curses.error:
                return  # 改宽附近的过期鼠标通知不应结束整个左栏。
        size = os.get_terminal_size()
        if self.screen.getmaxyx() != (size.lines, size.columns):
            curses.resizeterm(size.lines, size.columns)
        height, width = self.screen.getmaxyx()
        if key == curses.KEY_MOUSE:
            _, x, y, _, buttons = mouse
            if self.menu:
                self.menu_event(key, (x, y, buttons))
                return
            if buttons & (curses.BUTTON4_PRESSED | getattr(curses, "BUTTON5_PRESSED", 0)):
                delta = -1 if buttons & curses.BUTTON4_PRESSED else 1
                self.offset = max(0, min(self.offset + delta * 3, max(0, len(self.layout) - self.capacity())))
            elif buttons & (curses.BUTTON3_PRESSED | curses.BUTTON3_CLICKED |
                            curses.BUTTON3_DOUBLE_CLICKED | curses.BUTTON3_TRIPLE_CLICKED):
                hit = self.hits.get(y)
                if hit:
                    self.selected = hit[0]["key"]
                    self.menu = {"row": hit[0].copy(), "x": x, "y": y, "selected": 0, "confirm": False}
            elif buttons & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED |
                            curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON1_TRIPLE_CLICKED):
                if (width <= 5 and y == 0) or (width > 5 and y == 1 and width - 7 <= x < width - 3):
                    self.workspace.toggle()
                elif (width <= 5 and y == 4) or (width > 5 and y == 1 and x >= width - 3):
                    self.workspace.close()
                elif (width <= 5 and y == 2) or (width > 5 and y == 2):
                    self.workspace.new()
                elif width > 5:
                    hit = self.hits.get(y)
                    if hit:
                        row, part = hit
                        self.selected = row["key"]
                        if part == "top" and x >= width - 8:
                            self.pin(row)
                        else:
                            self.workspace.activate(row)
        elif self.menu:
            self.menu_event(key)
        elif key in ("q", "Q"):
            self.workspace.close()
        elif key in ("c", "C"):
            self.workspace.toggle()
        elif key in (curses.KEY_DOWN, "j", curses.KEY_UP, "k"):
            keys = [r["key"] for r in self.rows]
            current = keys.index(self.selected) if self.selected in keys else 0
            current = max(0, min(len(keys) - 1, current + (1 if key in (curses.KEY_DOWN, "j") else -1)))
            if keys:
                self.selected = keys[current]
                line = next(i for i, (part, row) in enumerate(self.layout) if part == "top" and row["key"] == self.selected)
                end = line + 2 + len(title_lines(self.rows[current]["title"], width - 7))
                self.offset = min(max(self.offset, end - self.capacity()), line)
        elif key in ("\n", " "):
            row = next((r for r in self.rows if r["key"] == self.selected), None)
            if row:
                self.pin(row) if key == " " else self.workspace.activate(row)

    def run(self):
        last, snapshot = 0, None
        while True:
            if time.monotonic() - last > 1:
                self.workspace.state = read(self.workspace.state_file, self.workspace.state)
                self.refresh()
                last = time.monotonic()
            current = (repr(self.rows), self.offset, self.selected, self.active, repr(self.menu), self.message, self.screen.getmaxyx())
            if current != snapshot:
                self.draw()
                snapshot = current
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            try:
                self.message = ""
                self.event(key)
                snapshot = None
            except (RuntimeError, OSError) as error:
                self.message = str(error)


def main():
    parser = argparse.ArgumentParser(description="Pi 可折叠会话左栏")
    parser.add_argument("action", nargs="?", default="open", choices=["open", "tui", "close", "toggle"])
    parser.add_argument("--source")
    args = parser.parse_args()
    if not os.environ.get("TMUX") and args.action == "open":
        if subprocess.run(["tmux", "has-session", "-t", "=pi-sidebar"], capture_output=True).returncode:
            tmux("new-session", "-d", "-s", "pi-sidebar")
        workspace = Workspace()
        source = tmux("list-panes", "-t", "=pi-sidebar", "-F", "#{pane_id}").splitlines()[0]
        workspace.open(source)
        os.execvp("tmux", ["tmux", "attach-session", "-t", "=pi-sidebar"])
    workspace = Workspace()
    if args.action == "open":
        workspace.open(args.source or os.environ.get("TMUX_PANE"))
    elif args.action == "tui":
        curses.wrapper(lambda screen: Sidebar(screen, workspace).run())
    else:
        getattr(workspace, args.action)()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
