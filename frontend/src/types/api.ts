export interface PermissionInfo {
  id: string
  code: string
  name: string
  description: string
  resource_type: string
}

export interface RoleInfo {
  id: string
  name: string
  description: string
  is_system: boolean
  permissions: string[]
  created_at: string
}

export interface UserInfo {
  id: string
  username: string
  email: string | null
  display_name: string
  is_active: boolean
  is_superuser: boolean
  department_id: string | null
  roles: string[]
  created_at: string
}

export interface ModelProvider {
  id: string
  name: string
  provider_type: string
  base_url: string | null
  enabled: boolean
  description: string
  has_api_key: boolean
  masked_api_key: string
  extra: Record<string, unknown>
  created_at: string
}

export interface ModelParams {
  temperature: number
  max_tokens: number
  top_p: number
}

export interface ModelCapabilities {
  supports_tools: boolean
  supports_streaming: boolean
  supports_json: boolean
}

export interface ModelConfig {
  id: string
  name: string
  display_name: string
  provider_id: string
  provider_name: string
  provider_type: string
  model_name: string
  params: ModelParams
  capabilities: ModelCapabilities
  enabled: boolean
  is_default: boolean
  description: string
  extra: Record<string, unknown>
  created_at: string
}

export interface ModelHealth {
  ok: boolean
  message: string
  latency_ms: number
}

export interface PromptVariable {
  name: string
  required: boolean
  default: string
}

export interface PromptInfo {
  id: string
  name: string
  description: string
  current_version: number
  status: string
  created_at: string
  updated_at: string
}

export interface PromptVersion {
  id: string
  prompt_id: string
  version: number
  system_prompt: string
  task_prompt: string
  tool_instructions: string
  variables: PromptVariable[]
  created_by: string | null
  created_at: string
}

export interface ResolvedPrompt {
  prompt_id: string
  version_id: string
  version: number
  system_prompt: string
  task_prompt: string
  tool_instructions: string
}

export interface AgentModelBinding {
  config_id: string
  temperature: number
  max_tokens: number
  top_p: number
}

export interface AgentPromptBinding {
  prompt_id: string
  version_id: string | null
  variables: Record<string, string>
}

export interface AgentExecutionSettings {
  mode: 'agent' | 'workflow' | 'hybrid'
  workflow_id?: string | null
  max_iterations: number
  timeout_seconds: number
  approval_risk_levels: string[]
  streaming: boolean
}

export interface AgentPermissionSettings {
  required_permissions: string[]
  allowed_roles: string[]
}

export interface AgentConfig {
  model: AgentModelBinding
  prompt: AgentPromptBinding
  tools: string[]
  knowledge_base_ids: string[]
  skill_names: string[]
  retrieval: AgentKnowledgeSettings
  execution: AgentExecutionSettings
  permissions: AgentPermissionSettings
  metadata: Record<string, unknown>
}

export interface AgentInfo {
  id: string
  name: string
  description: string
  status: string
  owner_id: string
  draft_config: AgentConfig
  current_version_id: string | null
  current_version: number | null
  published_at: string | null
  created_at: string
  updated_at: string
}

export interface WorkspaceAgentInfo {
  id: string
  name: string
  description: string
  status: string
  current_version: number | null
  published_at: string | null
}

export type FileRootPermission = 'read' | 'write'

export interface FileRootInfo {
  id: string
  name: string
  description: string
  owner_id: string
  created_at: string
  updated_at: string
}

export interface FileRootAccess {
  id: string
  name: string
  description: string
  permission: FileRootPermission
}

export interface FileRootEntry {
  name: string
  path: string
  type: 'file' | 'directory'
  size: number
  modified_at: string | null
}

export interface FileRootGrant {
  id: string
  agent_id: string
  root_id: string
  permission: FileRootPermission
  agent_name: string
  created_at: string
  updated_at: string
}

export interface TaskInfo {
  id: string
  agent_id: string
  user_id: string
  conversation_id: string | null
  goal: string
  constraints: Record<string, unknown>
  success_criteria: unknown[]
  status: string
  created_at: string
  updated_at: string
}

