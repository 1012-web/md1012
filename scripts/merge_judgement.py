# 合并各批判定（judgement-b1.json、judgement-b2.json……，批数不限）为 judgement.json，
# 核对字段与编号，再按「改法」分组列出要改字的条目（B/C，把握高）、原书误植、把握低的条目。
# 用法：python -X utf8 merge_judgement.py <输出目录>
# 结果格式见 references/judging-prompt.md。编号重复、缺号、字段不全都会列出来并以退出码 1 结束，不写 judgement.json。
import collections
import json
import re
import sys
from pathlib import Path

REQUIRED = ("n", "minerU", "ocr", "verdict", "confidence", "reason")
VERDICTS = {"A", "B", "C", "D"}


def batches(folder: Path) -> list[Path]:
    found = [(int(m.group(1)), p) for p in folder.glob("judgement-b*.json")
             if (m := re.fullmatch(r"judgement-b(\d+)\.json", p.name))]
    return [p for _, p in sorted(found)]


def problems_of(items: list[dict], manifest: dict[int, dict]) -> list[str]:
    problems = []
    seen = collections.Counter(item.get("n") for item in items)
    problems += [f"#{n} 在各批里出现 {count} 次" for n, count in sorted(seen.items(), key=lambda kv: str(kv[0])) if count > 1]
    problems += [f"#{n} 没有判定" for n in sorted(set(manifest) - set(seen))]
    problems += [f"#{n} 不在 manifest 里" for n in sorted(set(seen) - set(manifest), key=str)]
    for item in items:
        n = item.get("n")
        lacking = [key for key in REQUIRED if key not in item]
        if lacking:
            problems.append(f"#{n} 缺字段 {lacking}")
            continue
        if item["verdict"] not in VERDICTS:
            problems.append(f"#{n} verdict 应为 A/B/C/D，实为 {item['verdict']!r}")
        if item["confidence"] not in ("高", "低"):
            problems.append(f"#{n} confidence 应为 高/低，实为 {item['confidence']!r}")
        if item["verdict"] == "C" and not item.get("correct"):
            problems.append(f"#{n} 判 C 却没写 correct")
    return problems


def target(item: dict) -> str:
    variant = item.get("variant") or {}
    return variant.get("standard") or (item["ocr"] if item["verdict"] == "B" else item["correct"])


def main() -> None:
    folder = Path(sys.argv[1])
    manifest = {item["n"]: item for item in json.loads((folder / "manifest.json").read_text("utf-8"))}
    files = batches(folder)
    if not files:
        raise SystemExit(f"{folder} 里没有 judgement-bN.json")
    merged = []
    for path in files:
        merged.extend(json.loads(path.read_text("utf-8")))
    problems = problems_of(merged, manifest)
    print("batches", len(files), "items", len(merged))
    if problems:
        print("判定结果有问题，未写 judgement.json：")
        for line in problems:
            print("  " + line)
        raise SystemExit(1)
    merged.sort(key=lambda item: item["n"])
    (folder / "judgement.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), "utf-8")

    change = [i for i in merged if i["verdict"] in ("B", "C") and i["confidence"] == "高"]
    groups = collections.Counter((i["minerU"], target(i)) for i in change)
    print("要改字（把握高）", len(change))
    for (before, after), count in groups.most_common():
        sample = next(i for i in change if i["minerU"] == before and target(i) == after)
        print(f"  {before}→{after} ×{count}  例 {sample.get('page', '')} {manifest[sample['n']]['context']}")
    misprints = [i for i in merged if i.get("misprint") and i["confidence"] == "高"]
    print("原书误植（照录出校记）", [(i.get("page", ""), manifest[i["n"]]["context"], i["misprint"]) for i in misprints])
    low = [i for i in merged if i["confidence"] != "高" or i["verdict"] == "D"]
    print("把握低", len(low))
    for i in low:
        print(f"  #{i['n']} {i.get('page', '')} PDF{manifest[i['n']]['pdfPage']} {manifest[i['n']]['context']} "
              f"正文「{i['minerU']}」 判{i['verdict']}→「{i.get('correct') or ''}」 {i['reason'][:40]}")


if __name__ == "__main__":
    main()
