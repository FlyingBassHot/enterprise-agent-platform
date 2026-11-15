<script setup lang="ts">
import { computed } from 'vue'
import { formatDate } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    role: string
    content: string
    createdAt?: string | null
    streaming?: boolean
    pending?: boolean
    error?: string | null
  }>(),
  { createdAt: null, streaming: false, pending: false, error: null },
)

const meta = computed(() => {
  switch (props.role) {
    case 'user':
      return { label: '我', tone: 'user' }
    case 'assistant':
      return { label: 'Agent', tone: 'assistant' }
    case 'tool':
      return { label: '工具结果', tone: 'tool' }
    default:
      return { label: '系统', tone: 'system' }
  }
})

const showPlaceholder = computed(
  () => !props.content && (props.streaming || props.pending),
)
</script>

<template>
  <div class="message-row" :class="meta.tone">
    <div class="bubble">
      <div class="head">
        <span class="role">{{ meta.label }}</span>
        <span v-if="createdAt" class="time">{{ formatDate(createdAt) }}</span>
      </div>
      <div v-if="content" class="content">{{ content }}</div>
      <div v-else-if="showPlaceholder" class="content placeholder">
        正在生成<span class="cursor" />
      </div>
      <div v-else class="content placeholder">（空消息）</div>
      <div v-if="streaming && content" class="streaming-foot">
        <span class="dot" /> 生成中
      </div>
      <div v-if="error" class="error-line">{{ error }}</div>
    </div>
  </div>
</template>

<style scoped>
.message-row {
  display: flex;
  margin-bottom: 14px;
}

.message-row.user {
  justify-content: flex-end;
}

.message-row.assistant,
.message-row.tool,
.message-row.system {
  justify-content: flex-start;
}

.bubble {
  max-width: min(760px, 86%);
  border-radius: 14px;
  padding: 12px 14px;
  border: 1px solid var(--app-border);
  background: #fff;
  box-shadow: 0 1px 2px rgba(23, 32, 51, 0.03);
}

.user .bubble {
  background: linear-gradient(135deg, #2f6bff, #4b83ff);
  border-color: transparent;
  color: #fff;
}

.assistant .bubble {
  background: #fff;
}

.tool .bubble {
  background: #f8fafc;
  border-style: dashed;
}

.system .bubble {
  background: #fff8e6;
  border-color: #f5d68a;
}

.head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.role {
  font-size: 11px;
  font-weight: 650;
  letter-spacing: 0.3px;
  text-transform: uppercase;
}

.user .role {
  color: rgba(255, 255, 255, 0.82);
}

.assistant .role {
  color: var(--el-color-primary);
}

.time {
  font-size: 10.5px;
  color: var(--app-muted);
}

.user .time {
  color: rgba(255, 255, 255, 0.7);
}

.content {
  font-size: 13.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.placeholder {
  color: var(--app-muted);
  font-size: 12.5px;
}

.cursor {
  display: inline-block;
  width: 7px;
  height: 13px;
  margin-left: 3px;
  background: currentColor;
  vertical-align: -2px;
  animation: blink 1s step-end infinite;
}

.streaming-foot {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 11px;
  color: var(--app-muted);
}

.streaming-foot .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-color-primary);
  animation: pulse 1.1s ease-in-out infinite;
}

.error-line {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-color-danger);
}

@keyframes blink {
  50% {
    opacity: 0;
  }
}

@keyframes pulse {
  50% {
    opacity: 0.3;
  }
}
</style>
