import { Callout, Divider, Grid, H1, H2, Stack, Stat, Table, Text } from 'qoder/canvas';

export default function ChatSplitLayoutReport() {
  return (
    <Stack gap={24}>
      <H1>Chat 页面响应式双模式重构 — 完成报告</H1>

      <Grid columns={4} gap={16}>
        <Stat value="10" label="变更文件" />
        <Stat value="503" label="测试通过" tone="success" />
        <Stat value="1" label="关键 Bug 修复" tone="warning" />
        <Stat value="✓" label="已部署验证" tone="success" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Text>
        将 /chat/ 页面从传统「列表 → 跳转详情」模式重构为响应式分栏布局：桌面端（≥768px）左右两栏同屏常驻，左栏固定 300px 对话列表 + 右栏聊天窗口；移动端（&lt;768px）IM 式单栏交互，列表视图与聊天视图通过 data-view 属性切换。全程保留 PaChatTurn 异步轮询、浮窗 API、权限隔离等既有能力。
      </Text>
      <Callout tone="success">
        修复了空状态首次点击对话项右栏不切换的关键 Bug — 根因是聊天骨架 DOM 元素被包在条件分支内，空状态时不存在导致 JS 静默跳过。已通过始终渲染骨架 + JS 动态更新 form action 修复。
      </Callout>

      <Divider />

      <H2>关键步骤</H2>
      <Table
        headers={['阶段', '内容']}
        rows={[
          ['后端', 'views.py 增加可选 conversation_id 参数，同时加载 active_conversation + chat_messages'],
          ['路由', 'urls.py 新增 conversation_list_with_active 路由；detail 移至 <id>/detail/ 避免冲突'],
          ['模板重写', 'conversation_list.html 完全重写为分栏容器 + 内联 PaChatSplit JS 控制器'],
          ['CSS', 'custom.css 添加 .chat-layout / .chat-sidebar / .chat-main 响应式规则'],
          ['URL 同步', 'base.html 浮窗 fullLink、dashboard、search_panel、detail 返回链接统一更新'],
          ['测试修复', 'ConversationListDesktopLayoutTest 重写；core/tests.py 分类调整'],
          ['Bug 发现', '浏览器验证发现空状态首次点击右栏不切换（桌面 + 移动端均中招）'],
          ['Bug 修复', '聊天骨架移出条件分支始终渲染；JS selectConversation 动态更新所有 form action'],
          ['部署验证', 'rsync → restart gunicorn → curl 200 → 浏览器实测确认 Bug 已修复'],
        ]}
      />

      <Divider />

      <H2>变更文件清单</H2>
      <Table
        headers={['文件', '操作', '说明']}
        rows={[
          ['chat/views.py', '修改', 'conversation_list 增加可选 conversation_id'],
          ['chat/urls.py', '修改', '新增路由 + detail 路径变更'],
          ['templates/chat/conversation_list.html', '完全重写', '分栏布局 + 内联 JS 控制器'],
          ['templates/chat/conversation_detail.html', '修改', '返回链接 + JS base URL 更新'],
          ['templates/base.html', '修改', '浮窗 fullLink → /detail/'],
          ['templates/core/dashboard.html', '修改', '对话链接 → conversation_list_with_active'],
          ['templates/core/_search_panel.html', '修改', '搜索结果对话链接更新'],
          ['static/css/custom.css', '修改', '新增 ~70 行分栏布局 CSS'],
          ['chat/tests.py', '修改', '桌面布局测试重写'],
          ['core/tests.py', '修改', 'conversation_list 分类调整'],
        ]}
      />

      <Divider />

      <H2>验证依据</H2>
      <Stack gap={12}>
        <Callout tone="info">
          <Text weight="bold">自动化测试</Text>
          <Text>503 项 Django 测试全绿，无回归。</Text>
        </Callout>
        <Callout tone="info">
          <Text weight="bold">生产站点浏览器验证（ravenclaw.top）</Text>
          <Text>
            1. /chat/ 空状态页：左栏对话列表 + 右栏「选择一个对话开始」引导正确渲染{'\n'}
            2. 点击对话项：URL 变为 /chat/15/，右栏标题更新为「新对话」，操作按钮出现，消息区显示「发送消息开始对话」{'\n'}
            3. /chat/15/detail/ 无 JS 降级页正常渲染，含「← 返回对话列表」链接{'\n'}
            4. base.html 浮窗 fullLink 已更新为 /detail/ 路径{'\n'}
            5. gunicorn active + curl 登录页 HTTP 200
          </Text>
        </Callout>
      </Stack>

      <Divider />

      <H2>Spec 合规审计</H2>
      <Table
        headers={['Spec 章节', '要求', '状态']}
        rows={[
          ['一、后端改动', 'conversation_list 支持可选 ID + context 传递', '✅ 通过'],
          ['一、后端改动', '新增路由 + detail 路径变更', '✅ 通过'],
          ['二、模板改动', '分栏容器 + 左栏 + 右栏结构', '✅ 通过'],
          ['二、模板改动', '标题截断 + hover tooltip + 选中高亮', '✅ 通过'],
          ['二、模板改动', '移动端返回按钮 + partials 复用', '✅ 通过'],
          ['二、模板改动', 'detail 保留 + 返回链接', '✅ 通过'],
          ['二、模板改动', '浮窗 fullLink 更新', '✅ 通过'],
          ['三、CSS', '响应式 flexbox + data-view 切换 + 桌面端 300px 左栏', '✅ 通过'],
          ['四、JS', 'PaChatSplit 控制器 + halt/resume + popstate 监听', '✅ 通过'],
          ['五、测试', '503 项全量测试无回归', '✅ 通过'],
          ['六、部署', 'rsync + restart + curl + 浏览器验证', '✅ 通过'],
        ]}
      />

      <Divider />

      <H2>关键结论</H2>
      <Text>
        Spec 中全部 6 大类要求逐项验证通过。本次重构中发现的最关键问题是空状态首次点击 Bug：聊天骨架 DOM 元素原在条件分支内，空状态时不存在导致 JS 选择器返回 null 后静默跳过，移动端更严重（data-view 隐藏列表但右栏无返回按钮，用户卡死）。修复方案为始终渲染骨架 + JS 动态填充，已在生产环境验证通过。
      </Text>
    </Stack>
  );
}
