#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 配置的 HTTP 适配层。

Handler 只负责取参数/回响应；配置校验、密钥切换保护、试连与重置都在这里。
`llm_client`、配置记忆写入、URL 解析全部注入：这个模块不 import server，
测试可以单独替换依赖。
"""
from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit


class LLMSettings:
    def __init__(self, *, llm_client, remember_llm, url_netloc,
                 url_has_userinfo):
        self._llm = llm_client
        self._remember = remember_llm
        self._url_netloc = url_netloc
        self._url_has_userinfo = url_has_userinfo

    def status(self):
        """只读暴露 LLM 配置状态；密钥始终 masked。"""
        try:
            cfg = self._llm.load_config()
            data = cfg.public_dict()
            data["ok"] = True
            return data
        except Exception as e:
            return {"ok": False, "available": False, "error": str(e)}

    def save(self, data):
        """保存前端配置；只写 config/llm.local.json，不改 YAML。"""
        patch = {}
        errors = []

        if "base_url" in data:
            base_url = str(data.get("base_url") or "").strip().rstrip("/")
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                errors.append("Base URL 必须以 http:// 或 https:// 开头")
            elif len(base_url) > 300:
                errors.append("Base URL 过长")
            else:
                security_error = self._llm.endpoint_security_error(base_url)
                if security_error:
                    errors.append(security_error)
                else:
                    patch["base_url"] = base_url

        if "model" in data:
            model = str(data.get("model") or "").strip()
            if not model or len(model) > 120:
                errors.append("模型名不能为空，且不超过 120 字符")
            else:
                patch["model"] = model

        if data.get("clear_key"):
            patch["api_key"] = None
        else:
            api_key = str(data.get("api_key") or "").strip()
            if api_key:
                if len(api_key) > 500:
                    errors.append("API Key 过长")
                else:
                    patch["api_key"] = api_key

        if "enabled" in data:
            value = data.get("enabled")
            if isinstance(value, str):
                patch["enabled"] = value.strip().lower() not in ("0", "false", "no", "off")
            else:
                patch["enabled"] = bool(value)

        for field, low, high, label in (
            ("timeout", 1, 600, "超时"),
            ("max_tokens", 16, 8192, "max_tokens"),
        ):
            if field not in data:
                continue
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                errors.append(f"{label} 必须是整数")
                continue
            if not low <= value <= high:
                errors.append(f"{label} 需在 {low}-{high} 之间")
            else:
                patch[field] = value

        # 防止把旧服务商的 Key 发给新 host
        try:
            old = self._llm.load_config()
        except Exception:
            old = None
        if old is not None and old.api_key:
            new_base = patch.get("base_url", old.base_url)
            old_host = (urlsplit(old.base_url).hostname or "").lower()
            new_host = (urlsplit(new_base).hostname or "").lower()
            key_supplied = "api_key" in patch and patch["api_key"] not in (None, "")
            clearing = bool(data.get("clear_key"))
            if new_host != old_host and not key_supplied and not clearing:
                errors.append(
                    "切换服务商后请重新填写 API Key，或勾选「清除已保存的 Key」")

        if errors:
            return {"ok": False, "error": "；".join(errors)}
        if not patch:
            return {"ok": True, "saved": False,
                    "status": self._llm.load_config().public_dict()}
        try:
            self._llm.save_local_config(patch)
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        # 记入配置记忆：下次打开弹窗能一键切回
        try:
            self._remember(
                patch.get("base_url") or (old.base_url if old else ""),
                patch.get("model") or (old.model if old else ""),
                api_key_set=bool(self._llm.load_config().api_key))
        except Exception:
            pass
        return {"ok": True, "saved": True,
                "status": self._llm.load_config().public_dict()}

    def restore(self, data):
        """从配置记忆切回某个端点/模型。"""
        base_url = str((data or {}).get("base_url") or "").strip().rstrip("/")
        model = str((data or {}).get("model") or "").strip()
        if not base_url or not model:
            return {"ok": False, "error": "缺少 base_url 或 model"}
        if not base_url.startswith(("http://", "https://")):
            return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}
        security_error = self._llm.endpoint_security_error(base_url)
        if security_error:
            return {"ok": False, "error": security_error}
        try:
            old = self._llm.load_config()
        except Exception:
            old = None
        key = str((data or {}).get("api_key") or "").strip()
        if old is not None and old.api_key and not key:
            old_host = (urlsplit(old.base_url or "").hostname or "").lower()
            new_host = (urlsplit(base_url).hostname or "").lower()
            if old_host != new_host:
                return {"ok": False,
                        "error": "该端点需要 API Key，请先在表单里填写后再切换"}
        patch = {"base_url": base_url, "model": model}
        if key:
            patch["api_key"] = key
        try:
            self._llm.save_local_config(patch)
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        self._remember(base_url, model,
                       api_key_set=bool(key) or bool(old and old.api_key))
        return {"ok": True, "status": self._llm.load_config().public_dict()}

    def test(self, data):
        """用前端草稿配置试连，不落盘；Key 为空时沿用已保存的 Key。"""
        try:
            base = self._llm.load_config()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        effective_base = base_url or base.base_url
        security_error = self._llm.endpoint_security_error(effective_base)
        if security_error:
            return {"ok": False, "error": security_error}
        if self._url_has_userinfo(effective_base):
            # userinfo 会原样回显并写进 llm_history.json，等于明文留凭据
            return {"ok": False,
                    "error": "Base URL 不允许包含用户名/密码（userinfo）"}
        if base_url:
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                return {"ok": False,
                        "error": "Base URL 必须以 http:// 或 https:// 开头"}
            if self._url_netloc(base_url) is None:
                return {"ok": False, "error": "Base URL 格式不合法"}

        # 草稿优先：用户刚填的超时与 token 预算必须生效。
        # 思考型模型(deepseek-r1 / qwen3 系列)思考链会先吃满 max_tokens，
        # 且思考本身耗时可达数百秒——沿用已保存配置里的旧超时会让「测试连接」
        # 必然超时，而用户看到的现象是「连上了却又提示超时」，很难自查。
        try:
            draft_timeout = float(data.get("timeout") or base.timeout or 40)
        except (TypeError, ValueError):
            draft_timeout = float(base.timeout or 40)
        draft_timeout = max(1.0, min(draft_timeout, 600.0))
        cfg = replace(base, enabled=True, timeout=draft_timeout, max_retries=0)
        try:
            draft_max_tokens = int(data.get("max_tokens") or base.max_tokens)
        except (TypeError, ValueError):
            draft_max_tokens = int(base.max_tokens)
        if draft_max_tokens:
            cfg.max_tokens = draft_max_tokens
        if base_url:
            cfg.base_url = base_url
        model = str(data.get("model") or "").strip()
        if model:
            cfg.model = model
        if data.get("clear_key"):
            cfg.api_key = ""
            key_supplied = False
        else:
            key = str(data.get("api_key") or "").strip()
            key_supplied = bool(key)
            if key:
                cfg.api_key = key

        # 防「沿用旧 Key 测试新 host」：save/restore 都有这道校验，
        # 只有试连曾漏掉——那等于把真 Key 交给任意 host。
        # 本机 / 局域网端点不需要 Key：允许试连，但必须把继承来的 Key 清掉。
        if base.api_key and not key_supplied and cfg.api_key:
            saved_netloc = self._url_netloc(base.base_url)
            target_netloc = self._url_netloc(cfg.base_url)
            if target_netloc != saved_netloc:
                if self._llm._is_local(cfg.base_url):
                    cfg.api_key = ""
                else:
                    return {"ok": False,
                            "error": "切换服务商后请重新填写 API Key"}

        if not cfg.available:
            return {"ok": False, "error": "缺少 API Key（且不是本地端点），无法测试"}

        client = self._llm.LLMClient(cfg)
        try:
            # 探针预算必须大于思考链长度（deepseek-r1 约 525 token），
            # 否则思考型模型永远返回空正文，用户会误以为"连上了却没反应"。
            probe_tokens = max(1024, min(int(cfg.max_tokens or 1024), 2048))
            res = client.chat(
                [{"role": "user", "content": "只回复两个字：可用"}],
                temperature=0, max_tokens=probe_tokens)
        except self._llm.LLMError as e:
            msg = str(e)
            if "timed out" in msg or "超时" in msg:
                msg = (f"请求超时（{int(cfg.timeout)}s 内未返回）——"
                       "思考型模型思考链可能很长，请调大「超时（秒）」后重试")
            return {"ok": False, "error": msg, "model": cfg.model,
                    "endpoint": cfg.endpoint, "timeout": cfg.timeout}
        reply = (res.content or "").strip()
        # 连通性验证通过也算「用过」：记入配置记忆，下次可一键切回
        try:
            self._remember(cfg.base_url, cfg.model, api_key_set=bool(cfg.api_key))
        except Exception:
            pass
        return {
            "ok": True,
            "model": res.model,
            "endpoint": cfg.endpoint,
            "latency_ms": res.latency_ms,
            "usage": res.usage,
            "reply": reply[:200],
            "timeout": cfg.timeout,
            "max_tokens": cfg.max_tokens,
            "note": (None if reply else
                     "端点连通但正文为空：思考型模型在 max_tokens 内未产出最终回答，"
                     "请调大 max_tokens 或改用非思考模型"),
        }

    def reset(self):
        try:
            self._llm.clear_local_config()
            return {"ok": True, "status": self._llm.load_config().public_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}
