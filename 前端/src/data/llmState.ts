/**
 * 大模型配置的状态映射。
 *
 * 单独成一个模块、而不是写在 LlmConfig.vue 里，只有一个原因：**要能被测试钉住**。
 * 写在组件里就得挂载整个页面（它 onMounted 会打三个接口）才能测一个 if，
 * 于是实际上没人测，于是哪天有人把三态合并成两态也没人发现。
 */

/** 后端 last_test_ok 的三种取值。null 是「没测过」，不是 false。 */
export type TestState = 'untested' | 'ok' | 'failed'

export function testState(lastTestOk: boolean | null | undefined): TestState {
  if (lastTestOk === null || lastTestOk === undefined) return 'untested'
  return lastTestOk ? 'ok' : 'failed'
}

/**
 * 三态各自的标签样式与文字。
 *
 * **「未测试」不许用「不通」的样式，也不许用「通过」的样式。**
 * 合成两态的后果很具体：
 *   并成「通过」→ 用户以为能用，跑批时全军覆没
 *   并成「不通」→ 用户去查一个根本没问题的配置
 * 这两种误导都比多一个灰标签贵。
 */
export const TEST_TAG: Record<TestState, { cls: string; text: string }> = {
  untested: { cls: 't-na', text: '未测试' },
  ok: { cls: 't-pass', text: '通过' },
  failed: { cls: 't-hit', text: '不通' },
}

export function testTag(lastTestOk: boolean | null | undefined) {
  return TEST_TAG[testState(lastTestOk)]
}

/**
 * 失败原因 → 人话。
 *
 * 分类的唯一标准是**下一步该干什么**，所以每条文案里都带动作：
 * 是去厂商控制台，还是查网络，还是重选型号。
 * 「调用失败」这种文案等于没说，用户只能瞎试。
 *
 * 键必须和后端 app/llm/client.py 里的 err_kind 对齐；
 * contract.spec.ts 里有一条测试钉住这个集合——
 * 后端加了新的 err_kind 而这里没加，界面会把英文原样显示给用户看。
 */
export const ERR_KINDS = [
  'no_key',
  'auth',
  'quota',
  'dns',
  'tls',
  'timeout',
  'model_not_found',
  'parse',
  'http',
] as const

export const ERR_LABEL: Record<string, string> = {
  no_key: '没填 Key',
  auth: '鉴权被拒 · 去厂商控制台',
  quota: '额度/限流 · 去厂商控制台',
  dns: '域名解析不了 · 地址或外网',
  tls: 'TLS 失败 · 多为代理中间人',
  timeout: '超时 · 网络或超时设置',
  model_not_found: '型号不存在 · 重选型号',
  parse: '响应解析不了 · 见原文',
  http: 'HTTP 错误',
}

/** 认不出来的原因原样显示，但不要显示成空白——空白会被当成「没有错误」。 */
export function errLabel(kind: string | null | undefined) {
  if (!kind) return ''
  return ERR_LABEL[kind] || kind
}

/**
 * 用途与模型能力的交叉校验。**这是这个模块里最重要的一条。**
 *
 * vision_read 是拿模型去读发票、行程单的图。配一个纯文本模型上去，
 * 结果不是报错——是模型根据 prompt 里的文字线索**编一段看起来合理的内容**。
 * 那种错误没有任何技术信号：HTTP 200、有回复、格式正确、读起来像真的。
 * 下游拿它当「已核验的票面信息」用，就成了「AI 看过发票了」而其实它没看见图。
 *
 * 只警告不拦：用户可能用自建网关或预置里没有的多模态型号，那时判不出来。
 * 拦死会把对的用法也挡掉。但文案要写得没法当装饰看，所以直接写后果。
 *
 * 后端 app/api/sys_llm.py 的 modality_warning() 是同一套判断。
 * 两边都要有：只在前端校验，绕过界面直接调接口就没人管了。
 */
/**
 * 常见多模态型号名里的标记。**只用来判「有」，不用来判「没有」**：
 * gpt-4o、kimi-k3、step-1o 这些多模态型号名里就没有 vl/vision，
 * 所以缺标记只能降级成「判不出，请自己确认」。
 */
const VISION_MARKERS = ['vl', 'vision', 'omni', '4o', '-v-', 'glm-4v', 'image']

export function looksVision(modelName: string | null | undefined): boolean {
  const m = (modelName || '').toLowerCase()
  return VISION_MARKERS.some((k) => m.includes(k))
}

export function modalityWarning(
  purpose: string,
  modality: 'text' | 'vision' | 'both' | null | undefined,
  modelName?: string | null,
  /** 已经配成文本用途（rule_judge / extract）的型号名。判「同一型号两用」。 */
  textModels?: string[],
  suggestModel?: string | null,
): string {
  if (purpose !== 'vision_read') return ''
  if (!modality) {
    return (
      '用途是「读票据图」，但没选厂商预置，判不出这个型号有没有视觉能力。' +
      '请自己确认它是多模态型号——纯文本模型拿到图不会报错，' +
      '会按 prompt 里的文字线索编一段看起来合理的内容。'
    )
  }
  if (modality === 'text') {
    return (
      '这个预置是纯文本型号，不能用于「读票据图」。' +
      '配上去不会报错，模型会按 prompt 里的文字线索编一段看起来合理的内容，' +
      '而下游会把它当成已核验的票面信息。请换成带「读图」/ VL 的预置。'
    )
  }
  // 预置说能读图，但真正发给厂商的是 model_name —— 2026-10-19 收紧的就是这一段。
  if (!modelName) return ''
  if ((textModels || []).includes(modelName)) {
    return (
      `这个型号（${modelName}）同时被配成了文本用途，所以它大概率是文本模型，` +
      '不是读图模型——**预置选对了不等于型号填对了**。' +
      '纯文本模型拿到图不会报错，会按 prompt 里的文字线索编一段看起来合理的内容，' +
      '而下游会把它当成已核验的票面信息。' +
      (suggestModel
        ? `这个预置建议的读图型号是 ${suggestModel}——**建议型号也可能已被厂商下架**，`
          + '点「测试 · 只列型号」看你账号上真有哪些。'
        : '')
    )
  }
  if (!looksVision(modelName)) {
    return (
      `预置标注能读图，但型号名（${modelName}）里没有 vl / vision / omni 这类标记，` +
      '**判不出**它有没有视觉能力（gpt-4o、kimi-k3 这类多模态型号名里也没有，' +
      '所以这不是结论）。请自己确认。' +
      (suggestModel
        ? `这个预置建议的读图型号是 ${suggestModel}，但**建议型号可能已下架**——`
          + '点「测试 · 只列型号」看你账号上真有哪些。'
        : '')
    )
  }
  return ''
}

/** Key 存储格式 → 人话。legacy 是旧的单向摘要，取不回明文，必须重填。 */
export const KEY_FORMAT: Record<string, { cls: string; text: string; hint: string }> = {
  ok: { cls: 't-pass', text: '已加密', hint: '' },
  none: { cls: 't-na', text: '未填', hint: '这条还没有 API Key，测试会直接失败' },
  legacy: {
    cls: 't-hit',
    text: '旧格式',
    hint: 'Key 是旧的单向摘要，取不回明文，测试必然失败。请重新填一次 API Key。',
  },
}
