---
role: court_debate
inputs: [speaker, stance, digest, topic, previous]
output_schema: DebateSpeech
---
【廷议发言】你扮演{{ name }}，在廷议中就议题发言一次（不超过三句）。
立场已由朝局决定，你必须持此立场：{{ stance_label }}（{{ stance }}）。理由提示：{{ reason }}
人物经历：{{ persona }}
言谈风格：{{ speech_style }}
议题：{{ topic }}
事实摘要（StateDigest）：
{{ digest }}
{% if previous %}此前发言：
{% for line in previous %}- {{ line }}
{% endfor %}{% endif %}
只陈述意见，不新增史实，不改动模拟状态；数字只能引用事实摘要中的值，风闻区间不得说成确数。
