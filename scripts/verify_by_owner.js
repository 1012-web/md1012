// 站长核对纸本后认可的疑字：按 doubt_crops 的 manifest 编号写进账本（判定者：站长核对纸本原书）。
// 参数：window.__ownerVerify = { dir, numbers: [17, 28], basis: "纸本原书作入" }；结果写在 window.__ownerVerifyResult
(() => {
  window.__ownerVerifyResult = "running";
  const options = window.__ownerVerify || {};
  (async () => {
    const fs = require("fs");
    const path = require("path");
    const manifest = JSON.parse(fs.readFileSync(path.join(options.dir, "manifest.json"), "utf8"));
    const pdf = app.plugins.plugins.note1012.getModule("pdf");
    const today = new Date().toISOString().slice(0, 10);
    const done = [];
    for (const n of options.numbers) {
      const item = manifest.find((entry) => entry.n === n);
      const match = /^(.*)\*\*(.+?)\*\*(.*)$/.exec(item.context);
      const [before, after] = [match[1], match[3]];
      const text = await app.vault.cachedRead(app.vault.getAbstractFileByPath(item.file));
      if (text.slice(item.from, item.from + item.minerU.length) !== item.minerU) throw new Error(`#${n} 位置上的字已不是「${item.minerU}」`);
      const book = await pdf.store.bookForFile(item.file);
      const ledger = await pdf.collation.ledger(book);
      const key = [item.file, before, item.minerU, after].join(String.fromCharCode(0));
      if (!ledger.verified.some((x) => x.key === key)) {
        await pdf.collation.saveLedger({
          ...ledger,
          verified: [...ledger.verified, { key, file: item.file, value: item.minerU, before, after, verifiedAt: today, by: "站长核对纸本原书", basis: options.basis }]
        });
      }
      done.push(n);
      await pdf.collation.writeDoubtReport(book, false);
    }
    window.__ownerVerifyResult = JSON.stringify({ done });
  })().catch((error) => {
    window.__ownerVerifyResult = `error ${error && error.message ? error.message : error}`;
  });
  return "started";
})();
