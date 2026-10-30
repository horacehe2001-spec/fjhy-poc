-- ============================================================================
-- 0042 · 制度文件库：多模态读图作为**独立取文路径**
-- ----------------------------------------------------------------------------
-- 在这次改动之前，抽取只认两个文本来源（doclib_extract.py）：
--     (parse_state='已解析' AND body IS NOT NULL)        原生文字层
--  OR (ocr_state='已OCR' AND ocr_fingerprint = fingerprint)  本机 tesseract
-- 而 VL 读图的结果虽然逐页落在 doc_page_vision 里，**抽取一行都不读它**。
-- 读图当时是接在 OCR 之后的补充：vision.pages_to_read() 从 doc_lib_ocr 里
-- 挑「OCR 认出表格」或「OCR 一个字都没认出」的页，连页面图都是 OCR 渲的。
-- 于是出现了一个说不通的局面：**本机 OCR 能让一份扫描件进抽取，
-- 效果更好的多模态模型反而不能。**
--
-- 这次把读图升为与 OCR 平级的第三条取文路径，七列与 ocr_* 一一对应，
-- 让 doclib_extract 的取文闸能用同一套写法判断。
--
-- 【用户 2026-10-30 的三项决策，写在这里以免以后被当成实现随意性】
--
-- 1. **单模型，不做双模型互证。**
--    app/vision.py 纪律 #2 原本要求「两个模型各读一遍，只有两边一致的格子
--    才算读出来」，理由是「一个模型读出来的东西，没有任何东西能证伪它」。
--    用户选择单模型（qwen3-vl-plus），这条纪律**就此作废，不是没实现**。
--    代价明确记在这里：读错的格子没有第二个来源能发现。
--
-- 2. **VL 读出来的文本与原生文本同等对待。**
--    纪律 #3 原本要求 doc_page_vision.verified_by 为空时，读出来的数字
--    「不许进 standard_limit、不许进任何规则阈值」。用户选择同等对待，
--    于是 verified_by 退化成一个**可选的人工复核留痕**，不再是闸。
--    代价：模型把看不清的格子编出来时，没有任何自动闸能拦住它，
--    只剩提示词里那条「看不清写 ?」和 unreadable 计数。
--    unreadable 因此比改动之前更重要——它是现在唯一的警报。
--
-- 3. **范围是两个实例全量**：822港航 9 份 / 120 页，福建海运集团 4 份 / 59 页。
--
-- 不动 doc_page_vision 表：它本来就逐页存、自带 image_path 与 token 留痕，
-- 结构够用。这里只补「文件级」的汇总与指纹，和 0018 给 OCR 补的那七列一样。
-- ============================================================================

ALTER TABLE doc_lib_file
    ADD COLUMN IF NOT EXISTS vision_state       TEXT,
    ADD COLUMN IF NOT EXISTS vision_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS vision_pages       INTEGER,
    ADD COLUMN IF NOT EXISTS vision_chars       INTEGER,
    ADD COLUMN IF NOT EXISTS vision_unreadable  INTEGER,
    ADD COLUMN IF NOT EXISTS vision_fingerprint TEXT,
    ADD COLUMN IF NOT EXISTS vision_model       TEXT,
    ADD COLUMN IF NOT EXISTS vision_note        TEXT;

COMMENT ON COLUMN doc_lib_file.vision_state IS
  '读图状态：已读图 / 读图失败 / NULL（没读过）。**和 ocr_state 互不替代**：'
  '同一份文件可以既 OCR 过又读图过，抽取按 doclib_extract 里的优先级选一个。';

COMMENT ON COLUMN doc_lib_file.vision_fingerprint IS
  '读图当时这份文件的指纹。**和 fingerprint 不等就说明文件后来被换过**，'
  '这时读图文本已经对应不上磁盘上的文件，抽取必须把它当没读过——'
  '口径和 ocr_fingerprint 完全一致。拿旧版文本抽现行制度的规则，'
  '抽出来的东西看着有据可查，实际对的是一份已经不存在的文件。';

COMMENT ON COLUMN doc_lib_file.vision_unreadable IS
  '模型自报「看不清」的格子数合计（逐页取 max(自报, 实数的 ? 个数)）。'
  '**这个数比读出来的内容更重要**：提示词给了模型一条「可以说不知道」的出路，'
  '一个一个 ? 都没有的回答，要么是真读清楚了，要么是把看不清的编出来了。'
  '用户已决定 VL 文本与原生文本同等对待（见文件头决策 2），'
  '所以这里是**唯一**还在报警的地方，界面上不许省掉它。';

COMMENT ON COLUMN doc_lib_file.vision_model IS
  '读这份文件用的型号。换型号重读会覆盖，但 doc_page_vision 里逐次留痕不删，'
  '所以「这段文字是哪个型号读出来的」始终查得到。';

CREATE INDEX IF NOT EXISTS ix_doclib_file_vision
    ON doc_lib_file (vision_state)
    WHERE vision_state IS NOT NULL;
