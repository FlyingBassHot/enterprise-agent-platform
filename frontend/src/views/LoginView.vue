<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Lock, User } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()
const mode = ref<'login' | 'bootstrap'>('login')
const form = reactive({
  username: 'admin',
  password: '',
  display_name: 'Administrator',
  token: '',
})

async function submit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  try {
    if (mode.value === 'bootstrap') {
      await auth.bootstrap(form)
      ElMessage.success('管理员初始化成功')
    } else {
      await auth.login(form.username, form.password)
    }
    const redirect = (route.query.redirect as string) || '/dashboard'
    await router.replace(redirect)
  } catch {
    // interceptor shows the backend message
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-panel">
      <div class="login-brand">
        <div class="logo">AP</div>
        <h1>Enterprise Agent Platform</h1>
        <p>Agent 配置化 · 工具治理 · 执行可追踪 · 私有化可插拔</p>
      </div>

      <div class="login-card">
        <div class="mode-switch">
          <button :class="{ active: mode === 'login' }" @click="mode = 'login'">登录</button>
          <button :class="{ active: mode === 'bootstrap' }" @click="mode = 'bootstrap'">
            首次初始化
          </button>
        </div>

        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="用户名">
            <el-input v-model="form.username" size="large" :prefix-icon="User" autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input
              v-model="form.password"
              type="password"
              size="large"
              show-password
              :prefix-icon="Lock"
              autocomplete="current-password"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-form-item v-if="mode === 'bootstrap'" label="显示名称">
            <el-input v-model="form.display_name" size="large" />
          </el-form-item>
          <el-form-item v-if="mode === 'bootstrap'" label="初始化令牌（生产环境必填）">
            <el-input v-model="form.token" size="large" placeholder="APP_BOOTSTRAP_TOKEN" />
          </el-form-item>
          <el-button
            type="primary"
            size="large"
            class="submit"
            :loading="auth.loading"
            @click="submit"
          >
            {{ mode === 'bootstrap' ? '创建管理员并进入' : '进入平台' }}
          </el-button>
        </el-form>

        <p class="hint">
          {{ mode === 'bootstrap'
            ? '仅在平台首次部署、尚无用户时可用。'
            : '使用管理员分配的账号登录。权限由服务端强制执行。' }}
        </p>
      </div>

      <div class="login-foot">SQLite / 本地文件 / Mock 模型默认，可替换为 PostgreSQL / MinIO / vLLM</div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 32px;
  background:
    radial-gradient(900px 600px at 15% 10%, rgba(47, 107, 255, 0.18), transparent 60%),
    radial-gradient(800px 500px at 90% 90%, rgba(124, 58, 237, 0.14), transparent 60%),
    #0b1220;
}

.login-panel {
  width: min(920px, 100%);
  display: grid;
  grid-template-columns: 1.1fr 1fr;
  gap: 16px;
  align-items: center;
}

.login-brand {
  color: #fff;
  padding: 24px 20px;
}

.logo {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: 18px;
  background: linear-gradient(135deg, #2f6bff, #7c3aed);
  margin-bottom: 22px;
}

.login-brand h1 {
  font-size: 30px;
  margin: 0 0 12px;
  line-height: 1.25;
}

.login-brand p {
  color: #93a4c3;
  margin: 0;
  line-height: 1.7;
  font-size: 13px;
}

.login-card {
  background: rgba(255, 255, 255, 0.97);
  border-radius: 18px;
  padding: 26px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
}

.mode-switch {
  display: flex;
  gap: 6px;
  padding: 4px;
  border-radius: 10px;
  background: #f1f4fa;
  margin-bottom: 20px;
}

.mode-switch button {
  flex: 1;
  border: 0;
  background: transparent;
  padding: 8px 10px;
  border-radius: 8px;
  color: #5b6880;
  font-size: 13px;
  cursor: pointer;
}

.mode-switch button.active {
  background: #fff;
  color: #172033;
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(23, 32, 51, 0.12);
}

.submit {
  width: 100%;
  margin-top: 6px;
}

.hint {
  margin: 16px 0 0;
  color: #7b879d;
  font-size: 11px;
  line-height: 1.6;
}

.login-foot {
  grid-column: 1 / -1;
  text-align: center;
  color: #5d6d8c;
  font-size: 11px;
  margin-top: 8px;
}

@media (max-width: 760px) {
  .login-panel {
    grid-template-columns: 1fr;
  }
  .login-brand {
    display: none;
  }
}
</style>
