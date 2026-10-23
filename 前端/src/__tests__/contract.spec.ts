/**
 * 前端契约测试。只测那些**坏了不会报错**的东西。
 *
 * 不测「按钮点了有没有反应」这类——那种坏了一眼就能看出来。
 * 这里守的是四件会悄悄出错的事：
 *   1. 领域知识资产各栏目的顺序（顺序本身是信息，乱了就看不出规则加工链）
 *   2. 错误被吞成空数据（页面显示「无数据」而实际是接口挂了）
 *   3.「待人工」长得像「通过」（审核岗会以为查过了没问题）
 *   4. 未接通的栏目被藏起来（会让人以为功能不存在，而不是「还没做」）
 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { ApiError } from '../api'
import { NAV, RA_NAV, REVIEW_NAV, SYS_NAV, flatItems } from '../data/navTree'
import { isComplete, label as domainLabel } from '../data/domainState'
import {
  ERR_KINDS,
  ERR_LABEL,
  KEY_FORMAT,
  errLabel,
  looksVision,
  modalityWarning,
  testState,
  testTag,
} from '../data/llmState'
import Outcome from '../components/Outcome.vue'
import Async from '../components/Async.vue'

describe('导航树', () => {
  it('领域知识资产有且只有 7 个栏目：4 个一级 + 本体工场下 3 个', () => {
    // 5 → 6：2026-10-19 新增「业务本体」。它原来没有页面——17 实体 / 121 概念 /
    // 21 枚举只能在接口里看到，而**本体怎么建的**这一步整个在系统之外
    // （人读材料手写 YAML + 命令行装载）。
    // 6 → 7：2026-10-20 新增「本体工场」。业务本体那一页补的是「能看见候选」，
    // 补不了「客户自己把三元本体从零搭一遍」——那条流水线仍然被切在三个页面上，
    // 外加两条只能在命令行跑的装载。工场把它合成一条，八步。
    // 7 → 4+3：2026-10-21 把业务/规则/流程三个本体页收进「本体工场」做子项
    // （用户决定）。**页面一个没少，还是 7 个**，变的是隶属关系——
    // 所以这里两个数都要钉：一级 4 个，压平 7 个。只钉一个的话，
    // 哪天有人把子项又平铺回去，另一个数仍然对得上，测试照样绿。
    // 数字留在旧值的话，这条测试就变成了保护一个已经作废的口径。
    expect(RA_NAV).toHaveLength(4)
    expect(flatItems(RA_NAV)).toHaveLength(7)
  })

  it('领域知识资产这一组有落地页入口', () => {
    // 组名是进「选领域/实例」那一页的唯一入口。path 没了，组名退化成死文字，
    // 而页面还在、路由还通——**没有任何报错**，只是谁也进不去了。
    expect(NAV.find((g) => g.key === 'asset')!.path).toBe('/asset')
    // 其余组没有落地页：点了没反应的链接比没有链接更坏
    expect(NAV.find((g) => g.key === 'review')!.path).toBeUndefined()
    expect(NAV.find((g) => g.key === 'sys')!.path).toBeUndefined()
  })

  it('大模型配置在「系统」组，不在领域知识资产里', () => {
    // 它是系统级配置，不是规则加工链上的一环。混进加工链会让那条链读不清。
    expect(RA_NAV.map((x) => x.key)).not.toContain('llm')
    expect(SYS_NAV.map((x) => x.key)).toContain('llm')
    // 路由也得跟着走，否则菜单在「系统」下而地址栏还是 /asset/…，看着像挂错了
    expect(SYS_NAV.find((x) => x.key === 'llm')!.path).toBe('/sys/llm')
  })

  it('栏目按规则加工链排序，顺序不能乱', () => {
    // 制度文件库 → 规则抽取 → 本体工场（业务本体 → 规则本体 → 流程本体）→ 发布管理
    // 这个顺序本身就是信息：上游没有的东西，下游不可能凭空长出来。
    // **工场排在三个本体页之前**：它是搭建入口，那三页是搭完之后看的；
    // 现在那三页就挂在它下面，隶属关系直接写在菜单结构里。
    // 顺序打乱，用户就看不出这条依赖关系了。
    expect(RA_NAV.map((x) => x.key)).toEqual(['doc-lib', 'extract', 'workshop', 'publish'])
    expect(RA_NAV.find((x) => x.key === 'workshop')!.children!.map((x) => x.key)).toEqual([
      'biz-onto',
      'onto',
      'proc',
    ])
    // 压平之后就是那条加工链本身，一字不差
    expect(flatItems(RA_NAV).map((x) => x.key)).toEqual([
      'doc-lib',
      'extract',
      'workshop',
      'biz-onto',
      'onto',
      'proc',
      'publish',
    ])
  })

  it('每个菜单项都有路径', () => {
    // 菜单项不再带说明文字：侧栏是导航不是文档，口径写在各页页首。
    // 路径仍然要钉——路径写错是白屏，比没有说明严重得多。
    for (const g of NAV) {
      for (const it of flatItems(g.items)) {
        expect(it.path, `${it.key} 缺 path`).toMatch(/^\//)
      }
    }
  })

  it('未接通的栏目必须写明缺什么，而且不许隐藏', () => {
    // 藏起来会让人以为功能不存在；标成未接，才知道是「还没做」而不是「做不了」。
    const all = NAV.flatMap((g) => flatItems(g.items))
    for (const it of all) {
      if (!it.built) {
        expect(it.todo, `${it.key} 标了 built=false 但没写 todo`).toBeTruthy()
      }
    }
    // 有 todo 的也必须出现在导航里（即不被过滤掉）
    //
    // 原来这里还有一条 `withTodo.length > 0`，用来防止这条测试空转。
    // 现在所有栏目都接通了、一个 todo 都没有，于是它开始报假红——
    // **而假红比没有测试坏**：它会让人养成「红了先看看是不是又是那条」的习惯。
    // 真正的不变量是上面那条（built=false 必须写 todo），它与「现在有没有未接栏目」无关。
    const withTodo = all.filter((x) => x.todo)
    for (const it of withTodo) {
      expect(NAV.some((g) => flatItems(g.items).includes(it))).toBe(true)
    }
    // 空转防护改成钉「导航本身不是空的」——这才是这组测试真正依赖的前提
    expect(all.length).toBeGreaterThan(0)
  })

  // 顺序也钉住：AI智能审核是入口（触发三层审核），工作台是出口（不合格单流进来），
  // 再往后才是追溯与回流。加页面时顺序排错，审核岗会从出口开始找入口。
  it('审核端的四个页面都在而且顺序没乱', () => {
    expect(REVIEW_NAV.map((x) => x.key)).toEqual([
      'ai-review', 'workbench', 'trace', 'deviation',
    ])
  })
})

describe('接口错误不许被吞', () => {
  it('ApiError 带状态码与后端 detail', () => {
    const e = new ApiError(404, '报账单 X 不存在')
    expect(e.status).toBe(404)
    expect(e.detail).toBe('报账单 X 不存在')
    expect(e.message).toContain('404')
  })

  it('Async 组件在出错时显示错误，而不是退化成空列表', async () => {
    // 这是前端最常见的隐藏故障方式：catch 之后 return []。
    // 页面上看着是「没有数据」，实际是接口挂了——
    // 审核岗据此以为「这批单没问题」，而真相是什么都没查。
    const w = mount(Async, {
      props: { load: () => Promise.reject(new ApiError(500, '数据库连不上')) },
      slots: { default: '<div class="ok-slot">有数据</div>' },
    })
    await new Promise((r) => setTimeout(r, 0))
    expect(w.text()).toContain('取数失败')
    expect(w.text()).toContain('数据库连不上')
    expect(w.text()).toContain('这不是「没有数据」')
    expect(w.find('.ok-slot').exists()).toBe(false)
  })

  it('Async 组件成功时才渲染插槽', async () => {
    const w = mount(Async, {
      props: { load: () => Promise.resolve({ n: 1 }) },
      slots: { default: '<div class="ok-slot">有数据</div>' },
    })
    await new Promise((r) => setTimeout(r, 0))
    expect(w.find('.ok-slot').exists()).toBe(true)
    expect(w.text()).not.toContain('取数失败')
  })
})

describe('判定结果标签', () => {
  const cls = (outcome: string, extra = {}) =>
    mount(Outcome, { props: { outcome, ...extra } }).find('.tag').classes()

  it('待人工不能长得像通过', () => {
    // 把 pending 显示成绿色或干脆不显示，审核岗就会以为这条查过了而且没问题，
    // 而实际是「判不了」。那等于让系统声称复核了没复核的东西。
    expect(cls('pending')).toContain('t-pend')
    expect(cls('pending')).not.toContain('t-pass')
    expect(cls('pass')).toContain('t-pass')
    expect(cls('hit')).toContain('t-hit')
  })

  it('四态的样式两两不同', () => {
    const got = ['hit', 'pass', 'pending', 'excluded'].map((o) =>
      cls(o).filter((c) => c.startsWith('t-')).join(),
    )
    expect(new Set(got).size).toBe(4)
  })

  it('不适用与通过要分开显示', () => {
    // 「不适用」说明这条规则跟本单无关；「合规」说明查过了而且没问题。
    // 混在一起会让覆盖面看起来比实际大。
    const na = mount(Outcome, { props: { outcome: 'pass', notApplicable: true } })
    expect(na.text()).toContain('不适用')
    const pass = mount(Outcome, { props: { outcome: 'pass' } })
    expect(pass.text()).toContain('通过')
  })

  it('命中时要显示控制类型，禁止与警告不同色', () => {
    const block = mount(Outcome, { props: { outcome: 'hit', controlType: '禁止' } })
    const warn = mount(Outcome, { props: { outcome: 'hit', controlType: '警告' } })
    expect(block.html()).toContain('t-block')
    expect(warn.html()).toContain('t-warn')
    // 不命中时不显示控制类型——一条通过的规则标着「禁止」会让人误读
    const pass = mount(Outcome, { props: { outcome: 'pass', controlType: '禁止' } })
    expect(pass.html()).not.toContain('t-block')
  })
})

describe('判定四态的颜色（功能色，不是装饰）', () => {
  // 上面那几条测的是 class 名。class 名对了而四个 class 恰好被调成同一个颜色，
  // 测试照样全绿——改配色时这正是最容易发生的事：
  // 为了跟官网协调，把四个颜色一起往蓝灰里压，压到肉眼分不开。
  // 所以这里直接读 style.css 量颜色距离。
  // vitest 的 import.meta.url 不是 file: 协议（是 Vite 的模块 URL），
  // 拿它喂 readFileSync 会报 'The URL must be of scheme file'。走 cwd。
  const cssPath = resolve(process.cwd(), 'src/style.css')
  expect(existsSync(cssPath), 'style.css 找不到：' + cssPath).toBe(true)
  const css = readFileSync(cssPath, 'utf8')

  /** 取 :root 里的变量定义 */
  const vars = Object.fromEntries(
    [...css.matchAll(/--([a-z-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]),
  )

  /** 解一层 var(--x)，再转 RGB */
  const rgb = (raw: string): [number, number, number] => {
    const v = raw.replace(/var\(--([a-z-]+)\)/, (_, n) => vars[n] ?? '')
    const h = v.trim().replace('#', '')
    const f = h.length === 3 ? h.split('').map((c) => c + c).join('') : h
    expect(f, `色值解不出来：${raw}`).toMatch(/^[0-9a-fA-F]{6}$/)
    return [0, 2, 4].map((i) => parseInt(f.slice(i, i + 2), 16)) as [number, number, number]
  }

  const dist = (a: string, b: string) =>
    Math.hypot(...rgb(a).map((x, i) => x - rgb(b)[i]))

  it('四个变量都有定义', () => {
    for (const k of ['hit', 'pend', 'pass', 'na']) {
      expect(vars[k], `--${k} 没定义`).toBeTruthy()
    }
  })

  it('四态颜色两两拉得开', () => {
    const keys = ['hit', 'pend', 'pass', 'na']
    for (let i = 0; i < keys.length; i++) {
      for (let j = i + 1; j < keys.length; j++) {
        const d = dist(vars[keys[i]], vars[keys[j]])
        // 60 是经验阈值：低于这个数，两个色块并排放在表格里一眼分不出。
        expect(d, `--${keys[i]} 与 --${keys[j]} 太像了（距离 ${d.toFixed(0)}）`,
        ).toBeGreaterThan(60)
      }
    }
  })

  it('待人工与通过的标签文字色拉得开', () => {
    // 标签是浅底 + 深色文字 + 同色边框，**文字色才是主信号**。
    // 浅底两者接近是可以的，文字色不行。
    // 不用正则取：模板字符串里的 \\s 很容易在编辑过程中退化成 s，
    // 那样匹配不到规则，测试会以「规则找不到」的形式失败——
    // 看着像 CSS 少了一条，实际是测试自己的转义坏了。纯字符串切片没这个风险。
    const pick = (cls: string) => {
      const at = css.indexOf('.' + cls + ' ')
      expect(at, `.${cls} 规则找不到`).toBeGreaterThan(-1)
      const body = css.slice(css.indexOf('{', at) + 1, css.indexOf('}', at))
      const decl = body.split(';').map((x) => x.trim())
        .find((x) => x.startsWith('color:'))
      expect(decl, `.${cls} 没写 color`).toBeTruthy()
      return decl!.slice('color:'.length).trim()
    }
    const d = dist(pick('t-pend'), pick('t-pass'))
    expect(d, `t-pend 与 t-pass 文字色太像（距离 ${d.toFixed(0)}）`).toBeGreaterThan(60)
  })

  it('官网主色与金色点缀没被改掉', () => {
    // 这几个值是从 www.fjpg.cn 的 CSS 里取的。
    // 不是"差不多的蓝"——换掉就不是参照官网了，所以钉住。
    expect(vars['brand']).toBe('#015293')
    expect(vars['brand-light']).toBe('#1f93f9')
    expect(vars['gold']).toBe('#c19c41')
  })
})

