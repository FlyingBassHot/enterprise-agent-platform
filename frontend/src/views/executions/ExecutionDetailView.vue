<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  CopyDocument,
  Refresh,
  VideoPause,
  VideoPlay,
} from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import JsonBlock from '@/components/JsonBlock.vue'
import MetricCard from '@/components/MetricCard.vue'
import ExecutionTimeline from '@/components/ExecutionTimeline.vue'
import KnowledgeCitations from '@/components/KnowledgeCitations.vue'
import { agentsApi, approvalsApi, executionsApi } from '@/api/platform'
import {
  useExecutionStream,
  type ExecutionEventDto,
} from '@/composables/useExecutionStream'
import {
  mergeKnowledgeCitations,
  parseKnowledgeCitations,
  type KnowledgeCitation,
} from '@/components/knowledgeCitations'
import { useAuthStore } from '@/stores/auth'
import type {
  AgentInfo,
  ApprovalInfo,
  ExecutionDetail,
  ExecutionEventInfo,
  ToolCallRecord,
} from '@/types/api'
import { formatDate, formatDuration, shortId } from '@/utils/format'

const TERMINAL_STATUSES = ['completed', 'incomplete', 'failed', 'cancelled']
const CANCELLABLE_STATUSES = ['pending', 'running', 'waiting_approval']
const RESUMABLE_STATUSES = ['incomplete', 'failed', 'waiting_approval']

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const detail = ref<ExecutionDetail | null>(null)
const events = ref<ExecutionEventInfo[]>([])
const toolCalls = ref<ToolCallRecord[]>([])
const knowledgeCitations = ref<KnowledgeCitation[]>([])
const agents = ref<AgentInfo[]>([])
const loading = ref(false)
const error = ref('')
const toolCallsLoading = ref(false)
const toolCallsError = ref('')
const activeTab = ref('events')
const resumeLoading = ref(false)
const cancelLoading = ref(false)
const approvalActionId = ref('')
const now = ref(Date.now())

let timer: number | undefined
let loadToken = 0

const executionId = computed(() => String(route.params.id || ''))

const {
  connected,
  error: streamError,
  start: startStream,
  stop: stopStream,
} = useExecutionStream({ onEvent: handleStreamEvent })

const status = computed(() => detail.value?.status || '')

const isTerminal = computed(() => TERMINAL_STATUSES.includes(status.value))

const canWrite = computed(() => auth.has('executions:write'))

const canCancel = computed(() => canWrite.value && CANCELLABLE_STATUSES.includes(status.value))

const canResume = computed(
  () =>
    canWrite.value &&
    RESUMABLE_STATUSES.includes(status.value) &&
    pendingApprovals.value.length === 0,
)

const canApprove = computed(() => {
  if (!auth.has('approvals:write')) return false
  if (auth.has('approvals:read_all') || auth.has('approvals:override')) return true
  return Boolean(detail.value && detail.value.user_id !== auth.user?.id)
})

const agentName = computed(() => {
  if (!detail.value) return '-'
  return (
    agents.value.find((item) => item.id === detail.value?.agent_id)?.name ||
    shortId(detail.value.agent_id, 12)
  )
})

const pendingApprovals = computed(
  () => detail.value?.approvals.filter((item) => item.status === 'pending') || [],
)

const verification = computed<{
  status?: string
  attempts?: number
  issues?: Array<{ code?: string; message?: string }>
} | null>(() => {
  const payload = detail.value?.output_payload as Record<string, unknown> | null | undefined
  const value = payload?.verification
  return value && typeof value === 'object' ? (value as {
    status?: string
    attempts?: number
    issues?: Array<{ code?: string; message?: string }>
  }) : null
})

const usage = computed<Record<string, number>>(() => detail.value?.token_usage || {})

const durationMs = computed(() => {
  if (!detail.value?.started_at) return null
  const start = new Date(detail.value.started_at).getTime()
  if (Number.isNaN(start)) return null
  const end = detail.value.finished_at
    ? new Date(detail.value.finished_at).getTime()
    : now.value
  if (Number.isNaN(end)) return null
  return Math.max(0, end - start)
})

const streamLabel = computed(() => {
  if (connected.value) return '事件流已连接'
  if (isTerminal.value) return '执行已结束'
  return streamError.value ? '事件流连接中断' : '事件流连接中'
})

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

