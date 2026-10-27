#!/usr/bin/env python3
"""Linux 集成测试：真实运行两个 PTY 终端和 System V 队列，不模拟消息传输。"""
import argparse
import ctypes
import errno
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import unittest

PARSER = argparse.ArgumentParser(description=__doc__)
PARSER.add_argument("binary", nargs="?", default="./chat")
PARSER.add_argument("--log-dir", default="test_logs")
ARGS = None
LIBC = None


class Terminal:
    def __init__(self, *args):
        import pty
        import termios
        self.master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self.data = bytearray()
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(
            [ARGS.binary, *args], stdin=slave, stdout=slave, stderr=slave,
            start_new_session=True, close_fds=True,
        )
        os.close(slave)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            try:
                data = os.read(self.master, 4096)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.data.extend(data)

    @property
    def text(self):
        with self.lock:
            return bytes(self.data).decode("utf-8", errors="replace").replace("\r\n", "\n")

    def wait_for(self, text, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text in self.text:
                return
            if self.proc.poll() is not None:
                self.reader.join(timeout=1)
                if text in self.text:
                    return
                break
            time.sleep(0.01)
        raise AssertionError(f"未等到 {text!r}\n实际终端输出：\n{self.text}")

    def send(self, text):
        self.send_bytes((text + "\n").encode("utf-8"))

    def send_bytes(self, data):
        while data:
            sent = os.write(self.master, data)
            data = data[sent:]

    def wait_exit(self, expected=0):
        code = self.proc.wait(timeout=5)
        self.reader.join(timeout=1)
        if code != expected:
            raise AssertionError(f"退出码 {code}，期望 {expected}\n{self.text}")

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.reader.join(timeout=1)
        os.close(self.master)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.terminals = []
        self.qids = []

    def terminal(self, *args):
        term = Terminal(*args)
        self.terminals.append(term)
        return term

    def start_a(self):
        a = self.terminal("a")
        a.wait_for("[终端 A] 就绪")
        qid = int(re.search(r"QUEUE_ID=(\d+)", a.text).group(1))
        self.qids.append(qid)
        return a, qid

    def pair(self):
        a, qid = self.start_a()
        b = self.terminal("b", str(qid))
        b.wait_for("[终端 B] 就绪")
        return a, b, qid

    def assert_queue_removed(self, qid):
        buffer = ctypes.create_string_buffer(4096)
        ctypes.set_errno(0)
        rc = LIBC.msgctl(qid, 2, buffer)  # Linux IPC_STAT=2
        self.assertEqual(rc, -1, "会话退出后消息队列仍存在")
        self.assertIn(ctypes.get_errno(), (errno.EINVAL, errno.EIDRM))

    def tearDown(self):
        # 仅终止本测试启动的进程、释放本测试创建且记录的内核队列。
        # 不调用 ipcrm -a，也不删除任何文件。
        for index, term in enumerate(self.terminals):
            term.close()
            (ARGS.log_dir / f"{self._testMethodName}_terminal_{index + 1}.log").write_text(
                term.text, encoding="utf-8"
            )
        for qid in self.qids:
            LIBC.msgctl(qid, 0, None)  # Linux IPC_RMID=0；已释放则无操作。

    def test_01_bidirectional_chinese(self):
        a, b, _ = self.pair()
        a.send("你好，鲁班猫1！")
        b.wait_for("[对方] 你好，鲁班猫1！")
        b.send("收到，双向通信正常。")
        a.wait_for("[对方] 收到，双向通信正常。")
        self.assertNotIn("[对方] 你好，鲁班猫1！", a.text)
        self.assertNotIn("[对方] 收到，双向通信正常。", b.text)

    def test_02_message_before_b_joins(self):
        a, qid = self.start_a()
        a.send("queued-before-join")
        a.wait_for("[已发送] queued-before-join")
        b = self.terminal("b", str(qid))
        b.wait_for("[对方] queued-before-join")

    def test_03_simultaneous_ordered_messages(self):
        a, b, _ = self.pair()
        def burst(term, prefix):
            for i in range(20):
                term.send(f"{prefix}-{i:02d}")
        workers = [threading.Thread(target=burst, args=(a, "A")),
                   threading.Thread(target=burst, args=(b, "B"))]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        a.wait_for("[对方] B-19")
        b.wait_for("[对方] A-19")
        for term, prefix in ((a, "B"), (b, "A")):
            received = re.findall(r"\[对方\] " + prefix + r"-(\d{2})", term.text)
            self.assertEqual(received, [f"{i:02d}" for i in range(20)])

    def test_04_long_line_rejected_and_recovers(self):
        a, b, _ = self.pair()
        a.send("X" * 1025)
        a.wait_for("[未发送] 此行含 NUL 或超过 1024 字节")
        a.send("after-long-line")
        b.wait_for("[对方] after-long-line")
        self.assertNotIn("[对方] X", b.text)

    def test_05_exact_byte_limit(self):
        a, b, _ = self.pair()
        payload = "M" * 1024
        a.send(payload)
        b.wait_for("[对方] " + payload + "\n")

    def test_06_empty_line_ignored(self):
        a, b, _ = self.pair()
        a.send("")
        a.send("after-empty-line")
        b.wait_for("[对方] after-empty-line")
        self.assertNotIn("[对方] \n", b.text)

    def test_07_duplicate_b_rejected_without_interrupting(self):
        a, b, qid = self.pair()
        duplicate = self.terminal("b", str(qid))
        duplicate.wait_exit(expected=1)
        duplicate.wait_for("无法加入")
        a.send("still-alive")
        b.wait_for("[对方] still-alive")

    def test_08_quit_a_releases_queue_and_exits_b(self):
        a, b, qid = self.pair()
        a.send("/quit")
        a.wait_exit()
        b.wait_exit()
        self.assert_queue_removed(qid)

    def test_09_quit_b_releases_queue_and_exits_a(self):
        a, b, qid = self.pair()
        b.send("/quit")
        b.wait_exit()
        a.wait_exit()
        self.assert_queue_removed(qid)

    def test_10_sigint(self):
        a, b, qid = self.pair()
        b.proc.send_signal(signal.SIGINT)
        b.wait_exit()
        a.wait_exit()
        self.assert_queue_removed(qid)

    def test_11_eof(self):
        a, b, qid = self.pair()
        a.send_bytes(b"\x04")  # canonical PTY 中，空行 Ctrl+D 产生 EOF。
        a.wait_exit()
        b.wait_exit()
        self.assert_queue_removed(qid)

    def test_12_invalid_arguments(self):
        for args in ((), ("c",), ("a", "1"), ("b",), ("b", "-1"),
                     ("b", "12x"), ("b", "999999999999999999999999999")):
            with self.subTest(args=args):
                term = self.terminal(*args)
                term.wait_exit(expected=1)

    def test_13_full_queue_is_reported_and_input_recovers(self):
        a, qid = self.start_a()
        # B 未加入。发送超过常见系统 msg_qbytes 的量，验证不在 msgsnd 中卡死。
        for _ in range(64):
            a.send("Q" * 1024)
        a.wait_for("[未发送] 队列已满")
        b = self.terminal("b", str(qid))
        b.wait_for("[对方] " + "Q" * 1024)
        # 先确认所有前置行都处理完，再等接收方排空队列，避免恢复消息仍遭遇满队列。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            text = a.text
            if text.count("[已发送]") + text.count("[未发送]") == 64:
                break
            time.sleep(0.01)
        else:
            self.fail("队列满后，发送端未继续处理输入")
        sent = a.text.count("[已发送]")
        deadline = time.monotonic() + 5
        while b.text.count("[对方]") < sent:
            if time.monotonic() >= deadline:
                self.fail("接收方未排空已发送的消息")
            time.sleep(0.01)
        a.send("recovered-after-full")
        b.wait_for("[对方] recovered-after-full")

    def test_14_independent_sessions(self):
        a1, b1, qid1 = self.pair()
        a2, b2, qid2 = self.pair()
        self.assertNotEqual(qid1, qid2)
        a1.send("session-one-only")
        b1.wait_for("[对方] session-one-only")
        a2.send("session-two-only")
        b2.wait_for("[对方] session-two-only")
        self.assertNotIn("session-two-only", b1.text)
        self.assertNotIn("session-one-only", b2.text)
        a1.send("/quit")
        a1.wait_exit()
        b1.wait_exit()
        b2.send("session-two-still-alive")
        a2.wait_for("[对方] session-two-still-alive")


def main():
    global ARGS, LIBC
    ARGS = PARSER.parse_args()
    if not sys.platform.startswith("linux"):
        PARSER.error("集成测试必须在 Linux 上运行；Windows 不提供 System V 消息队列。")
    ARGS.binary = str(Path(ARGS.binary).resolve())
    if not Path(ARGS.binary).is_file():
        PARSER.error("未找到可执行程序，请先运行 make。")
    ARGS.log_dir = Path(ARGS.log_dir) / time.strftime("%Y%m%d-%H%M%S")
    ARGS.log_dir = ARGS.log_dir.with_name(ARGS.log_dir.name + f"-{time.time_ns() % 1000000:06d}")
    ARGS.log_dir.mkdir(parents=True, exist_ok=False)
    LIBC = ctypes.CDLL(None, use_errno=True)
    LIBC.msgctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    LIBC.msgctl.restype = ctypes.c_int
    print(f"终端原始日志：{ARGS.log_dir.resolve()}", flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChatTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
