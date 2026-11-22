<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FolderOpened, Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import { agentsApi, fileRootsApi } from '@/api/platform'
import type { AgentInfo, FileRootGrant, FileRootInfo, FileRootPermission } from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

interface CreateForm {
  name: string
  description: string
}

interface GrantForm {
  agent_id: string
  permission: FileRootPermission
}

const roots = ref<FileRootInfo[]>([])
const agents = ref<AgentInfo[]>([])
const loading = ref(false)
const error = ref('')

const createVisible = ref(false)
const creating = ref(false)
const createForm = reactive<CreateForm>({ name: '', description: '' })

const grantsVisible = ref(false)
const grantsRoot = ref<FileRootInfo | null>(null)
const grants = ref<FileRootGrant[]>([])
const grantsLoading = ref(false)
const grantsError = ref('')
const agentsError = ref('')
const granting = ref(false)
const grantForm = reactive<GrantForm>({ agent_id: '', permission: 'read' })

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function permissionLabel(permission: FileRootPermission): string {
  return permission === 'write' ? '读写' : '只读'
}

function permissionTagType(permission: FileRootPermission): 'warning' | 'info' {
  return permission === 'write' ? 'warning' : 'info'
}

function agentLabel(grant: FileRootGrant): string {
  return grant.agent_name || `Agent ${shortId(grant.agent_id, 8)}`
}

async function load() {
  loading.value = true
  error.value = ''
  agentsError.value = ''
  const [rootsResult, agentsResult] = await Promise.allSettled([
    fileRootsApi.list(),
    agentsApi.list(),
  ])
  if (rootsResult.status === 'fulfilled') {
    roots.value = rootsResult.value
  } else {
    roots.value = []
    error.value = describeError(rootsResult.reason)
    ElMessage.error(error.value)
  }
  if (agentsResult.status === 'fulfilled') {
    agents.value = agentsResult.value.filter((agent) => agent.status !== 'deleted')
  } else {
    agents.value = []
    agentsError.value = describeError(agentsResult.reason)
  }
  loading.value = false
}

function openCreate() {
  createForm.name = ''
  createForm.description = ''
  createVisible.value = true
}

async function submitCreate() {
  const name = createForm.name.trim()
  if (!name) {
    ElMessage.warning('请填写根目录名称')
    return
  }
  creating.value = true
  try {
    const root = await fileRootsApi.create({
      name,
      description: createForm.description.trim(),
    })
    ElMessage.success(`根目录「${root.name}」已创建`)
    createVisible.value = false
    await load()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    creating.value = false
  }
}

async function openGrants(root: FileRootInfo) {
  grantsRoot.value = root
  grants.value = []
  grantsError.value = ''
  grantForm.agent_id = ''
  grantForm.permission = 'read'
  grantsVisible.value = true
  await loadGrants()
}

async function loadGrants() {
  const root = grantsRoot.value
  if (!root) return
  grantsLoading.value = true
  grantsError.value = ''
  try {
    const result = await fileRootsApi.grants(root.id)
    if (grantsRoot.value?.id === root.id) grants.value = result
  } catch (err) {
    if (grantsRoot.value?.id === root.id) {
      grants.value = []
      grantsError.value = describeError(err)
      ElMessage.error(grantsError.value)
    }
  } finally {
    grantsLoading.value = false
  }
}

async function submitGrant() {
  const root = grantsRoot.value
  if (!root) return
  if (!grantForm.agent_id) {
    ElMessage.warning('请选择要授权的 Agent')
    return
  }
  granting.value = true
  try {
    const grant = await fileRootsApi.grant(root.id, {
      agent_id: grantForm.agent_id,
      permission: grantForm.permission,
    })
    ElMessage.success(
      `已授予「${grant.agent_name || '该 Agent'}」${permissionLabel(grant.permission)}权限`,
    )
    grantForm.agent_id = ''
    await loadGrants()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    granting.value = false
  }
}

