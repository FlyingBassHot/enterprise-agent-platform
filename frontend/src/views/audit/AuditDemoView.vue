<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  CopyDocument,
  Download,
  Link,
  Refresh,
  Upload,
  VideoPlay,
} from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import MetricCard from '@/components/MetricCard.vue'
import { agentsApi, artifactsApi, auditApi, executionsApi } from '@/api/platform'
import {
  useExecutionStream,
  type ExecutionEventDto,
} from '@/composables/useExecutionStream'
import { useAuthStore } from '@/stores/auth'
import type {
  AgentInfo,
  ArtifactInfo,
  AuditReportResponse,
  AuditRulesResponse,
  ExecutionEventInfo,
  ExecutionInfo,
  ToolCallRecord,
} from '@/types/api'
import { formatBytes, formatDate, shortId } from '@/utils/format'

const AUDIT_PROMPT = [
  '请对上传的交易明细文件执行财务异常审计分析：',
  '1. 调用 analyze_transactions 完成确定性规则检测（重复付款、大额交易、短期重复付款、数据质量）。',
  '2. 调用 generate_audit_report 生成 Markdown 审计报告，包含规则命中汇总、重点异常、数据质量问题与整改建议。',
  '3. 用中文总结异常总数、主要风险和下一步建议。',
].join('\n')

const ACTIVE_STATUSES = ['pending', 'running', 'waiting_approval']
const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled']

interface AnomalyRow {
  rule: string
  severity: string
  rowNumber: string
  vendor: string
  amount: number | null
  date: string
  description: string
}

interface QualityRow {
  rowNumber: string
  field: string
  issue: string
  message: string
  value: string
}

interface ProgressStep {
  title: string
  description: string
  state: 'wait' | 'process' | 'finish' | 'error'
}

const router = useRouter()
const auth = useAuthStore()

const rulesData = ref<AuditRulesResponse | null>(null)
const agents = ref<AgentInfo[]>([])
const rulesLoading = ref(false)
const pageError = ref('')

const sampleDownloading = ref(false)
const sampleError = ref('')

const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const uploadedArtifact = ref<ArtifactInfo | null>(null)
const uploading = ref(false)
const starting = ref(false)
const runError = ref('')
const settling = ref(false)

const execution = ref<ExecutionInfo | null>(null)
const runStatus = ref('')
const events = ref<ExecutionEventInfo[]>([])
const toolCalls = ref<ToolCallRecord[]>([])
const toolCallsError = ref('')
const toolCallsLoading = ref(false)
const assistantText = ref('')
const streamSeq = ref(0)

const report = ref<AuditReportResponse | null>(null)
const reportLoading = ref(false)
const reportError = ref('')

const {
  error: streamError,
  start: startStream,
  stop: stopStream,
} = useExecutionStream({ onEvent: handleStreamEvent })

const publishedAgents = computed(() => agents.value.filter((item) => item.status === 'published'))

function toolsOf(agent: AgentInfo): string[] {
  const tools = agent.draft_config?.tools
  return Array.isArray(tools) ? tools : []
}

const selectedAgent = computed(() => {
  const candidates = publishedAgents.value
  return (
    candidates.find((item) => item.name === 'audit-analyst') ||
    candidates.find((item) => toolsOf(item).includes('analyze_transactions')) ||
    null
  )
})

const ruleNameMap = computed(() => {
  const map: Record<string, string> = {}
  for (const rule of rulesData.value?.rules || []) map[rule.rule] = rule.name
  return map
})

const isActive = computed(() => ACTIVE_STATUSES.includes(runStatus.value))

const finished = computed(() => TERMINAL_STATUSES.includes(runStatus.value))

const failed = computed(() => runStatus.value === 'failed' || runStatus.value === 'cancelled')

const canRun = computed(() => auth.has('executions:write'))

const canStart = computed(
  () =>
    canRun.value &&
    Boolean(selectedAgent.value) &&
    Boolean(file.value) &&
    !uploading.value &&
    !starting.value &&
    !settling.value &&
    !isActive.value,
)

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

