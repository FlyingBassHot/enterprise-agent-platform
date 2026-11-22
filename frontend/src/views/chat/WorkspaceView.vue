<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Link, Promotion, Refresh, Upload } from '@element-plus/icons-vue'
import MessageBubble from '@/components/MessageBubble.vue'
import ToolCallCard from '@/components/ToolCallCard.vue'
import ApprovalDialog from '@/components/ApprovalDialog.vue'
import KnowledgeCitations from '@/components/KnowledgeCitations.vue'
import WorkspaceRootSidebar from '@/components/WorkspaceRootSidebar.vue'
import WorkspaceFilePreview from '@/components/WorkspaceFilePreview.vue'
import StatusTag from '@/components/StatusTag.vue'
import {
  approvalsApi,
  artifactsApi,
  executionsApi,
  workspaceApi,
} from '@/api/platform'
import { useExecutionStream, type ExecutionEventDto } from '@/composables/useExecutionStream'
import { useAuthStore } from '@/stores/auth'
import type {
  ApprovalInfo,
  ArtifactInfo,
  ConversationInfo,
  ExecutionDetail,
  ExecutionInfo,
  FileRootAccess,
  FileRootEntry,
  WorkspaceAgentInfo,
} from '@/types/api'
import {
  mergeKnowledgeCitations,
  parseKnowledgeCitations,
  type KnowledgeCitation,
} from '@/components/knowledgeCitations'
import { formatBytes, formatDate } from '@/utils/format'

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

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const agentId = computed(() => String(route.params.agentId || ''))

const agents = ref<WorkspaceAgentInfo[]>([])
const agentsLoading = ref(false)
const agentsError = ref('')

const conversations = ref<ConversationInfo[]>([])
const conversationsLoading = ref(false)
const conversationsError = ref('')

const fileRoots = ref<FileRootAccess[]>([])
const fileRootsLoading = ref(false)
const fileRootsError = ref('')

const activeRootId = ref<string | null>(null)
const pendingRootId = ref<string | null>(null)
const previewFile = ref<{
  rootId: string
  rootName: string
  entry: FileRootEntry
} | null>(null)

const messages = ref<ChatMessage[]>([])
const messagesLoading = ref(false)
const selectedConversationId = ref('')

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
let agentsToken = 0
let appliedSeq = 0

const {
  connected,
  error: streamError,
  start: startStream,
  stop: stopStream,
} = useExecutionStream({ onEvent: handleStreamEvent })

const selectedAgent = computed(
  () => agents.value.find((item) => item.id === agentId.value) || null,
)

const agentUnavailable = computed(() => !agentsLoading.value && !selectedAgent.value)

const activeRoot = computed(
  () => fileRoots.value.find((item) => item.id === activeRootId.value) || null,
)

const activeRootName = computed(
  () => activeRoot.value?.name || (activeRootId.value ? '其他会话' : '未归档工作区'),
)

const rootUnavailable = computed(
  () => activeRootId.value !== null && !activeRoot.value && !selectedConversationId.value,
)

const currentRootId = computed(() => pendingRootId.value ?? activeRootId.value)

const recentSessions = computed(() =>
  [...conversations.value]
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, 8),
)

const pendingApprovals = computed(() =>
  liveApprovals.value.filter((item) => item.status === 'pending'),
)

const selectedConversation = computed(
  () => conversations.value.find((item) => item.id === selectedConversationId.value) || null,
)

const conversationAgentMismatch = computed(() => {
  const conversation = selectedConversation.value
  if (!conversation) return false
  return conversation.agent_id !== agentId.value
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
    !rootUnavailable.value &&
    !sending.value &&
    !isActive.value &&
    !uploading.value &&
    !conversationAgentMismatch.value &&
    draft.value.trim().length > 0,
)

const composerDisabled = computed(
  () => !selectedAgent.value || rootUnavailable.value || isActive.value || sending.value,
)

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function errorStatus(value: unknown): number | null {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const status = (value as { response?: { status?: number } }).response?.status
    return typeof status === 'number' ? status : null
  }
  return null
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
  const token = ++agentsToken
  agentsLoading.value = true
  agentsError.value = ''
  try {
    const list = await workspaceApi.agents()
    if (token !== agentsToken) return
    agents.value = list.filter((item) => item.status === 'published')
  } catch (err) {
    if (token !== agentsToken) return
    agentsError.value = describeError(err)
    const status = errorStatus(err)
    if (status === 401 || status === 403) agents.value = []
  } finally {
    if (token === agentsToken) agentsLoading.value = false
  }
}

