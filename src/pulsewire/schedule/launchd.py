"""macOS launchd 每日调度:生成 plist + 包装脚本,打印安装说明。[阶段 7]

形态:本地 Mac + launchd 每日触发一次完整流水线($0,弃 VPS / APScheduler 常驻)。
- 不自动写入 ~/Library/LaunchAgents、不自动 launchctl load(改系统状态 = 留给用户手动)。
- 只把 plist + run_daily.sh 生成到仓库 deploy/,并打印 `launchctl` 安装命令。
- 包装脚本 cd 进项目跑 `uv run pulsewire run`,日志追加到 deploy/logs/;
  launchd 无登录 shell,DeepSeek key 由 resolve_deepseek_key() 走 Keychain 兜底(见 config)。
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

from pulsewire.config import PROJECT_ROOT

LABEL = "com.pulsewire.daily"
SENTINEL_LABEL = "com.pulsewire.sentinel"


def _uv_path() -> str:
    """解析 uv 绝对路径(launchd 的 PATH 极简,必须用绝对路径)。"""
    return shutil.which("uv") or "/opt/homebrew/bin/uv"


def _docker_path() -> str:
    """解析 docker 绝对路径(同 uv:launchd PATH 极简,必须用绝对路径)。"""
    return shutil.which("docker") or "/usr/local/bin/docker"


def render_wrapper(*, project_root: Path, uv: str, docker: str, log_dir: Path, hour: int = 6) -> str:
    """run_daily.sh:cd 项目 → 确保 Docker daemon + postgres 就绪 → uv run pulsewire run,日志追加。

    launchd 无登录 shell。早间触发(如 06:00)时 Mac 常刚睡醒、Docker Desktop 还在启动:
    先尝试拉起 Docker.app 并**等 daemon 可达**(最多 ~180s),再 `docker compose up -d postgres`
    等 healthy(最多 60s)。否则 compose up 会因 daemon socket 不存在当场失败——2026-06-14 06:12
    首跑即栽在此(Docker 没开 → postgres 起不来 → 整跑退出)。daemon 始终起不来才冒泡失败落日志,
    绝不静默产空报。

    2026-06-15 硬化:caffeinate -i(整跑防空闲休眠掐 postgres 连接)+ 跑完关 Docker Desktop。
    2026-06-27 同步进生成器(此前只手改了 deploy/run_daily.sh、生成器没跟上,重生成会回归):
      ① 顶部补课闸——launchd 现除正点外每 5 分钟也触发(plist StartInterval=300),用于「插电开机
         后自动补」;今天已交付/未到正点/未插电/已在跑则秒退,只在「该跑且没跑」才真跑(幂等)。
      ② 关 Docker 改用官方 `docker desktop stop`(旧 osascript quit "Docker Desktop" 只关界面、
         后台 daemon+VM 不下 → Docker 永远关不掉);留 osascript 作回退。

    说明:生成的是基础版包装脚本(四道补课闸 + Docker 生命周期)。更进一步的运维加固
    (「已在跑」的僵尸超时判定、失败退避与当日上限等)不进模板——bash 在 f-string 里全走
    `{{}}` 转义、堆复杂逻辑易错;需要的话在生成后的 run_daily.sh 里手改追加(脚本本就可手改,
    重生成覆盖前会自动留 .bak 备份,见 _backup_if_differs)。
    """
    return f"""#!/bin/bash
# pulsewire 每日跑包装脚本(launchd 调用)。由 `pulsewire schedule` 生成,可手改。
set -euo pipefail
cd "{project_root}"
mkdir -p "{log_dir}"

# ── 补课闸（2026-06-27）：launchd 现在除正点 {hour:02d}:00 外，每 5 分钟也触发本脚本（StartInterval），
#    用于「插电开机后自动补」。每次触发先过这道闸，只有「该跑且没跑」才真开 Docker 跑，其余秒退——
#    保证幂等、不抢跑、不重复、不在电池下空耗电。回滚:删掉①-④四道闸即回到裸跑。
today="$(date +%Y-%m-%d)"
receipt="{project_root}/deploy/state/last_delivery_feishu"
catchup_log="{log_dir}/catchup.log"
note() {{ echo "[catchup] $(date '+%Y-%m-%d %H:%M:%S') $*" >> "$catchup_log"; }}

