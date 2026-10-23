/**
 * 导航树。
 *
 * 领域知识资产的二级菜单**按规则加工链排序**，不是按字母或重要性：
 *   制度文件库 → 规则抽取 → 本体工场（业务本体 / 规则本体 / 流程本体）→ 发布管理
 *
 * **三个本体页是本体工场的子项**（2026-10-21 按要求）：三元本体就是工场搭出来的，
 * 平铺成同级会让人以为是四个互不相干的栏目，看不出「工场是入口、那三页是
 * 搭完之后看的」这层隶属关系。key 和 path 都没动，发出去的链接照旧能打开。
 *
 * 大模型配置原先挂在这条链上（标为「支撑项」），2026-10-18 按要求移到「系统」组：
 * 它是系统级配置，不是加工链上的一环，挂在链里反而让这条链读不清。
 * 路由随之从 /asset/llm 改为 /sys/llm，老路径留一条 redirect——
 * 已经发出去的链接和浏览器书签不该因为菜单挪了位置就 404。
 *
 * 这个顺序本身就是信息：上游没有的东西，下游不可能凭空长出来。
 * 每条规则都必须能回溯到制度文件库里的某一份文件、某一条条款。
 * 菜单顺序打乱，用户就看不出这条依赖关系了。
 *
 * `built` 标记哪些已经接了真实后端。**没接的不要藏起来**——
 * 藏起来会让人以为功能不存在；标成未接，才知道是「还没做」而不是「做不了」。
 *
 * 菜单项不带说明文字（2026-10-18 按要求去掉）：侧栏是导航，不是文档。
 * 每一栏的口径写在该页页首，看页面的人才需要看它。
 */
export interface NavItem {
  key: string
  label: string
  path: string
  built: boolean
  /** 未接时的说明。built=false 必须写，否则不知道缺什么 */
  todo?: string
  /** 隶属于本项的子菜单。目前只有「本体工场」有：三元本体是它搭出来的 */
  children?: NavItem[]
}

/**
 * 把带子项的菜单压平成一串。
 *
 * **凡是「遍历所有菜单项」的地方都要用它**：漏掉子项的校验等于没校验——
 * 子项的 path 写错一样是白屏，而它不在 g.items 里，循环根本碰不到它。
 */
export function flatItems(items: NavItem[]): NavItem[] {
  return items.flatMap((it) => [it, ...(it.children || [])])
}

export interface NavGroup {
  key: string
  label: string
  items: NavItem[]
  /**
   * 组自己的落地页。目前只有「领域知识资产」有：点组名进去选领域/实例。
   * 其余组没有，组名就是个标题，点了不动——**有没有落地页要看得出来**，
   * 所以 App.vue 里只有带 path 的组名才渲染成链接。
   */
  path?: string
}

export const RA_NAV: NavItem[] = [
  {
    key: 'doc-lib',
    label: '制度文件库',
    path: '/asset/docs',
    built: true,
  },
  {
    key: 'extract',
    label: '规则抽取',
    path: '/asset/extract',
    built: true,
  },
  {
    // 本体工场排在三个本体页**之前**：它是搭建入口，那三页是搭完之后看的。
    // 八步一条流水线，把原来切在三页上、外加两条命令行的过程合成一条。
    // 三元本体挂成它的子项——隶属关系要在菜单上看得出来。
    key: 'workshop',
    label: '本体工场',
    path: '/asset/workshop',
    built: true,
    children: [
      {
        // 业务本体在规则本体**之前**：规则引用概念，上游没有的概念下游长不出来。
        // 顺序照用户 2026-10-19 的说法排：源头 → 规则 → 业务本体 → 规则本体 → 版本发布。
        key: 'biz-onto',
        label: '业务本体',
        path: '/asset/biz',
        built: true,
      },
      {
        key: 'onto',
        label: '规则本体',
        path: '/asset/rules',
        built: true,
      },
      {
        key: 'proc',
        label: '流程本体',
        path: '/asset/process',
        built: true,
      },
    ],
  },
  {
    key: 'publish',
    label: '发布管理',
    path: '/asset/releases',
    built: true,
  },
]

export const REVIEW_NAV: NavItem[] = [
  {
    // AI智能审核在审核岗工作台之上：它是入口，工作台是出口。
    // 智能审核触发 → 规则引擎 + LLM 三层跑完 → 不合格单流入工作台。
    key: 'ai-review',
    label: 'AI智能审核',
    path: '/review/ai-review',
    built: true,
  },
  {
    key: 'workbench',
    label: '审核岗工作台',
    path: '/review/bills',
    built: true,
  },
  {
    key: 'trace',
    label: '审计追溯',
    path: '/review/trace',
    built: true,
  },
  {
    key: 'deviation',
    label: '偏差与回流',
    path: '/review/deviation',
    built: true,
  },
]

export const SYS_NAV: NavItem[] = [
  {
    // 智能体工作台放在系统组的**第一个**：它是全站 12 个 Agent 唯一能一次看全的
    // 地方（契约在文件里、状态在 agent_state、痕迹在信号与周期台账里，三处分散）。
    // 2026-10-19 按要求新增。
    key: 'agents',
    label: '智能体工作台',
    path: '/sys/agents',
    built: true,
  },
  {
    key: 'llm',
    label: '大模型配置',
    path: '/sys/llm',
    built: true,
  },
  {
    key: 'startup',
    label: '系统自检',
    path: '/sys/startup',
    built: true,
  },
]

export const NAV: NavGroup[] = [
  { key: 'asset', label: '领域知识资产', items: RA_NAV, path: '/asset' },
  { key: 'review', label: '报账单 AI 审核', items: REVIEW_NAV },
  { key: 'sys', label: '系统', items: SYS_NAV },
]
