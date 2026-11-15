import client from './client'
import type {
  AccessPolicyInfo,
  AgentConfig,
  AgentInfo,
  AgentValidation,
  AgentVersion,
  ModelConfig,
  ModelHealth,
  ModelProvider,
  PermissionInfo,
  PromptInfo,
  PromptVariable,
  PromptVersion,
  ResolvedPrompt,
  RoleInfo,
  ToolInfo,
  ToolTestResult,
  ExecutionInfo,
  ExecutionDetail,
  ExecutionEventInfo,
  ConversationInfo,
  MessageInfo,
  ApprovalInfo,
  ArtifactInfo,
  AuditLogInfo,
  AuditRulesResponse,
  AuditReportResponse,
  KnowledgeBaseInfo,
  KnowledgeDocumentInfo,
  KnowledgeDocumentChunk,
  KnowledgeSearchResponse,
  MCPServerInfo,
  MCPSyncResult,
  SkillInfo,
  SkillVersionInfo,
  WorkflowDefinition,
  WorkflowInfo,
  WorkflowValidation,
  WorkflowVersionInfo,
  ToolCallRecord,
  UserInfo,
} from '@/types/api'

export const agentsApi = {
  list: () => client.get<AgentInfo[]>('/agents').then((r) => r.data),
  get: (id: string) => client.get<AgentInfo>(`/agents/${id}`).then((r) => r.data),
  create: (payload: { name: string; description: string; config: AgentConfig }) =>
    client.post<AgentInfo>('/agents', payload).then((r) => r.data),
  update: (id: string, payload: { name?: string; description?: string; config?: AgentConfig }) =>
    client.put<AgentInfo>(`/agents/${id}`, payload).then((r) => r.data),
  remove: (id: string) => client.delete(`/agents/${id}`),
  validate: (id: string) =>
    client.post<AgentValidation>(`/agents/${id}/validate`).then((r) => r.data),
  publish: (id: string) => client.post<AgentInfo>(`/agents/${id}/publish`).then((r) => r.data),
  disable: (id: string) => client.post<AgentInfo>(`/agents/${id}/disable`).then((r) => r.data),
  versions: (id: string) =>
    client.get<AgentVersion[]>(`/agents/${id}/versions`).then((r) => r.data),
}

export const promptsApi = {
  list: () => client.get<PromptInfo[]>('/prompts').then((r) => r.data),
  get: (id: string) => client.get<PromptInfo>(`/prompts/${id}`).then((r) => r.data),
  create: (payload: { name: string; description: string }) =>
    client.post<PromptInfo>('/prompts', payload).then((r) => r.data),
  update: (id: string, payload: { name?: string; description?: string }) =>
    client.put<PromptInfo>(`/prompts/${id}`, payload).then((r) => r.data),
  remove: (id: string) => client.delete(`/prompts/${id}`),
  versions: (id: string) =>
    client.get<PromptVersion[]>(`/prompts/${id}/versions`).then((r) => r.data),
  createVersion: (
    id: string,
    payload: {
      system_prompt: string
      task_prompt: string
      tool_instructions: string
      variables: PromptVariable[]
      publish: boolean
    },
  ) => client.post<PromptVersion>(`/prompts/${id}/versions`, payload).then((r) => r.data),
  resolve: (id: string, payload: { version_id?: string | null; variables: Record<string, string> }) =>
    client.post<ResolvedPrompt>(`/prompts/${id}/resolve`, payload).then((r) => r.data),
}

export const modelsApi = {
  providers: () =>
    client.get<ModelProvider[]>('/model-providers').then((r) => r.data),
  createProvider: (payload: Record<string, unknown>) =>
    client.post<ModelProvider>('/model-providers', payload).then((r) => r.data),
  updateProvider: (id: string, payload: Record<string, unknown>) =>
    client.patch<ModelProvider>(`/model-providers/${id}`, payload).then((r) => r.data),
  removeProvider: (id: string) => client.delete(`/model-providers/${id}`),
  configs: () => client.get<ModelConfig[]>('/model-configs').then((r) => r.data),
  createConfig: (payload: Record<string, unknown>) =>
    client.post<ModelConfig>('/model-configs', payload).then((r) => r.data),
  updateConfig: (id: string, payload: Record<string, unknown>) =>
    client.patch<ModelConfig>(`/model-configs/${id}`, payload).then((r) => r.data),
  removeConfig: (id: string) => client.delete(`/model-configs/${id}`),
  health: (id: string) =>
    client.post<ModelHealth>(`/model-configs/${id}/health`).then((r) => r.data),
}

export const usersApi = {
  list: () => client.get<UserInfo[]>('/users').then((r) => r.data),
  create: (payload: Record<string, unknown>) =>
    client.post<UserInfo>('/users', payload).then((r) => r.data),
  update: (id: string, payload: Record<string, unknown>) =>
    client.patch<UserInfo>(`/users/${id}`, payload).then((r) => r.data),
  roles: () => client.get<RoleInfo[]>('/roles').then((r) => r.data),
  createRole: (payload: Record<string, unknown>) =>
    client.post<RoleInfo>('/roles', payload).then((r) => r.data),
  updateRole: (id: string, payload: Record<string, unknown>) =>
    client.patch<RoleInfo>(`/roles/${id}`, payload).then((r) => r.data),
  permissions: () => client.get<PermissionInfo[]>('/permissions').then((r) => r.data),
}

