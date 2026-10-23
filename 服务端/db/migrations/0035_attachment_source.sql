-- ============================================================================
-- 0035 · 单据附件：来源、序号、内容指纹；并把「申报张数」与「实际附件数」分开
-- ----------------------------------------------------------------------------
-- 背景（2026-10-21，用户指出 + 实测核实）：
--
-- 0004 的表注释写着「这张表的源数据在 40 张样本里 100% 为空（G-29，阻塞）」。
-- **事实没记错，归因错了。** B 列「附件」的**单元格文本**确实是空的，
-- 但附件一直在交付物里——它们是**内嵌在 xlsx 里的 PNG 图片**：
--
--   材料/单据明细表与审批意见/差旅报账单明细表.xlsx     「1报销附件」表    143 张
--   材料/单据明细表与审批意见/多人差旅报账单明细表.xlsx 「2多人差旅-报销附件」 76 张
--   合计 219 张 / 37.3MB，锚在 B 列往右，**覆盖全部 40 张单**（每单 1~21 张）
--   单据号与库里 40 张真实单 40:40 完全对得上，无单边
--
-- 看不见的原因是：xlsx 轻解析只读 XML 文本（sharedStrings + 单元格），
-- 而图片在 xl/media/ 下、靠 xl/drawings/drawing1.xml 的锚点挂到行列上。
-- **「读不到」和「没有」是两件事**，这正是本项目反复强调的那条纪律的又一例：
-- 把「我们的解析器看不见」记成「客户没给数据」，就会去跟客户要一份已经给过的东西。
--
-- 实测抽样确认了内容（不是靠文件名推断）：
--   image61 = 完整的**出差申请单**（出差人/出发地/目的地/返回地/最高职位/人数/
--             起止时间/天数/事由/经办部门负责人，外加差旅用餐情况表与差旅用车情况表）
--   image1  = 铁路电子客票电子发票（真实发票，含个人信息）
--
-- 【本次决策（用户 2026-10-21）】
--   ① **先抽图入库，暂不判类型**：attach_type 留空。
--      TR-D6-01（必要附件完整性）要的是**类型**（A01 出差审批单 / A03 交通费凭证
--      / A04 住宿费发票…），决策表 11 种类型里 5 种标着 ocr_needed:true——
--      从图片判类型必须读图。**有 219 张图 ≠ 能跑 TR-D6-01**，这一步只解决
--      「这张单有没有附件、几张、能不能点开看」。
--   ② 落盘到 **var/attachments/{领域id}/{实例id}/{单据号}/**，
--      **不混进 var/doclib 的托管区**（0034）：那是制度文件库，是规则的上游；
--      单据附件是业务数据。混在一起，「规则从哪堆资料里抽的」就再也说不清。
--   ③ 「申报附件张数」与「实际附件数」**当两个指标并存，不做成规则**。
--
-- 【为什么 attach_pages 不能当附件数用】
--   attach_pages 是 C0117「附件张数」，来自**审批意见 PDF**
--   （app/perception/approval_pdf.py）。实测与 xlsx 实际图片数 **34/40 不一致**：
--       申报 > 实际 32 张（+1 的 15 张、+2 的 11 张，最多 +7）
--       相等      1 张
--       申报 < 实际 2 张（−3、−10）
--   **这不等于「申报不实」**：「张数」可能按纸质页数算，一张图可能是两页合并，
--   申报数也可能含未扫描的原件。在没搞清口径之前把它做成规则，
--   就是一条 32/40 命中的误报机——而误报会把正常交付物判成问题件。
--   所以两个数并存、页面并列显示、各自标明口径，**谁也不覆盖谁**。
-- ============================================================================

ALTER TABLE attachment
    ADD COLUMN IF NOT EXISTS source_kind TEXT,
    ADD COLUMN IF NOT EXISTS source_ref  TEXT,
    ADD COLUMN IF NOT EXISTS seq         INTEGER,
    ADD COLUMN IF NOT EXISTS sha256      TEXT,
    ADD COLUMN IF NOT EXISTS byte_size   BIGINT,
    ADD COLUMN IF NOT EXISTS content_type TEXT;

-- 回填：本迁移之前这张表里只有演示数据（实测 54 行，全部挂在 is_demo 的 29 张单上，
-- 40 张真实单一行都没有）。如实标成「演示合成」，**不要混进实际抽取出来的那些**——
-- 演示数据和客户真实附件在页面上必须一眼可辨（全局纪律第 12 条）。
UPDATE attachment SET source_kind = '演示合成' WHERE source_kind IS NULL;

ALTER TABLE attachment
    ALTER COLUMN source_kind SET NOT NULL,
    ALTER COLUMN source_kind SET DEFAULT 'xlsx内嵌';

DO $$ BEGIN
    ALTER TABLE attachment ADD CONSTRAINT ck_att_source_kind
        CHECK (source_kind IN ('xlsx内嵌', '演示合成', '上传'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 同一张单里同一份内容只许有一条在库记录。
-- 抽取器可以反复重跑而不会把 219 张变成 438 张。
CREATE UNIQUE INDEX IF NOT EXISTS ux_att_bill_sha
    ON attachment (bill_no, sha256) WHERE sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_att_bill_seq ON attachment (bill_no, seq);

COMMENT ON COLUMN attachment.source_kind IS
  'xlsx内嵌 | 演示合成 | 上传。**演示数据必须一眼可辨**：'
  '实测本表在本迁移之前的 54 行全部是演示单（is_demo）的合成数据，'
  '40 张真实单一行都没有。把客户真实附件和合成数据混在一起看不出来，'
  '演示现场会把「这是编的」说成「这是客户的」。';

COMMENT ON COLUMN attachment.source_ref IS
  '这一份附件是从哪儿来的，精确到可复查：'
  '形如「差旅报账单明细表.xlsx · 1报销附件 · 第9行 · xl/media/image30.png」。'
  '**只写文件名不够**——同一个单据号在两份 xlsx 里都可能出现，'
  '而「哪一行、哪张图」是事后回查原件的唯一入口。';

COMMENT ON COLUMN attachment.seq IS
  '该单据内的顺序，按 xlsx 里图片锚点的列号排（B 列往右）。'
  '**不是页码**：一张图可能是多页合并的（实测出差申请单那张就是整表一张图）。'
  'pages 列才是页数，而内嵌 PNG 没有页的概念，所以抽取时 pages 留空、不猜。';

COMMENT ON COLUMN attachment.attach_type IS
  'C1301 附件类型（enum EN17）。**抽图入库这一步刻意留空**：'
  '从一张 PNG 判它是出差审批单还是住宿费发票，必须读图'
  '（必要附件决策表里 11 种类型有 5 种标着 ocr_needed:true）。'
  '留空表示「还没判」，**不等于「没有类型」**——'
  'TR-D6-01 看到类型为空时必须报「判不出」，不许当成「缺件」去拦单。';

COMMENT ON COLUMN attachment.sha256 IS
  '内容哈希。两个用处：抽取器重跑时去重（同一张单同一份内容只留一条）；'
  '以及事后证明「页面上显示的这张图，就是 xlsx 里的那一张」。';

-- ─────────────────────────────────────────────────────────────────────────────
-- 实际附件数：与「申报张数」并存的第二个指标
-- ─────────────────────────────────────────────────────────────────────────────
ALTER TABLE expense_bill
    ADD COLUMN IF NOT EXISTS attach_count INTEGER;

COMMENT ON COLUMN expense_bill.attach_count IS
  '**实际**附件数：从 xlsx 内嵌图片抽出来、真的落了盘的那几张。'
  '与 attach_pages（C0117 申报附件张数，来自审批意见 PDF）**是两个指标，不是一个**，'
  '实测 34/40 张单两者不等（申报 > 实际 32 张，相等 1 张，申报 < 实际 2 张）。'
  '**不要把不等当成「申报不实」去做规则**：「张数」可能按纸质页数算、'
  '一张图可能是两页合并、申报数也可能含未扫描的原件。'
  '在口径搞清楚之前，那会是一条 32/40 命中的误报机。两个数并列显示，各自标明口径。';

COMMENT ON TABLE attachment IS
  '单据附件。**0004 的旧注释说「源数据 100% 为空（G-29，阻塞）」——事实对，归因错。**'
  '附件一直在交付物里，是**内嵌在 xlsx 里的 PNG**（219 张，覆盖全部 40 张单），'
  '只是 xlsx 轻解析只读 XML 文本，看不见 xl/media/ 下的图片。'
  '「我们的解析器读不到」和「客户没给」是两件事，混了就会去要一份已经给过的东西。'
  '2026-10-21 起由 app/perception/attachment_extract.py 抽取入库。'
  'TR-D6-01 仍未解除阻塞：它要的是**附件类型**，而类型要读图才知道，见 attach_type 的注释。';
