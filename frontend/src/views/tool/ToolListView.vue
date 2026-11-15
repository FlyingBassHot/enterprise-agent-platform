<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import { toolsApi, mcpApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { MCPServerInfo, MCPSyncResult, ToolInfo, ToolTestResult } from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

interface ToolFormState {
  name: string
  display_name: string
  description: string
  risk_level: string
  permission_scope: string[]
  requires_approval: boolean
  enabled: boolean
  method: string
  url: string
  headersText: string
  queryText: string
  body_mode: string
  timeout_seconds: number
}

interface McpFormState {
  name: string
  transport: 'stdio' | 'http'
  command: string
  argsText: string
  envText: string
  url: string
  headersText: string
  enabled: boolean
}

const auth = useAuthStore()

const activeTab = ref<'tools' | 'mcp'>('tools')

const tools = ref<ToolInfo[]>([])
const loading = ref(false)
const error = ref('')

const sourceFilter = ref('all')
const enabledFilter = ref('all')

const detailVisible = ref(false)
const detailTool = ref<ToolInfo | null>(null)

const dialogVisible = ref(false)
const editingTool = ref<ToolInfo | null>(null)
const saving = ref(false)
const form = ref<ToolFormState>(emptyForm())

const testVisible = ref(false)
const testTool = ref<ToolInfo | null>(null)
const testArgsText = ref('{}')
const testing = ref(false)
const testResult = ref<ToolTestResult | null>(null)
const testRunError = ref('')

const mcpServers = ref<MCPServerInfo[]>([])
const mcpLoading = ref(false)
const mcpError = ref('')
const mcpSyncingId = ref('')
const mcpTogglingId = ref('')

const mcpDialogVisible = ref(false)
const editingMcp = ref<MCPServerInfo | null>(null)
const savingMcp = ref(false)
const mcpForm = ref<McpFormState>(emptyMcpForm())
const originalEnvText = ref('{}')
const originalHeadersText = ref('{}')

const mcpToolsVisible = ref(false)
const mcpToolsServer = ref<MCPServerInfo | null>(null)
const mcpTools = ref<ToolInfo[]>([])
const mcpToolsLoading = ref(false)
const mcpToolsError = ref('')

const syncResultVisible = ref(false)
const syncResult = ref<MCPSyncResult | null>(null)

const riskLevels = [
  { value: 'low', label: '低风险' },
  { value: 'medium', label: '中风险' },
  { value: 'high', label: '高风险' },
  { value: 'critical', label: '极高风险' },
]

const methods = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']

const canWrite = computed(() => auth.has('tools:admin'))
const canManageMcp = computed(
  () => Boolean(auth.user?.is_superuser) || auth.has('tools:admin'),
)

const filteredTools = computed(() =>
  tools.value.filter((tool) => {
    if (sourceFilter.value !== 'all' && tool.source_type !== sourceFilter.value) return false
    if (enabledFilter.value === 'enabled' && !tool.enabled) return false
    if (enabledFilter.value === 'disabled' && tool.enabled) return false
    return true
  }),
)

const permissionAutocomplete = computed(() => {
  const codes = new Set<string>()
  for (const tool of tools.value) {
    for (const code of tool.permission_scope) codes.add(code)
  }
  return Array.from(codes)
})

function emptyForm(): ToolFormState {
  return {
    name: '',
    display_name: '',
    description: '',
    risk_level: 'low',
    permission_scope: [],
    requires_approval: false,
    enabled: true,
    method: 'GET',
    url: '',
    headersText: '{}',
    queryText: '{}',
    body_mode: 'json',
    timeout_seconds: 60,
  }
}

function emptyMcpForm(): McpFormState {
  return {
    name: '',
    transport: 'stdio',
    command: '',
    argsText: '[]',
    envText: '{}',
    url: '',
    headersText: '{}',
    enabled: true,
  }
}

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function sourceLabel(source: string): string {
  if (source === 'native') return '原生注册'
  if (source === 'http') return 'HTTP 声明式'
  return source
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function toJsonText(value: unknown): string {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    return JSON.stringify(value, null, 2)
  }
  return '{}'
}

function parseStringRecord(text: string, label: string): Record<string, string> | null {
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
  const result: Record<string, string> = {}
  for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof value !== 'string') {
      ElMessage.error(`${label} 中「${key}」的值必须是字符串`)
      return null
    }
    result[key] = value
  }
  return result
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

function parseStringArray(text: string, label: string): string[] | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(text.trim() || '[]')
  } catch {
    ElMessage.error(`${label} 不是合法 JSON`)
    return null
  }
  if (!Array.isArray(parsed)) {
    ElMessage.error(`${label} 必须是 JSON 字符串数组`)
    return null
  }
  const values: string[] = []
  for (const item of parsed) {
    if (typeof item !== 'string') {
      ElMessage.error(`${label} 中的每一项都必须是字符串`)
      return null
    }
    values.push(item)
  }
  return values
}

