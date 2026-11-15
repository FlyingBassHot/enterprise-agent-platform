<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, View } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import client from '@/api/client'
import { agentsApi, knowledgeApi, modelsApi, promptsApi, skillsApi, usersApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type {
  AgentConfig,
  AgentInfo,
  AgentValidation,
  KnowledgeBaseInfo,
  ModelConfig,
  PermissionInfo,
  PromptInfo,
  PromptVersion,
  ResolvedPrompt,
  RoleInfo,
  SkillInfo,
} from '@/types/api'
import { formatDate, shortId } from '@/utils/format'

interface ToolOption {
  id: string
  name: string
  display_name: string
  description: string
  enabled: boolean
  risk_level: string
  source_type: string
  requires_approval: boolean
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const agentId = ref(String(route.params.id || ''))
const agent = ref<AgentInfo | null>(null)
const name = ref('')
const description = ref('')
const config = ref<AgentConfig>(emptyConfig())

const loading = ref(false)
const loadError = ref('')
const saving = ref(false)
const validating = ref(false)
const publishing = ref(false)

const models = ref<ModelConfig[]>([])
const modelsLoading = ref(false)
const prompts = ref<PromptInfo[]>([])
const promptsLoading = ref(false)
const promptVersions = ref<PromptVersion[]>([])
const promptVersionsLoading = ref(false)
const promptVersionsPromptId = ref('')
const tools = ref<ToolOption[]>([])
const toolsLoading = ref(false)
const knowledgeBases = ref<KnowledgeBaseInfo[]>([])
const knowledgeLoading = ref(false)
const skills = ref<SkillInfo[]>([])
const skillsLoading = ref(false)
const permissions = ref<PermissionInfo[]>([])
const roles = ref<RoleInfo[]>([])

const validation = ref<AgentValidation | null>(null)
const validationVisible = ref(false)
const previewVisible = ref(false)
const preview = ref<ResolvedPrompt | null>(null)
const previewError = ref('')
const previewLoading = ref(false)

const riskLevels = [
  { value: 'low', label: '低' },
  { value: 'medium', label: '中' },
  { value: 'high', label: '高' },
  { value: 'critical', label: '极高' },
]

const canWrite = computed(() => auth.has('agents:write'))
const canPublish = computed(() => auth.has('agents:publish'))
const canManageRoles = computed(() => auth.has('roles:manage'))
const isEdit = computed(() => Boolean(agentId.value))

const selectedPrompt = computed(
  () => prompts.value.find((item) => item.id === config.value.prompt.prompt_id) || null,
)

const latestPromptVersion = computed(() => {
  return [...promptVersions.value].sort((a, b) => b.version - a.version)[0] || null
})

const selectedPromptVersion = computed(() => {
  const versionId = config.value.prompt.version_id
  if (versionId) {
    return promptVersions.value.find((item) => item.id === versionId) || null
  }
  return latestPromptVersion.value
})

const promptVariables = computed(() => selectedPromptVersion.value?.variables || [])

const selectableTools = computed(() => {
  const selected = new Set(config.value.tools)
  return tools.value.filter((tool) => tool.enabled || selected.has(tool.name))
})

const selectedToolList = computed(() =>
  config.value.tools
    .map((toolName) => tools.value.find((tool) => tool.name === toolName))
    .filter((tool): tool is ToolOption => Boolean(tool)),
)

const selectableKnowledgeBases = computed(() => {
  const selected = new Set(config.value.knowledge_base_ids)
  return knowledgeBases.value.filter(
    (base) => base.status === 'active' || selected.has(base.id),
  )
})

const selectableSkills = computed(() => {
  const selected = new Set(config.value.skill_names)
  return skills.value.filter((skill) => skill.enabled || selected.has(skill.name))
})

const selectedSkillList = computed(() =>
  config.value.skill_names
    .map((skillName) => skills.value.find((skill) => skill.name === skillName))
    .filter((skill): skill is SkillInfo => Boolean(skill)),
)

function scopeLabel(scope: string): string {
  const map: Record<string, string> = {
    public: '公开',
    department: '部门',
    private: '私有',
  }
  return map[scope] || scope
}

function emptyConfig(): AgentConfig {
  return {
    model: { config_id: '', temperature: 0.2, max_tokens: 2048, top_p: 1 },
    prompt: { prompt_id: '', version_id: null, variables: {} },
    tools: [],
    knowledge_base_ids: [],
    skill_names: [],
    retrieval: { enabled: true, top_k: 5 },
    execution: {
      mode: 'agent',
      max_iterations: 12,
      timeout_seconds: 600,
      approval_risk_levels: ['high', 'critical'],
      streaming: true,
    },
    permissions: { required_permissions: [], allowed_roles: [] },
    metadata: {},
  }
}

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

async function loadOptions() {
  modelsLoading.value = true
  promptsLoading.value = true
  toolsLoading.value = true
  knowledgeLoading.value = true
  skillsLoading.value = true

  const [modelResult, promptResult, toolResult, knowledgeResult, skillResult] =
    await Promise.allSettled([
      modelsApi.configs(),
      promptsApi.list(),
      client.get<ToolOption[]>('/tools').then((response) => response.data),
      knowledgeApi.listBases(),
      skillsApi.list(),
    ])

  if (modelResult.status === 'fulfilled') {
    models.value = modelResult.value
    if (!config.value.model.config_id) {
      const preferred =
        modelResult.value.find((item) => item.is_default && item.enabled) ||
        modelResult.value.find((item) => item.enabled)
      if (preferred) config.value.model.config_id = preferred.id
    }
  }
  modelsLoading.value = false

  if (promptResult.status === 'fulfilled') prompts.value = promptResult.value
  promptsLoading.value = false

  if (toolResult.status === 'fulfilled') tools.value = toolResult.value
  toolsLoading.value = false

  if (knowledgeResult.status === 'fulfilled') knowledgeBases.value = knowledgeResult.value
  knowledgeLoading.value = false

  if (skillResult.status === 'fulfilled') skills.value = skillResult.value
  skillsLoading.value = false

  if (canManageRoles.value) {
    const [permissionResult, roleResult] = await Promise.allSettled([
      usersApi.permissions(),
      usersApi.roles(),
    ])
    if (permissionResult.status === 'fulfilled') permissions.value = permissionResult.value
    if (roleResult.status === 'fulfilled') roles.value = roleResult.value
  }
}

async function loadPromptVersions(promptId: string) {
  promptVersionsPromptId.value = ''
  if (!promptId) {
    promptVersions.value = []
    return
  }
  promptVersionsLoading.value = true
  try {
    promptVersions.value = await promptsApi.versions(promptId)
    promptVersionsPromptId.value = promptId
  } catch {
    promptVersions.value = []
  } finally {
    promptVersionsLoading.value = false
  }
}

function syncPromptVariables() {
  if (promptVersionsPromptId.value !== config.value.prompt.prompt_id) return
  const declared = new Set(promptVariables.value.map((variable) => variable.name))
  const next: Record<string, string> = {}
  for (const [key, value] of Object.entries(config.value.prompt.variables)) {
    if (declared.has(key)) next[key] = value
  }
  for (const variable of promptVariables.value) {
    if (next[variable.name] === undefined) {
      next[variable.name] = variable.default || ''
    }
  }
  config.value.prompt.variables = next
}

async function onPromptChange(promptId: string) {
  config.value.prompt.version_id = null
  config.value.prompt.variables = {}
  promptVersions.value = []
  if (promptId) {
    await loadPromptVersions(promptId)
  }
  syncPromptVariables()
}

function onVersionChange() {
  syncPromptVariables()
}

async function loadAgent(id: string) {
  loading.value = true
  loadError.value = ''
  try {
    const data = await agentsApi.get(id)
    agent.value = data
    name.value = data.name
    description.value = data.description
    config.value = structuredClone(data.draft_config)
    await loadPromptVersions(data.draft_config.prompt.prompt_id)
    syncPromptVariables()
  } catch (err) {
    loadError.value = describeError(err)
  } finally {
    loading.value = false
  }
}

function validateForm(): boolean {
  if (name.value.trim().length < 2) {
    ElMessage.warning('名称至少需要 2 个字符')
    return false
  }
  if (!config.value.model.config_id) {
    ElMessage.warning('请选择模型配置')
    return false
  }
  if (!config.value.prompt.prompt_id) {
    ElMessage.warning('请选择 Prompt')
    return false
  }
  return true
}

async function saveDraft(silent = false): Promise<boolean> {
  if (!canWrite.value) {
    ElMessage.warning('当前账号没有 Agent 编辑权限')
    return false
  }
  if (!validateForm()) return false
  saving.value = true
  try {
    const payload = {
      name: name.value.trim(),
      description: description.value,
      config: config.value,
    }
    const data = agentId.value
      ? await agentsApi.update(agentId.value, payload)
      : await agentsApi.create(payload)
    agent.value = data
    if (!agentId.value) {
      agentId.value = data.id
      await router.replace(`/agents/${data.id}/edit`)
    }
    config.value = structuredClone(data.draft_config)
    if (!silent) ElMessage.success('草稿已保存')
    return true
  } catch {
    return false
  } finally {
    saving.value = false
  }
}

async function handleValidate() {
  const saved = await saveDraft(true)
  if (!saved || !agentId.value) return
  validating.value = true
  try {
    validation.value = await agentsApi.validate(agentId.value)
    validationVisible.value = true
  } catch {
    // interceptor surfaces the backend message
  } finally {
    validating.value = false
  }
}

async function handlePublish() {
  if (!canPublish.value) {
    ElMessage.warning('当前账号没有发布权限')
    return
  }
  const saved = await saveDraft(true)
  if (!saved || !agentId.value) return
  publishing.value = true
  try {
    let result: AgentValidation
    try {
      result = await agentsApi.validate(agentId.value)
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
    try {
      const data = await agentsApi.publish(agentId.value)
      agent.value = data
      ElMessage.success(`发布成功${data.current_version ? ` · v${data.current_version}` : ''}`)
    } catch {
      // interceptor surfaces the backend message
    }
  } finally {
    publishing.value = false
  }
}

async function openPreview() {
  if (!config.value.prompt.prompt_id) {
    ElMessage.warning('请先选择 Prompt')
    return
  }
  previewVisible.value = true
  preview.value = null
  previewError.value = ''
  previewLoading.value = true
  try {
    preview.value = await promptsApi.resolve(config.value.prompt.prompt_id, {
      version_id: config.value.prompt.version_id,
      variables: config.value.prompt.variables,
    })
  } catch (err) {
    previewError.value = describeError(err)
  } finally {
    previewLoading.value = false
  }
}

function goBack() {
  void router.push('/agents')
}

onMounted(async () => {
  await loadOptions()
  if (agentId.value) {
    await loadAgent(agentId.value)
    syncPromptVariables()
  }
})
</script>

<template>
  <div class="page-stack">
    <PageHeader
      :title="isEdit ? '编辑 Agent' : '新建 Agent'"
      subtitle="配置模型、Prompt、工具、知识与执行策略；发布后将生成不可变版本快照"
    >
      <template #actions>
        <el-button :icon="ArrowLeft" @click="goBack">返回</el-button>
        <el-button :icon="View" :disabled="!config.prompt.prompt_id" @click="openPreview">
          Prompt 预览
        </el-button>
        <el-button :loading="validating" @click="handleValidate">校验</el-button>
        <el-button
          v-if="canPublish"
          type="success"
          plain
          :loading="publishing"
          @click="handlePublish"
        >
          发布
        </el-button>
        <el-button v-if="canWrite" type="primary" :loading="saving" @click="saveDraft()">
          保存草稿
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="loadError" type="error" :closable="false" show-icon :title="loadError">
      <el-button v-if="agentId" link type="primary" @click="loadAgent(agentId)">重试</el-button>
    </el-alert>

    <el-skeleton v-if="loading" :rows="10" animated />

    <el-form v-else-if="!loadError" label-position="top" class="agent-form" @submit.prevent>
      <div class="card">
        <div class="section-head">
          <h3>基本信息</h3>
          <span v-if="agent" class="muted mono">{{ shortId(agent.id, 12) }}</span>
        </div>
        <div class="grid-2">
          <el-form-item label="名称" required>
            <el-input
              v-model="name"
              maxlength="120"
              show-word-limit
              placeholder="如：审计报告助手"
            />
          </el-form-item>
          <el-form-item label="状态">
            <div class="status-row">
              <StatusTag :status="agent?.status || 'draft'" />
              <span v-if="agent?.current_version" class="muted">
                当前版本 v{{ agent.current_version }}
              </span>
              <span v-else class="muted">尚未发布</span>
              <span v-if="agent" class="muted">更新于 {{ formatDate(agent.updated_at) }}</span>
            </div>
          </el-form-item>
        </div>
        <el-form-item label="描述">
          <el-input
            v-model="description"
            type="textarea"
            :rows="2"
            maxlength="500"
            show-word-limit
            placeholder="描述 Agent 的适用场景与能力边界"
          />
        </el-form-item>
      </div>

      <div class="card">
        <div class="section-head">
          <h3>模型</h3>
          <span class="muted">选择已启用的模型配置，并覆盖采样参数</span>
        </div>
        <div class="grid-3">
          <el-form-item label="模型配置" required class="span-2">
            <el-select
              v-model="config.model.config_id"
              filterable
              :loading="modelsLoading"
              placeholder="选择模型配置"
              class="full"
            >
              <el-option
                v-for="model in models"
                :key="model.id"
                :label="`${model.name} · ${model.model_name}`"
                :value="model.id"
              >
                <div class="option-row">
                  <span>{{ model.name }}</span>
                  <span class="muted">{{ model.provider_name }} · {{ model.model_name }}</span>
                  <el-tag v-if="model.is_default" size="small" type="success" effect="plain">
                    默认
                  </el-tag>
                  <el-tag v-if="!model.enabled" size="small" type="info" effect="plain">
                    已停用
                  </el-tag>
                </div>
              </el-option>
            </el-select>
            <div v-if="!modelsLoading && !models.length" class="inline-hint muted">
              暂无模型配置，请先在「模型管理」中创建。
            </div>
          </el-form-item>
          <el-form-item label="Temperature">
            <el-input-number
              v-model="config.model.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              :precision="2"
              class="full"
            />
          </el-form-item>
          <el-form-item label="Max Tokens">
            <el-input-number
              v-model="config.model.max_tokens"
              :min="1"
              :max="131072"
              :step="256"
              class="full"
            />
          </el-form-item>
          <el-form-item label="Top P">
            <el-input-number
              v-model="config.model.top_p"
              :min="0.01"
              :max="1"
              :step="0.05"
              :precision="2"
              class="full"
            />
          </el-form-item>
        </div>
      </div>

      <div class="card">
        <div class="section-head">
          <h3>Prompt</h3>
          <span class="muted">绑定模板版本；留空版本表示始终使用最新发布版本</span>
        </div>
        <div class="grid-2">
          <el-form-item label="Prompt 模板" required>
            <el-select
              v-model="config.prompt.prompt_id"
              filterable
              :loading="promptsLoading"
              placeholder="选择 Prompt 模板"
              class="full"
              @change="onPromptChange"
            >
              <el-option
                v-for="prompt in prompts"
                :key="prompt.id"
                :label="`${prompt.name} · v${prompt.current_version}`"
                :value="prompt.id"
              >
                <div class="option-row">
                  <span>{{ prompt.name }}</span>
                  <span class="muted">当前 v{{ prompt.current_version }}</span>
                  <StatusTag :status="prompt.status" />
                </div>
              </el-option>
            </el-select>
            <div v-if="!promptsLoading && !prompts.length" class="inline-hint muted">
              暂无 Prompt，请先在「Prompt 管理」中创建。
            </div>
          </el-form-item>
          <el-form-item label="版本">
            <el-select
              v-model="config.prompt.version_id"
              clearable
              :loading="promptVersionsLoading"
              :disabled="!config.prompt.prompt_id"
              placeholder="最新版本"
              class="full"
              @change="onVersionChange"
            >
              <el-option
                v-for="version in promptVersions"
                :key="version.id"
                :label="`v${version.version} · ${formatDate(version.created_at)}`"
                :value="version.id"
              />
            </el-select>
            <div v-if="selectedPrompt" class="inline-hint muted">
              已选择：{{ selectedPrompt.name }}
              · {{ selectedPromptVersion ? `v${selectedPromptVersion.version}` : '最新版本' }}
            </div>
          </el-form-item>
        </div>

        <div v-if="config.prompt.prompt_id" class="variables-block">
          <div class="sub-label">
            变量
            <span class="muted">按所选版本定义填写，支持默认值</span>
          </div>
          <div v-if="promptVariables.length" class="grid-3">
            <el-form-item
              v-for="variable in promptVariables"
              :key="variable.name"
              :label="variable.name"
              :required="variable.required"
            >
              <el-input
                v-model="config.prompt.variables[variable.name]"
                :placeholder="variable.default || '请输入变量值'"
              />
              <div class="inline-hint muted">
                {{ variable.required ? '必填' : '可选' }}
                <template v-if="variable.default"> · 默认 {{ variable.default }}</template>
              </div>
            </el-form-item>
          </div>
          <el-empty v-else description="所选版本未定义变量" :image-size="44" />
        </div>
        <el-empty
          v-else
          description="选择 Prompt 后可配置变量"
          :image-size="50"
        />
      </div>

      <div class="card">
        <div class="section-head">
          <h3>工具</h3>
          <span class="muted">仅可调用已启用且当前用户有权访问的工具</span>
        </div>
        <el-select
          v-model="config.tools"
          multiple
          filterable
          :loading="toolsLoading"
          placeholder="选择允许调用的工具"
          class="full"
        >
          <el-option
            v-for="tool in selectableTools"
            :key="tool.id"
            :label="tool.display_name || tool.name"
            :value="tool.name"
          >
            <div class="option-row">
              <span>{{ tool.display_name || tool.name }}</span>
              <span class="mono muted">{{ tool.name }}</span>
              <StatusTag :status="tool.risk_level" />
              <el-tag v-if="tool.requires_approval" size="small" type="warning" effect="plain">
                需审批
              </el-tag>
              <el-tag v-if="!tool.enabled" size="small" type="info" effect="plain">已停用</el-tag>
            </div>
          </el-option>
        </el-select>
        <div v-if="selectedToolList.length" class="tool-chips">
          <div v-for="tool in selectedToolList" :key="tool.name" class="tool-chip">
            <span class="tool-name">{{ tool.display_name || tool.name }}</span>
            <span class="mono muted">{{ tool.name }}</span>
            <StatusTag :status="tool.risk_level" />
            <el-tag v-if="tool.requires_approval" size="small" type="warning" effect="plain">
              审批
            </el-tag>
            <el-tag v-if="!tool.enabled" size="small" type="info" effect="plain">已停用</el-tag>
          </div>
        </div>
        <div v-if="!toolsLoading && !tools.length" class="inline-hint muted">
          暂无可用工具。
        </div>
      </div>

      <div class="card">
        <div class="section-head">
          <h3>知识与技能</h3>
          <span class="muted">绑定知识库与 Skill；发布时由服务端校验可用性与权限</span>
        </div>
        <el-form-item label="知识库">
          <el-select
            v-model="config.knowledge_base_ids"
            multiple
            filterable
            :loading="knowledgeLoading"
            placeholder="选择知识库（可多选）"
            class="full"
          >
            <el-option
              v-for="base in selectableKnowledgeBases"
              :key="base.id"
              :label="base.name"
              :value="base.id"
            >
              <div class="option-row">
                <span>{{ base.name }}</span>
                <span class="muted">{{ base.document_count ?? 0 }} 文档</span>
                <el-tag size="small" effect="plain">{{ scopeLabel(base.access_scope) }}</el-tag>
                <el-tag v-if="base.status !== 'active'" size="small" type="info" effect="plain">
                  已停用
                </el-tag>
              </div>
            </el-option>
          </el-select>
          <div v-if="!knowledgeLoading && !knowledgeBases.length" class="inline-hint muted">
            暂无可用知识库，可先在「知识库」中创建并上传文档。
          </div>
        </el-form-item>

        <div class="grid-2">
          <el-form-item label="知识检索">
            <el-switch
              v-model="config.retrieval.enabled"
              active-text="启用"
              inactive-text="关闭"
            />
            <div class="inline-hint muted">关闭后不会向模型注入知识库检索结果。</div>
          </el-form-item>
          <el-form-item label="检索 Top K">
            <el-input-number
              v-model="config.retrieval.top_k"
              :min="1"
              :max="20"
              :disabled="!config.retrieval.enabled"
              class="full"
            />
          </el-form-item>
        </div>

        <el-form-item label="Skill">
          <el-select
            v-model="config.skill_names"
            multiple
            filterable
            :loading="skillsLoading"
            placeholder="选择 Skill（可多选）"
            class="full"
          >
            <el-option
              v-for="skill in selectableSkills"
              :key="skill.id"
              :label="`${skill.name} · v${skill.current_version}`"
              :value="skill.name"
            >
              <div class="option-row">
                <span>{{ skill.name }}</span>
                <span class="muted">v{{ skill.current_version }}</span>
                <el-tag v-if="skill.required_tools.length" size="small" effect="plain">
                  {{ skill.required_tools.length }} 工具
                </el-tag>
                <el-tag
                  v-if="skill.required_permissions.length"
                  size="small"
                  type="warning"
                  effect="plain"
                >
                  {{ skill.required_permissions.length }} 权限
                </el-tag>
                <el-tag v-if="!skill.enabled" size="small" type="info" effect="plain">
                  已停用
                </el-tag>
              </div>
            </el-option>
          </el-select>
          <div v-if="!skillsLoading && !skills.length" class="inline-hint muted">
            暂无 Skill，可先在「Skill 管理」中同步。
          </div>
        </el-form-item>

        <div v-if="selectedSkillList.length" class="skill-chips">
          <div v-for="skill in selectedSkillList" :key="skill.name" class="skill-chip">
            <div class="skill-chip-head">
              <span class="tool-name">{{ skill.name }}</span>
              <span class="muted mono">v{{ skill.current_version }}</span>
              <el-tag v-if="!skill.enabled" size="small" type="info" effect="plain">
                已停用
              </el-tag>
            </div>
            <div v-if="skill.required_tools.length" class="skill-req muted">
              依赖工具：{{ skill.required_tools.join('、') }}
            </div>
            <div v-if="skill.required_permissions.length" class="skill-req muted">
              所需权限：{{ skill.required_permissions.join('、') }}
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="section-head">
          <h3>执行策略</h3>
          <span class="muted">Agent 模式固定为 agent，服务端强制执行迭代与超时上限</span>
        </div>
        <div class="grid-3">
          <el-form-item label="执行模式">
            <el-input model-value="agent" disabled>
              <template #append>固定</template>
            </el-input>
          </el-form-item>
          <el-form-item label="最大迭代次数">
            <el-input-number
              v-model="config.execution.max_iterations"
              :min="1"
              :max="200"
              class="full"
            />
          </el-form-item>
          <el-form-item label="超时时间（秒）">
            <el-input-number
              v-model="config.execution.timeout_seconds"
              :min="1"
              :max="86400"
              :step="30"
              class="full"
            />
          </el-form-item>
        </div>
        <el-form-item label="需人工审批的风险等级">
          <el-checkbox-group v-model="config.execution.approval_risk_levels">
            <el-checkbox v-for="level in riskLevels" :key="level.value" :value="level.value">
              {{ level.label }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item label="流式输出">
          <el-switch
            v-model="config.execution.streaming"
            active-text="开启"
            inactive-text="关闭"
          />
        </el-form-item>
      </div>

      <div class="card">
        <div class="section-head">
          <h3>权限</h3>
          <span class="muted">限制可调用该 Agent 的权限与角色；服务端最终裁决</span>
        </div>
        <div class="grid-2">
          <el-form-item label="所需权限 required_permissions">
            <el-select
              v-if="permissions.length"
              v-model="config.permissions.required_permissions"
              multiple
              filterable
              placeholder="选择权限"
              class="full"
            >
              <el-option
                v-for="permission in permissions"
                :key="permission.code"
                :label="permission.name"
                :value="permission.code"
              >
                <div class="option-row">
                  <span>{{ permission.name }}</span>
                  <span class="mono muted">{{ permission.code }}</span>
                </div>
              </el-option>
            </el-select>
            <div v-else class="readonly-tags">
              <el-tag
                v-for="code in config.permissions.required_permissions"
                :key="code"
                size="small"
                class="perm-tag"
              >
                {{ code }}
              </el-tag>
              <span v-if="!config.permissions.required_permissions.length" class="muted">未配置</span>
              <div class="inline-hint muted">
                当前账号无权读取权限清单，已保留现有配置且不可编辑。
              </div>
            </div>
          </el-form-item>
          <el-form-item label="允许角色 allowed_roles">
            <el-select
              v-if="roles.length"
              v-model="config.permissions.allowed_roles"
              multiple
              filterable
              placeholder="选择角色（留空表示不限制）"
              class="full"
            >
              <el-option
                v-for="role in roles"
                :key="role.id"
                :label="role.name"
                :value="role.name"
              />
            </el-select>
            <div v-else class="readonly-tags">
              <el-tag
                v-for="roleName in config.permissions.allowed_roles"
                :key="roleName"
                size="small"
                class="perm-tag"
              >
                {{ roleName }}
              </el-tag>
              <span v-if="!config.permissions.allowed_roles.length" class="muted">未配置</span>
              <div class="inline-hint muted">
                {{ canManageRoles ? '角色列表加载失败或为空。' : '当前账号无权读取角色列表，已保留现有配置。' }}
              </div>
            </div>
          </el-form-item>
        </div>
      </div>
    </el-form>

    <el-dialog v-model="validationVisible" title="校验结果" width="560px">
      <template v-if="validation">
        <div class="validation-head">
          <StatusTag :status="validation.valid ? 'success' : 'failed'" />
          <span class="muted">
            {{ validation.valid ? '配置校验通过' : '配置存在错误，请修复后重试' }}
          </span>
        </div>
        <el-alert
          v-if="validation.errors.length"
          type="error"
          :closable="false"
          title="错误"
          class="validation-alert"
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
          class="validation-alert"
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

    <el-dialog v-model="previewVisible" title="Prompt 渲染预览" width="720px">
      <el-skeleton v-if="previewLoading" :rows="8" animated />
      <el-alert
        v-else-if="previewError"
        type="error"
        :closable="false"
        show-icon
        :title="previewError"
      />
      <template v-else-if="preview">
        <div class="preview-meta muted">
          版本 v{{ preview.version }} · {{ preview.version_id }}
        </div>
        <div class="preview-section">
          <div class="sub-label">System Prompt</div>
          <pre>{{ preview.system_prompt || '（空）' }}</pre>
        </div>
        <div class="preview-section">
          <div class="sub-label">Task Prompt</div>
          <pre>{{ preview.task_prompt || '（空）' }}</pre>
        </div>
        <div class="preview-section">
          <div class="sub-label">Tool Instructions</div>
          <pre>{{ preview.tool_instructions || '（空）' }}</pre>
        </div>
      </template>
      <el-empty v-else description="暂无预览结果" :image-size="60" />
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.agent-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 18px 20px;
}

.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.section-head h3 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 650;
}

.section-head .muted {
  font-size: 11.5px;
}

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 0 18px;
}

.grid-3 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 0 18px;
}

