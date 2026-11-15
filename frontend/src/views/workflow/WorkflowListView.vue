<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, VideoPlay } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { workflowApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type {
  ExecutionInfo,
  WorkflowDefinition,
  WorkflowInfo,
  WorkflowNode,
  WorkflowNodeType,
  WorkflowValidation,
  WorkflowVersionInfo,
} from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

const auth = useAuthStore()
const router = useRouter()

const workflows = ref<WorkflowInfo[]>([])
const loading = ref(false)
const error = ref('')

const editorVisible = ref(false)
const editingWorkflow = ref<WorkflowInfo | null>(null)
const editorForm = ref({ name: '', description: '' })
const definitionText = ref('')
const saving = ref(false)
const validating = ref(false)
const publishing = ref(false)
const validation = ref<WorkflowValidation | null>(null)
const validationVisible = ref(false)

const versionsVisible = ref(false)
const versionsWorkflow = ref<WorkflowInfo | null>(null)
const versions = ref<WorkflowVersionInfo[]>([])
const versionsLoading = ref(false)
const versionsError = ref('')
const selectedVersionId = ref('')

const executeVisible = ref(false)
const executeWorkflowId = ref('')
const executeInput = ref('')
const executeVariablesText = ref('{}')
const executing = ref(false)
const executeError = ref('')
const executeResult = ref<ExecutionInfo | null>(null)

const canWrite = computed(() => auth.has('workflows:write'))
const canPublish = computed(() => auth.has('workflows:publish'))

const publishedWorkflows = computed(() =>
  workflows.value.filter((workflow) => workflow.status === 'published'),
)

const editorTitle = computed(() =>
  editingWorkflow.value ? `编辑工作流 · ${editingWorkflow.value.name}` : '新建工作流',
)

const selectedVersion = computed(
  () => versions.value.find((item) => item.id === selectedVersionId.value) || null,
)

const nodeTypes: { value: WorkflowNodeType; label: string; hint: string }[] = [
  { value: 'start', label: '开始', hint: '唯一入口，next 必填' },
  { value: 'llm', label: 'LLM', hint: 'prompt 必填，写入 output_key' },
  { value: 'agent', label: 'Agent', hint: 'agent_id 必填' },
  { value: 'tool', label: '工具', hint: 'tool_name / arguments' },
  { value: 'knowledge', label: '知识检索', hint: '知识库 + query' },
  { value: 'condition', label: '条件分支', hint: 'expression + on_true/on_false' },
  { value: 'approval', label: '人工审批', hint: 'on_reject: fail|continue' },
  { value: 'end', label: '结束', hint: '不能有出边' },
]

const nodeTemplates: Record<WorkflowNodeType, Record<string, unknown>> = {
  start: {},
  llm: {
    prompt: '请根据输入生成结果',
    system_prompt: '',
    model_config_id: '',
    output_key: 'llm_output',
  },
  agent: {
    agent_id: '',
    input: '{{input}}',
    output_key: 'agent_output',
    max_iterations: 6,
  },
  tool: {
    tool_name: '',
    arguments: {},
    output_key: 'tool_output',
    requires_approval: false,
  },
  knowledge: {
    knowledge_base_ids: [],
    query: '{{input}}',
    top_k: 5,
    output_key: 'knowledge',
  },
  condition: {
    expression: 'true',
  },
  approval: {
    title: '需要人工审批',
    description: '请确认继续执行',
    on_reject: 'fail',
  },
  end: {
    output_key: 'result',
  },
}

const nodeReferences = [
  {
    type: 'start',
    fields: 'next',
    note: '必须且仅有一个入口节点，next 指向后继节点；config 会被忽略',
  },
  {
    type: 'llm',
    fields: 'prompt / system_prompt / model_config_id / output_key',
    note: 'prompt 必填；output_key 默认 llm_output，需符合标识符格式',
  },
  {
    type: 'agent',
    fields: 'agent_id / input / output_key / max_iterations',
    note: 'agent_id 必填；input 可使用 {{input}} 或上游输出',
  },
  {
    type: 'tool',
    fields: 'tool_name / arguments / output_key / requires_approval',
    note: 'tool_name 必填；arguments 必须是 JSON 对象',
  },
  {
    type: 'knowledge',
    fields: 'knowledge_base_ids / query / top_k / output_key',
    note: '至少一个知识库，query 必填；top_k 建议 1–20',
  },
  {
    type: 'condition',
    fields: 'expression / on_true / on_false',
    note: 'expression 必填；on_true 与 on_false 均必填且指向已存在节点',
  },
  {
    type: 'approval',
    fields: 'title / description / on_reject',
    note: "on_reject 只能是 'fail' 或 'continue'",
  },
  {
    type: 'end',
    fields: 'output_key',
    note: '终止节点，不能配置 next / on_true / on_false',
  },
  {
    type: '公共字段',
    fields: 'id / type / name / config / next / on_true / on_false / retry',
    note: 'id 需以字母或下划线开头；retry 为 {max_attempts, backoff_seconds}',
  },
]

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

function exampleDefinition(): WorkflowDefinition {
  return {
    version: 1,
    start_node: 'start',
    nodes: [
      {
        id: 'start',
        type: 'start',
        name: '开始',
        config: {},
        next: 'llm_1',
        on_true: null,
        on_false: null,
        retry: null,
      },
      {
        id: 'llm_1',
        type: 'llm',
        name: '生成结果',
        config: {
          prompt: '请根据用户输入生成结果',
          system_prompt: '',
          model_config_id: '',
          output_key: 'llm_output',
        },
        next: 'end',
        on_true: null,
        on_false: null,
        retry: null,
      },
      {
        id: 'end',
        type: 'end',
        name: '结束',
        config: { output_key: 'llm_output' },
        next: null,
        on_true: null,
        on_false: null,
        retry: null,
      },
    ],
    output: 'llm_output',
  }
}

function readDefinition(showError = true): WorkflowDefinition | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(definitionText.value)
  } catch {
    if (showError) ElMessage.error('工作流定义不是合法 JSON')
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    if (showError) ElMessage.error('工作流定义必须是 JSON 对象')
    return null
  }
  const candidate = parsed as Partial<WorkflowDefinition>
  if (!Array.isArray(candidate.nodes)) {
    if (showError) ElMessage.error('工作流定义必须包含 nodes 数组')
    return null
  }
  return parsed as WorkflowDefinition
}

