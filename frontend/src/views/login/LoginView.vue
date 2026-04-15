<template>
  <div class="login-container">
    <div class="login-card">
      <div class="login-header">
        <h1 class="login-title">百度同步</h1>
        <p class="login-subtitle">请登录您的账户</p>
      </div>

      <form class="login-form" @submit.prevent="handleLogin">
        <div class="login-field">
          <input
            v-model="loginForm.username"
            name="username"
            type="text"
            autocomplete="username"
            placeholder="用户名"
            class="login-input"
            :disabled="loading"
          />
        </div>

        <div class="login-field">
          <input
            v-model="loginForm.password"
            name="current-password"
            type="password"
            autocomplete="current-password"
            placeholder="密码"
            class="login-input"
            :disabled="loading"
          />
        </div>

        <div class="login-button-item">
          <el-button
            type="primary"
            size="large"
            class="login-button"
            :loading="loading"
            native-type="submit"
          >
            {{ loading ? '登录中...' : '登录' }}
          </el-button>
        </div>
      </form>

      <div class="login-footer">
        <div class="version-info">
          <span>版本 {{ APP_VERSION }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { APP_VERSION } from '@/config/version'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const loading = ref(false)

// 表单数据
const loginForm = reactive({
  username: '',
  password: ''
})

// 处理登录
const handleLogin = async () => {
  if (!loginForm.username.trim()) {
    ElMessage.warning('请输入用户名')
    return
  }
  if (!loginForm.password) {
    ElMessage.warning('请输入密码')
    return
  }
  if (loginForm.password.length < 6) {
    ElMessage.warning('密码长度不能少于6位')
    return
  }

  loading.value = true

  try {
    await authStore.login(loginForm.username, loginForm.password)

    ElMessage.success('登录成功')

    const redirect = route.query.redirect as string
    await router.push(redirect || '/')
  } catch (error) {
    ElMessage.error(`登录失败: ${error}`)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-container {
  min-height: 100vh;
  display: flex;
  justify-content: center;
  align-items: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  padding: 20px;
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: white;
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
  padding: 40px;
  position: relative;
  overflow: hidden;
}

.login-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 4px;
  background: linear-gradient(90deg, #409eff, #67c23a, #e6a23c, #f56c6c);
}

.login-header {
  text-align: center;
  margin-bottom: 40px;
}

.login-title {
  font-size: 24px;
  font-weight: 600;
  color: #333;
  margin-bottom: 8px;
}

.login-subtitle {
  font-size: 14px;
  color: #666;
  margin: 0;
}

.login-form {
  margin-bottom: 20px;
}

.login-field {
  margin-bottom: 24px;
}

.login-input {
  width: 100%;
  min-height: 44px;
  padding: 0 14px;
  font-size: 14px;
  color: #303133;
  background: #fff;
  border: 1px solid #dcdfe6;
  border-radius: 8px;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}

.login-input:focus {
  outline: none;
  border-color: #409eff;
  box-shadow: 0 0 0 2px rgba(64, 158, 255, 0.12);
}

.login-input::placeholder {
  color: #a8abb2;
}

.login-button-item {
  margin-bottom: 0;
}

.login-button {
  width: 100%;
  height: 44px;
  font-size: 16px;
  font-weight: 500;
  border-radius: 8px;
}

.login-footer {
  text-align: center;
  padding-top: 20px;
  border-top: 1px solid #f0f0f0;
}

.version-info {
  font-size: 12px;
  color: #999;
}

@media (max-width: 480px) {
  .login-container {
    padding: 16px;
  }

  .login-card {
    padding: 30px 24px;
  }

  .login-title {
    font-size: 20px;
  }
}

@media (prefers-color-scheme: dark) {
  .login-card {
    background: #2d2d2d;
    color: #fff;
  }

  .login-title {
    color: #fff;
  }

  .login-subtitle,
  .version-info {
    color: #ccc;
  }

  .login-input {
    color: #f5f7fa;
    background: #1f1f1f;
    border-color: #4c4d4f;
  }

  .login-input::placeholder {
    color: #8d9095;
  }

  .login-footer {
    border-top-color: #4c4d4f;
  }
}
</style>
