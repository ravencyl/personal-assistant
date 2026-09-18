import {
  BarChart,
  Callout,
  Card,
  CardBody,
  CardHeader,
  CollapsibleSection,
  Divider,
  Grid,
  H1,
  H2,
  H3,
  Pill,
  Progress,
  Row,
  Stack,
  Stat,
  Table,
  Tag,
  Text,
  useHostTheme,
} from 'qoder/canvas';

/* ─── 数据 ─── */

interface Feature {
  id: string;
  name: string;
  desc: string;
  effort: 'S' | 'M' | 'L';
  impact: '高' | '中' | '低';
  category: string;
  deps: string[];
  highlights: string[];
}

const features: Feature[] = [
  {
    id: 'A1',
    name: '活动前提醒推送',
    desc: '活动开始前 30 分钟自动推送 Web Push，深链直达活动详情。cron 每 5 分钟扫描即将开始的活动。',
    effort: 'S',
    impact: '高',
    category: '主动智能',
    deps: [],
    highlights: ['复用 PushSchedule 基础设施', '深链 /activities/<id>/', '幂等：已推送不重复发'],
  },
  {
    id: 'A2',
    name: '每日「今日焦点」智能排序',
    desc: '在 /today/ 页面顶部用 AI 从今日活动+笔记+记忆中选出最重要的 3 件事，附一句话理由。',
    effort: 'M',
    impact: '高',
    category: '主动智能',
    deps: [],
    highlights: ['复用 ai_round_trip', '失败降级为规则排序', '缓存避免重复调用'],
  },
  {
    id: 'A3',
    name: '目标进度自动追踪',
    desc: '记忆中的「目标」类记忆自动关联相关活动，计算完成百分比，在 /memory/ 页面可视化展示。',
    effort: 'M',
    impact: '中',
    category: '主动智能',
    deps: [],
    highlights: ['基于 tags 匹配记忆与活动', '进度条 UI：已完成/总相关活动', '周回顾中自动汇报目标进展'],
  },
  {
    id: 'A4',
    name: '行为模式学习',
    desc: '分析过去 30 天活动完成模式（周几最活跃、什么类型完成率高），在 Daily 页给出个性化建议。',
    effort: 'L',
    impact: '中',
    category: '主动智能',
    deps: [],
    highlights: ['cron 每周日跑一次模式分析', '结果存为 Memory（习惯类）', 'Daily 建议区展示洞察'],
  },
  {
    id: 'B1',
    name: 'SQLite FTS5 全文搜索',
    desc: '用 SQLite 内置 FTS5 替换 icontains，支持中文分词、相关度排序、搜索结果高亮。',
    effort: 'M',
    impact: '高',
    category: '搜索增强',
    deps: [],
    highlights: ['新建 FTS5 虚拟表 + 数据同步', '搜索 API 增加 rank 排序', '结果关键词高亮'],
  },
  {
    id: 'B2',
    name: '搜索历史 & 自动补全',
    desc: '记录最近 20 条搜索词，输入时下拉建议。Cmd+K 命令面板集成搜索历史。',
    effort: 'S',
    impact: '中',
    category: '搜索增强',
    deps: [],
    highlights: ['localStorage 存搜索历史', '输入防抖 300ms 触发建议', '点击历史直接跳转结果'],
  },
  {
    id: 'B3',
    name: '记忆模块搜索集成',
    desc: '全局搜索覆盖 Memory 模块，搜索结果按相关度展示记忆条目。',
    effort: 'S',
    impact: '中',
    category: '搜索增强',
    deps: [],
    highlights: ['search.py 新增 memory 分组', '展示记忆类别 + 内容摘要', '点击跳转记忆管理页'],
  },
  {
    id: 'C1',
    name: '活动依赖关系图',
    desc: '在活动列表页增加「依赖图」视图，用 SVG 绘制 blocked_by 关系的有向无环图。',
    effort: 'M',
    impact: '中',
    category: '数据可视化',
    deps: [],
    highlights: ['SVG 力导向布局（纯 JS）', '节点颜色按状态着色', '点击节点跳转详情'],
  },
  {
    id: 'C2',
    name: '记忆可视化面板',
    desc: '/memory/ 页面新增「记忆地图」Tab：按类别分组的雷达图 + 时间线散点图 + 重要性分布。',
    effort: 'M',
    impact: '中',
    category: '数据可视化',
    deps: [],
    highlights: ['雷达图：6 类记忆数量分布', '时间线：记忆创建/访问散点', '统计卡片：总数/本月新增/最高重要性'],
  },
  {
    id: 'C3',
    name: '费用趋势热力图',
    desc: '在费用报告页增加按月份 x 类别的热力图，一眼看出哪个月在哪类花费最多。',
    effort: 'S',
    impact: '中',
    category: '数据可视化',
    deps: [],
    highlights: ['SVG 热力图（颜色深浅 = 金额）', 'X 轴月份 x Y 轴费用类别', 'hover 显示具体金额'],
  },
  {
    id: 'D1',
    name: '知识库自动标签建议',
    desc: '创建/编辑知识文章时，AI 自动分析内容并建议 3-5 个标签，用户一键采纳或忽略。',
    effort: 'S',
    impact: '中',
    category: '知识增强',
    deps: [],
    highlights: ['表单提交前异步调 AI 分析', '标签 chips 展示建议结果', '采纳后合并到标签体系'],
  },
  {
    id: 'D2',
    name: '知识问答增强（RAG）',
    desc: 'knowledge.search 升级为 RAG 模式：先检索相关段落，再拼入 AI 上下文回答。',
    effort: 'L',
    impact: '高',
    category: '知识增强',
    deps: [],
    highlights: ['文章按段落切分 + 嵌入向量', '检索 top-K 段落注入上下文', '回答末尾附引用来源链接'],
  },
  {
    id: 'D3',
    name: '知识文章版本历史',
    desc: '编辑文章时自动保存历史版本，支持查看 diff 和回滚。',
    effort: 'M',
    impact: '低',
    category: '知识增强',
    deps: [],
    highlights: ['ArticleVersion 模型', '编辑时自动创建版本快照', 'diff 视图高亮变更'],
  },
  {
    id: 'E1',
    name: '快速日程规划（拖拽排期）',
    desc: '在日历视图中支持拖拽活动调整日期，拖到某天即修改 start_date。',
    effort: 'M',
    impact: '中',
    category: '效率工具',
    deps: [],
    highlights: ['HTML5 Drag & Drop API', '拖拽结束 fetch PATCH', '冲突时高亮提示'],
  },
  {
    id: 'E2',
    name: '批量操作增强',
    desc: '活动列表页支持多选后批量修改状态/标签/日期，Cmd+K 也可触发。',
    effort: 'M',
    impact: '中',
    category: '效率工具',
    deps: [],
    highlights: ['checkbox 多选 + 底部操作栏', '批量操作确认弹窗', '操作日志逐条记录'],
  },
  {
    id: 'E3',
    name: '快捷模板（常用活动）',
    desc: '把常用活动配置保存为模板，创建时一键填充名称/标签/参与者/预算。',
    effort: 'S',
    impact: '中',
    category: '效率工具',
    deps: [],
    highlights: ['ActivityTemplate 模型', '创建页「从模板创建」下拉', 'AI 工具也支持模板创建'],
  },
  {
    id: 'F1',
    name: '离线模式 PWA 增强',
    desc: 'Service Worker 缓存关键页面，离线时可查看活动/笔记/记忆（只读），联网后同步。',
    effort: 'L',
    impact: '中',
    category: '体验优化',
    deps: [],
    highlights: ['SW 预缓存关键页面', 'IndexedDB 存离线操作队列', 'Background Sync API'],
  },
  {
    id: 'F2',
    name: '键盘导航增强',
    desc: '全站支持 j/k 上下选择、Enter 打开、Esc 返回，类 Vim 风格。',
    effort: 'M',
    impact: '低',
    category: '体验优化',
    deps: [],
    highlights: ['全局 keydown 监听', '焦点环视觉反馈', '列表页/对话页/活动页可导航'],
  },
];

