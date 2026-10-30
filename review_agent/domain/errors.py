# -*- coding: utf-8 -*-
"""领域异常：所有「不变量被破坏」的出口。

分两类，调用方的处理方式不同：
    · InvariantViolation —— 数据本身不自洽（confidence=1.5、终态却没结束时间）。
      这是代码 bug 或外部脏数据，必须抛出，不能吞掉降级。
    · InvalidTransition —— 状态机不允许的迁移（created → completed）。
      这是流程 bug，要在测试里覆盖，不允许生产环境「先跑起来再说」。

旧实现的错误处理是 ValueError / KeyError / 裸 Exception 混用，server 侧只能
`except Exception` 一把抓。领域层把错误类型收敛后，上层才能区分「输入不可读」
（可回显给用户）与「系统不自洽」（该报警）。
"""
from __future__ import annotations


class DomainError(Exception):
    """领域层异常基类。

    三个类属性是给**上层**用的，不是给领域层用的：
        kind         分类（input / config / system）——决定提示语与能否让用户自己修
        http_status  对应的 HTTP 语义（400 / 404 / 500）——server 只需转发，不再猜
        retryable    同一输入重试是否有意义（超时/临时故障才算）

    以前这些判断散在各处：CLI 打印「找不到待审文件」后退出 1，server 把 stderr
    原样贴给前端，用户看不出「是我路径写错了」还是「你们服务坏了」。
    """

    kind = "system"
    http_status = 500
    retryable = False

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "http_status": self.http_status,
                "retryable": self.retryable, "message": str(self)}


class InvariantViolation(DomainError):
    """领域对象自洽性被破坏。"""


class InvalidTransition(DomainError):
    """状态机不允许的迁移。"""


class IdFormatError(DomainError, ValueError):
    """标识符格式不合法（R-20261030-001 / ISS-0001 / EV-0001 …）。"""


class AdapterError(DomainError):
    """外部数据（历史报告、manifest）无法映射为领域对象。带上下文，便于定位。"""

    def __init__(self, message: str, *, where: str = "", cause: Exception | None = None):
        super().__init__(f"{where}: {message}" if where else message)
        self.where = where
        self.cause = cause


class InputError(DomainError, ValueError):
    """输入问题：用户自己改一改就能解决（路径写错、文件不可读、格式不支持）。

    继承 ValueError 是为了兼容旧代码里的 `except ValueError`——这次只加分类，
    不改既有捕获行为。
    """

    kind = "input"
    http_status = 400


class NotFoundError(InputError):
    """输入指向的东西不存在。比 InputError 更具体，前端可以提示「检查路径」。"""

    http_status = 404
    # 404 不是「服务端出错」，同一路径重试没有意义
    retryable = False


class ConfigError(DomainError):
    """配置/部署问题：用户改不了，要维护者修（manifest 缺处理器、规格文件坏掉）。"""

    kind = "config"
    http_status = 500

