import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      component: () => import('@/layouts/MainLayout.vue'),
      redirect: '/dashboard',
      children: [
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/dashboard/DashboardView.vue'),
          meta: { title: '仪表盘', permission: 'dashboard:read' },
        },
        {
          path: 'agents',
          name: 'agents',
          component: () => import('@/views/agent/AgentListView.vue'),
          meta: { title: 'Agent 管理', permission: 'agents:read' },
        },
        {
          path: 'agents/new',
          name: 'agent-new',
          component: () => import('@/views/agent/AgentEditorView.vue'),
          meta: { title: '新建 Agent', permission: 'agents:write' },
        },
        {
          path: 'agents/:id/edit',
          name: 'agent-edit',
          component: () => import('@/views/agent/AgentEditorView.vue'),
          meta: { title: '编辑 Agent', permission: 'agents:write' },
        },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: () => import('@/views/knowledge/KnowledgeView.vue'),
          meta: { title: '知识库', permission: 'knowledge:read' },
        },
        {
          path: 'skills',
          name: 'skills',
          component: () => import('@/views/skill/SkillListView.vue'),
          meta: { title: 'Skill 管理', permission: 'skills:read' },
        },
        {
          path: 'workflows',
          name: 'workflows',
          component: () => import('@/views/workflow/WorkflowListView.vue'),
          meta: { title: '工作流', permission: 'workflows:read' },
        },
        {
          path: 'prompts',
          name: 'prompts',
          component: () => import('@/views/prompt/PromptListView.vue'),
          meta: { title: 'Prompt 管理', permission: 'prompts:read' },
        },
        {
          path: 'models',
          name: 'models',
          component: () => import('@/views/model/ModelView.vue'),
          meta: { title: '模型管理', permission: 'models:read' },
        },
        {
          path: 'tools',
          name: 'tools',
          component: () => import('@/views/tool/ToolListView.vue'),
          meta: { title: '工具管理', permission: 'tools:read' },
        },
        {
          path: 'workspace',
          name: 'workspace',
          component: () => import('@/views/chat/WorkspaceView.vue'),
          meta: { title: 'Agent 工作台', permission: 'executions:write' },
        },
        {
          path: 'executions',
          name: 'executions',
          component: () => import('@/views/executions/ExecutionListView.vue'),
          meta: { title: '执行中心', permission: 'executions:read' },
        },
        {
          path: 'executions/:id',
          name: 'execution-detail',
          component: () => import('@/views/executions/ExecutionDetailView.vue'),
          meta: { title: '执行详情', permission: 'executions:read' },
        },
        {
          path: 'audit-logs',
          name: 'audit-logs',
          component: () => import('@/views/settings/AuditLogsView.vue'),
          meta: { title: '审计日志', permission: 'audit:logs:read' },
        },
        {
          path: 'users',
          name: 'users',
          component: () => import('@/views/users/AdminView.vue'),
          meta: { title: '用户与角色', permission: 'users:manage' },
        },
        {
          path: 'audit',
          name: 'audit',
          component: () => import('@/views/audit/AuditDemoView.vue'),
          meta: { title: '审计演示', permission: 'audit:read' },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { public: true, title: '未找到' },
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.meta.public) {
    if (to.name === 'login' && auth.isAuthenticated) return { name: 'dashboard' }
    return true
  }
  if (!auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (!auth.user) {
    await auth.fetchMe()
  }
  if (!auth.user) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  const permission = to.meta.permission as string | undefined
  if (permission && !auth.has(permission)) {
    return { name: 'dashboard' }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · Agent Platform` : 'Agent Platform'
})

export default router
