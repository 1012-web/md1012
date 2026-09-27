// 用户核对纸本后认可的疑字：按 doubt_crops 的 manifest 编号写进账本（判定者：用户核对纸本原书）。
// 参数：window.__ownerVerify = { dir, numbers: [17, 28], basis: "纸本原书作入" }；结果写在 window.__ownerVerifyResult
// 截图之后正文可能又改过（前面改了字位置会挪、上下文里的字也可能变了），所以不按截图时的偏移认可，
// 而是先重写本书的 OCR 疑字清单，在当前清单里找同一个字：字相同、前或后四个字对得上，取离原位置最近的一处。
// 找不到的编号列在 notFound 里，不写账本。
// dryRun: true 时只核对能否找到、找到的是哪一处（matches），不写账本。
(() => {
  window.__ownerVerifyResult = "running";
  const options = window.__ownerVerify || {};
  (async () => {
    const fs = require("fs");
    const path = require("path");
    const manifest = JSON.parse(fs.readFileSync(path.join(options.dir, "manifest.json"), "utf8"));
    const pdf = app.plugins.plugins.note1012.getModule("pdf");
    const today = new Date().toISOString().slice(0, 10);
    const split = (context) => {
      const match = /^(.*)\*\*(.+?)\*\*(.*)$/.exec(context.trim());
      return match ? { before: match[1], value: match[2], after: match[3] } : null;
    };
    // 当前清单里的每一行：文件、前后文、偏移
    const rowsOf = async (book) => {
      await pdf.collation.writeDoubtReport(book, false);
      const note = await app.vault.read(app.vault.getAbstractFileByPath(`${book.markdownRoot}/校勘/${book.title} OCR疑字.md`));
      const rows = [];
      for (const line of note.split("\n")) {
        const context = line.split(" | ").find((cell) => /\*\*.+?\*\*/.test(cell));
        const link = /\(obsidian:\/\/note1012-goto\?([^)]+)\)/.exec(line);
        if (!context || !link) continue;
        const params = new URLSearchParams(link[1]);
        const parts = split(context.replace(/\\\|/g, "|"));
        const file = params.get("file") ? `${book.markdownRoot}/${params.get("file")}` : params.get("path");
        if (parts) rows.push({ ...parts, file, from: Number(params.get("from")) });
      }
      return rows;
    };
    const done = [];
    const notFound = [];
    const cache = new Map();
    for (const n of options.numbers) {
      const item = manifest.find((entry) => entry.n === n);
      const shot = item && split(item.context);
      if (!shot) {
        notFound.push(n);
        continue;
      }
      const book = await pdf.store.bookForFile(item.file);
      if (!cache.has(book.id)) cache.set(book.id, await rowsOf(book));
      const hit = cache.get(book.id)
        .filter((row) => row.file === item.file && row.value === item.minerU
          && (row.before.endsWith(shot.before.slice(-4)) || row.after.startsWith(shot.after.slice(0, 4))))
        .sort((a, b) => Math.abs(a.from - item.from) - Math.abs(b.from - item.from))[0];
      if (!hit) {
        notFound.push(n);
        continue;
      }
      if (options.dryRun) {
        done.push({ n, file: hit.file, from: hit.from, context: `${hit.before}**${hit.value}**${hit.after}` });
        continue;
      }
      const ledger = await pdf.collation.ledger(book);
      const key = [hit.file, hit.before, hit.value, hit.after].join(String.fromCharCode(0));
      if (!ledger.verified.some((x) => x.key === key)) {
        await pdf.collation.saveLedger({
          ...ledger,
          verified: [...ledger.verified, { key, file: hit.file, value: hit.value, before: hit.before, after: hit.after, verifiedAt: today, by: "用户核对纸本原书", basis: options.basis }]
        });
      }
      done.push(n);
    }
    for (const bookId of options.dryRun ? [] : cache.keys()) {
      const book = (await pdf.store.loadCatalog()).books.find((entry) => entry.id === bookId);
      await pdf.collation.writeDoubtReport(book, false);
    }
    window.__ownerVerifyResult = JSON.stringify(options.dryRun ? { matches: done, notFound } : { done, notFound });
  })().catch((error) => {
    window.__ownerVerifyResult = `error ${error && error.message ? error.message : error}`;
  });
  return "started";
})();
