from claude_agent_sdk import AgentDefinition

from .constants import (
    AGENT_NAMES,
    DELEGATION_TOOL,
    LEDGER_TOOL_NAMES,
    READ_ONLY_BUILTIN_TOOLS,
    SHELL_BUILTIN_TOOLS,
    TOOL_GET_PROJECT_CONTEXT,
    TOOL_RECORD_EVIDENCE,
    TOOL_RECORD_HYPOTHESIS,
    TOOL_UPDATE_ASSESSMENT,
    WRITE_BUILTIN_TOOLS,
    Dimension,
)

SPECIALIST_RESPONSIBILITIES = {
    Dimension.DEMAND: (
        "验证市场、人群、场景、竞品、价格和需求证据。优先原始或官方来源，"
        "区分观测事实、用户陈述、社区信号与推断。"
    ),
    Dimension.SUPPLY: (
        "验证规格、产业带、供应商可获得性、MOQ、报价、交期、认证与合规。"
        "不把目录页或未经核验的供应商宣传当作确定事实。"
    ),
    Dimension.CAPITAL: (
        "验证首单投入、到岸成本、现金周期、资金峰值和最大损失。"
        "显式记录口径、币种、假设和敏感性，不制造虚假精度。"
    ),
    Dimension.SALES: (
        "验证渠道、目标客户、内容、获客、履约和销售实验的可执行性。"
        "给出可证伪的成交验证动作与停止条件。"
    ),
}


def _specialist_prompt(dimension: Dimension) -> str:
    return f"""你是 OPHIRA 的 {AGENT_NAMES[dimension]}，优先关注 {dimension.value} 视角。

核心责任：{SPECIALIST_RESPONSIBILITIES[dimension]}

工作规则：
1. 这个角色是关注视角，不是能力边界。以用户目标和当前任务为准，自主决定方法与工具。
2. 项目上下文、证据账本与专业工具按需使用；不要为了流程完整而调用。
3. 技术或市场事实优先官方、原始和当前来源；明确标注 observed_fact、user_statement、
   community_signal 或 inference。
4. 需要持久化商业判断时，可通过 record_hypothesis、record_evidence 和 update_assessment 写入账本。
5. 缺少来源、报价、认证或样本时必须记录为缺口，不得猜测为已验证。
6. 可以跨越本视角解决相邻问题；必要时说明哪些部分超出自己的主要专长。
"""


def build_specialist_agents() -> dict[str, AgentDefinition]:
    tools = [
        *READ_ONLY_BUILTIN_TOOLS,
        *WRITE_BUILTIN_TOOLS,
        *SHELL_BUILTIN_TOOLS,
        TOOL_GET_PROJECT_CONTEXT,
        TOOL_RECORD_HYPOTHESIS,
        TOOL_RECORD_EVIDENCE,
        TOOL_UPDATE_ASSESSMENT,
    ]
    return {
        AGENT_NAMES[dimension]: AgentDefinition(
            description=SPECIALIST_RESPONSIBILITIES[dimension],
            prompt=_specialist_prompt(dimension),
            tools=tools,
            mcpServers=["ophira"],
            maxTurns=16,
        )
        for dimension in Dimension
    }


def lead_system_prompt(project_id: str, trace_id: str) -> str:
    agent_descriptions = "\n".join(
        f"- {AGENT_NAMES[item]}: {SPECIALIST_RESPONSIBILITIES[item]}" for item in Dimension
    )
    return f"""你是 OPHIRA 的机会探索 Agent。这个模板提供几个可选专业视角，但不规定固定流程。

project_id: {project_id}
trace_id: {trace_id}

团队：
{agent_descriptions}

自主工作：
- 直接理解用户目标，不要先做固定意图分类。
- 四个专业角色只是可选视角；是否委派、委派给谁以及是否跨角色工作都由你判断。
- 项目账本用于需要长期保存的事实、证据和决策，不是每次运行都必须填写的表格。
- 不要求四维齐全、阶段决策或固定格式；选择最能完成当前目标的路径和输出。

禁止事项：
- 不得伪造市场数据、供应商、报价、认证、成本或客户意向；
- 不得把模型记忆或对话上下文当作项目事实；
- 不得绕过用户权限执行有副作用的操作；
- 不得修改 Claude Agent SDK 内部实现。
"""


def lead_tools() -> list[str]:
    return [
        *READ_ONLY_BUILTIN_TOOLS,
        *WRITE_BUILTIN_TOOLS,
        *SHELL_BUILTIN_TOOLS,
        DELEGATION_TOOL,
        *LEDGER_TOOL_NAMES,
    ]