function parseJsonObject(text: string, label: string): Record<string, unknown> | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(text.trim() || '{}')
  } catch {
    ElMessage.error(`${label} 不是合法 JSON`)
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    ElMessage.error(`${label} 必须是 JSON 对象`)
    return null
  }
  return parsed as Record<string, unknown>
}

async function loadWorkflows() {
  loading.value = true
  error.value = ''
  try {
    workflows.value = await workflowApi.list()
  } catch (err) {
    workflows.value = []
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingWorkflow.value = null
  editorForm.value = { name: '', description: '' }
  definitionText.value = pretty(exampleDefinition())
  validation.value = null
  editorVisible.value = true
}

function openEdit(workflow: WorkflowInfo) {
  editingWorkflow.value = workflow
  editorForm.value = { name: workflow.name, description: workflow.description }
  definitionText.value = pretty(workflow.definition)
  validation.value = null
  editorVisible.value = true
}

function formatDefinition() {
  const definition = readDefinition()
  if (!definition) return
  definitionText.value = pretty(definition)
  ElMessage.success('JSON 已格式化')
}

function restoreExample() {
  definitionText.value = pretty(exampleDefinition())
  if (!editorForm.value.name) editorForm.value.name = '示例工作流'
  ElMessage.success('已载入示例定义')
}

function addNode(type: WorkflowNodeType) {
  const definition = readDefinition()
  if (!definition) return
  const ids = new Set(definition.nodes.map((node) => node.id))
  let id: string = type
  let counter = 2
  while (ids.has(id)) {
    id = `${type}_${counter}`
    counter += 1
  }
  const meta = nodeTypes.find((item) => item.value === type)
  const node: WorkflowNode = {
    id,
    type,
    name: meta?.label || type,
    config: structuredClone(nodeTemplates[type]),
    next: null,
    on_true: null,
    on_false: null,
    retry: null,
  }
  definition.nodes.push(node)
  if (!definition.version) definition.version = 1
  if (type === 'start' && !definition.start_node) definition.start_node = id
  definitionText.value = pretty(definition)
  ElMessage.success(`已插入节点 ${id}，请补充配置与连线`)
}

async function saveDraft(silent = false): Promise<boolean> {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有工作流编辑权限')
    return false
  }
  const name = editorForm.value.name.trim()
  if (name.length < 2) {
    ElMessage.warning('工作流名称至少需要 2 个字符')
    return false
  }
  const definition = readDefinition()
  if (!definition) return false

  saving.value = true
  try {
    const payload = {
      name,
      description: editorForm.value.description,
      definition,
    }
    const saved = editingWorkflow.value
      ? await workflowApi.update(editingWorkflow.value.id, payload)
      : await workflowApi.create(payload)
    editingWorkflow.value = saved
    editorForm.value = { name: saved.name, description: saved.description }
    definitionText.value = pretty(saved.definition)
    await loadWorkflows()
    if (!silent) ElMessage.success('工作流草稿已保存')
    return true
  } catch {
    return false
  } finally {
    saving.value = false
  }
}