describe('当前领域/实例', () => {
  it('没选实例时说「未选实例」，不编一个出来', () => {
    // 「审核 / 」或者退到上一个实例名，都会让人以为选中了某个实例。
    expect(domainLabel({ domain_id: 'report', domain_label: '报表',
                         instance_id: null, instance_label: null, mismatch: false }))
      .toBe('报表 / 未选实例')
  })

  it('领域与实例不匹配时要显示出来，不许装作正常', () => {
    const c = { domain_id: 'report', domain_label: '报表', instance_id: null,
                instance_label: null, mismatch: true }
    expect(domainLabel(c)).toContain('不匹配')
    expect(isComplete(c)).toBe(false)
  })

  it('领域和实例都齐了才算选全', () => {
    expect(isComplete({ domain_id: 'audit', domain_label: '审核',
                        instance_id: 'fjhy', instance_label: '福建海运集团',
                        mismatch: false })).toBe(true)
    expect(isComplete(null)).toBe(false)
  })
})

describe('大模型配置的状态显示', () => {
  it('「没测过」「通了」「不通」是三态，不许合成两态', () => {
    // 合成两态的后果很具体：
    //   并成「通过」→ 用户以为能用，跑批时全军覆没
    //   并成「不通」→ 用户去查一个根本没问题的配置
    expect(testState(null)).toBe('untested')
    expect(testState(undefined)).toBe('untested')
    expect(testState(true)).toBe('ok')
    expect(testState(false)).toBe('failed')
    const styles = [null, true, false].map((v) => testTag(v).cls)
    expect(new Set(styles).size).toBe(3)
  })

  it('未测试不许长得像通过，也不许长得像不通', () => {
    expect(testTag(null).cls).not.toBe(testTag(true).cls)
    expect(testTag(null).cls).not.toBe(testTag(false).cls)
    expect(testTag(null).text).toContain('未测')
  })

  it('每种失败原因都要有中文文案，而且文案里带「下一步干什么」', () => {
    // 后端 app/llm/client.py 加了新的 err_kind 而这里没加，
    // 界面会把英文原样显示给用户看。那种退化不报错、没人会注意到。
    for (const k of ERR_KINDS) {
      expect(ERR_LABEL[k], `err_kind ${k} 没有中文文案`).toBeTruthy()
    }
    // 「调用失败」这种文案等于没说。至少这几类要指出去哪儿解决。
    for (const k of ['auth', 'quota']) {
      expect(ERR_LABEL[k]).toContain('厂商')
    }
    expect(ERR_LABEL['model_not_found']).toContain('型号')
  })

  it('认不出来的原因原样显示，不能变成空白', () => {
    // 空白会被当成「没有错误」——那是把失败显示成了正常。
    expect(errLabel('something_new')).toBe('something_new')
    expect(errLabel(null)).toBe('')
  })
})