export const toolsApi = {
  list: () => client.get<ToolInfo[]>('/tools').then((r) => r.data),
  get: (id: string) => client.get<ToolInfo>(`/tools/${id}`).then((r) => r.data),
  create: (payload: Record<string, unknown>) =>
    client.post<ToolInfo>('/tools', payload).then((r) => r.data),
  update: (id: string, payload: Record<string, unknown>) =>
    client.put<ToolInfo>(`/tools/${id}`, payload).then((r) => r.data),
  remove: (id: string) => client.delete(`/tools/${id}`),
  test: (id: string, payload: { arguments: Record<string, unknown> }) =>
    client.post<ToolTestResult>(`/tools/${id}/test`, payload).then((r) => r.data),
  enable: (id: string) => client.post<ToolInfo>(`/tools/${id}/enable`).then((r) => r.data),
  disable: (id: string) => client.post<ToolInfo>(`/tools/${id}/disable`).then((r) => r.data),
}

export const executionsApi = {
  create: (payload: {
    agent_id?: string
    workflow_id?: string
    input: string
    conversation_id?: string | null
    artifact_ids?: string[]
    variables?: Record<string, unknown>
    metadata?: Record<string, unknown>
  }) => client.post<ExecutionInfo>('/executions', payload).then((r) => r.data),
  list: (params?: { status?: string; agent_id?: string; limit?: number; offset?: number }) =>
    client.get<ExecutionInfo[]>('/executions', { params }).then((r) => r.data),
  get: (id: string) =>
    client.get<ExecutionDetail>(`/executions/${id}`).then((r) => r.data),
  events: (id: string, afterSeq = 0, limit = 500) =>
    client
      .get<ExecutionEventInfo[]>(`/executions/${id}/events`, { params: { after_seq: afterSeq, limit } })
      .then((r) => r.data),
  cancel: (id: string) =>
    client.post<{ execution_id: string; status: string }>(`/executions/${id}/cancel`).then((r) => r.data),
  resume: (id: string) =>
    client.post<ExecutionInfo>(`/executions/${id}/resume`).then((r) => r.data),
  toolCalls: (id: string) =>
    client.get<ToolCallRecord[]>(`/executions/${id}/tool-calls`).then((r) => r.data),
}

export const workflowApi = {
  list: () => client.get<WorkflowInfo[]>('/workflows').then((r) => r.data),
  create: (payload: { name: string; description: string; definition: WorkflowDefinition }) =>
    client.post<WorkflowInfo>('/workflows', payload).then((r) => r.data),
  get: (id: string) => client.get<WorkflowInfo>(`/workflows/${id}`).then((r) => r.data),
  update: (
    id: string,
    payload: { name?: string; description?: string; definition?: WorkflowDefinition },
  ) => client.put<WorkflowInfo>(`/workflows/${id}`, payload).then((r) => r.data),
  remove: (id: string) => client.delete(`/workflows/${id}`),
  validate: (id: string) =>
    client.post<WorkflowValidation>(`/workflows/${id}/validate`).then((r) => r.data),
  publish: (id: string) =>
    client.post<WorkflowInfo>(`/workflows/${id}/publish`).then((r) => r.data),
  disable: (id: string) =>
    client.post<WorkflowInfo>(`/workflows/${id}/disable`).then((r) => r.data),
  versions: (id: string) =>
    client.get<WorkflowVersionInfo[]>(`/workflows/${id}/versions`).then((r) => r.data),
  execute: (
    id: string,
    payload: { input: string; variables?: Record<string, unknown>; metadata?: Record<string, unknown> },
  ) => client.post<ExecutionInfo>(`/workflows/${id}/execute`, payload).then((r) => r.data),
}

export const conversationsApi = {
  list: () => client.get<ConversationInfo[]>('/conversations').then((r) => r.data),
  create: (payload: { agent_id: string; title?: string }) =>
    client.post<ConversationInfo>('/conversations', payload).then((r) => r.data),
  get: (id: string) => client.get<ConversationInfo>(`/conversations/${id}`).then((r) => r.data),
  messages: (id: string) =>
    client.get<MessageInfo[]>(`/conversations/${id}/messages`).then((r) => r.data),
}

export const approvalsApi = {
  list: (params?: { status?: string; execution_id?: string }) =>
    client.get<ApprovalInfo[]>('/approvals', { params }).then((r) => r.data),
  approve: (id: string, comment = '') =>
    client.post<ApprovalInfo>(`/approvals/${id}/approve`, { comment }).then((r) => r.data),
  reject: (id: string, comment = '') =>
    client.post<ApprovalInfo>(`/approvals/${id}/reject`, { comment }).then((r) => r.data),
}