function checkSecretRefs(mapping: Record<string, string>, label: string): boolean {
  const pattern = /\$\{([^}]*)\}/g
  for (const [key, value] of Object.entries(mapping)) {
    for (const match of value.matchAll(pattern)) {
      if (!match[1].startsWith('TOOL_SECRET_')) {
        ElMessage.error(
          `${label} 中「${key}」引用了 ${match[0]}，环境变量引用必须以 \${TOOL_SECRET_} 开头`,
        )
        return false
      }
    }
  }
  return true
}

function hasMaskedValue(mapping: Record<string, string>): boolean {
  return Object.values(mapping).some((value) => value.includes('***'))
}

function transportLabel(transport: string): string {
  return transport === 'http' ? 'HTTP' : 'stdio'
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    tools.value = await toolsApi.list()
  } catch (err) {
    tools.value = []
    error.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function openDetail(tool: ToolInfo) {
  detailTool.value = tool
  detailVisible.value = true
}

function openCreate() {
  editingTool.value = null
  form.value = emptyForm()
  dialogVisible.value = true
}

function openEdit(tool: ToolInfo) {
  if (tool.source_type !== 'http') {
    ElMessage.info('原生工具由代码/插件注册，请在详情中查看或直接启停/测试')
    return
  }
  const config = tool.config || {}
  editingTool.value = tool
  form.value = {
    name: tool.name,
    display_name: tool.display_name,
    description: tool.description,
    risk_level: tool.risk_level,
    permission_scope: [...tool.permission_scope],
    requires_approval: tool.requires_approval,
    enabled: tool.enabled,
    method: asString(config.method, 'GET'),
    url: asString(config.url),
    headersText: toJsonText(config.headers),
    queryText: toJsonText(config.query),
    body_mode: asString(config.body_mode, 'json'),
    timeout_seconds:
      typeof config.timeout_seconds === 'number' ? config.timeout_seconds : tool.timeout_seconds,
  }
  dialogVisible.value = true
}

async function saveTool() {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有工具编辑权限')
    return
  }
  const name = form.value.name.trim()
  if (!editingTool.value) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(name) || name.length < 2) {
      ElMessage.warning('工具名称需以字母或数字开头，仅包含字母、数字、下划线、点、冒号、短横线')
      return
    }
  }
  const url = form.value.url.trim()
  if (!/^https?:\/\//i.test(url)) {
    ElMessage.warning('URL 必须使用 http 或 https 协议')
    return
  }
  const headers = parseStringRecord(form.value.headersText, 'Headers')
  if (!headers) return
  const query = parseStringRecord(form.value.queryText, 'Query')
  if (!query) return

  const timeout = Number(form.value.timeout_seconds) || 60
  const payload: Record<string, unknown> = {
    display_name: form.value.display_name,
    description: form.value.description,
    risk_level: form.value.risk_level,
    permission_scope: form.value.permission_scope,
    requires_approval: form.value.requires_approval,
    enabled: form.value.enabled,
    timeout_seconds: timeout,
    config: {
      method: form.value.method,
      url,
      headers,
      query,
      body_mode: form.value.body_mode,
      timeout_seconds: timeout,
    },
  }

  saving.value = true
  try {
    if (editingTool.value) {
      await toolsApi.update(editingTool.value.id, payload)
      ElMessage.success('工具已更新')
    } else {
      await toolsApi.create({ ...payload, name, source_type: 'http' })
      ElMessage.success('HTTP 工具已创建')
    }
    dialogVisible.value = false
    await load()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(tool: ToolInfo) {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有工具编辑权限')
    return
  }
  try {
    if (tool.enabled) {
      await toolsApi.disable(tool.id)
      ElMessage.success('工具已停用')
    } else {
      await toolsApi.enable(tool.id)
      ElMessage.success('工具已启用')
    }
    await load()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function removeTool(tool: ToolInfo) {
  if (tool.source_type !== 'http') return
  try {
    await ElMessageBox.confirm(
      `确定删除 HTTP 工具「${tool.name}」吗？该操作不可撤销。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await toolsApi.remove(tool.id)
    ElMessage.success('工具已删除')
    if (detailTool.value?.id === tool.id) detailVisible.value = false
    await load()
  } catch {
    // interceptor surfaces the backend message
  }
}

function openTest(tool: ToolInfo) {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有工具测试权限')
    return
  }
  testTool.value = tool
  testArgsText.value = '{}'
  testResult.value = null
  testRunError.value = ''
  testVisible.value = true
}

async function runTest() {
  if (!testTool.value) return
  const args = parseJsonObject(testArgsText.value, '测试参数')
  if (!args) return
  testing.value = true
  testResult.value = null
  testRunError.value = ''
  try {
    testResult.value = await toolsApi.test(testTool.value.id, { arguments: args })
  } catch (err) {
    testRunError.value = describeError(err)
  } finally {
    testing.value = false
  }
}

function refreshCurrent() {
  if (activeTab.value === 'mcp') {
    void loadMcp()
  } else {
    void load()
  }
}

async function loadMcp() {
  if (!canManageMcp.value) return
  mcpLoading.value = true
  mcpError.value = ''
  try {
    mcpServers.value = await mcpApi.list()
  } catch (err) {
    mcpServers.value = []
    mcpError.value = describeError(err)
  } finally {
    mcpLoading.value = false
  }
}

function openMcpDialog(server?: MCPServerInfo) {
  editingMcp.value = server || null
  if (server) {
    const envText = toJsonText(server.env)
    const headersText = toJsonText(server.headers)
    originalEnvText.value = envText
    originalHeadersText.value = headersText
    mcpForm.value = {
      name: server.name,
      transport: server.transport === 'http' ? 'http' : 'stdio',
      command: server.command || '',
      argsText: JSON.stringify(server.args || [], null, 2),
      envText,
      url: server.url || '',
      headersText,
      enabled: server.enabled,
    }
  } else {
    originalEnvText.value = '{}'
    originalHeadersText.value = '{}'
    mcpForm.value = emptyMcpForm()
  }
  mcpDialogVisible.value = true
}

async function saveMcp() {
  if (!canManageMcp.value) {
    ElMessage.warning('当前账号没有 MCP 管理权限')
    return
  }
  const name = mcpForm.value.name.trim()
  if (!editingMcp.value && (!/^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(name) || name.length < 2)) {
    ElMessage.warning('服务名称需以字母或数字开头，仅包含字母、数字、下划线、点、冒号、短横线')
    return
  }

  const transport = mcpForm.value.transport
  const payload: Record<string, unknown> = {
    transport,
    enabled: mcpForm.value.enabled,
  }

  if (transport === 'stdio') {
    const command = mcpForm.value.command.trim()
    if (!command) {
      ElMessage.warning('stdio 传输需要填写命令')
      return
    }
    const args = parseStringArray(mcpForm.value.argsText, 'Args')
    if (!args) return
    payload.command = command
    payload.args = args

    const envUnchanged =
      editingMcp.value !== null &&
      editingMcp.value.transport === 'stdio' &&
      mcpForm.value.envText === originalEnvText.value
    if (!envUnchanged) {
      const env = parseStringRecord(mcpForm.value.envText, 'Env')
      if (!env || !checkSecretRefs(env, 'Env')) return
      if (hasMaskedValue(env)) {
        ElMessage.warning('Env 含服务端掩码 ***，请重新填写完整值或恢复原配置以保留现有密钥')
        return
      }
      payload.env = env
    }
  } else {
    const url = mcpForm.value.url.trim()
    if (!/^https?:\/\//i.test(url)) {
      ElMessage.warning('HTTP 传输的 URL 必须使用 http 或 https 协议')
      return
    }
    payload.url = url

    const headersUnchanged =
      editingMcp.value !== null &&
      editingMcp.value.transport === 'http' &&
      mcpForm.value.headersText === originalHeadersText.value
    if (!headersUnchanged) {
      const headers = parseStringRecord(mcpForm.value.headersText, 'Headers')
      if (!headers || !checkSecretRefs(headers, 'Headers')) return
      if (hasMaskedValue(headers)) {
        ElMessage.warning('Headers 含服务端掩码 ***，请重新填写完整值或恢复原配置以保留现有密钥')
        return
      }
      payload.headers = headers
    }
  }

  savingMcp.value = true
  try {
    if (editingMcp.value) {
      await mcpApi.update(editingMcp.value.id, payload)
      ElMessage.success('MCP 服务已更新')
    } else {
      await mcpApi.create({ ...payload, name })
      ElMessage.success('MCP 服务已创建')
    }
    mcpDialogVisible.value = false
    await loadMcp()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingMcp.value = false
  }
}

async function toggleMcp(server: MCPServerInfo) {
  if (!canManageMcp.value) return
  mcpTogglingId.value = server.id
  try {
    const updated = await mcpApi.update(server.id, { enabled: !server.enabled })
    const index = mcpServers.value.findIndex((item) => item.id === updated.id)
    if (index >= 0) mcpServers.value[index] = updated
    ElMessage.success(updated.enabled ? 'MCP 服务已启用' : 'MCP 服务已停用')
  } catch {
    // interceptor surfaces the backend message
  } finally {
    mcpTogglingId.value = ''
  }
}

async function deleteMcp(server: MCPServerInfo) {
  if (!canManageMcp.value) return
  try {
    await ElMessageBox.confirm(
      `确定删除 MCP 服务「${server.name}」及其已注册工具吗？该操作不可撤销。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await mcpApi.remove(server.id)
    ElMessage.success('MCP 服务已删除')
    if (mcpToolsServer.value?.id === server.id) mcpToolsVisible.value = false
    await loadMcp()
    await load()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function syncMcp(server: MCPServerInfo) {
  if (!canManageMcp.value) return
  mcpSyncingId.value = server.id
  try {
    const result = await mcpApi.sync(server.id)
    syncResult.value = result
    syncResultVisible.value = true
    ElMessage.success(
      `同步完成：新增 ${result.created} · 更新 ${result.updated} · 未变 ${result.unchanged} · 停用 ${result.disabled}`,
    )
    await Promise.all([loadMcp(), load()])
  } catch {
    await loadMcp()
  } finally {
    mcpSyncingId.value = ''
  }
}

async function openMcpTools(server: MCPServerInfo) {
  mcpToolsServer.value = server
  mcpToolsVisible.value = true
  mcpToolsLoading.value = true
  mcpToolsError.value = ''
  mcpTools.value = []
  try {
    mcpTools.value = await mcpApi.tools(server.id)
  } catch (err) {
    mcpToolsError.value = describeError(err)
  } finally {
    mcpToolsLoading.value = false
  }
}

onMounted(() => {
  void load()
  if (canManageMcp.value) void loadMcp()
})
</script>

<template>
  <div class="page-stack">
    <PageHeader title="工具管理" subtitle="工具注册表、风险与权限治理、声明式 HTTP 工具与 MCP 服务">
      <template #actions>
        <el-button
          :loading="activeTab === 'mcp' ? mcpLoading : loading"
          @click="refreshCurrent"
        >
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button
          v-if="activeTab === 'tools' && canWrite"
          type="primary"
          :icon="Plus"
          @click="openCreate"
        >
          新建 HTTP 工具
        </el-button>
        <el-button
          v-if="activeTab === 'mcp' && canManageMcp"
          type="primary"
          :icon="Plus"
          @click="openMcpDialog()"
        >
          新建 MCP 服务
        </el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="activeTab === 'tools'"
      type="info"
      :closable="false"
      show-icon
      title="原生工具由代码/插件注册（重启或同步时自动入库），此处仅支持查看、启停与测试；HTTP 工具可在此创建和编辑。"
    />

    <el-alert v-if="activeTab === 'tools' && error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <div class="card">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="工具注册表" name="tools">
          <div class="toolbar filter-bar">
        <div class="filter-group">
          <el-select v-model="sourceFilter" size="small" class="filter-select">
            <el-option label="全部来源" value="all" />
            <el-option label="原生注册" value="native" />
            <el-option label="HTTP 声明式" value="http" />
          </el-select>
          <el-select v-model="enabledFilter" size="small" class="filter-select">
            <el-option label="全部状态" value="all" />
            <el-option label="已启用" value="enabled" />
            <el-option label="已停用" value="disabled" />
          </el-select>
        </div>
        <span class="muted">共 {{ filteredTools.length }} 个工具</span>
      </div>

      <el-table v-loading="loading" :data="filteredTools" row-key="id" size="small">
        <el-table-column label="名称" min-width="170">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span class="muted">{{ row.display_name || '-' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ sourceLabel(row.source_type) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="风险" width="94">
          <template #default="{ row }">
            <StatusTag :status="row.risk_level" />
          </template>
        </el-table-column>
        <el-table-column label="状态" width="94">
          <template #default="{ row }">
            <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
          </template>
        </el-table-column>
        <el-table-column label="权限范围" min-width="180">
          <template #default="{ row }">
            <div v-if="row.permission_scope.length" class="tag-row">
              <el-tag
                v-for="code in row.permission_scope"
                :key="code"
                size="small"
                effect="plain"
                class="mono"
              >
                {{ code }}
              </el-tag>
            </div>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="需审批" width="88" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.requires_approval" size="small" type="warning" effect="plain">
              是
            </el-tag>
            <span v-else class="muted">否</span>
          </template>
        </el-table-column>
        <el-table-column label="当前版本" width="100">
          <template #default="{ row }">
            <span class="mono">v{{ row.current_version }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="268" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" @click="openDetail(row)">详情</el-button>
              <el-button v-if="canWrite" link @click="openTest(row)">测试</el-button>
              <el-button
                v-if="canWrite && row.source_type === 'http'"
                link
                type="primary"
                @click="openEdit(row)"
              >
                编辑
              </el-button>
              <el-button v-if="canWrite" link @click="toggleEnabled(row)">
                {{ row.enabled ? '停用' : '启用' }}
              </el-button>
              <el-button
                v-if="canWrite && row.source_type === 'http'"
                link
                type="danger"
                @click="removeTool(row)"
              >
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无工具" :image-size="80" />
        </template>
      </el-table>
        </el-tab-pane>

        <el-tab-pane v-if="canManageMcp" label="MCP 服务" name="mcp">
          <el-alert
            v-if="mcpError"
            type="error"
            :closable="false"
            show-icon
            :title="mcpError"
            class="gap"
          >
            <el-button link type="primary" @click="loadMcp">重试</el-button>
          </el-alert>

          <div class="toolbar filter-bar">
            <span class="muted">共 {{ mcpServers.length }} 个 MCP 服务</span>
            <span class="muted">同步会从远端发现工具并写入工具注册表</span>
          </div>

          <el-table v-loading="mcpLoading" :data="mcpServers" row-key="id" size="small">
            <el-table-column label="名称" min-width="150">
              <template #default="{ row }">
                <div class="name-cell">
                  <span class="name">{{ row.name }}</span>
                  <span class="muted mono">{{ shortId(row.id, 12) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="传输" width="90">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ transportLabel(row.transport) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="命令 / URL" min-width="230">
              <template #default="{ row }">
                <div v-if="row.transport === 'http'" class="mcp-url mono">
                  {{ row.url || '-' }}
                </div>
                <div v-else class="name-cell">
                  <span class="mono">{{ row.command || '-' }}</span>
                  <span v-if="row.args.length" class="muted mono mcp-args">
                    {{ row.args.join(' ') }}
                  </span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="94">
              <template #default="{ row }">
                <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
              </template>
            </el-table-column>
            <el-table-column label="最近同步" width="166">
              <template #default="{ row }">{{ formatDate(row.last_sync_at) }}</template>
            </el-table-column>
            <el-table-column label="最近错误" min-width="190">
              <template #default="{ row }">
                <span v-if="row.last_error" class="mcp-error">{{ row.last_error }}</span>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="298" fixed="right">
              <template #default="{ row }">
                <div class="row-actions">
                  <el-button
                    link
                    type="primary"
                    :loading="mcpSyncingId === row.id"
                    @click="syncMcp(row)"
                  >
                    同步
                  </el-button>
                  <el-button link @click="openMcpTools(row)">工具</el-button>
                  <el-button link type="primary" @click="openMcpDialog(row)">编辑</el-button>
                  <el-button
                    link
                    :loading="mcpTogglingId === row.id"
                    @click="toggleMcp(row)"
                  >
                    {{ row.enabled ? '停用' : '启用' }}
                  </el-button>
                  <el-button link type="danger" @click="deleteMcp(row)">删除</el-button>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无 MCP 服务" :image-size="80" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-drawer
      v-model="detailVisible"
      size="600px"
      :title="detailTool ? detailTool.name : '工具详情'"
    >
      <template v-if="detailTool">
        <div class="detail-head">
          <div class="name-cell">
            <span class="detail-name">{{ detailTool.display_name || detailTool.name }}</span>
            <span class="muted mono">{{ detailTool.id }}</span>
          </div>
          <div class="detail-tags">
            <StatusTag :status="detailTool.enabled ? 'enabled' : 'disabled'" />
            <StatusTag :status="detailTool.risk_level" />
          </div>
        </div>

        <div class="meta-grid">
          <div><span class="muted">来源</span><span>{{ sourceLabel(detailTool.source_type) }}</span></div>
          <div><span class="muted">当前版本</span><span class="mono">v{{ detailTool.current_version }}</span></div>
          <div><span class="muted">版本标识</span><span class="mono">{{ detailTool.version }}</span></div>
          <div><span class="muted">超时</span><span>{{ detailTool.timeout_seconds }} s</span></div>
          <div>
            <span class="muted">需审批</span>
            <el-tag v-if="detailTool.requires_approval" size="small" type="warning" effect="plain">是</el-tag>
            <span v-else>否</span>
          </div>
          <div><span class="muted">更新时间</span><span>{{ formatDate(detailTool.updated_at) }}</span></div>
          <div><span class="muted">创建人</span><span class="mono">{{ shortId(detailTool.created_by, 12) }}</span></div>
        </div>

        <p v-if="detailTool.description" class="description">{{ detailTool.description }}</p>

        <div class="block">
          <div class="block-label">权限范围</div>
          <div v-if="detailTool.permission_scope.length" class="tag-row">
            <el-tag
              v-for="code in detailTool.permission_scope"
              :key="code"
              size="small"
              effect="plain"
              class="mono"
            >
              {{ code }}
            </el-tag>
          </div>
          <span v-else class="muted">未限定</span>
        </div>

        <div v-if="detailTool.sensitive_fields.length" class="block">
          <div class="block-label">敏感字段</div>
          <div class="tag-row">
            <el-tag
              v-for="field in detailTool.sensitive_fields"
              :key="field"
              size="small"
              type="danger"
              effect="plain"
            >
              {{ field }}
            </el-tag>
          </div>
        </div>

        <div class="block">
          <div class="block-label">输入 Schema</div>
          <JsonBlock :value="detailTool.input_schema" max-height="240px" />
        </div>
        <div class="block">
          <div class="block-label">输出 Schema</div>
          <JsonBlock :value="detailTool.output_schema" max-height="200px" />
        </div>
        <div class="block">
          <div class="block-label">执行配置</div>
          <JsonBlock :value="detailTool.config" max-height="280px" />
        </div>
      </template>
    </el-drawer>

    <el-dialog
      v-model="dialogVisible"
      :title="editingTool ? '编辑 HTTP 工具' : '新建 HTTP 工具'"
      width="680px"
    >
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="仅用于声明式 HTTP 工具；原生工具由代码/插件注册，不能在此创建。"
        class="dialog-alert"
      />
      <el-form label-position="top" @submit.prevent="saveTool">
        <div class="grid-2">
          <el-form-item label="工具名称" required>
            <el-input
              v-model="form.name"
              :disabled="Boolean(editingTool)"
              maxlength="120"
              placeholder="如 weather.query"
            />
            <div v-if="editingTool" class="inline-hint muted">工具名称与来源创建后不可修改。</div>
          </el-form-item>
          <el-form-item label="显示名称">
            <el-input v-model="form.display_name" maxlength="160" placeholder="用于界面展示" />
          </el-form-item>
        </div>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <div class="grid-3">
          <el-form-item label="风险等级">
            <el-select v-model="form.risk_level" class="full">
              <el-option
                v-for="level in riskLevels"
                :key="level.value"
                :label="level.label"
                :value="level.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number
              v-model="form.timeout_seconds"
              :min="1"
              :max="600"
              class="full"
            />
          </el-form-item>
          <el-form-item label="状态">
            <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" />
          </el-form-item>
        </div>
        <el-form-item label="权限范围">
          <el-select
            v-model="form.permission_scope"
            multiple
            filterable
            allow-create
            default-first-option
            class="full"
            placeholder="执行该工具所需的权限码，如 tools:execute"
          >
            <el-option
              v-for="code in permissionAutocomplete"
              :key="code"
              :label="code"
              :value="code"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="需要人工审批">
          <el-switch v-model="form.requires_approval" active-text="是" inactive-text="否" />
        </el-form-item>

        <div class="section-label">HTTP 配置</div>
        <div class="grid-2">
          <el-form-item label="Method">
            <el-select v-model="form.method" class="full">
              <el-option v-for="method in methods" :key="method" :label="method" :value="method" />
            </el-select>
          </el-form-item>
          <el-form-item label="Body Mode">
            <el-select v-model="form.body_mode" class="full">
              <el-option label="JSON" value="json" />
              <el-option label="Form" value="form" />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item label="URL" required>
          <el-input v-model="form.url" placeholder="https://api.example.com/v1/resource" />
        </el-form-item>
        <el-form-item label="Headers（JSON）">
          <el-input
            v-model="form.headersText"
            type="textarea"
            :rows="3"
            class="mono-input"
            placeholder='{"Authorization": "Bearer ${API_TOKEN}"}'
          />
          <div class="inline-hint muted">
            敏感请求头只能使用 <span class="mono">${ENV_VAR}</span> 形式引用环境变量，禁止字面量密钥。
          </div>
        </el-form-item>
        <el-form-item label="Query（JSON）">
          <el-input
            v-model="form.queryText"
            type="textarea"
            :rows="3"
            class="mono-input"
            placeholder='{"limit": "10"}'
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTool">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="testVisible" title="工具测试" width="680px">
      <template v-if="testTool">
        <div class="test-head">
          <span class="test-name">{{ testTool.display_name || testTool.name }}</span>
          <StatusTag :status="testTool.risk_level" />
          <el-tag v-if="testTool.requires_approval" size="small" type="warning" effect="plain">
            需审批
          </el-tag>
        </div>
        <el-form label-position="top" @submit.prevent="runTest">
          <el-form-item label="参数（JSON 对象）">
            <el-input
              v-model="testArgsText"
              type="textarea"
              :rows="5"
              class="mono-input"
              placeholder='{"query": "example"}'
            />
          </el-form-item>
        </el-form>
        <div class="test-actions">
          <el-button type="primary" :loading="testing" @click="runTest">执行测试</el-button>
        </div>

        <el-skeleton v-if="testing" :rows="4" animated />

        <el-alert
          v-else-if="testRunError"
          type="error"
          :closable="false"
          show-icon
          :title="testRunError"
          class="dialog-alert"
        />

        <template v-else-if="testResult">
          <div class="result-head">
            <el-tag :type="testResult.ok ? 'success' : 'danger'" effect="light">
              {{ testResult.ok ? '通过' : '失败' }}
            </el-tag>
            <span class="muted">状态：<span class="mono">{{ testResult.status }}</span></span>
            <span class="muted">耗时：{{ testResult.duration_ms }} ms</span>
          </div>
          <el-alert
            v-if="testResult.error"
            type="error"
            :closable="false"
            show-icon
            :title="testResult.error"
            class="dialog-alert"
          />
          <div class="block">
            <div class="block-label">脱敏参数</div>
            <JsonBlock :value="testResult.masked_arguments" max-height="140px" />
          </div>
          <div class="block">
            <div class="block-label">返回结果</div>
            <JsonBlock :value="testResult.result" max-height="240px" />
          </div>
        </template>
      </template>
      <template #footer>
        <el-button @click="testVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="mcpDialogVisible"
      :title="editingMcp ? '编辑 MCP 服务' : '新建 MCP 服务'"
      width="640px"
    >
      <el-alert
        v-if="editingMcp"
        type="warning"
        :closable="false"
        show-icon
        title="服务端会把 env/headers 中的密钥值掩码为 ***。未修改该字段保存时会保留原值；若修改该字段，请重新填写所有完整值。"
        class="dialog-alert"
      />
      <el-form label-position="top" @submit.prevent="saveMcp">
        <div class="grid-2">
          <el-form-item label="名称" required>
            <el-input
              v-model="mcpForm.name"
              :disabled="Boolean(editingMcp)"
              maxlength="120"
              placeholder="如 filesystem"
            />
            <div v-if="editingMcp" class="inline-hint muted">服务名称创建后不可修改。</div>
          </el-form-item>
          <el-form-item label="传输方式">
            <el-select v-model="mcpForm.transport" class="full">
              <el-option label="stdio（本地进程）" value="stdio" />
              <el-option label="HTTP（远程服务）" value="http" />
            </el-select>
          </el-form-item>
        </div>

        <template v-if="mcpForm.transport === 'stdio'">
          <el-form-item label="命令" required>
            <el-input v-model="mcpForm.command" placeholder="npx" />
          </el-form-item>
          <el-form-item label="Args（JSON 字符串数组）">
            <el-input
              v-model="mcpForm.argsText"
              type="textarea"
              :rows="3"
              class="mono-input"
              placeholder='["-y", "@modelcontextprotocol/server-filesystem", "/data"]'
            />
          </el-form-item>
          <el-form-item label="Env（JSON 对象）">
            <el-input
              v-model="mcpForm.envText"
              type="textarea"
              :rows="3"
              class="mono-input"
              placeholder='{"API_KEY": "${TOOL_SECRET_API_KEY}"}'
            />
            <div class="inline-hint muted">
              值可以是字面量，或 <span class="mono">${TOOL_SECRET_*}</span> 形式的密钥引用；禁止其他环境变量前缀。
            </div>
          </el-form-item>
        </template>

        <template v-else>
          <el-form-item label="URL" required>
            <el-input v-model="mcpForm.url" placeholder="https://mcp.example.com/sse" />
          </el-form-item>
          <el-form-item label="Headers（JSON）">
            <el-input
              v-model="mcpForm.headersText"
              type="textarea"
              :rows="3"
              class="mono-input"
              placeholder='{"Authorization": "Bearer ${TOOL_SECRET_TOKEN}"}'
            />
            <div class="inline-hint muted">
              敏感请求头值需使用 <span class="mono">${TOOL_SECRET_*}</span> 引用。
            </div>
          </el-form-item>
        </template>

        <el-form-item label="启用">
          <el-switch v-model="mcpForm.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mcpDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingMcp" @click="saveMcp">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="syncResultVisible" title="MCP 同步结果" width="660px">
      <template v-if="syncResult">
        <div class="sync-head">
          <span class="sync-server">{{ syncResult.server_name }}</span>
          <span class="muted mono">{{ shortId(syncResult.server_id, 12) }}</span>
        </div>
        <div class="sync-stats">
          <div><span class="muted">新增</span><b>{{ syncResult.created }}</b></div>
          <div><span class="muted">更新</span><b>{{ syncResult.updated }}</b></div>
          <div><span class="muted">未变</span><b>{{ syncResult.unchanged }}</b></div>
          <div><span class="muted">停用</span><b>{{ syncResult.disabled }}</b></div>
        </div>
        <div class="block-label">发现的工具</div>
        <el-table v-if="syncResult.tools.length" :data="syncResult.tools" size="small" row-key="id">
          <el-table-column label="名称" min-width="180">
            <template #default="{ row }">
              <div class="name-cell">
                <span class="name">{{ row.name }}</span>
                <span class="muted">{{ row.display_name || '-' }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="风险" width="96">
            <template #default="{ row }"><StatusTag :status="row.risk_level" /></template>
          </el-table-column>
          <el-table-column label="状态" width="96">
            <template #default="{ row }">
              <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
            </template>
          </el-table-column>
          <el-table-column label="版本" width="80">
            <template #default="{ row }"><span class="mono">v{{ row.current_version }}</span></template>
          </el-table-column>
          <template #empty>
            <el-empty description="未发现工具" :image-size="60" />
          </template>
        </el-table>
        <el-empty v-else description="未发现工具" :image-size="60" />
      </template>
      <template #footer>
        <el-button @click="syncResultVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="mcpToolsVisible"
      size="680px"
      :title="mcpToolsServer ? `MCP 工具 · ${mcpToolsServer.name}` : 'MCP 工具'"
    >
      <div class="toolbar drawer-toolbar">
        <span class="muted">共 {{ mcpTools.length }} 个工具</span>
        <el-button
          size="small"
          :loading="mcpToolsLoading"
          @click="mcpToolsServer && openMcpTools(mcpToolsServer)"
        >
          刷新
        </el-button>
      </div>

      <el-alert
        v-if="mcpToolsError"
        type="error"
        :closable="false"
        show-icon
        :title="mcpToolsError"
        class="gap"
      />

      <el-skeleton v-if="mcpToolsLoading" :rows="6" animated />

      <el-table v-else-if="mcpTools.length" :data="mcpTools" row-key="id" size="small">
        <el-table-column label="名称" min-width="180">
          <template #default="{ row }">
            <div class="name-cell">
              <span class="name">{{ row.name }}</span>
              <span class="muted">{{ row.display_name || '-' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="风险" width="96">
          <template #default="{ row }"><StatusTag :status="row.risk_level" /></template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
          </template>
        </el-table-column>
        <el-table-column label="权限范围" min-width="150">
          <template #default="{ row }">
            <div v-if="row.permission_scope.length" class="tag-row">
              <el-tag
                v-for="code in row.permission_scope"
                :key="code"
                size="small"
                effect="plain"
                class="mono"
              >
                {{ code }}
              </el-tag>
            </div>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="80">
          <template #default="{ row }"><span class="mono">v{{ row.current_version }}</span></template>
        </el-table-column>
      </el-table>

      <el-empty
        v-else-if="!mcpToolsError"
        description="尚未同步工具，点击「同步」从 MCP 服务发现"
        :image-size="80"
      />
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

.filter-group {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-select {
  width: 148px;
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

.tag-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
}

.detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 14px;
}

.detail-name {
  font-size: 16px;
  font-weight: 650;
}

.detail-tags {
  display: flex;
  gap: 6px;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px 16px;
  margin-bottom: 14px;
  font-size: 12px;
}

.meta-grid > div {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.meta-grid .muted {
  font-size: 11px;
}

.description {
  margin: 0 0 14px;
  color: var(--app-muted);
  font-size: 12.5px;
  line-height: 1.7;
}

.block {
  margin-bottom: 14px;
}

.block-label {
  font-size: 11.5px;
  font-weight: 600;
  color: var(--app-muted);
  margin-bottom: 6px;
}

.full {
  width: 100%;
}

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0 16px;
}

.grid-3 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 0 16px;
}

.inline-hint {
  font-size: 11.5px;
  line-height: 1.5;
  margin-top: 4px;
}

.section-label {
  font-size: 12.5px;
  font-weight: 650;
  margin: 6px 0 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--app-border);
}

.mono-input :deep(textarea) {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}

.dialog-alert {
  margin-bottom: 12px;
}

.test-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.test-name {
  font-weight: 650;
}

.test-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 12px;
}

.result-head {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.gap {
  margin-top: 12px;
}

.mcp-url {
  font-size: 11.5px;
  word-break: break-all;
  color: var(--app-muted);
}

.mcp-args {
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mcp-error {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 11.5px;
  line-height: 1.45;
  color: var(--el-color-danger);
  word-break: break-word;
}

.sync-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 12px;
}

.sync-server {
  font-size: 15px;
  font-weight: 650;
}

.sync-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 18px;
}

.sync-stats > div {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: #f8fafc;
}

.sync-stats b {
  font-size: 20px;
  font-weight: 680;
  letter-spacing: -0.3px;
}

.drawer-toolbar {
  margin-bottom: 12px;
}
</style>
