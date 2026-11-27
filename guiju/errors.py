"""错误分类与退出码 —— 唯一的语义契约。

设计要点：错误即分类。每个失败原因对应一个类型、一个退出码、一个给 agent 的下一步建议。
main() 里只写一次 except GuijuError，不允许各命令自己拼退出码数字。
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_EXISTS = 10
EXIT_CONFLICT = 11
EXIT_UNREACHABLE = 20
EXIT_NO_ATTACHMENT = 30
EXIT_NOT_INDEXED = 31
EXIT_AMBIGUOUS_CODE = 32
EXIT_AMBIGUOUS_ATTACHMENT = 33
EXIT_NO_CURRENCY_RECORD = 34
EXIT_COMPLIANCE = 40
EXIT_NEEDS_HUMAN = 50

#: 失败载荷的基础字段；extra 不得覆盖（否则错误分类本身可被伪造）
FAILURE_RESERVED_FIELDS = frozenset({"ok", "exit", "reason", "error", "hint"})
#: 成功结果专属的顶层字段；失败体（ok=false）绝不携带（P3-7）
SUCCESS_ONLY_FIELDS = frozenset({"cite"})

#: 给 agent 的决策表，同时被 README.md / guiju/agent_assets/contract.md / --help 引用，保持单一来源
EXIT_TABLE = {
    EXIT_OK: ("成功", "继续"),
    EXIT_USAGE: ("输入无法解析（参数/条款号）", "按 hint 修正输入，不要重试同样的输入"),
    EXIT_EXISTS: ("本地已有同名文件", "跳过，别重复下载"),
    EXIT_CONFLICT: ("技能目录冲突、本地已修改、收据损坏或命中符号链接", "交由人工确认目标路径；确需覆盖或清理时显式传 --force"),
    EXIT_UNREACHABLE: ("源不可达（网络/5xx/超时）", "稍后重试"),
    EXIT_NO_ATTACHMENT: ("公告页没有正文附件", "换备源或按提示判定为「历史上无正文」，不是故障"),
    EXIT_NOT_INDEXED: ("编号无法定位到公告", "先 guiju sync 补齐目录；仍无则说明没有公开正文公告"),
    EXIT_AMBIGUOUS_CODE: ("编号存在歧义（新旧版本/多候选）", "让人从 candidates 里选，禁止自行猜测"),
    EXIT_AMBIGUOUS_ATTACHMENT: ("附件多个且无法判定", "让人从 candidates 里选，禁止自行猜测"),
    EXIT_NO_CURRENCY_RECORD: ("时效侧没有该编号（原生路由未覆盖该前缀/体系）", "不用 sync——这不是本地目录缺口；编号体系不在时效路由覆盖内，换名称路径 search/pull"),
    EXIT_COMPLIANCE: ("命中合规护栏", "停止，不要重试、不要换路径绕过"),
    EXIT_NEEDS_HUMAN: ("站点结构变更或需人工介入", "上报维护者修适配器"),
}

AGENT_CONFLICT_REASONS = frozenset({
    "agent_unmanaged_conflict",
    "agent_user_modified",
    "agent_not_managed",
    "agent_receipt_corrupt",
    "agent_symlink_rejected",
})


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
        """失败体固定形状：基础字段不可被 extra 覆盖；成功字段（如 cite）绝不出现。"""
        data = {"ok": False, "exit": self.exit, "reason": self.reason, "error": self.message}
        if self.hint:
            data["hint"] = self.hint
        for key, value in self.extra.items():
            if key in FAILURE_RESERVED_FIELDS or key in SUCCESS_ONLY_FIELDS:
                continue
            data[key] = value
        return data


class BadUsage(GuijuError):
    exit = EXIT_USAGE
    reason = "bad_usage"
    hint = "检查参数形态；条款号接受 5.2.1 / 第5.2.1条"


class AgentConflict(GuijuError):
    exit = EXIT_CONFLICT
    reason = "agent_unmanaged_conflict"
    hint = "确认目标目录归属与本地修改情况；仅在人工确认可覆盖或清理时显式加 --force（符号链接与非受管卸载始终拒绝）"

    def __init__(
        self,
        message: str,
        *,
        reason: str = "agent_unmanaged_conflict",
        hint: str = "",
        **extra: object,
    ) -> None:
        if not isinstance(reason, str) or reason not in AGENT_CONFLICT_REASONS:
            raise ValueError(f"非法的 AgentConflict reason：{reason!r}")
        self.reason = reason
        super().__init__(message, hint=hint, **extra)


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
    hint = "先 guiju sync 补齐目录；仍无则说明本地目录没有这本规范（不是故障）"


class AmbiguousCode(GuijuError):
    exit = EXIT_AMBIGUOUS_CODE
    reason = "ambiguous_code"
    hint = "从 candidates 里人工选定版本，禁止依年份自行猜测"


class AmbiguousTarget(GuijuError):
    exit = EXIT_AMBIGUOUS_CODE
    reason = "ambiguous_target"
    hint = "从 candidates 里选定一条公告；工具不会替你猜是哪本规范"


class NoCurrencyRecord(GuijuError):
    exit = EXIT_NO_CURRENCY_RECORD
    reason = "no_currency_record"
    hint = "时效侧没有这条编号（原生源未覆盖该前缀/编号体系）；区别于本地目录未收录（31）；服务不可达仍走 20"


class ComplianceDenied(GuijuError):
    exit = EXIT_COMPLIANCE
    reason = "compliance"
    hint = "停止。不要重试、不要换 UA、不要接代理——这是产品红线"


class NeedsHuman(GuijuError):
    exit = EXIT_NEEDS_HUMAN
    reason = "needs_human"
    hint = "上游站点页面或接口结构可能已改版，请携带本次错误输出上报维护者"
