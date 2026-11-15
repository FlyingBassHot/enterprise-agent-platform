<template>
  <div class="not-found">
    <div class="code">404</div>
    <p>页面不存在或已被移除。</p>
    <el-button type="primary" @click="$router.push('/dashboard')">返回仪表盘</el-button>
  </div>
</template>

<style scoped>
.not-found {
  min-height: 100vh;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 12px;
  background: var(--app-bg);
}

.code {
  font-size: 72px;
  font-weight: 800;
  letter-spacing: -4px;
  color: var(--el-color-primary);
}

p {
  margin: 0 0 8px;
  color: var(--app-muted);
}
</style>
