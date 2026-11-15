<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import PromptVariableEditor from '@/components/PromptVariableEditor.vue'
import { promptsApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { PromptInfo, PromptVariable, PromptVersion, ResolvedPrompt } from '@/types/api'
import { formatDate } from '@/utils/format'

const auth = useAuthStore()

const prompts = ref<PromptInfo[]>([])
const loading = ref(false)
const error = ref('')

const canWrite = computed(() => auth.has('prompts:write'))

const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ name: '', description: '' })

const detailVisible = ref(false)
const activePrompt = ref<PromptInfo | null>(null)
const versions = ref<PromptVersion[]>([])
const versionsLoading = ref(false)
const versionsError = ref('')
const selectedVersionId = ref('')

const editingVersion = ref(false)
const savingVersion = ref(false)
const versionForm = ref({
  system_prompt: '',
  task_prompt: '',
  tool_instructions: '',
  variables: [] as PromptVariable[],
  publish: true,
})

const resolveVariables = ref<Record<string, string>>({})
const resolving = ref(false)
const resolveError = ref('')
const resolved = ref<ResolvedPrompt | null>(null)

const selectedVersion = computed(
  () => versions.value.find((item) => item.id === selectedVersionId.value) || null,
)

const variablePattern = /^[A-Za-z_][A-Za-z0-9_]*$/

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