# ① 今天已交付 → 跳过（收据由 deliver 成功时写当天日期）
if [ -f "$receipt" ] && [ "$(cat "$receipt" 2>/dev/null)" = "$today" ]; then
  note "今天($today)已交付，跳过"; exit 0
fi
# ② 没到点（本地 {hour:02d}:00 前）→ 不抢跑，等正点(与 plist 的正点小时同源)
if [ "$(date +%H)" -lt {hour} ]; then
  note "未到 {hour:02d}:00，跳过"; exit 0
fi
# ③ 没插电 → 不在电池下开 Docker 跑重活，等插电后补
if ! /usr/bin/pmset -g batt | grep -q "AC Power"; then
  note "未插电(电池供电)，待插电后补，跳过"; exit 0
fi
# ④ 已有一条 pulsewire run 在跑（正点跑/上次补课/手动跑未结束）→ 不重复开第二条流水线
if pgrep -f "pulsewire run" >/dev/null 2>&1; then
  note "已有 pulsewire run 在跑，跳过"; exit 0
fi
note "需补课：today=$today 收据=$([ -f "$receipt" ] && cat "$receipt" 2>/dev/null || echo 无)；插电+已到点+无在跑 → 开跑"

ts="$(date +%Y%m%d_%H%M%S)"
log="{log_dir}/run_${{ts}}.log"

# 先确保 Docker daemon 就绪,再拉 postgres 等 healthy(早间机器刚睡醒时 Docker 可能还没起来)。
{{
  echo "[wrapper] $(date '+%Y-%m-%d %H:%M:%S') 等 Docker daemon 就绪"
  if ! "{docker}" info >/dev/null 2>&1; then
    /usr/bin/open -a Docker >/dev/null 2>&1 || true   # best-effort 拉起 Docker Desktop(已开则 no-op)
  fi
  for i in $(seq 1 60); do
    if "{docker}" info >/dev/null 2>&1; then echo "[wrapper] docker daemon 就绪"; break; fi
    if [ "$i" -eq 60 ]; then echo "[wrapper] docker daemon 180s 内未就绪,放弃本次"; exit 1; fi
    sleep 3
  done
  echo "[wrapper] 确保 postgres 就绪"
  "{docker}" compose up -d postgres
  for i in $(seq 1 30); do
    if "{docker}" compose ps postgres 2>/dev/null | grep -q healthy; then
      echo "[wrapper] postgres healthy"; break
    fi
    if [ "$i" -eq 30 ]; then echo "[wrapper] postgres 60s 内未 healthy,放弃本次"; exit 1; fi
    sleep 2
  done
}} >> "$log" 2>&1

# 失败要冒泡:run 内部已多通道告警 + 写 runs.status=failed;这里只负责落日志。
# 不能用 exec(否则跑完无法回来关 Docker):捕获退出码,跑完无论成败都退出 Docker,再以原码退出。
rc=0
# caffeinate -i:整跑期间阻止系统空闲休眠,防 6 点机器睡回去导致运行中 postgres 连接被掐
# (2026-06-15 真凶:OSError :5432,机器睡着连接掉)。跑完即释放,不常驻。
/usr/bin/caffeinate -i "{uv}" run pulsewire run >> "$log" 2>&1 || rc=$?