function tokenTotal(source?: Record<string, number> | null): number {
  if (!source) return 0
  if (typeof source.total_tokens === 'number') return source.total_tokens
  return (source.prompt_tokens || 0) + (source.completion_tokens || 0)
}

function maxSeq(): number {
  return events.value.reduce((max, item) => Math.max(max, item.seq), 0)
}

function citationsFromDetail(data: ExecutionDetail | null): KnowledgeCitation[] {
  return parseKnowledgeCitations(data?.output_payload?.knowledge_citations)
}

function citationsFromEvents(list: ExecutionEventInfo[]): KnowledgeCitation[] {
  const collected: KnowledgeCitation[] = []
  for (const event of list) {
    if (event.type === 'knowledge_retrieved') {
      collected.push(...parseKnowledgeCitations(event.payload.citations))
    }
  }
  return collected
}

function mergeCitations(data: ExecutionDetail | null) {
  knowledgeCitations.value = mergeKnowledgeCitations(
    knowledgeCitations.value,
    citationsFromDetail(data),
    citationsFromEvents(events.value),
  )
}

function mergeEvent(event: ExecutionEventDto): boolean {
  if (events.value.some((item) => item.seq === event.seq)) return false
  events.value.push({
    seq: event.seq,
    execution_id: event.execution_id || executionId.value,
    type: event.type,
    payload: event.payload || {},
    created_at: event.created_at || new Date().toISOString(),
  })
  events.value.sort((a, b) => a.seq - b.seq)
  return true
}

function handleStreamEvent(event: ExecutionEventDto) {
  if (event.execution_id && event.execution_id !== executionId.value) return
  if (!mergeEvent(event)) return
  const terminalStatus =
    event.type === 'execution_completed'
      ? 'completed'
      : event.type === 'execution_incomplete'
        ? 'incomplete'
        : event.type === 'execution_failed'
        ? 'failed'
        : event.type === 'execution_cancelled'
          ? 'cancelled'
          : ''
  if (detail.value) {
    if (terminalStatus) {
      detail.value = { ...detail.value, status: terminalStatus }
    } else if (event.type === 'execution_started') {
      detail.value = { ...detail.value, status: 'running' }
    } else if (event.type === 'approval_required') {
      detail.value = { ...detail.value, status: 'waiting_approval' }
    } else if (event.type === 'approval_resolved') {
      detail.value = { ...detail.value, status: 'running' }
    }
  }
  if (event.type === 'knowledge_retrieved') {
    const incoming = parseKnowledgeCitations(event.payload.citations)
    if (incoming.length) {
      knowledgeCitations.value = mergeKnowledgeCitations(
        knowledgeCitations.value,
        incoming,
      )
    }
  }
  if (event.type === 'tool_call_completed') {
    void loadToolCalls(executionId.value)
  }
  if (event.type === 'approval_required' || event.type === 'approval_resolved') {
    void reloadDetail()
  }
  if (terminalStatus) {
    stopStream()
    void finishTerminal()
  }
}

async function reloadDetail() {
  const id = executionId.value
  if (!id) return
  try {
    const data = await executionsApi.get(id)
    if (executionId.value !== id) return
    detail.value = data
    mergeCitations(data)
  } catch (err) {
    if (executionId.value === id) error.value = describeError(err)
  }
}

async function finishTerminal() {
  await reloadDetail()
  await loadToolCalls(executionId.value)
  if (detail.value && !isTerminal.value) startStreaming()
}

function startStreaming() {
  if (!detail.value) return
  if (isTerminal.value) return
  stopStream()
  startStream(detail.value.id, maxSeq())
}

async function loadToolCalls(targetId: string) {
  if (!targetId) return
  toolCallsLoading.value = true
  toolCallsError.value = ''
  let lastError: unknown = null
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const records = await executionsApi.toolCalls(targetId)
      if (executionId.value !== targetId) return
      toolCalls.value = records
      toolCallsLoading.value = false
      return
    } catch (err) {
      lastError = err
      if (attempt < 2) await delay(400)
    }
  }
  if (executionId.value !== targetId) return
  toolCalls.value = []
  toolCallsError.value = describeError(lastError)
  toolCallsLoading.value = false
}

async function loadAgents() {
  try {
    agents.value = await agentsApi.list()
  } catch {
    agents.value = []
  }
}

