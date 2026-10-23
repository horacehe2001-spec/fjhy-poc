/**
 * 当前领域 / 实例的前端共享状态。
 *
 * 单独成模块而不是写在页面里，两个原因：
 *   ① 落地页和各栏目页头的徽标要**同一份数据**。各自取一次，切换之后
 *      一个变了一个没变，看的人会以为切换没生效。
 *   ② 能被测试钉住，不用挂载整个页面。
 *
 * **真相在库里，这里只是缓存。** 当前选择是全局单例（所有人共用一行），
 * 所以别处的人改了，本地这份就是旧的——刷新即可拿到最新。
 * 不做轮询：为一个一天切不了两次的值轮询后端，不值。
 */
import { ref } from 'vue'
import { api } from '../api'
import type { DomainCurrent, DomainRow } from '../api'

export const current = ref<DomainCurrent | null>(null)
export const domains = ref<DomainRow[]>([])
export const loadErr = ref('')

/** 当前实例的一行标识。**没选实例时说「未选实例」，不编一个出来。** */
export function label(c: DomainCurrent | null): string {
  if (!c || !c.domain_label) return '未选领域'
  if (c.mismatch) return `${c.domain_label} / 实例与领域不匹配`
  return `${c.domain_label} / ${c.instance_label || '未选实例'}`
}

/** 当前选择是否完整（领域和实例都有）。不完整时页面要显式提示，不装作正常。 */
export function isComplete(c: DomainCurrent | null): boolean {
  return !!(c && c.domain_id && c.instance_id && !c.mismatch)
}

export async function load(force = false) {
  if (current.value && !force) return
  try {
    const d = await api.domainState()
    current.value = d.current
    domains.value = d.domains
    loadErr.value = ''
  } catch (e) {
    // 取不到**不能静默**：静默的结果是页头什么都不显示，
    // 看着像「没有领域这个概念」，而实际是接口挂了。
    loadErr.value = e instanceof Error ? e.message : String(e)
  }
}

export async function setCurrent(domainId: string, instanceId?: string | null) {
  const r = await api.domainSetCurrent(domainId, instanceId ?? null, 'ui')
  current.value = r.current
  return r
}
