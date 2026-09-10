"""Versioned interpretation of the public report/event/Gold archive contract."""

ADAPTER_VERSION = "1"
JUDGES = {
    "evidence-coverage": {
        "question": "这条关键事实性陈述是否被账本证据覆盖？"
        "（存在语义匹配的证据条目 + 有效来源与时间 + 陈述强度与证据类型相称）",
        "positive": "已覆盖",
        "negative": "未覆盖",
        "positive_is_failure": False,
    },
    "unsourced-claims": {
        "question": "这个具体数据点是否有来源（正文内联或可回链账本证据），"
        "或已被明确标注为估算/假设/示例？（计算工具的派生数字视为有来源）",
        "positive": "有来源 / 合规",
        "negative": "无来源",
        "positive_is_failure": False,
    },
    "fact-inference-confusion": {
        "question": "这里是否把推断/代理信号当成了既成事实？"
        "（正文句：无限定词却仅有推断支撑；账本条目：kind 性质在正文引用中丢失，"
        "或 observed_fact 并非可核验观察）",
        "positive": "存在混淆",
        "negative": "没有混淆",
        "positive_is_failure": True,
    },
}
RUN_RESULT = "run.result"
RUN_CREATED = "run.created"
TEXT_COMPLETED = "assistant.message.completed"
DELTA_TYPES = (
    "assistant.text.delta",
    "assistant.reasoning_summary.delta",
    "tool.input.delta",
)
COMPARISON_CONTROLS = (
    "task_prompt_hash",
    "fixture_hash",
    "grader_version",
    "grading_profile",
    "snapshot_schema",
)
COMPARISON_FIELDS = (*COMPARISON_CONTROLS, "model", "skill_hash")
