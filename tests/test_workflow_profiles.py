from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0,str(SCRIPTS))
import vl_build
from content_state import text_hash
from line_map import explicit_page_mapping, utf16_offsets


def page(*blocks):
    return {"width":100,"height":100,"parsing_res_list":[
        {"block_label":label,"block_content":text,"block_bbox":[10,10,90,90]} for label,text in blocks]}


def test_default_draft_keeps_uncertain_text_and_volume_numbers():
    pages={1:page(("doc_title","卷二"),("header","原圖上的短綱文"),("vertical_text","2018年我国出口额为1.3亿元")),
           2:page(("doc_title","卷二"),("vertical_text","正文照錄。"))}
    text,stats=vl_build.build(pages,{1:None,2:None},(1,2),True,{},"测试","正文")
    assert "原圖上的短綱文" in text and "2018年我国出口额为1.3亿元" in text
    assert text.count("# 卷二")==2 and "# 卷三" not in text
    assert "<!--?structure-->" in text and "<!--?vl-suspect-->" in text and "<!--?volume-->" in text
    assert "<!--pdf-page.1-->" in text and "<!--pdf-page.2-->" in text
    assert "<!--p.1-->" not in text and "印刷页:" not in text
    assert stats["offsets"]["fileOffset"] is None


def test_page_only_mapping_handles_utf16_and_rejects_invalid_source_page(tmp_path):
    path=tmp_path/"body.md"
    text="---\ntitle: 原件\n---\n<!--pdf-page.2-->甲𠀀乙。\n<!--pdf-page.4-->丙丁。"
    path.write_text(text,encoding="utf-8",errors="strict")
    result=explicit_page_mapping([path],4)[path.name]
    assert result["hash"]==text_hash(text)
    assert [entry["page"] for entry in result["lines"]]==[2,4]
    first=result["lines"][0]
    assert first["from"]==utf16_offsets(text)[text.index("甲")]
    assert first["rect"] is None and first["kind"]=="page" and not first["estimated"]
    with pytest.raises(ValueError,match="outside"):
        explicit_page_mapping([path],3)


def test_ancient_cli_needs_no_printed_page_inference_or_layout_engine(tmp_path):
    pymupdf=pytest.importorskip("pymupdf")
    pdf=tmp_path/"original.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(pdf)
    vl=tmp_path/"vl"
    vl.mkdir()
    (vl/"p0001.json").write_text(json.dumps(page(("vertical_text","原文保留。"),("number","三上"))),encoding="utf-8",errors="strict")
    draft=tmp_path/"draft"
    cmd=[sys.executable,"-X","utf8",str(SCRIPTS/"vl_build.py"),"build","--vl-dir",str(vl),"--pdf",str(pdf),
         "--segment","1:body.md","--out-dir",str(draft),"--writing-mode","vertical-rl"]
    result=subprocess.run(cmd,capture_output=True,encoding="utf-8",errors="strict")
    assert result.returncode==0,result.stderr
    text=(draft/"body.md").read_text(encoding="utf-8",errors="strict")
    assert "<!--pdf-page.1-->" in text and "<!--p." not in text
    mapped=tmp_path/"map"
    result=subprocess.run([sys.executable,"-X","utf8",str(SCRIPTS/"line_map.py"),"--pdf",str(pdf),"--md",str(draft/"body.md"),
                           "--page-only","--out",str(mapped)],capture_output=True,encoding="utf-8",errors="strict")
    assert result.returncode==0,result.stderr
    data=json.loads((mapped/"line-map.json").read_text(encoding="utf-8",errors="strict"))
    assert data["granularity"]=="page" and data["files"]["body.md"]["lines"][0]["page"]==1