async function load() {
  const id = executionId.value
  if (!id) return
  const token = ++loadToken
  loading.value = true
  error.value = ''
  stopStream()
  try {
    const [detailData, eventList] = await Promise.all([
      executionsApi.get(id),
      executionsApi.events(id, 0, 2000),
    ])
    if (token !== loadToken || executionId.value !== id) return
    detail.value = detailData
    events.value = eventList
    knowledgeCitations.value = mergeKnowledgeCitations(
      citationsFromDetail(detailData),
      citationsFromEvents(eventList),
    )
    startStreaming()
    void loadToolCalls(id)
  } catch (err) {
    if (token !== loadToken) return
    detail.value = null
    events.value = []
    toolCalls.value = []
    knowledgeCitations.value = []
    error.value = describeError(err)
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function resetAndLoad() {
  detail.value = null
  events.value = []
  toolCalls.value = []
  knowledgeCitations.value = []
  toolCallsError.value = ''
  error.value = ''
  activeTab.value = 'events'
  void load()
}

async function copyText(value?: string | null) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.error('复制失败，请手动选择文本')
  }
}

async function cancelExecution() {
  if (!detail.value) return
  try {
    await ElMessageBox.confirm(
      `确认取消执行 ${shortId(detail.value.id, 10)}？`,
      '取消执行',
      { type: 'warning', confirmButtonText: '取消执行', cancelButtonText: '保留' },
    )
  } catch {
    return
  }
  cancelLoading.value = true
  try {
    await executionsApi.cancel(detail.value.id)
    ElMessage.success('已请求取消')
    await reloadDetail()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    cancelLoading.value = false
  }
}

async function resumeExecution() {
  if (!detail.value) return
  resumeLoading.value = true
  try {
    const stopReason = String(
      (detail.value.output_payload as Record<string, unknown> | null)?.stop_reason || '',
    )
    const tokenBudget = Number(
      (detail.value.output_payload as Record<string, unknown> | null)?.token_budget || 0,
    )
    await executionsApi.resume(
      detail.value.id,
      stopReason === 'token_budget'
        ? { extraTokens: tokenBudget || 200_000 }
        : status.value === 'incomplete'
          ? { extraIterations: 8 }
          : undefined,
    )
    ElMessage.success('已恢复执行')
    await load()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    resumeLoading.value = false
  }
}

async function decideApproval(approval: ApprovalInfo, approved: boolean) {
  let comment = ''
  try {
    const result = await ElMessageBox.prompt(
      approved ? '填写批准备注（可选）' : '填写拒绝原因（可选）',
      approved ? '批准工具调用' : '拒绝工具调用',
      {
        inputValue: '',
        confirmButtonText: approved ? '批准' : '拒绝',
        cancelButtonText: '取消',
        inputPlaceholder: '备注会记录在审批历史中',
      },
    )
    comment = String(result.value || '')
  } catch {
    return
  }
  approvalActionId.value = approval.id
  try {
    if (approved) await approvalsApi.approve(approval.id, comment)
    else await approvalsApi.reject(approval.id, comment)
    ElMessage.success(approved ? '已批准，执行将继续' : '已拒绝，执行将继续')
    await reloadDetail()
    if (!isTerminal.value && !connected.value) startStreaming()
  } catch (err) {
    error.value = describeError(err)
  } finally {
    approvalActionId.value = ''
  }
}

function openList() {
  void router.push('/executions')
}

watch(
  () => route.params.id,
  () => {
    resetAndLoad()
  },
)

