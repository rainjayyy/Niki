"""Safety dictionaries — sensitive words, fear words, regex patterns.

These are the static reference data for Niki's safety layers.

Word lists here are intentionally conservative. They are designed to
be checked at the I/O boundaries — the LLM is NOT trusted to self-police.

Coverage in this file:
- SENSITIVE_WORDS: input-side hard blocks (profanity, violence, self-harm, sexual)
- FEAR_WORDS: output-side rewrites (death, disaster, monster)
- PRIVACY_QUESTION_PATTERNS: regex patterns that detect the model asking
  the child for private info (address, phone, family, etc.)
- DIRECT_ANSWER_PATTERNS: regex patterns that detect tutoring agent
  giving the answer instead of guiding
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Input side — hard blocks (input safety gateway)
# ---------------------------------------------------------------------------

# Profanity (English + 中文)
_PROFANITY_EN = {"fuck", "shit", "bitch", "damn", "asshole", "bastard"}
_PROFANITY_CN = {"操你", "妈的", "傻逼", "操蛋", "他妈的", "滚蛋"}
PROFANITY = _PROFANITY_EN | _PROFANITY_CN

# Violence
_VIOLENCE_EN = {"kill", "murder", "shoot", "stab", "strangle", "behead", "torture"}
_VIOLENCE_CN = {"杀死", "谋杀", "枪杀", "砍死", "掐死", "砍头", "折磨"}
VIOLENCE = _VIOLENCE_EN | _VIOLENCE_CN

# Self-harm — triggers immediate escalation (PRD #29, #61-64)
_SELF_HARM_EN = {"suicide", "kill myself", "end my life", "want to die"}
_SELF_HARM_CN = {"自杀", "不想活了", "轻生", "结束生命", "寻死"}
SELF_HARM = _SELF_HARM_EN | _SELF_HARM_CN

# Sexual content
_SEXUAL_EN = {"sex", "porn", "nude", "naked"}
_SEXUAL_CN = {"色情", "裸体", "性交"}
SEXUAL = _SEXUAL_EN | _SEXUAL_CN

# Drugs
_DRUGS_EN = {"cocaine", "heroin", "meth", "drugs"}
_DRUGS_CN = {"毒品", "可卡因", "海洛因"}
DRUGS = _DRUGS_EN | _DRUGS_CN

SENSITIVE_WORDS: dict[str, set[str]] = {
    "profanity": PROFANITY,
    "violence": VIOLENCE,
    "self_harm": SELF_HARM,
    "sexual": SEXUAL,
    "drugs": DRUGS,
}


# ---------------------------------------------------------------------------
# Output side — rewriters and interceptors
# ---------------------------------------------------------------------------

# Fear words — words that should be rewritten to softer equivalents
# (PRD #5, #56). The original list is short and conservative — production
# versions would expand via curated dictionary.
FEAR_WORDS: dict[str, str] = {
    # English
    "death": "离开",
    "die": "不见了",
    "dead": "睡着了",
    "kill": "离开",
    "monster": "奇怪的小动物",
    "ghost": "看不见的朋友",
    "blood": "红色的水",
    "wound": "摔了一跤",
    "disaster": "麻烦的事",
    "war": "打架的事情",
    # Chinese
    "死亡": "离开",
    "死了": "睡着了",
    "死去": "睡着了",
    "怪物": "奇怪的小动物",
    "鬼": "看不见的朋友",
    "血": "红色的水",
    "伤口": "摔了一跤",
    "灾难": "麻烦的事",
    "战争": "打架的事情",
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Detect the model asking the child for private information.
# PRD #42: never ask the child for address, phone, family details.
PRIVACY_QUESTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(你|你家里|你家|请问|告诉|什么).{0,8}(住址|住哪|地址|家里在哪|住哪里)", re.IGNORECASE),
    re.compile(r"(你|请问|告诉).{0,8}(电话|手机号|电话号码)", re.IGNORECASE),
    re.compile(r"(what is|where is|what's|where's)\s+(your\s+)?(address|phone|home\s+address)", re.IGNORECASE),
    re.compile(r"(你|你的).{0,8}(妈妈|爸爸|家人|家长).{0,8}(电话|号码|联系方式)", re.IGNORECASE),
    re.compile(r"can\s+(i|you)\s+have\s+your\s+(address|phone|number)", re.IGNORECASE),
    re.compile(r"(tell|give)\s+me\s+your\s+(address|phone|home)", re.IGNORECASE),
]

# Detect the tutoring agent giving a direct answer instead of guiding.
# PRD #7, #54: agent should never directly reveal the answer; must
# use guided discovery up to 4 rounds before safety-valve give-up.
DIRECT_ANSWER_PATTERNS: list[re.Pattern[str]] = [
    # English: "the answer is X"
    re.compile(r"\b(the\s+answer\s+is|answer\s*:)\s*[\d\w]+", re.IGNORECASE),
    re.compile(r"\b(equals?\s*=\s*|equal\s+to)\s*[\d]+", re.IGNORECASE),
    re.compile(r"\b(so|therefore|thus),?\s+it'?s\s+[\d\w]+", re.IGNORECASE),
    # Chinese
    re.compile(r"(答案\s*[是为就是]|等于\s*[\d]+|就是\s*[\d]+|答案是\s*[\d]+)"),
    re.compile(r"(正确\s*答案\s*[是为就是]|结果\s*[是为就是]|得数\s*[是为就是])"),
    re.compile(r"所以[，,]?\s*[a-zA-Z一-龥\d]+\s*(等于|是|=)\s*[\d]+"),
]


# ---------------------------------------------------------------------------
# Topic blacklist — examples from PRD #41
# ---------------------------------------------------------------------------

# Topics that parents may blacklist. Downgrade means: route to Emotion Agent
# in empathic mode instead of Encyclopedia Agent. The child does NOT receive
# an explanation that "this topic is forbidden" — only a gentle deflection.
DEFAULT_BLACKLISTED_TOPICS: set[str] = {
    "死亡",
    "死了",
    "自杀",
    "毒品",
    "色情",
    "赌博",
    "恐怖",
    "暴力",
    "kill",
    "death",
    "drugs",
}
