<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChatDotRound, Refresh, Search } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import { workspaceApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { WorkspaceAgentInfo } from '@/types/api'
import { formatDate } from '@/utils/format'

const router = useRouter()
const auth = useAuthStore()

const agents = ref<WorkspaceAgentInfo[]>([])
const loading = ref(false)
const error = ref('')
const forbidden = ref(false)
const keyword = ref('')

type SortMode = 'recent' | 'published' | 'name'
const RECENT_KEY = 'workspace_recent_agents'
const sortMode = ref<SortMode>('recent')
const recentIds = ref<string[]>(readRecent())

function readRecent(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]')
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string')
      : []
  } catch {
    return []
  }
}

function remember(agentId: string) {
  recentIds.value = [agentId, ...recentIds.value.filter((item) => item !== agentId)].slice(0, 20)
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(recentIds.value))
  } catch {
    // Recent ordering is best-effort when storage is unavailable.
  }
}

function recentRank(agentId: string): number {
  const index = recentIds.value.indexOf(agentId)
  return index < 0 ? Number.MAX_SAFE_INTEGER : index
}

const filtered = computed(() => {
  const query = keyword.value.trim().toLowerCase()
  const list = query
    ? agents.value.filter(
        (agent) =>
          agent.name.toLowerCase().includes(query) ||
          (agent.description || '').toLowerCase().includes(query),
      )
    : [...agents.value]
  return list.sort((a, b) => {
    if (sortMode.value === 'name') return a.name.localeCompare(b.name, 'zh-CN')
    if (sortMode.value === 'published') {
      const left = a.published_at ? Date.parse(a.published_at) : 0
      const right = b.published_at ? Date.parse(b.published_at) : 0
      return right - left
    }
    const rank = recentRank(a.id) - recentRank(b.id)
    if (rank !== 0) return rank
    const left = a.published_at ? Date.parse(a.published_at) : 0
    const right = b.published_at ? Date.parse(b.published_at) : 0
    return right - left
  })
})

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { status?: number; data?: { message?: string } } })
      .response
    if (response?.status === 403) return '当前账号无权访问 Agent 工作中心'
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function isForbidden(value: unknown): boolean {
  if (typeof value !== 'object' || value === null || !('response' in value)) return false
  const status = (value as { response?: { status?: number } }).response?.status
  return status === 401 || status === 403
}

async function load() {
  loading.value = true
  error.value = ''
  forbidden.value = false
  try {
    const list = await workspaceApi.agents()
    agents.value = list.filter((agent) => agent.status === 'published')
  } catch (err) {
    agents.value = []
    forbidden.value = isForbidden(err)
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function enter(agent: WorkspaceAgentInfo) {
  remember(agent.id)
  void router.push(`/workspaces/${agent.id}`)
}

function goAgentManagement() {
  void router.push('/agents')
}

onMounted(load)
</script>

<template>
  <div class="workspace-select">
    <PageHeader
      title="Agent 工作中心"
      subtitle="先选择一个已发布 Agent，再进入其专属工作区。会话、执行与文件访问均限定在该 Agent 的授权范围内。"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
      </template>
    </PageHeader>

    <div class="toolbar">
      <el-input
        v-model="keyword"
        class="search"
        clearable
        placeholder="搜索 Agent 名称或描述"
        :prefix-icon="Search"
      />
      <div class="toolbar-right">
        <el-radio-group v-model="sortMode" size="small">
          <el-radio-button value="recent">最近使用</el-radio-button>
          <el-radio-button value="published">最近发布</el-radio-button>
          <el-radio-button value="name">名称</el-radio-button>
        </el-radio-group>
        <span class="count">共 {{ filtered.length }} 个可用 Agent</span>
      </div>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
      class="error-alert"
    >
      <el-button size="small" type="primary" plain @click="load">重新加载</el-button>
    </el-alert>

    <div v-if="loading && !agents.length" class="agent-grid">
      <div v-for="index in 3" :key="index" class="agent-card skeleton-card">
        <el-skeleton :rows="3" animated />
      </div>
    </div>

    <el-empty
      v-else-if="forbidden"
      description="当前账号无权访问 Agent 工作中心，请联系管理员分配执行权限"
    />

    <el-empty v-else-if="error && !agents.length" description="Agent 列表加载失败，请稍后重试">
      <el-button type="primary" @click="load">重新加载</el-button>
    </el-empty>

    <el-empty v-else-if="!agents.length" description="暂无已发布的 Agent，无法进入工作区">
      <el-button v-if="auth.has('agents:read')" type="primary" @click="goAgentManagement">
        前往 Agent 管理
      </el-button>
    </el-empty>

    <el-empty
      v-else-if="!filtered.length"
      description="没有匹配的 Agent，请调整搜索关键词"
    />

    <div v-else class="agent-grid">
      <article
        v-for="agent in filtered"
        :key="agent.id"
        class="agent-card"
        tabindex="0"
        @click="enter(agent)"
        @keydown.enter="enter(agent)"
      >
        <div class="card-top">
          <div class="card-icon">
            <el-icon><ChatDotRound /></el-icon>
          </div>
          <StatusTag :status="agent.status" />
        </div>
        <div class="card-name">{{ agent.name }}</div>
        <div class="card-desc">{{ agent.description || '暂无描述' }}</div>
        <div class="card-meta">
          <span>v{{ agent.current_version ?? '-' }}</span>
          <span>发布于 {{ formatDate(agent.published_at) }}</span>
        </div>
        <div class="card-actions">
          <el-button type="primary" plain @click.stop="enter(agent)">进入工作区</el-button>
        </div>
      </article>
    </div>
  </div>
</template>

<style scoped>
.workspace-select {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.search {
  max-width: 320px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.count {
  font-size: 12px;
  color: var(--app-muted);
}

.error-alert {
  margin-bottom: 4px;
}

.agent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
}

.agent-card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  cursor: pointer;
  transition:
    border-color 0.14s ease,
    box-shadow 0.14s ease,
    transform 0.14s ease;
  box-shadow: 0 1px 2px rgba(23, 32, 51, 0.03);
}

.agent-card:hover,
.agent-card:focus-visible {
  border-color: rgba(47, 107, 255, 0.4);
  box-shadow: 0 8px 24px rgba(47, 107, 255, 0.1);
  transform: translateY(-1px);
  outline: none;
}

.skeleton-card {
  cursor: default;
}

.skeleton-card:hover {
  border-color: var(--app-border);
  box-shadow: 0 1px 2px rgba(23, 32, 51, 0.03);
  transform: none;
}

.card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.card-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: rgba(47, 107, 255, 0.1);
  color: #2f6bff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
}

.card-name {
  font-size: 16px;
  font-weight: 660;
  letter-spacing: -0.1px;
}

.card-desc {
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--app-muted);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  min-height: 40px;
}

.card-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 11px;
  color: var(--app-muted);
  border-top: 1px dashed var(--app-border);
  padding-top: 10px;
}

.card-actions {
  display: flex;
  justify-content: flex-end;
}
</style>
