<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import PageHeader from '@/components/PageHeader.vue'
import StatusTag from '@/components/StatusTag.vue'
import { modelsApi } from '@/api/platform'
import { useAuthStore } from '@/stores/auth'
import type { ModelConfig, ModelProvider } from '@/types/api'
import { formatDate } from '@/utils/format'

const auth = useAuthStore()

const activeTab = ref<'providers' | 'configs'>('providers')
const providers = ref<ModelProvider[]>([])
const configs = ref<ModelConfig[]>([])
const loading = ref(false)
const error = ref('')
const healthLoadingId = ref('')

const canWrite = computed(() => auth.has('models:write'))
const canManageProviders = computed(() => auth.user?.is_superuser === true || auth.has('*'))

const providerVisible = ref(false)
const editingProvider = ref<ModelProvider | null>(null)
const savingProvider = ref(false)
const providerForm = ref({
  name: '',
  provider_type: 'mock',
  base_url: '',
  api_key: '',
  description: '',
  enabled: true,
})

const configVisible = ref(false)
const editingConfig = ref<ModelConfig | null>(null)
const savingConfig = ref(false)
const configForm = ref({
  name: '',
  display_name: '',
  provider_id: '',
  model_name: '',
  temperature: 0.2,
  max_tokens: 2048,
  top_p: 1,
  supports_tools: true,
  supports_streaming: true,
  supports_json: true,
  enabled: true,
  is_default: false,
  description: '',
  extraText: '{}',
})

function describeError(value: unknown): string {
  if (typeof value === 'object' && value !== null && 'response' in value) {
    const response = (value as { response?: { data?: { message?: string } } }).response
    if (response?.data?.message) return response.data.message
  }
  return value instanceof Error ? value.message : '请求失败'
}

function providerTypeLabel(type: string): string {
  if (type === 'mock') return 'Mock'
  if (type === 'openai_compatible') return 'OpenAI 兼容'
  return type
}

function paramsLabel(config: ModelConfig): string {
  return `T ${config.params.temperature} · ${config.params.max_tokens} tokens · P ${config.params.top_p}`
}

async function load() {
  loading.value = true
  error.value = ''
  const [providerResult, configResult] = await Promise.allSettled([
    modelsApi.providers(),
    modelsApi.configs(),
  ])
  if (providerResult.status === 'fulfilled') {
    providers.value = providerResult.value
  } else {
    providers.value = []
    error.value = describeError(providerResult.reason)
  }
  if (configResult.status === 'fulfilled') {
    configs.value = configResult.value
  } else {
    configs.value = []
    if (!error.value) error.value = describeError(configResult.reason)
  }
  loading.value = false
}

function openProviderDialog(provider?: ModelProvider) {
  editingProvider.value = provider || null
  providerForm.value = provider
    ? {
        name: provider.name,
        provider_type: provider.provider_type,
        base_url: provider.base_url || '',
        api_key: '',
        description: provider.description,
        enabled: provider.enabled,
      }
    : {
        name: '',
        provider_type: 'mock',
        base_url: '',
        api_key: '',
        description: '',
        enabled: true,
      }
  providerVisible.value = true
}

async function saveProvider() {
  const name = providerForm.value.name.trim()
  if (name.length < 2) {
    ElMessage.warning('供应商名称至少需要 2 个字符')
    return
  }
  if (
    providerForm.value.provider_type === 'openai_compatible' &&
    providerForm.value.api_key.trim() &&
    !providerForm.value.base_url.trim()
  ) {
    ElMessage.warning('OpenAI 兼容供应商需要填写 Base URL')
    return
  }
  savingProvider.value = true
  try {
    const payload: Record<string, unknown> = {
      name,
      base_url: providerForm.value.base_url.trim() || null,
      description: providerForm.value.description,
      enabled: providerForm.value.enabled,
    }
    if (providerForm.value.api_key.trim()) {
      payload.api_key = providerForm.value.api_key.trim()
    }
    if (editingProvider.value) {
      await modelsApi.updateProvider(editingProvider.value.id, payload)
      ElMessage.success('供应商已更新')
    } else {
      payload.provider_type = providerForm.value.provider_type
      await modelsApi.createProvider(payload)
      ElMessage.success('供应商已创建')
    }
    providerVisible.value = false
    await load()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingProvider.value = false
  }
}