const categories = [
  { key: '主动智能', color: '#6366f1', icon: '⚡' },
  { key: '搜索增强', color: '#0ea5e9', icon: '🔍' },
  { key: '数据可视化', color: '#10b981', icon: '📊' },
  { key: '知识增强', color: '#f59e0b', icon: '📚' },
  { key: '效率工具', color: '#ec4899', icon: '⚙️' },
  { key: '体验优化', color: '#8b5cf6', icon: '✨' },
];

const effortWeight: Record<string, number> = { S: 1, M: 2, L: 3 };
const impactScore: Record<string, number> = { '高': 3, '中': 2, '低': 1 };

function EffortBadge({ effort }: { effort: 'S' | 'M' | 'L' }) {
  const map: Record<string, { label: string; tone: 'success' | 'warning' | 'danger' }> = {
    S: { label: '小', tone: 'success' },
    M: { label: '中', tone: 'warning' },
    L: { label: '大', tone: 'danger' },
  };
  const { label, tone } = map[effort];
  return <Tag tone={tone}>{label}</Tag>;
}

function ImpactBadge({ impact }: { impact: string }) {
  const tone = impact === '高' ? 'success' : impact === '中' ? 'warning' : 'neutral';
  return <Tag tone={tone as 'success' | 'warning' | 'neutral'}>{impact}</Tag>;
}