# 用完就关:跑完(成功/失败都)关闭 Docker Desktop —— 平时不常驻,只在每日跑那十几分钟活着。
# 2026-06-27 修:旧版 `osascript quit "Docker Desktop"` 只关了界面 Electron 进程,后台 com.docker.backend
#   + 虚拟机 com.docker.virtualization 照跑 → daemon 不下,Docker 看着永远关不掉(用户报"总是不自动关")。
#   改用官方 `docker desktop stop`(同步等待,真把 UI+后台+VM 一起停;实测 ~6s 后 daemon DOWN、进程清光)。
#   留 osascript 作回退(万一 CLI 不在/报错)。
{{
  echo "[wrapper] $(date '+%Y-%m-%d %H:%M:%S') 跑完 rc=$rc,关闭 Docker Desktop"
  if {docker} desktop stop >/dev/null 2>&1; then
    echo "[wrapper] docker desktop stop 成功,daemon 已停"
  else
    echo "[wrapper] docker desktop stop 失败/不可用,回退 osascript quit"
    /usr/bin/osascript -e 'quit app "Docker Desktop"' >/dev/null 2>&1 || true
  fi
}} >> "$log" 2>&1
exit "$rc"
"""


def render_plist(*, wrapper: Path, log_dir: Path, hour: int, minute: int) -> str:
    """launchd plist:每日 hour:minute 跑一次 wrapper。

    StartInterval=300(2026-06-27):除正点外每 5 分钟也触发,配合 run_daily.sh 顶部补课闸做
    「插电开机后自动补」;同 label launchd 自动串行,闸幂等,不会双跑。
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{wrapper}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <!-- 补课触发（2026-06-27）：除正点 {hour:02d}:{minute:02d} 外，每 5 分钟也触发一次；run_daily.sh 顶部的补课闸
         判定（今天已交付/未到点/未插电/已在跑则秒退），只在「插电+已到点+今天没跑成」时才真跑。
         插电开机后约 5 分钟内自动补上当天日报。回滚：移除本 StartInterval 并 reload。 -->
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_dir}/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/launchd.err.log</string>
</dict>
</plist>
"""


def render_sentinel_wrapper(*, project_root: Path, uv: str, log_dir: Path) -> str:
    """sentinel.sh:交付哨兵包装脚本。只读交付收据文件 + 发告警,不碰 Docker/DB(2026-06-15 二⑥)。

    与 run_daily.sh 不同:哨兵不需要 postgres/Docker(日报跑完会关 Docker),所以不拉 Docker、
    不等 postgres——直接 `uv run pulsewire sentinel`,轻量、快、不会因 Docker 没起而自己也挂。
    """
    return f"""#!/bin/bash
# pulsewire 交付哨兵包装脚本(launchd 调用)。由 `pulsewire schedule` 生成,可手改。
# 只读交付收据文件判断今天日报送没送,不依赖 Docker/postgres。
set -euo pipefail
cd "{project_root}"
mkdir -p "{log_dir}"
exec "{uv}" run pulsewire sentinel >> "{log_dir}/sentinel.log" 2>&1
"""


def render_sentinel_plist(*, wrapper: Path, log_dir: Path, hour: int, minute: int) -> str:
    """launchd plist:每日 hour:minute 跑一次哨兵 wrapper(机器睡则唤醒后补跑)。"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{SENTINEL_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{wrapper}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_dir}/sentinel.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/sentinel.launchd.err.log</string>
