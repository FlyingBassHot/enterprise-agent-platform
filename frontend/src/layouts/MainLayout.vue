<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChatDotRound,
  Cpu,
  DataAnalysis,
  Document,
  FolderOpened,
  Grid,
  Monitor,
  Reading,
  Setting,
  Share,
  Tickets,
  SwitchButton,
  Tools,
  User,
  UserFilled,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const menuItems = computed(() => [
  { path: '/dashboard', label: '仪表盘', icon: Grid, permission: 'dashboard:read' },
  { path: '/workspace', label: 'Agent 工作台', icon: ChatDotRound, permission: 'executions:write' },
  { path: '/agents', label: 'Agent 管理', icon: Cpu, permission: 'agents:read' },
  { path: '/knowledge', label: '知识库', icon: FolderOpened, permission: 'knowledge:read' },
  { path: '/skills', label: 'Skill 管理', icon: Reading, permission: 'skills:read' },
  { path: '/workflows', label: '工作流', icon: Share, permission: 'workflows:read' },
  { path: '/prompts', label: 'Prompt 管理', icon: Document, permission: 'prompts:read' },
  { path: '/models', label: '模型管理', icon: Monitor, permission: 'models:read' },
  { path: '/tools', label: '工具管理', icon: Tools, permission: 'tools:read' },
  { path: '/executions', label: '执行中心', icon: DataAnalysis, permission: 'executions:read' },
  { path: '/audit', label: '审计演示', icon: Setting, permission: 'audit:read' },
  { path: '/audit-logs', label: '审计日志', icon: Tickets, permission: 'audit:logs:read' },
  { path: '/users', label: '用户与角色', icon: User, permission: 'users:manage' },
].filter((item) => auth.has(item.permission)))

const activeMenu = computed(() => {
  const path = route.path
  if (path.startsWith('/agents')) return '/agents'
  if (path.startsWith('/executions')) return '/executions'
  return path
})

async function handleCommand(command: string) {
  if (command === 'logout') {
    auth.logout()
    await router.push('/login')
  }
}

onMounted(() => {
  if (!auth.user) void auth.fetchMe()
})
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">
        <div class="brand-mark">AP</div>
        <div>
          <div class="brand-name">Agent Platform</div>
          <div class="brand-sub">Enterprise Runtime</div>
        </div>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        class="sidebar-menu"
        background-color="transparent"
        text-color="#b6c2d9"
        active-text-color="#ffffff"
      >
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
      <div class="sidebar-footer">
        <span class="status-dot" /> 本地可插拔模式
      </div>
    </el-aside>

    <el-container>
      <el-header class="topbar">
        <div>
          <div class="page-title">{{ route.meta.title }}</div>
          <div class="page-sub">Enterprise Agent Platform · V1</div>
        </div>
        <el-dropdown trigger="click" @command="handleCommand">
          <div class="user-chip">
            <el-avatar :size="30" :icon="UserFilled" />
            <div class="user-meta">
              <span>{{ auth.user?.display_name || auth.user?.username }}</span>
              <small>{{ auth.user?.roles.join(' / ') }}</small>
            </div>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">
                <el-icon><SwitchButton /></el-icon> 退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  min-height: 100vh;
  background: var(--app-bg);
}

.sidebar {
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, #101828 0%, #0b1220 100%);
  border-right: 1px solid rgba(255, 255, 255, 0.06);
  padding: 18px 12px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 10px 22px;
}

.brand-mark {
  width: 38px;
  height: 38px;
  border-radius: 11px;
  display: grid;
  place-items: center;
  color: #fff;
  font-weight: 700;
  letter-spacing: 0.5px;
  background: linear-gradient(135deg, var(--el-color-primary), #7c3aed);
}

.brand-name {
  color: #fff;
  font-weight: 650;
  font-size: 15px;
}

.brand-sub {
  color: #7d8bab;
  font-size: 11px;
  margin-top: 2px;
}

.sidebar-menu {
  border-right: 0;
  flex: 1;
}

.sidebar-menu :deep(.el-menu-item) {
  height: 42px;
  margin-bottom: 4px;
  border-radius: 9px;
}

.sidebar-menu :deep(.el-menu-item.is-active) {
  background: rgba(64, 128, 255, 0.18);
  box-shadow: inset 0 0 0 1px rgba(64, 128, 255, 0.35);
}

.sidebar-footer {
  color: #70809f;
  font-size: 11px;
  padding: 12px 10px 4px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #22c55e;
  display: inline-block;
}

.topbar {
  height: 76px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: rgba(255, 255, 255, 0.72);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--app-border);
}

.page-title {
  font-size: 18px;
  font-weight: 650;
  color: var(--app-text);
}

.page-sub {
  color: var(--app-muted);
  font-size: 12px;
  margin-top: 2px;
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  padding: 6px 10px;
  border-radius: 10px;
  transition: background 0.15s ease;
}

.user-chip:hover {
  background: rgba(15, 23, 42, 0.05);
}

.user-meta {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  font-size: 13px;
}

.user-meta small {
  color: var(--app-muted);
  font-size: 11px;
}

.content {
  padding: 24px 28px 40px;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.12s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
