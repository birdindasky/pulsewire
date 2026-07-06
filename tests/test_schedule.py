"""launchd 调度生成测试 + 失败告警 skip 路径(均无网络/无 DB)。"""

from __future__ import annotations

import pytest

from pulsewire.obs.alert import alert_failure
from pulsewire.schedule.launchd import LABEL, generate

pytestmark_async = pytest.mark.asyncio


def test_generate_plist_and_wrapper(tmp_path):
    info = generate(hour=9, minute=15, project_root=tmp_path)
    plist = tmp_path / "deploy" / f"{LABEL}.plist"
    wrapper = tmp_path / "deploy" / "run_daily.sh"
    assert plist.exists() and wrapper.exists()

    plist_text = plist.read_text(encoding="utf-8")
    assert f"<string>{LABEL}</string>" in plist_text
    assert "<integer>9</integer>" in plist_text   # Hour
    assert "<integer>15</integer>" in plist_text  # Minute
    assert str(wrapper) in plist_text             # plist 指向包装脚本

    wrapper_text = wrapper.read_text(encoding="utf-8")
    assert "pulsewire run" in wrapper_text
    assert str(tmp_path) in wrapper_text          # cd 进项目根
    # 早间触发硬化:先等 Docker daemon 就绪(必要时拉起 Docker.app)再 compose up,
    # 顺序必须在 postgres 之前(否则 daemon 没起时 compose up 当场失败,2026-06-14 06:12 教训)
    assert "docker daemon 就绪" in wrapper_text
    assert "open -a Docker" in wrapper_text
    assert wrapper_text.index("info") < wrapper_text.index("compose up")
    # 安装说明只打印命令、不自动改系统状态
    assert "launchctl load" in info["instructions"]


@pytest.mark.asyncio
async def test_alert_skips_when_unconfigured():
    """未配任何通道 → 各渠道 skipped,不假装发成功、不发网络请求。

    2026-07-03 f02 起飞书告警首选自建应用通道(app_id/secret/openid),故"未配"须把
    这三件也置空——否则从 .env 读到真凭证会真发一条告警(本测试曾因此误发)。"""
    from pulsewire.config import Settings

    settings = Settings(
        feishu_webhook=None, serverchan_token=None,
        feishu_app_id=None, feishu_app_secret=None, feishu_user_openid=None,
    )
    res = await alert_failure(settings, run_id="r1", stage="rank", error="boom", error_type="RuntimeError")
    assert res == {"feishu": "skipped", "wechat": "skipped"}