function unwrap(value: unknown): unknown {
  if (typeof value === 'string') {
    try {
      return unwrap(JSON.parse(value) as unknown)
    } catch {
      return value
    }
  }
  if (isRecord(value) && !('anomalies' in value) && 'output' in value) {
    return unwrap(value.output)
  }
  return value
}

function asText(value: unknown): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

function formatAmount(value: number | null): string {
  if (value === null) return '-'
  return `¥${value.toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`
}

const analysisToolCall = computed(
  () => toolCalls.value.find((item) => item.tool_name === 'analyze_transactions') || null,
)

function analysisFromEvents(): Record<string, unknown> | null {
  for (let index = events.value.length - 1; index >= 0; index -= 1) {
    const event = events.value[index]
    if (
      event.type === 'tool_call_completed' &&
      event.payload.tool_name === 'analyze_transactions'
    ) {
      const candidate = unwrap(event.payload.output ?? event.payload.result)
      if (isRecord(candidate) && Array.isArray(candidate.anomalies)) return candidate
    }
  }
  return null
}

function analysisFromOutput(): Record<string, unknown> | null {
  const raw = execution.value?.output_text
  if (!raw) return null
  const parsed = unwrap(raw)
  if (isRecord(parsed) && Array.isArray(parsed.anomalies)) return parsed
  return null
}

const analysis = computed<Record<string, unknown> | null>(() => {
  if (analysisToolCall.value) {
    const parsed = unwrap(analysisToolCall.value.result)
    if (isRecord(parsed)) {
      const nested = unwrap(parsed)
      if (isRecord(nested) && Array.isArray(nested.anomalies)) return nested
    }
  }
  return analysisFromEvents() || analysisFromOutput()
})

const summary = computed<Record<string, unknown>>(() => {
  const fromAnalysis = analysis.value?.summary
  if (isRecord(fromAnalysis)) return fromAnalysis
  return report.value?.summary || {}
})

function summaryNumber(key: string): number | null {
  const value = summary.value[key]
  if (typeof value === 'number') return value
  if (typeof value === 'string' && value.trim() !== '' && !Number.isNaN(Number(value))) {
    return Number(value)
  }
  return null
}

const byRule = computed<Record<string, unknown>>(() =>
  isRecord(summary.value.by_rule) ? summary.value.by_rule : {},
)

const anomalies = computed<AnomalyRow[]>(() => {
  const raw = analysis.value?.anomalies
  if (!Array.isArray(raw)) return []
  return raw.filter(isRecord).map((item) => ({
    rule: asText(item.rule),
    severity: asText(item.severity) || 'medium',
    rowNumber: asText(item.row_number) || '-',
    vendor: asText(item.vendor) || '-',
    amount:
      typeof item.amount === 'number'
        ? item.amount
        : typeof item.amount === 'string' && Number.isFinite(Number(item.amount))
          ? Number(item.amount)
          : null,
    date: asText(item.date) || '-',
    description: asText(item.description) || '-',
  }))
})

const qualityIssues = computed<QualityRow[]>(() => {
  const raw = analysis.value?.quality_issues
  if (!Array.isArray(raw)) return []
  return raw.filter(isRecord).map((item) => ({
    rowNumber: asText(item.row_number) || '-',
    field: asText(item.field) || '-',
    issue: asText(item.issue) || '-',
    message: asText(item.message) || '-',
    value: asText(item.value) || '-',
  }))
})

const reportFromTool = computed<AuditReportResponse | null>(() => {
  const record = toolCalls.value.find((item) => item.tool_name === 'generate_audit_report')
  if (!record) return null
  const value = unwrap(record.result)
  if (!isRecord(value) || typeof value.markdown !== 'string') return null
  return {
    format: asText(value.format) || 'markdown',
    markdown: value.markdown,
    truncated: Boolean(value.truncated),
    summary: isRecord(value.summary) ? value.summary : {},
  }
})

const displayOutput = computed(() => execution.value?.output_text || assistantText.value)