describe('用途与模型能力的交叉校验', () => {
  it('读票据图配纯文本模型必须警告', () => {
    // 这是整套系统里最难发现的错误：纯文本模型拿到图不报错，
    // 它会按 prompt 里的文字线索编一段看起来合理的内容——
    // HTTP 200、有回复、格式正确、读起来像真的，没有任何技术信号。
    const w = modalityWarning('vision_read', 'text')
    expect(w).toBeTruthy()
    expect(w).toContain('编')          // 文案必须点明「会编」这个后果
  })

  it('读图配多模态不该报警告', () => {
    expect(modalityWarning('vision_read', 'vision')).toBe('')
    expect(modalityWarning('vision_read', 'both')).toBe('')
  })

  it('判不出能力时也要提醒，不能当成没问题', () => {
    // 手动填地址时 modality 是 null。静默放过等于把「不知道」当成「没问题」。
    expect(modalityWarning('vision_read', null)).toBeTruthy()
    expect(modalityWarning('vision_read', undefined)).toBeTruthy()
  })

  // ── 2026-10-19 收紧：预置选对了不等于型号填对了 ────────────────────────
  // 实测躺过一个配置：preset=aliyun_bailian_vision（标注 vision），
  // 而 model_name=qwen3.5-122b-a10b —— 和两个文本用途的配置一模一样。
  // 原来只看预置，于是这条检查一次都没报过。
  it('预置能读图但型号被当成文本用途在用，要报警告', () => {
    const w = modalityWarning('vision_read', 'vision', 'qwen3.5-122b-a10b',
                              ['qwen3.5-122b-a10b'], 'qwen-vl-max')
    expect(w).toBeTruthy()
    expect(w).toContain('qwen3.5-122b-a10b')   // 要指名道姓，不能只说「有问题」
    expect(w).toContain('qwen-vl-max')         // 要给出该填什么
    expect(w).toContain('编')                  // 后果必须写出来
  })

  it('型号名带 vl 标记且没被当文本用，不报警告（不许误伤）', () => {
    expect(modalityWarning('vision_read', 'vision', 'qwen-vl-max', [], 'qwen-vl-max'))
      .toBe('')
    expect(modalityWarning('vision_read', 'both', 'glm-4v-plus', [])).toBe('')
  })

  it('型号名没有多模态标记时只说「判不出」，不说「不行」', () => {
    // gpt-4o / kimi-k3 这类多模态型号名里也没有 vl/vision，
    // 所以缺标记只能降级成提醒。把「不知道」说成「有问题」也是错的。
    const w = modalityWarning('vision_read', 'vision', 'my-gateway-model-7b', [])
    expect(w).toBeTruthy()
    expect(w).toContain('判不出')
    expect(w).not.toContain('不能用于')
  })

  it('纯文本预置的硬结论不受影响（原有逻辑不许被改坏）', () => {
    const w = modalityWarning('vision_read', 'text', 'qwen-vl-max', [])
    expect(w).toContain('不能用于')
  })

  it('没填型号名时不乱报', () => {
    expect(modalityWarning('vision_read', 'vision', '', [])).toBe('')
    expect(modalityWarning('vision_read', 'vision', null, [])).toBe('')
  })

  it('looksVision 只用来判「有」，不用来判「没有」', () => {
    expect(looksVision('qwen-vl-max')).toBe(true)
    expect(looksVision('gpt-4o')).toBe(true)
    expect(looksVision('glm-4v-plus')).toBe(true)
    expect(looksVision('qwen3.5-122b-a10b')).toBe(false)
  })

  it('其它用途不受这条校验影响', () => {
    expect(modalityWarning('rule_judge', 'text')).toBe('')
    expect(modalityWarning('extract', 'text')).toBe('')
  })
})

