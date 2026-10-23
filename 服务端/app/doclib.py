# -*- coding: utf-8 -*-
"""制度文件库：文件进库 → 递归登记 → 轻解析 → 形成快照。

这一步要回答的是两个问题，别的都不是它的事：
  1. 「规则是从哪堆资料里抽的」——库里到底有哪些文件，边界在哪。
  2. 「那堆资料后来变没变」——重扫一次就知道新增/修改/消失了哪些。

文件有两种进库方式（迁移 0034，用户 2026-10-21 决策「双模式并存」）：

  **托管**（kind='托管'）客户端上传 → 落到 var/doclib/{领域id}/{实例id}/
          这是**本系统自己的工作区**，允许写、允许删。
          一个实例一个托管根，由 ensure_managed_root() 幂等创建。
  **引用**（kind='引用'）指定服务器上已有的目录 → 原地递归扫描。
          这是**客户的资料目录**，下面第 1 条纪律对它原样生效。

三条纪律，违反任何一条这个模块就失去意义：

1. **引用目录只读。** 绝不向 kind='引用' 的目录写入任何东西，临时文件也不落。
   客户的资料目录是证据，不是工作区。「移出文件库」只删登记，磁盘一个字节不动。
   **纪律没有因为加了上传而放松，是它的边界被写清楚了**：写盘只发生在托管根，
   而 save_uploads / delete_managed_file 的第一行就校验 kind，
   引用根上调用一律抛 ScopeError。只写在注释里而不在入口卡住，等于没有纪律。

2. **解析不了就说解析不了，并且说清是哪一种解析不了。**
   最要紧的区分是「需OCR」与「未解析」：前者是 PDF 没有文字层，本机装了
   tesseract，下一步**办得到**；后者是 .doc/.ofd 这类在轻解析阶段取不到文，
   要换路子。混成一个状态，没人知道该去跑 OCR 还是该去转格式。
   而把任何一种记成「正文为空」最坏——下游检索会据此认为「制度里没写过这件事」。

3. **扫描要快到可以随时重跑。** 指纹用「路径+大小+修改时间」，不读全文件算哈希；
   OCR 一律不在扫描阶段跑。重扫一慢就没人跑，快照也就不再反映现状。
   代价写在明处，见 scan() 与迁移 0015 的列注释。

安全口径：
  · browse/add_roots 把本机目录结构暴露给了前端，所有外来路径一律当不可信输入
    处理（safe_dir）。本系统是本机单用户的 POC，若要对外提供，必须先加一层
    「允许的根目录白名单」，而不是靠前端不传坏路径。
  · **上传的文件名与相对路径是第二个、也是更危险的攻击面**：它直接参与拼路径。
    一律过 safe_rel()，它把目录穿越、盘符、NTFS 数据流、Windows 保留名
    全部挡在门外，并且**只在托管根下拼路径**。
"""
import datetime
import hashlib
import os
import re
import shutil
import string
import uuid

from . import config, db

# ── 扫描闸门 ────────────────────────────────────────────────────────────────
# 选到 C:\ 这种根目录时，没有上限会把服务拖死，所以每一项都要有天花板。
MAX_FILES = 8000            # 单次扫描纳入的文件数上限
MAX_DEPTH = 12              # 递归层数上限
MAX_PARSE_BYTES = 8 << 20   # 超过这个大小不尝试解析正文（仍然登记）
MAX_OOXML_BYTES = 200 << 20 # docx/xlsx 体积主要是内嵌图片，解析只读 XML，单独放宽
MAX_PDF_BYTES = 120 << 20   # PDF 取文字层是按页读，与扫描图体积关系不大，也放宽
MAX_TEXT_CHARS = 300_000    # 单份正文入库上限，超出截断并标注

# 与制度资料无关、且往往巨大的目录，扫描时跳过
SKIP_DIRS = {
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", "$RECYCLE.BIN",
    "System Volume Information", "Windows", "Program Files",
    "Program Files (x86)", "ProgramData", "AppData",
}

# 登记哪些扩展名。其余（exe/dll/图片…）不进库，免得把文件库稀释成文件管理器。
DOC_EXT = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm",
    ".docx", ".doc", ".xlsx", ".xls", ".pdf", ".ofd", ".wps", ".et", ".rtf",
}

# Office 打开文档时生成的锁文件（~$xxx.docx）：扩展名合法、内容是几百字节二进制，
# 收进来只会多出一批「解析失败」。按文件名前缀挡掉。
SKIP_FILE_PREFIX = ("~$", ".~")

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}

# 轻解析阶段明确取不到正文的格式，连同「该怎么办」一起告诉使用者。
# 每条都写明下一步动作——「不支持」这种话等于没说。
NO_PARSER = {
    ".doc": "未解析：.doc 是二进制复合文档，需另存为 .docx 或用 LibreOffice 转换",
    ".xls": "未解析：.xls 是二进制格式，需另存为 .xlsx",
    ".ofd": "未解析：OFD 为国产版式格式，需用版式阅读器导出 PDF",
    ".wps": "未解析：WPS 私有格式，需另存为 .docx",
    ".et": "未解析：WPS 表格私有格式，需另存为 .xlsx",
    ".rtf": "未解析：RTF 控制字较多，轻解析阶段不处理，需另存为 .docx",
}

STATES = ("已解析", "需OCR", "未解析", "空文件", "解析失败", "未扫描")


class DocLibError(Exception):
    """本模块一切「使用者的问题」的基类。接口层统一回 400 而不是 500——
    混成 500 会让人去翻后端日志找一个本该在界面上说清楚的原因。"""


class PathError(DocLibError):
    """路径不可用：不存在、不是目录、没权限、文件名不合法。"""


class ScopeError(DocLibError):
    """归属问题：没选实例、往引用根里写、跨实例操作。

    和 PathError 分开是因为处置动作完全不同：PathError 要改路径，
    ScopeError 要先去「领域知识资产」那一页选实例，或者改用托管根。
    """


class ConflictError(DocLibError):
    """动作本身合法，但会破坏已有的证据链（删一份已经抽出规则的文件）。

    单独一类是为了让界面能把「你输错了」和「这件事后果严重」分开说。
    """


# ── 领域 / 实例归属 ─────────────────────────────────────────────────────────
def current_instance():
    """当前实例 id，没选返回 None。

    **刻意反向 import（核心模块引 api 层）**：api/domain.py 明写了
    「各处自己去读 app_setting 迟早会读出不一致的口径」，它是唯一入口。
    为了不复制那段一致性校验（实例是否属于当前领域），这里宁可反着引。
    函数内 import 是为了避开模块加载顺序，不是为了省时间。
    """
    from .api.domain import current as _cur
    return _cur().get("instance_id")


def resolve_instance(instance_id=None):
    """传了就用传的，没传用当前的。**都没有就抛，不挑一个默认值填上去。**

    挑默认值的后果：客户传的制度文件落到了另一个实例的目录里，
    而页面按当前实例过滤，于是「传成功了但清单里看不到」——
    这种现象查起来要翻落盘目录才看得出来。
    """
    iid = (instance_id or "").strip() or current_instance()
    if not iid:
        raise ScopeError("还没有选定实例。请先在「领域知识资产」页选好领域与实例，"
                         "再回来上传——制度文件必须有归属，没有归属的文件"
                         "不知道该进哪个实例的知识库。")
    row = db.query_one("""SELECT i.instance_id, i.domain_id, i.label AS instance_label,
                                 d.label AS domain_label
                            FROM domain_instance i
                            JOIN domain d ON d.domain_id = i.domain_id
                           WHERE i.instance_id = %s""", (iid,))
    if not row:
        raise ScopeError("实例不存在：%s" % iid)
    return row


# ── 路径安全 ────────────────────────────────────────────────────────────────
def safe_dir(p):
    """把外来路径规范化并校验。目录浏览是对外接口，必须当不可信输入处理。"""
    if not p or not str(p).strip():
        raise PathError("路径为空")
    p = os.path.abspath(os.path.expanduser(str(p).strip().strip('"').strip("'")))
    if not os.path.exists(p):
        raise PathError("路径不存在：%s" % p)
    if not os.path.isdir(p):
        raise PathError("不是文件夹：%s" % p)
    return p


# 文件名里**在 Windows 上非法**的字符。注意是 ASCII 的那几个：
#   `:` 还顺带挡住盘符（C:\）和 NTFS 备用数据流（报告.pdf:hidden.exe）
# 全角的 ＜＞：？ 在中文文件名里是正常的，不在此列——
# 把它们一起挡掉就会误伤「差旅问答：住宿费篇.docx」这种真实文件名。
# 分隔符不在这里：`\` 在入口就统一成了 `/`，而 `/` 是切段用的字符。
_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"|?*]')

# Windows 保留设备名。带不带扩展名都不行：CON.txt 一样创建不了。
_WIN_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                 | {"COM%d" % i for i in range(1, 10)}
                 | {"LPT%d" % i for i in range(1, 10)})

# 单段字节上限。NTFS 的限制是 255 字节，中文 UTF-8 一个字 3 字节，
# 所以按字符算会在 85 字之后悄悄失败；按字节算才是真的那条线。
MAX_SEG_BYTES = 200
# 完整路径上限。Windows 默认 MAX_PATH=260，超了 open() 直接报错，
# 而托管根本身就占掉一百多个字符（项目路径很长），所以必须预留着算。
MAX_FULL_PATH = 240


