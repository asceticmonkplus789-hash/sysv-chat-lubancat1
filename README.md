# System V 消息队列双终端聊天

用 C11 和 POSIX 线程实现同一台 Linux 设备上两个终端的双向文字聊天。两个程序通过 **System V 消息队列** 交换消息，适合作为 Linux 进程间通信课程小项目。

用户已在鲁班猫 1 上完成原生编译、人工双终端聊天和一轮自动集成测试；保存的日志显示 **14 项测试全部通过**。完整结果范围见 [板端测试说明](docs/BOARD_TEST.md)。

## 获取与编译

在 Linux 机器或开发板上操作，依赖 C 编译器、Linux 开发头文件、POSIX 线程库、make；自动测试另需 Python 3，Python 脚本不依赖第三方库。

```sh
git clone https://github.com/asceticmonkplus789-hash/sysv-chat-lubancat1.git
cd sysv-chat-lubancat1
make
```

也可以直接编译：

```sh
cc -std=c11 -O2 -Wall -Wextra -Wpedantic -Werror -pthread chat.c -o chat
```

本项目需在 Linux 上编译运行。Windows 的 VS Code 可以通过 Remote SSH 操作开发板；两端聊天进程都运行在板上。

## 两个终端运行

两个终端须使用同一 Linux 用户，并处于同一 IPC 命名空间。

终端 A：

```sh
./chat a
```

A 创建一个新队列，打印 `QUEUE_ID=数字`，并给出 B 的加入命令。在终端 B 中执行 A **本次实际显示**的命令，例如：

```sh
./chat b 12345
```

`12345` 只是示例，不能直接当作本次编号。合法编号也可以是 0。任一端输入文字并按回车后，另一端显示 `[对方] ...`；本端显示 `[已发送] ...`。输入 `/quit` 并回车，或者按 Ctrl+C，结束双方会话并释放本次队列。

预期交互示例：

```text
A 输入：你好，我是终端A。
B 显示：[对方] 你好，我是终端A。
B 输入：收到，我是终端B。
A 显示：[对方] 收到，我是终端B。
```

## 自动测试

```sh
make test
```

或直接运行并保留日志：

```sh
python3 tests/test_chat.py ./chat > test-console.log 2>&1
echo $?
cat test-console.log
```

退出码 0 且结尾为 `Ran 14 tests`、`OK` 表示本轮全部通过。自动测试使用真实伪终端和真实 System V 队列；每个终端的输出保存在 `test_logs/时间戳/`。覆盖中文双向收发、加入前暂存、每方向 20 条并发消息、输入长度边界、空行、重复 B、正常退出、SIGINT、EOF、错误参数、队列满和会话隔离。

## 实现结构

| 文件 | 作用 |
| --- | --- |
| `chat.c` | 聊天主程序、输入和接收线程、消息队列生命周期 |
| `Makefile` | C11 编译选项、线程链接及测试入口 |
| `tests/test_chat.py` | 14 项 Linux 集成测试 |
| `docs/BOARD_TEST.md` | 已取得的板端证据及未覆盖范围 |
| `SHA256SUMS` | 本仓库交付文件的 SHA-256 清单 |

A 用 `msgget(IPC_PRIVATE, IPC_CREAT | 0600)` 创建独立队列。A→B 使用消息类型 2，B→A 使用类型 1；类型 3 保存一次性加入凭据，避免第二个 B 抢收消息。主线程按行读取输入并用 `msgsnd` 发送，接收线程阻塞在 `msgrcv`，可独立于本端输入显示消息。任一参与端退出时用 `msgctl(IPC_RMID)` 释放队列，并等待接收线程结束。

正文上限为 **1024 个 UTF-8 字节**，不包含末尾 NUL；不是 1024 个汉字。空行忽略，超长行整行拒绝。队列满时明确提示未发送，需由用户重新输入，不会自动重发。`[已发送]` 表示消息已进入内核队列，不代表对方已读。

## 边界

- 这是同机 IPC，不是跨设备网络聊天；无需为聊天程序配置网络端口。
- 普通终端中，对方消息可能插入正在输入的一行的显示位置；程序未实现全屏聊天界面。
- 会话结束时未接收的消息会被丢弃，未按回车的残行不发送；不提供历史消息持久化。
- 本次证据支持功能验证，未测量消息延迟、吞吐量或长期稳定性。SIGKILL、断电等情况下不保证队列清理。
- 一次性加入凭据用于重复 B 检查，并非强安全认证；只应加入 A 实际显示的本程序队列编号。

## 参考

- [msgget(2)](https://man7.org/linux/man-pages/man2/msgget.2.html)
- [msgsnd / msgrcv(2)](https://man7.org/linux/man-pages/man2/msgop.2.html)
- [msgctl(2)](https://man7.org/linux/man-pages/man2/msgctl.2.html)
- [pthread_create(3)](https://man7.org/linux/man-pages/man3/pthread_create.3.html)