describe('Key 存储格式', () => {
  it('三种格式两两不同，且旧格式要明说必须重填', () => {
    const cls = ['ok', 'none', 'legacy'].map((k) => KEY_FORMAT[k].cls)
    expect(new Set(cls).size).toBe(3)
    expect(KEY_FORMAT.legacy.hint).toContain('重新填')
    // 「未填」不能长得像「已加密」——那会让人以为配好了
    expect(KEY_FORMAT.none.cls).not.toBe(KEY_FORMAT.ok.cls)
  })
})

describe('规则本体 · 统一口径', () => {
  const src = readFileSync(resolve(__dirname, '../api.ts'), 'utf-8')
  const page = readFileSync(resolve(__dirname, '../pages/RuleOnto.vue'), 'utf-8')

  it('归并组与口径冲突的客户端方法在 onto 下，不在 extract 下', () => {
    // 「统一口径是本体要做的事，规则抽取只负责抽取」。
    // 两处都挂着同一组接口更糟：改了一处忘另一处，两个页面显示的冲突数会不一样。
    // 注意别把引号写进断言：这几条是模板字符串（反引号 + ${qs(...)}），
    // 带着单引号断言会红，而代码是对的——第一版就这么红了一次。
    expect(src).toContain('/api/onto/conflicts')
    expect(src).toContain('/api/onto/groups')
    expect(src).not.toContain('/api/extract/conflicts')
    expect(src).not.toContain('/api/extract/groups')
  })

  it('三档与五档一个都不许少', () => {
    // verdict 的「判不出」和 state 的「编号对不上」是后端刻意不下断言的档位。
    // 前端把它们折叠掉，等于替后端下了它不肯下的结论：
    //   判不出 → 未落地   人会去新增一条其实已经存在的规则（重复规则对同一张单给两条结论）
    //   编号对不上 → 查无依据  人会去改一条其实有依据的规则
    for (const v of ['已落地', '未落地', '判不出']) expect(page).toContain(v)
    for (const st of ['有制度依据', '仅规则表', '编号对不上', '查无依据', '来源未写明']) {
      expect(page).toContain(st)
    }
  })

  it('双向对账不许合成一个覆盖率', () => {
    expect(page).toContain('不合成一个覆盖率')
    expect(src).not.toMatch(/coverage_rate|覆盖率:/)
  })

  it('采纳表单不给控制类型等默认值', () => {
    // 预填一个控制类型，人就会直接点确认；而控制类型选错，
    // 就是把「提醒」变成「拦单」，或者反过来让一道闸静默失效。
    expect(page).toContain("form.value = { name:")
    expect(page).not.toMatch(/control_type:\s*'(禁止|警告)'/)
  })

  it('协同视图只有一份实现，两条链共用', () => {
    // 留两份拷贝的下场：两个页面上「跑到一半失败」长得不一样，
    // 而没人说得清哪个才是对的。后端对应 app/agent_job.py。
    expect(existsSync(resolve(__dirname, '../components/AgentRuntime.vue'))).toBe(true)
    const ex = readFileSync(resolve(__dirname, '../pages/Extract.vue'), 'utf-8')
    expect(ex).toContain('AgentRuntime')
    expect(page).toContain('AgentRuntime')
    // 进度条样式也只许有一份：两份会各自调，然后两条链的条看起来不一样
    expect(ex).not.toContain('.pano .bus')
    expect(page).not.toContain('.pano .bus')
  })
})

