// 在 Obsidian 里执行：把某书 OCR 疑字清单里剩下的全部认可（用户决定整批放行时用）。
// 读当前清单笔记的每一行（上下文「前文**字**后文」与定位链接里的文件），按清单的前后文记认可，
// 不依赖截图时记下的偏移（前面改过字会让偏移挪动）。
// 参数：window.__verifyRemaining = { title: "书名", by: "用户认可（未核纸本）", basis: "…" }
// 结果写在 window.__verifyRemainingResult。
(() => {
  window.__verifyRemainingResult = "running";
  const options = window.__verifyRemaining || {};
  (async () => {
    const pdf = app.plugins.plugins.note1012.getModule("pdf");
    const target = (await pdf.store.loadCatalog()).books.find((book) => book.title === options.title);
    if (!target) throw new Error(`书目里没有《${options.title}》`);
    // 清单在书文件夹的「校勘」里
    const notePath = `${target.markdownRoot}/校勘/${options.title} OCR疑字.md`;
    const note = await app.vault.read(app.vault.getAbstractFileByPath(notePath));
    const today = new Date().toISOString().slice(0, 10);
    const rows = [];
    for (const line of note.split("\n")) {
      const cells = line.split(" | ");
      const context = cells.find((cell) => /\*\*.+?\*\*/.test(cell));
      const link = /\(obsidian:\/\/note1012-goto\?([^)]+)\)/.exec(line);
      if (!context || !link) continue;
      const params = new URLSearchParams(link[1]);
      const match = /^(.*)\*\*(.+?)\*\*(.*)$/.exec(context.trim());
      // 表格里的竖线写成了 \|，还原
      const unescape = (value) => value.replace(/\\\|/g, "|");
      // 新链接写「书 id + 相对书文件夹的文件名」，旧链接写库内路径
      const file = params.get("file") ? `${target.markdownRoot}/${params.get("file")}` : params.get("path");
      rows.push({ file, before: unescape(match[1]), value: unescape(match[2]), after: unescape(match[3]) });
    }
    const books = new Map();
    for (const row of rows) {
      const book = await pdf.store.bookForFile(row.file);
      const list = books.get(book.id) || { book, rows: [] };
      list.rows.push(row);
      books.set(book.id, list);
    }
    let added = 0;
    for (const { book, rows: list } of books.values()) {
      const ledger = await pdf.collation.ledger(book);
      const verified = [...ledger.verified];
      for (const row of list) {
        const key = [row.file, row.before, row.value, row.after].join(String.fromCharCode(0));
        if (verified.some((item) => item.key === key)) continue;
        verified.push({ key, ...row, verifiedAt: today, by: options.by || "用户认可（未核纸本）", ...(options.basis ? { basis: options.basis } : {}) });
        added += 1;
      }
      await pdf.collation.saveLedger({ ...ledger, verified });
      await pdf.collation.writeDoubtReport(book, false);
    }
    window.__verifyRemainingResult = JSON.stringify({ rows: rows.length, added });
  })().catch((error) => {
    window.__verifyRemainingResult = `error ${error && error.message ? error.message : error}`;
  });
  return "started";
})();
