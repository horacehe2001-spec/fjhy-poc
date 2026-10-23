# -*- coding: utf-8 -*-
import openpyxl, collections, json, io
p=r"D:/01 WORK/18 应用一部/华电财务共享中心/福建港口集团/福建海运集团POC/材料/差旅审核制度规则/财务共享业务场景核算及审核规则.xlsx"
wb=openpyxl.load_workbook(p, data_only=True, read_only=True)
rows=list(wb["业务域"].iter_rows(values_only=True))
f=io.open("att_scan.txt","w",encoding="utf-8")
sel=[]
for r in rows[1:]:
    if not r or not r[0]: continue
    doc=str(r[1] or ""); big=str(r[3] or "")
    if "差旅" in doc or "差旅" in big: sel.append(r)
f.write("命中行数(系统单据或业务大类含'差旅'): %d\n" % len(sel))
docc=collections.Counter(str(r[1] or "") for r in sel)
bigc=collections.Counter(str(r[3] or "") for r in sel)
f.write("按系统单据: %s\n" % dict(docc))
f.write("按业务大类: %s\n\n" % dict(bigc))
# only 差旅报账单
trav=[r for r in sel if "差旅" in str(r[1] or "")]
f.write("== 差旅报账单行数: %d\n" % len(trav))
groups=collections.OrderedDict()
for r in trav:
    key=(str(r[2] or ""),str(r[3] or ""),str(r[4] or ""),str(r[5] or ""))
    groups.setdefault(key,[]).append(r)
f.write("== 唯一(大类编码,大类,小类编码,小类): %d\n\n" % len(groups))
out=[]
for key,rs in groups.items():
    atts=collections.OrderedDict(); pts=collections.OrderedDict(); sc=collections.OrderedDict(); rm=collections.OrderedDict()
    for r in rs:
        for idx,d in ((7,atts),(8,pts),(6,sc),(9,rm)):
            v=(str(r[idx]).strip() if r[idx] else "")
            if v: d[v]=d.get(v,0)+1
    out.append({"大类编码":key[0],"大类":key[1],"小类编码":key[2],"小类":key[3],"行数":len(rs),
        "场景释义":list(sc.items()),"报账附件":list(atts.items()),"专用审核要点":list(pts.items()),"备注":list(rm.items())})
for g in out:
    f.write("="*110+"\n")
    f.write("[%s %s] / [%s %s]  行数=%d\n" % (g["大类编码"],g["大类"],g["小类编码"],g["小类"],g["行数"]))
    for s,c in g["场景释义"]: f.write("  场景(x%d): %s\n" % (c,s))
    for a,c in g["报账附件"]: f.write("  --附件(x%d)--\n%s\n" % (c,a))
    for t,c in g["专用审核要点"]: f.write("  **要点(x%d)**\n%s\n" % (c,t))
    for m,c in g["备注"]: f.write("  ~备注(x%d): %s\n" % (c,m))
uniq=collections.OrderedDict()
for g in out:
    for a,c in g["报账附件"]: uniq[a]=uniq.get(a,0)+c
f.write("\n\n####### 唯一附件口径文本数: %d\n" % len(uniq))
for i,(a,c) in enumerate(uniq.items(),1):
    f.write("\n--- U%02d (x%d) ---\n%s\n" % (i,c,a))
f.close()
json.dump(out, io.open("att_groups.json","w",encoding="utf-8"), ensure_ascii=False, indent=1)
