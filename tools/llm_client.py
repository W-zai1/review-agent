#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_client.py —— OpenAI 兼容 Chat Completions 客户端（零依赖，urllib）

设计目标：
  - 不引入 openai SDK，只用标准库，保持项目「零依赖」；
  - base_url / model / api_key / 超时 / 重试全部走 config/llm.yaml + 环境变量；
  - 无凭据时 available=False，由 review.py 降级为纯规则审，绝不伪造 LLM 结论；
  - 不把密钥写进日志或报告（只输出 masked 形式）。

CLI:
    python tools/llm_client.py --check          # 检查配置是否可用（不真调用）
    python tools/llm_client.py "你好"            # 真调一次，打印回复
"""
import os
import sys
import json
import time
import ipaddress
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

try:
    import yaml
except ImportError:  # 环境变量模式仍可用
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "llm.yaml"
LOCAL_PATH = ROOT / "config" / "llm.local.json"

LOCAL_KEYS = {
    "enabled", "base_url", "model", "api_key", "timeout", "max_retries",
    "temperature", "max_tokens", "max_input_chars", "max_issues_per_dim",
    "chunking", "chunk_overlap", "chunk_chars", "cache",
}

DEFAULTS = {
    "enabled": "auto",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",
    "timeout": 40,
    "max_retries": 2,
    "temperature": 0,
    "max_tokens": 1200,
    "max_input_chars": 12000,
    "max_issues_per_dim": 5,
    "chunking": True,
    "chunk_overlap": 400,
    "chunk_chars": 6000,
    "cache": True,
    "headers": {},
}

# 常见 OpenAI 兼容服务商预设；前端配置台据此生成下拉选项。
PROVIDERS = [
    {
        "id": "deepseek", "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "needs_key": True, "local": False,
        "hint": "国内直连，OpenAI 兼容。到 platform.deepseek.com 创建 API Key。",
    },
    {
        "id": "openai", "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "needs_key": True, "local": False,
        "hint": "官方端点，需要可访问 OpenAI 的网络环境。",
    },
    {
        "id": "dashscope", "name": "阿里云百炼 / 通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "needs_key": True, "local": False,
        "hint": "百炼兼容模式端点，Key 在阿里云百炼控制台获取。",
    },
    {
        "id": "moonshot", "name": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k"],
        "needs_key": True, "local": False,
        "hint": "Moonshot 开放平台，OpenAI 兼容。",
    },
    {
        "id": "zhipu", "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-plus"],
        "needs_key": True, "local": False,
        "hint": "智谱开放平台 v4 端点，OpenAI 兼容。",
    },
    {
        "id": "siliconflow", "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "needs_key": True, "local": False,
        "hint": "聚合多家开源模型，模型名区分大小写。",
    },
    {
        "id": "volcengine", "name": "火山方舟（豆包）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1-5-pro-32k-250115",
        "models": ["doubao-1-5-pro-32k-250115", "doubao-1-5-lite-32k-250115"],
        "needs_key": True, "local": False,
        "hint": "方舟端点；模型名也可填推理接入点 ID（ep-xxxx）。",
    },
    {
        "id": "qianfan", "name": "百度千帆",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.0-8k",
        "models": ["ernie-4.0-8k", "ernie-3.5-8k"],
        "needs_key": True, "local": False,
        "hint": "千帆 v2 OpenAI 兼容端点，模型名以控制台为准。",
    },
    {
        "id": "ollama", "name": "Ollama（本地）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "llama3.1:8b", "deepseek-r1:7b"],
        "needs_key": False, "local": True,
        "hint": "先运行 ollama serve，并 ollama pull qwen2.5:7b。本地端点无需 Key。",
    },
    {
        "id": "lmstudio", "name": "LM Studio（本地）",
        "base_url": "http://127.0.0.1:1234/v1",
        "model": "local-model",
        "models": ["local-model"],
        "needs_key": False, "local": True,
        "hint": "在 LM Studio 打开 Local Server（OpenAI Compatible），端口默认 1234。",
    },
    {
        "id": "vllm", "name": "vLLM（本地）",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
        "models": ["Qwen/Qwen2.5-7B-Instruct"],
        "needs_key": False, "local": True,
        "hint": "vLLM 启动时加 --served-model-name，端口按实际填写。",
    },
    {
        "id": "custom", "name": "自定义（OpenAI 兼容）",
        "base_url": "", "model": "", "models": [],
        "needs_key": None, "local": False,
        "hint": "任意兼容 /chat/completions 的端点；本机或局域网地址可留空 Key。",
    },
]


def list_providers():
    return [dict(p, models=list(p.get("models") or [])) for p in PROVIDERS]


class LLMError(RuntimeError):
    """调用失败（网络 / HTTP / 响应格式）。消息可直接写进日志。"""


@dataclass
class LLMResult:
    content: str
    usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})
    model: str = ""
    latency_ms: int = 0
    # finish_reason=length 表示输出被 max_tokens 截断（思考型模型尤其常见），
    # 此时 content 可能为空或不完整，必须让调用方知道。
    finish_reason: str = ""


@dataclass
class LLMConfig:
    enabled: bool = True
    base_url: str = DEFAULTS["base_url"]
    model: str = DEFAULTS["model"]
    api_key: str = ""
    timeout: float = 40
    max_retries: int = 2
    temperature: float = 0
    max_tokens: int = 1200
    max_input_chars: int = 12000
    max_issues_per_dim: int = 5
    chunking: bool = True       # 长文分块送审（不截断）
    chunk_overlap: int = 400    # 相邻块重叠字符数，避免切断句子
    chunk_chars: int = 6000     # 每块字符数：太小费钱、太大单块发现写不完就截断
    cache: bool = True          # 语义审结果缓存（原文+规则+模型+prompt 版本一致才命中）
    headers: dict = field(default_factory=dict)
    source: str = "default"  # 便于排查：default / config / env

    @property
    def endpoint(self):
        base = (self.base_url or "").rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    @property
    def available(self):
        """有模型 + (有密钥 或 本地端点) 才算可用。"""
        if not self.enabled or not self.model or not self.base_url:
            return False
        return bool(self.api_key) or _is_local(self.base_url)

    def masked_key(self):
        """只回显前缀，绝不回显末几位。

        `sk-` 这类公共前缀不构成秘密；末 4 位是真值的一部分，连同前 4 位
        足以在低熵 Key 上爆破出大部分内容。
        """
        key = self.api_key or ""
        if not key:
            return ""
        if len(key) <= 8:
            return "***"
        return f"{key[:3]}…"

    def public_dict(self):
        return {
            "enabled": self.enabled,
            "available": self.available,
            "base_url": self.base_url,
            "endpoint": self.endpoint,
            "model": self.model,
            "api_key": self.masked_key(),
            "api_key_set": bool(self.api_key),
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "max_input_chars": self.max_input_chars,
            "chunking": self.chunking,
            "chunk_chars": self.chunk_chars,
            "max_issues_per_dim": self.max_issues_per_dim,
            "source": self.source,
            "local_override": LOCAL_PATH.exists(),
            "local_config": "config/llm.local.json",
        }


def _is_local(url):
    """本机 / 局域网端点视为无需 Key 的本地模型端点。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"):
        return True
    if host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def _as_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "disabled")


