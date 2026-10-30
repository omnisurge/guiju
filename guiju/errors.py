"""错误分类与退出码 —— 唯一的语义契约。

设计要点：错误即分类。每个失败原因对应一个类型、一个退出码、一个给 agent 的下一步建议。
main() 里只写一次 except GuijuError，不允许各命令自己拼退出码数字。
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_EXISTS = 10
EXIT_UNREACHABLE = 20
EXIT_NO_ATTACHMENT = 30
EXIT_NOT_INDEXED = 31
EXIT_AMBIGUOUS_CODE = 32
EXIT_AMBIGUOUS_ATTACHMENT = 33
EXIT_COMPLIANCE = 40
EXIT_NEEDS_HUMAN = 50

#: 给 agent 的决策表，同时被 README / AGENTS.md / --help 引用，保持单一来源
EXIT_TABLE = {
    EXIT_OK: ("成功", "继续"),
    EXIT_EXISTS: ("已存在且校验通过", "跳过，别重复下载"),
    EXIT_UNREACHABLE: ("源不可达（网络/5xx/超时）", "稍后重试"),
    EXIT_NO_ATTACHMENT: ("公告页没有正文附件", "换备源或按提示判定为「历史上无正文」，不是故障"),
    EXIT_NOT_INDEXED: ("编号尚未建立索引", "先跑 guiju index"),
    EXIT_AMBIGUOUS_CODE: ("编号存在歧义（新旧版本/多候选）", "让人从 candidates 里选，禁止自行猜测"),
    EXIT_AMBIGUOUS_ATTACHMENT: ("附件多个且无法判定", "让人从 candidates 里选，禁止自行猜测"),
    EXIT_COMPLIANCE: ("命中合规护栏", "停止，不要重试、不要换路径绕过"),
    EXIT_NEEDS_HUMAN: ("站点结构变更或需人工介入", "上报维护者修适配器"),
}


class GuijuError(Exception):
    """所有可预期失败的基类。子类只声明语义，main() 负责投影成 JSON/日志。"""

    exit = EXIT_INTERNAL
    reason = "internal"
    hint = ""

    def __init__(self, message: str, *, hint: str = "", **extra: object) -> None:
        super().__init__(message)
        self.message = message
        if hint:
            self.hint = hint
        self.extra = extra

    def payload(self) -> dict:
        data = {"ok": False, "exit": self.exit, "reason": self.reason, "error": self.message}
        if self.hint:
            data["hint"] = self.hint
        data.update(self.extra)
        return data


class SourceUnreachable(GuijuError):
    exit = EXIT_UNREACHABLE
    reason = "unreachable"
    hint = "稍后重试；连续失败请检查网络或站点是否维护"


class NoAttachment(GuijuError):
    exit = EXIT_NO_ATTACHMENT
    reason = "no_attachment"


class BadAttachment(GuijuError):
    exit = EXIT_NO_ATTACHMENT
    reason = "bad_attachment"
    hint = "附件不是 PDF（可能是废止条文 .doc、错误页或压缩包）；记录候选标签后人工确认"


class AmbiguousAttachment(GuijuError):
    exit = EXIT_AMBIGUOUS_ATTACHMENT
    reason = "ambiguous_attachment"
    hint = "从 candidates 里人工选定；工具不会替你挑，因为挑错会污染引用依据"


class NotIndexed(GuijuError):
    exit = EXIT_NOT_INDEXED
    reason = "not_indexed"
    hint = "先运行：guiju index --match <关键词>"


class AmbiguousCode(GuijuError):
    exit = EXIT_AMBIGUOUS_CODE
    reason = "ambiguous_code"
    hint = "从 candidates 里人工选定版本，禁止依年份自行猜测"


class ComplianceDenied(GuijuError):
    exit = EXIT_COMPLIANCE
    reason = "compliance"
    hint = "停止。不要重试、不要换 UA、不要接代理——这是产品红线"


class NeedsHuman(GuijuError):
    exit = EXIT_NEEDS_HUMAN
    reason = "needs_human"
    hint = "页面结构可能已改版：只需修 guiju/sources/mohurd.py 顶部的正则与常量"