onMounted(() => {
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
  void loadAgents()
  void load()
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="page-stack">
    <div>
      <el-button link @click="openList">
        <el-icon><ArrowLeft /></el-icon>
        返回执行中心
      </el-button>
    </div>

    <PageHeader
      title="执行详情"
      :subtitle="detail ? `执行 ${shortId(detail.id, 12)} · Trace ${shortId(detail.trace_id, 14)}` : '加载执行记录'"
    >
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button
          v-if="canCancel"
          type="danger"
          plain
          :loading="cancelLoading"
          @click="cancelExecution"
        >
          <el-icon><VideoPause /></el-icon>
          取消执行
        </el-button>
        <el-button
          v-if="canResume"
          type="primary"
          plain
          :loading="resumeLoading"
          @click="resumeExecution"
        >
          <el-icon><VideoPlay /></el-icon>
          恢复执行
        </el-button>
      </template>
    </PageHeader>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      :title="error"
    >
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <el-skeleton v-if="loading && !detail" :rows="8" animated />

    <template v-else-if="detail">
      <div class="card summary-card">
        <div class="summary-top">
          <div class="status-block">
            <StatusTag :status="detail.status" />
            <span class="mode">{{ detail.mode }}</span>
            <span v-if="pendingApprovals.length" class="pending-hint">
              {{ pendingApprovals.length }} 个审批待处理
            </span>
          </div>
          <div class="stream-state">
            <span
              class="state-dot"
              :class="{ live: connected, err: Boolean(streamError) && !isTerminal }"
            />
            {{ streamLabel }}
            <el-button
              v-if="streamError && !isTerminal"
              link
              type="primary"
              @click="startStreaming"
            >
              重新连接
            </el-button>
          </div>
        </div>

        <div v-if="detail.steps.length" class="steps-block">
          <div class="steps-title">任务步骤</div>
          <div
            v-for="step in detail.steps"
            :key="step.id"
            class="step-row"
          >
            <StatusTag :status="step.status" />
            <span class="step-index mono">{{ step.position + 1 }}</span>
            <span class="step-title">{{ step.title }}</span>
          </div>
        </div>

        <div
          v-if="verification && verification.status === 'failed'"
          class="verify-block"
        >
          <div class="steps-title">
            完成校验未通过（第 {{ verification.attempts || 0 }} 次）
          </div>
          <ul class="verify-issues">
            <li v-for="(issue, index) in verification.issues || []" :key="index">
              <span class="mono">{{ issue.code }}</span>：{{ issue.message }}
            </li>
          </ul>
        </div>

        <div class="meta-grid">
          <div class="meta-item">
            <span class="meta-label">{{ detail.agent_id ? 'Agent' : detail.workflow_id ? 'Workflow' : 'Agent' }}</span>
            <div class="meta-value">
              <template v-if="detail.agent_id">{{ agentName }} · v{{ detail.agent_version }}</template>
              <template v-else-if="detail.workflow_id">Workflow 执行</template>
              <template v-else>-</template>
            </div>
          </div>
          <div v-if="detail.workflow_id" class="meta-item">
            <span class="meta-label">Workflow ID</span>
            <div class="meta-value mono">
              {{ detail.workflow_id }}
              <el-button link size="small" @click="copyText(detail.workflow_id)">
                <el-icon><CopyDocument /></el-icon>
              </el-button>
              <span v-if="detail.workflow_version_id" class="workflow-version">
                · 版本 {{ shortId(detail.workflow_version_id, 8) }}
              </span>
            </div>
          </div>
          <div class="meta-item">
            <span class="meta-label">执行 ID</span>
            <div class="meta-value mono">
              {{ detail.id }}
              <el-button link size="small" @click="copyText(detail.id)">
                <el-icon><CopyDocument /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="meta-item">
            <span class="meta-label">Trace ID</span>
            <div class="meta-value mono">
              {{ detail.trace_id || '-' }}
              <el-button
                v-if="detail.trace_id"
                link
                size="small"
                @click="copyText(detail.trace_id)"
              >
                <el-icon><CopyDocument /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="meta-item">
            <span class="meta-label">发起人</span>
            <div class="meta-value mono">{{ detail.user_id }}</div>
          </div>
          <div class="meta-item">
            <span class="meta-label">创建 / 开始</span>
            <div class="meta-value">
              {{ formatDate(detail.created_at) }} · {{ formatDate(detail.started_at) }}
            </div>
          </div>
          <div class="meta-item">
            <span class="meta-label">结束</span>
            <div class="meta-value">{{ formatDate(detail.finished_at) }}</div>
          </div>
        </div>
      </div>

      <div class="metrics">
        <MetricCard
          label="Prompt Tokens"
          :value="(usage.prompt_tokens || 0).toLocaleString('zh-CN')"
          accent="blue"
        />
        <MetricCard
          label="Completion Tokens"
          :value="(usage.completion_tokens || 0).toLocaleString('zh-CN')"
          accent="green"
        />
        <MetricCard
          label="总 Tokens"
          :value="tokenTotal(usage).toLocaleString('zh-CN')"
          :hint="`LLM 调用 ${detail.llm_calls.length} 次`"
          accent="violet"
        />
        <MetricCard
          label="执行耗时"
          :value="formatDuration(durationMs)"
          :hint="`事件 ${Math.max(detail.events_count, events.length)} · 工具 ${toolCalls.length}`"
          accent="amber"
        />
      </div>

      <div class="io-grid">
        <div class="card io-card">
          <div class="card-head">
            <h3>输入</h3>
            <el-button v-if="detail.input_text" link size="small" @click="copyText(detail.input_text)">
              <el-icon><CopyDocument /></el-icon>
              复制
            </el-button>
          </div>
          <pre v-if="detail.input_text" class="text-block">{{ detail.input_text }}</pre>
          <div v-else class="muted">暂无输入内容</div>
        </div>
        <div class="card io-card">
          <div class="card-head">
            <h3>输出</h3>
            <el-button
              v-if="detail.output_text"
              link
              size="small"
              @click="copyText(detail.output_text)"
            >
              <el-icon><CopyDocument /></el-icon>
              复制
            </el-button>
          </div>
          <el-alert
            v-if="detail.error"
            type="error"
            :closable="false"
            show-icon
            :title="detail.error"
            class="error-alert"
          />
          <pre v-if="detail.output_text" class="text-block">{{ detail.output_text }}</pre>
          <div v-else class="muted">暂无输出内容</div>
        </div>
      </div>

      <KnowledgeCitations
        v-if="knowledgeCitations.length"
        :citations="knowledgeCitations"
        title="知识引用"
      />

      <div class="card">
        <el-tabs v-model="activeTab">
          <el-tab-pane :label="`事件时间线 (${events.length})`" name="events">
            <ExecutionTimeline
              :events="events"
              max-height="620px"
              :auto-scroll="!isTerminal"
            />
          </el-tab-pane>

          <el-tab-pane :label="`工具调用 (${toolCalls.length})`" name="tools">
            <el-alert
              v-if="toolCallsError"
              type="warning"
              :closable="false"
              show-icon
              :title="toolCallsError"
              class="tab-alert"
            >
              <el-button link type="primary" @click="loadToolCalls(detail.id)">重试</el-button>
            </el-alert>
            <el-table
              v-loading="toolCallsLoading"
              :data="toolCalls"
              row-key="id"
              size="small"
            >
              <el-table-column type="expand">
                <template #default="{ row }">
                  <div class="expand-grid">
                    <div>
                      <div class="expand-label">参数（已脱敏）</div>
                      <JsonBlock :value="row.arguments_masked" max-height="240px" />
                    </div>
                    <div>
                      <div class="expand-label">结果</div>
                      <JsonBlock :value="row.result" max-height="320px" />
                    </div>
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="工具" min-width="180">
                <template #default="{ row }">
                  <span class="mono">{{ row.tool_name }}</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <StatusTag :status="row.status" />
                </template>
              </el-table-column>
              <el-table-column label="耗时" width="110">
                <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
              </el-table-column>
              <el-table-column label="错误" min-width="180">
                <template #default="{ row }">
                  <span class="error-text">{{ row.error || '-' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="调用时间" width="170">
                <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
              </el-table-column>
              <template #empty>
                <el-empty description="暂无工具调用记录" :image-size="80" />
              </template>
            </el-table>
          </el-tab-pane>

          <el-tab-pane :label="`审批 (${detail.approvals.length})`" name="approvals">
            <div v-if="detail.approvals.length" class="approval-list">
              <div
                v-for="approval in detail.approvals"
                :key="approval.id"
                class="approval-card"
              >
                <div class="approval-head">
                  <div class="approval-title">
                    <strong>{{ approval.tool_name }}</strong>
                    <span class="muted">{{ approval.kind }}</span>
                  </div>
                  <StatusTag :status="approval.status" />
                </div>
                <div class="approval-meta">
                  申请 {{ formatDate(approval.requested_at) }} · 处理
                  {{ formatDate(approval.resolved_at) }}
                  <span v-if="approval.resolved_by" class="muted">
                    · {{ shortId(approval.resolved_by) }}
                  </span>
                </div>
                <JsonBlock :value="approval.arguments" max-height="180px" />
                <div v-if="approval.comment" class="approval-comment">
                  备注：{{ approval.comment }}
                </div>
                <div v-if="approval.status === 'pending' && canApprove" class="approval-actions">
                  <el-button
                    type="primary"
                    size="small"
                    :loading="approvalActionId === approval.id"
                    @click="decideApproval(approval, true)"
                  >
                    批准
                  </el-button>
                  <el-button
                    type="danger"
                    size="small"
                    plain
                    :loading="approvalActionId === approval.id"
                    @click="decideApproval(approval, false)"
                  >
                    拒绝
                  </el-button>
                </div>
              </div>
            </div>
            <el-empty v-else description="暂无审批记录" :image-size="80" />
          </el-tab-pane>

          <el-tab-pane :label="`LLM 调用 (${detail.llm_calls.length})`" name="llm">
            <el-table :data="detail.llm_calls" row-key="id" size="small">
              <el-table-column label="Provider" min-width="120">
                <template #default="{ row }">{{ row.provider || '-' }}</template>
              </el-table-column>
              <el-table-column label="模型" min-width="150">
                <template #default="{ row }">
                  <span class="mono">{{ row.model }}</span>
                </template>
              </el-table-column>
              <el-table-column label="Prompt" width="100" align="right">
                <template #default="{ row }">{{ row.prompt_tokens }}</template>
              </el-table-column>
              <el-table-column label="Completion" width="110" align="right">
                <template #default="{ row }">{{ row.completion_tokens }}</template>
              </el-table-column>
              <el-table-column label="Total" width="90" align="right">
                <template #default="{ row }">{{ row.total_tokens }}</template>
              </el-table-column>
              <el-table-column label="延迟" width="100">
                <template #default="{ row }">{{ formatDuration(row.latency_ms) }}</template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <StatusTag :status="row.status" />
                </template>
              </el-table-column>
              <el-table-column label="时间" width="170">
                <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
              </el-table-column>
              <template #empty>
                <el-empty description="暂无 LLM 调用记录" :image-size="80" />
              </template>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </div>
    </template>

    <el-empty v-else-if="!error" description="执行记录不存在" />
  </div>
</template>

<style scoped>
.summary-card {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.summary-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.verify-block {
  margin-top: 16px;
  padding: 12px 14px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
}

.verify-issues {
  margin: 8px 0 0;
  padding-left: 18px;
  color: var(--el-text-color-regular);
}

.steps-block {
  margin-top: 16px;
  padding: 12px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
}

.steps-title {
  font-weight: 600;
  margin-bottom: 8px;
}

.step-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}

.step-index {
  color: var(--el-text-color-secondary);
  min-width: 18px;
}

.step-title {
  flex: 1;
}

.status-block {
  display: flex;
  align-items: center;
  gap: 10px;
}

.mode {
  font-size: 11.5px;
  color: var(--app-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.pending-hint {
  font-size: 11.5px;
  color: var(--el-color-warning);
}

.stream-state {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11.5px;
  color: var(--app-muted);
}

.state-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #cbd5e1;
}

.state-dot.live {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.16);
}

.state-dot.err {
  background: #ef4444;
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px 20px;
}

.meta-item {
  min-width: 0;
}

.workflow-version {
  margin-left: 4px;
  color: var(--app-muted);
  font-size: 11px;
}

.meta-label {
  font-size: 11px;
  color: var(--app-muted);
}

.meta-value {
  margin-top: 4px;
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 4px;
  word-break: break-all;
}

.mono {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
}

.io-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
}

.io-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.card-head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
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
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.error-alert {
  margin-bottom: 4px;
}

.tab-alert {
  margin-bottom: 12px;
}

.expand-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 14px;
  padding: 8px 12px;
}

.expand-label {
  font-size: 11px;
  color: var(--app-muted);
  margin-bottom: 6px;
}

.error-text {
  color: var(--el-color-danger);
  font-size: 12px;
  word-break: break-word;
}

.approval-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.approval-card {
  border: 1px solid var(--app-border);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: #fbfcfe;
}

.approval-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.approval-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
}

.approval-meta {
  font-size: 11.5px;
  color: var(--app-muted);
}

.approval-comment {
  font-size: 12px;
  color: #475569;
}

.approval-actions {
  display: flex;
  gap: 8px;
}
</style>