describe('本体工场 · 装载两步的闸', () => {
  const src = readFileSync(
    resolve(__dirname, '../components/OntoLoadSteps.vue'), 'utf-8')

  it('灰不灰照抄后端，前端不自己判', () => {
    // 前端判一遍 = 两套规则，而前端那套永远先过期。
    expect(src).toContain('ontoLoadAssetsReady')
    expect(src).toContain('!gate?.ok')
    // 不许出现前端自己拼的判定条件（比如照 checks 里某一项自己算）
    expect(src).not.toMatch(/checks\.(every|some|filter)\s*\(/)
  })

  it('闸取不到时按保守的一侧落：当作没过', () => {
    // 取不到就放行，等于网络抖一下就能绕过这道闸。
    const fallback = src.slice(src.indexOf('catch (e)'), src.indexOf('async function run'))
    expect(fallback).toContain('ok: false')
  })

  it('两档照显，不许折叠成一档', () => {
    // warning 折进 blocker 会把能用的功能锁死（库里现在就有一条 OUT 绑不上）；
    // blocker 折进 warning 会让坏数据静默进库。
    expect(src).toContain("'blocker'")
    expect(src).toContain("t-pend")   // warning 用待定色，不是失败色
    expect(src).toContain("t-hit")    // blocker 才是失败色
  })

  it('灰着的按钮说得出为什么灰，而且不是悬停提示', () => {
    expect(src).toContain('gate.reason')
    expect(src).not.toMatch(/title=["']\{\{\s*gate/)
  })

  it('③ 的按钮受闸控制，② 不受', () => {
    const btn3 = src.slice(src.indexOf('loadAssets">'), src.indexOf('loadAssets">') + 200)
    expect(src).toContain(':disabled="!!busy || !gate?.ok" @click="loadAssets"')
    expect(btn3.length).toBeGreaterThan(0)
    // ② 只受 busy 控制：本体装载本身没有前置
    expect(src).toContain(':disabled="!!busy" @click="loadOnto"')
  })

  it('不许再对外说装载会清空 release_current', () => {
    const api = readFileSync(resolve(__dirname, '../api.ts'), 'utf-8')
    expect(api).not.toContain('why_not_a_button')
    expect(src).not.toContain('清空 release_current')
  })
})