function FeatureCard({ f }: { f: Feature }) {
  const cat = categories.find((c) => c.key === f.category);
  return (
    <Card>
      <CardHeader>
        <Row gap={8} style={{ alignItems: 'center' }}>
          <span style={{ fontSize: 16 }}>{cat?.icon}</span>
          <Text weight="semibold" size="small">{f.name}</Text>
          <span style={{ marginLeft: 'auto' }} />
          <EffortBadge effort={f.effort} />
          <ImpactBadge impact={f.impact} />
        </Row>
      </CardHeader>
      <CardBody>
        <Text size="small" tone="secondary" style={{ marginBottom: 10, display: 'block' }}>{f.desc}</Text>
        <Stack gap={4}>
          {f.highlights.map((h, i) => (
            <Row key={i} gap={6} style={{ alignItems: 'flex-start' }}>
              <span style={{ color: cat?.color, fontSize: 10, marginTop: 4, flexShrink: 0 }}>●</span>
              <Text size="small">{h}</Text>
            </Row>
          ))}
        </Stack>
        {f.deps.length > 0 && (
          <Row gap={4} style={{ marginTop: 10, flexWrap: 'wrap' }}>
            <Text size="small" tone="secondary">依赖：</Text>
            {f.deps.map((d) => (
              <Pill key={d} size="small">{d}</Pill>
            ))}
          </Row>
        )}
      </CardBody>
    </Card>
  );
}

function CategorySection({ catKey }: { catKey: string }) {
  const cat = categories.find((c) => c.key === catKey)!;
  const items = features.filter((f) => f.category === catKey);
  const totalEffort = items.reduce((s, f) => s + effortWeight[f.effort], 0);
  const avgImpact = items.reduce((s, f) => s + impactScore[f.impact], 0) / items.length;

  return (
    <CollapsibleSection title={<Row gap={8} style={{ alignItems: 'center' }}><span>{cat.icon}</span><H3>{catKey}</H3></Row>} defaultOpen>
      <Row gap={12} style={{ marginBottom: 12 }}>
        <Stat value={String(items.length)} label="功能点" />
        <Stat value={String(totalEffort)} label="总工作量" />
        <Stat value={avgImpact.toFixed(1)} label="平均影响力" />
      </Row>
      <Grid columns={2} gap={12}>
        {items.map((f) => (
          <FeatureCard key={f.id} f={f} />
        ))}
      </Grid>
    </CollapsibleSection>
  );
}

function PriorityMatrix() {
  const theme = useHostTheme();

  return (
    <Card>
      <CardHeader>
        <H3>优先级矩阵（影响力 vs 工作量）</H3>
      </CardHeader>
      <CardBody>
        <svg viewBox="0 0 420 320" style={{ width: '100%', maxWidth: 520 }}>
          <line x1="50" y1="20" x2="50" y2="270" stroke={theme.tokens.colorBorder} strokeWidth="1" />
          <line x1="50" y1="270" x2="400" y2="270" stroke={theme.tokens.colorBorder} strokeWidth="1" />
          <line x1="225" y1="20" x2="225" y2="270" stroke={theme.tokens.colorBorder} strokeWidth="0.5" strokeDasharray="4" />
          <line x1="50" y1="145" x2="400" y2="145" stroke={theme.tokens.colorBorder} strokeWidth="0.5" strokeDasharray="4" />
          <text x="225" y="15" textAnchor="middle" fontSize="10" fill={theme.tokens.colorTextSecondary}>影响力 ↑</text>
          <text x="405" y="270" textAnchor="start" fontSize="10" fill={theme.tokens.colorTextSecondary}>工作量 →</text>
          <text x="135" y="75" textAnchor="middle" fontSize="11" fill="#10b981" opacity="0.7" fontWeight="600">快速胜利 ★</text>
          <text x="315" y="75" textAnchor="middle" fontSize="11" fill={theme.tokens.colorWarning} opacity="0.6">重大投入</text>
          <text x="135" y="215" textAnchor="middle" fontSize="11" fill={theme.tokens.colorTextSecondary} opacity="0.4">低优先级</text>
          <text x="315" y="215" textAnchor="middle" fontSize="11" fill={theme.tokens.colorTextSecondary} opacity="0.4">谨慎考虑</text>
          {features.map((f) => {
            const cat = categories.find((c) => c.key === f.category);
            const x = 50 + (effortWeight[f.effort] / 3) * 350;
            const y = 270 - (impactScore[f.impact] / 3) * 250;
            return (
              <g key={f.id}>
                <circle cx={x} cy={y} r="10" fill={cat?.color} opacity="0.85" />
                <text x={x} y={y + 3.5} textAnchor="middle" fontSize="7" fill="white" fontWeight="bold">{f.id}</text>
              </g>
            );
          })}
        </svg>
        <Row gap={12} style={{ marginTop: 8, flexWrap: 'wrap', justifyContent: 'center' }}>
          {categories.map((c) => (
            <Row key={c.key} gap={4} style={{ alignItems: 'center' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: c.color, display: 'inline-block' }} />
              <Text size="small" tone="secondary">{c.key}</Text>
            </Row>
          ))}
        </Row>
      </CardBody>
    </Card>
  );
}

