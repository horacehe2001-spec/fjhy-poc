-- ============================================================================
-- 0024 · 本体投影补 alias：装载脚本一直在丢别名，下游因此报假红
-- ----------------------------------------------------------------------------
-- 本体 YAML 里实体、概念、流程节点都可以带 alias，而且**别名是口径信息，
-- 不是装饰**：客户材料里同一个东西在不同表里叫不同名字。实例（原文照抄）：
--   concepts: {id: C0107, name: 核算主体, alias: [报账人公司],
--              note: 明细表列名为「报账人公司」，审批意见页签名为「核算主体」，同一概念}
--   nodes:    {id: N01, name: 提单, alias: [开始]}
--
-- 而 app/ontology/loader.py 从来没把 alias 写进库。后果是实测出来的：
--   1. 「业务本体构建」比对单据列头时，把「报账人公司」报成「本体里没有这个概念」。
--      照着这条去加概念，本体里就会多出一个与 C0107 重复的概念——
--      **重复概念比缺概念更糟**：两条概念各被一批规则引用，
--      同一个字段从此有两套口径，而两边都「有出处」。
--   2. 流程漂移核对把审批记录里的「开始」（103 次）报成「本体里找不到对应节点」，
--      而它就是 N01 提单的别名。**这是假红**，而假红看多了真红也不会有人看。
--
-- 「库是 YAML 的投影」这件事，只有投影**完整**才成立。投影缺一列，
-- 下游要么读不到（于是报假红），要么绕过库直接读 YAML——
-- 后者更糟：绕过去之后，库里有没有这列就没人管了，漂移彻底失去检测点。
--
-- 不补 observed：YAML 里每个节点有 observed（N01=67、N02=35、N05=97），
-- 而实测是 103/67/125。那组数是「第一版解析器漏了 24%」那一版留下的，
-- **载入它等于把一组已知是错的数字固化进库**。页面上的实测次数一律现算。
-- ============================================================================

ALTER TABLE ontology_entity  ADD COLUMN IF NOT EXISTS alias TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE ontology_concept ADD COLUMN IF NOT EXISTS alias TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE process_node     ADD COLUMN IF NOT EXISTS alias TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS ix_onto_concept_alias ON ontology_concept USING GIN (alias);
CREATE INDEX IF NOT EXISTS ix_process_node_alias ON process_node USING GIN (alias);

COMMENT ON COLUMN ontology_concept.alias IS
  '同一概念在客户材料里的其它叫法。**比对列头、比对条款时必须连别名一起比**，'
  '否则会把「报账人公司」这类别名报成缺概念，而照着去新增就产生了重复概念。'
  '实测：121 个概念里有 6 个带别名。';

COMMENT ON COLUMN process_node.alias IS
  '节点在审批记录里的其它叫法（N01 提单 的别名是「开始」，实测出现 103 次）。'
  '漂移核对必须连别名一起比：不比别名，103 次出现会被报成「本体里找不到这个节点」。';

COMMENT ON COLUMN ontology_entity.alias IS
  '实体别名（E01 报账单 的别名含 差旅费报账单 / 多人差旅费报账单 / 单据）。';
