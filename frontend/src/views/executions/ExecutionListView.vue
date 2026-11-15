<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, VideoPause } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import { agentsApi, executionsApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { AgentInfo, ExecutionInfo } from '@/types/api'
import { formatDate, formatDuration, shortId } from '@/utils/format'

const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'pending', label: '等待中' },
  { value: 'running', label: '执行中' },
  { value: 'waiting_approval', label: '待审批' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

const router = useRouter()
const auth = useAuthStore()

const agents = ref<AgentInfo[]>([])
const executions = ref<ExecutionInfo[]>([])
const loading = ref(false)
const error = ref('')
const statusFilter = ref('all')
const agentFilter = ref('')
const cancellingId = ref('')
const now = ref(Date.now())

let timer: number | undefined
let loadToken = 0

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function agentName(id?: string | null): string {
  if (!id) return '-'
  return agents.value.find((item) => item.id === id)?.name || shortId(id)
}

function tokenTotal(row: ExecutionInfo): number {
  const usage = row.token_usage || {}
  if (typeof usage.total_tokens === 'number') return usage.total_tokens
  return (usage.prompt_tokens || 0) + (usage.completion_tokens || 0)
}

function isRunning(row: ExecutionInfo): boolean {
  return row.status === 'running'
}

function durationMs(row: ExecutionInfo): number | null {
  if (!row.started_at) return null
  const start = new Date(row.started_at).getTime()
  if (Number.isNaN(start)) return null
  const finished = row.finished_at ? new Date(row.finished_at).getTime() : now.value
  if (Number.isNaN(finished)) return null
  return Math.max(0, finished - start)
}

async function loadAgents() {
  try {
    agents.value = await agentsApi.list()
  } catch {
    agents.value = []
  }
}

async function load() {
  const token = ++loadToken
  loading.value = true
  error.value = ''
  try {
    const executionsList = await executionsApi.list({
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      agent_id: agentFilter.value || undefined,
      limit: 100,
    })
    if (token !== loadToken) return
    executions.value = executionsList
  } catch (err) {
    if (token !== loadToken) return
    executions.value = []
    error.value = describeError(err)
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function openExecution(row: ExecutionInfo) {
  void router.push(`/executions/${row.id}`)
}

async function cancelExecution(row: ExecutionInfo) {
  try {
    await ElMessageBox.confirm(
      `确认取消执行 ${shortId(row.id, 10)}？已产生的工具结果会被保留。`,
      '取消执行',
      { type: 'warning', confirmButtonText: '取消执行', cancelButtonText: '保留' },
    )
  } catch {
    return
  }
  cancellingId.value = row.id
  try {
    await executionsApi.cancel(row.id)
    ElMessage.success('已请求取消')
    await load()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    cancellingId.value = ''
  }
}

onMounted(async () => {
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 5000)
  await Promise.all([loadAgents(), load()])
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="page-stack">
    <PageHeader title="执行中心" subtitle="查看 Agent 执行记录、运行状态与耗时">
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
      </template>
    </PageHeader>

    <div class="card">
      <div class="toolbar">
        <div class="filters">
          <el-select v-model="statusFilter" style="width: 150px" @change="load">
            <el-option
              v-for="option in STATUS_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <el-select
            v-model="agentFilter"
            clearable
            filterable
            placeholder="全部 Agent"
            style="width: 220px"
            @change="load"
          >
            <el-option
              v-for="agent in agents"
              :key="agent.id"
              :label="agent.name"
              :value="agent.id"
            />
          </el-select>
          <span class="muted">共 {{ executions.length }} 条记录</span>
        </div>
      </div>

      <el-alert
        v-if="error"
        type="error"
        :closable="false"
        show-icon
        :title="error"
        class="table-alert"
      >
        <el-button link type="primary" @click="load">重试</el-button>
      </el-alert>

      <el-table
        v-loading="loading"
        :data="executions"
        row-key="id"
        class="execution-table"
        @row-click="openExecution"
      >
        <el-table-column label="执行 ID" width="120">
          <template #default="{ row }">
            <span class="mono">{{ shortId(row.id, 10) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="Agent" min-width="150">
          <template #default="{ row }">
            <span>{{ agentName(row.agent_id) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <StatusTag :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="发起人" width="120">
          <template #default="{ row }">
            <span class="mono muted">{{ shortId(row.user_id, 8) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }">v{{ row.agent_version }}</template>
        </el-table-column>
        <el-table-column label="Tokens" width="100" align="right">
          <template #default="{ row }">{{ tokenTotal(row).toLocaleString('zh-CN') }}</template>
        </el-table-column>
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">{{ formatDuration(durationMs(row)) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" align="right">
          <template #default="{ row }">
            <el-button
              v-if="isRunning(row) && auth.has('executions:write')"
              link
              type="danger"
              :loading="cancellingId === row.id"
              @click.stop="cancelExecution(row)"
            >
              <el-icon><VideoPause /></el-icon>
              取消
            </el-button>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无执行记录" :image-size="90" />
        </template>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.table-alert {
  margin-top: 12px;
}

.execution-table {
  margin-top: 8px;
  cursor: pointer;
}

.execution-table :deep(.el-table__row) {
  cursor: pointer;
}

.mono {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}
</style>
