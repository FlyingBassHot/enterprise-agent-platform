<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import MetricCard from '@/components/MetricCard.vue'
import StatusTag from '@/components/StatusTag.vue'
import { dashboardApi, type DashboardStats, type ExecutionSummary } from '@/api/dashboard'
import { formatDate, formatDuration, shortId } from '@/utils/format'

const router = useRouter()
const stats = ref<DashboardStats | null>(null)
const loading = ref(false)
const error = ref('')

const STATUS_ORDER = [
  'pending',
  'running',
  'waiting_approval',
  'completed',
  'failed',
  'cancelled',
]

const breakdown = computed(() => {
  const entries = Object.entries(stats.value?.status_breakdown || {})
  return entries.sort(([a], [b]) => {
    const ai = STATUS_ORDER.indexOf(a)
    const bi = STATUS_ORDER.indexOf(b)
    if (ai === -1 && bi === -1) return a.localeCompare(b)
    if (ai === -1) return 1
    if (bi === -1) return -1
    return ai - bi
  })
})

const successRate = computed(() => `${(stats.value?.success_rate ?? 0).toFixed(1)}%`)

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '无法加载仪表盘数据'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    stats.value = await dashboardApi.stats()
  } catch (err) {
    stats.value = null
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function excerpt(text?: string | null, length = 60): string {
  if (!text) return '-'
  return text.length > length ? `${text.slice(0, length)}…` : text
}

function openExecution(row: ExecutionSummary) {
  void router.push(`/executions/${row.id}`)
}

onMounted(load)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="仪表盘" subtitle="平台资产与执行运行概览">
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <el-skeleton v-if="loading && !stats" :rows="6" animated />

    <template v-else-if="stats">
      <div class="metrics">
        <MetricCard
          label="Agent 总数"
          :value="stats.agents_total"
          :hint="`已发布 ${stats.agents_published}`"
          accent="blue"
        />
        <MetricCard
          label="执行总数"
          :value="stats.executions_total"
          :hint="`成功率 ${successRate} · 失败 ${stats.executions_failed}`"
          accent="green"
        />
        <MetricCard
          label="Token 总量"
          :value="stats.tokens_total.toLocaleString('zh-CN')"
          :hint="`平均耗时 ${formatDuration(stats.avg_duration_ms)}`"
          accent="violet"
        />
        <MetricCard
          label="工具数量"
          :value="stats.tools_total"
          :hint="`已启用 ${stats.tools_enabled}`"
          accent="amber"
        />
      </div>

      <div class="card breakdown">
        <div class="section-head">
          <h3>执行状态分布</h3>
          <span class="muted">运行中 {{ stats.executions_running }} · 已完成 {{ stats.executions_completed }}</span>
        </div>
        <div v-if="breakdown.length" class="status-strip">
          <div v-for="[status, count] in breakdown" :key="status" class="status-item">
            <StatusTag :status="status" />
            <span class="count">{{ count }}</span>
          </div>
        </div>
        <el-empty v-else description="暂无执行记录" :image-size="60" />
      </div>

      <div class="card">
        <div class="section-head">
          <h3>最近执行</h3>
          <el-button link type="primary" @click="router.push('/executions')">查看全部</el-button>
        </div>
        <el-table
          v-if="stats.recent_executions.length"
          :data="stats.recent_executions"
          size="small"
          row-key="id"
          class="recent-table"
          @row-click="openExecution"
        >
          <el-table-column label="Agent" min-width="150">
            <template #default="{ row }">
              <span>{{ row.agent_name || shortId(row.agent_id) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="输入摘要" min-width="220">
            <template #default="{ row }">
              <span class="muted">{{ excerpt(row.input_text) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="104">
            <template #default="{ row }">
              <StatusTag :status="row.status" />
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="96">
            <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="170">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="" width="72" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openExecution(row)">查看</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无近期执行" :image-size="80" />
      </div>
    </template>

    <el-empty v-else-if="!error" description="暂无仪表盘数据" />
  </div>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 14px;
}

.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.section-head h3 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 650;
}

.status-strip {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.status-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border-radius: 10px;
  background: #f8fafc;
  border: 1px solid var(--app-border);
}

.count {
  font-size: 16px;
  font-weight: 650;
}

.recent-table {
  cursor: pointer;
}
</style>