.span-2 {
  grid-column: span 2;
}

@media (max-width: 900px) {
  .span-2 {
    grid-column: span 1;
  }
}

.full {
  width: 100%;
}

.status-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  min-height: 32px;
}

.option-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  overflow: hidden;
}

.option-row > span:first-child {
  flex-shrink: 0;
}

.option-row .mono,
.option-row .muted {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.inline-hint {
  margin-top: 4px;
  font-size: 11.5px;
  line-height: 1.5;
}

.variables-block {
  padding-top: 4px;
  border-top: 1px dashed var(--app-border);
  margin-top: 4px;
}

.sub-label {
  font-size: 12.5px;
  font-weight: 600;
  margin: 10px 0 8px;
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.sub-label .muted {
  font-weight: 400;
  font-size: 11px;
}

.tool-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.tool-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 9px;
  background: #f8fafc;
  border: 1px solid var(--app-border);
  font-size: 12px;
}

.tool-name {
  font-weight: 560;
}

.skill-chips {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 12px;
}

.skill-chip {
  padding: 8px 12px;
  border-radius: 9px;
  background: #f8fafc;
  border: 1px solid var(--app-border);
  font-size: 12px;
}

.skill-chip-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.skill-req {
  margin-top: 3px;
  font-size: 11px;
  line-height: 1.5;
  word-break: break-word;
}

.readonly-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  width: 100%;
  min-height: 32px;
}

.perm-tag {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
}

.validation-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.validation-alert {
  margin-bottom: 10px;
}

.check-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
}

.preview-meta {
  font-size: 11.5px;
  margin-bottom: 10px;
}

.preview-section {
  margin-bottom: 12px;
}

.preview-section pre {
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
</style>
