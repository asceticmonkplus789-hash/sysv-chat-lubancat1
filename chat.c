/* 第81页：System V 消息队列实现同机双终端聊天。Linux / C11。 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/msg.h>
#include <unistd.h>

#define MAX_TEXT 1024
#define JOIN_TYPE 3L
#define JOIN_TOKEN "SYSV-CHAT-V1"

/* mtype 必须是正 long；msgsz 只计算 text，不包含 long 或结构体尾部填充。 */
struct message { long mtype; char text[MAX_TEXT + 1]; };
struct context { int qid; long incoming; long outgoing; };
static atomic_bool running = ATOMIC_VAR_INIT(true);
static atomic_bool receive_failed = ATOMIC_VAR_INIT(false);
static volatile sig_atomic_t interrupted = 0;

static void on_signal(int sig) { (void)sig; interrupted = 1; }

static bool queue_gone(int error) { return error == EIDRM || error == EINVAL; }

static void *receive_messages(void *arg)
{
    const struct context *ctx = arg;
    struct message msg;
    for (;;) {
        ssize_t n = msgrcv(ctx->qid, &msg, sizeof msg.text, ctx->incoming, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (queue_gone(errno)) {
                puts("\n[会话结束] 消息队列已释放。");
            } else {
                perror("msgrcv");
                atomic_store(&receive_failed, true);
            }
            atomic_store(&running, false);
            return NULL;
        }
        /* 不信任消息内的终止符；限制可显示的长度。 */
        if (n == 0 || msg.text[n - 1] != '\0') {
            fputs("[提示] 忽略格式不正确的消息。\n", stderr);
            continue;
        }
        printf("[对方] %s\n", msg.text);
    }
}

/* 返回 1 表示退出，0 表示继续，-1 表示失败。队列满时不阻塞输入线程。 */
static int send_line(const struct context *ctx, const char *text, size_t len)
{
    struct message msg;
    if (len == 0) return 0;
    if (strcmp(text, "/quit") == 0) return 1;
    msg.mtype = ctx->outgoing;
    memcpy(msg.text, text, len + 1);
    while (msgsnd(ctx->qid, &msg, len + 1, IPC_NOWAIT) < 0) {
        if (errno == EINTR) {
            if (interrupted) return 1;
            continue;
        }
        if (errno == EAGAIN) {
            fputs("[未发送] 队列已满，请等对方接收后重新输入此行。\n", stderr);
            return 0;
        }
        if (queue_gone(errno)) return 1;
        perror("msgsnd");
        return -1;
    }
    printf("[已发送] %s\n", text);
    return 0;
}

static void usage(const char *name)
{
    fprintf(stderr, "用法：\n  %s a          创建会话，显示队列编号\n"
                    "  %s b 队列编号 加入会话\n"
                    "输入文字后回车发送；/quit、Ctrl+C 或输入结束会结束双方会话。\n",
                    name, name);
}

