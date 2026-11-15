<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowLeft, ArrowRight, Refresh, Search } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { auditLogsApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { AuditLogInfo } from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

const auth = useAuthStore()

const PAGE_SIZE = 100

const logs = ref<AuditLogInfo[]>([])
const loading = ref(false)
const error = ref('')
const offset = ref(0)

const actionFilter = ref('')
const actorFilter = ref('')
const resourceTypeFilter = ref('')
const resultFilter = ref('')
const dateRange = ref<[Date, Date] | null>(null)

const detailVisible = ref(false)
const detailLog = ref<AuditLogInfo | null>(null)

const canRead = computed(
  () => Boolean(auth.user?.is_superuser) || auth.has('audit:logs:read'),
)

const currentPage = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
const hasNextPage = computed(() => logs.value.length === PAGE_SIZE)

const resultOptions = [
  { value: 'success', label: '成功' },
  { value: 'failure', label: '失败' },
  { value: 'denied', label: '拒绝' },
]

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function resultMeta(result: string): {
  label: string
  type: 'success' | 'danger' | 'warning' | 'info'
} {
  if (result === 'success') return { label: '成功', type: 'success' }
  if (result === 'failure') return { label: '失败', type: 'danger' }
  if (result === 'denied') return { label: '拒绝', type: 'warning' }
  return { label: result || '-', type: 'info' }
}

async function loadLogs() {
  if (!canRead.value) return
  loading.value = true
  error.value = ''
  const params: {
    action?: string
    actor_id?: string
    resource_type?: string
    result?: string
    after?: string
    before?: string
    limit?: number
    offset?: number
  } = {
    limit: PAGE_SIZE,
    offset: offset.value,
  }
  const action = actionFilter.value.trim()
  const actorId = actorFilter.value.trim()
  const resourceType = resourceTypeFilter.value.trim()
  if (action) params.action = action
  if (actorId) params.actor_id = actorId
  if (resourceType) params.resource_type = resourceType
  if (resultFilter.value) params.result = resultFilter.value
  const range = dateRange.value
  if (range && range.length === 2) {
    params.after = range[0].toISOString()
    params.before = range[1].toISOString()
  }

  try {
    logs.value = await auditLogsApi.list(params)
  } catch (err) {
    logs.value = []
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function applyFilters() {
  offset.value = 0
  void loadLogs()
}

function resetFilters() {
  actionFilter.value = ''
  actorFilter.value = ''
  resourceTypeFilter.value = ''
  resultFilter.value = ''
  dateRange.value = null
  offset.value = 0
  void loadLogs()
}

function goPrevPage() {
  if (offset.value === 0) return
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
  void loadLogs()
}

function goNextPage() {
  if (!hasNextPage.value) return
  offset.value += PAGE_SIZE
  void loadLogs()
}

function openDetail(log: AuditLogInfo) {
  detailLog.value = log
  detailVisible.value = true
}

onMounted(() => {
  if (canRead.value) {
    void loadLogs()
  }
})
</script>

<template>
  <div class="page-stack">
    <PageHeader title="审计日志" subtitle="平台操作留痕：按操作、主体、资源与结果检索（只读）">
      <template #actions>
        <el-button v-if="canRead" :loading="loading" @click="loadLogs">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="!canRead"
      type="warning"
      :closable="false"
      show-icon
      title="当前账号没有审计日志查看权限（audit:logs:read）。"
    />

    <template v-else>
      <div class="card filter-card">
        <el-form label-position="top" @submit.prevent="applyFilters">
          <div class="filter-grid">
          <el-form-item label="操作（精确匹配）">
            <el-input
              v-model="actionFilter"
              size="small"
              clearable
              placeholder="如 agents.publish"
              @keyup.enter="applyFilters"
            />
          </el-form-item>
          <el-form-item label="操作者 ID">
            <el-input
              v-model="actorFilter"
              size="small"
              clearable
              placeholder="actor_id"
              @keyup.enter="applyFilters"
            />
          </el-form-item>
          <el-form-item label="资源类型">
            <el-input
              v-model="resourceTypeFilter"
              size="small"
              clearable
              placeholder="如 agent / tool"
              @keyup.enter="applyFilters"
            />
          </el-form-item>
          <el-form-item label="结果">
            <el-select v-model="resultFilter" size="small" clearable placeholder="全部结果">
              <el-option
                v-for="option in resultOptions"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="时间范围" class="range-item">
            <el-date-picker
              v-model="dateRange"
              type="datetimerange"
              size="small"
              start-placeholder="开始时间"
              end-placeholder="结束时间"
              range-separator="至"
            />
          </el-form-item>
          <div class="filter-actions">
            <el-button type="primary" size="small" :icon="Search" @click="applyFilters">
              查询
            </el-button>
            <el-button size="small" @click="resetFilters">重置</el-button>
          </div>
          </div>
        </el-form>
      </div>

      <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
        <el-button link type="primary" @click="loadLogs">重试</el-button>
      </el-alert>

      <div class="card">
        <el-table
          v-loading="loading"
          :data="logs"
          row-key="id"
          size="small"
          class="audit-table"
          @row-click="openDetail"
        >
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatDate(row.occurred_at) }}</template>
          </el-table-column>
          <el-table-column label="操作者" min-width="150">
            <template #default="{ row }">
              <div class="name-cell">
                <span>{{ row.actor_username || '匿名' }}</span>
                <span class="muted mono">{{ shortId(row.actor_id, 12) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="动作" min-width="170">
            <template #default="{ row }">
              <span class="mono action">{{ row.action }}</span>
            </template>
          </el-table-column>
          <el-table-column label="资源" min-width="180">
            <template #default="{ row }">
              <div class="name-cell">
                <span>{{ row.resource_type || '-' }}</span>
                <span class="muted mono">{{ shortId(row.resource_id, 14) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="88">
            <template #default="{ row }">
              <el-tag size="small" effect="light" :type="resultMeta(row.result).type">
                {{ resultMeta(row.result).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态码" width="84">
            <template #default="{ row }">
              <span :class="row.status_code && row.status_code >= 400 ? 'code-error' : 'mono'">
                {{ row.status_code ?? '-' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="IP" width="128">
            <template #default="{ row }">
              <span class="mono muted">{{ row.ip_address || '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="Request ID" width="150">
            <template #default="{ row }">
              <span class="mono muted" :title="row.request_id || ''">
                {{ shortId(row.request_id, 16) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="" width="70" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openDetail(row)">详情</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="没有符合条件的审计日志" :image-size="80" />
          </template>
        </el-table>

        <div class="pager">
          <span class="muted">每页 {{ PAGE_SIZE }} 条 · 第 {{ currentPage }} 页</span>
          <div class="pager-actions">
            <el-button
              size="small"
              :icon="ArrowLeft"
              :disabled="offset === 0 || loading"
              @click="goPrevPage"
            >
              上一页
            </el-button>
            <el-button
              size="small"
              :disabled="!hasNextPage || loading"
              @click="goNextPage"
            >
              下一页
              <el-icon class="el-icon--right"><ArrowRight /></el-icon>
            </el-button>
          </div>
        </div>
      </div>
    </template>

    <el-drawer v-model="detailVisible" size="640px" title="审计日志详情">
      <template v-if="detailLog">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="日志 ID">
            <span class="mono">{{ detailLog.id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="发生时间">
            {{ formatDate(detailLog.occurred_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="操作者">
            <span>{{ detailLog.actor_username || '匿名' }}</span>
            <span class="mono muted detail-extra">{{ detailLog.actor_id || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="动作">
            <span class="mono">{{ detailLog.action }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="资源类型">
            <span>{{ detailLog.resource_type || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="资源 ID">
            <span class="mono">{{ detailLog.resource_id || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="结果">
            <el-tag size="small" effect="light" :type="resultMeta(detailLog.result).type">
              {{ resultMeta(detailLog.result).label }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="状态码">
            <span class="mono">{{ detailLog.status_code ?? '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="IP 地址">
            <span class="mono">{{ detailLog.ip_address || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="Request ID">
            <span class="mono">{{ detailLog.request_id || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="租户">
            <span class="mono">{{ detailLog.tenant_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="写入时间">
            {{ formatDate(detailLog.created_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="details-block">
          <div class="block-label">Details</div>
          <JsonBlock :value="detailLog.details" max-height="420px" />
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.filter-card {
  padding-bottom: 4px;
}

.filter-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 0 14px;
  align-items: end;
}

.filter-grid :deep(.el-form-item) {
  margin-bottom: 14px;
}

.range-item {
  grid-column: span 2;
}

@media (max-width: 980px) {
  .range-item {
    grid-column: span 1;
  }
}

.filter-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;
}

.audit-table {
  cursor: pointer;
}

.name-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.35;
}

.action {
  font-weight: 560;
  word-break: break-all;
}

.code-error {
  color: var(--el-color-danger);
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
  flex-wrap: wrap;
}

.pager-actions {
  display: flex;
  gap: 8px;
}

.details-block {
  margin-top: 16px;
}

.block-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--app-muted);
  margin-bottom: 6px;
}

.detail-extra {
  margin-left: 8px;
  font-size: 11px;
}
</style>
