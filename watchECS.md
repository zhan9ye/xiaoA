对于你这种**“只做纯粹的 IP 代理，不需要跑任何其他业务”**的极简服务器，千万不要去装 Zabbix、Prometheus 这种重型的监控软件，它们本身占用的 CPU 和内存可能比你的代理软件还要高，纯属本末倒置。

针对你的需求，最优雅、资源占用几乎为 **0** 的做法是：**写一个轻量级的 Bash 脚本，配合 Linux 系统自带的定时任务（Cron），每分钟巡检一次。**

我为你编写了一个“贴身保镖”脚本，它的逻辑是：**实时扫描 CPU 或内存占用超过特定阈值的进程，如果这个进程不在你的“白名单（Squid、SSH、系统核心进程）”里，就直接 `kill -9` 杀掉，并记录在案。**

---

### 第一步：创建自动化巡检脚本

1. 通过阿里云终端连接到你的 ECS 服务器。
2. 创建并编辑一个脚本文件，比如叫 `guard.sh`：
   ```bash
   nano /root/guard.sh
   ```
3. 将下面的代码完整复制粘贴进去：

```bash

#!/bin/bash
# ================= 配置区 =================
# 1. 记录被杀进程的日志位置
LOG_FILE="/var/log/ecs_guard.log"
# 2. 触发阈值（百分比），超过这个值的进程才会被重点审查
CPU_LIMIT=30.0
MEM_LIMIT=30.0
# 3. 绝对白名单（正则表达式）。
# 务必保留 systemd, sshd, kworker, aliyun 等核心进程，以及你的代理 squid
WHITELIST="squid|sshd|systemd|kworker|rcu|migration|aliyun|AliYun|agetty|bash|cron|dbus|network|syslog|journal|grep|ps|sadc|unattended-upgr|needrestart|dpkg|apt"
# =========================================
# 获取所有进程的 PID, 命令, CPU占用率, 内存占用率
ps -eo pid,comm,%cpu,%mem --sort=-%cpu | awk 'NR>1' | while read pid cmd cpu mem; do
    
    # 检查进程是否存在
    if [ ! -d "/proc/$pid" ]; then
        continue
    fi

    # 使用 awk 判断 CPU 或 内存 是否超过阈值
    OVER_LIMIT=$(awk -v c="$cpu" -v m="$mem" -v cl="$CPU_LIMIT" -v ml="$MEM_LIMIT" 'BEGIN {if (c >= cl || m >= ml) print 1; else print 0}')

    if [ "$OVER_LIMIT" -eq 1 ]; then
        # 如果超过阈值，检查是否在白名单中
        if ! echo "$cmd" | grep -iqE "$WHITELIST"; then
            # 不在白名单中，触发警报并击杀！
            TIME=$(date '+%Y-%m-%d %H:%M:%S')
            echo "$TIME - [拦截] 发现违规进程! PID: $pid | 进程名: $cmd | CPU: $cpu% | 内存: $mem%" >> $LOG_FILE
            
            # 发送强制终止信号
            kill -9 $pid
            
            echo "$TIME - [执行] 已强制击杀 PID: $pid" >> $LOG_FILE
        fi
    fi
done

```
4. 保存并退出（在 `nano` 中按 `Ctrl+O` 回车保存，`Ctrl+X` 退出）。
5. 赋予这个脚本可执行权限：
   ```bash

   chmod +x /root/guard.sh

   ```

---

### 第二步：设置定时任务（每分钟自动执行）

我们需要让系统每隔 1 分钟就在后台默默运行一次这个脚本。

1. 打开定时任务编辑器：
   ```bash

   crontab -e

   ```
   *(如果提示选择编辑器，按数字选择 `nano` 即可)*
2. 在文件最底部，新起一行加入以下代码：
   ```bash

   * * * * * /root/guard.sh >/dev/null 2>&1

   ```
   *(最前面的 5 个星号代表“每分钟执行一次”)*
3. 保存并退出。

---

### 第三步：怎么看它有没有在干活？

如果你怀疑服务器里混进了挖矿木马，或者有什么进程乱吃资源，你只需要查看日志文件就行了：

```bash
# 实时查看保镖击杀记录
cat /var/log/ecs_guard.log
```
如果里面空空如也，说明服务器非常健康，只有你的 Squid 在安静地跑；如果它自动干掉了某些进程，日志里会清晰记录发生的时间、进程名以及它当时的 CPU 占用率。

### 💡 给你的额外建议

**为什么我不建议“非白名单的统统杀掉”，而是“超过阈值且非白名单的才杀掉”？**

因为 Linux 系统底层有大量随时产生、随时销毁的临时进程（比如网络包处理任务、磁盘 IO 任务）。如果你写一个绝对的白名单，很容易误杀内核线程，导致服务器直接死机（Kernel Panic）失联，你连 SSH 都登不进去，只能去阿里云后台强制重启。

我们这个方案的逻辑是**“抓大放小”**：底层的临时小进程随它去，只要有不明进程敢吃超过 30% 的 CPU 或内存（通常是恶意扫描脚本、挖矿木马，或者崩溃的死循环程序），并且它不是 Squid 和系统核心服务，就一击必杀。这对于纯代理服务器来说，是最稳妥、最不需要操心的防御策略。