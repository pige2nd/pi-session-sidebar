"""隔离 tmux socket；不操作用户的会话或发送模型请求。"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("sidebar", Path(__file__).with_name("sidebar.py"))
sidebar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sidebar)


def fixture(path, cwd, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = [{"type": "session", "id": path.stem, "cwd": str(cwd)},
               {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "首条消息"}]}},
               {"type": "session_info", "name": title}]
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n")


class IndexTest(unittest.TestCase):
    def test_titles_append_partial_tail_and_deleted_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "项目 空格" / "one.jsonl"
            fixture(path, root, "中文标题")
            index = sidebar.Index([root])
            self.assertEqual(index.scan()[0]["title"], "中文标题")
            with path.open("a") as stream:
                stream.write('{"type":"session_info","name":"新标题"}\n{"type":')
            self.assertEqual(index.scan()[0]["title"], "新标题")
            self.assertEqual(index.scan()[0]["cwd"], str(root))
            path.unlink()
            self.assertEqual(index.scan(), [])

    def test_pins_survive_restart_and_ansi_is_not_rendered(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pins.json"
            sidebar.save(path, ["中文 空格.jsonl"])
            self.assertEqual(sidebar.read(path, []), ["中文 空格.jsonl"])
        self.assertNotIn("\x1b", sidebar.clip("a\x1b[31m你好", 30))
        self.assertEqual(sidebar.clip("你好abc", 5), "你好a")

    def test_long_title_uses_two_lines(self):
        lines = sidebar.title_lines("你好，你是什么模型？这是另一段会话标题", 14)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "你好，你是什么")
        self.assertTrue(lines[1].endswith("…"))


class MouseResizeTest(unittest.TestCase):
    def test_read_mouse_before_resize_clears_its_queue(self):
        panel = sidebar.Sidebar.__new__(sidebar.Sidebar)
        panel.workspace = Mock()
        panel.menu = None
        dimensions = [34, 28]
        panel.screen = Mock()
        panel.screen.getmaxyx.side_effect = lambda: tuple(dimensions)
        queue = [(0, 0, 0, 0, sidebar.curses.BUTTON1_PRESSED)]

        def resize(height, width):
            dimensions[:] = [height, width]
            queue.clear()  # 模拟改宽期间鼠标通知已发出、记录却已失效的时序。

        def getmouse():
            if not queue:
                raise sidebar.curses.error("getmouse() returned ERR")
            return queue.pop()

        with patch.object(sidebar.os, "get_terminal_size", return_value=os.terminal_size((3, 34))), \
             patch.object(sidebar.curses, "resizeterm", side_effect=resize), \
             patch.object(sidebar.curses, "getmouse", side_effect=getmouse):
            panel.event(sidebar.curses.KEY_MOUSE)
        panel.workspace.toggle.assert_called_once()

    def test_expired_mouse_notification_does_not_crash(self):
        panel = sidebar.Sidebar.__new__(sidebar.Sidebar)
        panel.screen = Mock()
        panel.screen.getmaxyx.return_value = (34, 28)
        with patch.object(sidebar.os, "get_terminal_size", return_value=os.terminal_size((28, 34))), \
             patch.object(sidebar.curses, "getmouse", side_effect=sidebar.curses.error("getmouse() returned ERR")):
            panel.event(sidebar.curses.KEY_MOUSE)


class TmuxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pi-sidebar-test-")
        cls.root = Path(cls.temp.name)
        cls.oldenv = dict(os.environ)
        cls.socket = "pi-sidebar-test-" + str(os.getpid())
        cls.addClassCleanup(cls.cleanup)
        shim = cls.root / "bin"
        shim.mkdir()
        executable = shutil.which("tmux")
        (shim / "tmux").write_text(f'#!/bin/sh\nexec {executable} -L {cls.socket} -f /dev/null "$@"\n')
        (shim / "tmux").chmod(0o755)
        os.environ.update(PATH=str(shim) + ":" + os.environ["PATH"],
                          XDG_STATE_HOME=str(cls.root / "state"),
                          PI_CODING_AGENT_SESSION_DIR=str(cls.root / "sessions"),
                          TERM="xterm-256color", SHELL="/bin/sh")
        os.environ.pop("TMUX", None)
        os.environ.pop("TMUX_PANE", None)
        cls.cwd = cls.root / "项目 有空格 ' $"
        cls.cwd.mkdir()
        cls.files = {}
        for number in range(22):
            path = cls.root / "sessions" / "project" / f"{number:02}.jsonl"
            fixture(path, cls.cwd, f"会话{number:02}")
            cls.files[number] = path
        fake = cls.root / "fake-pi"
        fake.write_text('''#!/usr/bin/python3
import json,os,pathlib,subprocess,sys
args=sys.argv[1:]
file=args[args.index('--session')+1] if '--session' in args else ''
pane=os.environ['TMUX_PANE']
birth=pathlib.Path('/proc/self/stat').read_text().rsplit(') ',1)[1].split()[19]
def report(file):
 data={'pid':os.getpid(),'birth':birth,'file':file,'cwd':os.getcwd(),'name':'运行'+pathlib.Path(file).stem,'busy':False}
 subprocess.run(['tmux','set-option','-p','-t',pane,'@pi_sidebar',json.dumps(data)],check=True)
 print('FAKE_PI '+pathlib.Path(file).name,flush=True)
report(file)
for line in sys.stdin:
 if line.startswith('NEW:'): report(line.strip()[4:])
 if line.strip()=='EXIT': break
''')
        fake.chmod(0o755)
        os.environ["PI_SIDEBAR_PI"] = str(fake)
        cls.a = sidebar.tmux("new-session", "-d", "-s", "host", "-x", "120", "-y", "34", "-P", "-F", "#{pane_id}",
                             "-c", cls.cwd, f"{fake} --session {cls.files[0]}")
        sidebar.tmux("set-option", "-g", "default-terminal", "tmux-256color")
        sidebar.tmux("set-option", "-g", "pane-base-index", "1")
        sidebar.tmux("bind-key", "-n", "C-g", "display-message", "KEEP_CTRL_G")
        sidebar.tmux("set-option", "-t", "host", "mouse", "off")
        cls.original = sidebar.tmux("display-message", "-p", "-t", cls.a, "#{window_id}")
        cls.workspace = sidebar.Workspace()
        cls.wait(lambda: bool(cls.workspace.panes()))
        cls.workspace.open(cls.a)
        cls.sidebar = cls.workspace.state["sidebar"]
        cls.client, cls.master = pty.fork()
        if cls.client == 0:
            os.execvp("tmux", ["tmux", "attach-session", "-t", "host"])
        fcntl.ioctl(cls.master, termios.TIOCSWINSZ, struct.pack("HHHH", 34, 120, 0, 0))
        cls.wait(lambda: bool(sidebar.tmux("list-clients", "-F", "#{client_tty}")))
        cls.wait(lambda: "全部会话" in cls.capture(cls.sidebar))

    @classmethod
    def cleanup(cls):
        subprocess.run(["tmux", "kill-server"], capture_output=True)
        if hasattr(cls, "master"):
            os.close(cls.master)
            os.waitpid(cls.client, 0)
        os.environ.clear()
        os.environ.update(cls.oldenv)
        cls.temp.cleanup()

    @classmethod
    def drain(cls):
        if hasattr(cls, "master"):
            while select.select([cls.master], [], [], 0)[0]:
                try:
                    os.read(cls.master, 65536)
                except OSError:
                    break

    @classmethod
    def wait(cls, predicate, timeout=7):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            cls.drain()
            if predicate():
                return
            time.sleep(0.04)
        if hasattr(cls, "sidebar"):
            print("SIDEBAR", cls.capture(cls.sidebar))
            print("STATE", sidebar.read(cls.workspace.state_file, {}))
            print("PANES", sidebar.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_width}x#{pane_height} #{pane_dead} #{pane_current_command}"))
            print("CLIENTS", sidebar.tmux("list-clients", "-F", "#{client_tty} #{session_name} #{window_id} #{pane_id} #{client_termname}"))
            print("MOUSE", sidebar.tmux("display-message", "-p", "-t", cls.sidebar, "#{mouse_standard_flag} #{mouse_sgr_flag} #{pane_left} #{pane_top}"))
        raise AssertionError("等待状态变化超时")

    @classmethod
    def capture(cls, pane):
        return subprocess.run(["tmux", "capture-pane", "-p", "-t", pane],
                              capture_output=True, text=True).stdout.rstrip("\n")

    def click(self, x, y, button=0):
        os.write(self.master, f"\x1b[<{button};{x + 1};{y + 1}M".encode())
        time.sleep(0.1)
        self.drain()
        os.write(self.master, f"\x1b[<{button};{x + 1};{y + 1}m".encode())
        time.sleep(0.15)
        self.drain()

    def line(self, text):
        return next(i for i, line in enumerate(self.capture(self.sidebar).splitlines()) if text in line)

    def choose(self, label):
        self.wait(lambda: label in self.capture(self.sidebar))
        self.click(6, self.line(label))

    def test_complete_mouse_lifecycle(self):
        w = self.workspace
        self.assertEqual(w.right(), self.a, "采用原来的 pane，不重新启动 Pi")
        self.assertEqual(sidebar.tmux("show-options", "-g", "-v", "pane-base-index"), "1")
        self.assertIn("KEEP_CTRL_G", sidebar.tmux("list-keys", "-T", "root", "C-g"))
        # 复现用户的已退出窄栏：再次打开必须恢复 UI，并保留右侧 Pi。
        self.click(21, 1)
        self.wait(lambda: sidebar.tmux("display-message", "-p", "-t", self.sidebar, "#{pane_width}") == "3")
        sidebar.tmux("respawn-pane", "-k", "-t", self.sidebar, "false")
        self.wait(lambda: sidebar.tmux("display-message", "-p", "-t", self.sidebar, "#{pane_dead}") == "1")
        w.state = sidebar.read(w.state_file, {})
        w.open()
        self.wait(lambda: sidebar.tmux("display-message", "-p", "-t", self.sidebar, "#{pane_dead}") == "0")
        self.wait(lambda: "全部会话" in self.capture(self.sidebar))
        self.assertEqual(w.right(), self.a)
        # 真正向附着的 tmux 客户端发送鼠标事件，验证转发与命中。
        self.click(21, 1)
        self.wait(lambda: sidebar.tmux("display-message", "-p", "-t", self.sidebar, "#{pane_width}") == "3")
        self.wait(lambda: self.capture(self.sidebar).splitlines()[0].strip() == ">")
        self.click(0, 0)
        self.wait(lambda: sidebar.tmux("display-message", "-p", "-t", self.sidebar, "#{pane_width}") == "28")
        self.wait(lambda: "全部会话" in self.capture(self.sidebar))
        os.write(self.master, b"\x1b[<65;6;7M")
        self.wait(lambda: "运行00" not in self.capture(self.sidebar))
        os.write(self.master, b"\x1b[<64;6;7M")
        self.wait(lambda: "运行00" in self.capture(self.sidebar))
        self.wait(lambda: "会话21" in self.capture(self.sidebar))
        row = next(i for i, line in enumerate(self.capture(self.sidebar).splitlines()) if "会话21" in line)
        self.click(5, row)
        self.wait(lambda: "FAKE_PI 21.jsonl" in self.capture(w.right()))
        c = w.right()
        self.wait(lambda: any(r["pane"] == c for r in w.panes()))
        sidebar.tmux("select-pane", "-t", self.sidebar)
        self.wait(lambda: "运行21" in self.capture(self.sidebar))
        row = next(i for i, line in enumerate(self.capture(self.sidebar).splitlines()) if "运行21" in line)
        self.click(23, row - 1)
        self.wait(lambda: str(self.files[21]) in sidebar.read(w.pins_file, []))
        self.wait(lambda: "运行21" in "\n".join(self.capture(self.sidebar).splitlines()[6:7]))
        self.assertIn("📌", self.capture(self.sidebar))
        self.assertNotIn("☆", self.capture(self.sidebar))
        self.assertNotIn("后台", self.capture(self.sidebar))
        self.assertNotIn("项目 有空格", self.capture(self.sidebar))
        # 标题可以通过右键修改，不需要切换会话或改写对话文件。
        self.click(6, self.line("会话18"), button=2)
        self.choose("修改标题")
        os.write(self.master, "历史会话新标题\r".encode())
        self.wait(lambda: sidebar.read(w.titles_file, {}).get(str(self.files[18])) == "历史会话新标题")
        self.wait(lambda: "历史会话新标题" in self.capture(self.sidebar))
        # 右键仅打开菜单；历史也能置顶，分组之间有空白。
        self.click(6, self.line("历史会话新标题"), button=2)
        self.choose("置顶  ")
        self.wait(lambda: str(self.files[18]) in sidebar.read(w.pins_file, []))
        self.assertEqual(w.right(), c, "右键和菜单置顶不切换当前会话")
        screen = self.capture(self.sidebar).splitlines()
        divider = next(i for i, line in enumerate(screen) if "其他会话" in line)
        self.assertEqual(screen[divider - 1].strip(), "", "置顶与非置顶之间留一行")
        # 删除置顶历史会清除置顶，并把原始文件放到可恢复目录。
        self.click(6, self.line("历史会话新标题"), button=2)
        self.choose("删除会话")
        self.wait(lambda: not self.files[18].exists())
        self.assertNotIn(str(self.files[18]), sidebar.read(w.pins_file, []))
        self.assertNotIn(str(self.files[18]), sidebar.read(w.titles_file, {}))
        manifests = [sidebar.read(p, {}) for p in (w.pins_file.parent / "trash").glob("*/restore.json")]
        self.assertTrue(any(item.get("original") == str(self.files[18]) for item in manifests))
        self.assertEqual(w.right(), c)
        # 运行会话的删除先确认；取消保留进程及文件。
        self.click(6, self.line("运行21"), button=2)
        self.choose("删除会话")
        self.wait(lambda: "确认删除" in self.capture(self.sidebar))
        self.assertTrue(any(r["pane"] == c for r in w.panes()))
        self.choose("取消")
        self.assertTrue(self.files[21].exists())
        # 同一历史连续选择复用已运行进程。
        for _ in range(2):
            w.state = sidebar.read(w.state_file, {})
            w.activate({"key": str(self.files[21]), "file": str(self.files[21]), "cwd": str(self.cwd)})
        self.assertEqual(len([r for r in w.panes() if r["file"] == str(self.files[21])]), 1)
        # 当前 Pi 自己换了会话，旧历史不能错误地跳到这个进程。
        sidebar.tmux("send-keys", "-t", c, "NEW:" + str(self.files[20]), "Enter")
        self.wait(lambda: any(r["pane"] == c and r["file"] == str(self.files[20]) for r in w.panes()))
        w.activate({"key": str(self.files[21]), "file": str(self.files[21]), "cwd": str(self.cwd)})
        self.wait(lambda: any(r["file"] == str(self.files[21]) for r in w.panes()))
        resumed = w.right()
        self.assertNotEqual(c, resumed)
        w.activate({"key": str(self.files[21]), "file": str(self.files[21]), "cwd": str(self.cwd)})
        self.wait(lambda: any(r["file"] == str(self.files[21]) for r in w.panes()))
        final = w.right()
        # 明确确认后只结束被选中的 Pi，其他运行会话继续。
        self.wait(lambda: "运行20" in self.capture(self.sidebar))
        self.click(6, self.line("运行20"), button=2)
        self.choose("删除会话")
        self.choose("确认删除")
        self.wait(lambda: not self.files[20].exists())
        self.assertFalse(any(r["pane"] == c for r in w.panes()))
        self.assertTrue(any(r["pane"] == self.a for r in w.panes()))
        self.assertTrue(any(r["pane"] == final for r in w.panes()))
        self.click(6, self.line("运行21"), button=2)
        self.choose("修改标题")
        os.write(self.master, "自定义标题\r".encode())
        self.wait(lambda: sidebar.read(w.titles_file, {}).get(str(self.files[21])) == "自定义标题")
        self.wait(lambda: "自定义标题" in self.capture(self.sidebar))
        self.click(6, self.line("自定义标题"), button=2)
        self.choose("修改标题")
        os.write(self.master, "取消这次修改".encode() + b"\x1b")
        self.wait(lambda: "Enter 保存" not in self.capture(self.sidebar))
        self.assertEqual(sidebar.read(w.titles_file, {})[str(self.files[21])], "自定义标题")
        sidebar.tmux("select-pane", "-t", self.sidebar)
        self.click(25, 1)
        self.wait(lambda: sidebar.read(w.state_file, None) == {})
        self.assertEqual(sidebar.tmux("display-message", "-p", "-t", self.a, "#{window_id}"), self.original)
        self.assertEqual(sidebar.tmux("show-options", "-t", "host", "-v", "mouse"), "off")
        self.assertTrue(any(r["pane"] == final for r in w.panes()), "关闭左栏不关闭后台 Pi")
        self.assertEqual(sidebar.read(w.pins_file, []), [str(self.files[21])])
        self.assertEqual(sidebar.tmux("show-options", "-g", "-v", "pane-base-index"), "1")
        w.state = {}
        w.open(self.a)
        type(self).sidebar = w.state["sidebar"]
        self.wait(lambda: "自定义标题" in "\n".join(self.capture(self.sidebar).splitlines()[6:7]))
        w.activate({"key": str(self.files[21]), "file": str(self.files[21]), "cwd": str(self.cwd)})
        self.wait(lambda: "自定义标题" in self.capture(self.sidebar))
        time.sleep(1.1)  # 等待实际 UI 记录前台 Pi 的进程身份。
        sidebar.tmux("send-keys", "-t", final, "EXIT", "Enter")
        self.wait(lambda: sidebar.read(w.state_file, None) == {})
        shell = sidebar.tmux("list-panes", "-t", self.original, "-F", "#{pane_id}").splitlines()[0]
        self.assertEqual(sidebar.tmux("display-message", "-p", "-t", shell, "#{pane_dead}"), "0")
        self.wait(lambda: sidebar.tmux("display-message", "-p", "-t", shell, "#{pane_current_command}") == "sh")
        sidebar.tmux("send-keys", "-t", shell, "printf 'SHELL_RETURN_OK\\n'", "Enter")
        self.wait(lambda: "SHELL_RETURN_OK" in self.capture(shell))
        self.assertEqual(sidebar.tmux("display-message", "-p", "-t", shell, "#{pane_current_path}"), str(self.cwd))
        self.assertEqual(w.panes(), [], "quit 时前后台 Pi 全部退出")
        self.assertTrue(self.files[0].exists())
        self.assertTrue(self.files[21].exists(), "quit 不删除历史对话")
        print("PASS 折叠/展开、历史恢复、置顶分区、右键置顶/删除、运行删除确认/取消、回收目录、进程复用与关闭还原")


if __name__ == "__main__":
    unittest.main(verbosity=2)