async function loadConversations() {
  const currentAgentId = agentId.value
  if (!currentAgentId) {
    conversations.value = []
    return
  }
  conversationsLoading.value = true
  conversationsError.value = ''
  try {
    const list = await workspaceApi.conversations(currentAgentId, { limit: 100 })
    if (currentAgentId !== agentId.value) return
    conversations.value = list.filter((item) => item.agent_id === currentAgentId)
  } catch (err) {
    if (currentAgentId === agentId.value) {
      conversations.value = []
      conversationsError.value = describeError(err)
    }
  } finally {
    if (currentAgentId === agentId.value) conversationsLoading.value = false
  }
}

async function loadFileRoots() {
  const currentAgentId = agentId.value
  if (!currentAgentId) {
    fileRoots.value = []
    return
  }
  fileRootsLoading.value = true
  fileRootsError.value = ''
  try {
    const list = await workspaceApi.fileRoots(currentAgentId)
    if (currentAgentId === agentId.value) fileRoots.value = list
  } catch (err) {
    if (currentAgentId === agentId.value) {
      fileRoots.value = []
      fileRootsError.value = describeError(err)
    }
  } finally {
    if (currentAgentId === agentId.value) fileRootsLoading.value = false
  }
}

async function loadWorkspaceSide() {
  await loadFileRoots()
}

