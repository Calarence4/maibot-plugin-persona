"""词表、权重与内置风格素材。

词表全部可用配置覆盖（见 :class:`persona.settings` 与 ``config.toml``），这里是安全默认值。
所有评分都是纯规则，禁止 Embedding / 本地模型 / 记忆 LLM。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 情绪与意图（《方案.md》§5）
# ---------------------------------------------------------------------------

EMOTIONS: tuple[str, ...] = (
    "happy",
    "excited",
    "calm",
    "sad",
    "angry",
    "wronged",
    "shy",
    "nervous",
    "bored",
    "jealous",
)

INTENTS: tuple[str, ...] = (
    "chat",
    "greeting",
    "question",
    "comfort",
    "encourage",
    "tease",
    "rant",
    "coquetry",
    "refute",
    "explain",
    "apologize",
    "care",
    "complain",
    "close",
)

EMOTION_LEXICON: dict[str, tuple[str, ...]] = {
    "happy": ("开心", "高兴", "快乐", "好耶", "太好了", "哈哈", "嘿嘿", "笑死", "幸福", "喜欢"),
    "excited": ("激动", "兴奋", "啊啊啊", "冲冲冲", "迫不及待", "期待", "！！", "好想"),
    "calm": ("还好", "平静", "没事", "嗯嗯", "好的", "还行", "随便", "都行"),
    "sad": ("难过", "伤心", "哭", "委屈", "失落", "想哭", "呜呜", "不开心", "低落", "难受"),
    "angry": ("生气", "气死", "烦死", "讨厌", "滚", "无语", "离谱", "过分", "恼火", "凭什么"),
    "wronged": ("冤枉", "不是我的错", "我没错", "凭什么怪我", "委屈", "被误会"),
    "shy": ("害羞", "脸红", "不好意思", "别看", "讨厌啦", "羞", "害"),
    "nervous": ("紧张", "害怕", "担心", "慌", "忐忑", "不敢", "怕"),
    "bored": ("无聊", "没意思", "好闲", "发呆", "没劲", "困"),
    "jealous": ("吃醋", "嫉妒", "凭什么她", "凭什么他", "酸了", "羡慕"),
}

INTENT_LEXICON: dict[str, tuple[str, ...]] = {
    "greeting": ("你好", "早安", "晚安", "在吗", "早上好", "晚上好", "嗨", "hello", "hi"),
    "question": ("吗", "？", "?", "为什么", "怎么", "什么", "哪", "多少", "是不是", "能不能"),
    "comfort": ("抱抱", "摸摸", "别难过", "别哭", "我在", "陪你", "安慰"),
    "encourage": ("加油", "你可以", "相信你", "冲", "支持你", "会好的"),
    "tease": ("逗你", "开玩笑", "笨蛋", "傻瓜", "哈哈", "笑你"),
    "rant": ("烦死了", "气死", "受不了", "吐槽", "崩溃", "垃圾"),
    "coquetry": ("嘛", "啦", "人家", "要抱", "亲亲", "贴贴", "哼", "不要嘛"),
    "refute": ("才不是", "不是这样", "你错了", "反驳", "才没有", "瞎说"),
    "explain": ("因为", "所以", "原因是", "其实", "是这样"),
    "apologize": ("对不起", "抱歉", "我错了", "原谅我", "不好意思"),
    "care": ("多喝热水", "早点睡", "别熬夜", "注意身体", "吃饭了吗", "照顾好自己"),
    "complain": ("好累", "不想", "讨厌", "烦", "难受", "委屈"),
    "close": ("拜拜", "再见", "睡了", "下线", "先这样", "晚安啦"),
}

# ---------------------------------------------------------------------------
# 重要度信号（《方案.md》§3.3）
# ---------------------------------------------------------------------------

IMPORTANCE_BASE = 8
IMPORTANCE_OWNER_PRIVATE = 42
IMPORTANCE_OWNER_IN_GROUP = 32
IMPORTANCE_DIRECT = 8
IMPORTANCE_EMOTION_MAX = 24
IMPORTANCE_FIRST = 30
IMPORTANCE_PROMISE = 16
IMPORTANCE_CONFLICT = 14
IMPORTANCE_ABOUT_SELF = 5
IMPORTANCE_TOO_SHORT = -12

PROMISE_WORDS: tuple[str, ...] = (
    "约定",
    "答应",
    "保证",
    "说好",
    "下次",
    "以后",
    "一定",
    "承诺",
    "约好",
    "等你",
)

CONFLICT_WORDS: tuple[str, ...] = (
    "对不起",
    "道歉",
    "和好",
    "吵架",
    "误会",
    "原谅",
    "矛盾",
    "冷战",
    "闹别扭",
    "生气",
)

ABOUT_SELF_WORDS: tuple[str, ...] = (
    "我是",
    "我喜欢",
    "我讨厌",
    "我害怕",
    "我想要",
    "我的生日",
    "我的名字",
    "我记得",
    "我不喜欢",
    "我习惯",
)

# 采集过滤：群聊里带这些信号才落盘（可被配置词表覆盖）
GROUP_SIGNAL_WORDS: tuple[str, ...] = (
    *EMOTION_LEXICON["sad"],
    *EMOTION_LEXICON["angry"],
    *EMOTION_LEXICON["happy"],
    *PROMISE_WORDS,
    *CONFLICT_WORDS,
    "生日",
    "纪念日",
    "第一次",
    "去世",
    "生病",
    "结婚",
    "分手",
    "考试",
    "约定",
)

# 首次相遇概率（《方案.md》§3.5）：0–9 / 10–19 / 20–29 / ≥30 天
INITIAL_TIER_DAYS: tuple[int, ...] = (10, 20, 30)
INITIAL_TIER_PROBABILITIES: tuple[float, ...] = (0.90, 0.55, 0.30, 0.03)

# 清晰度阈值（《方案.md》§3.4）
CLARITY_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("vivid", 80.0),
    ("clear", 55.0),
    ("fuzzy", 30.0),
    ("trace", 10.0),
)

CLARITY_ORDER: tuple[str, ...] = ("vivid", "clear", "fuzzy", "trace", "forgotten")

# ---------------------------------------------------------------------------
# 内置风格
# ---------------------------------------------------------------------------

FALLBACK_STYLE_ID = "fallback"
CASUAL_STYLE_ID = "casual"

FALLBACK_STYLE: dict[str, object] = {
    "id": "fallback",
    "name": "自然",
    "description": "默认兜底风格：自然、口语、不过度解释。",
    "emotions": list(EMOTIONS),
    "intents": list(INTENTS),
    "scenes": ["any"],
    "priority": 0,
    "prompt": (
        "保持自然口语，像真人聊天一样短句回复。不要解释自己在用什么风格，"
        "不要复述设定，不要写旁白或括号动作。"
    ),
    "reference": [
        "标签: chat\n嗯，我在。",
        "标签: comfort\n抱抱，先别想那么多。",
        "标签: close\n好啦，早点睡。",
    ],
}

CASUAL_STYLE: dict[str, object] = {
    "id": "casual",
    "name": "日常",
    "description": "日常闲聊：轻松、短、有情绪但不夸张。",
    "emotions": ["calm", "happy", "bored"],
    "intents": ["chat", "greeting", "tease", "coquetry", "close"],
    "scenes": ["private", "group"],
    "priority": 10,
    "prompt": "轻松日常地接话，句子短一点，可以带一点语气词，但不要刻意卖萌或堆表情。",
    "reference": [
        "标签: chat,tease\n你怎么又这样啊，笑死。",
        "标签: greeting\n在的在的，怎么啦？",
        "标签: close\n那我先去忙啦，晚点找你。",
    ],
}

DEFAULT_STYLE_INDEX_HEADER = """# 风格索引

> 本文件只作为风格选择器的输入，不是 replyer 常驻上下文。

| id | 名称 | 情绪 | 意图 | 场景 | 优先级 |
| --- | --- | --- | --- | --- | ---: |
"""