export interface AgentVersion {
  id: string
  agent_id: string
  version: number
  config: Record<string, unknown>
  snapshot: Record<string, unknown>
  status: string
  published_by: string | null
  published_at: string | null
  created_at: string
}

export interface AgentValidation {
  valid: boolean
  errors: string[]
  warnings: string[]
}

export interface ScoreCards {
  [key: string]: number | string | null
}

export interface ToolInfo {
  id: string
  name: string
  display_name: string
  description: string
  version: string
  current_version: number
  source_type: 'native' | 'http' | string
  input_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  risk_level: string
  timeout_seconds: number
  permission_scope: string[]
  requires_approval: boolean
  enabled: boolean
  config: Record<string, unknown>
  sensitive_fields: string[]
  created_by: string | null
  created_at: string
  updated_at: string
}

export interface ToolTestResult {
  ok: boolean
  status: string
  result: unknown
  error: string | null
  duration_ms: number
  masked_arguments: Record<string, unknown>
}

export interface ConversationInfo {
  id: string
  agent_id: string
  user_id: string
  root_id: string | null
  title: string
  created_at: string
  updated_at: string
}

export interface MessageInfo {
  id: string
  conversation_id: string
  role: string
  content: string
  tool_calls: unknown[] | null
  execution_id: string | null
  created_at: string
}

