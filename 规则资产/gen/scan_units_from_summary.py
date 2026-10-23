# -*- coding: utf-8 -*-
import openpyxl, io, re, collections
S=r"D:/01 WORK/18 应用一部/华电财务共享中心/福建港口集团/福建海运集团POC/材料/单据明细表与审批意见/"
f=io.open("sum_scan.txt","w",encoding="utf-8")
def rows(fn,sh):
    wb=openpyxl.load_workbook(S+fn, data_only=True, read_only=True)
    return [r for r in wb[sh].iter_rows(values_only=True) if r and any(x is not None for x in r)]
allsum=[]
r=rows("差旅报账单明细表.xlsx","1差旅费报账单")
h=[str(c) if c is not None else "" for c in r[0]]
for x in r[1:]: allsum.append(str(x[h.index("摘要")] or ""))
r2=rows("多人差旅报账单明细表.xlsx","2多人差旅报账单")
h2=[str(c) if c is not None else "" for c in r2[0]]
for x in r2[1:]: allsum.append(str(x[h2.index("摘要")] or ""))
f.write("摘要共 %d 条\n" % len(allsum))
for i,s in enumerate(allsum,1): f.write("  %02d %s\n" % (i,s))
# 抽「X港务」「X集团」「X公司」等单位名
pat=re.compile(r"[\u4e00-\u9fa5]{2,6}(?:港务(?:集团)?|集团|公司|有限公司|港区|码头)")
c=collections.Counter()
for s in allsum:
    for m in pat.findall(s): c[m]+=1
f.write("\n摘要里出现的单位名候选:\n")
for k,v in c.most_common(): f.write("   %-18s %d\n" % (k,v))
# 地名候选（市/县/区/港）
pat2=re.compile(r"[\u4e00-\u9fa5]{2,4}(?:市|县|区|港|镇)")
c2=collections.Counter()
for s in allsum:
    for m in pat2.findall(s): c2[m]+=1
f.write("\n摘要里出现的地名候选:\n")
for k,v in c2.most_common(): f.write("   %-18s %d\n" % (k,v))
f.close()