async function loadPrompts() {
  loading.value = true
  error.value = ''
  try {
    prompts.value = await promptsApi.list()
  } catch (err) {
    prompts.value = []
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  createForm.value = { name: '', description: '' }
  createVisible.value = true
}

async function submitCreate() {
  const name = createForm.value.name.trim()
  if (name.length < 2) {
    ElMessage.warning('名称至少需要 2 个字符')
    return
  }
  creating.value = true
  try {
    const created = await promptsApi.create({
      name,
      description: createForm.value.description,
    })
    ElMessage.success('Prompt 已创建')
    createVisible.value = false
    await loadPrompts()
    await openDetail(created)
  } catch {
    // interceptor surfaces the backend message
  } finally {
    creating.value = false
  }
}

async function loadVersions(promptId: string) {
  versionsLoading.value = true
  versionsError.value = ''
  versions.value = []
  selectedVersionId.value = ''
  try {
    versions.value = await promptsApi.versions(promptId)
    const latest = [...versions.value].sort((a, b) => b.version - a.version)[0]
    if (latest) selectVersion(latest.id)
  } catch (err) {
    versionsError.value = describeError(err)
  } finally {
    versionsLoading.value = false
  }
}

async function openDetail(prompt: PromptInfo) {
  activePrompt.value = prompt
  detailVisible.value = true
  editingVersion.value = false
  resolved.value = null
  resolveError.value = ''
  await loadVersions(prompt.id)
}

function selectVersion(versionId: string) {
  selectedVersionId.value = versionId
  resolved.value = null
  resolveError.value = ''
  const next: Record<string, string> = {}
  for (const variable of selectedVersion.value?.variables || []) {
    next[variable.name] = variable.default || ''
  }
  resolveVariables.value = next
}

function startNewVersion() {
  const source = selectedVersion.value
  versionForm.value = {
    system_prompt: source?.system_prompt || '',
    task_prompt: source?.task_prompt || '',
    tool_instructions: source?.tool_instructions || '',
    variables: source ? structuredClone(source.variables) : [],
    publish: true,
  }
  editingVersion.value = true
}

function cancelNewVersion() {
  editingVersion.value = false
}

function validateVersionForm(): boolean {
  if (!versionForm.value.system_prompt.trim()) {
    ElMessage.warning('System Prompt 不能为空')
    return false
  }
  const names = versionForm.value.variables.map((item) => item.name.trim())
  if (names.some((name) => !variablePattern.test(name))) {
    ElMessage.warning('变量名需以字母或下划线开头，仅包含字母、数字与下划线')
    return false
  }
  if (new Set(names).size !== names.length) {
    ElMessage.warning('变量名不能重复')
    return false
  }
  return true
}

async function submitVersion() {
  if (!activePrompt.value || !validateVersionForm()) return
  savingVersion.value = true
  try {
    const created = await promptsApi.createVersion(activePrompt.value.id, {
      system_prompt: versionForm.value.system_prompt,
      task_prompt: versionForm.value.task_prompt,
      tool_instructions: versionForm.value.tool_instructions,
      variables: versionForm.value.variables.map((item) => ({
        name: item.name.trim(),
        required: item.required,
        default: item.default,
      })),
      publish: versionForm.value.publish,
    })
    ElMessage.success(versionForm.value.publish ? '新版本已创建并发布' : '新版本草稿已创建')
    editingVersion.value = false
    await loadPrompts()
    const refreshed = prompts.value.find((item) => item.id === activePrompt.value?.id)
    if (refreshed) activePrompt.value = refreshed
    await loadVersions(created.prompt_id)
    selectVersion(created.id)
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingVersion.value = false
  }
}

async function resolvePreview() {
  if (!activePrompt.value || !selectedVersion.value) return
  for (const variable of selectedVersion.value.variables) {
    if (variable.required && !resolveVariables.value[variable.name]) {
      ElMessage.warning(`请填写必填变量 ${variable.name}`)
      return
    }
  }
  resolving.value = true
  resolveError.value = ''
  resolved.value = null
  try {
    resolved.value = await promptsApi.resolve(activePrompt.value.id, {
      version_id: selectedVersion.value.id,
      variables: resolveVariables.value,
    })
  } catch (err) {
    resolveError.value = describeError(err)
  } finally {
    resolving.value = false
  }
}

onMounted(loadPrompts)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="Prompt 管理" subtitle="集中管理提示词模板、版本与变量渲染">
      <template #actions>
        <el-button :loading="loading" @click="loadPrompts">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openCreate">
          新建 Prompt
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="loadPrompts">重试</el-button>
    </el-alert>

    <div class="card">
      <el-table
        v-loading="loading"
        :data="prompts"
        row-key="id"
        size="small"
        class="prompt-table"
        @row-click="openDetail"
      >
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span class="muted mono">{{ row.id }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="描述" min-width="240">
          <template #default="{ row }">
            <span class="muted">{{ row.description || '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="当前版本" width="104">
          <template #default="{ row }">
            <span class="mono">v{{ row.current_version }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <StatusTag :status="row.status" />
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="" width="76" align="right">
          <template #default="{ row }">
            <el-button link type="primary" @click.stop="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无 Prompt 模板" :image-size="80" />
        </template>
      </el-table>
    </div>

    <el-dialog v-model="createVisible" title="新建 Prompt" width="520px">
      <el-form label-position="top" @submit.prevent="submitCreate">
        <el-form-item label="名称" required>
          <el-input v-model="createForm.name" maxlength="120" show-word-limit placeholder="如：审计报告生成" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input
            v-model="createForm.description"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="描述模板用途"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-drawer v-model="detailVisible" size="720px" :title="activePrompt?.name || 'Prompt 详情'">
      <div v-if="activePrompt" class="detail">
        <div class="detail-head">
          <div>
            <div class="muted desc">{{ activePrompt.description || '暂无描述' }}</div>
          </div>
          <div class="head-actions">
            <StatusTag :status="activePrompt.status" />
            <el-button
              v-if="canWrite && !editingVersion"
              size="small"
              type="primary"
              plain
              :icon="Plus"
              @click="startNewVersion"
            >
              新建版本
            </el-button>
          </div>
        </div>

        <el-alert
          v-if="versionsError"
          type="error"
          :closable="false"
          show-icon
          :title="versionsError"
          class="gap"
        />

        <el-skeleton v-if="versionsLoading" :rows="6" animated />

        <template v-else-if="versions.length">
          <div class="section-label">版本</div>
          <div class="version-list">
            <button
              v-for="version in versions"
              :key="version.id"
              type="button"
              class="version-item"
              :class="{ active: version.id === selectedVersionId }"
              @click="selectVersion(version.id)"
            >
              <span class="version-no">v{{ version.version }}</span>
              <span class="muted">{{ formatDate(version.created_at) }}</span>
              <span v-if="version.version === activePrompt.current_version" class="current-tag">当前</span>
            </button>
          </div>

          <div v-if="editingVersion" class="version-editor">
            <div class="section-label">新建版本</div>
            <el-form label-position="top" @submit.prevent="submitVersion">
              <el-form-item label="System Prompt" required>
                <el-input v-model="versionForm.system_prompt" type="textarea" :rows="5" />
              </el-form-item>
              <el-form-item label="Task Prompt">
                <el-input v-model="versionForm.task_prompt" type="textarea" :rows="4" />
              </el-form-item>
              <el-form-item label="Tool Instructions">
                <el-input v-model="versionForm.tool_instructions" type="textarea" :rows="3" />
              </el-form-item>
              <el-form-item label="变量定义">
                <PromptVariableEditor v-model="versionForm.variables" />
              </el-form-item>
              <el-form-item label="发布">
                <el-switch v-model="versionForm.publish" active-text="创建后立即发布" />
              </el-form-item>
            </el-form>
            <div class="editor-actions">
              <el-button @click="cancelNewVersion">取消</el-button>
              <el-button type="primary" :loading="savingVersion" @click="submitVersion">
                保存版本
              </el-button>
            </div>
          </div>

          <template v-else-if="selectedVersion">
            <div class="content-grid">
              <div class="content-block">
                <div class="block-label">System Prompt</div>
                <pre>{{ selectedVersion.system_prompt || '（空）' }}</pre>
              </div>
              <div class="content-block">
                <div class="block-label">Task Prompt</div>
                <pre>{{ selectedVersion.task_prompt || '（空）' }}</pre>
              </div>
              <div class="content-block">
                <div class="block-label">Tool Instructions</div>
                <pre>{{ selectedVersion.tool_instructions || '（空）' }}</pre>
              </div>
            </div>

            <div class="section-label">变量</div>
            <el-table
              v-if="selectedVersion.variables.length"
              :data="selectedVersion.variables"
              size="small"
              border
            >
              <el-table-column prop="name" label="变量名" min-width="150">
                <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
              </el-table-column>
              <el-table-column label="必填" width="76" align="center">
                <template #default="{ row }">
                  <el-tag :type="row.required ? 'warning' : 'info'" size="small" effect="plain">
                    {{ row.required ? '必填' : '可选' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="默认值" min-width="180">
                <template #default="{ row }">
                  <span class="muted">{{ row.default || '-' }}</span>
                </template>
              </el-table-column>
            </el-table>
            <el-empty v-else description="该版本未定义变量" :image-size="50" />

            <div class="section-label">渲染预览</div>
            <div v-if="selectedVersion.variables.length" class="resolve-grid">
              <el-form-item
                v-for="variable in selectedVersion.variables"
                :key="variable.name"
                :label="variable.name"
                :required="variable.required"
              >
                <el-input v-model="resolveVariables[variable.name]" :placeholder="variable.default || '请输入变量值'" />
              </el-form-item>
            </div>
            <el-button
              type="primary"
              plain
              :loading="resolving"
              :disabled="!selectedVersion"
              @click="resolvePreview"
            >
              解析预览
            </el-button>

            <el-alert
              v-if="resolveError"
              type="error"
              :closable="false"
              show-icon
              :title="resolveError"
              class="gap"
            />
            <div v-if="resolved" class="resolved">
              <div class="muted resolved-meta">渲染版本 v{{ resolved.version }}</div>
              <div class="content-block">
                <div class="block-label">System Prompt</div>
                <pre>{{ resolved.system_prompt || '（空）' }}</pre>
              </div>
              <div class="content-block">
                <div class="block-label">Task Prompt</div>
                <pre>{{ resolved.task_prompt || '（空）' }}</pre>
              </div>
              <div class="content-block">
                <div class="block-label">Tool Instructions</div>
                <pre>{{ resolved.tool_instructions || '（空）' }}</pre>
              </div>
            </div>
          </template>
        </template>

        <el-empty
          v-else-if="!versionsError"
          description="暂无版本，点击「新建版本」创建"
          :image-size="80"
        />
      </div>
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

.prompt-table {
  cursor: pointer;
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

.detail {
  padding: 4px 6px 24px;
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 16px;
}

.desc {
  font-size: 12px;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.section-label {
  font-size: 12.5px;
  font-weight: 650;
  margin: 14px 0 8px;
}

.version-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.version-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 12px;
  border-radius: 9px;
  border: 1px solid var(--app-border);
  background: #fff;
  font-size: 12px;
  cursor: pointer;
  color: var(--app-text);
  font-family: inherit;
}

.version-item:hover {
  border-color: #b9cdfb;
}

.version-item.active {
  border-color: var(--el-color-primary);
  background: rgba(47, 107, 255, 0.06);
}

.version-no {
  font-weight: 650;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
}

.current-tag {
  font-size: 10px;
  color: #16a34a;
  background: rgba(22, 163, 74, 0.1);
  padding: 1px 6px;
  border-radius: 999px;
}

.content-grid {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.content-block .block-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--app-muted);
  margin-bottom: 6px;
}

.content-block pre {
  margin: 0;
  padding: 12px 14px;
  border-radius: 10px;
  background: #0f172a;
  color: #d7e2f5;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 220px;
  overflow: auto;
}

.version-editor {
  margin-top: 6px;
}

.editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}

.resolve-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0 14px;
  margin-bottom: 4px;
}

.gap {
  margin-top: 12px;
}

.resolved {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.resolved-meta {
  font-size: 11.5px;
}
</style>
