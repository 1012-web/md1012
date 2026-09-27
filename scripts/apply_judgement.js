// 在 Obsidian 里执行（经 Obsidian 命令行 eval 读入本文件）：按「AI 看原图判定」的结果改正文、写账本。
// 用法（先设参数再读入本文件）：
//   window.__judgement = { dir: "C:/Users/yan/.mineru-pdf-sync/doubt-trial", variants: "normalize" | "keep"（必填，按该书选定的异体字方针）, by: "AI 看原图判定" };
//   eval(require("fs").readFileSync("<本文件>", "utf8"))
// dir 里要有 doubt_crops.py 写的 manifest.json 与判定结果 judgement.json（格式见 references/ai-judging.md）。
// 结果写在 window.__judgementResult（JSON 字符串）。需要笔记1012 插件（note1012）的 PDF 模块。
(() => {
  window.__judgementResult = "running";
  const options = window.__judgement || {};
  (async () => {
    const fs = require("fs");
    const path = require("path");
    const dir = options.dir;
    // 异体字方针由站长每本书开工时选定（SKILL.md「异体字方针」），不设默认：没给或给错就停，不替站长选。
    if (options.variants !== "keep" && options.variants !== "normalize") {
      throw new Error(`variants must be "keep" or "normalize" (the book's chosen variant policy), got ${JSON.stringify(options.variants)}`);
    }
    const variants = options.variants;
    const by = options.by || "AI 看原图判定";
    const today = new Date().toISOString().slice(0, 10);
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, "manifest.json"), "utf8"));
    const judgement = JSON.parse(fs.readFileSync(path.join(dir, "judgement.json"), "utf8"));
    const pdf = app.plugins.plugins.note1012.getModule("pdf");
    const split = (context) => {
      const match = /^(.*)\*\*(.+?)\*\*(.*)$/.exec(context);
      return match ? { before: match[1], after: match[3] } : { before: "", after: "" };
    };
    const skipped = [];
    const verifiedItems = [];
    const edits = [];
    for (const item of manifest) {
      const verdict = judgement.find((entry) => entry.n === item.n);
      if (!verdict || verdict.verdict === "D" || verdict.confidence !== "高") {
        skipped.push(item.n);
        continue;
      }
      const { before, after } = split(item.context);
      if (verdict.verdict === "A") {
        verifiedItems.push({ item, before, after, verdict });
        continue;
      }
      // B / C：改成原书的字；异体按选项取规范字或原书字形
      let target = verdict.verdict === "B" ? item.ocr : verdict.correct;
      let note = verdict.note || "";
      if (verdict.variant && verdict.variant.printed && verdict.variant.standard) {
        const { printed, standard } = verdict.variant;
        target = variants === "keep" ? printed : standard;
        note = variants === "keep" ? `规范字作${standard}` : `原书作${printed}（${standard}的异体），改作规范字`;
      }
      edits.push({ item, before, after, verdict, target, note });
    }
    // 1. 改正文：按文件分组，从后往前替换，改前核对位置上还是 MinerU 那个字
    const byFile = new Map();
    for (const edit of edits) byFile.set(edit.item.file, [...(byFile.get(edit.item.file) || []), edit]);
    const applied = [];
    const mismatched = [];
    for (const [file, list] of byFile) {
      const entry = app.vault.getAbstractFileByPath(file);
      await app.vault.process(entry, (text) => {
        let out = text;
        for (const edit of [...list].sort((a, b) => b.item.from - a.item.from)) {
          let from = edit.item.from;
          // 截图之后前面改过字（如换成扩展区的字占两个单位）位置会挪：按前后文在原位附近找回
          if (out.slice(from, from + edit.item.minerU.length) !== edit.item.minerU) {
            const needle = edit.before.slice(-4) + edit.item.minerU + edit.after.slice(0, 4);
            const near = out.indexOf(needle, Math.max(0, from - 200));
            from = near >= 0 && near - from < 400 ? near + edit.before.slice(-4).length : -1;
          }
          const to = from + edit.item.minerU.length;
          if (from < 0 || out.slice(from, to) !== edit.item.minerU) {
            mismatched.push(edit.item.n);
            continue;
          }
          out = out.slice(0, from) + edit.target + out.slice(to);
          applied.push(edit);
        }
        return out;
      });
    }
    // 2. 账本：改字先让比对算出条目，再补判定信息；认可的字直接记
    const books = new Map();
    for (const edit of [...applied, ...verifiedItems]) {
      const book = await pdf.store.bookForFile(edit.item.file);
      books.set(book.id, book);
    }
    for (const book of books.values()) {
      await pdf.collation.updateLedger(book, { quiet: true });
      const ledger = await pdf.collation.ledger(book);
      const rulings = [...(ledger.rulings || [])];
      for (const edit of applied.filter((e) => e.item.file.startsWith(book.markdownRoot + "/"))) {
        const hit = ledger.edits.find((e) => e.file === edit.item.file && e.state === "present" && e.original === edit.item.minerU && e.current === edit.target && e.before.endsWith(edit.before.slice(-4)))
          || ledger.edits.find((e) => e.file === edit.item.file && e.state === "present" && e.original === edit.item.minerU && e.current === edit.target);
        if (!hit) continue;
        rulings.push({ file: hit.file, before: hit.before, value: hit.current, after: hit.after, by, basis: edit.verdict.reason, ...(edit.note ? { note: edit.note } : {}), at: today });
      }
      const verified = [...ledger.verified];
      for (const v of verifiedItems.filter((e) => e.item.file.startsWith(book.markdownRoot + "/"))) {
        const key = [v.item.file, v.before, v.item.minerU, v.after].join(String.fromCharCode(0));
        if (verified.some((x) => x.key === key)) continue;
        verified.push({
          key, file: v.item.file, value: v.item.minerU, before: v.before, after: v.after, verifiedAt: today,
          by, basis: v.verdict.reason,
          ...(v.verdict.misprint ? { note: `原书作${v.item.minerU}，疑为${v.verdict.misprint}之误` } : {}),
          ...(v.verdict.variant && v.verdict.variant.standard ? { note: `原书作${v.verdict.variant.printed}，规范字作${v.verdict.variant.standard}` } : {})
        });
      }
      await pdf.collation.saveLedger({ ...ledger, rulings, verified });
      // 重写校勘记笔记（带判定信息）与疑字清单
      await pdf.collation.updateLedger(book, { quiet: true });
      await pdf.collation.writeDoubtReport(book, false);
    }
    window.__judgementResult = JSON.stringify({
      applied: applied.map((e) => ({ n: e.item.n, from: e.item.minerU, to: e.target })),
      verified: verifiedItems.map((e) => e.item.n),
      skipped,
      mismatched,
      variants
    });
  })().catch((error) => {
    window.__judgementResult = `error ${error && error.message ? error.message : error}`;
  });
  return "started";
})();