function toolState(name: string): 'wait' | 'process' | 'finish' | 'error' {
  const record = toolCalls.value.find((item) => item.tool_name === name)
  if (record) {
    return record.status === 'success' ? 'finish' : 'error'
  }
  const completed = [...events.value]
    .reverse()
    .find((item) => item.type === 'tool_call_completed' && item.payload.tool_name === name)
  if (completed) {
    const ok = completed.payload.ok
    const status = asText(completed.payload.status)
    return ok === false || (status && status !== 'success') ? 'error' : 'finish'
  }
  if (
    events.value.some(
      (item) => item.type === 'tool_call_started' && item.payload.tool_name === name,
    )
  ) {
    return 'process'
  }
  return 'wait'
}

const steps = computed<ProgressStep[]>(() => {
  let analyze = toolState('analyze_transactions')
  if (analysis.value) analyze = 'finish'
  let reportTool = toolState('generate_audit_report')
  if (report.value) reportTool = 'finish'
  else if (finished.value && reportError.value) reportTool = 'error'
  let completion: ProgressStep['state'] = 'wait'
  if (runStatus.value === 'completed') completion = 'finish'
  else if (failed.value) completion = 'error'
  else if (isActive.value) completion = 'process'
  return [
    {
      title: '上传交易文件',
      description: uploadedArtifact.value?.filename || '下载示例或选择本地文件',
      state: uploadedArtifact.value ? 'finish' : uploading.value ? 'process' : 'wait',
    },
    {
      title: '创建执行',
      description: execution.value ? `执行 ${shortId(execution.value.id, 10)}` : '等待发起',
      state: execution.value ? 'finish' : starting.value ? 'process' : 'wait',
    },
    { title: '规则检测', description: 'analyze_transactions', state: analyze },
    { title: '生成报告', description: 'generate_audit_report', state: reportTool },
    { title: '完成', description: '汇总结果与报告', state: completion },
  ]
})

const activeStep = computed(() => {
  const pendingIndex = steps.value.findIndex((item) => item.state !== 'finish')
  return pendingIndex === -1 ? steps.value.length : pendingIndex
})

const stepProcessStatus = computed(() => (failed.value ? 'error' : 'process'))

function ruleOptionEntries(options: Record<string, unknown>): [string, string][] {
  return Object.entries(options).map(([key, value]) => [key, asText(value)])
}

async function loadRules() {
  rulesLoading.value = true
  pageError.value = ''
  try {
    rulesData.value = await auditApi.rules()
  } catch (err) {
    pageError.value = describeError(err)
    rulesData.value = null
  } finally {
    rulesLoading.value = false
  }
}

async function loadAgents() {
  try {
    agents.value = await agentsApi.list()
  } catch {
    agents.value = []
  }
}

function pickFile() {
  fileInput.value?.click()
}

function onFileChange(event: Event) {
  const target = event.target as HTMLInputElement
  const selected = target.files?.[0] || null
  target.value = ''
  runError.value = ''
  sampleError.value = ''
  if (selected && !/\.(xlsx|xls|csv)$/i.test(selected.name)) {
    runError.value = '仅支持 .xlsx / .xls / .csv 交易明细文件'
    file.value = null
    return
  }
  file.value = selected
}