</dict>
</plist>
"""


def generate_sentinel(
    *, hour: int = 8, minute: int = 0, project_root: Path | None = None
) -> dict[str, str]:
    """把交付哨兵 plist + 包装脚本写到 deploy/,返回各路径 + 安装说明。不改系统状态。

    独立于 generate():只写 sentinel.sh + 哨兵 plist,不碰已手改硬化的 run_daily.sh。
    默认 08:00(给 06:00 日报留足完成余量;哨兵自带"run 正在跑则不报"的护栏防唤醒同触发误报)。
    """
    root = project_root or PROJECT_ROOT
    deploy = root / "deploy"
    log_dir = deploy / "logs"
    deploy.mkdir(parents=True, exist_ok=True)
    uv = _uv_path()

    wrapper = deploy / "sentinel.sh"
    plist = deploy / f"{SENTINEL_LABEL}.plist"
    wrapper_text = render_sentinel_wrapper(project_root=root, uv=uv, log_dir=log_dir)
    plist_text = render_sentinel_plist(wrapper=wrapper, log_dir=log_dir, hour=hour, minute=minute)
    for _p, _t in ((wrapper, wrapper_text), (plist, plist_text)):
        _backup_if_differs(_p, _t)
    wrapper.write_text(wrapper_text, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    plist.write_text(plist_text, encoding="utf-8")

    target = Path.home() / "Library" / "LaunchAgents" / f"{SENTINEL_LABEL}.plist"
    instructions = (
        f"已生成交付哨兵(未改系统状态):\n"
        f"  - 包装脚本:{wrapper}\n"
        f"  - launchd plist:{plist}(每日 {hour:02d}:{minute:02d} 触发)\n\n"
        f"安装:\n"
        f"  cp {plist} {target}\n"
        f"  launchctl load {target}\n"
        f"卸载:  launchctl unload {target} && rm {target}\n"
    )
    return {
        "wrapper": str(wrapper), "plist": str(plist), "target": str(target),
        "hour": str(hour), "minute": str(minute), "instructions": instructions,
    }


def _backup_if_differs(path: Path, new_text: str) -> str | None:
    """目标已存在且内容与将写入的不同 → 先备份成 <名>.bak-<时间戳>,返回备份路径(否则 None)。

    防手改过的生产文件被重新生成无声盖掉(2026-07-05 真踩过:硬化版 run_daily.sh 被默认参数
    重生成覆盖,只能靠改动流水重建)。生成器永远先留底再落笔。
    """
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old != new_text:
            from datetime import datetime

            bak = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            bak.write_text(old, encoding="utf-8")
            return str(bak)
    return None


def generate(*, hour: int = 8, minute: int = 30, project_root: Path | None = None) -> dict[str, str]:
    """把 plist + 包装脚本写到 deploy/,返回各路径 + 安装说明。不改系统状态。

    覆盖已有且内容不同的文件前,自动留 .bak-<时间戳> 备份(见 _backup_if_differs)。
    """
    root = project_root or PROJECT_ROOT
    deploy = root / "deploy"
    log_dir = deploy / "logs"
    deploy.mkdir(parents=True, exist_ok=True)
    uv = _uv_path()
    docker = _docker_path()

    wrapper = deploy / "run_daily.sh"
    plist = deploy / f"{LABEL}.plist"

    wrapper_text = render_wrapper(project_root=root, uv=uv, docker=docker, log_dir=log_dir, hour=hour)
    plist_text = render_plist(wrapper=wrapper, log_dir=log_dir, hour=hour, minute=minute)
    backups = [b for b in (_backup_if_differs(wrapper, wrapper_text),
                           _backup_if_differs(plist, plist_text)) if b]

    wrapper.write_text(wrapper_text, encoding="utf-8")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    plist.write_text(plist_text, encoding="utf-8")

    target = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    bak_note = ("".join(f"  - ⚠️ 原文件内容不同,已备份:{b}\n" for b in backups)) if backups else ""
    instructions = (
        f"已生成(未改系统状态):\n"
        f"  - 包装脚本:{wrapper}\n"
        f"  - launchd plist:{plist}(每日 {hour:02d}:{minute:02d} 触发)\n"
        f"  - 日志目录:{log_dir}\n"
        f"{bak_note}\n"
        f"安装(把每日任务装进 launchd,需你手动执行):\n"
        f"  cp {plist} {target}\n"
        f"  launchctl load {target}\n\n"
        f"立即试跑一次:  launchctl start {LABEL}\n"
        f"查看是否已装:  launchctl list | grep pulsewire\n"
        f"卸载:          launchctl unload {target} && rm {target}\n"
    )
    return {
        "wrapper": str(wrapper),
        "plist": str(plist),
        "log_dir": str(log_dir),
        "target": str(target),
        "uv": uv,
        "hour": str(hour),
        "minute": str(minute),
        "instructions": instructions,
    }