export interface ExecutionInfo {
  id: string
  task_id: string | null
  agent_id: string
  agent_version_id: string
  agent_version: number
  workflow_id: string | null
  workflow_version_id: string | null
  user_id: string
  conversation_id: string | null
  mode: string
  status: string
  input_text: string
  output_text: string | null
  error: string | null
  trace_id: string | null
  token_usage: Record<string, number>
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface ExecutionEventInfo {
  seq: number
  execution_id: string
  type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface LLMCallInfo {
  id: string
  execution_id: string
  model_config_id: string | null
  provider: string
  model: string
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  latency_ms: number
  status: string
  error: string | null
  created_at: string
}

export interface ApprovalInfo {
  id: string
  execution_id: string
  kind: string
  tool_call_id: string | null
  tool_name: string
  arguments: Record<string, unknown>
  status: string
  requested_at: string
  resolved_at: string | null
  resolved_by: string | null
  comment: string | null
}

export interface TaskStepInfo {
  id: string
  step_key: string
  position: number
  title: string
  status: string
  result: string | null
  verified: boolean | null
}

export interface ExecutionDetail extends ExecutionInfo {
  steps: TaskStepInfo[]
  input_payload: Record<string, unknown>
  output_payload: Record<string, unknown> | null
  events_count: number
  approvals: ApprovalInfo[]
  llm_calls: LLMCallInfo[]
}

export interface ArtifactInfo {
  id: string
  filename: string
  content_type: string
  size: number
  storage_key: string
  owner_id: string
  conversation_id: string | null
  created_at: string
}

export interface AuditRuleInfo {
  rule: string
  name: string
  description: string
  severity: string
  default_options: Record<string, unknown>
}

export interface AuditRulesResponse {
  total: number
  rules: AuditRuleInfo[]
  sample_file_available: boolean
  sample_labels_available: boolean
}

export interface ToolCallRecord {
  id: string
  tool_id: string | null
  tool_name: string
  execution_id: string | null
  user_id: string
  agent_id: string | null
  arguments_masked: Record<string, unknown>
  result: unknown
  status: string
  error: string | null
  duration_ms: number
  created_at: string
}

export interface AuditReportResponse {
  format: string
  markdown: string
  truncated: boolean
  summary: Record<string, unknown>
}

export interface KnowledgeBaseInfo {
  id: string
  name: string
  description: string
  owner_id: string
  tenant_id: string
  department_id: string | null
  access_scope: 'public' | 'department' | 'private' | string
  status: string
  embedding_model: string
  chunk_size: number
  chunk_overlap: number
  document_count?: number
  created_at: string
  updated_at: string
}

export interface KnowledgeSearchResponse {
  query: string
  knowledge_base_ids: string[]
  results: RetrievalResult[]
}

export interface KnowledgeDocumentInfo {
  id: string
  knowledge_base_id: string
  owner_id: string
  department_id: string | null
  tenant_id: string
  filename: string
  content_type: string
  size: number
  status: string
  version: number
  error: string | null
  metadata: Record<string, unknown>
  access_scope: string
  chunk_count?: number
  created_at: string
  updated_at: string
}

export interface KnowledgeDocumentChunk {
  id: string
  document_id: string
  version: number
  chunk_index: number
  content: string
  char_count: number
  metadata: Record<string, unknown>
}

export interface RetrievalResult {
  chunk_id: string
  document_id: string
  document_title: string
  knowledge_base_id: string
  content: string
  score: number
  metadata: Record<string, unknown>
}

export interface SkillInfo {
  id: string
  name: string
  description: string
  version: string
  current_version: number
  enabled: boolean
  source_path: string | null
  required_tools: string[]
  required_permissions: string[]
  inputs: string[]
  outputs: string[]
  metadata: Record<string, unknown>
  instructions?: string
  dependencies?: Array<{ id: string; kind: string; ref: string }>
  created_at: string
  updated_at: string
}

export interface SkillVersionInfo {
  id: string
  skill_id: string
  version: number
  content: string
  snapshot: Record<string, unknown>
  created_at: string
}

export interface AgentKnowledgeSettings {
  enabled: boolean
  top_k: number
}

export interface MCPServerInfo {
  id: string
  name: string
  transport: 'stdio' | 'http' | string
  command: string | null
  args: string[]
  env: Record<string, string>
  url: string | null
  headers: Record<string, string>
  enabled: boolean
  last_sync_at: string | null
  last_error: string | null
  created_by: string | null
  created_at: string
  updated_at: string
}

export interface MCPSyncResult {
  server_id: string
  server_name: string
  created: number
  updated: number
  unchanged: number
  disabled: number
  tools: ToolInfo[]
}

export type WorkflowNodeType =
  | 'start'
  | 'llm'
  | 'agent'
  | 'tool'
  | 'knowledge'
  | 'condition'
  | 'approval'
  | 'end'

export interface WorkflowNode {
  id: string
  type: WorkflowNodeType
  name: string
  config: Record<string, unknown>
  next: string | null
  on_true: string | null
  on_false: string | null
  retry?: { max_attempts: number; backoff_seconds: number } | null
}

export interface WorkflowDefinition {
  version: number
  start_node: string | null
  nodes: WorkflowNode[]
  output: string
}

export interface WorkflowInfo {
  id: string
  name: string
  description: string
  status: string
  owner_id: string
  current_version: number | null
  current_version_id: string | null
  definition: WorkflowDefinition
  published_at: string | null
  created_at: string
  updated_at: string
}

export interface WorkflowVersionInfo {
  id: string
  workflow_id: string
  version: number
  definition: Record<string, unknown>
  status: string
  created_by: string | null
  published_at: string | null
  created_at: string
}

export interface WorkflowValidation {
  valid: boolean
  errors: string[]
  warnings: string[]
}

export interface AuditLogInfo {
  id: string
  occurred_at: string
  actor_id: string | null
  actor_username: string
  action: string
  resource_type: string
  resource_id: string | null
  result: 'success' | 'failure' | 'denied' | string
  status_code: number | null
  ip_address: string | null
  request_id: string | null
  tenant_id: string
  details: Record<string, unknown>
  created_at: string
}

export interface AccessPolicyInfo {
  id: string
  name: string
  description: string
  effect: 'allow' | 'deny' | string
  resource_type: string
  action: string
  subject_roles: string[]
  subject_departments: string[]
  tenant_ids: string[]
  resource_ids: string[]
  priority: number
  enabled: boolean
  created_at: string
}