def safe_rel(rel):
    """把客户端给的文件名 / 相对路径收成一个安全的相对路径。→ (rel_path, warns)

    **这是上传链路上唯一的路径闸。** 浏览器会把整个文件夹的相对路径
    （webkitRelativePath）原样交上来，那是外来输入，直接参与拼路径。

    误报与漏报两个方向都要测（全局纪律第 9 条），而这里**误报的代价更大**：
    漏掉一个穿越路径，攻击者要先能访问到这个内网 POC；而误伤一个正常文件名，
    客户当场就会说「我这份制度传不上去」。所以挡的是**确定非法**的那些，
    不是「看着可疑」的那些：

      挡：目录穿越（..）、盘符与 NTFS 数据流（:）、控制字符、<>"|?*
          Windows 保留设备名、超长段、超长总路径
          以及**扫描会跳过的那些名字**（~$ / .~ 前缀、$ 开头的目录段、
          node_modules 之类 SKIP_DIRS）——见下
      放：中文、空格、《》〔〕（）、& ' # % [] {} ! + = @ - _ ~ 以及全角标点

    为什么「扫描会跳过的名字」也要挡：
      上传落盘之后是**复用 scan() 做登记与解析**的，而 _walk 会跳过 ~$ 锁文件、
      $ 开头的目录和 SKIP_DIRS。那种文件落了盘却永远不出现在清单里——
      使用者看到的是「传成功了但库里没有」，而磁盘上其实已经有了。
      上传接受的集合必须是扫描登记集合的子集，这条有单测钉住。
    """
    if rel is None:
        raise PathError("文件名为空")
    s = str(rel).replace("\\", "/").strip()
    if not s:
        raise PathError("文件名为空")
    warns = []
    segs = []
    for raw in s.split("/"):
        seg = raw.strip()
        if seg in ("", "."):
            continue                      # 「a//b」「./a」是客户端拼出来的噪声，不是攻击
        if seg == "..":
            raise PathError("路径里不许出现「..」：%s" % s)
        # 段尾的点和空格：Windows 会**静默去掉**，于是磁盘上的名字和库里登记的
        # 不一致，而 abs_path 是主键级别的东西。这里主动规范化并说一声。
        trimmed = seg.rstrip(". ")
        if trimmed != seg:
            if not trimmed:
                raise PathError("文件名只有点或空格：%s" % raw)
            warns.append("「%s」末尾的点或空格被去掉（Windows 会静默去掉，"
                         "留着会让磁盘上的名字和登记的对不上）" % seg)
            seg = trimmed
        bad = _ILLEGAL_CHARS.search(seg)
        if bad:
            ch = bad.group(0)
            raise PathError("文件名里有不能用的字符「%s」：%s%s"
                            % (repr(ch)[1:-1], raw,
                               "（含盘符或 NTFS 数据流）" if ch == ":" else ""))
        if seg.rsplit(".", 1)[0].upper() in _WIN_RESERVED:
            raise PathError("「%s」是 Windows 保留的设备名，建不出这个文件" % seg)
        if seg.startswith(SKIP_FILE_PREFIX):
            raise PathError("「%s」是 Office 打开文档时生成的锁文件（%s 开头），"
                            "不是制度文件；扫描阶段也会跳过它，"
                            "收进来只会变成一条「解析失败」"
                            % (seg, "/".join(SKIP_FILE_PREFIX)))
        if len(seg.encode("utf-8")) > MAX_SEG_BYTES:
            raise PathError("名字太长（上限 %d 字节，约 %d 个汉字）：%s"
                            % (MAX_SEG_BYTES, MAX_SEG_BYTES // 3, seg[:40] + "…"))
        segs.append(seg)
    if not segs:
        raise PathError("路径里没有有效的文件名：%s" % s)
    # 除最后一段（文件名）以外都是目录段，要按 _walk 的跳过规则再过一遍
    for seg in segs[:-1]:
        if seg.startswith("$"):
            raise PathError("文件夹名不能以 $ 开头（扫描阶段会跳过它）：%s" % seg)
        if seg in SKIP_DIRS:
            raise PathError("「%s」在扫描跳过名单里（与制度资料无关、往往巨大的目录），"
                            "传进来也不会被登记。请把文件放到别的文件夹名下" % seg)
    if len(segs) - 1 >= MAX_DEPTH:
        raise PathError("目录层数超过 %d 层：%s" % (MAX_DEPTH, s))
    ext = os.path.splitext(segs[-1])[1].lower()
    if ext not in DOC_EXT:
        raise PathError("不收这种扩展名「%s」。文件库只收制度类文档：%s"
                        % (ext or "（无扩展名）", "、".join(sorted(DOC_EXT))))
    return "/".join(segs), warns


def drives():
    """Windows 盘符；非 Windows 回落到根目录与用户目录。"""
    if os.name != "nt":
        return [{"name": "/", "path": "/"},
                {"name": "~", "path": os.path.expanduser("~")}]
    out = []
    for d in string.ascii_uppercase:
        root = "%s:\\" % d
        if os.path.exists(root):
            out.append({"name": "%s:" % d, "path": root})
    return out


def browse(path=None):
    """列出一层：子文件夹 + 本层可登记的文件数。不传 path 则给盘符。"""
    if not path:
        return {"path": None, "parent": None, "drives": drives(), "dirs": [], "files": 0}
    p = safe_dir(path)
    dirs, files = [], 0
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if e.name in SKIP_DIRS or e.name.startswith("$"):
                            continue
                        dirs.append({"name": e.name, "path": e.path})
                    elif e.is_file(follow_symlinks=False):
                        if (os.path.splitext(e.name)[1].lower() in DOC_EXT
                                and not e.name.startswith(SKIP_FILE_PREFIX)):
                            files += 1
                except OSError:
                    continue
    except PermissionError:
        raise PathError("无权限读取：%s" % p)
    dirs.sort(key=lambda x: x["name"])
    parent = os.path.dirname(p.rstrip("\\/")) or None
    if parent == p:
        parent = None
    return {"path": p, "parent": parent, "drives": drives(), "dirs": dirs, "files": files}


# ── 文件级属性：分类 / 文号 / 标题 / 发文年份 ───────────────────────────────
# 这四样都是**文件自己的属性**，所以在这一栏算，不在规则抽取那一栏算。
# 边界：本栏只回答「有哪些文件、是什么、能不能识别」，不回答「里面写了什么」。

# 文书范本：合同、申请书、模板。里面也有「第X条」，但那是文书条款不是制度条款。
TEMPLATE_WORDS = ("模板", "范本", "样表", "样本", "合同", "协议", "承诺书",
                  "确认书", "授权书", "声明", "申请书", "保函", "约定",
                  # 「表单」「附表」是**空白表格**，和填好的单据不是一回事。
                  # 搬运词表时漏了这两个，「出差审批表单」就掉进了「其他」——
                  # 既不算范本也不算单据，于是它会被当成候选来源参与统计。
                  "表单", "附表")
# 业务单据：报账单、审批意见、明细表。实扫 14 份客户资料里有 5 份是这个，
# 混进「制度」统计会让「还有多少条没变成规则」整体失真。
RECEIPT_WORDS = ("报账单", "审批意见", "明细表", "申请单", "结算单", "对账单",
                 "发票", "行程单", "台账", "审批单", "报销单")
POLICY_WORDS = ("管理办法", "审核要点", "规定", "细则", "指引", "制度", "标准",
                "办法", "通知", "规程", "须知", "解答", "问答", "口径", "规则", "要点")

# 文号：机关字〔年〕序号 号。方括号/圆括号都认——实测文件名里三种括号都出现过。
DOC_NO = re.compile(
    r"[一-鿿]{2,12}\s*[〔\[\(（]\s*(\d{4})\s*[〕\]\)）]\s*(\d{1,4})\s*号")
# 标题：书名号里的整串
DOC_TITLE = re.compile(r"《([^《》]{2,80})》")


def classify(name):
    """按文件名判文件类别。**判不出就是「其他」，不猜。**

    顺序是有讲究的：范本 → 单据 → 制度。
    「出差审批表单」既像单据又像范本，它是空白模板，按范本算；
    「差旅费审批意见」是具体某一单的意见，按单据算。
    顺序反过来，模板就会被当成真单据。
    """
    stem = name.rsplit(".", 1)[0]
    if any(w in stem for w in TEMPLATE_WORDS):
        return "范本"
    if any(w in stem for w in RECEIPT_WORDS):
        return "单据"
    if any(w in stem for w in POLICY_WORDS):
        return "制度"
    return "其他"


def parse_doc_meta(name):
    """从**文件名**取文号、标题、发文年份。→ (doc_number, doc_title, year)

    **只认文件名上的硬证据，不碰正文。** 实测 OCR 把「闽港财智〔2025〕20号」
    识别成「赣港财知2025] 20号」——拿正文去匹配文号必然错配，
    而错配给出的是一条看起来有据可查、实际指错地方的证据链。
    取不到就返回 None，宁可空着。
    """
    stem = name.rsplit(".", 1)[0]
    no = year = None
    m = DOC_NO.search(stem)
    if m:
        no = m.group(0).strip()
        year = int(m.group(1))
    titles = DOC_TITLE.findall(stem)
    # 多个书名号时取最长的：「关于印发《业务接待管理办法》的通知（…）」
    # 要的是被印发的那份办法，不是「通知」本身。
    title = max(titles, key=len) if titles else None
    return no, title, year


# ── 文件夹（根）的增删启停 ───────────────────────────────────────────────────
def _rid(abs_path):
    """按路径定 ID：同一个文件夹移出再加回来，ID 不变，历史对得上。"""
    return "dlr-" + hashlib.sha1(
        os.path.normcase(abs_path).encode("utf-8", "replace")).hexdigest()[:10]


def _fid(abs_path):
    return "dlf-" + hashlib.sha1(
        os.path.normcase(abs_path).encode("utf-8", "replace")).hexdigest()[:12]


def list_roots(instance_id=None):
    """instance_id=None 表示**全部实例**。

    为什么默认不过滤：add_roots 的嵌套检查必须看全部——abs_path 是全局唯一的，
    两个实例各加一次同一个目录，第二次会撞唯一键，而「已在库中」的提示
    必须说得出它在哪个实例下。接口层传当前实例，内部校验传 None。
    """
    sql = ["""
        SELECT r.*, (SELECT count(*) FROM doc_lib_file f
                      WHERE f.root_id = r.root_id AND NOT f.missing) AS files,
               i.label AS instance_label, i.domain_id, d.label AS domain_label
          FROM doc_lib_root r
          LEFT JOIN domain_instance i ON i.instance_id = r.instance_id
          LEFT JOIN domain d          ON d.domain_id   = i.domain_id"""]
    args = []
    if instance_id:
        sql.append("WHERE r.instance_id = %s"); args.append(instance_id)
    # 托管根排在最前：它是默认入口（上传落在那里），引用根是补充手段。
    sql.append("ORDER BY (r.kind <> '托管'), r.added_at")
    rows = db.query(" ".join(sql), tuple(args))
    for r in rows:
        # 文件夹可能被人删了或盘没挂上。这里如实回报，但**不自动删登记**：
        # 盘没插就把登记清掉，等于一次误操作毁掉整条溯源链。
        r["exists"] = os.path.isdir(r["abs_path"])
        r["writable"] = r["kind"] == "托管"
    return rows


# ── 托管根：客户端上传落在这里 ───────────────────────────────────────────────
def managed_dir(inst):
    """托管目录的绝对路径。inst 是 resolve_instance() 的返回。

    目录名用 **id 不用中文展示名**：展示名允许改（0032 的迁移刻意不覆盖人改过的
    展示名），改了磁盘目录不会跟着改，abs_path 就对不上了。
    """
    return os.path.join(config.DOCLIB_DIR, inst["domain_id"], inst["instance_id"])


def ensure_managed_root(instance_id=None):
    """取（必要时创建）这个实例的托管根。**幂等**，可以每次上传都调。

    为什么不让用户手动「新建托管目录」：那是一个纯粹的仪式——
    用户真正想做的是「把文件传上去」，中间插一步建目录只会多一个
    「忘了建目录所以传不上去」的失败态。目录和登记行都由系统保证存在。
    """
    inst = resolve_instance(instance_id)
    path = managed_dir(inst)
    os.makedirs(path, exist_ok=True)      # 托管区是我们自己的目录，建它不违反只读纪律
    rid = _rid(path)
    row = db.query_one("SELECT * FROM doc_lib_root WHERE root_id = %s", (rid,))
    if not row:
        label = "%s / %s · 上传" % (inst["domain_label"], inst["instance_label"])
        db.execute("""
            INSERT INTO doc_lib_root(root_id, abs_path, label, enabled, kind,
                                     instance_id, note)
            VALUES (%s, %s, %s, TRUE, '托管', %s, %s)
            ON CONFLICT (abs_path) DO NOTHING""",
            (rid, path, label, inst["instance_id"],
             "系统托管目录：客户端上传的制度文件落在这里。"
             "这是本系统自己的工作区，允许写入与删除；"
             "客户的资料目录请用「纳入服务器已有目录」，那种是只读的。"))
        row = db.query_one("SELECT * FROM doc_lib_root WHERE root_id = %s", (rid,))
    # 已有登记但被人停用过：上传时要显式报出来，不要默默传进一个停用的根
    # （停用的根不参与扫描，文件会落盘却永远登记不上）。
    if row and not row["enabled"]:
        raise ScopeError("「%s」已被停用。停用的根不参与扫描，文件会落盘却登记不上。"
                         "请先在文件来源那一页启用它，再上传。" % row["label"])
    if row and row["kind"] != "托管":
        # 理论上到不了这里（唯一索引 + id 由路径算）。真到了说明有人手改过库，
        # 这时**宁可拒绝也不能往引用根里写**。
        raise ScopeError("路径 %s 已被登记为「%s」根，不能当托管目录用" % (path, row["kind"]))
    return row


def _nested(a, b):
    """a 是否在 b 之下（含相等）。嵌套的两个文件夹会把同一文件算两遍。"""
    a = os.path.normcase(a.rstrip("\\/"))
    b = os.path.normcase(b.rstrip("\\/"))
    return a == b or a.startswith(b + os.sep)


def add_roots(paths, label=None, instance_id=None):
    """纳入服务器上已有的目录，**原地只读扫描**（kind='引用'）。

    一次可加多个，**逐个独立判定**：一个路径不对，不影响其余的加入。
    """
    inst = resolve_instance(instance_id)
    # 嵌套/重复检查要看**全部实例**：abs_path 是全局唯一的，
    # 只看本实例会让第二个实例加同一个目录时撞唯一键报 500。
    cur = {r["abs_path"]: r for r in list_roots()}
    out = []
    for p in paths or []:
        try:
            full = safe_dir(p)
        except PathError as e:
            out.append({"path": p, "ok": False, "reason": str(e)})
            continue
        # 托管区不许当引用根纳入：那样同一批文件会有两个登记（一个可写一个只读），
        # 而「能不能删这个文件」就取决于从哪条登记看它。
        # **这一条排在「已在库中」之前**：托管根本来就已经在库里，
        # 按顺序先判重就只会回一句「已在库中」，而使用者真正需要知道的是
        # 「这个目录是系统托管区，它的文件该用上传入口管」。
        if _nested(full, config.DOCLIB_DIR) or _nested(config.DOCLIB_DIR, full):
            out.append({"path": full, "ok": False,
                        "reason": "这是系统托管区（%s）或它的上级目录，不能当引用根纳入。"
                                  "托管区里的文件请用上传入口管理" % config.DOCLIB_DIR})
            continue
        if full in cur:
            owner = cur[full]
            same = owner.get("instance_id") == inst["instance_id"]
            out.append({"path": full, "ok": False,
                        "reason": "已在库中" if same else
                                  "已归属于另一个实例「%s」。同一个目录不能同时属于两个"
                                  "实例——那会让「这条规则的依据文件属于谁」两头都说得通"
                                  % (owner.get("instance_label") or owner.get("instance_id"))})
            continue
        overlap = [q for q in cur if _nested(full, q) or _nested(q, full)]
        note = ("与已有文件夹嵌套：%s；扫描按绝对路径去重，文件归属先到先得"
                % "、".join(overlap)) if overlap else None
        rid = _rid(full)
        db.execute("""
            INSERT INTO doc_lib_root(root_id, abs_path, label, enabled, kind,
                                     instance_id, note)
            VALUES (%s, %s, %s, TRUE, '引用', %s, %s)
            ON CONFLICT (abs_path) DO NOTHING""",
            (rid, full, label or os.path.basename(full.rstrip("\\/")) or full,
             inst["instance_id"], note))
        cur[full] = {"abs_path": full, "instance_id": inst["instance_id"]}
        out.append({"path": full, "ok": True, "root_id": rid, "kind": "引用",
                    "instance_id": inst["instance_id"], "note": note})
    return out


def set_root(root_id, enabled=None, label=None):
    n = 0
    if enabled is not None:
        n += db.execute("UPDATE doc_lib_root SET enabled=%s WHERE root_id=%s",
                        (bool(enabled), root_id))
    if label is not None:
        n += db.execute("UPDATE doc_lib_root SET label=%s WHERE root_id=%s",
                        (label, root_id))
    return n > 0


def remove_root(root_id):
    """移出文件库 = 删登记。**磁盘上的文件一个不动。**

    托管根是例外：它里面有文件时**拒绝移出**。理由是后果不对称——
    引用根的文件本来就在客户目录里，删了登记文件还在原处；
    而托管根一删，doc_lib_file 行跟着级联消失（0017/0018/0027/0029 四张表
    都是 ON DELETE CASCADE），条款、OCR、读图、规则明细一起没了，
    磁盘上却留下一堆再也没有登记指向的文件。要清空托管根请逐份删文件。
    """
    r = db.query_one("SELECT * FROM doc_lib_root WHERE root_id=%s", (root_id,))
    n = db.scalar("SELECT count(*) FROM doc_lib_file WHERE root_id=%s", (root_id,)) or 0
    if r and r["kind"] == "托管" and n:
        raise ConflictError(
            "「%s」是系统托管目录，里面还有 %d 份文件登记，不能整个移出。"
            "整体移出会把条款、OCR、读图结果、规则明细一起级联删掉，"
            "而磁盘上的文件会留下来变成没人认领的孤儿。请先逐份删除文件。"
            % (r["label"], n))
    gone = db.execute("DELETE FROM doc_lib_root WHERE root_id=%s", (root_id,))
    return {"root_id": root_id, "removed": gone > 0, "files_unregistered": n,
            "note": "只删登记，磁盘上的文件未做任何改动"}


# ── 轻解析 ──────────────────────────────────────────────────────────────────
_TAG = re.compile(r"<[^>]+>")


def _decode(b):
    """中文资料里 gb18030 比 utf-8 还常见，顺序试过去，最后兜底不抛异常。"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return b.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace"), "utf-8/replace"


def _docx_text(path):
    """用 python-docx 取 docx 正文：段落 + 表格。

    **表格必须取**：差旅制度里最关键的东西（住宿费标准、交通工具等级）
    几乎都在表里。只取段落会让一份「有标准表的制度」解析出一堆空话，
    而状态还显示「已解析」——这比解析失败更难发现。
    """
    import docx
    d = docx.Document(path)
    lines = [p.text.strip() for p in d.paragraphs if p.text and p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            # 合并单元格会让同一个值在一行里重复出现，去掉相邻重复
            dedup = [c for i, c in enumerate(cells) if c and (i == 0 or c != cells[i - 1])]
            if dedup:
                lines.append(" | ".join(dedup))
    return "\n".join(lines)


def _one_line(s):
    """单元格文本压成一行：换行/制表位替成空格，首尾去空。

    **一行必须还是一行**——行是列位置的载体，断了行就等于丢了列。
    """
    return re.sub(r"[\r\n\t]+", " ", s).strip()


def _xlsx_text(path):
    """用 openpyxl 只读模式取 xlsx 文本。**空单元格保留位置，不丢列。**

    第一版写的是「不求还原版式，只求字都在」，过滤掉了空单元格：
        cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
    对全文检索够用，但它把**列的位置**弄丢了，而规则表的全部价值在列上。
    实测代价：
      《系统审核算法.xlsx》表头 15 列，而「审核算法规则」整列是空的，
      于是数据行导出成 14 个值，「控制类型」之后的所有列左移一格——
      按位置读，「控制类型」读到的是「自定义」，「状态」读到的是更新时间。
      《差旅费审核规则.xlsx》第 2 行「是否已有」空着，评估意见整体左移一列。
    **这和 OCR 那条教训是同一句话：丢的不是字，是结构。**
    在映射那头靠闭集合猜对齐，只是给这里的损失打补丁，而且猜不准——
    实测 6 行算法有 7 种对齐都说得通。

    只裁**尾部**空列：Excel 的工作表常带一串没用过的空列，
    不裁会让每行都变成几十列宽。中间的空列必须留着，它才是位置本身。
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        lines = []
        for ws in wb.worksheets:
            lines.append("# %s" % ws.title)
            for row in ws.iter_rows(values_only=True):
                # 单元格里的换行要替成空格。**一行必须还是一行。**
                # 不替的代价：一个多行单元格会把这一行拆成好几行文本，
                # 实测《财务共享业务场景…》的「专用审核要点」单元格写的是
                # 「1.行程日期审核：…\n2.金额核对：…」，于是整行断在第一句后面，
                # 后面几句变成几个 3～4 列的碎片行——列数对不上，要点也丢了大半。
                # docx 那一路（_docx_text）早就这么做了，xlsx 这一路漏了。
                cells = ["" if v is None else _one_line(str(v))
                         for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        wb.close()


def _pdf_text(path):
    """用 PyMuPDF 取 PDF 文字层 → (正文, 总页数, 有字的页数)。

    **不在这里跑 OCR**。扫描阶段只回答「有没有文字层」：
    有就直接取（快、准），没有就标「需OCR」交给下一步。
    OCR 一页要几百毫秒到几秒，混进扫描会让重扫变成一件没人愿意干的事，
    而重扫不跑，快照就不再反映现状——那这个模块就白做了。
    """
    import fitz
    doc = fitz.open(path)
    try:
        pages, with_text = [], 0
        for page in doc:
            t = (page.get_text() or "").strip()
            if t:
                with_text += 1
                pages.append(t)
        return "\n".join(pages), doc.page_count, with_text
    finally:
        doc.close()


def extract_text(path, size=None):
    """→ (state, text, reason)。state 取值见 STATES。

    返回的 reason 在非「已解析」时必须有话说，而且要带下一步动作。
    """
    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path) if size is None else size
    if size == 0:
        return "空文件", "", "文件长度为 0 字节"

    if ext in NO_PARSER:
        return "未解析", "", NO_PARSER[ext]

    cap = (MAX_OOXML_BYTES if ext in (".docx", ".xlsx")
           else MAX_PDF_BYTES if ext == ".pdf"
           else MAX_PARSE_BYTES)
    if size > cap:
        return "未解析", "", "未解析：文件 %.1f MB 超过该类型解析上限 %d MB" % (
            size / 1048576, cap >> 20)

    try:
        reason = None
        if ext in TEXT_EXT:
            with open(path, "rb") as f:
                raw = f.read()
            txt, enc = _decode(raw)
            if ext in (".html", ".htm", ".xml"):
                txt = _TAG.sub(" ", txt)
            reason = "编码 %s" % enc
        elif ext == ".docx":
            txt = _docx_text(path)
        elif ext == ".xlsx":
            txt = _xlsx_text(path)
        elif ext == ".pdf":
            txt, pages, with_text = _pdf_text(path)
            if with_text == 0:
                return "需OCR", "", (
                    "需OCR：PDF 共 %d 页，一页都没有文字层（纯扫描件）。"
                    "本机已装 tesseract，可在规则抽取阶段按需 OCR。" % pages)
            if with_text < pages:
                # 混合件：封面是图、正文有字，或反过来。必须说出来——
                # 只报「已解析」会让人以为整份都读到了。
                reason = ("PDF 共 %d 页，其中 %d 页无文字层（这些页未取到内容，"
                          "需 OCR 才能补全）" % (pages, pages - with_text))
            else:
                reason = "PDF %d 页，均有文字层" % pages
        else:
            return "未解析", "", "未解析：未支持的扩展名 %s" % ext
    except ImportError as e:
        return "解析失败", "", "解析失败：缺少解析库（%s）" % e
    except (OSError, PermissionError) as e:
        return "解析失败", "", "解析失败：读不到文件（%s）" % e
    except Exception as e:
        # 解析第三方文档格式什么都可能炸（加密、损坏、改过扩展名）。
        # 这里兜住并**写明是哪一类异常**，不让一份坏文件把整次扫描带崩。
        return "解析失败", "", "解析失败：%s: %s" % (type(e).__name__, e)

    txt = re.sub(r"[ \t\u3000]+", " ", txt or "").strip()
    if not txt:
        return "空文件", "", "解析成功但没有任何文字内容"
    if len(txt) > MAX_TEXT_CHARS:
        return "已解析", txt[:MAX_TEXT_CHARS], "正文 %d 字，超 %d 字上限已截断" % (
            len(txt), MAX_TEXT_CHARS)
    return "已解析", txt, reason


# ── 扫描 ────────────────────────────────────────────────────────────────────
def _walk(root, warns):
    """递归遍历，带层数/条数闸门，**不跟随符号链接**（防跳出根、防成环）。"""
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        depth = dirpath[len(root):].count(os.sep)
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            warns.append("深度超过 %d 层，未再深入：%s" % (MAX_DEPTH, dirpath))
            continue
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith("$")
                       and not os.path.islink(os.path.join(dirpath, d))]
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in DOC_EXT or fn.startswith(SKIP_FILE_PREFIX):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                continue
            n += 1
            if n > MAX_FILES:
                warns.append("文件数超过上限 %d，扫描中止（本次结果不完整）" % MAX_FILES)
                return
            yield full, ext


def _fmt_size(n):
    return "%.1f KB" % (n / 1024) if n < 1048576 else "%.1f MB" % (n / 1048576)


def scan(root_ids=None, parse=True, force=False, instance_id=None):
    """重扫已启用的文件夹。**快照与变化感知就靠它。**

    不传 root_ids 时**只扫当前（或指定）实例的根**；显式传 root_ids 则照传的扫，
    这时批次的 instance_id 留空——跨实例扫算不出单一归属，不挑一个填上去。

    第二次及以后的扫描要回答的是「相对上次变了什么」，所以每次扫描都落一行
    doc_lib_scan，每个变化落一行 doc_lib_change。只把数字塞进返回值是不够的：
    返回值一刷新就没了，而规则抽取是隔天隔周才做的事，做的时候要能回头查
    「从我上次抽取到现在，制度库动过哪几份」。

    force=True 忽略指纹，对所有文件重跑解析。改过解析器之后必须用它：
    文件没动，指纹就没变，不强制的话新解析器一份也跑不到——
    那会得到一个「改了但看不出改了」的假象。
    **强制重扫不算「修改」**：文件本身没动，记成修改会把台账污染成一片假变更。
    """
    mode = "强制重扫" if force else ("增量" if parse else "仅登记")
    ts = datetime.datetime.now().replace(microsecond=0)
    # **扫描是事件，没有自然键，所以 ID 必须随机。**
    # 第一版拿「秒级时间戳+模式」做 ID，同一秒内扫两次就撞主键报 500——
    # 连点两下「扫描」就能踩到。文件夹和文件用路径做确定性 ID 是对的（同一个
    # 路径就该是同一行），但那条道理不能照搬到事件上。
    scan_id = "dls-" + uuid.uuid4().hex[:12]

    if root_ids is None:
        scope = resolve_instance(instance_id)["instance_id"]
        roots = [r for r in list_roots(scope) if r["enabled"]]
    else:
        scope = None          # 显式指定根 → 可能跨实例，不给批次挑归属
        roots = [r for r in list_roots() if r["enabled"] and r["root_id"] in root_ids]
    db.execute("INSERT INTO doc_lib_scan(scan_id, started_at, mode, root_cnt, instance_id) "
               "VALUES (%s,%s,%s,%s,%s)", (scan_id, ts, mode, len(roots), scope))

    known = {r["abs_path"]: r for r in db.query(
        """SELECT abs_path, file_id, fingerprint, root_id, file_size, mtime,
                  parse_state, missing, rel_path, file_name FROM doc_lib_file""")}
    scanned_roots = {r["root_id"] for r in roots}
    seen = set()
    added = updated = unchanged = 0
    warns, per_root, changes = [], [], []

    def note_change(kind, fid, full, rel, name, detail):
        changes.append({"change": kind, "file_id": fid, "abs_path": full,
                        "rel_path": rel, "file_name": name, "detail": detail})
        db.execute("""INSERT INTO doc_lib_change(
                          scan_id, file_id, abs_path, rel_path, file_name, change, detail)
                      VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                   (scan_id, fid, full, rel, name, kind, detail))

    for r in roots:
        if not os.path.isdir(r["abs_path"]):
            warns.append("文件夹已不存在，跳过：%s" % r["abs_path"])
            per_root.append({"root_id": r["root_id"], "path": r["abs_path"],
                             "files": 0, "note": "文件夹已不存在"})
            continue
        cnt = 0
        # 来源由**根的类型**推出，不额外存一处：托管根里的文件只能是传进来的
        # （那个目录除了上传没有别的写入口），引用根里的文件只能是原地扫到的。
        # 存两处必然对不上（全局纪律第 9 条的同类）。
        origin = "上传" if r["kind"] == "托管" else "扫描"
        for full, ext in _walk(r["abs_path"], warns):
            if full in seen:          # 嵌套文件夹：先到先得，不重复登记
                continue
            seen.add(full)
            cnt += 1
            try:
                st = os.stat(full)
            except OSError as e:
                warns.append("读不到文件信息，跳过：%s（%s）" % (full, e))
                continue
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).replace(microsecond=0)
            sig = hashlib.sha1(("%s|%d|%s" % (full, st.st_size, mtime.isoformat()))
                               .encode("utf-8", "replace")).hexdigest()[:16]
            old = known.get(full)
            rel = os.path.relpath(full, r["abs_path"])
            name = os.path.basename(full)
            # 文件级属性按文件名算，和内容无关——所以在这一栏算，不在抽取那一栏算
            kind = classify(name)
            dno, dtitle, dyear = parse_doc_meta(name)

            # 指纹没变 → 文件确实没动。force 时照样重解析，但**不记成变化**。
            if old and old["fingerprint"] == sig and not force:
                # 归属与来源每次都按根回写：列注释承诺了「以根为准，不会漂移」，
                # 只在变化分支回写就会让「没变过的文件」停在旧归属上。
                db.execute("""UPDATE doc_lib_file
                                 SET last_seen_at=%s, missing=FALSE, last_change='未变',
                                     instance_id=%s, origin=%s
                               WHERE file_id=%s""",
                           (ts, r["instance_id"], origin, old["file_id"]))
                unchanged += 1
                continue

            if parse:
                state, body, reason = extract_text(full, st.st_size)
            else:
                state, body, reason = "未扫描", "", "本次只登记未解析"
            fid = old["file_id"] if old else _fid(full)

            if old:
                really_changed = old["fingerprint"] != sig
                if really_changed:
                    bits = []
                    if old["file_size"] != st.st_size:
                        bits.append("大小 %s → %s" % (_fmt_size(old["file_size"]),
                                                      _fmt_size(st.st_size)))
                    # 库里是 TIMESTAMPTZ（带时区），磁盘上的 mtime 是本地朴素时间。
                    # 直接 replace(tzinfo=None) 比较会把 UTC 当本地，于是每次都显示
                    # 「修改时间变了」——判定本身不受影响（那靠指纹），但台账会撒谎。
                    old_mt = old["mtime"].astimezone().strftime("%Y-%m-%d %H:%M:%S") \
                        if old["mtime"] else None
                    new_mt = mtime.strftime("%Y-%m-%d %H:%M:%S")
                    if old_mt and old_mt != new_mt:
                        bits.append("修改时间 %s → %s" % (old_mt, new_mt))
                    if old["parse_state"] != state:
                        bits.append("解析状态 %s → %s" % (old["parse_state"], state))
                    if old["missing"]:
                        bits.append("上次标记为消失，这次又出现了")
                    detail = "；".join(bits) or "指纹变化"
                db.execute("""
                    UPDATE doc_lib_file SET root_id=%s, rel_path=%s, file_name=%s, ext=%s,
                           file_size=%s, mtime=%s, fingerprint=%s, parse_state=%s,
                           parse_reason=%s, text_chars=%s, body=%s,
                           doc_kind=%s, doc_number=%s, doc_title=%s, issued_year=%s,
                           last_seen_at=%s, missing=FALSE,
                           last_change=%s, changed_at=%s,
                           instance_id=%s, origin=%s
                     WHERE file_id=%s""",
                    (r["root_id"], rel, name, ext, st.st_size, mtime, sig, state,
                     reason, len(body), body, kind, dno, dtitle, dyear, ts,
                     "修改" if really_changed else "未变",
                     ts if really_changed else None,
                     r["instance_id"], origin, fid))
                if really_changed:
                    updated += 1
                    note_change("修改", fid, full, rel, name, detail)
                else:
                    unchanged += 1
            else:
                db.execute("""
                    INSERT INTO doc_lib_file(
                        file_id, root_id, abs_path, rel_path, file_name, ext, file_size,
                        mtime, fingerprint, parse_state, parse_reason, text_chars, body,
                        doc_kind, doc_number, doc_title, issued_year,
                        first_seen_at, last_seen_at, missing, last_change, changed_at,
                        instance_id, origin)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            %s,%s,FALSE,'新增',%s,%s,%s)
                    ON CONFLICT (abs_path) DO NOTHING""",
                    (fid, r["root_id"], full, rel, name, ext, st.st_size, mtime, sig,
                     state, reason, len(body), body, kind, dno, dtitle, dyear,
                     ts, ts, ts, r["instance_id"], origin))
                added += 1
                note_change("新增", fid, full, rel, name,
                            "%s · %s · %s" % (ext, _fmt_size(st.st_size), state))
        db.execute("UPDATE doc_lib_root SET last_scan_at=%s WHERE root_id=%s",
                   (ts, r["root_id"]))
        per_root.append({"root_id": r["root_id"], "path": r["abs_path"], "files": cnt})

    # 消失的只标记、不删行：删掉就说不清「规则当初依据的是哪份文件」。
    # 只在**本次真的扫了的根**里判消失——没扫的根里的文件当然看不见，
    # 把它们一并标成消失是纯粹的误报。
    gone = [p for p, v in known.items()
            if p not in seen and v["root_id"] in scanned_roots and not v["missing"]]
    for p in gone:
        v = known[p]
        db.execute("""UPDATE doc_lib_file SET missing=TRUE, last_change='消失', changed_at=%s
                       WHERE abs_path=%s""", (ts, p))
        note_change("消失", v["file_id"], p, v["rel_path"], v["file_name"],
                    "上次扫描时还在，这次没找到。可能是删除、改名、移走，"
                    "也可能是盘没挂上——登记保留，判断留给人")

    db.execute("""UPDATE doc_lib_scan SET finished_at=now(), added=%s, updated=%s,
                         unchanged=%s, missing=%s, in_lib=%s, warnings=%s
                   WHERE scan_id=%s""",
               (added, updated, unchanged, len(gone), len(seen),
                "\n".join(warns) or None, scan_id))

    return {"scan_id": scan_id, "scanned_at": ts.isoformat(sep=" "), "mode": mode,
            "roots": per_root, "added": added, "updated": updated,
            "unchanged": unchanged, "missing": len(gone), "in_lib": len(seen),
            "changes": changes[:500], "changes_total": len(changes),
            "warnings": warns,
            "note": "消失的文件只标记不删登记，否则说不清规则当初依据的是哪份文件"}


# ── 变化台账与待抽取 ────────────────────────────────────────────────────────
def list_scans(limit=20, instance_id=None):
    """批次台账。instance_id 传了就只给这个实例的批次。

    **跨实例批次（instance_id IS NULL）也一并给出**：那是显式指定 root_ids 扫出来的，
    它确实影响了本实例的文件。按「= 当前实例」硬过滤会把它藏起来，
    于是台账上会缺一次真实发生过的扫描。
    """
    sql = ["SELECT * FROM doc_lib_scan"]
    args = []
    if instance_id:
        sql.append("WHERE instance_id = %s OR instance_id IS NULL")
        args.append(instance_id)
    sql.append("ORDER BY started_at DESC LIMIT %s")
    args.append(max(1, min(200, int(limit))))
    return db.query(" ".join(sql), tuple(args))


def list_changes(scan_id=None, change=None, limit=500, instance_id=None):
    """变化明细。不传 scan_id 就给最近一次扫描的——
    「这次扫出什么」是最常被问的那个问题，不该要求调用方先去查批次号。

    按实例过滤走的是**批次**，不是逐行过滤变化。doc_lib_change 刻意没有外键
    （0016 的列注释：文件登记可能已经不在了，变化记录要留着），
    拿 file_id 去 join doc_lib_file 会把「消失」那一档过滤掉——
    而那一档恰恰是变化台账最该显示的。
    """
    if not scan_id:
        sql = ["SELECT scan_id FROM doc_lib_scan"]
        args = []
        if instance_id:
            sql.append("WHERE instance_id = %s OR instance_id IS NULL")
            args.append(instance_id)
        sql.append("ORDER BY started_at DESC LIMIT 1")
        scan_id = db.scalar(" ".join(sql), tuple(args))
    if not scan_id:
        return {"scan_id": None, "items": [], "total": 0,
                "note": "还没有扫描过，没有可比的基线"}
    sql = ["SELECT * FROM doc_lib_change WHERE scan_id=%s"]
    args = [scan_id]
    if change:
        sql.append("AND change=%s"); args.append(change)
    sql.append("ORDER BY change, rel_path LIMIT %s")
    args.append(max(1, min(2000, int(limit))))
    items = db.query(" ".join(sql), tuple(args))
    return {"scan_id": scan_id, "items": items, "total": len(items),
            "scan": db.query_one("SELECT * FROM doc_lib_scan WHERE scan_id=%s", (scan_id,))}


def pending_extract(limit=500, instance_id=None):
    """待抽取：能取到正文、且（从没抽过 或 抽过之后又被改了）的文件。

    用指纹比对算，不设 pending 标志位——标志位忘了清，会出现
    「永远待抽取」或「改了却不提示」，两种都很难发现。
    """
    # 两条查询共用同一个实例条件。**不要用 % 拼进 SQL 模板里再混 %%s**——
    # 那种写法看一眼分不清哪个 % 是 Python 的哪个是 psycopg 的，改一次就错一次。
    scope = ["AND instance_id = %s"] if instance_id else []
    a = [instance_id] if instance_id else []
    rows = db.query(" ".join([
        """SELECT file_id, root_id, rel_path, file_name, ext, file_size, text_chars,
                  parse_state, parse_reason, fingerprint, extract_fingerprint,
                  extracted_at, last_change, changed_at, origin, instance_id
             FROM doc_lib_file
            WHERE NOT missing AND parse_state = '已解析'"""]
        + scope
        + ["""AND (extracted_at IS NULL
                   OR extract_fingerprint IS DISTINCT FROM fingerprint)
              ORDER BY changed_at DESC NULLS LAST, rel_path LIMIT %s"""]),
        tuple(a + [max(1, min(2000, int(limit)))]))
    for r in rows:
        r["why"] = "从未抽取" if not r["extracted_at"] else "抽取后文件又被改过"
    blocked = db.query(" ".join([
        """SELECT parse_state, count(*) AS n FROM doc_lib_file
            WHERE NOT missing AND parse_state <> '已解析'"""]
        + scope + ["GROUP BY parse_state ORDER BY n DESC"]), tuple(a))
    return {"items": rows, "total": len(rows), "blocked": blocked,
            "note": "blocked 里的文件取不到正文，抽取覆盖不到它们。"
                    "「需OCR」那档是 PDF 没有文字层，跑一次 OCR 就能转进来；"
                    "「未解析」那档要先转格式。不说清这件事，"
                    "「抽完了」会被当成「制度都抽过了」。"}


def mark_extracted(file_ids, when=None):
    """抽取完成后把当时的指纹钉下来。文件再被改，两个指纹自然不等。"""
    when = when or datetime.datetime.now().replace(microsecond=0)
    n = 0
    for fid in file_ids or []:
        n += db.execute("""UPDATE doc_lib_file
                              SET extracted_at=%s, extract_fingerprint=fingerprint
                            WHERE file_id=%s""", (when, fid))
    return n


# ── 查询 ────────────────────────────────────────────────────────────────────
def stats(instance_id=None):
    """库里有多少、什么格式、能不能识别。instance_id 传了就只统计这个实例。

    **这些数字按实例过滤了**，而抽取/本体/发布那三栏的数字没有（迁移 0034 的
    「过滤边界」）。接口层把这件事写在 scope_note 里——让人以为全站都按实例
    过滤过，等于埋了一个将来说不清的坑。
    """
    w = "AND f.instance_id = %s" if instance_id else ""
    a = (instance_id,) if instance_id else ()

    def q(sql, extra=()):
        """把实例条件拼进 WHERE。f 是 doc_lib_file 的固定别名。"""
        return db.query(sql.replace("{SCOPE}", w), a + extra)

    row = db.query_one(
        ("SELECT count(*) AS n, COALESCE(sum(f.file_size),0) AS bytes "
         "FROM doc_lib_file f WHERE NOT f.missing " + w), a) or {"n": 0, "bytes": 0}
    by_ext = q("SELECT f.ext, count(*) AS n FROM doc_lib_file f "
               "WHERE NOT f.missing {SCOPE} GROUP BY f.ext ORDER BY n DESC, f.ext")
    by_state = q("SELECT f.parse_state AS state, count(*) AS n FROM doc_lib_file f "
                 "WHERE NOT f.missing {SCOPE} GROUP BY f.parse_state ORDER BY n DESC")
    by_origin = q("SELECT f.origin, count(*) AS n FROM doc_lib_file f "
                  "WHERE NOT f.missing {SCOPE} GROUP BY f.origin ORDER BY n DESC")
    # 「按能否识别」才是界面该拿来筛的那组数，by_state 留着给排查用。
    # 两者的区别就是「磁盘事实」和「我们处理到哪一步」的区别，不能只留一个。
    counts = {}
    for r in q("""SELECT f.parse_state, f.parse_reason, f.ocr_state, f.ocr_pages,
                         f.ocr_table_pages, f.ocr_mean_conf, f.ocr_note,
                         f.ocr_fingerprint, f.fingerprint
                    FROM doc_lib_file f WHERE NOT f.missing {SCOPE}"""):
        lab = recognizability(r)["label"]
        counts[lab] = counts.get(lab, 0) + 1
    by_recognize = [{"label": k, "n": v}
                    for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    by_kind = q("SELECT COALESCE(f.doc_kind,'其他') AS kind, count(*) AS n "
                "FROM doc_lib_file f WHERE NOT f.missing {SCOPE} GROUP BY 1 ORDER BY n DESC")
    one = lambda sql: db.scalar(sql.replace("{SCOPE}", w), a) or 0   # noqa: E731
    return {
        "files": row["n"], "bytes": int(row["bytes"] or 0),
        "by_ext": by_ext, "by_state": by_state, "by_recognize": by_recognize,
        "by_kind": by_kind, "by_origin": by_origin,
        "missing": one("SELECT count(*) FROM doc_lib_file f WHERE f.missing {SCOPE}"),
        "roots": db.scalar(
            "SELECT count(*) FROM doc_lib_root r WHERE r.enabled "
            + ("AND r.instance_id = %s" if instance_id else ""), a) or 0,
        "readable": one("SELECT count(*) FROM doc_lib_file f "
                        "WHERE NOT f.missing AND f.parse_state='已解析' {SCOPE}"),
        "instance_id": instance_id,
        "note": "「需OCR」不是「不能识别」：PDF 没有文字层而已，本机装了 tesseract，"
                "规则抽取阶段可按需 OCR。真正取不到的是「未解析」那一档。",
    }


def recognizability(r):
    """这份文件**到底能不能识别、该走哪条路**，一句话说清。

    为什么要专门算这个，而不是让人自己看 parse_state：
    parse_state 说的是「磁盘上这份文件本身有没有文字层」，是事实；
    但使用者真正要的答案是「我接下来该干什么」。
    「需OCR」三个字不回答这个问题——该跑本机 OCR？该上多模态？还是该去转格式？
    三条路成本差一个量级（本机免费 / 出网计费 / 人工返工），必须直说。

    最要紧的一档是「部分可识别」：OCR 跑完了、字也出来了，但其中几页是表格。
    **表格页的 OCR 文本不能当依据**——实测住宿费标准表数字全在
    （460/420/430/310/480/440），城市↔职级↔金额的对应关系却全乱了。
    把这种文件笼统标成「可识别」，等于把最危险的那几页藏进了一个绿标签里。
    """
    ps = r.get("parse_state")
    os_ = r.get("ocr_state")
    tp = r.get("ocr_table_pages") or 0
    pages = r.get("ocr_pages") or 0
    conf = r.get("ocr_mean_conf")
    stale = (os_ == "已OCR"
             and r.get("ocr_fingerprint") is not None
             and r.get("ocr_fingerprint") != r.get("fingerprint"))

    if ps == "已解析":
        return {"label": "可识别", "how": "原生文本", "cls": "pass",
                "note": r.get("parse_reason") or "文件自带文字层，直接取到正文"}

    if ps == "需OCR":
        if os_ == "已OCR" and stale:
            return {"label": "待重跑", "how": "本机 OCR", "cls": "pend",
                    "note": "文件在 OCR 之后又被改过，现有 OCR 文本是旧版的，请重跑 OCR"}
        if os_ == "已OCR":
            base = "已用本机 OCR 取到文字（tesseract，不出网、不计费，%d 页" % pages
            base += ("，平均置信 %.1f）" % float(conf)) if conf is not None else "）"
            if tp:
                return {"label": "部分可识别", "how": "OCR + 需多模态", "cls": "pend",
                        "note": base + "。**其中 %d 页是表格，这几页需多模态大模型读图"
                                       "或人眼读图**：OCR 丢的不是字是结构，"
                                       "数字可能都在但行列对应关系不可信。"
                                       "其余页的数字也需人工复核，不得直接进硬规则阈值。" % tp}
            return {"label": "可识别", "how": "本机 OCR", "cls": "pass",
                    "note": base + "。可信度比原生文本低一档：数字需人工复核，"
                                   "不得直接进硬规则阈值。"}
        if os_ == "OCR失败":
            return {"label": "不可识别", "how": "OCR 失败", "cls": "hit",
                    "note": r.get("ocr_note") or "OCR 执行失败，见日志"}
        return {"label": "待识别", "how": "本机 OCR 可解决", "cls": "pend",
                "note": (r.get("parse_reason") or "PDF 没有文字层（纯扫描件）。")
                        + " 点「跑 OCR」即可——本机 tesseract，不出网、不计费。"
                          "若其中有表格页，OCR 完会单独标出来，那几页要走多模态或人眼读图。"}

    if ps == "未解析":
        return {"label": "需转格式", "how": "转存后重扫", "cls": "na",
                "note": r.get("parse_reason") or "这个格式在轻解析阶段取不到正文"}
    if ps == "空文件":
        return {"label": "无内容", "how": "—", "cls": "na",
                "note": r.get("parse_reason") or "文件里没有文字"}
    if ps == "解析失败":
        return {"label": "不可识别", "how": "需人工查看", "cls": "hit",
                "note": r.get("parse_reason") or "解析出错"}
    return {"label": ps or "未知", "how": "—", "cls": "na",
            "note": r.get("parse_reason") or ""}


def list_files(q=None, ext=None, root_id=None, state=None, limit=500,
               recognize=None, kind=None, instance_id=None, origin=None):
    sql = ["""SELECT f.file_id, f.root_id, f.abs_path, f.rel_path, f.file_name, f.ext,
                     f.file_size, f.mtime, f.parse_state, f.parse_reason, f.text_chars,
                     f.first_seen_at, f.last_seen_at, f.missing, f.fingerprint,
                     f.doc_kind, f.doc_number, f.doc_title, f.issued_year,
                     f.ocr_state, f.ocr_pages, f.ocr_chars, f.ocr_table_pages,
                     f.ocr_mean_conf, f.ocr_fingerprint, f.ocr_note, f.ocr_at,
                     f.instance_id, f.origin, f.sha256, f.uploaded_by, f.uploaded_at,
                     r.kind AS root_kind, r.label AS root_label,
                     -- 同名另一版本：同一实例里文件名相同、在库的有几份。
                     -- 这个数**派生，不存列**——存一列「是否有同名版本」就要在
                     -- 上传、删除、重扫三处同时维护，必然对不上（纪律第 9 条同类）。
                     (SELECT count(*) FROM doc_lib_file g
                       WHERE g.instance_id = f.instance_id
                         AND g.file_name = f.file_name AND NOT g.missing) AS name_copies
                FROM doc_lib_file f
                JOIN doc_lib_root r ON r.root_id = f.root_id
               WHERE NOT f.missing"""]
    args = []
    if instance_id:
        sql.append("AND f.instance_id = %s"); args.append(instance_id)
    if q:
        sql.append("AND f.file_name ILIKE %s"); args.append("%%%s%%" % q)
    if ext:
        sql.append("AND f.ext = %s"); args.append(ext)
    if root_id:
        sql.append("AND f.root_id = %s"); args.append(root_id)
    if state:
        sql.append("AND f.parse_state = %s"); args.append(state)
    if kind:
        sql.append("AND f.doc_kind = %s"); args.append(kind)
    if origin:
        sql.append("AND f.origin = %s"); args.append(origin)
    sql.append("ORDER BY f.rel_path LIMIT %s")
    args.append(max(1, min(2000, int(limit))))
    rows = db.query(" ".join(sql), tuple(args))
    for r in rows:
        # 「能不能识别、下一步该干什么」跟着每一行走，不让人自己从状态码推
        r["recognize"] = recognizability(r)
        # 能不能删也跟着行走。前端自己按 origin 猜会猜错：引用根里的文件
        # 即使是「上传」来的（不可能，但万一有人手改过库）也不许删。
        # 判断的唯一依据是**根的类型**。
        r["deletable"] = r["root_kind"] == "托管"
    if recognize:
        # 按**识别结果**筛，而不是按 parse_state 筛。
        # parse_state 是磁盘事实（这份文件自己有没有文字层），OCR 跑完也不会变——
        # 让使用者对着一个写着「需OCR」的下拉去找已经 OCR 过的文件，是界面在骗人。
        rows = [r for r in rows if r["recognize"]["label"] == recognize]
    return rows


def get_file(file_id, chars=4000):
    row = db.query_one("SELECT * FROM doc_lib_file WHERE file_id=%s", (file_id,))
    if not row:
        return None
    body = row.pop("body") or ""
    row["preview"] = body[:chars]
    row["truncated"] = len(body) > chars
    row["recognize"] = recognizability(row)
    if row.get("ocr_state") == "已OCR":
        from . import ocr as _ocr
        row["ocr_pages_detail"] = _ocr.pages_of(file_id, limit=60)
    return row


# ═════════════════════════════════════════════════════════════════════════════
# 客户端上传 → 托管目录
# ─────────────────────────────────────────────────────────────────────────────
# 为什么上传完要**复用 scan() 做登记与解析**，而不在这里自己解析一遍：
#   六态判定、原因文案、指纹、变化台账、待抽取的进出规则，全都在 scan() 里。
#   在上传这条路上再写一份，两份逻辑第二天就会分叉——而分叉的表现是
#   「同一份文件，传进来显示已解析，重扫一次变成需OCR」，谁也说不清哪个对。
#   代价是上传完要多跑一次扫描（按指纹跳过没动的文件，几百份也是秒级）。
#
# 为什么暂存目录在**托管区之外**：
#   写一半的 .part 文件如果放在托管根里面，恰好这时有人点「扫描」，
#   那个残缺文件虽然因为扩展名不对进不了库，但下一个人很容易把暂存目录
#   挪进托管根而不知道这件事。放在同级的 var/doclib_staging/，
#   物理上不可能被扫到。
# ═════════════════════════════════════════════════════════════════════════════
STAGING_DIR = config.DOCLIB_DIR + "_staging"

# 流式读的块大小。不整份读进内存：200MB 的 docx 乘以并发就是几个 G。
CHUNK = 1 << 20


def _rm(path):
    """删一个文件，删不掉也不抛。清理失败不该把主流程判红。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _stage(stream, dest_dir):
    """把一份上传流写进暂存文件，边写边算 sha256 与大小。→ (path, sha, size)

    超过上限立刻停手并删掉半截文件，**不是先写完再判断**：
    先写完等于谁都能用一个 2GB 的文件把服务器磁盘填满。
    """
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, "up-" + uuid.uuid4().hex + ".part")
    h = hashlib.sha256()
    size = 0
    try:
        with open(path, "wb") as out:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise PathError(
                        "超过单份上限 %s（读到 %s 就停了，没有把它收完——"
                        "先收完再判断等于谁都能用一个大文件填满服务器磁盘）"
                        % (_fmt_size(config.MAX_UPLOAD_BYTES), _fmt_size(size)))
                h.update(chunk)
                out.write(chunk)
    except BaseException:
        _rm(path)
        raise
    return path, h.hexdigest(), size


