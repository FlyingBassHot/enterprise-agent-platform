<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Link, Plus, Promotion, Refresh, Upload } from '@element-plus/icons-vue'
import MessageBubble from '@/components/MessageBubble.vue'
import ToolCallCard from '@/components/ToolCallCard.vue'
import ApprovalDialog from '@/components/ApprovalDialog.vue'
import KnowledgeCitations from '@/components/KnowledgeCitations.vue'
import StatusTag from '@/components/StatusTag.vue'
import {
  agentsApi,
  approvalsApi,
  artifactsApi,
  conversationsApi,
  executionsApi,
} from '@/api/platform'
import { useExecutionStream, type ExecutionEventDto } from '@/composables/useExecutionStream'
import { useAuthStore } from '@/stores/auth'
import type {
  AgentInfo,
  ApprovalInfo,
  ArtifactInfo,
  ConversationInfo,
  ExecutionDetail,
  ExecutionInfo,
} from '@/types/api'
import {
  mergeKnowledgeCitations,
  parseKnowledgeCitations,
  type KnowledgeCitation,
} from '@/components/knowledgeCitations'
import { formatBytes, formatDate, shortId } from '@/utils/format'

interface ChatMessage {
  id: string
  role: string
  content: string
  created_at?: string | null
  execution_id?: string | null
  streaming?: boolean
  pending?: boolean
}

interface LiveToolCall {
  id: string
  name: string
  status: string
  ok?: boolean | null
  durationMs?: number | null
  arguments?: Record<string, unknown> | null
  error?: string | null
}

interface LiveApproval {
  id: string
  toolName: string
  toolCallId: string | null
  riskLevel: string
  arguments: Record<string, unknown>
  status: string
  comment: string | null
}

const ACTIVE_STATUSES = ['pending', 'running', 'waiting_approval']

const router = useRouter()
const auth = useAuthStore()

const agents = ref<AgentInfo[]>([])
const agentsLoading = ref(false)
const agentsError = ref('')

const conversations = ref<ConversationInfo[]>([])
const conversationsLoading = ref(false)
const conversationsError = ref('')

const messages = ref<ChatMessage[]>([])
const messagesLoading = ref(false)
const selectedConversationId = ref('')
const selectedAgentId = ref('')

const draft = ref('')
const attachments = ref<ArtifactInfo[]>([])
const uploading = ref(false)
const sending = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const execution = ref<ExecutionInfo | null>(null)
const executionDetail = ref<ExecutionDetail | null>(null)
const liveStatus = ref('')
const liveToolCalls = ref<LiveToolCall[]>([])
const liveApprovals = ref<LiveApproval[]>([])
const knowledgeCitations = ref<KnowledgeCitation[]>([])
const approvalDialogVisible = ref(false)
const decisionLoading = ref(false)
const cancelLoading = ref(false)
const chatError = ref('')
const streamSeq = ref(0)
const assistantDraftId = ref('')
const chatScroll = ref<HTMLElement | null>(null)
let messagesToken = 0
let appliedSeq = 0

const {
  events: streamEvents,
  connected,
  error: streamError,
  start: startStream,
  stop: stopStream,
} = useExecutionStream({ onEvent: handleStreamEvent })

const publishedAgents = computed(() => agents.value.filter((item) => item.status === 'published'))

const agentMap = computed(() => new Map(agents.value.map((item) => [item.id, item])))

const selectedAgent = computed(
  () => publishedAgents.value.find((item) => item.id === selectedAgentId.value) || null,
)

const selectedConversation = computed(
  () => conversations.value.find((item) => item.id === selectedConversationId.value) || null,
)

const conversationAgentMismatch = computed(() => {
  const conversation = selectedConversation.value
  if (!conversation) return false
  return conversation.agent_id !== selectedAgentId.value
})

const status = computed(
  () => liveStatus.value || executionDetail.value?.status || execution.value?.status || '',
)

const isActive = computed(() => Boolean(execution.value) && ACTIVE_STATUSES.includes(status.value))

const canDecide = computed(() => {
  if (!auth.has('approvals:write')) return false
  if (auth.has('approvals:read_all') || auth.has('approvals:override')) return true
  return Boolean(execution.value && execution.value.user_id !== auth.user?.id)
})

const canCancel = computed(() => auth.has('executions:write') && isActive.value)

const usage = computed<Record<string, number>>(
  () => executionDetail.value?.token_usage || execution.value?.token_usage || {},
)