export const artifactsApi = {
  upload: (file: File, conversationId?: string | null) => {
    const form = new FormData()
    form.append('file', file)
    if (conversationId) form.append('conversation_id', conversationId)
    return client
      .post<ArtifactInfo>('/artifacts', form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },
  meta: (id: string) => client.get<ArtifactInfo>(`/artifacts/${id}/meta`).then((r) => r.data),
  downloadUrl: (id: string) => `/api/v1/artifacts/${id}`,
}

export const auditApi = {
  rules: () => client.get<AuditRulesResponse>('/audit/rules').then((r) => r.data),
  report: (payload: {
    artifact_id: string
    options?: Record<string, unknown>
    max_anomalies?: number
  }) => client.post<AuditReportResponse>('/audit/report', payload).then((r) => r.data),
  sampleUrl: '/api/v1/audit/sample-file',
}

export const knowledgeApi = {
  listBases: () => client.get<KnowledgeBaseInfo[]>('/knowledge-bases').then((r) => r.data),
  createBase: (payload: Record<string, unknown>) =>
    client.post<KnowledgeBaseInfo>('/knowledge-bases', payload).then((r) => r.data),
  getBase: (id: string) =>
    client.get<KnowledgeBaseInfo>(`/knowledge-bases/${id}`).then((r) => r.data),
  updateBase: (id: string, payload: Record<string, unknown>) =>
    client.patch<KnowledgeBaseInfo>(`/knowledge-bases/${id}`, payload).then((r) => r.data),
  removeBase: (id: string) => client.delete(`/knowledge-bases/${id}`),
  documents: (baseId: string) =>
    client
      .get<KnowledgeDocumentInfo[]>(`/knowledge-bases/${baseId}/documents`)
      .then((r) => r.data),
  uploadDocument: (baseId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return client
      .post<KnowledgeDocumentInfo>(`/knowledge-bases/${baseId}/documents`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      .then((r) => r.data)
  },
  getDocument: (id: string) =>
    client.get<KnowledgeDocumentInfo>(`/documents/${id}`).then((r) => r.data),
  removeDocument: (id: string) => client.delete(`/documents/${id}`),
  reprocessDocument: (id: string) =>
    client.post<KnowledgeDocumentInfo>(`/documents/${id}/reprocess`).then((r) => r.data),
  chunks: (documentId: string) =>
    client.get<KnowledgeDocumentChunk[]>(`/documents/${documentId}/chunks`).then((r) => r.data),
  search: (
    baseId: string,
    payload: { query: string; top_k?: number; metadata_filter?: Record<string, unknown> },
  ) =>
    client
      .post<KnowledgeSearchResponse>(`/knowledge-bases/${baseId}/search`, payload)
      .then((r) => r.data.results),
}

export const skillsApi = {
  list: () => client.get<SkillInfo[]>('/skills').then((r) => r.data),
  sync: () => client.post<SkillInfo[]>('/skills/sync').then((r) => r.data),
  get: (id: string) => client.get<SkillInfo>(`/skills/${id}`).then((r) => r.data),
  enable: (id: string) => client.post<SkillInfo>(`/skills/${id}/enable`).then((r) => r.data),
  disable: (id: string) => client.post<SkillInfo>(`/skills/${id}/disable`).then((r) => r.data),
  versions: (id: string) =>
    client.get<SkillVersionInfo[]>(`/skills/${id}/versions`).then((r) => r.data),
}

export const mcpApi = {
  list: () => client.get<MCPServerInfo[]>('/mcp-servers').then((r) => r.data),
  create: (payload: Record<string, unknown>) =>
    client.post<MCPServerInfo>('/mcp-servers', payload).then((r) => r.data),
  update: (id: string, payload: Record<string, unknown>) =>
    client.patch<MCPServerInfo>(`/mcp-servers/${id}`, payload).then((r) => r.data),
  remove: (id: string) => client.delete(`/mcp-servers/${id}`),
  sync: (id: string) =>
    client.post<MCPSyncResult>(`/mcp-servers/${id}/sync`).then((r) => r.data),
  tools: (id: string) =>
    client.get<ToolInfo[]>(`/mcp-servers/${id}/tools`).then((r) => r.data),
}

export const auditLogsApi = {
  list: (params?: {
    action?: string
    actor_id?: string
    resource_type?: string
    result?: string
    after?: string
    before?: string
    limit?: number
    offset?: number
  }) => client.get<AuditLogInfo[]>('/audit-logs', { params }).then((r) => r.data),
  get: (id: string) => client.get<AuditLogInfo>(`/audit-logs/${id}`).then((r) => r.data),
}

export const accessPoliciesApi = {
  list: () => client.get<AccessPolicyInfo[]>('/access-policies').then((r) => r.data),
  create: (payload: Record<string, unknown>) =>
    client.post<AccessPolicyInfo>('/access-policies', payload).then((r) => r.data),
  update: (id: string, payload: Record<string, unknown>) =>
    client.patch<AccessPolicyInfo>(`/access-policies/${id}`, payload).then((r) => r.data),
  remove: (id: string) => client.delete(`/access-policies/${id}`),
}