async function revokeGrant(grant: FileRootGrant) {
  const root = grantsRoot.value
  if (!root) return
  try {
    await ElMessageBox.confirm(
      `确定撤销「${agentLabel(grant)}」对根目录「${root.name}」的授权吗？`,
      '撤销确认',
      {
        confirmButtonText: '撤销授权',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }
  try {
    await fileRootsApi.revoke(root.id, grant.agent_id)
    ElMessage.success('授权已撤销，该 Agent 的后续读取将立即被拒绝')
    await loadGrants()
  } catch {
    // interceptor surfaces the backend message
  }
}

onMounted(load)
</script>

<template>
  <div class="page-stack">
    <PageHeader
      title="文件根目录"
      subtitle="文件根目录是服务端托管的逻辑目录，可按读写权限同时授权给多个 Agent"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新建根目录</el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <div v-if="loading || roots.length" v-loading="loading" class="root-grid">
      <div v-for="root in roots" :key="root.id" class="root-card">
        <div class="root-head">
          <div class="root-icon">
            <el-icon><FolderOpened /></el-icon>
          </div>
          <div class="root-name">{{ root.name }}</div>
        </div>
        <p class="root-desc">{{ root.description || '暂无描述' }}</p>
        <div class="root-meta">
          <span class="muted mono">ID {{ root.id }}</span>
          <span class="muted">创建于 {{ formatDate(root.created_at) }}</span>
        </div>
        <div class="root-actions">
          <el-button type="primary" plain size="small" @click="openGrants(root)">
            授权管理
          </el-button>
        </div>
      </div>
    </div>

    <div v-if="!loading && !error && !roots.length" class="card empty-card">
      <el-empty description="暂无文件根目录，点击右上角新建" :image-size="90" />
    </div>

    <el-dialog v-model="createVisible" title="新建根目录" width="520px">
      <el-form label-position="top" @submit.prevent="submitCreate">
        <el-form-item label="名称" required>
          <el-input
            v-model="createForm.name"
            maxlength="120"
            show-word-limit
            placeholder="例如：共享资料目录"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="说明该目录的用途与授权范围（可选）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="grantsVisible"
      :title="grantsRoot ? `授权管理 · ${grantsRoot.name}` : '授权管理'"
      size="620px"
    >
      <template v-if="grantsRoot">
        <div class="drawer-head">
          <div>
            <div class="drawer-title">{{ grantsRoot.name }}</div>
            <div class="muted drawer-desc">{{ grantsRoot.description || '暂无描述' }}</div>
          </div>
          <span class="muted mono">{{ shortId(grantsRoot.id, 12) }}</span>
        </div>

        <el-alert
          type="info"
          :closable="false"
          show-icon
          class="hint"
          title="根目录由服务端统一托管，授权变更即时生效；只读权限可浏览与下载，读写权限还可上传和删除文件。"
        />

        <div class="grant-form">
          <div class="section-title">新增授权</div>
          <el-form label-position="top" @submit.prevent="submitGrant">
            <div class="grant-grid">
              <el-form-item label="Agent" required>
                <el-select
                  v-model="grantForm.agent_id"
                  filterable
                  clearable
                  placeholder="选择 Agent"
                  class="full"
                >
                  <el-option
                    v-for="agent in agents"
                    :key="agent.id"
                    :label="agent.name"
                    :value="agent.id"
                  >
                    <span>{{ agent.name }}</span>
                    <span class="muted mono option-hint">{{ shortId(agent.id, 8) }}</span>
                  </el-option>
                </el-select>
              </el-form-item>
              <el-form-item label="权限">
                <el-select v-model="grantForm.permission" class="full">
                  <el-option label="只读" value="read" />
                  <el-option label="读写" value="write" />
                </el-select>
              </el-form-item>
            </div>
            <div class="grant-submit">
              <el-button
                type="primary"
                :loading="granting"
                :disabled="!grantForm.agent_id"
                @click="submitGrant"
              >
                授权
              </el-button>
              <span class="muted form-hint">已授权的 Agent 再次授权将更新其权限</span>
            </div>
          </el-form>
          <el-alert
            v-if="agentsError"
            type="warning"
            :closable="false"
            show-icon
            class="agents-alert"
            :title="`Agent 列表加载失败：${agentsError}`"
          />
        </div>

        <div class="section-title">已授权 Agent（{{ grants.length }}）</div>

        <el-alert
          v-if="grantsError"
          type="error"
          :closable="false"
          show-icon
          class="hint"
          :title="grantsError"
        >
          <el-button link type="primary" @click="loadGrants">重试</el-button>
        </el-alert>

        <el-table v-loading="grantsLoading" :data="grants" row-key="id" size="small">
          <el-table-column label="Agent" min-width="170">
            <template #default="{ row }">
              <div class="agent-cell">
                <span>{{ agentLabel(row) }}</span>
                <span class="muted mono">{{ shortId(row.agent_id, 10) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="权限" width="90">
            <template #default="{ row }">
              <el-tag
                size="small"
                effect="light"
                round
                :type="permissionTagType(row.permission)"
              >
                {{ permissionLabel(row.permission) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="158">
            <template #default="{ row }">
              {{ formatDate(row.updated_at || row.created_at) }}
            </template>
          </el-table-column>
          <el-table-column label="操作" width="72" fixed="right">
            <template #default="{ row }">
              <el-button link type="danger" @click="revokeGrant(row)">撤销</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="尚未授权任何 Agent" :image-size="70" />
          </template>
        </el-table>

        <p class="muted revoke-hint">撤销授权后，该 Agent 对此根目录的后续读取将立即被拒绝。</p>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.root-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
  min-height: 120px;
}

.root-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 16px 18px;
}

.root-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.root-icon {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  font-size: 17px;
  color: var(--el-color-primary);
  background: rgba(47, 107, 255, 0.1);
}

.root-name {
  font-size: 15px;
  font-weight: 620;
}

.root-desc {
  margin: 0;
  color: var(--app-muted);
  font-size: 12.5px;
  line-height: 1.6;
  min-height: 40px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.root-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  font-size: 11px;
}

.root-actions {
  margin-top: auto;
  padding-top: 4px;
  display: flex;
  justify-content: flex-end;
}

.empty-card {
  padding: 10px 20px;
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

.drawer-desc {
  font-size: 12px;
}

.hint {
  margin-bottom: 14px;
}

.grant-form {
  padding: 14px 16px 12px;
  border: 1px solid var(--app-border);
  border-radius: 12px;
  background: rgba(47, 107, 255, 0.03);
}

.grant-form :deep(.el-form-item) {
  margin-bottom: 10px;
}

.grant-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 150px;
  gap: 12px;
}

.grant-submit {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.form-hint {
  font-size: 11.5px;
}

.option-hint {
  margin-left: 8px;
  font-size: 11px;
}

.agents-alert {
  margin-top: 10px;
}

.section-title {
  margin: 16px 0 8px;
  font-size: 12px;
  font-weight: 650;
  letter-spacing: 0.4px;
  color: var(--app-muted);
}

.agent-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.35;
}

.revoke-hint {
  margin: 10px 0 0;
  font-size: 11.5px;
}

.full {
  width: 100%;
}
</style>