def _free_name(dir_path, file_name):
    """同名不同内容时另取一个名字。→ (最终绝对路径, 落盘名)

    「差旅管理办法.pdf」已存在 → 「差旅管理办法(2).pdf」→「(3)」…
    **不覆盖**：覆盖会让已经抽出的规则指向一份不存在的原文，
    而页面上那条规则看着仍然有出处——证据链断在这里基本查不出来。
    """
    stem, ext = os.path.splitext(file_name)
    cand = file_name
    i = 1
    while os.path.exists(os.path.join(dir_path, cand)):
        i += 1
        cand = "%s(%d)%s" % (stem, i, ext)
        if i > 999:
            raise PathError("同名文件已经有 999 个版本了：%s" % file_name)
    return os.path.join(dir_path, cand), cand


def save_uploads(items, instance_id=None, by=None, subdir=None):
    """把客户端上传的文件存进当前（或指定）实例的托管目录。

    items: [(客户端给的文件名或相对路径, 可 read 的字节流), ...]
    subdir: 可选，统一放进托管根下的某个子目录（如「2025年度」）

    **逐份独立判定**：一份被拒不影响其余的（和 add_roots 同一个口径）。
    每一份都在 doc_lib_upload 里留一行，**被拒的也留**——客户传了 20 份
    库里只多 17 份时，差的 3 份必须查得出是哪一种拒绝。
    """
    inst = resolve_instance(instance_id)
    root = ensure_managed_root(inst["instance_id"])
    base = root["abs_path"]
    who = (by or "ui").strip()[:64] or "ui"
    batch = "dlu-" + uuid.uuid4().hex[:12]
    items = list(items or [])
    if not items:
        raise ScopeError("没有收到任何文件")
    if len(items) > config.MAX_UPLOAD_FILES:
        raise ScopeError("一次最多传 %d 份，这次给了 %d 份。请分批——"
                         "一次几百份时，中间任何一份出错都很难说清是哪一份"
                         % (config.MAX_UPLOAD_FILES, len(items)))

    prefix = ""
    if subdir:
        # 子目录也是外来输入，同一道闸。借 safe_rel 校验要配一个合法扩展名，
        # 校验完只取目录部分。
        prefix = os.path.dirname(safe_rel(str(subdir).strip("/\\ ") + "/_.txt")[0])
        prefix = (prefix + "/") if prefix else ""

    results = []
    seen_sha = {}            # 同一批里的重复：磁盘上还没有，查库查不到
    staged = []              # 本批已 rename 进托管目录的最终路径

    def record(**kw):
        for k in ("rel_path", "stored_name", "file_size", "sha256",
                  "reason", "file_id", "root_id"):
            kw.setdefault(k, None)
        db.execute("""INSERT INTO doc_lib_upload(
                          batch_id, instance_id, root_id, file_name, rel_path,
                          stored_name, file_size, sha256, result, reason,
                          file_id, uploaded_by)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (batch, inst["instance_id"], kw["root_id"], kw["file_name"],
                    kw["rel_path"], kw["stored_name"], kw["file_size"], kw["sha256"],
                    kw["result"], kw["reason"], kw["file_id"], who))
        results.append(kw)
        return kw

    for raw_name, stream in items:
        given = str(raw_name or "")
        shown = os.path.basename(given.replace("\\", "/")) or given or "（无名）"
        # ① 先校验名字再读字节：名字不合法不该花一次磁盘 IO
        try:
            rel, warns = safe_rel(prefix + given)
        except PathError as e:
            record(file_name=shown, rel_path=given or None,
                   result=("扩展名不支持" if "扩展名" in str(e) else "文件名不合法"),
                   reason=str(e))
            continue

        sub = os.path.dirname(rel)
        target_dir = os.path.join(base, *sub.split("/")) if sub else base
        # Windows 的 MAX_PATH 在这里是真会踩到的：项目路径本身就很长。
        # 不先算一遍，表现是 open() 报一个看不懂的 errno 22。
        probe = os.path.join(target_dir, os.path.basename(rel))
        if len(probe) > MAX_FULL_PATH:
            record(file_name=shown, rel_path=rel, result="文件名不合法",
                   reason="落盘后的完整路径有 %d 个字符，超过 %d 的上限"
                          "（Windows 的 MAX_PATH 是 260，托管根本身已占掉 %d）。"
                          "请把文件名改短，或者少放一层子目录"
                          % (len(probe), MAX_FULL_PATH, len(base)))
            continue

        # ② 流式落到暂存区，顺手算 sha256
        try:
            part, sha, size = _stage(stream, STAGING_DIR)
        except PathError as e:
            record(file_name=shown, rel_path=rel, result="超过大小上限", reason=str(e))
            continue
        except OSError as e:
            record(file_name=shown, rel_path=rel, result="写盘失败",
                   reason="写暂存文件出错：%s" % e)
            continue

        if not size:
            # 0 字节：收进来只会变成一行「空文件」。当场说清楚，
            # 不要让它走到清单里再让人去问「这份怎么是空的」。
            _rm(part)
            record(file_name=shown, rel_path=rel, file_size=0, result="文件名不合法",
                   reason="这份文件是 0 字节。常见原因是从网盘或邮件里拖出来时"
                          "没有真正下载完，请确认本地能打开再传")
            continue

        # ③ 去重：同实例、同内容、仍在库 → 不落第二份
        dup = db.query_one("""SELECT file_id, rel_path FROM doc_lib_file
                               WHERE instance_id=%s AND sha256=%s AND NOT missing""",
                           (inst["instance_id"], sha))
        if dup:
            _rm(part)
            record(file_name=shown, rel_path=rel, sha256=sha, file_size=size,
                   result="同内容已存在", file_id=dup["file_id"],
                   root_id=root["root_id"],
                   reason="库里已有内容完全相同的「%s」，没有再落一份。"
                          "判的是内容不是文件名：改个名字再传也还是同一份文件，"
                          "落第二份只会让「这条规则依据哪一份」多一个无意义的分叉"
                          % dup["rel_path"])
            continue
        if sha in seen_sha:
            _rm(part)
            record(file_name=shown, rel_path=rel, sha256=sha, file_size=size,
                   result="同内容已存在", root_id=root["root_id"],
                   reason="这一批里前面已经有一份内容相同的「%s」" % seen_sha[sha])
            continue

        # ④ 定最终名 → ⑤ 原子 rename 进托管目录
        try:
            os.makedirs(target_dir, exist_ok=True)
            final, stored = _free_name(target_dir, os.path.basename(rel))
            os.replace(part, final)
        except (OSError, PathError) as e:
            _rm(part)
            record(file_name=shown, rel_path=rel, sha256=sha, file_size=size,
                   result="写盘失败", root_id=root["root_id"],
                   reason="移入托管目录出错：%s" % e)
            continue

        seen_sha[sha] = rel
        staged.append(final)
        renamed = stored != os.path.basename(rel)
        note = []
        if renamed:
            note.append("库里已有同名但**内容不同**的文件，这一份按新版本存为「%s」；"
                        "旧的没有被覆盖——覆盖会让已经抽出的规则指向一份不存在的原文"
                        % stored)
        note.extend(warns)
        record(file_name=shown, rel_path=rel, stored_name=stored, sha256=sha,
               file_size=size, root_id=root["root_id"],
               result="同名另存" if renamed else "已入库",
               reason="；".join(note) or None)

    # ⑥ 落完之后统一跑一次扫描：登记、轻解析、指纹、变化台账全在那里
    scan_res = scan(root_ids=[root["root_id"]], parse=True) if staged else None

    # ⑦ 把内容指纹与上传人回写到刚登记的行上，并补台账里的 file_id。
    #    **顺序只能是「先扫描后回写」**：scan() 跑之前那些行还不存在。
    for r in results:
        if r["result"] not in ("已入库", "同名另存"):
            continue
        sub = os.path.dirname(r["rel_path"] or "")
        full = os.path.join(base, *(sub.split("/") if sub else []), r["stored_name"])
        fid = db.scalar("SELECT file_id FROM doc_lib_file WHERE abs_path=%s", (full,))
        if fid:
            db.execute("""UPDATE doc_lib_file SET sha256=%s, uploaded_by=%s,
                                 uploaded_at=now(), origin='上传'
                           WHERE file_id=%s""", (r["sha256"], who, fid))
            db.execute("""UPDATE doc_lib_upload SET file_id=%s
                           WHERE batch_id=%s AND stored_name=%s""",
                       (fid, batch, r["stored_name"]))
            r["file_id"] = fid
        else:
            # 落了盘却没登记上 = 「上传接受的集合」和「扫描登记的集合」不一致。
            # **必须显式报出来**，这正是 safe_rel 要挡住「扫描会跳过的名字」的原因；
            # 真出现了说明两边的规则漂移了，静默过去就变成「传上去了但清单里没有」。
            r["reason"] = ((r.get("reason") or "")
                           + "；**文件已落盘但扫描没有登记它**（%s）。"
                             "这说明上传的校验规则与扫描的跳过规则不一致，"
                             "请把这条报给开发——不是操作问题" % full).lstrip("；")

    ok = [r for r in results if r["result"] in ("已入库", "同名另存")]
    return {
        "batch_id": batch,
        "instance_id": inst["instance_id"],
        "instance_label": inst["instance_label"],
        "domain_label": inst["domain_label"],
        "root_id": root["root_id"],
        "root_label": root["label"],
        "dir": base,
        "items": results,
        "ok_count": len(ok),
        "rejected": [r for r in results if r["result"] not in ("已入库", "同名另存")],
        "scan": scan_res,
        "note": "上传完自动跑了一次扫描：六态判定、指纹、变化台账与「待抽取」"
                "都由扫描那条路产出，这里不另写一份解析逻辑。"
                "被拒的文件也在 /api/doclib/uploads 台账里留了行。",
    }


# ── 删除托管文件 ────────────────────────────────────────────────────────────
def _refs_of(file_id):
    """这份文件已经产出了什么。删除闸靠它。

    为什么 OCR 文本不算拦截项：OCR 是本机跑的、可以重跑的派生物。
    条款、规则明细、**已签字**的读图不一样——条款和明细是规则的依据，
    已签字的读图是人花时间核过的结论，删掉原文就再也复核不了。
    """
    return {
        "条款": db.scalar("SELECT count(*) FROM doclib_clause WHERE file_id=%s",
                          (file_id,)) or 0,
        "规则明细": db.scalar("SELECT count(*) FROM doclib_rule_detail WHERE file_id=%s",
                              (file_id,)) or 0,
        "已签字读图": db.scalar("SELECT count(*) FROM doc_page_vision "
                                "WHERE file_id=%s AND verified_by IS NOT NULL",
                                (file_id,)) or 0,
    }


def delete_managed_file(file_id, by=None):
    """删掉托管目录里的一份文件。

    三条口径，缺一条就会出事：

    1. **只能删托管根里的。** 引用根是客户的资料目录，0015 的只读纪律管着它。
    2. **已产出条款/规则明细/已签字读图的拒删。** 那条规则的依据会凭空消失，
       而页面上它看着仍然有出处——证据链断在这里基本查不出来。
    3. **doc_lib_file 的行不删，只标 missing。** 删行会级联清掉 OCR、读图、
       条款、规则明细（0017/0018/0027/0029 四张表都是 ON DELETE CASCADE），
       而「这份资料曾经在库里」必须留着。标记走扫描那条现成的路：
       删完磁盘文件再扫一次这个根，scan() 自己会判成「消失」并落台账。
    """
    row = db.query_one("""SELECT f.file_id, f.abs_path, f.rel_path, f.file_name,
                                 f.root_id, f.instance_id, f.origin,
                                 r.kind AS root_kind, r.label AS root_label
                            FROM doc_lib_file f
                            JOIN doc_lib_root r ON r.root_id = f.root_id
                           WHERE f.file_id = %s""", (file_id,))
    if not row:
        return None
    if row["root_kind"] != "托管":
        raise ScopeError(
            "「%s」在引用目录「%s」里，**不能删**。那是客户的资料目录，"
            "本系统对它只读（迁移 0015 的纪律）。要让它不再出现在库里，"
            "请把整个目录「移出文件库」——那只删登记，磁盘上一个字节都不动。"
            % (row["file_name"], row["root_label"]))

    refs = _refs_of(file_id)
    blocking = {k: v for k, v in refs.items() if v}
    if blocking:
        raise ConflictError(
            "「%s」已经产出了 %s，不能删。删掉原文之后，那些条款和规则明细"
            "会指向一份不存在的文件，而页面上它们看着仍然有出处——"
            "这种断链基本查不出来。请先在「规则抽取」那一栏把这些产出处理掉"
            "（作废，或改挂到别的出处），再回来删文件。"
            % (row["file_name"],
               "、".join("%d 条%s" % (v, k) for k, v in blocking.items())))

    existed = os.path.exists(row["abs_path"])
    if existed:
        try:
            os.remove(row["abs_path"])
        except OSError as e:
            raise PathError("删不掉这个文件：%s（%s）。可能正被别的程序打开着"
                            % (row["abs_path"], e))
    # 重扫这个根，让 scan() 把它判成「消失」并落变化台账。
    # 不在这里手写 doc_lib_change：那张表的 scan_id 是 NOT NULL 外键，
    # 手写就要先造一个假批次，而假批次会污染「历次扫描」那张表。
    scan_res = scan(root_ids=[row["root_id"]], parse=True)
    db.execute("""UPDATE doc_lib_upload
                     SET reason = COALESCE(reason || '；', '') || %s
                   WHERE file_id = %s""",
               ("已于 %s 被 %s 删除" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                        (by or "ui")), file_id))
    return {
        "file_id": file_id, "file_name": row["file_name"],
        "removed_from_disk": existed, "refs": refs, "scan": scan_res,
        "note": ("磁盘上的文件已删，**登记行按「消失」保留**——"
                 "删掉登记行会连带级联清空 OCR、读图、条款、规则明细，"
                 "而「这份资料曾经在库里」是溯源的一部分，只能作废不能蒸发。")
                if existed else
                ("磁盘上本来就没有这个文件（可能之前已被人在文件系统里删掉了），"
                 "登记行已按「消失」标记。"),
    }


# ── 台账与占用 ──────────────────────────────────────────────────────────────
def list_uploads(limit=200, instance_id=None, batch_id=None, only_rejected=False):
    """上传台账。**被拒的也在里面**，这是它存在的主要理由。"""
    sql = ["""SELECT u.*, f.parse_state, f.text_chars, f.missing
                FROM doc_lib_upload u
                LEFT JOIN doc_lib_file f ON f.file_id = u.file_id
               WHERE TRUE"""]
    args = []
    if instance_id:
        sql.append("AND u.instance_id = %s"); args.append(instance_id)
    if batch_id:
        sql.append("AND u.batch_id = %s"); args.append(batch_id)
    if only_rejected:
        sql.append("AND u.result NOT IN ('已入库', '同名另存')")
    sql.append("ORDER BY u.uploaded_at DESC, u.id DESC LIMIT %s")
    args.append(max(1, min(2000, int(limit))))
    rows = db.query(" ".join(sql), tuple(args))
    by_result = db.query(" ".join(
        ["SELECT result, count(*) AS n FROM doc_lib_upload WHERE TRUE"]
        + (["AND instance_id = %s"] if instance_id else [])
        + ["GROUP BY result ORDER BY n DESC"]),
        (instance_id,) if instance_id else ())
    return {"items": rows, "total": len(rows), "by_result": by_result,
            "note": "一份文件一行，**被拒的也留一行**。客户传了 20 份而库里只多 17 份时，"
                    "差的 3 份在这里查得出是「同内容重复」「扩展名不支持」还是「写盘失败」"
                    "——这三件事的下一步动作完全不同。"}


def storage(instance_id=None):
    """托管区占用。上传是会**改变服务器磁盘状态**的动作，占用必须看得见。"""
    rows = db.query(" ".join(
        ["""SELECT r.root_id, r.abs_path, r.label, r.instance_id,
                   i.label AS instance_label, d.label AS domain_label,
                   count(f.file_id) FILTER (WHERE NOT f.missing) AS files,
                   COALESCE(sum(f.file_size) FILTER (WHERE NOT f.missing), 0) AS bytes
              FROM doc_lib_root r
              LEFT JOIN doc_lib_file f ON f.root_id = r.root_id
              LEFT JOIN domain_instance i ON i.instance_id = r.instance_id
              LEFT JOIN domain d ON d.domain_id = i.domain_id
             WHERE r.kind = '托管'"""]
        + (["AND r.instance_id = %s"] if instance_id else [])
        + ["GROUP BY 1,2,3,4,5,6 ORDER BY bytes DESC"]),
        (instance_id,) if instance_id else ())
    for r in rows:
        r["exists"] = os.path.isdir(r["abs_path"])
        r["bytes"] = int(r["bytes"] or 0)
        r["size_text"] = _fmt_size(r["bytes"])
    # 暂存区本该是空的：每份上传要么 rename 走、要么当场删掉。
    # 非空说明有一次上传崩在中途，必须报出来而不是悄悄留着占磁盘。
    stale = []
    if os.path.isdir(STAGING_DIR):
        for fn in sorted(os.listdir(STAGING_DIR))[:50]:
            try:
                stale.append({"file_name": fn,
                              "bytes": os.path.getsize(os.path.join(STAGING_DIR, fn))})
            except OSError:
                continue
    return {
        "dirs": rows,
        "total_files": sum(r["files"] or 0 for r in rows),
        "total_bytes": sum(r["bytes"] for r in rows),
        "root_dir": config.DOCLIB_DIR,
        "staging_dir": STAGING_DIR,
        "staging_stale": stale,
        "limits": {"max_upload_bytes": config.MAX_UPLOAD_BYTES,
                   "max_upload_bytes_text": _fmt_size(config.MAX_UPLOAD_BYTES),
                   "max_upload_files": config.MAX_UPLOAD_FILES,
                   "allowed_ext": sorted(DOC_EXT)},
        "note": ("暂存区有 %d 个残留文件——每份上传要么移入托管目录、要么当场删掉，"
                 "残留说明有一次上传崩在中途。这些文件没有任何登记指向它们，"
                 "可以直接删。" % len(stale)) if stale else
                ("暂存区干净。托管区是本系统唯一会写入的文件库目录；"
                 "引用目录（客户的资料）一个字节都不写。"),
    }
