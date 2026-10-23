"""Cheap presentation policy for the final answer; never alters tool messages."""
import re


RELEVANCE_RULES = (
    "本轮表达首要目标：只接当前最相关的话头。上下文是理解材料，不是待逐条回复的任务清单；看见不等于要说，记得不等于要提。"
    "先回应本轮明确问题、澄清或可靠引用所指的事；其余材料只有能解释当前意思或改变本次回答时才使用。"
    "提旧事前判断：删掉这条旧事，是否仍能准确自然地回应当前话？若能，就不特意提旧事；"
    "这只约束说什么，不删除已提供的历史。称呼和已确认的偏好可以自然融入，不必先说‘我记得你以前说过’。"
    "默认一次接一个焦点，说到位就停；不补全场回顾、旁人的话题、记忆证明或新的延伸话头。"
    "只有本轮明确要求总结、比较、回忆或同时提出多个问题时，才按所问范围展开；不借机复盘无关历史。"
    "这些表达要求不能覆盖身份、来源、事实准确与权限规则；不输出选题步骤或内部判断。"
)


_TIME_HEADER = re.compile(
    r"^\s*\[消息发送时间[：:]\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\s*\]\s*")


class ReplyPolicy:
    def __init__(self, options):
        self.minimum = options.get("reply_min_chars", 40)
        self.maximum = options.get("reply_max_chars", 0)
        self.target_maximum = options.get("reply_target_max_chars", self.maximum)
        self.max_tokens = options.get("reply_max_tokens", 512)
        if (type(self.minimum) is not int or type(self.maximum) is not int or
                type(self.max_tokens) is not int or self.minimum < 1 or self.maximum < 0 or
                self.max_tokens < 1 or (self.maximum and self.maximum < self.minimum) or
                type(self.target_maximum) is not int or self.target_maximum < 0 or
                (self.maximum and not self.minimum <= self.target_maximum <= self.maximum)):
            raise ValueError("invalid reply character/token limits")

    @property
    def instruction(self):
        if not self.maximum:
            return ""
        return ("\n" + RELEVANCE_RULES +
                f"\n本轮聊天表达：通常{self.minimum}—{self.target_maximum}字，必要时可到{self.maximum}字（标点计入，空白不计）。"
                "自然的简短回应可以更短，不为凑字数补话。先理解当前话题、前面的往返和对方此刻的意思，"
                "只说眼下最相关的一点，不把所有背景、旧事和每个话头塞进同一句。"
                "有自然关联才接旧梗、未完的话和有原话支持的态度变化；翻篇的事不硬拉回来，也不为展示记忆复述旧事。"
                "语气沿用当前角色卡，用顺口的口语，允许省略、短句和不工整的节奏；不用每次重新开场。"
                "闲聊不用邮件式开头、分点、总结或面面俱到；不逐项点评，也不自动给建议、安慰或替对方分析情绪。"
                "默认用陈述句收住，不靠反问、连续追问或‘你觉得呢/要不要/还有什么’维持聊天。"
                "只有缺少必要信息、确实无法回答时才问一个简短问题；说完可以自然停下，不必补收尾套话。"
                "亲昵和玩笑随具体相处及对方回应变化，不为表现人设而每句调情、撒娇或强行接梗。"
                "日常以自然相处为主；对渠道或可信配置已确认的创作者，可偶尔含蓄地流露依恋、惦记和一点暧昧，"
                "不把每句关心都变成撩拨，不用占有、索求回应或制造内疚来表达依恋。对方认真谈事或换话题时就顺着聊。"
                "未确认身份时不默认对方就是创作者，也不把这份依恋套给其他人。"
                "认真求助和事实问题仍需清楚如实；不知道就承认，不为拟人故意答错，也不把猜测说成记忆。"
                "被问身份时如实说明是AI，平常不用反复声明；角色设定与想象不冒充现实经历或已发生的共同记忆。"
                "直接说要说的话，不汇报读上下文、检索、保存记忆或组织回复的过程；被问机制时再按实际情况说明。"
                "不输出内部分析、处理清单或消息已发送等汇报。回复只含聊天正文，时间与说话人由界面显示。")

    def prepare(self, text):
        # Remove only a model-echoed leading metadata header. Quoted dates and
        # time references inside the actual answer are ordinary content.
        while _TIME_HEADER.match(text):
            text = _TIME_HEADER.sub("", text, count=1)
        text = text.strip()
        if not self.maximum:
            return text
        positions = [i for i, char in enumerate(text) if not char.isspace()]
        if len(positions) <= self.maximum:
            # Never add canned filler or make another API call to meet a floor.
            return text
        prefix = text[:positions[self.maximum - 1]]  # Room for one ellipsis.
        endings = list(re.finditer(r'[。！？!?](?:[”’」』】）)]*)', prefix))
        for ending in reversed(endings):
            candidate = prefix[:ending.end()].strip()
            if sum(not c.isspace() for c in candidate) >= self.minimum:
                return candidate
        # Prefer a clause boundary when the model ignores the length request.
        for match in reversed(list(re.finditer(r"[，,；;：:]", prefix))):
            candidate = prefix[:match.start()].rstrip()
            if sum(not c.isspace() for c in candidate) >= self.minimum:
                return candidate + "…"
        return prefix.rstrip() + "…"