const eventCount = computed(() =>
  executionDetail.value ? executionDetail.value.events_count : streamEvents.value.length,
)

const dialogApproval = computed(() => {
  const approval = liveApprovals.value.find((item) => item.status === 'pending')
  if (!approval) return null
  return {
    approval_id: approval.id,
    tool_name: approval.toolName,
    tool_call_id: approval.toolCallId,
    risk_level: approval.riskLevel,
    arguments: approval.arguments,
  }
})

const canSend = computed(
  () =>
    Boolean(selectedAgent.value) &&
    !sending.value &&
    !isActive.value &&
    !uploading.value &&
    !conversationAgentMismatch.value &&
    draft.value.trim().length > 0,
)

const composerDisabled = computed(() => !selectedAgent.value || isActive.value || sending.value)

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function agentName(id?: string | null): string {
  if (!id) return '-'
  return agentMap.value.get(id)?.name || shortId(id)
}

function tokenTotal(source?: Record<string, number> | null): number {
  if (!source) return 0
  if (typeof source.total_tokens === 'number') return source.total_tokens
  return (source.prompt_tokens || 0) + (source.completion_tokens || 0)
}

function approvalLabel(value: string): string {
  if (value === 'approved') return '已批准'
  if (value === 'rejected') return '已拒绝'
  return '待审批'
}

function usageFrom(value: unknown): Record<string, number> {
  const result: Record<string, number> = {}
  if (value && typeof value === 'object') {
    for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
      const parsed = Number(raw)
      if (Number.isFinite(parsed)) result[key] = parsed
    }
  }
  return result
}

async function loadAgents() {
  agentsLoading.value = true
  agentsError.value = ''
  try {
    agents.value = await agentsApi.list()
    if (!publishedAgents.value.some((item) => item.id === selectedAgentId.value)) {
      selectedAgentId.value = publishedAgents.value[0]?.id || ''
    }
  } catch (err) {
    agentsError.value = describeError(err)
  } finally {
    agentsLoading.value = false
  }
}

async function loadConversations() {
  conversationsLoading.value = true
  conversationsError.value = ''
  try {
    conversations.value = await conversationsApi.list()
  } catch (err) {
    conversationsError.value = describeError(err)
  } finally {
    conversationsLoading.value = false
  }
}

async function loadMessages(conversationId: string) {
  const token = ++messagesToken
  messagesLoading.value = true
  chatError.value = ''
  try {
    const list = await conversationsApi.messages(conversationId)
    if (token !== messagesToken || selectedConversationId.value !== conversationId) return
    messages.value = list.map((item) => ({
      id: item.id,
      role: item.role,
      content: item.content,
      created_at: item.created_at,
      execution_id: item.execution_id,
    }))
  } catch (err) {
    if (token === messagesToken) chatError.value = describeError(err)
  } finally {
    if (token === messagesToken) messagesLoading.value = false
  }
}

function resetLiveState() {
  appliedSeq = 0
  liveToolCalls.value = []
  liveApprovals.value = []
  knowledgeCitations.value = []
  approvalDialogVisible.value = false
  assistantDraftId.value = ''
  streamSeq.value = 0
}

async function selectConversation(conversation: ConversationInfo) {
  if (isActive.value || sending.value) {
    ElMessage.warning('当前执行仍在进行，请等待结束或先取消执行')
    return
  }
  selectedConversationId.value = conversation.id
  messagesToken += 1
  draft.value = ''
  attachments.value = []
  execution.value = null
  executionDetail.value = null
  liveStatus.value = ''
  resetLiveState()
  chatError.value = ''
  stopStream()
  const published = publishedAgents.value.find((item) => item.id === conversation.agent_id)
  if (published) selectedAgentId.value = published.id
  await loadMessages(conversation.id)
}

function newChat() {
  if (isActive.value || sending.value) {
    ElMessage.warning('当前执行仍在进行，请等待结束或先取消执行')
    return
  }
  stopStream()
  selectedConversationId.value = ''
  messagesToken += 1
  messagesLoading.value = false
  messages.value = []
  draft.value = ''
  attachments.value = []
  execution.value = null
  executionDetail.value = null
  liveStatus.value = ''
  chatError.value = ''
  resetLiveState()
}