async function downloadSample() {
  sampleDownloading.value = true
  sampleError.value = ''
  try {
    const token = localStorage.getItem('access_token') || ''
    const response = await fetch(auditApi.sampleUrl, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!response.ok) {
      let message = `示例文件下载失败（HTTP ${response.status}）`
      try {
        const payload = (await response.json()) as { message?: string }
        if (payload?.message) message = payload.message
      } catch {
        // keep the HTTP fallback message
      }
      throw new Error(message)
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'audit_sample.xlsx'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    URL.revokeObjectURL(url)
    file.value = new File([blob], 'audit_sample.xlsx', {
      type:
        blob.type ||
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    runError.value = ''
    ElMessage.success('示例文件已下载，并设为当前分析文件')
  } catch (err) {
    sampleError.value = describeError(err)
  } finally {
    sampleDownloading.value = false
  }
}

function appendEvent(event: ExecutionEventDto): boolean {
  if (event.seq > streamSeq.value) streamSeq.value = event.seq
  if (events.value.some((item) => item.seq === event.seq)) return false
  events.value.push({
    seq: event.seq,
    execution_id: event.execution_id || execution.value?.id || '',
    type: event.type,
    payload: event.payload || {},
    created_at: event.created_at || new Date().toISOString(),
  })
  events.value.sort((a, b) => a.seq - b.seq)
  return true
}

function handleStreamEvent(event: ExecutionEventDto) {
  if (!execution.value) return
  if (event.execution_id && event.execution_id !== execution.value.id) return
  if (!appendEvent(event)) return
  const payload = event.payload || {}
  switch (event.type) {
    case 'execution_started':
      runStatus.value = 'running'
      break
    case 'message_delta': {
      const delta = asText(payload.delta ?? payload.content)
      if (delta) assistantText.value += delta
      break
    }
    case 'message_completed': {
      const content = asText(payload.content)
      if (content) assistantText.value = content
      break
    }
    case 'tool_call_started':
    case 'tool_call_completed':
      if (!finished.value) runStatus.value = 'running'
      break
    case 'approval_required':
      runStatus.value = 'waiting_approval'
      break
    case 'approval_resolved':
      runStatus.value = 'running'
      break
    case 'execution_completed':
      runStatus.value = 'completed'
      void finishRun(true)
      break
    case 'execution_failed':
      runStatus.value = 'failed'
      runError.value = asText(payload.error) || '审计执行失败'
      void finishRun(false)
      break
    case 'execution_cancelled':
      runStatus.value = 'cancelled'
      void finishRun(false)
      break
  }
}

function reconnectStream() {
  if (!execution.value) return
  stopStream()
  startStream(execution.value.id, streamSeq.value)
}

async function loadToolCalls(targetId: string) {
  toolCallsLoading.value = true
  toolCallsError.value = ''
  let lastError: unknown = null
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const records = await executionsApi.toolCalls(targetId)
      if (execution.value?.id !== targetId) return
      toolCalls.value = records
      toolCallsLoading.value = false
      return
    } catch (err) {
      lastError = err
      if (attempt < 2) await delay(400)
    }
  }
  if (execution.value?.id !== targetId) return
  toolCalls.value = []
  toolCallsError.value = describeError(lastError)
  toolCallsLoading.value = false
}

async function loadRunData() {
  const id = execution.value?.id
  if (!id) return
  try {
    const detail = await executionsApi.get(id)
    if (execution.value?.id !== id) return
    execution.value = detail
    runStatus.value = detail.status
  } catch (err) {
    if (execution.value?.id === id) runError.value = describeError(err)
  }
  if (execution.value?.id !== id) return
  await loadToolCalls(id)
}

async function requestReport() {
  const artifactId = uploadedArtifact.value?.id || asText(analysis.value?.artifact_id)
  const runId = execution.value?.id
  if (!artifactId) {
    reportError.value = '缺少交易文件 artifact_id，无法生成报告'
    return
  }
  reportLoading.value = true
  reportError.value = ''
  try {
    const generated = await auditApi.report({ artifact_id: artifactId, max_anomalies: 20 })
    if (execution.value?.id !== runId) return
    report.value = generated
  } catch (err) {
    if (execution.value?.id !== runId) return
    const fallback = reportFromTool.value
    if (fallback) {
      report.value = fallback
    } else {
      report.value = null
      reportError.value = describeError(err)
    }
  } finally {
    reportLoading.value = false
  }
}

async function finishRun(completed: boolean) {
  if (settling.value) return
  settling.value = true
  try {
    stopStream()
    await loadRunData()
    if (completed) await requestReport()
  } finally {
    settling.value = false
  }
}

async function startDemo() {
  if (settling.value) return
  if (!selectedAgent.value) {
    runError.value = '未找到已发布的 audit-analyst Agent'
    return
  }
  if (!file.value) {
    runError.value = '请先选择交易明细文件'
    return
  }
  stopStream()
  runError.value = ''
  sampleError.value = ''
  toolCallsError.value = ''
  toolCallsLoading.value = false
  reportError.value = ''
  report.value = null
  toolCalls.value = []
  events.value = []
  streamSeq.value = 0
  assistantText.value = ''
  execution.value = null
  runStatus.value = ''
  uploadedArtifact.value = null
  starting.value = true
  try {
    uploading.value = true
    const artifact = await artifactsApi.upload(file.value)
    uploadedArtifact.value = artifact
    uploading.value = false
    const created = await executionsApi.create({
      agent_id: selectedAgent.value.id,
      input: AUDIT_PROMPT,
      artifact_ids: [artifact.id],
    })
    execution.value = created
    runStatus.value = created.status
    startStream(created.id, 0)
  } catch (err) {
    runError.value = describeError(err)
  } finally {
    uploading.value = false
    starting.value = false
  }
}

async function copyText(value: string) {
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本')
  }
}