def _env(name, default=None):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _pick(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return ""


def _to(value, cast, default):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def read_local_config():
    """读取前端保存的本地覆盖配置（config/llm.local.json）。"""
    if not LOCAL_PATH.exists():
        return {}
    try:
        data = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_local_config(patch):
    """合并写入本地覆盖配置；值为 None 表示删除该键。"""
    current = read_local_config()
    for key in LOCAL_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCAL_PATH.with_name("llm.local.json.tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, LOCAL_PATH)
    try:
        os.chmod(LOCAL_PATH, 0o600)
    except OSError:
        pass
    return current


def clear_local_config():
    try:
        LOCAL_PATH.unlink()
        return True
    except FileNotFoundError:
        return False


def load_config(use_llm=None):
    """优先级：默认值 < config/llm.yaml < 环境变量 < 前端保存的本地覆盖。

    use_llm=False 强制关闭；use_llm=True 强制启用。
    """
    raw = dict(DEFAULTS)
    source = "default"
    if CONFIG_PATH.exists() and yaml is not None:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                raw.update({k: v for k, v in loaded.items() if v is not None})
                source = "config"
        except Exception:
            # 配置坏了不应让审查崩掉：退回默认值 + 环境变量
            source = "config-error"

    env_values = {
        "base_url": _env("REVIEW_AGENT_LLM_BASE_URL"),
        "model": _env("REVIEW_AGENT_LLM_MODEL"),
        "api_key": _env("REVIEW_AGENT_LLM_API_KEY"),
        "enabled": _env("REVIEW_AGENT_LLM_ENABLED"),
        "timeout": _env("REVIEW_AGENT_LLM_TIMEOUT"),
        "max_retries": _env("REVIEW_AGENT_LLM_MAX_RETRIES"),
        "max_tokens": _env("REVIEW_AGENT_LLM_MAX_TOKENS"),
        "max_input_chars": _env("REVIEW_AGENT_LLM_MAX_INPUT_CHARS"),
        "cache": _env("REVIEW_AGENT_LLM_CACHE"),
        "max_issues_per_dim": _env("REVIEW_AGENT_LLM_MAX_ISSUES"),
    }
    for key, value in env_values.items():
        if value is not None and value != "":
            raw[key] = value

    local = read_local_config()
    if local:
        for key, value in local.items():
            if key in LOCAL_KEYS and value is not None:
                raw[key] = value
        source = "ui"

    base_url = str(_pick(raw.get("base_url"), DEFAULTS["base_url"]))
    model = str(_pick(raw.get("model"), DEFAULTS["model"]))
    api_key = str(_pick(raw.get("api_key")))

    host = (urlsplit(base_url).hostname or "").lower() if base_url else ""
    if not api_key and "deepseek" in host:
        api_key = _env("DEEPSEEK_API_KEY", "")
    if not api_key and "openai" in host:
        api_key = _env("OPENAI_API_KEY", "")

    enabled = _as_bool(raw.get("enabled", "auto"), default=True)
    if use_llm is False:
        enabled = False
    elif use_llm is True:
        enabled = True

    headers = raw.get("headers")
    if not isinstance(headers, dict):
        headers = {}

    return LLMConfig(
        enabled=enabled,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=_to(raw.get("timeout"), float, 40),
        max_retries=_to(raw.get("max_retries"), int, 2),
        temperature=_to(raw.get("temperature"), float, 0),
        cache=_as_bool(raw.get("cache", True), default=True),
        max_tokens=_to(raw.get("max_tokens"), int, 1200),
        max_input_chars=_to(raw.get("max_input_chars"), int, 12000),
        max_issues_per_dim=_to(raw.get("max_issues_per_dim"), int, 5),
        headers={str(k): str(v) for k, v in headers.items()},
        source=source,
    )


class LLMClient:
    RETRYABLE = (408, 409, 429, 500, 502, 503, 504)

    def __init__(self, cfg):
        self.cfg = cfg

    def chat(self, messages, temperature=None, max_tokens=None):
        """一次 chat completion；失败抛 LLMError（已含重试）。"""
        if not self.cfg.available:
            raise LLMError("LLM 未启用或缺少 API Key / 本地端点")

        payload = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", **self.cfg.headers}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        last_error = None
        attempts = max(0, int(self.cfg.max_retries)) + 1
        for attempt in range(attempts):
            started = time.perf_counter()
            request = urllib.request.Request(
                self.cfg.endpoint, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.cfg.timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                return self._parse(body, started)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                hint = ""
                lower = detail.lower()
                if e.code == 404 and ("model" in lower or "not found" in lower):
                    hint = f"；若为本地 Ollama，请先执行 ollama pull {self.cfg.model}"
                last_error = LLMError(f"HTTP {e.code}: {detail}{hint}")
                if e.code not in self.RETRYABLE or attempt == attempts - 1:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                reason = getattr(e, "reason", e)
                last_error = LLMError(f"网络错误：{reason}")
                if attempt == attempts - 1:
                    raise last_error
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                raise LLMError(f"响应格式异常：{e}")
            time.sleep(min(2 ** attempt, 5))
        raise last_error or LLMError("未知错误")

    def _parse(self, body, started):
        obj = json.loads(body)
        choices = obj.get("choices") or []
        if not choices:
            raise LLMError(f"响应缺少 choices：{body[:200]}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            raise LLMError("响应缺少 message.content")
        usage = obj.get("usage") or {}
        return LLMResult(
            content=content,
            usage={
                "input": int(usage.get("prompt_tokens") or 0),
                "output": int(usage.get("completion_tokens") or 0),
            },
            model=obj.get("model") or self.cfg.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=(choices[0].get("finish_reason") or "") if choices else "",
        )


def main():
    args = sys.argv[1:]
    cfg = load_config()
    if "--check" in args or not args:
        print(json.dumps(cfg.public_dict(), ensure_ascii=False, indent=2))
        print("available =", cfg.available,
              "(set REVIEW_AGENT_LLM_API_KEY or use a local endpoint; otherwise rule-only)")
        return 0
    if not cfg.available:
        print("LLM 不可用：请设置 REVIEW_AGENT_LLM_API_KEY，或配置本地端点。")
        return 2
    client = LLMClient(cfg)
    result = client.chat([{"role": "user", "content": " ".join(args)}])
    print(result.content)
    print(json.dumps({"model": result.model, "usage": result.usage,
                      "latency_ms": result.latency_ms}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
