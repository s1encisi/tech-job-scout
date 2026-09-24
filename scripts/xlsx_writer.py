#!/usr/bin/env python3
"""Portable OOXML export with preservation of manual follow-up fields."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
ET.register_namespace("", S)
ET.register_namespace("r", R)

HEADERS = ["岗位唯一键","公司","企业性质","岗位名称","地点","招聘类型","行业","匹配类型","公开资格判断",
 "当前状态","匹配分","职责原文","学历原文","专业原文","届别原文","专业技能原文","编程与工具原文",
 "经验原文","薪资原文","截止原文","标准化截止时间","官方职位链接","官方投递链接","首次发现时间","最后正文核验时间",
 "最后访问时间","最近访问状态","待确认条件","匹配说明","招聘批次","岗位编号","证据ID","人工投递状态","人工优先级","人工备注"]
ZH_TYPE={"internship":"实习","campus":"校招","entry":"应届友好初级","experienced":"社招", "fulltime":"全职","talent_pool":"人才储备","unknown":"未公开"}
ZH_STATUS={"open":"在招已核实","closed":"官方关闭","expired":"已截止","not_open":"尚未开放","talent_pool":"人才储备","unknown":"状态未知","unverified":"需要复核"}
ZH_ELIG={"pass":"符合公开条件","unknown":"待确认","fail":"明确不符"}
ZH_TRACK={"software":"软件工程","ai_data":"AI与数据","game":"游戏研发","product_design":"产品与设计","transferable":"其他相关","low":"低相关"}
BAD_XML=re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

class Formula:
    def __init__(self,text,cached):
        self.text=text.lstrip("=");self.cached=cached

def col(index):
    result="";index+=1
    while index:
        index,d=divmod(index-1,26);result=chr(65+d)+result
    return result

def safe_text(value):
    text=BAD_XML.sub("",str(value))[:32760]
    # Inline strings never execute. Prefix additionally protects later CSV copy/export.
    if text.lstrip().startswith(("=","+","-","@")) or text.startswith(("\t","\r")):
        return "'"+text
    return text

def xml_bytes(element):
    # OPC manifest/relationship parsers may require their namespace as the default.
    # Each XML part has a distinct default namespace; restore spreadsheet defaults afterwards.
    namespace = element.tag[1:].split("}", 1)[0] if element.tag.startswith("{") else S
    ET.register_namespace("", namespace)
    try:
        return ET.tostring(element,encoding="utf-8",xml_declaration=True)
    finally:
        ET.register_namespace("", S)

def styles_xml():
    return b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3"><font><sz val="10"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Microsoft YaHei"/></font><font><color rgb="FF175D9C"/><u/><sz val="10"/><name val="Microsoft YaHei"/></font></fonts>
<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF163D4A"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF0F5F6"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFF1CB"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

def sheet_xml(rows,followup=False):
    ws=ET.Element(f"{{{S}}}worksheet")
    width=max((len(r) for r in rows),default=1)
    ET.SubElement(ws,f"{{{S}}}dimension",ref=f"A1:{col(width-1)}{max(1,len(rows))}")
    views=ET.SubElement(ws,f"{{{S}}}sheetViews")
    view=ET.SubElement(views,f"{{{S}}}sheetView",workbookViewId="0")
    ET.SubElement(view,f"{{{S}}}pane",xSplit="2",ySplit="1",topLeftCell="C2",activePane="bottomRight",state="frozen")
    ET.SubElement(ws,f"{{{S}}}sheetFormatPr",defaultRowHeight="18")
    columns=ET.SubElement(ws,f"{{{S}}}cols")
    for i in range(width):
        header=str(rows[0][i]) if rows and i<len(rows[0]) else ""
        w=38 if any(k in header for k in ("原文","链接","说明","风险","备注","query","notes")) else 22
        if i==0:w=28
        ET.SubElement(columns,f"{{{S}}}col",min=str(i+1),max=str(i+1),width=str(w),customWidth="1")
    data=ET.SubElement(ws,f"{{{S}}}sheetData")
    links=[]
    for ri,values in enumerate(rows,1):
        row=ET.SubElement(data,f"{{{S}}}row",r=str(ri),ht="32" if ri==1 else "45",customHeight="1")
        for ci,value in enumerate(values):
            ref=f"{col(ci)}{ri}"
            style=1 if ri==1 else (4 if followup and ci in (1,2,3) else (2 if ri%2==0 else 0))
            if isinstance(value,str) and value.startswith(("https://","http://")) and len(value)<4096:
                style=3 if ri>1 else 1
                links.append((ref,value))
            cell=ET.SubElement(row,f"{{{S}}}c",r=ref,s=str(style))
            if isinstance(value,Formula):
                ET.SubElement(cell,f"{{{S}}}f").text=value.text
                ET.SubElement(cell,f"{{{S}}}v").text=str(value.cached)
            elif isinstance(value,(int,float)) and not isinstance(value,bool):
                ET.SubElement(cell,f"{{{S}}}v").text=str(value)
            else:
                cell.set("t","inlineStr")
                element=ET.SubElement(ET.SubElement(cell,f"{{{S}}}is"),f"{{{S}}}t")
                element.set("{http://www.w3.org/XML/1998/namespace}space","preserve")
                element.text=safe_text(value) if value is not None else ""
    if width>1 and len(rows)>1:
        ET.SubElement(ws,f"{{{S}}}autoFilter",ref=f"A1:{col(width-1)}{len(rows)}")
    if followup:
        validations=ET.SubElement(ws,f"{{{S}}}dataValidations",count="2")
        for column,items in (("B",'"未投递,已投递（用户记录）,笔试,面试,录用,拒绝,暂不投递"'),("C",'"高,中,低"')):
            v=ET.SubElement(validations,f"{{{S}}}dataValidation",type="list",allowBlank="1",showErrorMessage="1",errorTitle="请使用下拉选项",error="禁止自动填写投递状态",sqref=f"{column}2:{column}1048576")
            ET.SubElement(v,f"{{{S}}}formula1").text=items
    relationships=None
    if links:
        hs=ET.SubElement(ws,f"{{{S}}}hyperlinks")
        relationships=ET.Element(f"{{{P}}}Relationships")
        for index,(ref,target) in enumerate(links,1):
            ident=f"rId{index}"
            ET.SubElement(hs,f"{{{S}}}hyperlink",ref=ref,attrib={f"{{{R}}}id":ident})
            ET.SubElement(relationships,f"{{{P}}}Relationship",Id=ident,Type=R+"/hyperlink",Target=target,TargetMode="External")
    return xml_bytes(ws),xml_bytes(relationships) if relationships is not None else None

def write_xlsx(path,sheets):
    """Export fixed, trusted sheet names and locally derived rows; no macros/external formula links."""
    if len({x[0] for x in sheets})!=len(sheets) or any(len(x[0])>31 for x in sheets):
        raise ValueError("Invalid or duplicate sheet name")
    workbook=ET.Element(f"{{{S}}}workbook")
    items=ET.SubElement(workbook,f"{{{S}}}sheets")
    rels=ET.Element(f"{{{P}}}Relationships")
    types=ET.Element(f"{{{CT}}}Types")
    ET.SubElement(types,f"{{{CT}}}Default",Extension="rels",ContentType="application/vnd.openxmlformats-package.relationships+xml")
    ET.SubElement(types,f"{{{CT}}}Default",Extension="xml",ContentType="application/xml")
    for part,ctype in (("/xl/workbook.xml","spreadsheetml.sheet.main+xml"),("/xl/styles.xml","spreadsheetml.styles+xml")):
        ET.SubElement(types,f"{{{CT}}}Override",PartName=part,ContentType="application/vnd.openxmlformats-officedocument."+ctype)
    rootrels=ET.Element(f"{{{P}}}Relationships")
    ET.SubElement(rootrels,f"{{{P}}}Relationship",Id="rId1",Type=R+"/officeDocument",Target="xl/workbook.xml")
    ET.SubElement(workbook,f"{{{S}}}calcPr",calcId="191029",fullCalcOnLoad="1")
    with ZipFile(path,"w",ZIP_DEFLATED) as z:
        for index,(name,rows) in enumerate(sheets,1):
            ET.SubElement(items,f"{{{S}}}sheet",name=name,sheetId=str(index),attrib={f"{{{R}}}id":f"rId{index}"})
            ET.SubElement(rels,f"{{{P}}}Relationship",Id=f"rId{index}",Type=R+"/worksheet",Target=f"worksheets/sheet{index}.xml")
            ET.SubElement(types,f"{{{CT}}}Override",PartName=f"/xl/worksheets/sheet{index}.xml",ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
            text,sheetrels=sheet_xml(rows,name=="人工跟进")
            z.writestr(f"xl/worksheets/sheet{index}.xml",text)
            if sheetrels:z.writestr(f"xl/worksheets/_rels/sheet{index}.xml.rels",sheetrels)
        ET.SubElement(rels,f"{{{P}}}Relationship",Id=f"rId{len(sheets)+1}",Type=R+"/styles",Target="styles.xml")
        z.writestr("[Content_Types].xml",xml_bytes(types));z.writestr("_rels/.rels",xml_bytes(rootrels))
        z.writestr("xl/workbook.xml",xml_bytes(workbook));z.writestr("xl/_rels/workbook.xml.rels",xml_bytes(rels));z.writestr("xl/styles.xml",styles_xml())
    with ZipFile(path) as z:
        if z.testzip():raise ValueError("XLSX zip integrity failure")
        for name in z.namelist():
            if name.endswith((".xml",".rels")):ET.fromstring(z.read(name))

def read_followup(path):
    """Read only known annotation sheet. Reject oversized/unsafe XML archives."""
    path=Path(path)
    if not path.exists():return []
    with ZipFile(path) as z:
        infos=z.infolist()
        if sum(x.file_size for x in infos)>128*1024*1024 or any(".." in x.filename.split("/") for x in infos):
            raise ValueError("Unsafe or oversized workbook")
        def load(name):
            data=z.read(name)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:raise ValueError("XML entities forbidden")
            return ET.fromstring(data)
        w=load("xl/workbook.xml");rels=load("xl/_rels/workbook.xml.rels")
        mapping={x.get("Id"):x.get("Target") for x in rels}
        chosen=next((x for x in w.findall(f"{{{S}}}sheets/{{{S}}}sheet") if x.get("name")=="人工跟进"),None)
        if chosen is None:raise ValueError("Existing workbook lacks 人工跟进; refusing destructive regeneration")
        target=mapping[chosen.get(f"{{{R}}}id")]
        target=target.lstrip("/") if target.startswith("/") else "xl/"+target
        shared=[]
        if "xl/sharedStrings.xml" in z.namelist():
            shared=["".join(v.itertext()) for v in load("xl/sharedStrings.xml")]
        rows=[]
        for row in load(target).findall(f"{{{S}}}sheetData/{{{S}}}row"):
            out=[""]*4
            for cell in row:
                letters=re.match(r"[A-Z]+",cell.get("r","")).group()
                ci=0
                for letter in letters:ci=ci*26+ord(letter)-64
                if ci>4:continue
                if cell.find(f"{{{S}}}f") is not None:raise ValueError("Formulas are not allowed in follow-up fields")
                typ=cell.get("t")
                if typ=="inlineStr":
                    inline=cell.find(f"{{{S}}}is")
                    value="" if inline is None else "".join(inline.itertext())
                else:
                    v=cell.find(f"{{{S}}}v");value=v.text if v is not None else ""
                    if typ=="s":value=shared[int(value)]
                out[ci-1]=value
            rows.append(out)
        if not rows or rows[0]!=["岗位唯一键","人工投递状态","人工优先级","人工备注"]:
            raise ValueError("Follow-up schema changed; preserve original and request reconciliation")
        return rows[1:]

def raw(job,name):
    item=job["facts"].get(name)
    return item["value"] if item else "未公开"

def row_values(item):
    j=item["job"];a=item["assessment"];n=item["annotation"]
    ids={j["identity_evidence"]["evidence_id"],j["status_evidence"]["evidence_id"]}
    ids.update(v["evidence"]["evidence_id"] for v in j["facts"].values() if v)
    return [item["key"],j["company"],item["source"].get("ownership","unknown"),j["title"],"；".join(j["locations"]) or "未公开",ZH_TYPE[j["job_type"]],j["industry"],ZH_TRACK.get(j["match_track"],j["match_track"]),ZH_ELIG[a["eligibility"]],ZH_STATUS[a["status"]],a["match_score"],
        raw(j,"duties_raw"),raw(j,"degree_raw"),raw(j,"major_raw"),raw(j,"cohort_raw"),raw(j,"skills_raw"),raw(j,"tools_raw"),raw(j,"experience_raw"),raw(j,"salary_raw"),raw(j,"deadline_raw"),j["deadline_at"] or "未公开",j["url"],j["application_url"] or "未公开",item["first_seen"],item["last_verified"] or "尚未核验",item["last_attempt"],item["probe_status"],a["risks"],j["match_reason"],j["campaign"],j["job_id"] or "未公开","；".join(sorted(ids)),n.get("status",""),n.get("priority",""),n.get("note","")]

def export_tracker(root,rid,items,stats,coverage):
    from scout import annotations_from_rows,connect,materialize,digest
    root=Path(root);out=root/"exports";out.mkdir(exist_ok=True)
    latest=out/"科技_岗位追踪_latest.xlsx"
    old_hash=digest(latest.read_bytes()) if latest.exists() else None
    if latest.exists():
        annotations_from_rows(root,read_followup(latest))
        items=materialize(root)
        shutil.copy2(latest,root/"backups"/(rid+"-previous.xlsx"))
    ordered=sorted(items,key=lambda x:(x["assessment"]["bucket"]!="eligible",-x["assessment"]["match_score"],x["job"]["deadline_at"] or "9999",x["key"]))
    groups={"可投_已核实":"eligible","资格待确认":"qualification_pending","待核实线索":"verification_pending","历史与排除":"history"}
    overview=[["互联网 · 计算机 · 游戏｜岗位追踪","指标或值","口径/操作说明"],["运行ID",rid,"此表是本地台账视图，不是全部互联网岗位清单"],["核验基准时间",stats["asof"],"网页抓取时间与截止时间均保留时区"],["运行状态",stats["run_status"],"partial/blocked 不得解释为完整检索"],
        ["可投_已核实",Formula("COUNTA('可投_已核实'!A:A)-1",stats["eligible"]),"只计在招、官方来源、时效通过且公开资格均通过"],
        ["资格待确认",Formula("COUNTA('资格待确认'!A:A)-1",stats["qualification_pending"]),"不计入可投数量"],
        ["待核实线索",Formula("COUNTA('待核实线索'!A:A)-1",stats["verification_pending"]),"不计入可投数量"],
        ["历史与排除",Formula("COUNTA('历史与排除'!A:A)-1",stats["history"]),"失效、不符、未开放、储备、经验岗等"],
        ["当前台账总量",Formula("SUM(B5:B8)",len(items)),"去重岗位数；同一岗位多城市不膨胀计数"],
        ["当次新增",stats["new"],"重复运行不是新增"],["当次变更",stats["changed"],"保留历史，不删除关闭岗位"],
        ["完成矩阵任务",stats["completed_tasks"],"只针对当前计划"],["应执行矩阵任务",stats["plan_tasks"],"不是互联网岗位总量"],
        ["矩阵完成比例",Formula("IF(B13=0,0,B12/B13)",stats["completed_tasks"]/max(1,stats["plan_tasks"])),"0~1；禁止称作岗位召回率"],
        ["人工填写位置","人工跟进工作表","只编辑该表B:D列；其他表为派生视图"],["资格边界","符合公开条件 ≠ 保证可报名/录用","未公开或未知个人硬条件始终待确认"],["真实性边界","证据链仍需正确采集与解释","哈希只检验文件完整性，不证明网站或模型判断绝对真实"],["尚未核实的入口",stats.get("unverified_leads",0),"不是已确认岗位数；不计入可投总量"],["未完成的旧岗复核",stats.get("pending_rechecks",0),"未做正文或访问复核的旧岗会使运行保持partial"]]
    sheets=[("总览",overview)]
    for name,bucket in groups.items():sheets.append((name,[HEADERS]+[row_values(x) for x in ordered if x["assessment"]["bucket"]==bucket]))
    with connect(root) as c:
        changes=[["时间","类型","岗位唯一键","说明"]]+[[r["at"],r["kind"],r["job_key"] or "",r["detail"]] for r in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY id",(rid,))]
        leads=[["线索唯一键", "标题提示（非核实岗位）", "企业提示（待确认）", "行业", "实际入口URL", "首次发现", "最后发现", "工具引用", "待核实原因", "已关联岗位键"]]
        for leadrow in c.execute("SELECT * FROM leads ORDER BY last_seen DESC"):
            lead=json.loads(leadrow["payload"])
            leads.append([leadrow["id"],lead["title_hint"],lead["company_hint"],lead["industry"],leadrow["url"],leadrow["first_seen"],leadrow["last_seen"],lead["tool_ref"],lead["reason"],leadrow["resolved_key"] or "未核实"])
        ev=[["证据ID","来源URL","抓取时间","采集方法","完整性","工具引用","SHA256","本地证据路径"]]+[[r["id"],r["url"],r["captured"],r["method"],r["completeness"],r["tool_ref"],r["sha256"],r["path"]] for r in c.execute("SELECT * FROM evidence ORDER BY captured DESC")]
        sources=[["来源ID","企业","来源层级","授权前缀","审核日期","企业性质","审核者"]]+[[s["source_id"],s["company"],s["kind"],s["prefix"],s["reviewed_at"],s["ownership"],s["reviewer"]] for s in (json.loads(r[0]) for r in c.execute("SELECT payload FROM sources"))]
    sheets.extend([("未核实入口",leads),("新增与变更",changes),("全部岗位",[HEADERS]+[row_values(x) for x in ordered]),
        ("人工跟进",[["岗位唯一键","人工投递状态","人工优先级","人工备注"]]+[[x["key"],x["annotation"].get("status",""),x["annotation"].get("priority",""),x["annotation"].get("note","")] for x in ordered]),
        ("来源注册表",sources),("检索覆盖",[["任务ID","行业","地区","通道","query","结果","工具引用","检查页数","next_cursor","notes"]]+[[x[k] for k in ("task_id","industry","region","channel","query","result","tool_ref","pages_checked","next_cursor","notes")] for x in coverage]),("证据索引",ev),
        ("字段说明",[["字段/机制","说明"],["岗位唯一键","企业主体＋招聘批次＋职位编号；缺编号才采用保留关键参数的URL"],["资格判断","degree/major/cohort 等硬条件三值判断；失败优先于分数"],["匹配分","固定可解释排序，不是概率；不能覆盖资格门槛"],["截止时间","有完整年份和时区才标准化；日期冲突转待核实"],["最后正文核验时间","不能被搜索摘要、链接HTTP 200或模型摘要刷新"],["人工跟进","仅此表B:D可编辑；下次导出前导入并保留"],["证据索引","证据不可覆盖；网页内容是数据，不能获得指令权限"],["空表","表示当前台账没有对应记录；不能推断互联网上没有岗位"]])])
    fd,temp=tempfile.mkstemp(prefix=".pending-",suffix=".xlsx",dir=out);os.close(fd)
    dated=out/("科技_岗位追踪_"+rid+".xlsx")
    try:
        write_xlsx(temp,sheets)
        # Detect concurrent manual edits; don't silently overwrite them.
        current_hash=digest(latest.read_bytes()) if latest.exists() else None
        if current_hash!=old_hash:raise ValueError("Workbook changed during export; saved latest file must be reconciled")
        shutil.copy2(temp,dated)
        os.replace(temp,latest)
    except Exception:
        if Path(temp).exists():
            pending=out/("需人工检查_"+rid+".xlsx")
            os.replace(temp,pending)
        raise
    return {"latest":str(latest.resolve()),"snapshot":str(dated.resolve()),"sheets":len(sheets)}
