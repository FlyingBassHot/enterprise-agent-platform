<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { agentsApi, modelsApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type {
  AgentInfo,
  AgentValidation,
  AgentVersion,
  ModelConfig,
} from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

const router = useRouter()
const auth = useAuthStore()

const agents = ref<AgentInfo[]>([])
const models = ref<ModelConfig[]>([])
const loading = ref(false)
const error = ref('')
const statusFilter = ref('all')

const validatingId = ref('')
const publishingId = ref('')
const validation = ref<AgentValidation | null>(null)
const validationVisible = ref(false)

const versionsVisible = ref(false)
const versionsLoading = ref(false)
const versionsError = ref('')
const versionsAgent = ref<AgentInfo | null>(null)
const versions = ref<AgentVersion[]>([])
const selectedVersionId = ref('')

const canWrite = computed(() => auth.has('agents:write'))
const canPublish = computed(() => auth.has('agents:publish'))

const modelMap = computed(() => new Map(models.value.map((item) => [item.id, item])))

const filteredAgents = computed(() =>
  statusFilter.value === 'all'
    ? agents.value
    : agents.value.filter((agent) => agent.status === statusFilter.value),
)

const selectedVersion = computed(
  () => versions.value.find((item) => item.id === selectedVersionId.value) || null,
)

const statusOptions = computed(() => [
  { value: 'all', label: '全部', count: agents.value.length },
  {
    value: 'draft',
    label: '草稿',
    count: agents.value.filter((item) => item.status === 'draft').length,
  },
  {
    value: 'published',
    label: '已发布',
    count: agents.value.filter((item) => item.status === 'published').length,
  },
  {
    value: 'disabled',
    label: '已停用',
    count: agents.value.filter((item) => item.status === 'disabled').length,
  },
])

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function modelLabel(agent: AgentInfo): string {
  const configId = agent.draft_config.model.config_id
  const config = modelMap.value.get(configId)
  return config ? config.name : shortId(configId, 10)
}

function modelHint(agent: AgentInfo): string {
  const model = agent.draft_config.model
  return `T ${model.temperature} · ${model.max_tokens} tokens`
}

async function load() {
  loading.value = true
  error.value = ''
  const [agentResult, modelResult] = await Promise.allSettled([
    agentsApi.list(),
    modelsApi.configs(),
  ])
  if (agentResult.status === 'fulfilled') {
    agents.value = agentResult.value
  } else {
    agents.value = []
    error.value = describeError(agentResult.reason)
  }
  if (modelResult.status === 'fulfilled') {
    models.value = modelResult.value
  }
  loading.value = false
}

function goCreate() {
  void router.push('/agents/new')
}

function goEdit(agent: AgentInfo) {
  void router.push(`/agents/${agent.id}/edit`)
}

async function validateAgent(agent: AgentInfo) {
  validatingId.value = agent.id
  try {
    validation.value = await agentsApi.validate(agent.id)
    validationVisible.value = true
  } catch {
    // interceptor surfaces the backend message
  } finally {
    validatingId.value = ''
  }
}

async function publishAgent(agent: AgentInfo) {
  publishingId.value = agent.id
  try {
    let result: AgentValidation
    try {
      result = await agentsApi.validate(agent.id)
    } catch {
      return
    }
    if (!result.valid) {
      validation.value = result
      validationVisible.value = true
      return
    }
    if (result.warnings.length) {
      try {
        await ElMessageBox.confirm(result.warnings.join('\n'), '发布前提示', {
          confirmButtonText: '仍然发布',
          cancelButtonText: '取消',
          type: 'warning',
        })
      } catch {
        return
      }
    }
    try {
      await agentsApi.publish(agent.id)
      ElMessage.success('Agent 已发布')
      await load()
    } catch {
      // interceptor surfaces the backend message
    }
  } finally {
    publishingId.value = ''
  }
}

async function disableAgent(agent: AgentInfo) {
  try {
    await ElMessageBox.confirm(`确定停用 Agent「${agent.name}」吗？`, '停用确认', {
      confirmButtonText: '停用',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await agentsApi.disable(agent.id)
    ElMessage.success('Agent 已停用')
    await load()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function openVersions(agent: AgentInfo) {
  versionsAgent.value = agent
  versionsVisible.value = true
  versionsLoading.value = true
  versionsError.value = ''
  versions.value = []
  selectedVersionId.value = ''
  try {
    versions.value = await agentsApi.versions(agent.id)
    selectedVersionId.value = versions.value[0]?.id || ''
  } catch (err) {
    versionsError.value = describeError(err)
  } finally {
    versionsLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="Agent 管理" subtitle="配置化 Agent、版本发布与生命周期管理">
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button v-if="canWrite" type="primary" :icon="Plus" @click="goCreate">
          新建 Agent
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <div class="card">
      <div class="toolbar filter-bar">
        <el-radio-group v-model="statusFilter" size="small">
          <el-radio-button
            v-for="option in statusOptions"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }} ({{ option.count }})
          </el-radio-button>
        </el-radio-group>
        <span class="muted">共 {{ filteredAgents.length }} 个 Agent</span>
      </div>

      <el-table v-loading="loading" :data="filteredAgents" row-key="id" size="small">
        <el-table-column label="名称" min-width="160">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span class="muted mono">{{ shortId(row.id, 10) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="描述" min-width="190">
          <template #default="{ row }">
            <span class="muted desc">{{ row.description || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusTag :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="当前版本" width="96">
          <template #default="{ row }">
            <span v-if="row.current_version" class="mono">v{{ row.current_version }}</span>
            <span v-else class="muted">未发布</span>
          </template>
        </el-table-column>
        <el-table-column label="模型" min-width="150">
          <template #default="{ row }">
            <div class="name-cell">
              <span>{{ modelLabel(row) }}</span>
              <span class="muted model-hint">{{ modelHint(row) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="模型配置 ID" width="130">
          <template #default="{ row }">
            <span class="mono muted">{{ shortId(row.draft_config.model.config_id, 12) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="168">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="248" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button v-if="canWrite" link type="primary" @click="goEdit(row)">编辑</el-button>
              <el-button
                link
                :loading="validatingId === row.id"
                @click="validateAgent(row)"
              >
                校验
              </el-button>
              <el-button
                v-if="canPublish"
                link
                type="success"
                :loading="publishingId === row.id"
                @click="publishAgent(row)"
              >
                发布
              </el-button>
              <el-button
                v-if="canPublish && row.status === 'published'"
                link
                type="warning"
                @click="disableAgent(row)"
              >
                停用
              </el-button>
              <el-button link @click="openVersions(row)">版本</el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty
            :description="statusFilter === 'all' ? '暂无 Agent，点击右上角新建' : '当前筛选条件下没有 Agent'"
            :image-size="80"
          />
        </template>
      </el-table>
    </div>

    <el-dialog v-model="validationVisible" title="校验结果" width="560px">
      <template v-if="validation">
        <div class="validation-head">
          <StatusTag :status="validation.valid ? 'success' : 'failed'" />
          <span class="muted">
            {{ validation.valid ? '配置校验通过' : '配置存在错误，请修复后重试' }}
          </span>
        </div>
        <el-alert
          v-if="validation.errors.length"
          type="error"
          :closable="false"
          title="错误"
          class="validation-alert"
        >
          <ul class="check-list">
            <li v-for="item in validation.errors" :key="item">{{ item }}</li>
          </ul>
        </el-alert>
        <el-alert
          v-if="validation.warnings.length"
          type="warning"
          :closable="false"
          title="警告"
          class="validation-alert"
        >
          <ul class="check-list">
            <li v-for="item in validation.warnings" :key="item">{{ item }}</li>
          </ul>
        </el-alert>
        <el-empty
          v-if="!validation.errors.length && !validation.warnings.length"
          description="无错误或警告"
          :image-size="50"
        />
      </template>
      <template #footer>
        <el-button @click="validationVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="versionsVisible" title="版本记录" size="560px">
      <div v-if="versionsAgent" class="drawer-head">
        <div>
          <div class="drawer-title">{{ versionsAgent.name }}</div>
          <div class="muted mono">{{ versionsAgent.id }}</div>
        </div>
        <StatusTag :status="versionsAgent.status" />
      </div>

      <el-alert
        v-if="versionsError"
        type="error"
        :closable="false"
        show-icon
        :title="versionsError"
        class="validation-alert"
      />

      <el-skeleton v-if="versionsLoading" :rows="6" animated />
      <template v-else-if="versions.length">
        <el-select v-model="selectedVersionId" placeholder="选择版本" class="version-select">
          <el-option
            v-for="version in versions"
            :key="version.id"
            :label="`v${version.version} · ${formatDate(version.published_at || version.created_at)}`"
            :value="version.id"
          />
        </el-select>
        <template v-if="selectedVersion">
          <div class="version-meta">
            <div><span class="muted">状态</span><StatusTag :status="selectedVersion.status" /></div>
            <div>
              <span class="muted">发布时间</span>
              <span>{{ formatDate(selectedVersion.published_at || selectedVersion.created_at) }}</span>
            </div>
            <div>
              <span class="muted">发布人</span>
              <span class="mono">{{ shortId(selectedVersion.published_by, 12) }}</span>
            </div>
          </div>
          <div class="json-title">快照</div>
          <JsonBlock :value="selectedVersion.snapshot" max-height="420px" />
        </template>
      </template>
      <el-empty v-else-if="!versionsError" description="暂无发布版本" :image-size="80" />
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

.filter-bar {
  margin-bottom: 14px;
}

.name-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.35;
}

.name {
  font-weight: 560;
}

.model-hint {
  font-size: 11px;
}

.desc {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 12px;
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
}

.validation-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.validation-alert {
  margin-bottom: 10px;
}

.check-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}

.drawer-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 14px;
}

.drawer-title {
  font-size: 16px;
  font-weight: 650;
  margin-bottom: 4px;
}

.version-select {
  width: 100%;
  margin-bottom: 14px;
}

.version-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 18px;
  margin-bottom: 14px;
  font-size: 12px;
}

.version-meta > div {
  display: flex;
  align-items: center;
  gap: 6px;
}

.json-title {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 8px;
}
</style>