int main(int argc, char **argv)
{
    struct context ctx;
    struct message token;
    struct sigaction action;
    bool creator;
    pthread_t receiver;
    int result = EXIT_SUCCESS;

    setvbuf(stdout, NULL, _IONBF, 0);
    if (argc < 2 || (strcmp(argv[1], "a") != 0 && strcmp(argv[1], "b") != 0)) {
        usage(argv[0]); return EXIT_FAILURE;
    }
    creator = strcmp(argv[1], "a") == 0;
    if ((creator && argc != 2) || (!creator && argc != 3)) {
        usage(argv[0]); return EXIT_FAILURE;
    }
    memset(&action, 0, sizeof action);
    action.sa_handler = on_signal;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, NULL) < 0 || sigaction(SIGTERM, &action, NULL) < 0) {
        perror("sigaction"); return EXIT_FAILURE;
    }
    if (creator) {
        /* 每次创建独立队列，0600 仅允许同一 Linux 用户访问。 */
        ctx.qid = msgget(IPC_PRIVATE, IPC_CREAT | 0600);
        if (ctx.qid < 0) { perror("msgget"); return EXIT_FAILURE; }
        token.mtype = JOIN_TYPE;
        memcpy(token.text, JOIN_TOKEN, sizeof JOIN_TOKEN);
        if (msgsnd(ctx.qid, &token, sizeof JOIN_TOKEN, IPC_NOWAIT) < 0) {
            perror("创建加入凭据");
            (void)msgctl(ctx.qid, IPC_RMID, NULL);
            return EXIT_FAILURE;
        }
    } else {
        char *end;
        struct msqid_ds info;
        long value;
        errno = 0;
        value = strtol(argv[2], &end, 10);
        if (errno || *argv[2] == '\0' || *end != '\0' || value < 0 || value > INT_MAX) {
            fputs("队列编号必须是非负十进制整数。\n", stderr); return EXIT_FAILURE;
        }
        ctx.qid = (int)value;
        if (msgctl(ctx.qid, IPC_STAT, &info) < 0) {
            perror("无法加入队列"); return EXIT_FAILURE;
        }
        if (info.msg_perm.uid != geteuid() || info.msg_perm.cuid != geteuid() ||
            (info.msg_perm.mode & 0777) != 0600) {
            fputs("拒绝加入：队列所有者或权限不符合本程序约定。\n", stderr);
            return EXIT_FAILURE;
        }
        /* 一次性凭据防止第二个 B 同时抢收消息；加入失败时不释放队列。 */
        ssize_t n = msgrcv(ctx.qid, &token, sizeof token.text, JOIN_TYPE, IPC_NOWAIT);
        if (n != (ssize_t)sizeof JOIN_TOKEN || memcmp(token.text, JOIN_TOKEN, sizeof JOIN_TOKEN) != 0) {
            fputs("无法加入：B 已加入、会话已结束，或编号不属于本程序。\n", stderr);
            return EXIT_FAILURE;
        }
    }
    ctx.incoming = creator ? 1L : 2L;
    ctx.outgoing = creator ? 2L : 1L;
    printf("QUEUE_ID=%d\n", ctx.qid);
    printf("[终端 %c] 就绪。每行最多 %d 字节；输入 /quit 退出。\n", creator ? 'A' : 'B', MAX_TEXT);
    if (creator) printf("请在同机同用户的另一终端运行：%s b %d\n", argv[0], ctx.qid);
    int err = pthread_create(&receiver, NULL, receive_messages, &ctx);
    if (err != 0) {
        fprintf(stderr, "pthread_create: %s\n", strerror(err));
        (void)msgctl(ctx.qid, IPC_RMID, NULL);
        return EXIT_FAILURE;
    }
    char line[MAX_TEXT + 1];
    size_t used = 0;
    bool discard = false;
    while (atomic_load(&running) && !interrupted) {
        struct pollfd input = { .fd = STDIN_FILENO, .events = POLLIN };
        int ready = poll(&input, 1, 100);
        if (ready < 0) {
            if (errno == EINTR) continue;
            perror("poll"); result = EXIT_FAILURE; break;
        }
        if (ready == 0) continue;
        if (input.revents & (POLLERR | POLLNVAL)) {
            fputs("标准输入不可读。\n", stderr); result = EXIT_FAILURE; break;
        }
        if (!(input.revents & (POLLIN | POLLHUP))) continue;
        char chunk[256];
        ssize_t n = read(STDIN_FILENO, chunk, sizeof chunk);
        if (n < 0) {
            if (errno == EINTR) continue;
            perror("read"); result = EXIT_FAILURE; break;
        }
        if (n == 0) break; /* EOF 是会话结束；未按回车的残行不发送。 */
        for (ssize_t i = 0; i < n; ++i) {
            unsigned char c = (unsigned char)chunk[i];
            if (c == '\n') {
                if (!discard) {
                    if (used && line[used - 1] == '\r') --used;
                    line[used] = '\0';
                    int status = send_line(&ctx, line, used);
                    if (status != 0) {
                        if (status < 0) result = EXIT_FAILURE;
                        atomic_store(&running, false);
                        break;
                    }
                }
                used = 0; discard = false;
            } else if (!discard) {
                if (c == '\0' || used == MAX_TEXT) {
                    fprintf(stderr, "[未发送] 此行含 NUL 或超过 %d 字节，整行已忽略。\n", MAX_TEXT);
                    discard = true;
                } else line[used++] = (char)c;
            }
        }
    }
    /* 只释放本次会话的内核队列；唤醒双方阻塞的 msgrcv，然后等待线程退出。 */
    if (msgctl(ctx.qid, IPC_RMID, NULL) < 0 && !queue_gone(errno)) {
        perror("msgctl IPC_RMID"); result = EXIT_FAILURE;
        /* 极少见权限变化时避免 join 永久等待。msgrcv 是 Linux 取消点。 */
        (void)pthread_cancel(receiver);
    }
    err = pthread_join(receiver, NULL);
    if (err) { fprintf(stderr, "pthread_join: %s\n", strerror(err)); result = EXIT_FAILURE; }
    if (atomic_load(&receive_failed)) result = EXIT_FAILURE;
    return result;
}
