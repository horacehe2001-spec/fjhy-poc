-- ============================================================================
-- 0013 · 报账单变更通知
-- ----------------------------------------------------------------------------
-- L0 感知 Agent 订阅 fjhy_bill_changed，但**没有任何 Agent 发送它**——
-- 这是 YAMLAgentLoader 的「订阅的通道都有 Agent 发送」那一项校验报出来的。
-- 它不该由 Agent 发：事实的源头是表本身，由表来通知最直接。
--
-- 为什么用触发器而不是在应用层发通知：
--   触发器在事务内执行，事务回滚则通知不会发出。应用层做不到——
--   「先 commit 再 notify」会在两步之间崩掉丢通知；
--   「先 notify 再 commit」会在回滚后留下一个指向不存在的行的通知。
--
-- payload 只带单据号，不带业务内容。两个理由：
--   一是 NOTIFY 的 payload 上限 8000 字节；
--   二是通知内容会进 PostgreSQL 的通知队列，那是又一个出口，
--      带了姓名账号就等于绕过了脱敏。订阅方拿到单号自己回表读。
-- ============================================================================

CREATE OR REPLACE FUNCTION fn_notify_bill_changed() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('fjhy_bill_changed',
                      json_build_object('bill_no', NEW.bill_no,
                                        'op', lower(TG_OP))::TEXT);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_notify_bill_changed ON expense_bill;
CREATE TRIGGER tr_notify_bill_changed
    AFTER INSERT OR UPDATE OF remark, amount, biz_type, bill_status ON expense_bill
    FOR EACH ROW EXECUTE FUNCTION fn_notify_bill_changed();

-- 只盯这几列的 UPDATE，不是全部列：
--   remark   补了说明 → 例外出口变了 → 要重审
--   amount / biz_type / bill_status → 判定依据变了 → 要重审
-- 盯全部列会让「审核结果回写」这类动作也触发通知，形成自激循环：
-- 审核 → 写 review_run → 若顺手更新了单据某列 → 又通知 → 又审核。
-- 列清单是这个循环的闸。
COMMENT ON FUNCTION fn_notify_bill_changed() IS
  '报账单变更通知。只在 remark/amount/biz_type/bill_status 变化时发，'
  '避免「审核结果回写触发重审」的自激循环。';