async function validateWorkflow() {
  const saved = await saveDraft(true)
  if (!saved || !editingWorkflow.value) return
  validating.value = true
  try {
    validation.value = await workflowApi.validate(editingWorkflow.value.id)
    validationVisible.value = true
  } catch {
    // interceptor surfaces the backend message
  } finally {
    validating.value = false
  }
}

async function publishWorkflow() {
  if (!canPublish.value) {
    ElMessage.warning('当前账号没有工作流发布权限')
    return
  }
  const saved = await saveDraft(true)
  if (!saved || !editingWorkflow.value) return
  publishing.value = true
  try {
    let result: WorkflowValidation
    try {
      result = await workflowApi.validate(editingWorkflow.value.id)
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
    const published = await workflowApi.publish(editingWorkflow.value.id)
    editingWorkflow.value = published
    ElMessage.success(
      `工作流已发布${published.current_version ? ` · v${published.current_version}` : ''}`,
    )
    await loadWorkflows()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    publishing.value = false
  }
}

async function publishFromList(workflow: WorkflowInfo) {
  if (!canPublish.value) return
  try {
    const published = await workflowApi.publish(workflow.id)
    ElMessage.success(
      `工作流已发布${published.current_version ? ` · v${published.current_version}` : ''}`,
    )
    await loadWorkflows()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function disableWorkflow(workflow: WorkflowInfo) {
  if (!canPublish.value) return
  try {
    await ElMessageBox.confirm(`确定停用工作流「${workflow.name}」吗？`, '停用确认', {
      confirmButtonText: '停用',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const updated = await workflowApi.disable(workflow.id)
    if (editingWorkflow.value?.id === updated.id) editingWorkflow.value = updated
    ElMessage.success('工作流已停用')
    await loadWorkflows()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function deleteWorkflow(workflow: WorkflowInfo) {
  if (!canWrite.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除工作流「${workflow.name}」吗？该操作不可撤销。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await workflowApi.remove(workflow.id)
    ElMessage.success('工作流已删除')
    if (editingWorkflow.value?.id === workflow.id) editorVisible.value = false
    await loadWorkflows()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function openVersions(workflow: WorkflowInfo) {
  versionsWorkflow.value = workflow
  versionsVisible.value = true
  versionsLoading.value = true
  versionsError.value = ''
  versions.value = []
  selectedVersionId.value = ''
  try {
    versions.value = await workflowApi.versions(workflow.id)
    selectedVersionId.value = versions.value[0]?.id || ''
  } catch (err) {
    versionsError.value = describeError(err)
  } finally {
    versionsLoading.value = false
  }
}

function openExecute(workflow?: WorkflowInfo) {
  executeWorkflowId.value = workflow?.id || publishedWorkflows.value[0]?.id || ''
  executeInput.value = ''
  executeVariablesText.value = '{}'
  executeResult.value = null
  executeError.value = ''
  executeVisible.value = true
}

async function runExecution() {
  if (!executeWorkflowId.value) {
    ElMessage.warning('请选择已发布的工作流')
    return
  }
  const variables = parseJsonObject(executeVariablesText.value, '变量')
  if (!variables) return
  executing.value = true
  executeError.value = ''
  executeResult.value = null
  try {
    executeResult.value = await workflowApi.execute(executeWorkflowId.value, {
      input: executeInput.value,
      variables,
    })
    ElMessage.success('工作流已开始执行')
  } catch (err) {
    executeError.value = describeError(err)
  } finally {
    executing.value = false
  }
}

function openExecution(id: string) {
  void router.push(`/executions/${id}`)
}

onMounted(loadWorkflows)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="工作流" subtitle="结构化编排 LLM、Agent、工具、知识与审批节点">
      <template #actions>
        <el-button :loading="loading" @click="loadWorkflows">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button :icon="VideoPlay" @click="openExecute()">执行工作流</el-button>
        <el-button v-if="canWrite" type="primary" :icon="Plus" @click="openCreate">
          新建工作流
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="loadWorkflows">重试</el-button>
    </el-alert>

    <div class="card">
      <el-table v-loading="loading" :data="workflows" row-key="id" size="small">
        <el-table-column label="名称" min-width="190">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span class="muted desc">{{ row.description || '暂无描述' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column label="当前版本" width="100">
          <template #default="{ row }">
            <span v-if="row.current_version" class="mono">v{{ row.current_version }}</span>
            <span v-else class="muted">未发布</span>
          </template>
        </el-table-column>
        <el-table-column label="所有者" width="120">
          <template #default="{ row }">
            <span class="mono muted">{{ shortId(row.owner_id, 12) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="168">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="292" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button v-if="canWrite" link type="primary" @click="openEdit(row)">
                编辑
              </el-button>
              <el-button
                v-if="row.status === 'published'"
                link
                type="success"
                @click="openExecute(row)"
              >
                执行
              </el-button>
              <el-button v-if="canPublish" link @click="publishFromList(row)">
                发布
              </el-button>
              <el-button
                v-if="canPublish && row.status === 'published'"
                link
                type="warning"
                @click="disableWorkflow(row)"
              >
                停用
              </el-button>
              <el-button link @click="openVersions(row)">版本</el-button>
              <el-button v-if="canWrite" link type="danger" @click="deleteWorkflow(row)">
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无工作流，点击右上角新建" :image-size="80" />
        </template>
      </el-table>
    </div>

    <el-drawer v-model="editorVisible" size="980px" :title="editorTitle">
      <div class="editor">
        <el-form label-position="top" @submit.prevent>
          <div class="grid-2">
            <el-form-item label="名称" required>
              <el-input v-model="editorForm.name" maxlength="120" show-word-limit />
            </el-form-item>
            <el-form-item label="描述">
              <el-input v-model="editorForm.description" maxlength="500" show-word-limit />
            </el-form-item>
          </div>
        </el-form>

        <div class="editor-grid">
          <div class="json-pane">
            <div class="pane-head">
              <span class="pane-title">WorkflowDefinition JSON</span>
              <div class="pane-actions">
                <el-button size="small" @click="formatDefinition">格式化</el-button>
                <el-button size="small" @click="restoreExample">载入示例</el-button>
              </div>
            </div>
            <el-input
              v-model="definitionText"
              type="textarea"
              :rows="24"
              class="json-input"
              spellcheck="false"
            />
            <div class="muted pane-hint">
              节点通过 next（普通节点）或 on_true/on_false（条件节点）连接；保存后由服务端校验可达性与环路。
            </div>
          </div>

          <aside class="palette">
            <div class="pane-title">节点面板</div>
            <div class="palette-list">
              <button
                v-for="node in nodeTypes"
                :key="node.value"
                type="button"
                class="palette-item"
                @click="addNode(node.value)"
              >
                <span class="palette-label">{{ node.label }}</span>
                <span class="palette-type mono">{{ node.value }}</span>
                <span class="palette-hint muted">{{ node.hint }}</span>
              </button>
            </div>
            <div class="palette-note muted">
              点击节点类型会向 JSON 的 nodes 数组追加一条模板，请继续补充 config 与 transitions。
            </div>
          </aside>
        </div>

        <div class="reference">
          <div class="pane-title">节点配置参考</div>
          <el-table :data="nodeReferences" size="small" border>
            <el-table-column prop="type" label="类型" width="110">
              <template #default="{ row }"><span class="mono">{{ row.type }}</span></template>
            </el-table-column>
            <el-table-column prop="fields" label="配置键" min-width="300" />
            <el-table-column prop="note" label="说明" min-width="320" />
          </el-table>
        </div>
      </div>

      <template #footer>
        <div class="editor-footer">
          <div class="footer-left">
            <el-button v-if="editingWorkflow" @click="openVersions(editingWorkflow)">
              版本记录
            </el-button>
            <el-button
              v-if="editingWorkflow && canPublish && editingWorkflow.status === 'published'"
              type="warning"
              plain
              @click="disableWorkflow(editingWorkflow)"
            >
              停用
            </el-button>
          </div>
          <div class="footer-right">
            <el-button @click="editorVisible = false">关闭</el-button>
            <el-button :loading="validating" @click="validateWorkflow">校验</el-button>
            <el-button
              v-if="canPublish"
              type="success"
              plain
              :loading="publishing"
              @click="publishWorkflow"
            >
              发布
            </el-button>
            <el-button v-if="canWrite" type="primary" :loading="saving" @click="saveDraft()">
              保存草稿
            </el-button>
          </div>
        </div>
      </template>
    </el-drawer>

    <el-drawer
      v-model="versionsVisible"
      size="600px"
      :title="versionsWorkflow ? `版本记录 · ${versionsWorkflow.name}` : '版本记录'"
    >
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
        <div class="version-list">
          <button
            v-for="version in versions"
            :key="version.id"
            type="button"
            class="version-item"
            :class="{ active: version.id === selectedVersionId }"
            @click="selectedVersionId = version.id"
          >
            <span class="version-no">v{{ version.version }}</span>
            <span class="muted">{{ formatDate(version.published_at || version.created_at) }}</span>
          </button>
        </div>
        <template v-if="selectedVersion">
          <div class="version-meta">
            <StatusTag :status="selectedVersion.status" />
            <span class="muted">发布人 {{ shortId(selectedVersion.created_by, 12) }}</span>
          </div>
          <div class="block-label">定义快照</div>
          <JsonBlock :value="selectedVersion.definition" max-height="480px" />
        </template>
      </template>
      <el-empty
        v-else-if="!versionsError"
        description="暂无发布版本"
        :image-size="80"
      />
    </el-drawer>

    <el-dialog v-model="executeVisible" title="执行工作流" width="620px">
      <el-form label-position="top" @submit.prevent="runExecution">
        <el-form-item label="工作流" required>
          <el-select
            v-model="executeWorkflowId"
            filterable
            class="full"
            placeholder="选择已发布的工作流"
          >
            <el-option
              v-for="workflow in publishedWorkflows"
              :key="workflow.id"
              :label="`${workflow.name} · v${workflow.current_version ?? '-'}`"
              :value="workflow.id"
            />
          </el-select>
          <div v-if="!publishedWorkflows.length" class="inline-hint muted">
            暂无已发布工作流，请先发布后再执行。
          </div>
        </el-form-item>
        <el-form-item label="输入">
          <el-input
            v-model="executeInput"
            type="textarea"
            :rows="4"
            placeholder="输入要交给工作流处理的内容"
          />
        </el-form-item>
        <el-form-item label="变量（JSON 对象）">
          <el-input
            v-model="executeVariablesText"
            type="textarea"
            :rows="3"
            class="mono-input"
            placeholder='{"customer": "ACME"}'
          />
        </el-form-item>
      </el-form>

      <el-alert
        v-if="executeError"
        type="error"
        :closable="false"
        show-icon
        :title="executeError"
        class="gap"
      />

      <div v-if="executeResult" class="execution-result">
        <el-tag type="success" effect="light">已创建执行</el-tag>
        <span class="mono">{{ executeResult.id }}</span>
        <el-button link type="primary" @click="openExecution(executeResult.id)">
          查看执行详情
        </el-button>
      </div>

      <template #footer>
        <el-button @click="executeVisible = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="executing"
          :disabled="!publishedWorkflows.length"
          @click="runExecution"
        >
          执行
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="validationVisible" title="校验结果" width="560px">
      <template v-if="validation">
        <div class="validation-head">
          <StatusTag :status="validation.valid ? 'success' : 'failed'" />
          <span class="muted">
            {{ validation.valid ? '定义校验通过' : '定义存在错误，请修复后重试' }}
          </span>
        </div>
        <el-alert
          v-if="validation.errors.length"
          type="error"
          :closable="false"
          title="错误"
          class="gap"
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
          class="gap"
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
  </div>
</template>

<style scoped>
.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
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

.desc {
  font-size: 11.5px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
}

.editor {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-bottom: 8px;
}

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0 18px;
}

.editor-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 250px;
  gap: 16px;
  align-items: start;
}

@media (max-width: 900px) {
  .editor-grid {
    grid-template-columns: 1fr;
  }
}

.json-pane {
  min-width: 0;
}

.pane-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 8px;
}

.pane-title {
  font-size: 13px;
  font-weight: 650;
}

.pane-actions {
  display: flex;
  gap: 6px;
}

.json-input :deep(textarea) {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  min-height: 520px;
}

.pane-hint {
  font-size: 11px;
  line-height: 1.5;
  margin-top: 6px;
}

.palette {
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 12px;
  background: #fbfcfe;
}

.palette-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
}

.palette-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 8px 10px;
  border: 1px solid var(--app-border);
  border-radius: 9px;
  background: #fff;
  cursor: pointer;
  font-family: inherit;
  text-align: left;
  transition: border-color 0.15s ease;
}

.palette-item:hover {
  border-color: var(--el-color-primary);
}

.palette-label {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--app-text);
}

.palette-type {
  font-size: 10.5px;
  color: var(--app-muted);
}

.palette-hint {
  font-size: 10.5px;
  line-height: 1.4;
}

.palette-note {
  margin-top: 10px;
  font-size: 10.5px;
  line-height: 1.5;
}

.reference {
  border-top: 1px dashed var(--app-border);
  padding-top: 14px;
}

.reference .pane-title {
  margin-bottom: 8px;
}

.editor-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.footer-left,
.footer-right {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.version-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 14px;
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

.version-item.active {
  border-color: var(--el-color-primary);
  background: rgba(47, 107, 255, 0.06);
}

.version-no {
  font-weight: 650;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
}

.version-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  font-size: 12px;
}

.block-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--app-muted);
  margin-bottom: 6px;
}

.gap {
  margin-top: 12px;
}

.full {
  width: 100%;
}

.mono-input :deep(textarea) {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}

.inline-hint {
  font-size: 11.5px;
  margin-top: 4px;
  line-height: 1.5;
}

.execution-result {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: #f8fafc;
  margin-top: 12px;
  font-size: 12px;
}

.validation-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.check-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}
</style>
