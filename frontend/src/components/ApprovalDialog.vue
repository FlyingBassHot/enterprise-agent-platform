<script setup lang="ts">
import { ref, watch } from 'vue'
import JsonBlock from '@/components/JsonBlock.vue'
import StatusTag from '@/components/StatusTag.vue'

interface ApprovalPayload {
  approval_id: string
  tool_name: string
  tool_call_id?: string | null
  risk_level?: string
  arguments: Record<string, unknown>
}

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    approval: ApprovalPayload | null
    loading?: boolean
  }>(),
  { loading: false },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  decide: [approved: boolean, comment: string]
}>()

const comment = ref('')

watch(
  [() => props.modelValue, () => props.approval?.approval_id],
  ([visible]) => {
    if (visible) comment.value = ''
  },
)

function close() {
  emit('update:modelValue', false)
}

function decide(approved: boolean) {
  emit('decide', approved, comment.value.trim())
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="工具调用需要审批"
    width="min(560px, 92vw)"
    :close-on-click-modal="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <template v-if="approval">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="Agent 请求执行一个需要人工确认的工具调用，处理前执行将暂停。"
      />
      <div class="approval-body">
        <div class="row">
          <span class="label">工具</span>
          <span class="value mono">{{ approval.tool_name }}</span>
        </div>
        <div class="row">
          <span class="label">风险等级</span>
          <StatusTag :status="approval.risk_level || 'medium'" />
        </div>
        <div v-if="approval.tool_call_id" class="row">
          <span class="label">调用 ID</span>
          <span class="value mono">{{ approval.tool_call_id }}</span>
        </div>
        <div class="args">
          <div class="label">参数</div>
          <JsonBlock :value="approval.arguments" max-height="240px" />
        </div>
        <div class="comment">
          <div class="label">审批备注（可选）</div>
          <el-input
            v-model="comment"
            type="textarea"
            :rows="2"
            maxlength="500"
            show-word-limit
            placeholder="填写批准或拒绝的原因，会记录在审批历史中"
          />
        </div>
      </div>
    </template>
    <el-empty v-else description="没有待处理的审批" :image-size="72" />
    <template #footer>
      <el-button :disabled="loading" @click="close">稍后处理</el-button>
      <el-button type="danger" :loading="loading" :disabled="!approval" @click="decide(false)">
        拒绝
      </el-button>
      <el-button type="primary" :loading="loading" :disabled="!approval" @click="decide(true)">
        批准并继续
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.approval-body {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-top: 16px;
}

.row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.label {
  color: var(--app-muted);
  font-size: 12px;
}

.value {
  font-size: 12.5px;
  font-weight: 600;
  word-break: break-all;
}

.args,
.comment {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
</style>
