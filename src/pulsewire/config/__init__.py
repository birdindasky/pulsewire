"""typed 配置加载(pydantic-settings)。

- 全局非密钥配置来自仓库根 `config.yaml`。
- 密钥(数据库密码 / DeepSeek / Jina / Server酱 / 飞书)只来自环境变量或 `.env`,绝不进仓库。
- 优先级:环境变量 > .env > config.yaml。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from .models import (
    AppCfg,
    DatabaseCfg,
    DedupCfg,
    DeliverCfg,
    EnrichCfg,
    EventCfg,
    FetchCfg,
    QaCfg,
    RankCfg,
    RenderCfg,
    RetentionCfg,
    RunCfg,
    Source,
    SourcesFile,
    SummarizeCfg,
    ThreadsCfg,
)

# 仓库根 = .../src/pulsewire/config/__init__.py 上溯 3 层
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
SOURCES_FILE = PROJECT_ROOT / "sources.yaml"


class Settings(BaseSettings):
    """全局配置 + 密钥。"""

    # --- 来自 config.yaml(可被 env 覆盖) ---
    app: AppCfg = Field(default_factory=AppCfg)
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)
    fetch: FetchCfg = Field(default_factory=FetchCfg)
    dedup: DedupCfg = Field(default_factory=DedupCfg)
    enrich: EnrichCfg = Field(default_factory=EnrichCfg)
    rank: RankCfg = Field(default_factory=RankCfg)
    event: EventCfg = Field(default_factory=EventCfg)
    summarize: SummarizeCfg = Field(default_factory=SummarizeCfg)
    threads: ThreadsCfg = Field(default_factory=ThreadsCfg)
    qa: QaCfg = Field(default_factory=QaCfg)
    render: RenderCfg = Field(default_factory=RenderCfg)
    deliver: DeliverCfg = Field(default_factory=DeliverCfg)
    retention: RetentionCfg = Field(default_factory=RetentionCfg)
    run: RunCfg = Field(default_factory=RunCfg)

    # --- 仅来自环境变量 / .env(密钥,默认 None) ---
    deepseek_api_key: str | None = None
    # DeepSeek key:env AI_API_KEY 或 macOS Keychain(service=AI_API_KEY)
    ai_api_key: str | None = None
    jina_api_key: str | None = None
    serverchan_token: str | None = None
    feishu_webhook: str | None = None
    # 飞书自建应用(deliver.feishu.mode=app 时用,发 PNG 到 open_id 私信)
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_user_openid: str | None = None
    # 前端方向 A:飞书折叠卡(一条 interactive 卡片装四板,可折叠)替代长图 PNG。默认关=照发 PNG(零变化);
    # 开则 feishu_app 发折叠卡。上线前须一次真机测发确认渲染(外部副作用,stop-and-confirm)。回滚=false。
    feishu_card_enabled: bool = False
    github_token: str | None = None  # 可选:GitHub API 提速率额度

    model_config = SettingsConfigDict(
        env_prefix="PULSEWIRE_",
        env_nested_delimiter="__",
        env_file=str(PROJECT_ROOT / ".env"),
        yaml_file=str(CONFIG_FILE),
        extra="ignore",
    )

    def resolve_deepseek_key(self) -> str | None:
        """解析 DeepSeek key,多源回退(密钥绝不进仓库,优先安全存储):

        1. PULSEWIRE_DEEPSEEK_API_KEY(env / .env)
        2. PULSEWIRE_AI_API_KEY(env / .env)
        3. AI_API_KEY(裸环境变量——交互式 shell 已从 Keychain 导出)
        4. macOS Keychain service=AI_API_KEY(launchd 等无 shell 场景;非 darwin 跳过)
        """
        if self.deepseek_api_key:
            return self.deepseek_api_key
        if self.ai_api_key:
            return self.ai_api_key
        env = os.environ.get("AI_API_KEY")
        if env:
            return env
        return _read_macos_keychain("AI_API_KEY")

    def resolve_github_token(self) -> str | None:
        """解析 GitHub token,多源回退(可选,提 API 速率额度;只读公开仓搜索,无需任何 scope):

        1. PULSEWIRE_GITHUB_TOKEN(env / .env)
        2. GITHUB_TOKEN(裸环境变量,交互式 shell)
        3. macOS Keychain service=GITHUB_TOKEN(launchd 等无 shell 场景;非 darwin 跳过)
        取不到 → None(未认证跑,搜索限速 10/min,功能不挂)。
        """
        if self.github_token:
            return self.github_token
        env = os.environ.get("GITHUB_TOKEN")
        if env:
            return env
        return _read_macos_keychain("GITHUB_TOKEN")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 优先级高→低:init > env > .env > config.yaml
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


def _read_macos_keychain(service: str) -> str | None:
    """从 macOS Keychain 取通用密码(service=<service>)。非 darwin / 取不到 → None,不冒泡。"""
    if sys.platform != "darwin":
        return None
    user = os.environ.get("USER") or ""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-a", user, "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    key = out.stdout.strip()
    return key or None


@lru_cache
def get_settings() -> Settings:
    """加载并缓存全局配置(校验失败会抛 ValidationError)。"""
    return Settings()


# 行内注释 = 策展好的人类可读名:`  - id: "slug"   # NVIDIA Blog`
_ID_COMMENT_RE = re.compile(r'^\s*-\s*id:\s*["\']?([A-Za-z0-9_-]+)["\']?\s*#\s*(.+?)\s*$')
# 启发式美化 slug 的尾巴(google-news 包装源)
_GOOGLE_NEWS_SUFFIX_RE = re.compile(r"-(?:via-)?google-?news$", re.IGNORECASE)


@lru_cache
def load_sources() -> list[Source]:
    """加载并校验信源注册表 sources.yaml;用行内注释回填缺失的 display_name。"""
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(f"找不到信源注册表:{SOURCES_FILE}")
    text = SOURCES_FILE.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    sources = SourcesFile.model_validate(raw).sources
    # sources.yaml 每条 id 后的 `# 人名` 就是策展好的可读名(277/278 覆盖)。不改文件、不破坏
    # 注释/格式(yaml.dump 会毁掉它们),只在载入时读取回填。显式写了 display_name 的不覆盖。
    comments: dict[str, str] = {}
    for line in text.splitlines():
        m = _ID_COMMENT_RE.match(line)
        if m:
            comments[m.group(1)] = m.group(2).strip()
    for s in sources:
        if s.display_name is None and s.id in comments:
            s.display_name = comments[s.id]
    return sources


@lru_cache
def _source_display_map() -> dict[str, str]:
    return {s.id: s.display_name for s in load_sources() if s.display_name}


def source_label(source_id: str) -> str:
    """源 id(机器 slug)→ 人类可读名(展示用)。

    优先用注册表 display_name(载入时从 sources.yaml 行内注释回填);缺失则启发式美化
    slug(去 google-news 尾巴、连字符转空格),保证孤儿源/未注册源也不把机器 slug 露给用户。
    """
    name = _source_display_map().get(source_id)
    if name:
        return name
    s = _GOOGLE_NEWS_SUFFIX_RE.sub("", source_id or "")
    s = s.replace("-", " ").replace("_", " ").strip()
    return s or (source_id or "")


__all__ = [
    "Settings", "Source", "get_settings", "load_sources", "source_label", "PROJECT_ROOT",
]
