<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: unknown; maxHeight?: string }>()
const text = computed(() => {
  if (props.value === undefined || props.value === null) return ''
  return JSON.stringify(props.value, null, 2)
})
</script>

<template>
  <pre class="json-block" :style="{ maxHeight: maxHeight || '320px' }">{{ text }}</pre>
</template>

<style scoped>
.json-block {
  margin: 0;
  padding: 12px 14px;
  border-radius: 10px;
  background: #0f172a;
  color: #d7e2f5;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.6;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
