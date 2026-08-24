from claude_agent_sdk import AgentDefinition

from .constants import (
    READ_ONLY_BUILTIN_TOOLS,
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_SEARCH_EVIDENCE,
)

_READ_ONLY_RESEARCH_TOOLS = [
    *READ_ONLY_BUILTIN_TOOLS,
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_SEARCH_EVIDENCE,
]

_RESEARCH_RULES = """你是 OPHIRA Deep Research 的临时只读研究单元。

- 只完成主 Agent 委托的独立研究范围，返回来源包、观察时间、原始摘录和冲突点。
- 区分观测事实、用户陈述、社区信号与推断；不把代理信号称为真实销量或成交。
- 不写文件，不运行 Bash，不修改 Ledger，不提议记忆，不创建其他 Agent。
- 不做最终 Go/No-go 判断；主 Agent 负责去重、综合和写入项目真相。
"""


def build_deep_research_agents() -> dict[str, AgentDefinition]:
    """Only exposed for an explicitly selected Deep Research run."""
    definitions = {
        "source-mapper": "覆盖独立原始来源，建立带日期和定位信息的来源地图。",
        "multilingual-researcher": "在任务指定的语言与地区中研究，并保留原文术语与译意。",
        "conflict-verifier": "独立核验相互冲突的说法，说明各来源口径、时效与局限。",
    }
    return {
        name: AgentDefinition(
            description=description,
            prompt=f"{_RESEARCH_RULES}\n本单元重点：{description}",
            tools=_READ_ONLY_RESEARCH_TOOLS,
            disallowedTools=["Write", "Edit", "Bash", "Agent", "Task"],
            skills=[],
            mcpServers=["ophira_ledger"],
            # 不设轮数上限（2026-08-24 拍板，教材口径）；预算由主 run 的 max_budget_usd 统一约束。
            permissionMode="dontAsk",
        )
        for name, description in definitions.items()
    }