async function removeProvider(provider: ModelProvider) {
  try {
    await ElMessageBox.confirm(
      `确定删除供应商「${provider.name}」吗？存在关联模型配置时无法删除。`,
      '删除确认',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await modelsApi.removeProvider(provider.id)
    ElMessage.success('供应商已删除')
    await load()
  } catch {
    // interceptor surfaces the backend message
  }
}

function openConfigDialog(config?: ModelConfig) {
  editingConfig.value = config || null
  if (config) {
    configForm.value = {
      name: config.name,
      display_name: config.display_name,
      provider_id: config.provider_id,
      model_name: config.model_name,
      temperature: config.params.temperature,
      max_tokens: config.params.max_tokens,
      top_p: config.params.top_p,
      supports_tools: config.capabilities.supports_tools,
      supports_streaming: config.capabilities.supports_streaming,
      supports_json: config.capabilities.supports_json,
      enabled: config.enabled,
      is_default: config.is_default,
      description: config.description,
      extraText: JSON.stringify(config.extra || {}, null, 2),
    }
  } else {
    const preferred = providers.value.find((item) => item.enabled) || providers.value[0]
    configForm.value = {
      name: '',
      display_name: '',
      provider_id: preferred?.id || '',
      model_name: '',
      temperature: 0.2,
      max_tokens: 2048,
      top_p: 1,
      supports_tools: true,
      supports_streaming: true,
      supports_json: true,
      enabled: true,
      is_default: false,
      description: '',
      extraText: '{}',
    }
  }
  configVisible.value = true
}

function parseExtra(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text || '{}')
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      ElMessage.error('extra 必须是 JSON 对象')
      return null
    }
    return parsed as Record<string, unknown>
  } catch {
    ElMessage.error('extra 不是合法 JSON')
    return null
  }
}

async function saveConfig() {
  const name = configForm.value.name.trim()
  if (name.length < 2) {
    ElMessage.warning('配置名称至少需要 2 个字符')
    return
  }
  if (!configForm.value.provider_id) {
    ElMessage.warning('请选择模型供应商')
    return
  }
  if (!configForm.value.model_name.trim()) {
    ElMessage.warning('请填写模型名称')
    return
  }
  const extra = parseExtra(configForm.value.extraText)
  if (!extra) return
  const payload: Record<string, unknown> = {
    display_name: configForm.value.display_name,
    provider_id: configForm.value.provider_id,
    model_name: configForm.value.model_name.trim(),
    params: {
      temperature: configForm.value.temperature,
      max_tokens: configForm.value.max_tokens,
      top_p: configForm.value.top_p,
    },
    capabilities: {
      supports_tools: configForm.value.supports_tools,
      supports_streaming: configForm.value.supports_streaming,
      supports_json: configForm.value.supports_json,
    },
    enabled: configForm.value.enabled,
    is_default: configForm.value.is_default,
    description: configForm.value.description,
    extra,
  }
  savingConfig.value = true
  try {
    if (editingConfig.value) {
      await modelsApi.updateConfig(editingConfig.value.id, payload)
      ElMessage.success('模型配置已更新')
    } else {
      await modelsApi.createConfig({ ...payload, name })
      ElMessage.success('模型配置已创建')
    }
    configVisible.value = false
    await load()
  } catch {
    // interceptor surfaces the backend message
  } finally {
    savingConfig.value = false
  }
}