function seedApprovals(approvals: ApprovalInfo[]) {
  liveApprovals.value = approvals.map((item) => ({
    id: item.id,
    toolName: item.tool_name,
    toolCallId: item.tool_call_id,
    riskLevel: 'medium',
    arguments: item.arguments,
    status: item.status,
    comment: item.comment,
  }))
}

async function finalizeExecution(nextStatus: string) {
  const targetId = execution.value?.id
  if (!targetId) return
  liveStatus.value = nextStatus
  try {
    const detail = await executionsApi.get(targetId)
    if (execution.value?.id !== targetId) return
    executionDetail.value = detail
    execution.value = detail
    liveStatus.value = detail.status
    seedApprovals(detail.approvals)
    knowledgeCitations.value = mergeKnowledgeCitations(
      knowledgeCitations.value,
      parseKnowledgeCitations(detail.output_payload?.knowledge_citations),
    )
    approvalDialogVisible.value = false
  } catch (err) {
    if (execution.value?.id === targetId) chatError.value = describeError(err)
  }
  await loadConversations()
  if (execution.value?.id !== targetId) return
  const conversationId =
    executionDetail.value?.conversation_id ||
    execution.value?.conversation_id ||
    selectedConversationId.value
  if (conversationId) {
    selectedConversationId.value = conversationId
    await loadMessages(conversationId)
  }
}

function ensureDraft(): ChatMessage {
  const existing = messages.value.find((item) => item.id === assistantDraftId.value)
  if (existing) return existing
  const message: ChatMessage = {
    id: `local-assistant-${Date.now()}`,
    role: 'assistant',
    content: '',
    created_at: new Date().toISOString(),
    streaming: true,
  }
  assistantDraftId.value = message.id
  messages.value.push(message)
  return message
}

function finalizeAssistant(payload: Record<string, unknown>) {
  const content = String(payload.content ?? '')
  const toolCalls = Array.isArray(payload.tool_calls) ? payload.tool_calls : []
  const index = messages.value.findIndex((item) => item.id === assistantDraftId.value)
  if (index >= 0) {
    const message = messages.value[index]
    if (!content && toolCalls.length > 0) {
      messages.value.splice(index, 1)
    } else {
      message.content = content || message.content
      message.streaming = false
    }
    assistantDraftId.value = ''
    return
  }
  if (content) {
    messages.value.push({
      id: `local-assistant-${Date.now()}`,
      role: 'assistant',
      content,
      created_at: new Date().toISOString(),
    })
  }
}

function stopDraftStreaming() {
  const message = messages.value.find((item) => item.id === assistantDraftId.value)
  if (message) message.streaming = false
  assistantDraftId.value = ''
}

function upsertTool(payload: Record<string, unknown>, completed: boolean) {
  const key = String(
    payload.tool_call_id || `${payload.tool_name || 'tool'}-${streamSeq.value}`,
  )
  const existing = liveToolCalls.value.find((item) => item.id === key)
  const parsedArguments =
    payload.arguments && typeof payload.arguments === 'object'
      ? (payload.arguments as Record<string, unknown>)
      : existing?.arguments || null
  const next: LiveToolCall = {
    id: key,
    name: String(payload.tool_name || existing?.name || '工具'),
    status: completed
      ? String(payload.status || (payload.ok ? 'success' : 'failed'))
      : 'running',
    ok: completed ? Boolean(payload.ok) : null,
    durationMs:
      completed && typeof payload.duration_ms === 'number' ? payload.duration_ms : null,
    arguments: parsedArguments,
    error: completed && payload.error ? String(payload.error) : null,
  }
  if (existing) Object.assign(existing, next)
  else liveToolCalls.value.push(next)
}

function addApproval(payload: Record<string, unknown>) {
  const id = String(payload.approval_id || '')
  if (!id || liveApprovals.value.some((item) => item.id === id)) return
  liveApprovals.value.push({
    id,
    toolName: String(payload.tool_name || '工具'),
    toolCallId: payload.tool_call_id ? String(payload.tool_call_id) : null,
    riskLevel: String(payload.risk_level || 'medium'),
    arguments:
      payload.arguments && typeof payload.arguments === 'object'
        ? (payload.arguments as Record<string, unknown>)
        : {},
    status: 'pending',
    comment: null,
  })
  approvalDialogVisible.value = canDecide.value
}

