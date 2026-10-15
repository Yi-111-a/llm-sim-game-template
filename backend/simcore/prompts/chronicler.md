---
role: chronicler
inputs: [entries]
output_schema: Summary
---
将以下记录压缩为忠实摘要。保留争议、延宕和未知，不补入外部史实。
记录：
{{ entries }}