async function removeConfig(config: ModelConfig) {
  try {
    await ElMessageBox.confirm(`确定删除模型配置「${config.name}」吗？`, '删除确认', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await modelsApi.removeConfig(config.id)
    ElMessage.success('模型配置已删除')
    await load()
  } catch {
    // interceptor surfaces the backend message
  }
}

async function checkHealth(config: ModelConfig) {
  healthLoadingId.value = config.id
  try {
    const result = await modelsApi.health(config.id)
    await ElMessageBox.alert(
      `${result.message || '无返回信息'}${result.latency_ms ? ` · ${result.latency_ms} ms` : ''}`,
      result.ok ? '健康检查通过' : '健康检查失败',
      { type: result.ok ? 'success' : 'error', confirmButtonText: '关闭' },
    )
  } catch {
    // interceptor surfaces the backend message
  } finally {
    healthLoadingId.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="page-stack">
    <PageHeader title="模型管理" subtitle="供应商接入与模型配置，支持 Mock 与 OpenAI 兼容协议">
      <template #actions>
        <el-button :loading="loading" @click="load">
          <el-icon><Refresh /></el-icon>
          刷新
        </el-button>
        <el-button
          v-if="activeTab === 'providers' ? canManageProviders : canWrite"
          type="primary"
          :icon="Plus"
          @click="activeTab === 'providers' ? openProviderDialog() : openConfigDialog()"
        >
          {{ activeTab === 'providers' ? '新建供应商' : '新建模型配置' }}
        </el-button>
      </template>
    </PageHeader>

    <el-alert v-if="error" type="error" :closable="false" show-icon :title="error">
      <el-button link type="primary" @click="load">重试</el-button>
    </el-alert>

    <div class="card">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="供应商" name="providers">
          <el-table v-loading="loading" :data="providers" row-key="id" size="small">
            <el-table-column label="名称" min-width="150">
              <template #default="{ row }">
                <div class="name-cell">
                  <span class="name">{{ row.name }}</span>
                  <span class="muted mono">{{ row.id }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="128">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ providerTypeLabel(row.provider_type) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="Base URL" min-width="210">
              <template #default="{ row }">
                <span class="mono muted">{{ row.base_url || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="API Key" width="150">
              <template #default="{ row }">
                <el-tag v-if="row.has_api_key" size="small" type="success" effect="plain">
                  {{ row.masked_api_key || '已配置' }}
                </el-tag>
                <span v-else class="muted">未配置</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="168">
              <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="132" fixed="right">
              <template #default="{ row }">
                <el-button v-if="canManageProviders" link type="primary" @click="openProviderDialog(row)">
                  编辑
                </el-button>
                <el-button v-if="canManageProviders" link type="danger" @click="removeProvider(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无模型供应商" :image-size="80" />
            </template>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="模型配置" name="configs">
          <el-table v-loading="loading" :data="configs" row-key="id" size="small">
            <el-table-column label="名称" min-width="150">
              <template #default="{ row }">
                <div class="name-cell">
                  <span class="name">{{ row.name }}</span>
                  <span class="muted">{{ row.display_name || '-' }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="供应商" min-width="120">
              <template #default="{ row }">
                <div class="name-cell">
                  <span>{{ row.provider_name }}</span>
                  <span class="muted">{{ providerTypeLabel(row.provider_type) }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="模型" min-width="150">
              <template #default="{ row }">
                <span class="mono">{{ row.model_name }}</span>
              </template>
            </el-table-column>
            <el-table-column label="参数" min-width="190">
              <template #default="{ row }">
                <span class="muted mono">{{ paramsLabel(row) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="能力" min-width="170">
              <template #default="{ row }">
                <div class="cap-tags">
                  <el-tag v-if="row.capabilities.supports_tools" size="small" effect="plain">
                    工具
                  </el-tag>
                  <el-tag v-if="row.capabilities.supports_streaming" size="small" effect="plain">
                    流式
                  </el-tag>
                  <el-tag v-if="row.capabilities.supports_json" size="small" effect="plain">
                    JSON
                  </el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="140">
              <template #default="{ row }">
                <div class="cap-tags">
                  <StatusTag :status="row.enabled ? 'enabled' : 'disabled'" />
                  <el-tag v-if="row.is_default" size="small" type="success" effect="dark">
                    默认
                  </el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="206" fixed="right">
              <template #default="{ row }">
                <el-button
                  link
                  :loading="healthLoadingId === row.id"
                  @click="checkHealth(row)"
                >
                  健康检查
                </el-button>
                <el-button v-if="canWrite" link type="primary" @click="openConfigDialog(row)">
                  编辑
                </el-button>
                <el-button v-if="canWrite" link type="danger" @click="removeConfig(row)">
                  删除
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无模型配置" :image-size="80" />
            </template>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog
      v-model="providerVisible"
      :title="editingProvider ? '编辑供应商' : '新建供应商'"
      width="560px"
    >
      <el-form label-position="top" @submit.prevent="saveProvider">
        <el-form-item label="名称" required>
          <el-input v-model="providerForm.name" maxlength="120" show-word-limit placeholder="如：本地 Mock" />
        </el-form-item>
        <el-form-item label="类型" required>
          <el-select v-model="providerForm.provider_type" :disabled="Boolean(editingProvider)" class="full">
            <el-option label="Mock（本地模拟）" value="mock" />
            <el-option label="OpenAI 兼容" value="openai_compatible" />
          </el-select>
          <div v-if="editingProvider" class="inline-hint muted">供应商类型创建后不可修改。</div>
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="providerForm.base_url" placeholder="https://api.example.com/v1" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input
            v-model="providerForm.api_key"
            type="password"
            show-password
            :placeholder="editingProvider?.has_api_key ? '留空则保持现有密钥' : '可选，服务端加密存储'"
          />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="providerForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="providerForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="providerVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingProvider" @click="saveProvider">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="configVisible"
      :title="editingConfig ? '编辑模型配置' : '新建模型配置'"
      width="640px"
    >
      <el-form label-position="top" @submit.prevent="saveConfig">
        <div class="grid-2">
          <el-form-item label="配置名称" required>
            <el-input
              v-model="configForm.name"
              maxlength="120"
              show-word-limit
              :disabled="Boolean(editingConfig)"
              placeholder="唯一标识，如 audit-gpt"
            />
            <div v-if="editingConfig" class="inline-hint muted">配置名称不可修改。</div>
          </el-form-item>
          <el-form-item label="显示名称">
            <el-input v-model="configForm.display_name" placeholder="用于界面展示" />
          </el-form-item>
        </div>
        <div class="grid-2">
          <el-form-item label="供应商" required>
            <el-select v-model="configForm.provider_id" class="full" placeholder="选择供应商">
              <el-option
                v-for="provider in providers"
                :key="provider.id"
                :label="`${provider.name}（${providerTypeLabel(provider.provider_type)}）`"
                :value="provider.id"
              />
            </el-select>
            <div v-if="!providers.length" class="inline-hint muted">暂无供应商，请先创建供应商。</div>
          </el-form-item>
          <el-form-item label="模型名称" required>
            <el-input v-model="configForm.model_name" placeholder="如 gpt-4o-mini" />
          </el-form-item>
        </div>
        <div class="grid-3">
          <el-form-item label="Temperature">
            <el-input-number
              v-model="configForm.temperature"
              :min="0"
              :max="2"
              :step="0.1"
              :precision="2"
              class="full"
            />
          </el-form-item>
          <el-form-item label="Max Tokens">
            <el-input-number
              v-model="configForm.max_tokens"
              :min="1"
              :max="131072"
              :step="256"
              class="full"
            />
          </el-form-item>
          <el-form-item label="Top P">
            <el-input-number
              v-model="configForm.top_p"
              :min="0.01"
              :max="1"
              :step="0.05"
              :precision="2"
              class="full"
            />
          </el-form-item>
        </div>
        <el-form-item label="能力">
          <el-checkbox v-model="configForm.supports_tools">支持工具调用</el-checkbox>
          <el-checkbox v-model="configForm.supports_streaming">支持流式输出</el-checkbox>
          <el-checkbox v-model="configForm.supports_json">支持 JSON 输出</el-checkbox>
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="configForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="Extra JSON">
          <el-input
            v-model="configForm.extraText"
            type="textarea"
            :rows="4"
            class="mono-input"
            placeholder="{}"
          />
        </el-form-item>
        <div class="switch-row">
          <el-switch v-model="configForm.enabled" active-text="启用" />
          <el-switch v-model="configForm.is_default" active-text="设为默认模型" />
        </div>
      </el-form>
      <template #footer>
        <el-button @click="configVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingConfig" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.card {
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  padding: 10px 20px 18px;
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

.cap-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.full {
  width: 100%;
}

.grid-2 {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
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

.switch-row {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}

.mono-input :deep(textarea) {
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}
</style>