async function loadMessages(conversationId: string) {
  const token = ++messagesToken
  const currentAgentId = agentId.value
  messagesLoading.value = true
  chatError.value = ''
  try {
    const list = await workspaceApi.messages(currentAgentId, conversationId)
    if (
      token !== messagesToken ||
      selectedConversationId.value !== conversationId ||
      currentAgentId !== agentId.value
    ) {
      return
    }
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

function navigateSession(conversationId?: string) {
  const base = `/workspaces/${agentId.value}`
  const target = conversationId ? `${base}/chat/${conversationId}` : base
  const current = String(route.params.sessionId || '')
  if (current === (conversationId || '')) return
  void router.push(target)
}

function clearChatState() {
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

const LAST_SESSION_PREFIX = 'workspace_last_session'

function lastSessionKey(): string {
  return `${LAST_SESSION_PREFIX}:${auth.user?.id || 'anon'}:${agentId.value}`
}

function rememberSession(conversationId: string) {
  if (!conversationId || !agentId.value) return
  try {
    localStorage.setItem(lastSessionKey(), conversationId)
  } catch {
    // Storage may be unavailable; restoring the last session is best-effort.
  }
}

function forgetSession() {
  try {
    localStorage.removeItem(lastSessionKey())
  } catch {
    // Ignore storage failures.
  }
}

async function restoreLastSession() {
  if (isActive.value || sending.value) return
  if (route.params.sessionId || selectedConversationId.value) return
  let stored = ''
  try {
    stored = localStorage.getItem(lastSessionKey()) || ''
  } catch {
    return
  }
  if (!stored) return
  const requestedAgentId = agentId.value
  const known = conversations.value.find(
    (item) => item.id === stored && item.agent_id === requestedAgentId,
  )
  if (known) {
    await selectConversation(known)
    return
  }
  try {
    const conversation = await workspaceApi.conversation(requestedAgentId, stored)
    if (requestedAgentId !== agentId.value || route.params.sessionId) return
    if (conversation.agent_id !== requestedAgentId) {
      forgetSession()
      return
    }
    conversations.value = [
      conversation,
      ...conversations.value.filter((item) => item.id !== conversation.id),
    ]
    await selectConversation(conversation)
  } catch (err) {
    // Only a confirmed missing session clears the preference; network
    // failures keep it for the next visit.
    if (errorStatus(err) === 404 && requestedAgentId === agentId.value) {
      forgetSession()
    }
  }
}

async function selectConversation(conversation: ConversationInfo, syncRoute = true) {
  if (conversation.agent_id !== agentId.value) {
    ElMessage.warning('该对话不属于当前 Agent，已忽略')
    return
  }
  if (isActive.value || sending.value) {
    ElMessage.warning('当前执行仍在进行，请等待结束或先取消执行')
    return
  }
  selectedConversationId.value = conversation.id
  activeRootId.value = conversation.root_id
  pendingRootId.value = null
  previewFile.value = null
  rememberSession(conversation.id)
  messagesToken += 1
  draft.value = ''
  attachments.value = []
  execution.value = null
  executionDetail.value = null
  liveStatus.value = ''
  resetLiveState()
  chatError.value = ''
  stopStream()
  if (syncRoute) navigateSession(conversation.id)
  await loadMessages(conversation.id)
}

function newChat(rootId: string | null = null) {
  if (isActive.value || sending.value) {
    ElMessage.warning('当前执行仍在进行，请等待结束或先取消执行')
    return
  }
  activeRootId.value = rootId
  pendingRootId.value = rootId
  previewFile.value = null
  forgetSession()
  clearChatState()
  navigateSession()
}

function selectRoot(rootId: string | null) {
  if (isActive.value || sending.value) return
  activeRootId.value = rootId
  pendingRootId.value = rootId
  previewFile.value = null
  if (selectedConversationId.value) {
    clearChatState()
    navigateSession()
  }
}

function selectFile(payload: {
  rootId: string
  rootName: string
  entry: FileRootEntry
}) {
  previewFile.value = payload
}

function rootLabel(rootId: string | null): string {
  if (!rootId) return '未归档'
  return fileRoots.value.find((root) => root.id === rootId)?.name || '已撤销工作区'
}

async function refreshWorkspace() {
  await Promise.all([loadConversations(), loadFileRoots()])
}

async function applyRouteSession() {
  if (isActive.value || sending.value) return
  const sessionId = String(route.params.sessionId || '')
  if (!sessionId) {
    if (selectedConversationId.value) clearChatState()
    return
  }
  if (sessionId === selectedConversationId.value) return
  if (!agentId.value || !selectedAgent.value) return

  const requestedAgentId = agentId.value
  const known = conversations.value.find(
    (item) => item.id === sessionId && item.agent_id === requestedAgentId,
  )
  if (known) {
    await selectConversation(known, false)
    return
  }

  try {
    const conversation = await workspaceApi.conversation(requestedAgentId, sessionId)
    if (
      requestedAgentId !== agentId.value ||
      String(route.params.sessionId || '') !== sessionId
    ) {
      return
    }
    if (conversation.agent_id !== requestedAgentId) {
      throw new Error('该会话不属于当前 Agent')
    }
    conversations.value = [
      conversation,
      ...conversations.value.filter((item) => item.id !== conversation.id),
    ]
    await selectConversation(conversation, false)
  } catch (err) {
    if (
      requestedAgentId !== agentId.value ||
      String(route.params.sessionId || '') !== sessionId
    ) {
      return
    }
    clearChatState()
    chatError.value = describeError(err)
    void router.replace(`/workspaces/${requestedAgentId}`)
  }
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
  const requestAgentId = agentId.value
  const requestConversationId = selectedConversationId.value
  const requestRootId = requestConversationId
    ? fileRoots.value.some((root) => root.id === activeRootId.value)
      ? activeRootId.value
      : null
    : currentRootId.value
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
      agent_id: requestAgentId,
      root_id: requestRootId || null,
      input: text,
      conversation_id: requestConversationId || null,
      artifact_ids: artifactIds,
    })
    if (requestAgentId !== agentId.value) return
    execution.value = created
    executionDetail.value = null
    liveStatus.value = created.status
    pendingRootId.value = null
    if (created.conversation_id) {
      selectedConversationId.value = created.conversation_id
      activeRootId.value = requestRootId
      rememberSession(created.conversation_id)
      if (String(route.params.sessionId || '') !== created.conversation_id) {
        void router.replace(`/workspaces/${requestAgentId}/chat/${created.conversation_id}`)
      }
    }
    stopStream()
    startStream(created.id, 0)
    void loadConversations()
  } catch (err) {
    if (requestAgentId === agentId.value) {
      messages.value = messages.value.filter((item) => item.id !== optimistic.id)
      draft.value = text
      attachments.value = uploaded
      chatError.value = describeError(err)
    }
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

function backToWorkspaces() {
  void router.push('/workspaces')
}

const totalContentLength = computed(() =>
  messages.value.reduce((total, item) => total + item.content.length, 0),
)

watch([() => messages.value.length, totalContentLength], async () => {
  await nextTick()
  const element = chatScroll.value
  if (element) element.scrollTop = element.scrollHeight
})

watch(
  () => route.params.agentId,
  async (next, previous) => {
    if (!next || next === previous) return
    clearChatState()
    conversations.value = []
    conversationsError.value = ''
    fileRoots.value = []
    fileRootsError.value = ''
    activeRootId.value = null
    pendingRootId.value = null
    previewFile.value = null
    await loadAgents()
    if (selectedAgent.value) {
      await loadConversations()
      loadWorkspaceSide()
    }
    await applyRouteSession()
    await restoreLastSession()
  },
)

watch(
  () => route.params.sessionId,
  async (next) => {
    const sessionId = String(next || '')
    if (sessionId === selectedConversationId.value) return
    await applyRouteSession()
  },
)

onMounted(async () => {
  await loadAgents()
  if (selectedAgent.value) {
    await loadConversations()
    loadWorkspaceSide()
  }
  await applyRouteSession()
  await restoreLastSession()
})
</script>

<template>
  <div class="workspace-page">
    <header class="immersive-bar">
      <el-button class="exit-btn" text @click="backToWorkspaces">
        <el-icon><ArrowLeft /></el-icon>
        退出工作区
      </el-button>
      <div class="immersive-identity">
        <span class="agent-name">{{ selectedAgent?.name || 'Agent 不可用' }}</span>
        <StatusTag v-if="selectedAgent" :status="selectedAgent.status" />
        <span class="workspace-chip">
          {{ previewFile ? previewFile.rootName : activeRootName }}
        </span>
        <StatusTag v-if="status" :status="status" />
      </div>
      <div class="immersive-actions">
        <el-button
          v-if="pendingApprovals.length"
          size="small"
          type="warning"
          plain
          @click="approvalDialogVisible = true"
        >
          待审批 {{ pendingApprovals.length }}
        </el-button>
        <el-button
          v-if="canCancel"
          link
          type="danger"
          :loading="cancelLoading"
          @click="cancelExecution"
        >
          取消执行
        </el-button>
        <el-button v-if="execution" link type="primary" @click="openDetail">
          <el-icon><Link /></el-icon>
          执行详情
        </el-button>
        <el-button
          :icon="Refresh"
          circle
          size="small"
          :loading="conversationsLoading || fileRootsLoading"
          title="刷新工作区"
          @click="refreshWorkspace"
        />
      </div>
    </header>

    <div class="workspace">
      <aside class="workspace-left">
        <WorkspaceRootSidebar
          :agent-id="agentId"
          :roots="fileRoots"
          :sessions="conversations"
          :active-root-id="activeRootId"
          :active-session-id="selectedConversationId"
          :loading-sessions="conversationsLoading"
          @select-root="selectRoot"
          @new-session="newChat"
          @select-session="selectConversation"
          @select-file="selectFile"
          @refresh="refreshWorkspace"
        />
        <div v-if="fileRootsError" class="side-error">{{ fileRootsError }}</div>
        <div v-if="conversationsError" class="side-error">{{ conversationsError }}</div>
      </aside>

      <section class="pane chat-pane">
        <div class="chat-head">
          <div class="chat-title">
            <span class="session-title">
              {{ selectedConversation?.title || (pendingRootId ? `${activeRootName} · 新会话` : '新会话') }}
            </span>
            <span class="session-sub">{{ activeRootName }} · {{ selectedAgent?.name || 'Agent' }}</span>
          </div>
          <div class="chat-head-right">
            <StatusTag v-if="status" :status="status" />
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
          <div v-if="agentUnavailable" class="empty-agents">
            <el-empty
              :description="
                agentsError || '该 Agent 不存在、已停用或当前账号无权访问'
              "
            >
              <el-button type="primary" @click="backToWorkspaces">返回 Agent 选择</el-button>
            </el-empty>
          </div>

          <template v-else>
            <div v-if="!messages.length && !isActive" class="empty-conversation">
              <div class="empty-title">
                {{
                  selectedAgent
                    ? `与 ${selectedAgent.name} 开始对话`
                    : agentsLoading
                      ? '正在加载 Agent'
                      : 'Agent 不可用'
                }}
              </div>
              <div class="empty-sub">
                左侧按文件根目录划分工作区；发送消息即可在当前工作区创建会话。
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
                <template v-if="rootUnavailable">当前工作区已不可用，请选择其他工作区</template>
                <template v-else-if="conversationAgentMismatch">当前对话属于其他 Agent，请新建对话</template>
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

      <aside class="workspace-right">
        <WorkspaceFilePreview
          v-if="previewFile"
          :agent-id="agentId"
          :root-id="previewFile.rootId"
          :root-name="previewFile.rootName"
          :file="previewFile.entry"
          @close="previewFile = null"
        />

        <template v-else>
          <div class="right-head">
            <div>
              <div class="right-title">最近会话</div>
              <div class="right-sub">{{ activeRootName }} · {{ selectedAgent?.name || 'Agent' }}</div>
            </div>
            <el-button
              link
              size="small"
              :icon="Refresh"
              :loading="conversationsLoading"
              title="刷新会话"
              @click="refreshWorkspace"
            />
          </div>
          <div class="right-body">
            <div v-if="!recentSessions.length" class="empty-hint">
              该 Agent 还没有会话，发送第一条消息即可创建。
            </div>
            <button
              v-for="session in recentSessions"
              :key="session.id"
              type="button"
              class="recent-session"
              :class="{ active: session.id === selectedConversationId }"
              @click="selectConversation(session)"
            >
              <span class="recent-title">{{ session.title || '未命名会话' }}</span>
              <span class="recent-meta">
                <span class="recent-root">{{ rootLabel(session.root_id) }}</span>
                <span>{{ formatDate(session.updated_at) }}</span>
              </span>
            </button>
          </div>
        </template>
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

.agent-head {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex-wrap: wrap;
}

.back-link {
  flex: 0 0 auto;
}

.agent-identity {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex-wrap: wrap;
}

.agent-name {
  font-size: 14px;
  font-weight: 660;
  white-space: nowrap;
}

.agent-version {
  font-size: 11px;
  color: var(--app-muted);
}

.agent-desc {
  font-size: 11.5px;
  color: var(--app-muted);
  max-width: 360px;
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

.file-roots-wrap {
  min-height: 48px;
}

.task-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  width: 100%;
  padding: 7px 9px;
  border: 1px solid var(--app-border);
  border-radius: 9px;
  background: var(--app-surface);
  text-align: left;
  font: inherit;
  cursor: pointer;
  transition: border-color 0.12s ease;
}

.task-item:hover,
.task-item.active {
  border-color: rgba(47, 107, 255, 0.4);
  background: rgba(47, 107, 255, 0.04);
}

.task-goal {
  font-size: 12px;
  font-weight: 550;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 10.5px;
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

  .agent-head {
    flex-wrap: wrap;
  }

  .agent-desc {
    max-width: 100%;
    white-space: normal;
  }
}

/* Immersive workspace layout */
.workspace-page {
  height: 100vh;
  min-height: 0;
  gap: 0;
  background: var(--app-bg);
  overflow: hidden;
}

.immersive-bar {
  height: 54px;
  flex: none;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 16px;
  background: var(--app-surface);
  border-bottom: 1px solid var(--app-border);
}

.exit-btn {
  flex: none;
  font-size: 12.5px;
  color: var(--app-muted);
}

.immersive-identity {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1 1 auto;
}

.immersive-identity .agent-name {
  font-size: 15px;
  font-weight: 680;
  letter-spacing: -0.2px;
  white-space: nowrap;
}

.workspace-chip {
  max-width: 240px;
  padding: 3px 9px;
  border-radius: 999px;
  background: rgba(47, 107, 255, 0.09);
  color: #2f6bff;
  font-size: 11.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.immersive-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

.workspace {
  height: calc(100vh - 54px);
  min-height: 0;
  grid-template-columns: 280px minmax(0, 1fr) 360px;
  gap: 12px;
  padding: 12px;
}

.workspace-left,
.workspace-right {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: 0 1px 2px rgba(23, 32, 51, 0.03);
}

.workspace-left {
  overflow-y: auto;
}

.side-error {
  padding: 8px 14px 12px;
  color: var(--el-color-danger);
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}

.chat-title {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}

.session-title {
  font-size: 14px;
  font-weight: 650;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-sub {
  font-size: 11px;
  color: var(--app-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.right-head {
  flex: none;
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--app-border);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}

.right-title {
  font-size: 13px;
  font-weight: 660;
}

.right-sub {
  margin-top: 3px;
  font-size: 11px;
  color: var(--app-muted);
}

.right-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.recent-session {
  display: flex;
  flex-direction: column;
  gap: 5px;
  width: 100%;
  padding: 10px 11px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    background 0.12s ease,
    border-color 0.12s ease;
}

.recent-session:hover {
  background: rgba(47, 107, 255, 0.05);
}

.recent-session.active {
  background: rgba(47, 107, 255, 0.09);
  border-color: rgba(47, 107, 255, 0.24);
}

.recent-title {
  font-size: 12.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.recent-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 10.5px;
  color: var(--app-muted);
}

.recent-root {
  max-width: 60%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 1320px) {
  .workspace {
    grid-template-columns: 250px minmax(0, 1fr) 310px;
  }
}

@media (max-width: 1180px) {
  .workspace {
    grid-template-columns: 230px minmax(0, 1fr);
    height: calc(100vh - 54px);
  }

  .workspace-right {
    display: none;
  }
}

@media (max-width: 780px) {
  .workspace {
    grid-template-columns: 1fr;
    height: auto;
    overflow-y: auto;
  }

  .workspace-left {
    max-height: 300px;
  }

  .chat-pane {
    min-height: 560px;
  }

  .immersive-identity .agent-name {
    font-size: 13px;
  }

  .workspace-chip {
    max-width: 120px;
  }
}
</style>
