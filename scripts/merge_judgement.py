# 合并 6 批判定为 judgement.json，按「改法」分组列出要改字的条目（B/C，把握高），以及把握低的条目
import collections
import json
import sys
from pathlib import Path

d = Path(sys.argv[1])
manifest = {i["n"]: i for i in json.loads((d / "manifest.json").read_text("utf-8"))}
merged = []
for k in range(1, 7):
    merged.extend(json.loads((d / f"judgement-b{k}.json").read_text("utf-8")))
merged.sort(key=lambda i: i["n"])
missing = sorted(set(manifest) - {i["n"] for i in merged})
(d / "judgement.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), "utf-8")
print("merged", len(merged), "missing", missing)
change = [i for i in merged if i["verdict"] in ("B", "C") and i["confidence"] == "高"]
groups = collections.Counter((i["minerU"], (i.get("variant") or {}).get("standard") or (i["ocr"] if i["verdict"] == "B" else i["correct"])) for i in change)
print("要改字（把握高）", len(change))
for (a, b), n in groups.most_common():
    sample = next(i for i in change if i["minerU"] == a)
    print(f"  {a}→{b} ×{n}  例 {sample['page']} {manifest[sample['n']]['context']}")
mis = [i for i in merged if i.get("misprint") and i["confidence"] == "高"]
print("原书误植（照录出校记）", [(i["page"], manifest[i["n"]]["context"], i["misprint"]) for i in mis])
low = [i for i in merged if i["confidence"] != "高" or i["verdict"] == "D"]
print("把握低", len(low))
for i in low:
    print(f"  #{i['n']} {i['page']} PDF{manifest[i['n']]['pdfPage']} {manifest[i['n']]['context']} 正文「{i['minerU']}」 判{i['verdict']}→「{i.get('correct') or ''}」 {i['reason'][:40]}")