function downloadReport() {
  if (!report.value?.markdown) return
  const blob = new Blob([report.value.markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `audit_report_${shortId(execution.value?.id || 'demo', 8)}.md`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

function openDetail() {
  if (execution.value) void router.push(`/executions/${execution.value.id}`)
}

function openAgents() {
  void router.push('/agents')
}

onMounted(async () => {
  await Promise.all([loadRules(), loadAgents()])
})

onBeforeUnmount(() => {
  stopStream()
})
</script>

<template>
  <div class="page-stack">
    <PageHeader
      title="审计演示"
      subtitle="确定性规则驱动的财务交易异常分析 Agent 演示"
    >
      <template #actions>
        <el-button :loading="rulesLoading" @click="loadRules">
          <el-icon><Refresh /></el-icon>
          刷新规则
        </el-button>
        <el-button v-if="execution" @click="openDetail">
          <el-icon><Link /></el-icon>
          查看执行详情
        </el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="pageError"
      type="error"
      :closable="false"
      show-icon
      :title="pageError"
    >
      <el-button link type="primary" @click="loadRules">重试</el-button>
    </el-alert>

    <div class="two-col">
      <div class="card rules-card">
        <div class="card-head">
          <div>
            <h3>审计规则</h3>
            <p class="muted">
              共 {{ rulesData?.total ?? 0 }} 条确定性规则
              <template v-if="rulesData">
                · 示例文件{{ rulesData.sample_file_available ? '可用' : '不可用' }}
              </template>
            </p>
          </div>
        </div>
        <el-skeleton v-if="rulesLoading && !rulesData" :rows="5" animated />
        <el-table v-else-if="rulesData?.rules.length" :data="rulesData.rules" size="small">
          <el-table-column label="规则" width="170">
            <template #default="{ row }">
              <div class="rule-name">{{ row.name }}</div>
              <div class="mono muted">{{ row.rule }}</div>
            </template>
          </el-table-column>
          <el-table-column label="级别" width="96">
            <template #default="{ row }">
              <StatusTag :status="row.severity" />
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="260">
            <template #default="{ row }">
              <span class="rule-desc">{{ row.description }}</span>
            </template>
          </el-table-column>
          <el-table-column label="默认参数" min-width="150">
            <template #default="{ row }">
              <div class="option-tags">
                <el-tag
                  v-for="[key, value] in ruleOptionEntries(row.default_options || {})"
                  :key="key"
                  size="small"
                  type="info"
                >
                  {{ key }}: {{ value }}
                </el-tag>
                <span
                  v-if="!ruleOptionEntries(row.default_options || {}).length"
                  class="muted"
                >
                  -
                </span>
              </div>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无审计规则" :image-size="70" />
          </template>
        </el-table>
        <el-empty v-else-if="!rulesLoading" description="暂无审计规则" :image-size="70" />
      </div>

      <div class="card run-card">
        <div class="card-head">
          <div>
            <h3>运行演示</h3>
            <p class="muted">上传交易明细，Agent 将调用审计工具并生成报告</p>
          </div>
        </div>

        <el-alert
          v-if="!selectedAgent && !rulesLoading"
          type="error"
          :closable="false"
          show-icon
          title="未找到已发布的 audit-analyst Agent，请先在 Agent 管理中发布该演示 Agent。"
        >
          <el-button
            v-if="auth.has('agents:read')"
            size="small"
            type="primary"
            plain
            @click="openAgents"
          >
            前往 Agent 管理
          </el-button>
        </el-alert>

        <div v-else-if="selectedAgent" class="agent-banner">
          <div class="agent-avatar">AA</div>
          <div class="agent-meta">
            <div class="agent-name">
              {{ selectedAgent.name }}
              <el-tag size="small" type="success" effect="light">
                v{{ selectedAgent.current_version ?? '-' }}
              </el-tag>
            </div>
            <div class="agent-desc">{{ selectedAgent.description || '审计分析 Agent' }}</div>
            <div class="agent-tools">
              <el-tag
                v-for="tool in toolsOf(selectedAgent)"
                :key="tool"
                size="small"
                type="info"
              >
                {{ tool }}
              </el-tag>
            </div>
          </div>
        </div>

        <el-alert
          v-if="!canRun"
          type="info"
          :closable="false"
          show-icon
          title="当前账号没有执行权限，可查看审计规则与示例文件，但无法运行演示。"
        />

        <div class="run-controls">
          <div class="control-row">
            <el-button
              :loading="sampleDownloading"
              :disabled="rulesData ? !rulesData.sample_file_available : false"
              @click="downloadSample"
            >
              <el-icon><Download /></el-icon>
              下载示例文件
            </el-button>
            <span class="muted">下载后可直接上传，或使用自备的 xlsx / csv 文件</span>
          </div>
          <el-alert
            v-if="rulesData && !rulesData.sample_file_available"
            type="info"
            :closable="false"
            show-icon
            title="服务器上未找到示例文件，请上传自备的 xlsx / csv 交易明细。"
          />
          <el-alert
            v-if="sampleError"
            type="error"
            :closable="false"
            show-icon
            :title="sampleError"
          />
          <div class="control-row">
            <input
              ref="fileInput"
              type="file"
              class="hidden-input"
              accept=".xlsx,.xls,.csv"
              @change="onFileChange"
            />
            <el-button :disabled="isActive" @click="pickFile">
              <el-icon><Upload /></el-icon>
              选择文件
            </el-button>
            <span v-if="file" class="file-chip">
              {{ file.name }} · {{ formatBytes(file.size) }}
            </span>
            <span v-else class="muted">未选择文件</span>
          </div>
          <el-alert
            v-if="runError"
            type="error"
            :closable="false"
            show-icon
            :title="runError"
          />
          <el-alert
            v-if="streamError && isActive"
            type="warning"
            :closable="false"
            show-icon
            :title="`事件流连接中断：${streamError}`"
          >
            <el-button size="small" @click="reconnectStream">重新连接</el-button>
          </el-alert>
          <el-alert
            v-if="runStatus === 'waiting_approval'"
            type="warning"
            :closable="false"
            show-icon
            title="工具调用正在等待人工审批，请前往执行详情处理，审批后会自动继续。"
          >
            <el-button size="small" @click="openDetail">处理审批</el-button>
          </el-alert>
          <div class="control-row">
            <el-button
              type="primary"
              :loading="starting || uploading"
              :disabled="!canStart"
              @click="startDemo"
            >
              <el-icon><VideoPlay /></el-icon>
              {{ execution ? '重新运行审计' : '开始审计分析' }}
            </el-button>
            <StatusTag v-if="runStatus" :status="runStatus" />
            <span v-if="execution" class="muted mono">
              {{ shortId(execution.id, 12) }} · {{ formatDate(execution.created_at) }}
            </span>
          </div>
        </div>

        <div class="steps">
          <el-steps
            :active="activeStep"
            finish-status="success"
            :process-status="stepProcessStatus"
            align-center
          >
            <el-step
              v-for="step in steps"
              :key="step.title"
              :title="step.title"
              :description="step.description"
            />
          </el-steps>
        </div>
      </div>
    </div>

    <template v-if="execution">
      <div class="results-head">
        <h3>审计结果</h3>
        <div class="results-actions">
          <StatusTag v-if="runStatus" :status="runStatus" />
          <el-button size="small" @click="openDetail">
            <el-icon><Link /></el-icon>
            执行详情
          </el-button>
        </div>
      </div>

      <el-alert
        v-if="toolCallsError"
        type="warning"
        :closable="false"
        show-icon
        :title="`工具调用记录加载失败：${toolCallsError}`"
      >
        <el-button
          v-if="execution"
          link
          type="primary"
          @click="loadToolCalls(execution.id)"
        >
          重试
        </el-button>
      </el-alert>

      <el-skeleton v-if="toolCallsLoading && !analysis" :rows="4" animated />

      <template v-if="analysis || report">
        <div class="metrics">
          <MetricCard
            label="交易笔数"
            :value="summaryNumber('total_transactions') ?? 0"
            accent="blue"
          />
          <MetricCard
            label="交易总金额"
            :value="formatAmount(summaryNumber('total_amount'))"
            accent="green"
          />
          <MetricCard
            label="异常数量"
            :value="summaryNumber('anomaly_count') ?? anomalies.length"
            accent="amber"
          />
          <MetricCard
            label="数据质量问题"
            :value="qualityIssues.length"
            accent="violet"
          />
        </div>

        <div v-if="Object.keys(byRule).length" class="card rule-strip">
          <span class="muted">规则命中</span>
          <div
            v-for="(count, rule) in byRule"
            :key="rule"
            class="rule-count"
          >
            <span>{{ ruleNameMap[rule] || rule }}</span>
            <strong>{{ count }}</strong>
          </div>
        </div>

        <div v-if="analysis" class="card">
          <div class="card-head">
            <h3>异常明细</h3>
            <span class="muted">共 {{ anomalies.length }} 条</span>
          </div>
          <el-table v-if="anomalies.length" :data="anomalies" size="small" max-height="460">
            <el-table-column label="规则" min-width="140">
              <template #default="{ row }">
                <div>{{ ruleNameMap[row.rule] || row.rule }}</div>
                <div class="mono muted">{{ row.rule }}</div>
              </template>
            </el-table-column>
            <el-table-column label="级别" width="96">
              <template #default="{ row }">
                <StatusTag :status="row.severity" />
              </template>
            </el-table-column>
            <el-table-column label="行号" width="80">
              <template #default="{ row }">{{ row.rowNumber }}</template>
            </el-table-column>
            <el-table-column label="供应商" min-width="140">
              <template #default="{ row }">{{ row.vendor }}</template>
            </el-table-column>
            <el-table-column label="金额" width="130" align="right">
              <template #default="{ row }">{{ formatAmount(row.amount) }}</template>
            </el-table-column>
            <el-table-column label="日期" width="110">
              <template #default="{ row }">{{ row.date }}</template>
            </el-table-column>
            <el-table-column label="摘要" min-width="200">
              <template #default="{ row }">{{ row.description }}</template>
            </el-table-column>
            <template #empty>
              <el-empty description="未检出异常" :image-size="70" />
            </template>
          </el-table>
          <el-empty v-else description="未检出异常" :image-size="70" />
        </div>

        <div v-if="analysis" class="card">
          <div class="card-head">
            <h3>数据质量问题</h3>
            <span class="muted">共 {{ qualityIssues.length }} 条</span>
          </div>
          <el-table v-if="qualityIssues.length" :data="qualityIssues" size="small" max-height="320">
            <el-table-column label="行号" width="90">
              <template #default="{ row }">{{ row.rowNumber }}</template>
            </el-table-column>
            <el-table-column label="字段" width="120">
              <template #default="{ row }">{{ row.field }}</template>
            </el-table-column>
            <el-table-column label="问题" width="170">
              <template #default="{ row }">
                <span class="mono">{{ row.issue }}</span>
              </template>
            </el-table-column>
            <el-table-column label="说明" min-width="220">
              <template #default="{ row }">{{ row.message }}</template>
            </el-table-column>
            <el-table-column label="原值" min-width="120">
              <template #default="{ row }">{{ row.value }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-else description="未发现数据质量问题" :image-size="70" />
        </div>
      </template>

      <el-alert
        v-else-if="finished && !toolCallsLoading"
        type="warning"
        :closable="false"
        show-icon
        title="未能解析结构化审计结果，以下为原始工具调用记录。"
      />

      <div v-if="displayOutput" class="card">
        <div class="card-head">
          <h3>Agent 输出</h3>
          <el-button size="small" link @click="copyText(displayOutput)">
            <el-icon><CopyDocument /></el-icon>
            复制
          </el-button>
        </div>
        <pre class="text-block">{{ displayOutput }}</pre>
      </div>

      <div v-if="report || reportError || reportLoading" class="card">
        <div class="card-head">
          <h3>审计报告</h3>
          <div class="report-actions">
            <el-button
              v-if="report"
              size="small"
              type="primary"
              plain
              @click="downloadReport"
            >
              <el-icon><Download /></el-icon>
              下载 .md
            </el-button>
            <el-button
              v-if="report"
              size="small"
              link
              @click="copyText(report.markdown)"
            >
              <el-icon><CopyDocument /></el-icon>
              复制
            </el-button>
          </div>
        </div>
        <el-alert
          v-if="reportError"
          type="error"
          :closable="false"
          show-icon
          :title="reportError"
        >
          <el-button link type="primary" @click="requestReport">重试</el-button>
        </el-alert>
        <el-skeleton v-if="reportLoading" :rows="5" animated />
        <template v-else-if="report">
          <el-alert
            v-if="report.truncated"
            type="warning"
            :closable="false"
            show-icon
            title="报告内容过长，已按长度限制截断，请结合明细数据复核。"
            class="truncated-alert"
          />
          <pre class="text-block markdown-block">{{ report.markdown }}</pre>
        </template>
      </div>

      <div v-if="!analysis && finished" class="card">
        <div class="card-head">
          <h3>原始工具记录</h3>
        </div>
        <JsonBlock :value="toolCalls" max-height="420px" />
      </div>

      <div v-if="!finished && !analysis" class="card muted-block">
        审计执行进行中，完成后将在此展示异常明细与报告。
      </div>
    </template>
  </div>
</template>

<style scoped>
.two-col {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.card-head h3 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 650;
}

.card-head p {
  margin: 4px 0 0;
  font-size: 11.5px;
}

.rule-name {
  font-size: 12.5px;
  font-weight: 600;
}

.rule-desc {
  font-size: 12px;
  color: #475569;
  line-height: 1.6;
}

.option-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.mono {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 11px;
}

.agent-banner {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px;
  border-radius: 12px;
  background: linear-gradient(135deg, rgba(47, 107, 255, 0.08), rgba(124, 58, 237, 0.06));
  border: 1px solid rgba(47, 107, 255, 0.18);
}

.agent-avatar {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  color: #fff;
  font-weight: 700;
  font-size: 12px;
  background: linear-gradient(135deg, #2f6bff, #7c3aed);
}

.agent-meta {
  min-width: 0;
}

.agent-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  font-weight: 650;
}

.agent-desc {
  font-size: 12px;
  color: var(--app-muted);
  margin-top: 3px;
  line-height: 1.5;
}

.agent-tools {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.run-controls {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 14px;
}

.control-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.hidden-input {
  display: none;
}

.file-chip {
  font-size: 12px;
  color: #334155;
  background: #f1f5f9;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 3px 8px;
}

.steps {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--app-border);
}

.results-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 4px;
}

.results-head h3 {
  margin: 0;
  font-size: 16px;
  font-weight: 660;
}

.results-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  gap: 14px;
}

.rule-strip {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 14px 18px;
}

.rule-count {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  background: #f8fafc;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 5px 10px;
}

.rule-count strong {
  font-size: 13px;
}

.text-block {
  margin: 0;
  padding: 12px 14px;
  background: #f8fafc;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.markdown-block {
  background: #0f172a;
  color: #d7e2f5;
  border-color: transparent;
  max-height: 520px;
}

.report-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.truncated-alert {
  margin-bottom: 10px;
}

.muted-block {
  color: var(--app-muted);
  font-size: 12.5px;
  text-align: center;
  padding: 28px;
}

@media (max-width: 1180px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
</style>