function resolveApproval(payload: Record<string, unknown>) {
  const id = String(payload.approval_id || '')
  const approval = liveApprovals.value.find((item) => item.id === id)
  if (approval) {
    approval.status = payload.approved ? 'approved' : 'rejected'
    approval.comment = payload.comment ? String(payload.comment) : null
  }
  if (dialogApproval.value?.approval_id === id) approvalDialogVisible.value = false
}

function handleStreamEvent(event: ExecutionEventDto) {
  const activeId = execution.value?.id
  if (!activeId) return
  if (event.execution_id && event.execution_id !== activeId) return
  if (event.seq > 0 && event.seq <= appliedSeq) return
  if (event.seq > appliedSeq) appliedSeq = event.seq
  if (event.seq > streamSeq.value) streamSeq.value = event.seq
  const payload = event.payload || {}
  switch (event.type) {
    case 'execution_started':
      liveStatus.value = 'running'
      break
    case 'message_delta': {
      const delta = String(payload.delta ?? payload.content ?? '')
      if (!delta) break
      const message = ensureDraft()
      message.content += delta
      break
    }
    case 'message_completed':
      finalizeAssistant(payload)
      break
    case 'tool_call_started':
      liveStatus.value = 'running'
      upsertTool(payload, false)
      break
    case 'tool_call_completed':
      upsertTool(payload, true)
      break
    case 'approval_required':
      liveStatus.value = 'waiting_approval'
      addApproval(payload)
      break
    case 'approval_resolved':
      liveStatus.value = 'running'
      resolveApproval(payload)
      break
    case 'knowledge_retrieved': {
      const incoming = parseKnowledgeCitations(payload.citations)
      if (incoming.length) {
        knowledgeCitations.value = mergeKnowledgeCitations(
          knowledgeCitations.value,
          incoming,
        )
      }
      break
    }
    case 'execution_completed': {
      stopDraftStreaming()
      const output = String(payload.output ?? '')
      if (output && !messages.value.some((item) => item.role === 'assistant')) {
        messages.value.push({
          id: `local-assistant-${Date.now()}`,
          role: 'assistant',
          content: output,
          created_at: new Date().toISOString(),
        })
      }
      if (execution.value) {
        execution.value = {
          ...execution.value,
          status: 'completed',
          token_usage: usageFrom(payload.usage),
        }
      }
      void finalizeExecution('completed')
      break
    }
    case 'execution_incomplete': {
      stopDraftStreaming()
      if (execution.value) {
        execution.value = {
          ...execution.value,
          status: 'incomplete',
          token_usage: usageFrom(payload.usage),
        }
      }
      liveStatus.value = 'incomplete'
      void finalizeExecution('incomplete')
      break
    }
    case 'execution_failed': {
      stopDraftStreaming()
      chatError.value = String(payload.error || '执行失败')
      liveStatus.value = 'failed'
      void finalizeExecution('failed')
      break
    }
    case 'execution_cancelled':
      stopDraftStreaming()
      liveStatus.value = 'cancelled'
      void finalizeExecution('cancelled')
      break
  }
}

function reconnectStream() {
  if (!execution.value) return
  stopStream()
  startStream(execution.value.id, streamSeq.value)
}

async function send() {
  if (!canSend.value || !selectedAgent.value) return
  chatError.value = ''
  const text = draft.value.trim()
  const uploaded = [...attachments.value]
  const artifactIds = uploaded.map((item) => item.id)
  const optimistic: ChatMessage = {
    id: `local-user-${Date.now()}`,
    role: 'user',
    content: text,
    created_at: new Date().toISOString(),
  }
  messagesToken += 1
  messagesLoading.value = false
  messages.value.push(optimistic)
  draft.value = ''
  attachments.value = []
  sending.value = true
  execution.value = null
  executionDetail.value = null
  liveStatus.value = ''
  resetLiveState()
  try {
    const created = await executionsApi.create({
      agent_id: selectedAgent.value.id,
      input: text,
      conversation_id: selectedConversationId.value || null,
      artifact_ids: artifactIds,
    })
    execution.value = created
    executionDetail.value = null
    liveStatus.value = created.status
    if (created.conversation_id) selectedConversationId.value = created.conversation_id
    stopStream()
    startStream(created.id, 0)
    void loadConversations()
  } catch (err) {
    messages.value = messages.value.filter((item) => item.id !== optimistic.id)
    draft.value = text
    attachments.value = uploaded
    chatError.value = describeError(err)
  } finally {
    sending.value = false
  }
}

