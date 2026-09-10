"""抓取端宽粗筛：只挡明显无关噪音，相关性交给 LLM。"""

from __future__ import annotations

import re

# 故意放宽：agreement/deal/supply/fiber 等即可进管线；LLM 再判是否 AI 产业链
BROAD_DEAL_PREFILTER = re.compile(
    r"("
    r"anthropic|openai|claude|gpt|xai|nvidia|agentforce|ai\s*agent|agentic|"
    r"\bai\b|inference|large\s*language\s*model|\bllm\b|generative\s*ai|copilot|"
    r"partnership|collaboration|integration|plugin|strategic|"
    r"agreement|contract|deal|supply|purchase|offtake|"
    r"data\s*center|datacenter|gpu|custom\s*semiconductor|hyperscale|"
    r"geothermal|\bppa\b|power\s+purchase|carbon-?free|"
    r"\bmegawatt|\bgigawatt|\b\d+\s*mw\b|"
    r"optical|fiber|dci|data\s*center\s*interconnect|long[- ]haul|"
    r"merger|acquisition|funding|"
    r"算力|数据中心|人工智能|地热|购电|电力协议|光纤|光模块|光通信|供应协议|合作"
    r")",
    re.I,
)