function RoadmapTimeline() {
  const quickWins = features
    .filter((f) => f.impact === '高' && f.effort === 'S')
    .sort((a, b) => effortWeight[a.effort] - effortWeight[b.effort]);
  const mediumWins = features
    .filter((f) => (f.impact === '高' && f.effort === 'M') || (f.impact === '中' && f.effort === 'S'))
    .sort((a, b) => impactScore[b.impact] - impactScore[a.impact]);
  const longTerm = features
    .filter((f) => !quickWins.includes(f) && !mediumWins.includes(f));

  const phases = [
    { name: 'Phase 1 · 快速胜利', items: quickWins, color: '#10b981' },
    { name: 'Phase 2 · 核心增强', items: mediumWins, color: '#6366f1' },
    { name: 'Phase 3 · 长期投入', items: longTerm, color: '#8b5cf6' },
  ];

  return (
    <Card>
      <CardHeader>
        <H3>建议实施路线</H3>
      </CardHeader>
      <CardBody>
        <Stack gap={16}>
          {phases.map((phase, pi) => (
            <Stack key={pi} gap={8}>
              <Row gap={8} style={{ alignItems: 'center' }}>
                <span style={{
                  width: 24, height: 24, borderRadius: '50%', background: phase.color,
                  color: 'white', display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: 12, fontWeight: 600, flexShrink: 0,
                }}>
                  {pi + 1}
                </span>
                <Text weight="semibold">{phase.name}</Text>
                <Tag tone="neutral">{phase.items.length} 项</Tag>
              </Row>
              <div style={{ marginLeft: 32, borderLeft: `2px solid ${phase.color}20`, paddingLeft: 16 }}>
                <Stack gap={4}>
                  {phase.items.map((f) => (
                    <Row key={f.id} gap={8} style={{ alignItems: 'center' }}>
                      <Text size="small" tone="secondary" style={{ fontFamily: 'monospace', minWidth: 24 }}>{f.id}</Text>
                      <Text size="small" weight="medium">{f.name}</Text>
                      <span style={{ marginLeft: 'auto' }} />
                      <EffortBadge effort={f.effort} />
                      <ImpactBadge impact={f.impact} />
                    </Row>
                  ))}
                </Stack>
              </div>
            </Stack>
          ))}
        </Stack>
      </CardBody>
    </Card>
  );
}

export default function InnovationRoadmap() {
  const totalEffort = features.reduce((s, f) => s + effortWeight[f.effort], 0);
  const highImpact = features.filter((f) => f.impact === '高').length;
  const smallEffort = features.filter((f) => f.effort === 'S').length;

  return (
    <Stack gap={20}>
      <H1>个人助手 · 下一轮创新路线图</H1>
      <Text tone="secondary">
        基于现有 7 大模块（活动/对话/知识/记忆/笔记/推送/报告）的能力盘点，
        识别出 6 个创新方向、18 个候选功能点。按影响力 x 工作量排列建议实施优先级。
      </Text>

      <Divider />

      <Grid columns={4} gap={12}>
        <Stat value={String(features.length)} label="候选功能" />
        <Stat value={String(highImpact)} label="高影响力" tone="success" />
        <Stat value={String(smallEffort)} label="小工作量" tone="info" />
        <Stat value={String(totalEffort)} label="总工作量单位" />
      </Grid>

      <Divider />

      <PriorityMatrix />

      <Divider />

      <RoadmapTimeline />

      <Divider />

      <H2>按方向浏览</H2>
      {categories.map((c) => (
        <CategorySection key={c.key} catKey={c.key} />
      ))}

      <Divider />

      <Callout tone="info">
        <Text size="small">
          <strong>评估标准：</strong>影响力 = 对日常使用频率和效率的提升程度；
          工作量 = 开发时间（S 小于 2h, M = 2-6h, L 大于 6h）。
          所有功能均遵循现有架构约定（visible_qs 权限、agent_tool 注册、幂等 cron）。
        </Text>
      </Callout>

      <Text tone="secondary" size="small">
        生成时间：2026-09-17 · 基于项目代码库全面盘点
      </Text>
    </Stack>
  );
}