function onEnter(event: Event) {
  const keyboard = event as KeyboardEvent
  if (keyboard.isComposing) return
  event.preventDefault()
  void send()
}

function pickFile() {
  fileInput.value?.click()
}

async function onFilesChange(event: Event) {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files || [])
  target.value = ''
  if (!files.length) return
  uploading.value = true
  chatError.value = ''
  try {
    for (const file of files) {
      const artifact = await artifactsApi.upload(file)
      attachments.value.push(artifact)
    }
  } catch (err) {
    chatError.value = describeError(err)
  } finally {
    uploading.value = false
  }
}

function removeAttachment(index: number) {
  attachments.value.splice(index, 1)
}

async function decide(approved: boolean, comment: string) {
  const approval = dialogApproval.value
  if (!approval) return
  decisionLoading.value = true
  try {
    if (approved) await approvalsApi.approve(approval.approval_id, comment)
    else await approvalsApi.reject(approval.approval_id, comment)
    const item = liveApprovals.value.find((entry) => entry.id === approval.approval_id)
    if (item) {
      item.status = approved ? 'approved' : 'rejected'
      item.comment = comment || null
    }
    approvalDialogVisible.value = false
    liveStatus.value = liveApprovals.value.some((entry) => entry.status === 'pending')
      ? 'waiting_approval'
      : 'running'
    ElMessage.success(approved ? '已批准，执行将继续' : '已拒绝，执行将继续')
  } catch (err) {
    chatError.value = describeError(err)
  } finally {
    decisionLoading.value = false
  }
}

async function cancelExecution() {
  const targetId = execution.value?.id
  if (!targetId) return
  cancelLoading.value = true
  try {
    const result = await executionsApi.cancel(targetId)
    ElMessage.success('已请求取消，等待执行结束')
    if (execution.value?.id === targetId && result.status) {
      liveStatus.value = result.status
      if (!connected.value) void finalizeExecution(result.status)
    }
  } catch (err) {
    chatError.value = describeError(err)
  } finally {
    cancelLoading.value = false
  }
}

function openDetail() {
  if (execution.value) void router.push(`/executions/${execution.value.id}`)
}

function openAgentManagement() {
  void router.push('/agents')
}

const totalContentLength = computed(() =>
  messages.value.reduce((total, item) => total + item.content.length, 0),
)

watch([() => messages.value.length, totalContentLength], async () => {
  await nextTick()
  const element = chatScroll.value
  if (element) element.scrollTop = element.scrollHeight
})

onMounted(async () => {
  await Promise.all([loadAgents(), loadConversations()])
})
</script>

<template>
  <div class="workspace-page">
    <div class="workspace">
      <aside class="pane conversations-pane">
        <div class="pane-head">
          <div>
            <div class="pane-title">对话</div>
            <div class="pane-sub">{{ conversations.length }} 个会话</div>
          </div>
          <div class="pane-actions">
            <el-button
              circle
              size="small"
              :loading="conversationsLoading"
              title="刷新对话"
              @click="loadConversations"
            >
              <el-icon><Refresh /></el-icon>
            </el-button>
            <el-button circle size="small" title="新建对话" @click="newChat">
              <el-icon><Plus /></el-icon>
            </el-button>
          </div>
        </div>
        <div class="pane-body conversation-list">
          <el-skeleton v-if="conversationsLoading && !conversations.length" :rows="4" animated />
          <el-alert
            v-if="conversationsError"
            type="error"
            :closable="false"
            show-icon
            :title="conversationsError"
          />
          <div
            v-for="conversation in conversations"
            :key="conversation.id"
            class="conversation-item"
            :class="{ active: conversation.id === selectedConversationId }"
            @click="selectConversation(conversation)"
          >
            <div class="conversation-title">
              {{ conversation.title || '未命名对话' }}
            </div>
            <div class="conversation-meta">
              <span>{{ agentName(conversation.agent_id) }}</span>
              <span>{{ formatDate(conversation.updated_at) }}</span>
            </div>
          </div>
          <div v-if="!conversationsLoading && !conversations.length" class="empty-hint">
            还没有对话，发送第一条消息即可自动创建。
          </div>
        </div>
      </aside>

      <section class="pane chat-pane">
        <div class="chat-head">
          <div class="agent-picker">
            <el-select
              v-model="selectedAgentId"
              :disabled="isActive"
              filterable
              placeholder="选择已发布的 Agent"
              class="agent-select"
            >
              <el-option
                v-for="agent in publishedAgents"
                :key="agent.id"
                :label="agent.name"
                :value="agent.id"
              >
                <span>{{ agent.name }}</span>
                <span class="option-version">v{{ agent.current_version ?? '-' }}</span>
              </el-option>
            </el-select>
            <span v-if="selectedAgent" class="agent-desc">
              {{ selectedAgent.description || '已发布 Agent' }}
            </span>
          </div>
          <div class="chat-head-right">
            <StatusTag v-if="status" :status="status" />
            <el-button v-if="canCancel" link type="danger" :loading="cancelLoading" @click="cancelExecution">
              取消执行
            </el-button>
            <el-button v-if="execution" link type="primary" @click="openDetail">
              <el-icon><Link /></el-icon>
              执行详情
            </el-button>
          </div>
        </div>

        <el-alert
          v-if="agentsError"
          type="error"
          :closable="false"
          show-icon
          :title="agentsError"
        />
        <el-alert
          v-if="chatError"
          type="error"
          :closable="false"
          show-icon
          :title="chatError"
        />
        <el-alert
          v-if="streamError && isActive"
          type="warning"
          :closable="false"
          show-icon
          :title="`事件流连接中断：${streamError}`"
        >
          <el-button size="small" type="warning" plain @click="reconnectStream">重新连接</el-button>
        </el-alert>

        <div ref="chatScroll" v-loading="messagesLoading" class="chat-scroll">
          <div v-if="!publishedAgents.length && !agentsLoading" class="empty-agents">
            <el-empty description="暂无已发布的 Agent，无法发起对话">
              <el-button v-if="auth.has('agents:read')" type="primary" @click="openAgentManagement">
                前往 Agent 管理
              </el-button>
            </el-empty>
          </div>

          <template v-else>
            <div v-if="!messages.length && !isActive" class="empty-conversation">
              <div class="empty-title">
                {{ selectedAgent ? `与 ${selectedAgent.name} 开始对话` : '选择 Agent 开始对话' }}
              </div>
              <div class="empty-sub">
                输入任务描述，Agent 会调用工具执行并在右侧展示实时状态。
              </div>
            </div>

            <MessageBubble
              v-for="message in messages"
              :key="message.id"
              :role="message.role"
              :content="message.content"
              :created-at="message.created_at"
              :streaming="message.streaming"
              :pending="message.pending"
            />

            <KnowledgeCitations
              v-if="knowledgeCitations.length"
              :citations="knowledgeCitations"
              title="知识引用"
              compact
            />

            <div v-if="liveToolCalls.length" class="tool-stack">
              <div class="tool-stack-title">工具调用</div>
              <ToolCallCard
                v-for="call in liveToolCalls"
                :key="call.id"
                :name="call.name"
                :status="call.status"
                :ok="call.ok"
                :duration-ms="call.durationMs"
                :arguments="call.arguments"
                :error="call.error"
              />
            </div>

            <div v-if="dialogApproval && !approvalDialogVisible" class="approval-prompt">
              <div>
                <strong>{{ dialogApproval.tool_name }}</strong> 等待审批
                <span class="muted">（风险 {{ dialogApproval.risk_level }}）</span>
              </div>
              <el-button
                v-if="canDecide"
                size="small"
                type="warning"
                @click="approvalDialogVisible = true"
              >
                处理审批
              </el-button>
              <span v-else class="muted">当前账号无审批权限</span>
            </div>
          </template>
        </div>

        <div class="composer">
          <div v-if="attachments.length" class="attachments">
            <el-tag
              v-for="(artifact, index) in attachments"
              :key="artifact.id"
              closable
              type="info"
              @close="removeAttachment(index)"
            >
              {{ artifact.filename }} · {{ formatBytes(artifact.size) }}
            </el-tag>
          </div>
          <el-input
            v-model="draft"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            resize="none"
            :disabled="composerDisabled"
            placeholder="描述要执行的任务，Enter 发送，Shift+Enter 换行"
            @keydown.enter.exact="onEnter"
          />
          <div class="composer-actions">
            <div class="composer-left">
              <input
                ref="fileInput"
                type="file"
                class="hidden-input"
                multiple
                @change="onFilesChange"
              />
              <el-button
                size="small"
                :loading="uploading"
                :disabled="isActive || sending"
                @click="pickFile"
              >
                <el-icon><Upload /></el-icon>
                附件
              </el-button>
              <span class="hint">
                <template v-if="conversationAgentMismatch">当前对话属于其他 Agent，请新建对话</template>
                <template v-else-if="isActive">执行进行中，结束后可继续发送</template>
                <template v-else>Enter 发送 · Shift+Enter 换行</template>
              </span>
            </div>
            <el-button type="primary" :loading="sending" :disabled="!canSend" @click="send">
              <el-icon><Promotion /></el-icon>
              发送
            </el-button>
          </div>
        </div>
      </section>

      <aside class="pane live-pane">
        <div class="pane-head">
          <div>
            <div class="pane-title">实时执行</div>
            <div class="pane-sub">
              <span class="state-dot" :class="{ live: connected, err: Boolean(streamError) }" />
              {{ connected ? '事件流已连接' : isActive ? '事件流连接中' : '空闲' }}
            </div>
          </div>
        </div>
        <div class="pane-body live-body">
          <div v-if="!execution" class="empty-hint">尚未发起执行。</div>
          <template v-else>
            <div class="meta-row">
              <span class="meta-label">状态</span>
              <StatusTag :status="status" />
            </div>
            <div class="meta-row">
              <span class="meta-label">{{ execution.workflow_id ? 'Workflow' : 'Agent' }}</span>
              <span class="meta-value">
                <template v-if="execution.workflow_id">
                  Workflow 执行
                  <span v-if="execution.agent_id" class="muted">
                    · {{ agentName(execution.agent_id) }} · v{{ execution.agent_version }}
                  </span>
                </template>
                <template v-else>
                  {{ agentName(execution.agent_id) }} · v{{ execution.agent_version }}
                </template>
              </span>
            </div>
            <div v-if="execution.workflow_id" class="meta-row">
              <span class="meta-label">Workflow ID</span>
              <span class="meta-value mono">{{ shortId(execution.workflow_id, 12) }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">执行 ID</span>
              <span class="meta-value mono">{{ shortId(execution.id, 12) }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">Trace</span>
              <span class="meta-value mono">{{ shortId(execution.trace_id, 14) }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">事件数</span>
              <span class="meta-value">{{ eventCount }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">知识引用</span>
              <span class="meta-value">{{ knowledgeCitations.length }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">Tokens</span>
              <span class="meta-value">{{ tokenTotal(usage).toLocaleString('zh-CN') }}</span>
            </div>
            <div class="token-breakdown">
              <span>输入 {{ usage.prompt_tokens || 0 }}</span>
              <span>输出 {{ usage.completion_tokens || 0 }}</span>
            </div>

            <div class="section-label">
              审批
              <span class="muted">{{ liveApprovals.length }}</span>
            </div>
            <div v-if="!liveApprovals.length" class="empty-hint small">暂无审批请求</div>
            <div v-for="approval in liveApprovals" :key="approval.id" class="approval-item">
              <div class="approval-top">
                <span class="approval-name">{{ approval.toolName }}</span>
                <el-tag
                  size="small"
                  :type="
                    approval.status === 'approved'
                      ? 'success'
                      : approval.status === 'rejected'
                        ? 'danger'
                        : 'warning'
                  "
                >
                  {{ approvalLabel(approval.status) }}
                </el-tag>
              </div>
              <div class="approval-comment">
                {{ approval.comment || (approval.status === 'pending' ? '等待人工决策' : '无备注') }}
              </div>
            </div>

            <div class="live-actions">
              <el-button size="small" type="primary" plain @click="openDetail">
                查看执行详情
              </el-button>
              <el-button
                v-if="streamError && isActive"
                size="small"
                @click="reconnectStream"
              >
                <el-icon><Refresh /></el-icon>
                重新连接
              </el-button>
            </div>
          </template>
        </div>
      </aside>
    </div>

    <ApprovalDialog
      v-model="approvalDialogVisible"
      :approval="dialogApproval"
      :loading="decisionLoading"
      @decide="decide"
    />
  </div>
</template>

<style scoped>
.workspace-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.workspace {
  display: grid;
  grid-template-columns: 264px minmax(0, 1fr) 320px;
  gap: 14px;
  height: calc(100vh - 148px);
  min-height: 560px;
}

.pane {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  box-shadow: 0 1px 2px rgba(23, 32, 51, 0.03);
}

.pane-head {
  padding: 14px 16px;
  border-bottom: 1px solid var(--app-border);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.pane-title {
  font-size: 14px;
  font-weight: 660;
}

.pane-sub {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--app-muted);
  display: flex;
  align-items: center;
  gap: 6px;
}

.pane-actions {
  display: flex;
  gap: 6px;
}

.pane-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px;
}

.conversation-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.conversation-item {
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  border: 1px solid transparent;
  transition: background 0.12s ease;
}

.conversation-item:hover {
  background: rgba(47, 107, 255, 0.06);
}

.conversation-item.active {
  background: rgba(47, 107, 255, 0.1);
  border-color: rgba(47, 107, 255, 0.24);
}

.conversation-title {
  font-size: 12.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conversation-meta {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: 5px;
  font-size: 10.5px;
  color: var(--app-muted);
}

.empty-hint {
  color: var(--app-muted);
  font-size: 12px;
  line-height: 1.6;
  padding: 8px 2px;
}

.empty-hint.small {
  font-size: 11.5px;
  padding: 4px 0;
}

.chat-pane {
  background: linear-gradient(180deg, #ffffff 0%, #fbfcff 100%);
}

.chat-head {
  padding: 12px 16px;
  border-bottom: 1px solid var(--app-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.agent-picker {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.agent-select {
  width: 220px;
  flex: 0 0 auto;
}

.option-version {
  float: right;
  color: var(--app-muted);
  font-size: 11px;
}

.agent-desc {
  font-size: 11.5px;
  color: var(--app-muted);
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.chat-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 18px 8px;
}

.empty-agents,
.empty-conversation {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
}

.empty-title {
  font-size: 15px;
  font-weight: 650;
}

.empty-sub {
  margin-top: 6px;
  font-size: 12px;
  color: var(--app-muted);
  max-width: 420px;
}

.tool-stack {
  margin-top: 8px;
  padding-top: 12px;
  border-top: 1px dashed var(--app-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tool-stack-title {
  font-size: 11.5px;
  font-weight: 650;
  color: var(--app-muted);
  letter-spacing: 0.4px;
}

.approval-prompt {
  margin-top: 12px;
  border: 1px solid #f3d19e;
  background: #fff8ec;
  border-radius: 10px;
  padding: 10px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  font-size: 12.5px;
}

.composer {
  border-top: 1px solid var(--app-border);
  padding: 12px 16px 14px;
  background: #fff;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.attachments {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.composer-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.hidden-input {
  display: none;
}

.hint {
  font-size: 11px;
  color: var(--app-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.live-body {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.meta-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
}

.meta-label {
  color: var(--app-muted);
}

.meta-value {
  font-weight: 550;
  text-align: right;
  word-break: break-all;
}

.token-breakdown {
  display: flex;
  gap: 14px;
  font-size: 11px;
  color: var(--app-muted);
  padding-bottom: 6px;
  border-bottom: 1px dashed var(--app-border);
}

.section-label {
  margin-top: 4px;
  font-size: 11.5px;
  font-weight: 650;
  color: var(--app-muted);
  letter-spacing: 0.4px;
  display: flex;
  gap: 6px;
}

.approval-item {
  border: 1px solid var(--app-border);
  border-radius: 10px;
  padding: 8px 10px;
  background: #fbfcfe;
}

.approval-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.approval-name {
  font-size: 12.5px;
  font-weight: 600;
  word-break: break-all;
}

.approval-comment {
  margin-top: 4px;
  font-size: 11px;
  color: var(--app-muted);
  word-break: break-word;
}

.live-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 4px;
}

.state-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #cbd5e1;
  display: inline-block;
}

.state-dot.live {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.16);
}

.state-dot.err {
  background: #ef4444;
}

@media (max-width: 1360px) {
  .workspace {
    grid-template-columns: 236px minmax(0, 1fr) 280px;
  }
}

@media (max-width: 1180px) {
  .workspace {
    grid-template-columns: 220px minmax(0, 1fr);
    height: auto;
  }

  .live-pane {
    grid-column: 1 / -1;
    min-height: 280px;
    max-height: 420px;
  }
}

@media (max-width: 820px) {
  .workspace {
    grid-template-columns: 1fr;
  }

  .conversations-pane {
    min-height: 220px;
    max-height: 260px;
  }

  .chat-pane {
    min-height: 600px;
  }

  .agent-select {
    width: 100%;
  }

  .agent-picker {
    flex-wrap: wrap;
  }
}
</style>
